#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发放执行 —— 逐笔赠送魔力值。纯标准库。

默认是 **dry-run**：只打印"打算发给谁、发多少"，一个字都不写库。

真实发放必须同时满足：
  1. 显式加 --confirm
  2. config.json 的 gift_form 配全（action / username / amount / success_url）
     —— 前两样由 `python probe.py` 探测后填，**工具绝不猜字段名**
  3. 这个人本周期还没发满 N 次（--force 可以忽略这条）

防超发的依据只有**一个数**：这个人本周期已经发出去几笔
  · 「每周期 N 次」= **每个人**这个周期都能被发 N 次（不是整组共享 N 次）
  · 没发满的人会被再发一次 —— 再点一次「发放」就好，这就是「每周 2 次」
  · 发满 N 次的人自动跳过（界面状态「发完」）；要重发先「重置周期」重置他
  · **不做逐人去重**：重复点「发放」/ 重跑命令 = 真的再发一笔，
    所以动手前看清清单，别把已经收到的人再点一遍

    python payout.py                          # dry-run
    python payout.py --period 2026-09
    python payout.py --period 2026-09 --only user001,user002
    python payout.py --period 2026-09 --include-unqualified
    python payout.py --period 2026-09 --confirm     # 真发（发满 N 次的人跳过）
    python payout.py --status                       # 看本周期发了几笔 / 每人几次
"""

import argparse
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import ledger as ledger_mod
import nexus
import roster as roster_mod
import settle as settle_mod

ROOT = nexus.DATA_ROOT
TB = 1024 ** 4

# 成功判定只看落点 URL 后缀：
#   点「赠送」提交到 mybonus.php?action=exchange
#   送出成功 → 页面跳 mybonus.php?do=transfer
#   10 秒内重复点 → 跳 mybonus.php?do=duplicated（这笔没送出，可安全重试）
DEFAULT_SUCCESS_URL = "do=transfer"
DUPLICATE_URL = "do=duplicated"


# ============================================================
# 表单规格校验 —— 没配全就不许真发
# ============================================================

def validate_gift_form(spec):
    """返回问题列表，空列表 = 可以真发。"""
    spec = spec or {}
    fields = spec.get("fields") or {}
    problems = []
    if not spec.get("action"):
        problems.append("gift_form.action 没配（不知道往哪提交）")
    if not fields.get("username"):
        problems.append("gift_form.fields.username 没配（不知道收礼人字段叫什么）")
    if not fields.get("amount"):
        problems.append("gift_form.fields.amount 没配（不知道金额字段叫什么）")
    if "success_url" in spec and not (spec.get("success_url") or "").strip():
        # 判定「这笔到底成没成」只看成功落点 URL 后缀。**默认值就在**，
        # 所以正常不用管；只有被显式清空才拒绝真发 —— 判不出来宁可发不出去。
        problems.append(
            "gift_form.success_url 被清空了（成功落点 URL 后缀）"
            "—— 没法确认这笔到底送没送出去，所以不允许真实发放")
    return problems


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _visible_variants(html):
    """
    返回 (带空白的正文, 去空白正文)。

    标记文字可能被 <b> 之类的标签切开，也可能中间夹了换行，
    所以除了原始 HTML，再对这两个变体各匹配一次。
    """
    raw = html or ""
    spaced = _WS_RE.sub(" ", _TAG_RE.sub(" ", raw))
    tight = _WS_RE.sub("", _TAG_RE.sub("", raw))
    return spaced, tight


def judge_response(status, html, final, spec):
    """
    判断这一笔到底成没成。返回 (ok, 说明)。

    **只看落点 URL**（用户 2026-09 实测提供）：
      点「赠送」这个动作提交到 mybonus.php?action=exchange；
        送出去了 → 页面跳 mybonus.php?do=transfer；
        10 秒内重复点 → 跳 mybonus.php?do=duplicated（= 这笔根本没送出）。
    成功响应里**没有任何文字**，所以除了落点 URL 没别的判据可用 ——
    不再认「成功标记 / 失败标记」那类文字（宽泛词还会误杀真成功）。

    落点两处都认：响应里的 JS 跳转目标，以及跟随 HTTP 重定向后的最终地址。
    只认这两处取到的 URL，**不整页搜串** —— 模板别处也可能带这两个词。
    可用 gift_form.success_url / duplicate_url 覆盖（设空串 = 关掉这条判据）。
    """
    spec = spec or {}
    if status == 0:
        return False, f"网络错误：{str(html)[:80]}"
    if nexus.looks_like_login(html):
        return False, "登录态失效（被跳回登录页）"

    v = spec.get("success_url")
    ok_url = DEFAULT_SUCCESS_URL if v is None else v
    v = spec.get("duplicate_url")
    dup_url = DUPLICATE_URL if v is None else v

    targets = []
    js = parse_js_redirect(html)
    if js:
        targets.append(js)
    if final and final not in targets:
        targets.append(final)

    for t in targets:
        if dup_url and dup_url in t:
            return False, (f"站点判定重复提交（跳到 {dup_url}）—— 确定没送出")
        if ok_url and ok_url in t:
            return True, f"落点 URL 是成功页（{ok_url}）"
    landed = targets[-1] if targets else "（响应里没有跳转目标）"
    return False, f"落点 URL 不是成功页：{landed}（HTTP {status}）"


_JS_REDIRECT_RE = re.compile(
    r"window\.location(?:\.href)?\s*=\s*['\"]([^'\"]+)['\"]", re.I)


def parse_js_redirect(html):
    """抓响应里的 JS 跳转目标（window.location.href = '...'）；没有返回 None。"""
    m = _JS_REDIRECT_RE.search(html or "")
    return m.group(1) if m else None


# 「确定**没提交**」的失败原因 —— 只有这些才允许自动重试。
# 其余失败（网络中断 / 落点 URL 没结论 / 登录态失效）都可能「其实已经提交了」，
# 再送一次就是重复发钱 —— 一律不重试，留给人核对：宁可这一笔发不出去。
# 现在唯一能证明「没送出」的就是站点自己给的重复提交页（跳 do=duplicated）。
RETRYABLE_HINTS = ["重复提交"]


def retryable_failure(why):
    """这句失败原因属于「确定没提交、可以安全重试」吗？"""
    return any(h in (why or "") for h in RETRYABLE_HINTS)


# ============================================================
# 从真实赠送页猜 gift_form —— 猜完还要人确认，绝不静默生效
# ============================================================

_USER_HINTS = ("username", "user", "name", "to")
_AMOUNT_HINTS = ("seedbonus", "bonus", "karma", "point", "amount", "gift", "value")
_MSG_HINTS = ("message", "msg", "comment", "reason", "note", "remarks", "body")

# 赠送成功页上没有任何文字，失败原因只来自落点 URL（重复提交）
# 和 HTTP / 网络层。


def _pick_field(names, hints):
    """按提示词挑字段名。先精确匹配，再包含匹配。"""
    low = {n.lower(): n for n in names}
    for h in hints:
        if h in low:
            return low[h]
    for h in hints:
        for n, orig in low.items():
            if h in n:
                return orig
    return ""


def suggest_gift_form(html, page_url="", ep=None, origin=None):
    """
    从赠送页 HTML 里认出提交目标与字段名。

    返回 (spec, notes)
      spec  可直接写进 config.json 的 gift_form
      notes 给人看的说明，含"哪些没认出来"
    """
    notes = []
    if origin is None and ep is not None:
        origin = getattr(ep, "origin", "") or ""
    forms = nexus.extract_forms(html)
    if not forms:
        return None, ["页面上一个表单都没找到（可能是 JS 动态渲染）。"
                      "请打开浏览器 F12 → Network，手工操作一次赠送，"
                      "把那个 POST 请求的字段名告诉我"]

    best, best_score = None, -1
    for f in forms:
        names = [x.get("name", "") for x in f["fields"] if x.get("name")]
        if not names:
            continue
        u = _pick_field(names, _USER_HINTS)
        a = _pick_field(names, _AMOUNT_HINTS)
        score = (2 if u else 0) + (2 if a else 0)
        if score > best_score:
            best, best_score = f, score

    if best is None or best_score <= 0:
        return None, [f"找到 {len(forms)} 个表单，但没有一个像赠送表单。"
                      f"字段名分别是 " +
                      "；".join(
                          ",".join(x.get("name", "") for x in f["fields"] if x.get("name"))
                          for f in forms)]

    names = [x.get("name", "") for x in best["fields"] if x.get("name")]
    user_f = _pick_field(names, _USER_HINTS)
    amt_f = _pick_field(names, _AMOUNT_HINTS)
    msg_f = _pick_field(names, _MSG_HINTS)
    for label, val in (("收礼人字段", user_f), ("金额字段", amt_f)):
        if not val:
            notes.append(f"[!] 没认出{label}，请手工填（表单字段有：{names}）")

    action = best.get("action") or ""
    if page_url:
        action = urllib.parse.urljoin(page_url, action)
        if origin:
            parts = urllib.parse.urlsplit(action)
            if f"{parts.scheme}://{parts.netloc}" == origin.rstrip("/"):
                action = parts.path + (("?" + parts.query) if parts.query else "")
        notes.append(f"提交目标已按赠送页地址补全为 {action!r}")

    spec = {
        "action": action,
        "fields": {"username": user_f, "amount": amt_f, "message": msg_f},
        "success_url": DEFAULT_SUCCESS_URL,
        "duplicate_url": DUPLICATE_URL,
    }
    notes.append(f"success_url 预填 {DEFAULT_SUCCESS_URL!r}"
                 "（成功落点 URL 后缀：送出成功后页面跳 "
                 "mybonus.php?do=transfer）。判定只看这个后缀，"
                 "不再看页面文字；换站要自己核一遍落点。")
    return spec, notes


# ============================================================
# 计划
# ============================================================

def pay_state(times, limit):
    """
    本周期发放状态文本（④ 清单「状态」列、汇总行都用它）：

      还没发过      → 待发
      发了但没发满  → 已发 n 次
      发满设定次数  → 发完
    """
    n = int(times or 0)
    limit = max(1, int(limit or 1))
    if n <= 0:
        return "待发"
    if n >= limit:
        return "发完"
    return f"已发 {n} 次"


def times_left(times, limit):
    """本周期还能领几次（0 = 已经发完）。"""
    return max(0, max(1, int(limit or 1)) - int(times or 0))


def plan_payout(rows, settings, ledger_path, period,
                include_unqualified=None, include_untested=None, only=None,
                force=False):
    """
    把工资表变成清单。

    不达标 / 未测的人算不算「待发」由两个开关决定，取值 None 时读配置：
      payout.include_unqualified  （默认 false）
      payout.include_untested     （默认 false）

    进了清单的人也带 default_pick=False —— 只有「达标」默认勾上，其余**默认不勾**，
    要发必须由人手动勾上。CLI 保持默认不出现（配置开了才出现并列出状态）。

    限额是**每人 N 次**（N = payout.rounds_per_period），**不做逐人去重**：
    发过的人照样进清单，带 paid_times（本周期已发次数）和 state
    （待发 / 已发 n 次 / 发完）。default_pick 看两条：达标 + 本周期没发满
    —— 所以点第二次「发放」时同一批人还是勾着的（这就是「每周 2 次」）。
    force=True（CLI 的 --force）忽略每人上限，发满的人也默认勾上。

    返回 (items, skipped, quota)
      items    [{uid, username, plan, amount, status, why, paid, paid_times,
                 paid_at, state, default_pick}]（default_pick=False = 默认不勾）
      skipped  被跳过 [{username, reason}] —— 只有「不该进清单」的人在这
                （没月薪 / 考核状态不认 / 不达标未测没开开关）
      quota    {"limit","sent","total","failed","done","partial","fresh"}
               本周期每人次数口径的汇总，界面拿它写汇总行
    """
    payout = settings["payout"]
    if include_unqualified is None:
        include_unqualified = bool(payout.get("include_unqualified", False))
    if include_untested is None:
        include_untested = bool(payout.get("include_untested", False))
    limit = max(1, int(payout.get("rounds_per_period", 1) or 1))
    # 本周期每人已经发出去几笔 —— 唯一的上限依据（不做事后去重）
    led = ledger_mod.period_state(ledger_path, period)
    times = led["uids"]
    only_set = None
    if only:
        only_set = {str(x).strip().lower() for x in only if str(x).strip()}

    items, skipped = [], []
    for r in rows:
        name = str(r.get("username") or "")
        uid = r.get("uid")
        key = str(uid)

        if only_set is not None and name.lower() not in only_set and key not in only_set:
            continue
        if r.get("gift") is None:
            skipped.append((name, "没有月薪（方案里没配 salary）"))
            continue
        if uid in (None, "", 0):
            skipped.append((name, "没有 uid —— 赠送按 username 走，先补 uid 更安全"))
        st = r.get("status")
        if st == "不达标":
            if not include_unqualified:
                skipped.append((name, f"考核不达标（{r.get('gap_text')}）—— 要发请加 --include-unqualified"
                                      "（或把配置 payout.include_unqualified 改成 true）"))
                continue
            why = f"不达标（{r.get('gap_text')}）"
        elif st == "未测":
            if not include_untested:
                skipped.append((name, "没采集到考核数据 —— 要发请加 --include-untested"
                                      "（或把配置 payout.include_untested 改成 true）"))
                continue
            why = "未测（没有考核数据）"
        elif st != "达标":
            skipped.append((name, f"考核状态 {st!r}，跳过"))
            continue
        else:
            why = "达标"

        info = times.get(key) or {}
        n = int(info.get("times") or 0)
        if n:
            why += f"（本周期已发 {n} 次）"
        items.append({
            "uid": uid, "username": name, "plan": r.get("plan_id"),
            "amount": int(r["gift"]), "salary": r.get("salary"),
            "status": st, "why": why,
            "paid": n > 0, "paid_times": n,
            "paid_at": info.get("last_at") or "",
            "state": pay_state(n, limit),
            # 达标 + 没发满才默认勾上；发满的人列出来、默认不勾
            # （一眼看出「这些人这个周期已经领够了」）
            "default_pick": st == "达标" and (force or n < limit),
        })

    quota = {
        "limit": limit,
        "sent": led["sent"],
        "total": led["total"],
        "failed": led["failed"],
        "done": sum(1 for i in items if i["paid_times"] >= limit),
        "partial": sum(1 for i in items if 0 < i["paid_times"] < limit),
        "fresh": sum(1 for i in items if not i["paid_times"]),
    }
    return items, skipped, quota


# ============================================================
# 执行
# ============================================================

def resolve_form_target(ep, sess, log=None):
    """
    每次发放前实时 GET 一次赠送页，把表单的 action 和**隐藏字段**捞出来。

    为什么必须这么做：真实 NexusPHP 的赠送表单里有
    `<input type="hidden" name="action" value="gift">`，不带这个字段
    服务端根本不认这笔赠送。各站二次开发后还可能加别的隐藏字段（含防重放令牌），
    所以**不把它们写死在配置里**，而是每次现抓。

    返回 {"action": ..., "hidden": {...}}；抓不到就返回 None（按配置里的提交）。
    """
    if not ep.has("gift_bonus"):
        return None
    url = ep.path("gift_bonus")
    status, html, final = sess.request(url)
    if status != 200 or nexus.looks_like_login(html):
        return None

    forms = nexus.extract_forms(html)
    best, best_score = None, -1
    for f in forms:
        names = [x.get("name", "") for x in f["fields"] if x.get("name")]
        if not names:
            continue
        u = _pick_field(names, _USER_HINTS)
        a = _pick_field(names, _AMOUNT_HINTS)
        score = (2 if u else 0) + (2 if a else 0)
        if score > best_score:
            best, best_score = f, score
    if best is None or best_score <= 0:
        return None

    action = best.get("action") or ""
    # 相对地址按「实际请求到的页面地址」补全，跟配置里的 base_url 无关
    action = urllib.parse.urljoin(final or url, action)
    origin = ep.origin or ""
    parts = urllib.parse.urlsplit(action)
    if origin and f"{parts.scheme}://{parts.netloc}" == origin.rstrip("/"):
        action = parts.path + (("?" + parts.query) if parts.query else "")

    hidden = {}
    for fld in best["fields"]:
        if fld.get("tag") == "input" and (fld.get("type") or "").lower() == "hidden" \
                and fld.get("name"):
            hidden[fld["name"]] = fld.get("value") or ""

    if log:
        log(f"  赠送表单现抓：" + repr(action)
            + ("  隐藏字段 " + ", ".join(f"{k}={v!r}" for k, v in hidden.items())
               if hidden else "  （没有隐藏字段）"))
    return {"action": action, "hidden": hidden}


def _save_gift_snapshot(snapshot_dir, ctr, uid, html, log=None):
    """
    判定失败的响应原样存档（samples/debug_gift_*.html，cap 3，gitignore 覆盖）。

    各站的响应页面千差万别 —— 有快照才排得动判定问题，不用靠截图猜页面长什么样。
    """
    if not snapshot_dir:
        return
    try:
        snapshot_dir = Path(snapshot_dir)
        snapshot_dir.mkdir(exist_ok=True)
        if ctr.get("n", 0) >= ctr.get("cap", 3):
            return
        p = snapshot_dir / f"debug_gift_{uid}_{ctr.get('n', 0)}.html"
        p.write_text(html or "", encoding="utf-8", errors="replace")
        ctr["n"] = ctr.get("n", 0) + 1
        if log:
            log(f"  [i] 响应已原样存进 {p.name}（排模板用，不会提交）")
    except OSError:
        pass


def send_one(sess, ep, spec, item, settings, period, live=None,
             snapshot_dir=None, snap_ctr=None, log=None):
    """发一笔。返回 (ok, 说明, http_status)。"""
    fields = spec.get("fields") or {}
    msg_tpl = settings["payout"].get("message_template") or ""

    data = dict((live or {}).get("hidden") or {})      # 先铺隐藏字段
    data[fields["username"]] = item["username"]        # 再盖我们的值
    data[fields["amount"]] = str(item["amount"])
    if fields.get("message") and msg_tpl:
        data[fields["message"]] = msg_tpl.format(
            period=period, plan_id=item.get("plan") or "",
            username=item["username"])

    action = (live or {}).get("action") or spec["action"]
    referer = ep.url("gift_bonus") if ep.has("gift_bonus") else sess.base
    status, html, final = sess.post(action, data, referer=referer)
    ok, why = judge_response(status, html, final, spec)
    if not ok and status and html:
        spaced, _ = _visible_variants(html)
        excerpt = spaced.strip()[:160]
        if log and excerpt and not nexus.looks_like_login(html):
            log(f"  [i] 响应正文开头：{excerpt}")
        if "登录态失效" not in (why or ""):
            _save_gift_snapshot(snapshot_dir, snap_ctr or {},
                                item.get("uid"), html, log=log)
    return ok, why, status


def execute(items, settings, ep, sess, ledger_path, period, spec=None,
            dry_run=True, delay=None, retries=2, log=print, fetch_form=True,
            snapshot_dir=None, progress=None, force=False):
    """
    逐笔执行。返回统计 dict。

    每笔发出**前**都重新数一遍这个人本周期已经发出去几笔（不依赖任何内存状态，
    所以「重复点发放」「重跑命令」「开了两个进程」都拦得住）：已经发满
    payout.rounds_per_period 次的人直接跳过，没发满的照发 —— 一个周期里每个人
    最多就是 N 笔，一次都不会多。force=True 忽略这条上限（CLI 的 --force）。

    重试只发生在「确定没提交」的失败上（站点限速页那类）；其余失败一律不重试
    （原因见 RETRYABLE_HINTS）。

    progress(phase, i, n, item) —— 每笔前后回调，GUI 靠它逐行刷新发放状态：
      phase: "sending"（正要发）/ "sent" / "failed" / "skipped"（本周期已发满，
             或者 dry-run 没真发）
    """
    payout = settings["payout"]
    interval = float(payout.get("interval_seconds", 10) if delay is None else delay)
    limit = max(1, int(payout.get("rounds_per_period", 1) or 1))
    spec = spec or {}

    stats = {"sent": 0, "failed": 0, "skipped_full": 0, "amount": 0,
             "results": []}
    total = len(items)
    if total == 0:
        return stats

    est = total * interval
    log(f"计划发放 {total} 人，应赠合计 {sum(i['amount'] for i in items):,}")
    log(f"间隔 {interval:g}s，纯等待约 {est / 60:.1f} 分钟"
        + ("（dry-run，不真发）" if dry_run else ""))
    log("-" * 72)

    live = None
    warned_login = False
    if not dry_run and fetch_form and sess is not None and ep is not None:
        live = resolve_form_target(ep, sess, log=log)
        if live is None:
            log("[!] 没能实时抓到赠送页表单，按 config.json 里配的提交"
                "（若站点要隐藏字段，这一批会全失败 —— 失败了也不会记账，很安全）")
            live = {}

    for n, item in enumerate(items, 1):
        tag = f"[{n}/{total}] {item['username']:<16} {item['amount']:>9,}"
        if progress:
            progress("sending", n, total, item)

        # ★ 发出前数一遍：这个人本周期已经发了几笔？发满就不发（唯一的限额）
        done_n = ledger_mod.times_of(ledger_path, period, item["uid"])
        if done_n >= limit and not force:
            stats["skipped_full"] += 1
            log(f"{tag}  跳过：本周期已经发满 {limit} 次（要重发先「重置周期」）")
            stats["results"].append({
                **item, "ok": False,
                "why": f"本周期已发满 {limit} 次，跳过"})
            if progress:
                progress("skipped", n, total, item)
            continue

        if dry_run:
            log(f"{tag}  将赠送（dry-run 未执行）")
            stats["results"].append({**item, "ok": None, "why": "dry-run"})
            if progress:
                progress("skipped", n, total, item)
            continue

        attempt = 0
        ok, why = False, ""
        snap_ctr = {"n": 0, "cap": 3}
        while attempt <= retries and not ok:
            attempt += 1
            ok, why, hstatus = send_one(sess, ep, spec, item, settings, period,
                                        live=live, snapshot_dir=snapshot_dir,
                                        snap_ctr=snap_ctr, log=log)
            if ok:
                break
            if "登录态失效" in (why or ""):
                if not warned_login:
                    warned_login = True
                    log("[x] 登录态失效 —— 后面每笔都会失败，先重新拿 cookie 再跑，"
                        "已经发成功的人台账照记，重跑会自动跳过")
                break
            if not retryable_failure(why):
                log(f"{tag}  [x] {why} —— 不重试：这句话证明不了「没送出去」，"
                    f"再送一次可能就重复发钱了，请人工核对这一笔")
                break
            if attempt <= retries:
                log(f"{tag}  第 {attempt} 次失败：{why}；{interval:g}s 后重试")
                time.sleep(interval)

        if ok:
            stats["sent"] += 1
            stats["amount"] += item["amount"]
            ledger_mod.record_gift(ledger_path, period, item["uid"],
                                   item["username"], item["amount"],
                                   item.get("plan") or "", status="ok", note=why)
            log(f"{tag}  [OK] {why}")
            if progress:
                progress("sent", n, total, item)
        else:
            stats["failed"] += 1
            ledger_mod.record_gift(ledger_path, period, item["uid"],
                                   item["username"], item["amount"],
                                   item.get("plan") or "", status="fail", note=why)
            log(f"{tag}  [x] 失败：{why}")
            if progress:
                progress("failed", n, total, item)
        stats["results"].append({**item, "ok": ok, "why": why})

        if n < total:
            time.sleep(interval)

    if stats["skipped_full"]:
        log(f"[i] 有 {stats['skipped_full']} 人本周期已经发满 {limit} 次，跳过了"
            f"（要重发先「重置周期」）")
    return stats


# ============================================================
# 打印
# ============================================================

def _pad(text, width, right=False):
    """
    按**显示宽度**补齐（中文、全角字符算 2 列）。

    str.ljust 只数字符个数，中英混排的表头/用户名会歪得没法看。
    """
    s = str(text)
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
    fill = " " * max(0, width - w)
    return (fill + s) if right else (s + fill)


def print_plan(items, skipped, quota, settings, period, ledger_path, dry_run):
    payout = settings["payout"]
    print()
    print("=" * 72)
    print(f"发放计划 · {period}   {'（dry-run，不会真发）' if dry_run else '（真实发放）'}")
    print("=" * 72)
    print(f"台账     : {ledger_path}")
    limit = max(1, int(payout.get("rounds_per_period", 1) or 1))
    done_n = quota.get("done", 0) if isinstance(quota, dict) else 0
    part_n = quota.get("partial", 0) if isinstance(quota, dict) else 0
    print(f"每人次数 : 本周期 {limit} 次 —— 已发满 {done_n} 人、"
          f"已发过没发满 {part_n} 人")
    print("           （没发满的人这次会被再发一次；发满的跳过，"
          "要重发先「重置周期」）")

    if items:
        print()
        print(f"{_pad('UID', 7, True)}  {_pad('用户名', 18)} {_pad('考核', 6)} "
              f"{_pad('方案', 5)} {_pad('实收', 9, True)} {_pad('应赠', 9, True)}")
        print("-" * 68)
        for it in items:
            mark = "" if it.get("default_pick", True) else " ★不勾"
            if it.get("paid_times"):
                mark += f"（本周期已发 {it['paid_times']} 次）"
            salary = f"{it['salary']:,}" if it.get("salary") else ""
            amount = f"{it['amount']:,}"
            print(f"{_pad(it['uid'], 7, True)}  {_pad(it['username'][:16], 18)} "
                  f"{_pad(it.get('status', ''), 6)} {_pad(it['plan'] or '', 5)} "
                  f"{_pad(salary, 9, True)} {_pad(amount, 9, True)}{mark}")
        print("-" * 68)
        print(f"合计 {len(items)} 人，应赠 {sum(i['amount'] for i in items):,}")
        extra = [i for i in items if not i.get("default_pick", True)]
        if extra:
            print()
            print(f"[!] 里面有 {len(extra)} 人不达标 / 未测（★标记），"
                  "默认不勾（GUI 里要发得手动勾）。")
            print("    —— 是配置里 payout.include_unqualified / include_untested 打开的；"
                  "不想在清单里看到就把它们改回 false。")
            print("    —— CLI 发出去的是清单里的所有人，动手前请逐行看清。")
    else:
        print()
        print("（没有要发的人）")

    if skipped:
        print()
        print(f"跳过 {len(skipped)} 人：")
        for name, why in skipped:
            print(f"   {name:<16} {why}")


# ============================================================

def main():
    ap = argparse.ArgumentParser(
        description="保种组工资发放（默认 dry-run）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="真实发放要加 --confirm，并且先把 gift_form 配全（跑 python probe.py 探测）。")
    ap.add_argument("--roster", default=None, help="考核表（默认 roster.csv）")
    ap.add_argument("--config", default=str(ROOT / nexus.CONFIG_NAME))
    ap.add_argument("--period", default=None, help="结算周期，默认本月 YYYY-MM")
    ap.add_argument("--confirm", action="store_true", help="真的发（不加就是 dry-run）")
    ap.add_argument("--force", action="store_true",
                    help="忽略「每人每周期 N 次」的上限（本周期发满的人这次也发）")
    ap.add_argument("--only", default=None, help="只发这些人（逗号分隔的用户名或 uid）")
    ap.add_argument("--include-unqualified", action="store_true", default=None,
                    help="不达标的人也列进清单（默认看配置 payout.include_unqualified）")
    ap.add_argument("--include-untested", action="store_true", default=None,
                    help="没采集到数据的人也列进清单（默认看配置 payout.include_untested）")
    ap.add_argument("--delay", type=float, default=None,
                    help="覆盖两次赠送的间隔秒数（默认用配置里的）")
    ap.add_argument("--log", default=None, help="把过程同时写进这个文件")
    ap.add_argument("--status", action="store_true", help="只看台账状态，不做别的")
    ap.add_argument("--mark-paid", metavar="UID", default=None,
                    help="人工补记：工具判失败但你在站点上核对过确实收到了，"
                         "把这笔记成已发（防下次重跑重复发）。"
                         "金额和用户名照抄台账里那条失败记录")
    ap.add_argument("--mark-note", default="人工核对站点收件箱确认已收到",
                    help="补记时的备注（配合 --mark-paid）")
    ap.add_argument("--selftest", action="store_true", help="离线自测")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if selftest() else 1)

    period = args.period or datetime.now().strftime("%Y-%m")

    try:
        cfg, settings, cfg_path, is_ex = settle_mod.load_settings(args.config)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"[x] 配置有问题：{e}")

    ledger_path = Path(cfg.get("payout", {}).get("ledger") or ledger_mod.DEFAULT_LEDGER)
    if not ledger_path.is_absolute():
        ledger_path = ROOT / ledger_path

    if args.status:
        print(f"台账文件 : {ledger_path}")
        st = ledger_mod.period_state(ledger_path, period)
        limit = max(1, int(settings["payout"].get("rounds_per_period", 1) or 1))
        print(f"周期     : {period}    每人本周期上限 {limit} 次")
        print(f"已发成功 : {st['sent']} 笔，合计 {st['total']:,}，"
              f"涉及 {len(st['uids'])} 人")
        print(f"有失败   : {st['failed']} 人")
        for uid, info in sorted(st["uids"].items(),
                                key=lambda kv: (-kv[1]["times"], kv[0])):
            print(f"   uid {uid:>8}  {str(info['username'])[:16]:<16} "
                  f"已发 {info['times']} 次 / 上限 {limit}"
                  + ("   ← 发完" if info["times"] >= limit else ""))
        sys.exit(0)

    # ---- 人工补记：工具判失败、但站点上确实收到了 --------------------------
    if args.mark_paid is not None:
        uid = str(args.mark_paid).strip()
        st0 = ledger_mod.period_state(ledger_path, period)
        prev = None
        for r in st0["records"]:
            if (r.get("type") == "gift" and str(r.get("uid")) == uid
                    and r.get("status") == "fail"):
                prev = r                     # 取最近一条失败记录当模板
        if prev is None:
            sys.exit(f"[x] 台账里找不到 uid {uid} 在 {period} 的失败记录，"
                     "没有可抄的金额 —— 先跑一次发放（dry-run 也行）再补记。")
        ledger_mod.record_gift(
            ledger_path, period, uid, prev.get("username", ""),
            prev.get("amount", 0), prev.get("plan", ""), status="ok",
            note=args.mark_note)
        limit0 = max(1, int(settings["payout"].get("rounds_per_period", 1) or 1))
        got = ledger_mod.times_of(ledger_path, period, uid)
        print(f"[OK] 已补记：uid {uid}（{prev.get('username', '')}）"
              f"{prev.get('amount', 0):,} —— 他在 {period} 一共记 {got} 笔"
              f"（上限 {limit0} 次"
              + ("，已发满" if got >= limit0 else "，还能再发") + "）。")
        sys.exit(0)

    roster_path = Path(args.roster) if args.roster else ROOT / "roster.csv"
    if not roster_path.exists():
        sys.exit(f"[x] 找不到考核表 {roster_path}")

    members = roster_mod.load_roster(roster_path)
    rows, errors = settle_mod.build_payroll(members, settings)
    if errors:
        print("[!] 工资表有数据错误：")
        for e in errors:
            print("   ", e)

    dry_run = not args.confirm
    if dry_run and args.force:
        print("[!] --force 只在真实发放时有意义，dry-run 下忽略。")

    # 真实发放前先把表单规格验一遍 —— 宁可打不开，也不要发错
    if not dry_run:
        problems = validate_gift_form(cfg.get("gift_form"))
        if problems:
            print()
            print("[x] 还不能真实发放，config.json 里这些没配全：")
            for p in problems:
                print("   · " + p)
            print()
            print("    这些字段各站不一样，工具不猜。先跑：")
            print("       python probe.py")
            print("    再把它打印出来的 gift_form 片段粘进 config.json。")
            sys.exit(2)

    only = args.only.split(",") if args.only else None
    items, skipped, quota = plan_payout(
        rows, settings, ledger_path, period,
        include_unqualified=args.include_unqualified,
        include_untested=args.include_untested, only=only, force=args.force)

    print_plan(items, skipped, quota, settings, period, ledger_path, dry_run)

    if not items:
        print()
        print("没有待发的人，结束。")
        sys.exit(0)

    if dry_run:
        print()
        print("=" * 72)
        print("以上是 dry-run，一个字都没写。")
        print("确认无误后加 --confirm 真实发放：")
        print(f"    python payout.py --period {period} --confirm")
        sys.exit(0)

    # ---------- 真实发放 ----------
    try:
        cfg2, ep, sess = nexus.load_config(args.config, ROOT, require_cookie=True)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"[x] 拿不到站点会话：{e}")

    print()
    print(f"站点 : {ep.base}")
    print("直接开跑，不做预检（每笔的落点 URL 会说明成没成）……")

    logfile = open(args.log, "a", encoding="utf-8") if args.log else None

    def log(msg):
        print(msg)
        if logfile:
            logfile.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
            logfile.flush()

    try:
        stats = execute(items, settings, ep, sess, ledger_path, period,
                        spec=cfg.get("gift_form"), dry_run=False,
                        delay=args.delay, log=log, force=args.force)
    finally:
        if logfile:
            logfile.close()

    # ---------- 收尾 ----------
    print()
    print("=" * 72)
    print(f"完成：成功 {stats['sent']}，失败 {stats['failed']}，"
          f"本周期已发满跳过 {stats['skipped_full']}")
    print(f"实发合计：{stats['amount']:,}")

    st = ledger_mod.period_state(ledger_path, period)
    limit = max(1, int(settings["payout"].get("rounds_per_period", 1) or 1))
    print()
    print(f"本周期累计 {st['sent']} 笔 / {st['total']:,}，"
          f"涉及 {len(st['uids'])} 人（每人上限 {limit} 次）。")
    if stats["skipped_full"]:
        print(f"[i] {stats['skipped_full']} 人已经发满 {limit} 次 —— 要让他们"
              f"再领，先重置这些人：")
        print(f"    python ledger.py --reset-period --period {period} "
              f"--uid <uid,uid>")
    if stats["failed"]:
        print(f"[!] 有 {stats['failed']} 人失败，先弄清「这笔到底送出去没有」：")
        print("    · 站点上确实收到了 → python payout.py --mark-paid <uid> 补记，"
              "别重发")
        print("    · 确定没送出去   → 直接再跑一次，发满的人会自动跳过")
    sys.exit(0)


# ============================================================
# 自测（纯函数层；整条链路在 tests/selftest_http.py 里跑）
# ============================================================

def _settings(gift_form=None, interval=10, rounds=1):
    cfg = {
        "assessment": {"metrics": ["volume"]},
        "plans": [{"id": "3T", "min_volume_tb": 3, "salary": 200000}],
        "payout": {"tax_rate": 0.9, "tax_flat": 4, "interval_seconds": interval,
                   "rounds_per_period": rounds,
                   "message_template": "保种组 {period}"},
    }
    s = settle_mod.validate_settings(cfg, "<selftest>")
    s["gift_form"] = gift_form
    return s


def selftest():
    import tempfile

    print("=" * 66)
    print("发放模块自测：表单校验 / 响应判定 / 计划过滤")
    print("=" * 66)
    fails = []

    def check(label, got, want):
        ok = got == want
        print(f"  [{'OK' if ok else 'XX'}] {label}: {got}" + ("" if ok else f"  (期望 {want})"))
        if not ok:
            fails.append(f"{label}: 期望 {want}，实际 {got}")

    def check_true(label, got):
        check(label, bool(got), True)

    print("\n-- 表单规格校验：没配全就不许真发 --")
    # 判定「这笔成没成」只看成功落点 URL，它的默认值就在（见下），
    # 所以正常情况根本不用管它 —— 只有被显式清空才拒绝真发。
    problems = validate_gift_form({})
    check("空配置报 3 个问题", len(problems), 3)
    problems = validate_gift_form({"action": "mybonus.php"})
    check("缺字段仍报错", len(problems), 2)
    problems = validate_gift_form({
        "action": "mybonus.php",
        "fields": {"username": "u", "amount": "a"}})
    check("配全了就放行（success_url 走默认值）", problems, [])
    check("success_url 显式清空 → 拒绝真发",
          len(validate_gift_form({"action": "/mybonus.php",
                                  "fields": {"username": "u", "amount": "a"},
                                  "success_url": ""})), 1)

    print("\n-- 响应判定：只看落点 URL，页面文字一律不看 --")
    check("登录页 → 失败",
          judge_response(200, "<form action='takelogin.php'>", "", {})[0], False)
    check("网络错误 → 失败", judge_response(0, "__EXCEPTION__", "", {})[0], False)
    # 成功响应里一个字都没有，所以「页面写着成功」判不出真假；
    # 反过来，页面碰巧带上「错误」「不能」这类宽泛词也不会误杀真成功。
    check("页面写着「赠送成功」但落点不对 → 不算成功",
          judge_response(200, "<p>赠送成功</p>", "", {})[0], False)
    check("页面写着「魔力值不足」又没跳转 → 失败",
          judge_response(200, "<p>魔力值不足</p>", "", {})[0], False)
    check_true("失败理由把落点报出来",
               "落点 URL" in judge_response(200, "<p>魔力值不足</p>", "", {})[1])
    check("成功后缀默认值", DEFAULT_SUCCESS_URL, "do=transfer")

    print("\n-- 落点 URL 判定（三种页面） --")
    spec = {}           # 用默认判据（成功响应里没有任何文字）
    ok, why = judge_response(
        200, "<p>魔力值: 12,454,903,652.1</p>"
             "<script>window.location.href = "
             "'https://site/mybonus.php?do=transfer';</script>", "", spec)
    check("跳转到 do=transfer → 成功", ok, True)
    check_true("理由点名 do=transfer", "do=transfer" in why)
    ok, why = judge_response(
        200, "<script>window.location.href = '/mybonus.php?do=duplicated';</script>",
        "", spec)
    check("跳转到 do=duplicated → 失败", ok, False)
    check_true("理由说清是重复提交", "重复提交" in why)
    check("重复提交确定没送出 → 可重试",
          retryable_failure(why), True)
    ok, why = judge_response(
        200, "<p>啥也没有</p><script>window.location.href='/index.php';</script>",
        "", spec)
    check("跳去别处 → 失败（判不出来就当没成）", ok, False)
    check("success_url 清空就关掉这条判据",
          judge_response(200, "<script>window.location.href="
                              "'https://site/mybonus.php?do=transfer';</script>",
                         "", dict(spec, success_url=""))[0], False)
    check("parse_js_redirect 抓目标",
          parse_js_redirect("a<script>window.location.href='X';</script>b"), "X")
    check("没有跳转 → None", parse_js_redirect("<p>普通页面</p>"), None)

    # 落点两处都认：响应里的 JS 跳转目标，以及跟随 HTTP 重定向后的最终地址
    check("HTTP 重定向（响应里没有 JS）→ 也按最终地址判成功",
          judge_response(200, "<p>没有跳转脚本</p>",
                         "https://site/mybonus.php?do=transfer", spec)[0], True)
    check("最终地址是重复页 → 失败且可重试",
          retryable_failure(judge_response(
              200, "<p>魔力值页</p>",
              "https://site/mybonus.php?do=duplicated", spec)[1]), True)
    check("落在别的 URL 上 → 失败",
          judge_response(200, "<p>普通页面</p>",
                         "https://site/index.php", spec)[0], False)
    check("必须声明为「确定没提交」的才敢自动重试",
          (retryable_failure("网络错误：timed out"),
           retryable_failure("落点 URL 不是成功页：/index.php"),
           retryable_failure("站点判定重复提交（跳到 do=duplicated）—— 确定没送出")),
          (False, False, True))
    check("重复跳转的 URL 可以在配置里改名",
          judge_response(
              200, "<script>window.location.href='/x?do=blocked';</script>",
              "", dict(spec, duplicate_url="do=blocked"))[0], False)

    print("\n-- 判定失败的响应快照（排模板用） --")
    with tempfile.TemporaryDirectory() as td2:
        sd = Path(td2)
        ctr = {"n": 0, "cap": 3}
        for k in range(5):                    # 超过 cap 的不写
            _save_gift_snapshot(sd, ctr, 9, f"<html>v{k}</html>")
        files = sorted(p.name for p in sd.glob("debug_gift_9_*.html"))
        check("快照最多存 cap 张", len(files), 3)
        check("文件带 uid 和序号", files[0], "debug_gift_9_0.html")

    print("\n-- 打印对齐：中文按两列算 --")
    check("普通补齐", _pad("ab", 5), "ab   ")
    check("中文按显示宽度补齐", _pad("用户名", 8), "用户名" + " " * 2)
    check("右对齐也按显示宽度", _pad("应赠", 6, True), "  " + "应赠")
    check_true("超宽不截断", _pad("很长的中文名字", 4).startswith("很长的中文名字"))

    print("\n-- 计划过滤：达标 / 不达标 / 未测 / 已发 --")
    with tempfile.TemporaryDirectory() as td:
        led = Path(td) / "l.jsonl"
        s = _settings()
        rows = [
            {"uid": 1, "username": "ok", "plan_id": "3T", "status": "达标",
             "gift": 222227, "salary": 200000, "gap_text": ""},
            {"uid": 2, "username": "bad", "plan_id": "3T", "status": "不达标",
             "gift": 222227, "salary": 200000, "gap_text": "缺 1.000 TB"},
            {"uid": 3, "username": "untested", "plan_id": "3T", "status": "未测",
             "gift": 222227, "salary": 200000, "gap_text": ""},
            {"uid": 4, "username": "noplan", "plan_id": "3T", "status": "达标",
             "gift": None, "salary": None, "gap_text": ""},
        ]
        items, skipped, quota = plan_payout(rows, s, led, "2026-09")
        check("默认只发达标的", [i["username"] for i in items], ["ok"])
        check("达标的默认勾选", [i["default_pick"] for i in items], [True])
        check("quota 报出每人上限", quota["limit"], 1)
        check("quota 计数：没人发过", (quota["fresh"], quota["partial"],
                                       quota["done"]), (1, 0, 0))
        reasons = dict(skipped)
        check_true("不达标被跳过并说明", "不达标" in reasons.get("bad", ""))
        check_true("未测被跳过并说明", "没采集到" in reasons.get("untested", ""))
        check_true("没月薪被跳过", "月薪" in reasons.get("noplan", ""))

        items, skipped, _ = plan_payout(rows, s, led, "2026-09",
                                        include_unqualified=True,
                                        include_untested=True)
        check("全放行后 3 人（noplan 没金额不算）",
              sorted(i["username"] for i in items), ["bad", "ok", "untested"])
        check("放行进来的都带 default_pick=False（默认不勾）",
              sorted((i["username"], i["default_pick"]) for i in items),
              [("bad", False), ("ok", True), ("untested", False)])
        check("不达标的人 why 里带着差距",
              [i["why"] for i in items if i["username"] == "bad"], ["不达标（缺 1.000 TB）"])

        # 非 GUI 的开关：配置里打开就跟传 --include-unqualified 一样
        s2 = _settings()
        s2["payout"]["include_unqualified"] = True
        items, _, _ = plan_payout(rows, s2, led, "2026-09")
        check("配置 payout.include_unqualified=true 就出现",
              sorted(i["username"] for i in items), ["bad", "ok"])
        check("配置打开进来的仍默认不勾",
              dict((i["username"], i["default_pick"]) for i in items),
              {"bad": False, "ok": True})
        s2["payout"]["include_untested"] = True
        items, _, _ = plan_payout(rows, s2, led, "2026-09")
        check("两个开关都开 → 未测也出现",
              sorted(i["username"] for i in items), ["bad", "ok", "untested"])
        items, _, _ = plan_payout(rows, s2, led, "2026-09", include_unqualified=False)
        check("显式传 False 能盖过配置",
              sorted(i["username"] for i in items), ["ok", "untested"])

        items, _, _ = plan_payout(rows, s, led, "2026-09", only=["ok", "bad"],
                                  include_unqualified=True)
        check("--only 过滤", sorted(i["username"] for i in items), ["bad", "ok"])
        items, _, _ = plan_payout(rows, s, led, "2026-09", only=["2"],
                                  include_unqualified=True)
        check("--only 也认 uid", [i["username"] for i in items], ["bad"])

        # ---- 状态文本：待发 / 已发 n 次 / 发完 ----
        print("\n-- 「状态」列文本（每人 N 次）--")
        check("没发过 → 待发", pay_state(0, 2), "待发")
        check("发了 1 次 / 上限 2 → 已发 1 次", pay_state(1, 2), "已发 1 次")
        check("发满 → 发完", pay_state(2, 2), "发完")
        check("超了也算发完", pay_state(3, 2), "发完")
        check("上限 1 次时发 1 次就是发完", pay_state(1, 1), "发完")
        check("还能领几次", [times_left(t, 2) for t in (0, 1, 2, 3)], [2, 1, 0, 0])

        # ---- 已发的人照样进清单（不做事后去重），发满的默认不勾 ----
        print("\n-- 已发的照样进清单；发满的默认不勾 --")
        ledger_mod.record_gift(led, "2026-09", 1, "ok", 222227, "3T")
        items, skipped, quota = plan_payout(rows, s, led, "2026-09")
        check("已发的照样在清单里", [i["username"] for i in items], ["ok"])
        check("带着本周期已发次数", items[0]["paid_times"], 1)
        check("why 里说明发过几次", items[0]["why"], "达标（本周期已发 1 次）")
        check("上限 1 次 → 发完，默认不勾", items[0]["default_pick"], False)
        check("状态就是「发完」", items[0]["state"], "发完")
        check("已发的人没被当成跳过（不去重）", "ok" in dict(skipped), False)
        check("quota 汇总", (quota["limit"], quota["done"], quota["partial"]),
              (1, 1, 0))

        items, _, quota = plan_payout(rows, _settings(rounds=2), led, "2026-09")
        check("上限 2 次 → 还能再发，默认勾上", items[0]["default_pick"], True)
        check("状态是「已发 1 次」", items[0]["state"], "已发 1 次")

        # ---- 「每周 2 次」= 每个达标的人都能领 2 次，发满就跳过 ----
        print("\n-- 每周 2 次：每个（达标）人能领 2 次，发满就跳过 --")
        led_r = Path(td) / "per_person.jsonl"
        s4 = _settings(rounds=2)
        r1, _, q1 = plan_payout(rows, s4, led_r, "2026-09")
        check("第 1 次：达标的人待发", [i["username"] for i in r1], ["ok"])
        check("第 1 次默认勾上", [i["default_pick"] for i in r1], [True])
        check("第 1 次状态「待发」", [i["state"] for i in r1], ["待发"])
        check("第 1 次 quota：1 个还没发", q1["fresh"], 1)
        ledger_mod.record_gift(led_r, "2026-09", 1, "ok", 222227, "3T")
        r2, _, q2 = plan_payout(rows, s4, led_r, "2026-09")
        check("第 2 次照样默认勾上（「再点一次也还是可以」）",
              [i["default_pick"] for i in r2], [True])
        check("第 2 次状态「已发 1 次」", [i["state"] for i in r2], ["已发 1 次"])
        check("第 2 次照样在清单里（不去重）",
              [i["username"] for i in r2], ["ok"])
        check("第 2 次 quota：1 个发过没发满", (q2["partial"], q2["done"]), (1, 0))
        ledger_mod.record_gift(led_r, "2026-09", 1, "ok", 222227, "3T")
        st_r = ledger_mod.period_state(led_r, "2026-09")
        check("两次都真发出去了（2 笔）", st_r["sent"], 2)
        check("同一人已发 2 次", st_r["uids"]["1"]["times"], 2)
        check("累计金额是两笔之和", st_r["total"], 222227 * 2)
        r3, _, q3 = plan_payout(rows, s4, led_r, "2026-09")
        check("发满 → 默认不勾", [i["default_pick"] for i in r3], [False])
        check("发满 → 状态「发完」", [i["state"] for i in r3], ["发完"])
        check("quota 算出发满人数", (q3["done"], q3["partial"], q3["fresh"]),
              (1, 0, 0))
        r_f, _, _ = plan_payout(rows, s4, led_r, "2026-09", force=True)
        check("--force 时发满的也默认勾（强发）",
              [i["default_pick"] for i in r_f], [True])

        # ---- 重置周期：把这些人清零 → 又能领 N 次 ----
        print("\n-- 重置周期（只动这些人）--")
        ledger_mod.record_gift(led_r, "2026-10", 1, "ok", 222227, "3T")
        ledger_mod.reset_period(led_r, "2026-09", uids=[1], note="自测：重置周期")
        r4, _, q4 = plan_payout(rows, s4, led_r, "2026-09")
        check("重置后回到「待发」", [i["state"] for i in r4], ["待发"])
        check("重置后默认勾上", [i["default_pick"] for i in r4], [True])
        check("重置后本周期的笔归零", q4["sent"], 0)
        check("重置只清本周期（10 月那笔还在）",
              ledger_mod.times_of(led_r, "2026-10", 1), 1)
        r5, _, _ = plan_payout(rows, s4, led_r, "2026-09")
        check("重置后又能领满 2 次（现在是 0 次）", r5[0]["paid_times"], 0)

    print("\n-- dry-run 不写库；发满的人会被跳过 --")
    with tempfile.TemporaryDirectory() as td:
        led = Path(td) / "l.jsonl"
        s = _settings()                       # 每人每周期 1 次
        items = [{"uid": 1, "username": "a", "plan": "3T", "amount": 222227,
                  "salary": 200000, "status": "达标", "why": "达标"}]
        st = execute(items, s, None, None, led, "2026-09", spec={},
                     dry_run=True, delay=0, log=lambda *a: None)
        check("dry-run 计数为 0", (st["sent"], st["failed"]), (0, 0))
        check("dry-run 谁都没跳过", st["skipped_full"], 0)
        check("台账文件根本没被创建", led.exists(), False)

        ledger_mod.record_gift(led, "2026-09", 1, "a", 222227, "3T")
        st = execute(items, s, None, None, led, "2026-09", spec={},
                     dry_run=True, delay=0, log=lambda *a: None)
        check("本周期已发满 1 次 → 跳过（dry-run 也看得出来）",
              st["skipped_full"], 1)
        check("跳过的原因写明白", st["results"][0]["why"],
              "本周期已发满 1 次，跳过")
        st = execute(items, _settings(rounds=2), None, None, led, "2026-09",
                     spec={}, dry_run=True, delay=0, log=lambda *a: None)
        check("上限调到 2 次 → 没发满，不跳过", st["skipped_full"], 0)
        st = execute(items, s, None, None, led, "2026-09", spec={},
                     dry_run=True, delay=0, log=lambda *a: None, force=True)
        check("--force 时无视上限，不跳过", st["skipped_full"], 0)
        check("dry-run 始终不写库（还是只有 1 条 gift）",
              len(ledger_mod.read_ledger(led)), 1)

    print()
    print("=" * 66)
    if fails:
        print("自测失败：")
        for x in fails:
            print("  x", x)
        return False
    print("自测全部通过 ✅")
    return True


if __name__ == "__main__":
    main()
