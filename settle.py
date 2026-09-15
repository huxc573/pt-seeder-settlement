#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
保种组工资结算器（纯标准库，零第三方依赖）

读考核表（.xlsx / .csv）+ 配置（config.json）→ 输出工资表 CSV。
默认**完全离线**，不碰网络。

用法：

  python settle.py --selftest        # 离线自测，不读任何真实文件
  python settle.py                   # 读 roster.csv + config.json → 出工资表
  python settle.py --roster 考核表.xlsx
  python settle.py --refresh         # 顺便联网刷新每人的体积/数量（只读）
  python settle.py --period 2026-09  # 指定结算周期

计薪口径：config.json 里 plans[].salary = **组员实收**，脚本反算你要送出多少。
考核口径：config.json 里 assessment.metrics = ["volume"] / ["count"] / 两个都写。
"""

import argparse
import csv
import json
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import nexus
import roster as roster_mod

ROOT = nexus.DATA_ROOT
TB = 1024 ** 4

METRIC_LABELS = {"volume": "体积", "count": "数量"}


# ============================================================
# 配置
# ============================================================

def load_settings(config_path=None):
    """
    读 config.json。返回 (cfg, settings, path, is_example)。

    只做校验，不建 HTTP 会话 —— 离线计薪用不着 cookie。
    """
    p, is_ex = nexus.resolve_config_path(config_path, ROOT)
    if not p.exists():
        raise FileNotFoundError(
            f"找不到 {p}\n"
            f"复制 {nexus.CONFIG_EXAMPLE_NAME} 为 {nexus.CONFIG_NAME} 再改")
    try:
        cfg = nexus.read_jsonc(p)
    except json.JSONDecodeError as e:
        raise ValueError(f"{p} 不是合法 JSON（允许 // 注释）：{e}") from e

    return cfg, validate_settings(cfg, p), p, is_ex


def validate_settings(cfg, src="config.json"):
    """校验考核口径与方案，返回规范化后的 settings。"""
    where = f"{src}"

    metrics = (cfg.get("assessment") or {}).get("metrics")
    if metrics is None:
        metrics = ["volume"]
    if isinstance(metrics, str):
        metrics = [metrics]
    metrics = [str(m).strip().lower() for m in metrics]
    if not metrics:
        raise ValueError(f"{where} → assessment.metrics 是空的，至少写一项：volume 或 count")
    unknown = [m for m in metrics if m not in METRIC_LABELS]
    if unknown:
        raise ValueError(
            f"{where} → assessment.metrics 里有不认识的项 {unknown}，"
            f"只能填 volume / count")

    raw_plans = cfg.get("plans") or []
    if not raw_plans:
        raise ValueError(f"{where} → plans 是空的，至少要有一套方案")

    plans = {}
    for i, p in enumerate(raw_plans, 1):
        pid = str(p.get("id") or "").strip()
        if not pid:
            raise ValueError(f"{where} → plans 第 {i} 项没有 id")
        key = pid.upper()
        if key in plans:
            raise ValueError(f"{where} → plans 里方案 id 重复：{pid}")
        if p.get("salary") is None:
            raise ValueError(f"{where} → 方案 {pid} 没配 salary（月薪）")

        need = {"volume": "min_volume_tb", "count": "min_count"}
        for m in metrics:
            if p.get(need[m]) is None:
                raise ValueError(
                    f"{where} → 方案 {pid} 缺 {need[m]}。"
                    f"assessment.metrics 含 {m}，每个方案都必须配 {need[m]}")

        plans[key] = {
            "id": pid,
            "salary": int(p["salary"]),
            "min_volume_tb": p.get("min_volume_tb"),
            "min_count": p.get("min_count"),
        }

    payout = dict(cfg.get("payout") or {})
    payout.setdefault("tax_rate", 0.9)
    payout.setdefault("tax_flat", 4)
    payout.setdefault("interval_seconds", 10)
    payout.setdefault("message_template", "保种组 {period} 月薪 · {plan_id}")

    return {"metrics": metrics, "plans": plans, "payout": payout}


# ============================================================
# 税收反算
# ============================================================

def received_from_gift(gift, tax_rate, tax_flat):
    """送 gift，对方实收多少。"""
    return math.floor(gift * tax_rate - tax_flat)


def gift_for_received(target, tax_rate, tax_flat):
    """
    要让对方实收 target，需送出多少（向上取整，宁多不少）。

    >>> gift_for_received(200000, 0.9, 4)
    222227
    >>> gift_for_received(400000, 0.9, 4)
    444449
    >>> gift_for_received(600000, 0.9, 4)
    666672
    """
    return int(math.ceil((target + tax_flat) / tax_rate))


# ============================================================
# 考核
# ============================================================

def _check_one(metric, plan, member):
    """
    单项考核。返回 (状态, 差额文本)。
    状态：达标 / 不达标 / 未测
    """
    if metric == "volume":
        quota = plan["min_volume_tb"] * TB
        got = member.measured_bytes
        gap_text = f"缺 {plan['min_volume_tb'] - (member.measured_tb or 0):.3f} TB"
    else:
        quota = plan["min_count"]
        got = member.measured_count
        gap_text = f"缺 {int(quota - (member.measured_count or 0))} 个"

    if got is None:
        return "未测", ""
    if got >= quota:
        return "达标", ""
    return "不达标", gap_text


def _result_note(status, plan, metrics, member):
    """
    工资表「结果」列 = **计算结果**，只写数据本身 —— 达标 / 不达标 / 未测
    前面「考核」列已经有了，这里不再重复：
      · 达标 / 不达标：实测值 / 要求值
      · 未测：写明抓取失败的原因（网络中断、HTTP 码、登录态失效、模板对不上……）
    实测值没拿到就留空，绝不写 0 —— 「0 做种」和「没抓到」是两回事。
    """
    if plan is None:
        return f"没有方案：{member.plan_id or '(空)'} 不在 config.json 的 plans 里"
    if status == "未测":
        return member.measure_error or "没有取到实测数据"
    got, quota = [], []
    if "volume" in metrics:
        got.append(f"{member.measured_tb:.3f} TB"
                   if member.measured_tb is not None else "—")
        quota.append(f"{plan['min_volume_tb']:g} TB")
    if "count" in metrics:
        got.append(f"{member.measured_count} 个"
                   if member.measured_count is not None else "—")
        quota.append(f"{plan['min_count']:g} 个")
    return f"{' + '.join(got)} / 要求 {' + '.join(quota)}"


def evaluate(member, plan, settings):
    """返回一条工资记录（dict）。"""
    metrics = settings["metrics"]
    payout = settings["payout"]
    salary = plan["salary"] if plan else None

    if salary is None:
        target_recv = gift = tax = None
    else:
        target_recv = salary
        gift = gift_for_received(salary, payout["tax_rate"], payout["tax_flat"])
        tax = gift - received_from_gift(gift, payout["tax_rate"], payout["tax_flat"])

    # --- 考核：多选时全部达标才算达标 ---
    if plan is None:
        status, gaps = "无方案", []
    else:
        results = [_check_one(m, plan, member) for m in metrics]
        gaps = [t for _, t in results if t]
        if any(s == "未测" for s, _ in results):
            status = "未测"
        elif all(s == "达标" for s, _ in results):
            status = "达标"
        else:
            status = "不达标"

    # --- 方案要求 / 实测 的可读文本 ---
    quota_bits = []
    if plan is not None:
        if "volume" in metrics:
            quota_bits.append(f"{plan['min_volume_tb']:g} TB")
        if "count" in metrics:
            quota_bits.append(f"{plan['min_count']:g} 个")

    return {
        "row_no": member.row_no,
        "uid": member.uid if member.uid is not None else "",
        "username": member.username,
        "plan_id": member.plan_id,
        "check_raw": member.check_raw,
        "measured_tb": member.measured_tb,
        "measured_count": member.measured_count,
        # 「体积」列：单位写进值里（5.725T），表头不再带 (TB)
        "vol_text": (f"{member.measured_tb:.3f}T"
                     if member.measured_tb is not None else ""),
        "quota_text": " + ".join(quota_bits),
        "status": status,
        "gap_text": "；".join(gaps),
        "salary": target_recv,
        "gift": gift,
        "tax": tax,
        "note": _result_note(status, plan, metrics, member),
    }


def build_payroll(members, settings):
    rows, errors = [], []
    for m in members:
        plan = settings["plans"].get(m.plan_id)
        if plan is None:
            who = m.username or (f"uid {m.uid}" if m.uid is not None else "未知成员")
            errors.append(
                f"第 {m.row_no} 行：{who} 的方案 {m.plan_id!r} 不在 config.json 的 plans 里")
            continue
        rows.append(evaluate(m, plan, settings))
    return rows, errors


# ============================================================
# 联网刷新体积/数量（只读）
# ============================================================

def _compose_check_raw(date, member):
    """检查日期格：日期-体积T(数量)，如 260919-5.725T(1839)。
    有测到谁就写谁 —— 考核口径只管判定，展示恒两样都给。"""
    bits = []
    if member.measured_tb is not None:
        bits.append(f"{member.measured_tb:.3f}T")
    if member.measured_count is not None:
        bits.append(f"({member.measured_count})")
    if not bits:
        return ""
    return date + "-" + "".join(bits)


def _save_snapshot(snapshot_dir, state, uid, html, log):
    """把站点返回的页面原样存进 samples/（.gitignore 只放行 *.sample.html），
    解析出问题时有据可查。总量封顶，别把目录撑爆。"""
    if snapshot_dir is None or not html or state["n"] >= state["cap"]:
        return
    try:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        p = snapshot_dir / f"debug_userdetails_{uid}_{state['n']}.html"
        p.write_text(html, encoding="utf-8")
        state["n"] += 1
        log(f"  [i] 页面快照已存 {p.name}（已被 .gitignore 覆盖，不会提交）")
    except OSError as e:
        log(f"  [!] 快照写不进去：{e}")


# 单次请求失败就重试的状态码：0 = 请求根本没成（断连 / SSL / 超时），
# 429 / 5xx = 站点自己说「我这边出问题了」。这些都是**只读**请求，重试绝对安全。
REFRESH_RETRY_STATUS = (0, 408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524)
REFRESH_ATTEMPTS = 3               # 1 次原始 + 2 次重试
REFRESH_BACKOFF = 1.0              # 第 n 次重试前等 n 秒


def brief_reason(body, limit=70):
    """把 HTTP 层的异常文本缩成一句人话，写进「结果」列。"""
    t = " ".join(str(body or "").replace("__EXCEPTION__", "").split())
    return (t[:limit] + "…") if len(t) > limit else t


def fetch_with_retry(session, path, attempts=REFRESH_ATTEMPTS, log=None,
                     backoff=REFRESH_BACKOFF, sleep=time.sleep):
    """抓一个页面，瞬时失败（断连 / SSL 抖动 / 站点 5xx）自动重试。

    详情页请求偶发 `SSL: UNEXPECTED_EOF_WHILE_READING`（连接被掐断），
    并发时更频繁。只读请求重试是安全的，不重试就会白白多出一批「未测」。
    4xx（除了 408/425/429）说明请求本身有问题，重试没用，直接返回。
    """
    st, body, final = 0, "", ""
    for k in range(max(1, attempts)):
        st, body, final = session.request(path)
        if st not in REFRESH_RETRY_STATUS:
            return st, body, final
        if k + 1 < max(1, attempts):
            why = "网络中断" if st == 0 else f"HTTP {st}"
            if log is not None:
                log(f"  [i] {path} → {why}（{brief_reason(body, 40)}），"
                    f"{backoff * (k + 1):g} 秒后重试")
            sleep(backoff * (k + 1))
    return st, body, final


def refresh_measurements(members, metrics, sess, ep, delay=1.0, log=print,
                         progress=None, snapshot_dir=None, workers=1,
                         session_factory=None, on_member=None,
                         attempts=REFRESH_ATTEMPTS, backoff=REFRESH_BACKOFF):
    """逐人抓取当前做种汇总，回填 measured_*。只读操作，无赠送限制。

    数据来自**个人详情页** userdetails.php —— 页面里本来就有
    「N 条记录 | 总大小：X TB」，不需要单独配做种列表接口。
    详情页解析不出、且配置里恰好写了 seeding_list 时，才去抓列表页兜底。

    log 可以换成别的可调用对象（GUI 里用来把逐人进度推到界面）。
    progress(i, n, member, text) 每人报进度（串行时抓之前报一次、抓完带实测值
    再报一次；并发时只在抓完报一次，i 是**已处理完的人数**）。
    on_member(member) 每人处理完（成功 / 失败 / 跳过都算）回调一次 ——
    GUI 拿它逐行刷表格，比「数同一个人报了几次进度」稳。
    snapshot_dir 给了就把前几张详情页原样存档，用户名认不出时必存 ——
    各站模板千奇百怪，有快照才排得动模板问题。

    workers > 1 走并发：单人耗时几乎全是干等站点响应，并发就是把这段等待叠起来
    （20 人 4 路 ≈ 快 4 倍）。总请求速率仍由每路的 delay 控制，不会突然打爆站点；
    每路一条自己的会话（session_factory 造），不共用 cookie jar。
    workers = 1（默认）保持原来的一个人一个人抓。

    抓不到就重试（`attempts`，默认 1 次原始 + 2 次重试），站点 SSL 抖动很常见。

    **「站点说没有记录」和「没抓到」必须分清**：
      · 响应里写着「没有记录」→ 真的 0 条，按 0 记 —— 0 必然不达标；
      · 请求失败 / 页面里认不出做种数据 → 记 `measure_error`，**实测列一律留空**
        （填 0 会把「没抓到」显示成「0 做种」，看起来是不达标，其实是数据没拿到）。
    """
    if not ep.has("user_details"):
        raise ValueError("config.json 的 endpoints 里没配 user_details，无法联网刷新")

    total = len(members)
    if not total:
        return 0, 0

    today = datetime.now().strftime("%y%m%d")
    lock = threading.Lock()                       # 并发时保护计数器和快照序号
    snap = {"n": 0, "cap": 8}
    stats = {"ok": 0, "fail": 0, "done": 0}

    def take_snapshot(uid, html):
        with lock:
            _save_snapshot(snapshot_dir, snap, uid, html, log)

    def fail(m, why, tag):
        """采集失败：实测列一律清空（不写 0），原因挂在 member 上给「结果」列用。"""
        m.measured_bytes = None
        m.measured_tb = None
        m.measured_count = None
        m.check_date = None
        m.check_raw = ""
        m.measure_error = why
        log(f"  {tag} {(m.username or f'uid {m.uid}'):<16} [x] {why}")
        return "fail", why

    def capture(idx, m, session):
        """抓一个人并回填。返回 (结果, 尾巴文字)，结果是 ok / fail / skip。"""
        tag = f"[{idx}/{total}]"
        if m.uid is None:
            log(f"  {tag} {m.username or '?':<16} 跳过（无 uid）")
            return "skip", "跳过（无 uid）"

        st, html, _ = fetch_with_retry(
            session, ep.path("user_details", uid=m.uid),
            attempts=attempts, log=log, backoff=backoff)
        if st != 200:
            return fail(m, "网络中断（%s）" % brief_reason(html) if st == 0
                        else f"详情页 HTTP {st}", tag)
        if nexus.looks_like_login(html):
            return fail(m, "登录态失效（站点把详情页跳回了登录页）", tag)

        # 第一张页面无论解析成败都存一张 —— 出问题有据可查
        take_snapshot(m.uid, html)

        # 用户名以站点为准（用户可能改名）：详情页上是谁就记谁。
        # 考核表里没填用户名的行也在这里补上。
        info = nexus.parse_userdetails(html)
        uname = info.get("username") or nexus.guess_username_from_html(html, m.uid)
        if uname and uname != m.username:
            log(f"  {tag} 用户名 {m.username or '(空)'} → {uname}（以站点为准）")
            m.username = uname

        count, size = nexus.parse_userdetails_seeding(html)
        if count == 0 and size is None:
            size = 0                          # 页面上明写「0 条记录」→ 体积必然 0

        why = ""
        if size is None and ep.has("seeding_list"):
            # 兜底（二次开发站的真身）：做种数据在 AJAX 列表接口里，详情页没有汇总行。
            # 有的站点详情页永远解析不出汇总，只能走做种列表逐行累加。
            st2, html2, _ = fetch_with_retry(
                session, ep.path("seeding_list", uid=m.uid, page=1),
                attempts=attempts, log=log, backoff=backoff)
            if st2 == 0:
                why = f"做种列表抓取失败：网络中断（{brief_reason(html2)}）"
            elif st2 != 200:
                why = f"做种列表 HTTP {st2}"
            elif nexus.looks_like_login(html2):
                why = "做种列表跳回登录页（登录态失效）"
            elif nexus.says_no_record(html2):
                count = size = 0
            else:
                c2, s2 = nexus.parse_seeding_summary(html2)
                if s2 is None:
                    trows = nexus.parse_seeding_rows(html2)
                    if trows:
                        s2 = sum(r["size_bytes"] for r in trows)
                        c2 = c2 if c2 is not None else len(trows)
                if s2 is None:
                    why = "做种列表认不出汇总行（站点模板和解析对不上）"
                    take_snapshot(m.uid, html2)     # 存下来才能照着改解析
                else:
                    count = c2 if c2 is not None else count
                    size = s2
        elif size is None:
            why = "详情页没有做种数据（也没配 seeding_list 接口）"

        if (("volume" in metrics and size is None)
                or ("count" in metrics and count is None)):
            return fail(m, why or "没拿到做种数据", tag)

        m.measured_bytes = size
        m.measured_tb = (size / TB) if size is not None else None
        m.measured_count = count
        m.check_date = today
        m.check_raw = _compose_check_raw(today, m)
        m.measure_error = ""

        show = []
        if "volume" in metrics:
            show.append(f"{m.measured_tb:.3f} TB" if m.measured_tb is not None else "体积?")
        if "count" in metrics:
            show.append(f"{m.measured_count} 个" if m.measured_count is not None else "数量?")
        if not m.username:
            log(f"  {tag} uid {m.uid} [!] 实测成功，但页面里认不出用户名"
                " —— 工资表该行「用户名」会空着，请核对")
        log(f"  {tag} {(m.username or f'uid {m.uid}'):<16} " + " | ".join(show))
        return "ok", " | ".join(show)

    # ---- 串行（默认）：进度按「抓前 / 抓后」上报两次
    if not workers or workers <= 1 or total == 1:
        for i, m in enumerate(members, 1):
            if progress is not None:
                progress(i, total, m,
                         f"联网刷新 {i}/{total} · uid {m.uid} {m.username or ''}")
            result, tail = capture(i, m, sess)
            if on_member is not None:
                on_member(m)
            if result == "skip":
                continue
            stats[result] += 1
            if progress is not None:
                progress(i, total, m,
                         f"联网刷新 {i}/{total} · "
                         f"{m.username or f'uid {m.uid}'} · {tail}")
            time.sleep(delay)
        return stats["ok"], stats["fail"]

    # ---- 并发：把「等站点响应」的时间叠起来
    local = threading.local()

    def session_for():
        s = getattr(local, "sess", None)
        if s is None:
            s = session_factory() if session_factory is not None else sess
            local.sess = s
        return s

    def one(idx, m):
        result, tail = capture(idx, m, session_for())
        if on_member is not None:
            on_member(m)
        with lock:
            stats["done"] += 1
            done_n = stats["done"]
            if result != "skip":
                stats[result] += 1
        if progress is not None:
            progress(done_n, total, m,
                     f"联网刷新 {done_n}/{total} · "
                     f"{m.username or f'uid {m.uid}'} · {tail}")
        if delay:
            time.sleep(delay)          # 每路自己的节奏，站点压力不变
        return result

    with ThreadPoolExecutor(max_workers=max(1, min(workers, total))) as pool:
        list(pool.map(one, range(1, total + 1), members))
    return stats["ok"], stats["fail"]


# ============================================================
# 输出
# ============================================================

def build_columns(metrics):
    """列顺序对齐考核表：UID / 方案 / 用户名 / 检查日期，后面才是脚本加的列。
    体积 / 数量**恒显示** —— 考核口径（metrics）只影响判定和「方案要求」，
    不影响展示：抓数据本来就是两样一起抓的。（metrics 参数保留只为签名兼容）
    """
    return [("uid", "UID"), ("plan_id", "方案"),
            ("username", "用户名"), ("check_raw", "检查日期"),
            ("vol_text", "体积"), ("measured_count", "数量"),
            ("quota_text", "方案要求"), ("status", "考核"),
            ("salary", "月薪(实收)"), ("gift", "应赠送"),
            ("gap_text", "差额"), ("note", "结果")]


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.3f}"
    return v


def write_payroll_csv(rows, columns, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([h for _, h in columns])
        for r in rows:
            w.writerow([fmt(r.get(k)) for k, _ in columns])


def print_report(rows, errors, settings, period):
    metrics = settings["metrics"]
    payout = settings["payout"]

    head = f"{'UID':>7}  {'方案':<5} {'用户名':<16}  {'检查日期':<22}"
    if "volume" in metrics:
        head += f"{'体积':>9}"
    if "count" in metrics:
        head += f"{'数量':>7}"
    head += f" {'考核':<7}{'实收':>9}{'应赠':>9}"
    width = len(head) + 12

    print()
    print("=" * width)
    print(f"保种组工资表 · {period}   （考核：{' + '.join(METRIC_LABELS[m] for m in metrics)}）")
    print("=" * width)
    print(head)
    print("-" * width)

    for r in rows:
        line = (f"{str(r['uid']):>7}  {r['plan_id'] or '-':<5} "
                f"{r['username'] or '-':<16}  {r['check_raw'] or '-':<22}")
        if "volume" in metrics:
            vol = f"{r['measured_tb']:.3f}T" if r["measured_tb"] is not None else "-"
            line += f"{vol:>9}"
        if "count" in metrics:
            cnt = str(r["measured_count"]) if r["measured_count"] is not None else "-"
            line += f"{cnt:>7}"
        line += f" {r['status']:<7}{str(r['salary'] or ''):>9}{str(r['gift'] or ''):>9}"
        if r["status"] != "达标":
            line += "  <<<"
        print(line)

    print("-" * width)

    by_plan = {}
    for r in rows:
        by_plan.setdefault(r["plan_id"], []).append(r)

    for pid, rs in sorted(by_plan.items()):
        sal = settings["plans"][pid]["salary"] if pid in settings["plans"] else 0
        g = sum(r["gift"] or 0 for r in rs)
        print(f"  {pid:<5} {len(rs):>2} 人  月薪 {sal:>7}  →  送出合计 {g:>9}")

    total_gift = sum(r["gift"] or 0 for r in rows)
    total_recv = sum(r["salary"] or 0 for r in rows)
    print()
    print(f"人数        : {len(rows)}")
    print(f"组员实收合计: {total_recv:,}")
    print(f"应赠总额    : {total_gift:,}")
    print(f"税收损耗    : {total_gift - total_recv:,}")
    print(f"时间成本    : {len(rows)} 人 × {payout['interval_seconds']} 秒 "
          f"≈ {len(rows) * payout['interval_seconds'] / 60:.1f} 分钟")

    bad = [r for r in rows if r["status"] == "不达标"]
    untested = [r for r in rows if r["status"] == "未测"]
    if bad:
        print()
        print(f"[!] 不达标 {len(bad)} 人（按约定：只标记，发不发你定）：")
        for r in bad:
            print(f"    {r['username']:<16} {r['plan_id']:<5} {r['gap_text']}")
    if untested:
        print()
        print(f"[!] 未采集到数据 {len(untested)} 人："
              + ", ".join(r["username"] for r in untested))
        print("    考核表里那格是空的，或写法不认识。"
              "体积写法：260815-3.560 TB ／ 数量写法：260815-4893 条")
    if errors:
        print()
        print("[x] 数据错误：")
        for e in errors:
            print("   ", e)


# ============================================================
# 离线自测
# ============================================================

def _make_min_xlsx(path, rows):
    """生成一个最小可用的 .xlsx，用来验证读表逻辑。rows 是字符串二维表。"""
    import zipfile

    shared, index = [], {}
    for row in rows:
        for cell in row:
            if cell not in index:
                index[cell] = len(shared)
                shared.append(cell)

    sst = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           f'count="{len(shared)}" uniqueCount="{len(shared)}">'
           + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")

    def xml_row(r, values):
        cells = "".join(
            f'<c r="{chr(65 + i)}{r}" t="s"><v>{index[v]}</v></c>'
            for i, v in enumerate(values))
        return f'<row r="{r}">{cells}</row>'

    sheet = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             "<sheetData>"
             + "".join(xml_row(i, r) for i, r in enumerate(rows, 1))
             + "</sheetData></worksheet>")

    wb = ('<?xml version="1.0" encoding="UTF-8"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets><sheet name="考核" sheetId="1" r:id="rId1"/></sheets></workbook>')

    rels = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>')

    ct = ('<?xml version="1.0" encoding="UTF-8"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="xml" ContentType="application/xml"/></Types>')

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/sharedStrings.xml", sst)
        z.writestr("xl/worksheets/sheet1.xml", sheet)


def _settings(metrics, plans=None):
    cfg = {
        "assessment": {"metrics": metrics},
        "plans": plans or [
            {"id": "3T", "min_volume_tb": 3, "min_count": 300, "salary": 200000},
            {"id": "6T", "min_volume_tb": 6, "min_count": 600, "salary": 400000},
            {"id": "12T", "min_volume_tb": 12, "min_count": 1200, "salary": 600000},
        ],
        "payout": {"tax_rate": 0.9, "tax_flat": 4, "interval_seconds": 10},
    }
    return validate_settings(cfg, "<selftest>")


def selftest():
    import tempfile

    print("=" * 66)
    print("离线自测：计薪 / 考核 / 读表 / 配置校验")
    print("=" * 66)
    fails = []

    def check(label, got, want):
        ok = got == want
        print(f"  [{'OK' if ok else 'XX'}] {label}: {got}" + ("" if ok else f"  (期望 {want})"))
        if not ok:
            fails.append(f"{label}: 期望 {want}，实际 {got}")

    def raises(label, fn):
        try:
            fn()
        except (ValueError, KeyError) as e:
            print(f"  [OK] {label}: 抛错 -> {str(e)[:70]}")
            return
        print(f"  [XX] {label}: 没抛错")
        fails.append(f"{label}: 应该抛错但没抛")

    print("\n-- 税收反算（月薪 = 组员实收）--")
    for salary, want in ((200000, 222227), (400000, 444449), (600000, 666672)):
        g = gift_for_received(salary, 0.9, 4)
        check(f"实收 {salary:,} → 应赠", g, want)
        check(f"  校验 {g} 送出后实收 ≥ 目标",
              received_from_gift(g, 0.9, 4) >= salary, True)
    check("送 450000 → 实收（截图实测）", received_from_gift(450000, 0.9, 4), 404996)

    print("\n-- 考核单元格解析 --")
    c = roster_mod.parse_check_cell("260815-3.560 TB")
    check("体积·日期", c["date"], "260815")
    check("体积·TB", round(c["size_tb"], 3), 3.56)
    check("体积·数量为空", c["count"], None)
    c = roster_mod.parse_check_cell("260815-4893 条")
    check("数量·日期", c["date"], "260815")
    check("数量·值", c["count"], 4893)
    check("数量·体积为空", c["size_bytes"], None)
    c = roster_mod.parse_check_cell("260815-3.560 TB / 4893个")
    check("两样都有·体积", round(c["size_tb"], 3), 3.56)
    check("两样都有·数量", c["count"], 4893)
    check("裸小数当体积", round(roster_mod.parse_check_cell("260815-3.560")["size_tb"], 3), 3.56)
    check("裸整数当数量", roster_mod.parse_check_cell("260815-4893")["count"], 4893)
    check("空单元格", roster_mod.parse_check_cell("")["size_bytes"], None)
    check("乱文本不猜", roster_mod.parse_check_cell("no-data")["count"], None)
    check("8 位日期", roster_mod.parse_check_cell("2026-08-15-3.5 TB")["date"], "2026-08-15")
    check("短写单位 G", roster_mod.parse_check_cell("260815-512 G")["size_bytes"], 512 * 1024 ** 3)
    c = roster_mod.parse_check_cell("260919-5.725T(1839)")
    check("新检查日期格·日期", c["date"], "260919")
    check("新检查日期格·体积", round(c["size_tb"], 3), 5.725)
    check("新检查日期格·数量", c["count"], 1839)
    check("只有数量也认", roster_mod.parse_check_cell("260919-(1839)")["count"], 1839)

    print("\n-- 检查日期格合成（日期-体积T(数量)）--")
    check("两样都有",
          _compose_check_raw("260919", roster_mod.Member(
              uid=1, username="u", plan_id="3T",
              measured_bytes=int(5.725 * TB), measured_tb=5.725,
              measured_count=1839)),
          "260919-5.725T(1839)")
    check("只有体积",
          _compose_check_raw("260919", roster_mod.Member(
              uid=1, username="u", plan_id="3T",
              measured_bytes=int(5.725 * TB), measured_tb=5.725)),
          "260919-5.725T")
    check("只有数量",
          _compose_check_raw("260919", roster_mod.Member(
              uid=1, username="u", plan_id="3T", measured_count=1839)),
          "260919-(1839)")
    check("什么都没有 → 空",
          _compose_check_raw("260919", roster_mod.Member(
              uid=1, username="u", plan_id="3T")), "")

    print("\n-- 考核判定：单选体积 --")
    s_vol = _settings(["volume"])
    mk = lambda **kw: roster_mod.Member(uid=1, username="u", plan_id="3T", **kw)
    check("够 → 达标", evaluate(mk(measured_bytes=int(3.5 * TB), measured_tb=3.5),
                                s_vol["plans"]["3T"], s_vol)["status"], "达标")
    r = evaluate(mk(measured_bytes=int(2.5 * TB), measured_tb=2.5),
                 s_vol["plans"]["3T"], s_vol)
    check("不够 → 不达标", r["status"], "不达标")
    check("  差额文本", r["gap_text"], "缺 0.500 TB")
    check("没测 → 未测", evaluate(mk(), s_vol["plans"]["3T"], s_vol)["status"], "未测")
    check("应赠仍照算", evaluate(mk(), s_vol["plans"]["3T"], s_vol)["gift"], 222227)

    print("\n-- 结果列 = 计算结果（不带状态字样，考核列已有）--")
    check("达标 → 实测 / 要求",
          evaluate(mk(measured_bytes=int(5.711 * TB), measured_tb=5.711),
                   s_vol["plans"]["3T"], s_vol)["note"], "5.711 TB / 要求 3 TB")
    check("不达标 → 也写清实测 / 要求", r["note"], "2.500 TB / 要求 3 TB")
    check("未测 → 写清为什么没抓到",
          evaluate(mk(measure_error="网络中断（URLError: 断了）"),
                   s_vol["plans"]["3T"], s_vol)["note"],
          "网络中断（URLError: 断了）")
    check("未测但没记原因 → 也给一句话",
          evaluate(mk(), s_vol["plans"]["3T"], s_vol)["note"], "没有取到实测数据")
    check("没方案 → 点明是配置问题",
          evaluate(roster_mod.Member(uid=1, username="u", plan_id="9T"),
                   None, s_vol)["note"],
          "没有方案：9T 不在 config.json 的 plans 里")
    check("0 做种是准确的实测值（不是缺数据）",
          evaluate(mk(measured_bytes=0, measured_tb=0.0),
                   s_vol["plans"]["3T"], s_vol)["note"],
          "0.000 TB / 要求 3 TB")

    print("\n-- 考核判定：单选数量 --")
    s_cnt = _settings(["count"])
    check("够 → 达标", evaluate(mk(measured_count=350), s_cnt["plans"]["3T"], s_cnt)["status"],
          "达标")
    r = evaluate(mk(measured_count=293), s_cnt["plans"]["3T"], s_cnt)
    check("不够 → 不达标", r["status"], "不达标")
    check("  差额文本", r["gap_text"], "缺 7 个")

    print("\n-- 考核判定：多选（体积 + 数量，全达标才算） --")
    s_both = _settings(["volume", "count"])
    check("都够 → 达标",
          evaluate(mk(measured_bytes=int(3.2 * TB), measured_tb=3.2, measured_count=300),
                   s_both["plans"]["3T"], s_both)["status"], "达标")
    r = evaluate(mk(measured_bytes=int(3.2 * TB), measured_tb=3.2, measured_count=250),
                 s_both["plans"]["3T"], s_both)
    check("数量不够 → 不达标", r["status"], "不达标")
    check("  差额只说缺的那项", r["gap_text"], "缺 50 个")
    r = evaluate(mk(measured_bytes=int(2.0 * TB), measured_tb=2.0, measured_count=250),
                 s_both["plans"]["3T"], s_both)
    check("两项都缺", r["gap_text"], "缺 1.000 TB；缺 50 个")

    print("\n-- 配置校验：该报错的要报错 --")
    raises("metrics 里有鬼", lambda: validate_settings(
        {"assessment": {"metrics": ["seedtime"]}, "plans": [{"id": "a", "salary": 1}]}))
    raises("metrics 空数组", lambda: validate_settings(
        {"assessment": {"metrics": []}, "plans": [{"id": "a", "salary": 1, "min_volume_tb": 1}]}))
    raises("plans 空", lambda: validate_settings(
        {"plans": []}))
    raises("方案缺 salary", lambda: validate_settings(
        {"plans": [{"id": "3T", "min_volume_tb": 3}]}))
    raises("考核体积但方案没配 min_volume_tb", lambda: validate_settings(
        {"assessment": {"metrics": ["volume"]}, "plans": [{"id": "3T", "salary": 1}]}))
    raises("考核数量但方案没配 min_count", lambda: validate_settings(
        {"assessment": {"metrics": ["count"]},
         "plans": [{"id": "3T", "salary": 1, "min_volume_tb": 3}]}))
    raises("方案 id 重复", lambda: validate_settings(
        {"plans": [{"id": "3T", "salary": 1, "min_volume_tb": 3},
                   {"id": "3t", "salary": 2, "min_volume_tb": 4}]}))
    check("默认 metrics（没写 assessment）",
          validate_settings({"plans": [{"id": "a", "salary": 1, "min_volume_tb": 1}]})["metrics"],
          ["volume"])

    print("\n-- 读 .xlsx（零依赖 zipfile+xml）--")
    s = _settings(["volume"])
    with tempfile.TemporaryDirectory() as td:
        xp = Path(td) / "t.xlsx"
        _make_min_xlsx(xp, [
            ["UID", "方案", "用户名", "检查日期", "备注"],
            ["10001", "3T", "user001", "260815-3.560 TB", ""],
            ["10004", "6T", "user002", "260815-5.645 TB", "待观察"],
            ["10003", "12T", "user003", "260815-73.735 TB", ""],
        ])
        ms = roster_mod.load_roster(xp)
        check("读到人数", len(ms), 3)
        check("第 1 人 uid", ms[0].uid, 10001)
        check("第 1 人用户名", ms[0].username, "user001")
        check("第 1 人方案", ms[0].plan_id, "3T")
        check("第 1 人体积", round(ms[0].measured_tb, 3), 3.56)
        check("第 1 人原始格原样保留", ms[0].check_raw, "260815-3.560 TB")
        check("备注", ms[1].note, "待观察")

        # WPS 把 xlsx 另存成 .csv 扩展名时，只认扩展名会拿 zip 当文本读
        mis = Path(td) / "wrong.csv"
        mis.write_bytes(xp.read_bytes())
        check("扩展名是 .csv 的 xlsx 按内容认出来", len(roster_mod.load_roster(mis)), 3)

        rows, errs = build_payroll(ms, s)
        check("无错误", errs, [])
        by = {r["username"]: r for r in rows}
        check("user001 达标", by["user001"]["status"], "达标")
        check("user002 不达标（5.645 < 6T）", by["user002"]["status"], "不达标")
        check("user003 达标", by["user003"]["status"], "达标")
        check("user001 应赠", by["user001"]["gift"], 222227)
        check("user002 应赠（不达标也照算额）", by["user002"]["gift"], 444449)

        out = Path(td) / "p.csv"
        write_payroll_csv(rows, build_columns(s["metrics"]), out)
        check("CSV 写成功", out.exists() and out.stat().st_size > 0, True)
        header = out.read_text(encoding="utf-8-sig").splitlines()[0]
        check("表头顺序对齐考核表",
              header.split(",")[:4], ["UID", "方案", "用户名", "检查日期"])

    print("\n-- 用户名列可以整个没有（用户名联网时从详情页自动取）--")
    with tempfile.TemporaryDirectory() as td:
        cp = Path(td) / "nouser.csv"
        cp.write_text("UID,方案,备注\n10001,3T,x\n10004,6T,\n", encoding="utf-8-sig")
        ms = roster_mod.load_roster(cp)
        check("读到人数", len(ms), 2)
        check("用户名为空不炸", ms[0].username, "")
        check("uid 照读", ms[1].uid, 10004)

    print("\n-- 读 .csv + 表头乱序 + 数量列 --")
    s_cnt = _settings(["count"])
    with tempfile.TemporaryDirectory() as td:
        cp = Path(td) / "t.csv"
        cp.write_text(
            "用户名,方案,UID,检查日期,做种数量,备注\n"
            "user001,3T,10001,260815,4893,\n"
            "user002,3T,10004,,,\n",
            encoding="utf-8-sig")
        ms = roster_mod.load_roster(cp)
        check("读到人数", len(ms), 2)
        check("列乱序也能对", (ms[0].uid, ms[0].plan_id, ms[0].note), (10001, "3T", ""))
        check("复合格裸整数→数量", ms[0].measured_count, 4893)
        rows, _ = build_payroll(ms, s_cnt)
        check("达标", rows[0]["status"], "达标")
        check("空数据 → 未测", rows[1]["status"], "未测")

    print("\n-- 输出列：体积 / 数量恒显示（不随考核口径增减）--")
    cols_vol = [h for _, h in build_columns(["volume"])]
    cols_cnt = [h for _, h in build_columns(["count"])]
    cols_none = [h for _, h in build_columns([])]
    for name, cols in (("体积口径", cols_vol), ("数量口径", cols_cnt), ("空口径", cols_none)):
        check(f"{name}有 体积", "体积" in cols, True)
        check(f"{name}有 数量", "数量" in cols, True)
    check("旧表头 实测体积(TB) 不再出现", "实测体积(TB)" in cols_none, False)

    print("\n-- 未知方案要报错，不能静默 --")
    ms = [roster_mod.Member(uid=1, username="ghost", plan_id="99T")]
    rows, errs = build_payroll(ms, s)
    check("报出错误", len(errs), 1)
    check("该行被剔除", rows, [])

    print()
    print("=" * 66)
    if fails:
        print("自测失败：")
        for x in fails:
            print("  x", x)
        return False
    print("自测全部通过 ✅")
    return True


# ============================================================

def main():
    ap = argparse.ArgumentParser(description="保种组工资结算器")
    ap.add_argument("--selftest", action="store_true", help="离线自测，不读真实文件")
    ap.add_argument("--roster", default=None, help="考核表 .xlsx / .csv（默认 roster.csv）")
    ap.add_argument("--config", default=str(ROOT / nexus.CONFIG_NAME), help="配置文件")
    ap.add_argument("--out", default=None, help="输出 CSV（默认 payroll_<周期>.csv）")
    ap.add_argument("--period", default=None, help="结算周期，默认本月 YYYY-MM")
    ap.add_argument("--refresh", action="store_true", help="联网刷新每人的体积/数量（只读）")
    ap.add_argument("--delay", type=float, default=1.0, help="--refresh 时的请求间隔秒数")
    ap.add_argument("--workers", type=int, default=1,
                    help="--refresh 并发路数，默认 1（一个人一个人抓）；"
                         "配置里的 measure_workers 是界面用的默认值")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if selftest() else 1)

    period = args.period or datetime.now().strftime("%Y-%m")
    roster_path = Path(args.roster) if args.roster else ROOT / "roster.csv"

    if not roster_path.exists():
        sys.exit(f"[x] 找不到考核表 {roster_path}\n"
                 f"    在 WPS 里「另存为 .xlsx」放到项目根目录，或用 --roster 指定路径")

    try:
        cfg, settings, cfg_path, is_ex = load_settings(args.config)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"[x] 配置有问题：{e}")

    metrics = settings["metrics"]

    print(f"考核表 : {roster_path}")
    print(f"配置   : {cfg_path}" + ("   [!] 用的是模板，不是 config.json" if is_ex else ""))
    print(f"周期   : {period}")
    print(f"考核   : {' + '.join(METRIC_LABELS[m] for m in metrics)}")
    print(f"口径   : 月薪 = 组员实收，反算赠送量"
          f"（税 {settings['payout']['tax_rate']}×x−{settings['payout']['tax_flat']}）")

    members = roster_mod.load_roster(roster_path)
    print(f"读到   : {len(members)} 人")

    if is_ex:
        print()
        print("[!] 现在用的是 config.example.json（示例站点 pt.example.com）。")
        print("    离线出工资表没问题；要联网刷新先跑 python login.py 生成 config.json。")

    if args.refresh:
        try:
            cfg, ep, sess = nexus.load_config(args.config, ROOT, require_cookie=True)
        except (FileNotFoundError, ValueError) as e:
            sys.exit(f"[x] --refresh 失败：{e}")
        print()
        print(f"站点   : {ep.base}")
        print(f"联网刷新（只读，间隔 {args.delay}s"
              + (f"，{args.workers} 路并发" if args.workers > 1 else "") + "）……")
        ok, fail = refresh_measurements(members, metrics, sess, ep, delay=args.delay,
                                        snapshot_dir=ROOT / "samples",
                                        workers=args.workers,
                                        session_factory=lambda: nexus.Session(
                                            ep.base, cfg.get("cookie") or "",
                                            timeout=int(cfg.get("timeout_seconds") or 25)))
        print(f"刷新完成：成功 {ok}，失败 {fail}")

    rows, errors = build_payroll(members, settings)
    print_report(rows, errors, settings, period)

    columns = build_columns(metrics)
    out = Path(args.out) if args.out else ROOT / f"payroll_{period}.csv"
    write_payroll_csv(rows, columns, out)
    print()
    print(f"工资表已写出：{out}")
    print("（只读 + 离线，没有做任何发放动作）")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
