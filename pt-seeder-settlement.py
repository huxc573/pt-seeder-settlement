#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图形界面 —— 只用 tkinter（Python 自带的），**不需要 pip install 任何东西**。

这里只是一层「点点点」的壳：每个按钮背后调的都是 CLI 那套同一份代码
（login / probe / settle / payout / ledger），所以界面里点一遍和命令行跑一遍，
行为完全一致，台账也是同一个文件。**没有第二套逻辑**。

    python pt-seeder-settlement.py

四个页签：
  ① 站点与登录   —— 站点地址、接口路径、获取 cookie、赠送表单探测
  ② 考核与方案   —— 考核口径（体积/数量）、各方案门槛与月薪、发放参数
  ③ 算工资       —— 导入考核表 → 联网计算（逐行显示；勾选的行可重抓重算）→ 导出/复制
  ④ 发放与台账   —— 生成清单（不达标 / 已发也列出、默认不勾）→ 发放 → 台账状态
                    每人每周期可领 N 次（顶栏「每 [周] [2] 次」）：
                    没发满的人再点一次「发放」就会再发一笔（这就是「每周 2 次」）；
                    发满的状态显示「发完」并自动跳过。要让他再领，
                    选中行右键「重置周期」把这些人的本周期记录清零

两个表格（③④）的勾选框交互一样：点格子切换、右键菜单勾选/取消/反选、底部全选/全不选。

★ 安全设计（和命令行一致）：
  · 真实发放每笔前都数一遍「这个人本周期已经发了几笔」，发满 N 次的一律跳过
    —— 这是唯一的防超发依据（**不做逐人去重**：重复点一次「发放」就是真的
    再发一笔，所以动手前看清清单里谁还是「待发」）
  · 发放前**不做任何预检**，直接开跑 —— 每笔的落点 URL 会说明成没成
  · 点「发放」前有个确认框，把人数、周期、应赠合计摆出来（金额标红）
"""

import os
import queue
import re
import sys
import threading
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import ledger as ledger_mod
import login as login_mod
import nexus
import payout as payout_mod
import roster as roster_mod
import settle as settle_mod

ROOT = nexus.DATA_ROOT      # 用户数据（config/roster/台账）放 exe 旁边
RES_ROOT = nexus.RES_ROOT   # 只读资源（VERSION / samples / 模板）

# 窗口标题 = 程序名 + 版本号；配置有改动时标题上再多一句提示
APP_NAME = "PT 保种组工资工具"

# 开源地址（底部状态栏那个链接，点了开浏览器）
REPO_URL = "https://github.com/huxc573/pt-seeder-settlement"


def _app_version(default="v1.0.0"):
    """版本号唯一来源是根目录 VERSION 文件；读不到（只拷走单个脚本）就退回默认值。"""
    try:
        v = (RES_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return default
    return "v" + v if v else default


APP_VERSION = _app_version()

# 发放结果弹窗里那行灰字
TALLY_HINT = "详情请确定后，查看清单。"

try:
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox, ttk
    HAVE_TK = True
except Exception:                                     # noqa: BLE001
    HAVE_TK = False


# ============================================================
# 纯逻辑（不碰界面，所以可以离线自测）
# ============================================================

def rows_to_tsv(rows, columns):
    """把表格拍成制表符分隔的文本，直接粘进 Excel / 腾讯文档。"""
    out = ["\t".join(h for _, h in columns)]
    for r in rows:
        out.append("\t".join(_cell(r.get(k)) for k, _ in columns))
    return "\n".join(out)


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


# 表格行色（③ 工资表 / ④ 清单共用）：达标绿、不达标红、未测橙。
# 未测单独一个颜色是有用意的：它**不是**达标，但也**不是**判过的不达标 ——
# 差的是数据（cookie 失效 / 抓取失败），重新抓一次就好了，别跟不达标混成一片红。
ROW_COLORS = {"pass": "#1f6e43", "fail": "#b3261e", "untested": "#8a6d0b"}


def status_tag(status):
    """考核状态 → 行色标签。"""
    return {"达标": "pass"}.get(status, "untested" if status == "未测" else "fail")


def fit_column_width(texts, min_w, max_w, pad=18):
    """
    按一列的内容量出它该多宽，夹在 [min_w, max_w] 之间。

    「结果」列用它：内容短就窄（不白占屏幕），内容长就长到上限，
    再长也不撑破窗口 —— 拖表格底部的横向滚动条看。
    """
    try:
        f = tkfont.nametofont("TkDefaultFont")
        widest = max([f.measure(str(t)) for t in texts if str(t).strip()] or [0])
    except Exception:                                  # noqa: BLE001
        return min_w
    return int(max(min_w, min(max_w, widest + pad)))


# ③ 工资表列宽：全列按内容自适应（见 _fit_pay_cols），这里只作插行前的初始宽。
# 除「结果」外的列加起来 ≈ 798px，默认窗口（1080）下全都看得见；
# 「结果」列插完行现量一遍（夹在 PAY_NOTE_MIN ~ PAY_NOTE_MAX），
# 装不下就拖表格底部的横向滚动条。
PAY_COL_WIDTH = {
    "uid": 52, "plan_id": 44, "username": 96, "check_raw": 130,
    "vol_text": 90, "measured_count": 68, "quota_text": 60,
    "status": 52, "salary": 76, "gift": 72, "gap_text": 86, "note": 180,
}
PAY_COL_LEFT = ("username", "check_raw", "gap_text", "note")
# 「结果」列的宽度区间：短就短（不白占屏），超长最多长到上限，再长拖横向滚动条
PAY_NOTE_MIN, PAY_NOTE_MAX = 150, 320          # ③ 工资表
PAY2_RESULT_MIN, PAY2_RESULT_MAX = 96, 320     # ④ 发放清单「结果」列
PAY2_STATE_MIN, PAY2_STATE_MAX = 66, 150       # ④ 发放清单「状态」列
PAY_COL_MAX = 240                              # 其余列的内容自适应上限

# ④ 清单列宽：列不多，一屏全能放下（不用横向滚动）。
# 「状态」= 这个人本周期已经发了几次（待发 / 已发 n 次 / 发完），
# 「结果」= 本次点「发放」的结果。这两列都按内容现量宽（见 _render_payout_tree）。
PAY2_COLUMNS = (
    ("no", "序", 44), ("pick", "选", 40), ("uid", "UID", 76),
    ("plan", "方案", 60), ("username", "用户名", 150),
    ("salary", "月薪(实收)", 96), ("amount", "应赠送", 92),
    ("status", "考核", 64), ("state", "状态", 88), ("result", "结果", 110),
)
# ④ 表里「状态」「结果」各是第几列（0 起算）—— 后台线程回填时用
PAY2_RESULT_IDX = [c for c, _, _ in PAY2_COLUMNS].index("result")
PAY2_STATE_IDX = [c for c, _, _ in PAY2_COLUMNS].index("state")


def metrics_from_flags(use_volume, use_count):
    """复选框 → assessment.metrics。"""
    m = []
    if use_volume:
        m.append("volume")
    if use_count:
        m.append("count")
    return m


def plans_from_rows(rows, metrics):
    """
    界面表格 → config 里的 plans 数组。

    rows 是 [(id, min_volume_tb, min_count, salary), ...]（字符串或数字都行）
    用不到的考核项不写进去，免得配置里挂一堆没意义的 0。
    """
    plans = []
    for i, r in enumerate(rows, 1):
        pid = str(r[0] or "").strip()
        if not pid:
            continue
        p = {"id": pid}
        if "volume" in metrics:
            p["min_volume_tb"] = _num(r[1], f"第 {i} 行 {pid} 的体积门槛")
        if "count" in metrics:
            p["min_count"] = _num(r[2], f"第 {i} 行 {pid} 的数量门槛")
        sal = _num(r[3], f"第 {i} 行 {pid} 的月薪")
        if sal is None:
            sal = 0
        p["salary"] = int(sal)
        plans.append(p)
    if not plans:
        raise ValueError("方案表是空的，至少要有一套方案")
    return plans


def _num(v, what):
    if v is None or str(v).strip() == "":
        return None
    try:
        f = float(str(v).strip())
    except ValueError:
        raise ValueError(f"{what} 不是数字：{v!r}") from None
    return int(f) if f == int(f) else f


# ④ 页「结算周期」：单位下拉（显示名 → 配置值），只决定周期键长什么样
PERIOD_UNITS = (("月", "month"), ("周", "week"), ("日", "day"))


def period_key(unit, when=None):
    """
    按单位算周期键 —— 台账里的 period 字段就是它。

    月 → 2026-09   周 → 2026-W37   日 → 2026-09-15
    """
    d = when or datetime.now()
    if unit == "week":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    if unit == "day":
        return d.strftime("%Y-%m-%d")
    return d.strftime("%Y-%m")


def periods_of(ledger_path):
    """台账里出现过的所有周期 + 状态，新周期在前。"""
    recs = ledger_mod.read_ledger(ledger_path)
    seen = []
    for r in recs:
        p = r.get("period")
        if p and p not in seen:
            seen.append(p)
    out = []
    for p in sorted(seen, reverse=True):
        st = ledger_mod.period_state(ledger_path, p)
        out.append({
            "period": p,
            "sent": st["sent"],
            "users": len(st["uids"]),
            "failed": st["failed"],
            "total": st["total"],
            "max_times": max([v["times"] for v in st["uids"].values()] or [0]),
        })
    return out


# ============================================================
# 界面
# ============================================================

class App:
    def __init__(self, root, cfg_path):
        self.root = root
        self.cfg_path = Path(cfg_path)
        self.q = queue.Queue()
        self.busy = False
        self.cfg = {}
        self.settings = None
        self.members = []
        self.rows = []
        self.columns = []
        self.items = []
        self._iid_by_rowno = {}           # ③ 表 row_no → 树 iid（边抓边刷用）
        self._period_err = ""             # ④ 页顶栏「次数」填得不对时的原话
        self.pay_progress = None          # ③ 页的过程显示，_tab_payroll 里创建
        self.pay_menu = None              # ③ 表格的右键菜单
        self.pay_menu2 = None             # ④ 表格的右键菜单
        self.period = datetime.now().strftime("%Y-%m")
        self.v = {}
        self._baseline = None             # 载入/上次保存时的界面快照（标题提示用）

        root.minsize(900, 600)

        self._setup_file_log()
        self._load_config()
        self._setup_style()
        self._build()

        # 默认窗口尺寸跟界面自然需求走：写死 740 时底部状态栏会被裁掉
        # （内容要求 ~794）。屏幕不够高就贴着屏幕收缩，用户还能手动拉大。
        root.update_idletasks()
        w = max(1080, root.winfo_reqwidth())
        h = max(740, min(root.winfo_reqheight(), root.winfo_screenheight() - 120))
        root.geometry(f"{w}x{h}")
        self._load_roster_offline()     # roster.csv 本身就是缓存：启动直接载入
        # 配置改没改，标题上要看得见：界面任何一个值一改（或者方案表一增一删一改），
        # 标题立刻多一句「配置有改动（未保存）」。基线 = 刚载入时的那份界面值。
        self._baseline = self._updates_or_none()
        for var in self.v.values():
            try:
                var.trace_add("write", self._refresh_title)
            except Exception:                          # noqa: BLE001
                pass
        self._refresh_title()
        self._drain()
        if self._log_path:
            self.log(f"日志写到：{self._log_path}"
                     "（界面不显示日志，点底部状态栏的「打开日志」按钮看）")

    # -------------------------------------------------- 文件日志

    def _setup_file_log(self):
        """界面日志同步落盘：logs/log_YYYY-MM-DD.log，按天一个，**全部保留**。"""
        self._log_path = None
        try:
            d = self.cfg_path.parent / "logs"
            d.mkdir(exist_ok=True)
            self._log_path = d / f"log_{datetime.now():%Y-%m-%d}.log"
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(f"\n===== 会话开始 "
                        f"{datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        except OSError:
            self._log_path = None       # 落盘失败不碍事，界面日志照常

    def _log_to_file(self, text):
        """带时间戳写进当天日志；写不进去就算了，绝不影响界面。"""
        if not self._log_path:
            return
        try:
            lines = str(text).splitlines() or [""]
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%H:%M:%S}] {lines[0]}\n")
                for ln in lines[1:]:
                    f.write(f"    {ln}\n")
        except OSError:
            pass

    def _open_log_folder(self):
        """打开今天的日志文件（记事本能看），方便直接翻或发人排查。"""
        if not self._log_path:
            messagebox.showinfo("没有日志", "日志文件没建起来（磁盘只读？）。")
            return
        self._log_to_file("打开日志文件")          # 顺手记一笔谁什么时候看的
        try:
            os.startfile(str(self._log_path))      # Windows：用默认程序打开
        except (AttributeError, OSError):
            try:
                os.startfile(str(self._log_path.parent))
            except (AttributeError, OSError):
                webbrowser.open(self._log_path.parent.as_uri())

    # -------------------------------------------------- 配置

    def _bootstrap_files(self):
        """首次运行自举：config.json / roster.csv 不在就从各自的 example 补一份。

        roster.csv 只在「配置就放在程序目录」时才补 —— 自测把 config 指到
        临时目录，不该往真实目录里塞名单。
        """
        cfgp = self.cfg_path
        if not cfgp.exists():
            ex = cfgp.with_name(nexus.CONFIG_EXAMPLE_NAME)
            if not ex.exists():
                ex = RES_ROOT / nexus.CONFIG_EXAMPLE_NAME
            if ex.exists():
                cfgp.write_text(ex.read_text(encoding="utf-8"), encoding="utf-8")
                self.log(f"[OK] 首次运行：已从 {ex.name} 生成 {cfgp.name}"
                         "（站点地址还是占位值，去 ① 页填成自己的站点）")
        if cfgp.parent.resolve() != ROOT.resolve():
            return
        rp = ROOT / "roster.csv"
        if not rp.exists():
            ex = ROOT / "roster.example.csv"
            if not ex.exists():
                ex = RES_ROOT / "roster.example.csv"
            if ex.exists():
                rp.write_text(ex.read_text(encoding="utf-8-sig"),
                              encoding="utf-8-sig")
                self.log(f"[OK] 首次运行：已从 {ex.name} 生成 {rp.name}"
                         "（里面是假数据，换成你自己的考核表）")

    def _load_config(self):
        self._bootstrap_files()
        cfgp = self.cfg_path
        try:
            self.cfg = nexus.read_jsonc(cfgp) if cfgp.exists() else {}
        except Exception as e:                         # noqa: BLE001
            self.cfg = {}
            print(f"[!] {cfgp} 读不出来：{e}")
        try:
            self.settings = settle_mod.validate_settings(self.cfg, str(cfgp))
        except Exception:                              # noqa: BLE001
            self.settings = None

    def _updates_or_none(self):
        """界面上的配置值（= 存盘会写进去的那些）；有填错的地方就返回 None。"""
        try:
            return self._collect_updates()
        except Exception:                              # noqa: BLE001
            return None

    def _has_unsaved_changes(self):
        """界面上的配置和「载入 / 上次保存」时对不上 → True（标题里给个提示）。"""
        if self._baseline is None:
            return False
        now = self._updates_or_none()
        return now is not None and now != self._baseline

    def _refresh_title(self, *_):
        """窗口标题 = 程序名 + 版本号（+ 配置有改动时的提示）。"""
        self.root.title(APP_NAME + " " + APP_VERSION
                        + ("　配置有改动（未保存）"
                           if self._has_unsaved_changes() else ""))

    def save_config(self, silent=False):
        """把界面上的值写回 config.json —— 走文本级替换，**注释不会丢**。"""
        try:
            updates = self._collect_updates()
        except ValueError as e:
            messagebox.showerror("有地方填得不对", str(e))
            return False

        text = self.cfg_path.read_text(encoding="utf-8")
        backup = self.cfg_path.with_suffix(".json.bak")
        backup.write_text(text, encoding="utf-8")

        changed = []
        for path, val in updates:
            try:
                new = nexus.patch_jsonc_value(text, path, val)
            except KeyError as e:
                self.log(f"[!] {e}")
                continue
            if new != text:
                changed.append(path)
            text = new

        self.cfg_path.write_text(text, encoding="utf-8")
        if changed and not silent:
            self.log(f"[OK] 已写入 {self.cfg_path.name}：{', '.join(changed)}")
            self.log(f"     （改前的原样备份在 {backup.name}，注释一行没动）")
        self._load_config()
        self._baseline = self._updates_or_none()       # 存过盘了 = 没有未保存改动
        self._refresh_title()
        # 改完配置立刻重算「发放」能不能点 —— 否则填好字段、保存了
        # 按钮还是灰的，人会以为没生效。
        if self.pay_menu2 is not None and self.items:
            self._refresh_payout_summary()
        return True

    def _collect_updates(self):
        g = self.v.get
        metrics = metrics_from_flags(g("m_volume").get(), g("m_count").get())
        if not metrics:
            raise ValueError("考核口径至少要选一项（体积 / 数量）")

        plans = plans_from_rows(self._plan_rows(), metrics)
        eps = {}
        for k, var in self.ep_vars.items():
            eps[k] = var.get().strip()
        if not g("base_url").get().strip():
            raise ValueError("站点地址不能为空")

        updates = [
            ("base_url", g("base_url").get().strip()),
            ("timeout_seconds", int(_num(g("timeout").get(), "超时秒数") or 25)),
            ("cookie", g("cookie").get().strip()),
            ("uid", int(_num(g("uid").get(), "我的 uid") or 0)),
            ("login_user", g("login_user").get().strip()),
            ("login_password", g("login_password").get()),
            ("login_form.action", g("login_action").get().strip()),
            ("login_form.fields.username", g("login_fu").get().strip()),
            ("login_form.fields.password", g("login_fp").get().strip()),
            ("assessment.metrics", metrics),
            ("plans", plans),
            ("payout.tax_rate", _num(g("tax_rate").get(), "税率系数")),
            ("payout.tax_flat", _num(g("tax_flat").get(), "固定税额")),
            ("payout.interval_seconds", _num(g("interval").get(), "发放间隔")),
            ("payout.rounds_per_period", self._pay_freq()),
            ("payout.period_unit", self._pay_unit_key()),
            ("payout.ledger", g("ledger").get().strip()),
            ("payout.message_template", g("msg_tpl").get()),
            ("gift_form", {
                "action": g("gift_action").get().strip(),
                "fields": {
                    "username": g("gift_user").get().strip(),
                    "amount": g("gift_amount").get().strip(),
                    "message": g("gift_msg").get().strip(),
                },
                "success_url": g("gift_success_url").get().strip(),
                "duplicate_url": g("gift_dup_url").get().strip(),
            }),
        ]
        for k, val in eps.items():
            updates.append((f"endpoints.{k}", val))
        return [(p, v) for p, v in updates if v is not None]

    def _plan_rows(self):
        out = []
        for iid in self.plan_tree.get_children():
            out.append(tuple(self.plan_tree.item(iid, "values")))
        return out

    # ---- ④ 页「结算周期」那两个值（写配置 / 算周期键都用它们）

    def _pay_unit_key(self):
        """单位下拉 → 配置值（month / week / day）。"""
        label = self.v["pay_unit"].get() if "pay_unit" in self.v else "月"
        return dict(PERIOD_UNITS).get(label, "month")

    def _pay_freq(self):
        """「每 [单位] [N] 次」里的 N —— **每人**本周期能领几次。填错就报出来。"""
        raw = (self.v["pay_freq"].get() if "pay_freq" in self.v else "") or "1"
        n = _num(raw, "每周期发放次数")
        if not n or int(n) < 1:
            raise ValueError(
                f"「每周期发放次数」要填 1 或更大的整数，现在填的是 {raw!r}")
        return int(n)

    def _sync_period_key(self, *_):
        """单位一变就把周期键重算一遍（手改过的周期键会被覆盖，这是有意的）。

        周期键一变，`_apply_period_edits` 跟着把每行状态重算一遍。
        """
        self.v["period"].set(period_key(self._pay_unit_key()))

    def _sync_items_from_ledger(self):
        """
        按**界面上那个周期键**把每行的「本周期已发次数 / 状态」重新对一遍台账。

        改次数 N 或手改周期键之后要立刻生效（不然人会以为改了没用），
        所以这里不用等点「生成清单」—— 只更新台账口径的两个字段，
        勾选和「结果」列一个字都不动。
        """
        st = ledger_mod.period_state(self._ledger_path(), self.period)
        limit = self._period_limit()
        for it in self.items:
            info = st["uids"].get(str(it["uid"])) or {}
            it["paid_times"] = int(info.get("times") or 0)
            it["paid"] = it["paid_times"] > 0
            it["paid_at"] = info.get("last_at") or ""
            it["state"] = payout_mod.pay_state(it["paid_times"], limit)

    def _apply_period_edits(self, *_):
        """
        ④ 页顶栏「每 [单位] [N] 次 / 周期键」一改就生效（边打边生效）。

        做两件事：按新周期键把每行的「状态」重新对一遍台账（本周期发了几次、
        是不是发完了）→ 重算汇总行和「发放」按钮。**不动勾选**，
        「结果」跟着 item 一起留着。
        """
        if getattr(self, "pay_tree2", None) is None:
            return                       # 界面还没搭完（构造期），先不管
        p = (self.v["period"].get() or "").strip() if "period" in self.v else ""
        if p:
            self.period = p
        if self.items:
            self._sync_items_from_ledger()
            self._render_payout_tree(keep_checks=True)
            self._refresh_payout_summary()

    def _save_period(self):
        """
        「保存」按钮：把界面上这两格（每 [单位] [N] 次 / 周期键）立刻存进 config.json。

        存完再走一遍 `_apply_period_edits` —— 每行「状态」、汇总、按钮
        全部按新值重算。`save_config` 内部会重新读一遍配置文件，所以
        `self.settings`（「生成清单」「发放」真正用的那份）也当场变成新值，
        不会出现「界面改了、跑起来还是旧的」。
        """
        try:
            limit = self._pay_freq()
        except ValueError as e:
            messagebox.showerror("周期填得不对", str(e))
            return
        if not self.save_config():
            return
        self._apply_period_edits()
        self.log(f"[OK] 周期已保存：每 {self.v['pay_unit'].get()} {limit} 次"
                 f" · 周期键 {self.period}")

    # -------------------------------------------------- 窗口骨架

    def _setup_style(self):
        if sys.platform == "win32":
            try:
                self.root.option_add("*Font", "{Microsoft YaHei UI} 9")
            except Exception:                          # noqa: BLE001
                pass
        style = ttk.Style()
        for name, size in (("Treeview", 9), ("Treeview.Heading", 9)):
            try:
                style.configure(name, rowheight=22)
            except Exception:                          # noqa: BLE001
                pass

    def _build(self):
        root = self.root
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=6, pady=(6, 0))

        for title, builder in (("① 站点与登录", self._tab_station),
                               ("② 考核与方案", self._tab_rules),
                               ("③ 算工资", self._tab_payroll),
                               ("④ 发放与台账", self._tab_payout)):
            frame = ttk.Frame(self.nb)
            self.nb.add(frame, text=title)
            builder(frame)

        # 底部只留一条状态栏。日志不再占界面地方 —— 全部写进
        # config.json 旁边的 logs/log_YYYY-MM-DD.log，想看就点「打开日志」。
        bottom = ttk.Frame(root)
        bottom.pack(fill="x", padx=6, pady=6)

        bar = ttk.Frame(bottom)
        bar.pack(fill="x")
        self.status = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status, anchor="w").pack(
            side="left", fill="x", expand=True)
        self.pb = ttk.Progressbar(bar, mode="determinate", length=220)
        self.pb.pack(side="right")
        ttk.Button(bar, text="打开日志",
                   command=self._open_log_folder).pack(side="right", padx=6)

        # 界面不显示日志，但内存里仍旧留一份（下面这个**没有 pack** 的 Text）：
        # 出问题时能回头把整条过程捞出来，也不会再占界面空间。
        self.logbox = tk.Text(root, wrap="none")
        self.logbox.configure(state="disabled")

    # -------------------------------------------------- 页签 ① 站点与登录

    def _tab_station(self, f):
        g = self.v
        cfg = self.cfg
        eps = dict(nexus.DEFAULT_ENDPOINTS)
        eps.setdefault("takelogin", "takelogin.php")
        eps.update(cfg.get("endpoints") or {})

        # 开源地址：单独一行挂在 ① 页最顶上，左对齐；蓝字可点，点了开浏览器
        repo = ttk.Frame(f)
        repo.pack(fill="x", padx=8, pady=(6, 0))
        ttk.Label(repo, text="开源地址：", foreground="#666").pack(side="left")
        repo_lab = ttk.Label(repo, text=REPO_URL, foreground="#2a6ea5",
                             cursor="hand2")
        repo_lab.pack(side="left")
        repo_lab.bind("<Button-1>", lambda _e: webbrowser.open(REPO_URL))

        top = ttk.LabelFrame(f, text="站点")
        top.pack(fill="x", padx=8, pady=6)
        g["base_url"] = tk.StringVar(value=cfg.get("base_url") or "")
        ttk.Label(top, text="站点地址").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(top, textvariable=g["base_url"], width=58).grid(
            row=0, column=1, columnspan=3, sticky="we", padx=6)
        ttk.Button(top, text="打开看看",
                   command=lambda: webbrowser.open(g["base_url"].get())).grid(
            row=0, column=4, padx=6)
        ttk.Label(top, text="（装在子目录就连子目录一起写，例如 https://host/nexusphp）",
                  foreground="#666").grid(row=1, column=1, sticky="w", padx=6)

        g["timeout"] = tk.StringVar(value=str(cfg.get("timeout_seconds") or 25))
        ttk.Label(top, text="超时(秒)").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(top, textvariable=g["timeout"], width=8).grid(
            row=2, column=1, sticky="w", padx=6)
        g["uid"] = tk.StringVar(value=str(cfg.get("uid") or ""))
        # 当前登录是谁 —— 站点说了算，只读；没验证前也一直显示着（红字「未取到」）
        g["whoami"] = tk.StringVar(value="")
        self.whoami_label = ttk.Label(top, textvariable=g["whoami"])
        self.whoami_label.grid(row=2, column=2, sticky="e", padx=6)
        self._set_whoami(None)
        ttk.Label(top, text="我的 uid").grid(row=2, column=3, sticky="e", padx=6)
        ttk.Entry(top, textvariable=g["uid"], width=12).grid(
            row=2, column=4, sticky="w")
        top.columnconfigure(1, weight=1)

        # ---------------- 接口路径 ----------------
        epf = ttk.LabelFrame(f, text="接口路径（各站二次开发版本不同，没有通用默认值）")
        epf.pack(fill="x", padx=8, pady=6)
        self.ep_vars = {}
        labels = {
            "login": "登录页",
            "takelogin": "登录提交",
            "user_details": "个人页",
            "gift_bonus": "赠送页",
        }
        # 注意：没有 seeding_list —— 做种数据直接从个人详情页解析，
        # 个别详情页没汇总行的站点才需要它，直接在 config.json 里手工加。
        cfg_eps = cfg.get("endpoints") or {}
        for i, (k, lab) in enumerate(labels.items()):
            self.ep_vars[k] = tk.StringVar(value=cfg_eps.get(k, eps.get(k, "")))
            ttk.Label(epf, text=lab, width=9).grid(
                row=i, column=0, sticky="w", padx=6, pady=2)
            ttk.Entry(epf, textvariable=self.ep_vars[k], width=68).grid(
                row=i, column=1, sticky="we", padx=6)
        ttk.Label(epf, text="以 / 开头 = 走站点根域；http(s):// = 原样用；"
                            "其他 = 相对站点地址。可用 {uid} {page}",
                  foreground="#666").grid(row=len(labels), column=1,
                                          sticky="w", padx=6, pady=2)
        epf.columnconfigure(1, weight=1)

        # ---------------- 登录态 ----------------
        logf = ttk.LabelFrame(f, text="登录态")
        logf.pack(fill="x", padx=8, pady=6)
        g["cookie"] = tk.StringVar(value=cfg.get("cookie") or "")
        ttk.Label(logf, text="cookie").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.cookie_entry = ttk.Entry(logf, textvariable=g["cookie"], width=70,
                                      show="*")
        self.cookie_entry.grid(row=0, column=1, columnspan=4, sticky="we", padx=6)
        self.show_cookie = tk.BooleanVar(value=False)
        ttk.Checkbutton(logf, text="显示", variable=self.show_cookie,
                        command=self._toggle_cookie).grid(row=0, column=5, padx=6)

        btns = ttk.Frame(logf)
        btns.grid(row=1, column=0, columnspan=6, sticky="w", padx=6, pady=4)
        for text, fn in (
                ("获取cookie", self.job_get_cookie),
                ("验证登录态", self.job_verify),
                ("探测赠送表单", self.job_probe_gift),
        ):
            ttk.Button(btns, text=text, command=fn).pack(side="left", padx=(0, 6))

        pw = ttk.Frame(logf)
        pw.grid(row=2, column=0, columnspan=6, sticky="we", padx=6, pady=4)
        g["login_user"] = tk.StringVar(value=cfg.get("login_user") or "")
        g["login_password"] = tk.StringVar(value="")
        ttk.Label(pw, text="账号").pack(side="left")
        ttk.Entry(pw, textvariable=g["login_user"], width=18).pack(side="left", padx=4)
        ttk.Label(pw, text="密码").pack(side="left")
        ttk.Entry(pw, textvariable=g["login_password"], width=18,
                  show="*").pack(side="left", padx=4)
        ttk.Button(pw, text="账号密码登录", command=self.job_password).pack(
            side="left", padx=6)
        ttk.Label(pw, text="（浏览器开着也能走这条路，不碰 cookie 库）",
                  foreground="#666").pack(side="left", padx=6)

        lf = dict(cfg.get("login_form") or {})
        lff = dict(lf.get("fields") or {})
        lrow = ttk.Frame(logf)
        lrow.grid(row=3, column=0, columnspan=6, sticky="we", padx=6, pady=4)
        g["login_action"] = tk.StringVar(value=lf.get("action") or "")
        g["login_fu"] = tk.StringVar(value=lff.get("username") or "")
        g["login_fp"] = tk.StringVar(value=lff.get("password") or "")
        ttk.Label(lrow, text="登录表单（留空就自动从登录页上认）").pack(side="left")
        for lab, key, w in (("提交到", "login_action", 18),
                            ("用户名字段", "login_fu", 12),
                            ("密码字段", "login_fp", 12)):
            ttk.Label(lrow, text=lab).pack(side="left", padx=(8, 2))
            ttk.Entry(lrow, textvariable=g[key], width=w).pack(side="left")


        ttk.Button(f, text="保存配置", command=self.save_config).pack(
            anchor="e", padx=10, pady=4)

    def _set_whoami(self, name):
        """
        ① 页右上角的「当前登录：xxx」（只读）。

        拿到用户名 = 绿字；没拿到（没验证 / 验证失败 / 站点认不出）= 红字「未取到」。
        永久占着这一格，不闪不消失 —— 免得让人怀疑「到底登录上没有」。
        """
        if name:
            self.v["whoami"].set(f"当前登录：{name}")
            self.whoami_label.configure(foreground="#1f6e43")
        else:
            self.v["whoami"].set("当前登录：未取到")
            self.whoami_label.configure(foreground="#b00020")

    def _toggle_cookie(self):
        self.cookie_entry.configure(show="" if self.show_cookie.get() else "*")

    # -------------------------------------------------- 页签 ② 考核与方案

    def _tab_rules(self, f):
        g = self.v
        cfg = self.cfg
        metrics = (cfg.get("assessment") or {}).get("metrics") or ["volume"]

        mf = ttk.LabelFrame(f, text="考核口径")
        mf.pack(fill="x", padx=8, pady=6)
        g["m_volume"] = tk.BooleanVar(value="volume" in metrics)
        g["m_count"] = tk.BooleanVar(value="count" in metrics)
        ttk.Checkbutton(mf, text="体积（保种总大小）",
                        variable=g["m_volume"]).pack(side="left", padx=8)
        ttk.Checkbutton(mf, text="数量（做种条数）",
                        variable=g["m_count"]).pack(side="left", padx=8)
        ttk.Label(mf, text="选两项时，两项都达标才算达标；方案表里必须配对应的门槛",
                  foreground="#666").pack(side="left", padx=8)

        pf = ttk.LabelFrame(f, text="方案与月薪（月薪口径 = 组员实收，工具自动反算要送多少）")
        pf.pack(fill="both", expand=True, padx=8, pady=6)
        cols = ("id", "vol", "cnt", "salary")
        self.plan_tree = ttk.Treeview(pf, columns=cols, show="headings", height=6)
        for c, h, w in (("id", "方案", 120), ("vol", "体积门槛(TB)", 120),
                        ("cnt", "数量门槛(个)", 120), ("salary", "月薪(实收)", 140)):
            self.plan_tree.heading(c, text=h)
            self.plan_tree.column(c, width=w, anchor="center")
        self.plan_tree.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        for p in (cfg.get("plans") or []):
            self.plan_tree.insert("", "end", values=(
                p.get("id", ""), p.get("min_volume_tb", ""),
                p.get("min_count", ""), p.get("salary", "")))

        side = ttk.Frame(pf)
        side.pack(side="right", fill="y", padx=6, pady=6)
        ttk.Button(side, text="添加方案", command=self._add_plan).pack(fill="x", pady=2)
        ttk.Button(side, text="删除选中", command=self._del_plan).pack(fill="x", pady=2)
        ttk.Label(side, text="双击单元格\n直接改", foreground="#666",
                  justify="left").pack(fill="x", pady=8)
        self.plan_tree.bind("<Double-1>", lambda e: self._edit_cell(self.plan_tree, e))

        po = dict(cfg.get("payout") or {})
        gf = ttk.LabelFrame(f, text="发放参数")
        gf.pack(fill="x", padx=8, pady=6)

        def row(r, label, key, default, width=10, hint=""):
            g[key] = tk.StringVar(value=str(po.get(key, default)))
            ttk.Label(gf, text=label, width=16, anchor="w").grid(
                row=r, column=0, sticky="w", padx=6, pady=3)
            ttk.Entry(gf, textvariable=g[key], width=width).grid(
                row=r, column=1, sticky="w")
            if hint:
                ttk.Label(gf, text=hint, foreground="#666").grid(
                    row=r, column=2, columnspan=3, sticky="w", padx=6)

        row(0, "税率系数", "tax_rate", 0.9, hint="实收 = 送出 × 系数 − 固定税额")
        row(1, "固定税额", "tax_flat", 4)
        row(2, "发放间隔(秒)", "interval", 10, hint="站点有限制，20 人 ≈ 200 秒")
        row(3, "台账文件", "ledger", "payout_ledger.jsonl", 28,
            hint="append-only 的 JSONL，发放次数全靠它，别删")
        row(4, "留言模板", "msg_tpl", "保种组 {period} 月薪 · {plan_id}", 40,
            hint="可用 {period} {plan_id} {username}")
        # 发几次只认 ④ 页顶栏那一格：台账里那个人的 gift 条数就是唯一的判断依据。

        gff = ttk.LabelFrame(f, text="赠送表单（真实发放必须配全，不配就禁止真发）")
        gff.pack(fill="x", padx=8, pady=6)
        gfc = dict(cfg.get("gift_form") or {})
        gfields = dict(gfc.get("fields") or {})
        g["gift_action"] = tk.StringVar(value=gfc.get("action") or "")
        g["gift_user"] = tk.StringVar(value=gfields.get("username") or "")
        g["gift_amount"] = tk.StringVar(value=gfields.get("amount") or "")
        g["gift_msg"] = tk.StringVar(value=gfields.get("message") or "")
        g["gift_success_url"] = tk.StringVar(
            value=gfc.get("success_url") or payout_mod.DEFAULT_SUCCESS_URL)
        g["gift_dup_url"] = tk.StringVar(
            value=gfc.get("duplicate_url") or payout_mod.DUPLICATE_URL)
        for i, (lab, key) in enumerate((("提交目标", "gift_action"),
                                        ("收礼人字段", "gift_user"),
                                        ("金额字段", "gift_amount"),
                                        ("留言字段", "gift_msg"),
                                        ("成功URL后缀", "gift_success_url"),
                                        ("重复URL后缀", "gift_dup_url"))):
            ttk.Label(gff, text=lab, width=11, anchor="w").grid(
                row=i, column=0, sticky="w", padx=6, pady=2)
            ttk.Entry(gff, textvariable=g[key], width=56).grid(
                row=i, column=1, sticky="we", padx=6)
        ttk.Label(gff, text="发放成没成只看落点 URL，页面上的字一个都不看 ——"
                            "成功响应里本来也没有任何文字。\n"
                            "成功URL后缀：送出成功后页面跳的地址里认这个串，"
                            "默认 do=transfer（= mybonus.php?do=transfer）。\n"
                            "重复URL后缀：10 秒内重复点会跳 do=duplicated，"
                            "站点自己说「这笔没送出」，是唯一允许自动重试的失败。\n"
                            "两个都清空 = 没有成功判据，会拒绝真实发放。",
                  foreground="#666", wraplength=760, justify="left").grid(
            row=6, column=1, sticky="w", padx=6, pady=2)
        gff.columnconfigure(1, weight=1)

        ttk.Button(f, text="保存配置", command=self.save_config).pack(
            anchor="e", padx=10, pady=4)

    def _add_plan(self):
        self.plan_tree.insert("", "end", values=("新方案", "", "", ""))
        self._refresh_title()             # 加了一行 = 配置有改动

    def _del_plan(self):
        for iid in self.plan_tree.selection():
            self.plan_tree.delete(iid)
        self._refresh_title()

    def _edit_cell(self, tree, event):
        """双击单元格就地编辑。"""
        row = tree.identify_row(event.y)
        col = tree.identify_column(event.x)
        if not row or not col:
            return
        x, y, w, h = tree.bbox(row, col)
        idx = int(col.replace("#", "")) - 1
        old = tree.item(row, "values")[idx]
        var = tk.StringVar(value=old)
        ent = ttk.Entry(tree, textvariable=var)
        ent.place(x=x, y=y, width=w, height=h)
        ent.focus_set()
        ent.select_range(0, "end")

        def commit(_=None):
            vals = list(tree.item(row, "values"))
            vals[idx] = var.get()
            tree.item(row, values=vals)
            ent.destroy()
            self._refresh_title()         # 方案表改了一格 = 配置有改动

        ent.bind("<Return>", commit)
        ent.bind("<FocusOut>", commit)
        ent.bind("<Escape>", lambda e: ent.destroy())

    # -------------------------------------------------- 页签 ③ 算工资

    def _tab_payroll(self, f):
        g = self.v
        top = ttk.Frame(f)
        top.pack(fill="x", padx=8, pady=6)
        g["roster"] = tk.StringVar(value=str(ROOT / "roster.csv"))
        ttk.Label(top, text="考核表").pack(side="left")
        ttk.Entry(top, textvariable=g["roster"], width=58).pack(
            side="left", padx=6)
        ttk.Button(top, text="浏览…", command=self._pick_roster).pack(side="left")
        ttk.Button(top, text="重新载入", command=self._load_roster_offline).pack(
            side="left", padx=4)
        ttk.Label(top, text="支持 xlsx / csv",
                  foreground="#666").pack(side="left", padx=8)

        # 计算按钮单独一行（和考核表选择分开，一眼能找到）
        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Button(btns, text="联网计算",
                   command=self.job_payroll_online).pack(side="left")
        ttk.Button(btns, text="选中重算",
                   command=self.job_payroll_selected).pack(side="left", padx=8)
        ttk.Label(btns, text="联网计算每人约 1 秒、边抓边显示；跑完自动勾上"
                            "未测 / 不达标的行；「选中重算」把勾选的行"
                            "全部重抓一遍（抓不到就记「未测」）",
                  foreground="#666").pack(side="left", padx=8)

        # 过程显示：联网计算是长流程（每人一秒），必须让人看见在动。
        # 算完不清空，保留一行总结。
        self.pay_progress = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.pay_progress, foreground="#2a6ea5").pack(
            fill="x", padx=10, pady=(0, 2))

        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True, padx=8, pady=4)
        self.pay_tree = ttk.Treeview(mid, show="headings", selectmode="extended")
        self.pay_tree.bind("<Button-1>", self._toggle_check_pay)
        self.pay_tree.bind("<Double-1>", self._edit_pay_cell)
        self.pay_menu = self._install_check_menu(self.pay_tree)
        ysb = ttk.Scrollbar(mid, orient="vertical", command=self.pay_tree.yview)
        xsb = ttk.Scrollbar(mid, orient="horizontal", command=self.pay_tree.xview)
        self.pay_tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        ysb.pack(side="right", fill="y")
        xsb.pack(side="bottom", fill="x")
        self.pay_tree.pack(side="left", fill="both", expand=True)

        bot = ttk.Frame(f)
        bot.pack(fill="x", padx=8, pady=6)
        self.pay_summary = tk.StringVar(
            value="启动时已自动载入考核表 roster.csv（它就是缓存）；要刷新实测点「联网计算」。")
        ttk.Label(bot, textvariable=self.pay_summary, anchor="w",
                  justify="left").pack(side="left", fill="x", expand=True)
        # pack(side="right") 是「每 pack 一个就越靠左」，所以调用顺序 = 从右数。
        # 目标（从左到右）：[复制表格] [导出 CSV] [全选] [全不选]
        ttk.Button(bot, text="全不选",
                   command=lambda: self._check_all(False, self.pay_tree)).pack(
            side="right", padx=4)
        ttk.Button(bot, text="全选",
                   command=lambda: self._check_all(True, self.pay_tree)).pack(
            side="right")
        ttk.Button(bot, text="导出 CSV", command=self._export_csv).pack(
            side="right", padx=4)
        ttk.Button(bot, text="复制表格", command=self._copy_table).pack(side="right")

    def _pick_roster(self):
        p = filedialog.askopenfilename(
            title="选考核表",
            initialdir=str(ROOT),
            filetypes=[("表格", "*.xlsx *.xls *.csv"), ("所有文件", "*.*")])
        if p:
            self.v["roster"].set(p)
            self._load_roster_offline()

    def _load_roster_offline(self):
        """把考核表**离线载入**进 ③ 表格：选完文件立刻显示；启动时也走这里
        （roster.csv 本身就是缓存 —— 实测结果每次算完都写回它）。
        外部改完 roster.csv 点「重新载入」即可同步进来，程序不锁这个文件。
        """
        if self.busy:
            self.log("[!] 当前有任务在跑，等它结束再载入考核表")
            return
        if self.settings is None:
            self.log("[!] 配置还没校验通过（② 页的口径 / 方案），载不了工资表")
            return
        p = Path((self.v["roster"].get() or "").strip())
        if not p.exists():
            return
        try:
            members = roster_mod.load_roster(p)
        except Exception as e:                          # noqa: BLE001
            messagebox.showerror("考核表读不出来", f"{p}\n\n{e}")
            return
        if not members:
            messagebox.showinfo("空表", f"{p} 里没读到人。")
            return
        self.q.put(("payreset", members))
        self._prog1(f"已载入考核表：{len(members)} 人"
                    "（离线，表内数据；要刷新实测点「联网计算」）")
        self.log(f"[OK] 已载入 {len(members)} 人（{p.name}）"
                 "—— 实测列用的是表里的值")

    def _export_csv(self):
        if not self.rows:
            messagebox.showinfo("没有数据", "先算一次工资。")
            return
        p = filedialog.asksaveasfilename(
            title="导出工资表", defaultextension=".csv",
            initialfile=f"payroll_{self.period}.csv",
            initialdir=str(ROOT))
        if not p:
            return
        settle_mod.write_payroll_csv(self.rows, self.columns, p)
        self.log(f"[OK] 已导出 {p}（UTF-8 BOM，Excel 直接双击不乱码）")

    def _copy_table(self):
        if not self.rows:
            messagebox.showinfo("没有数据", "先算一次工资。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(rows_to_tsv(self.rows, self.columns))
        self.log(f"[OK] 已复制 {len(self.rows)} 行到剪贴板，直接粘进表格即可")

    # -------------------------------------------------- 页签 ④ 发放与台账

    def _tab_payout(self, f):
        g = self.v
        po = dict(self.cfg.get("payout") or {})
        unit_key = po.get("period_unit") or "month"
        unit_label = next((lab for lab, k in PERIOD_UNITS if k == unit_key), "月")
        self.period = period_key(unit_key)          # 周期键默认按单位取当前周期

        top = ttk.Frame(f)
        top.pack(fill="x", padx=8, pady=6)
        # 周期可选：单位（月 / 周 / 日）+ 每周期发几次，默认「每 月 1 次」。
        # 周期键（台账里的 period）跟着单位自动算，也能手改成想结算的那一期。
        ttk.Label(top, text="结算周期").pack(side="left")
        ttk.Label(top, text="每").pack(side="left", padx=(6, 2))
        g["pay_unit"] = tk.StringVar(value=unit_label)
        cb = ttk.Combobox(top, textvariable=g["pay_unit"], width=4, state="readonly",
                          values=[lab for lab, _ in PERIOD_UNITS])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", self._sync_period_key)
        g["pay_freq"] = tk.StringVar(value=str(po.get("rounds_per_period", 1) or 1))
        ttk.Entry(top, textvariable=g["pay_freq"], width=4).pack(side="left", padx=2)
        ttk.Label(top, text="次").pack(side="left", padx=(0, 12))
        ttk.Label(top, text="周期键").pack(side="left")
        g["period"] = tk.StringVar(value=self.period)
        ttk.Entry(top, textvariable=g["period"], width=12).pack(side="left", padx=6)
        # 「保存」= 把这两格当场写进 config.json（不用等别的动作顺手存盘）。
        ttk.Button(top, text="保存", command=self._save_period).pack(
            side="left", padx=(2, 0))
        # 「次数」和「周期键」**边打边生效**：不用等点「生成清单」——
        # 次数决定每人本周期能领几次（「已发 2 次」还是「发完」），
        # 周期键决定拿哪一段台账来数。
        g["pay_freq"].trace_add("write", self._apply_period_edits)
        g["period"].trace_add("write", self._apply_period_edits)

        # 清单那几个按钮和提示**另起一行**，免得字一长被挤到可视区外
        top2 = ttk.Frame(f)
        top2.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Button(top2, text="生成清单", command=self.job_plan_payout).pack(
            side="left")
        ttk.Button(top2, text="台账状态", command=self.job_ledger).pack(
            side="left", padx=8)
        ttk.Label(top2, text="不达标 / 未测 / 已发的也会列出来，默认不勾",
                  foreground="#666").pack(side="left")

        # 发放过程显示（照搬 ③ 的做法）：没开始时藏住，发放时逐人刷新
        self.pay_progress2 = tk.StringVar(value="")
        self.pay_prog2_label = ttk.Label(f, textvariable=self.pay_progress2,
                                         foreground="#2a6ea5", justify="left",
                                         wraplength=1000)
        self.pay_prog2_label.pack(fill="x", padx=8, pady=(0, 2))

        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True, padx=8, pady=4)
        cols = tuple(c for c, _, _ in PAY2_COLUMNS)
        self.pay_tree2 = ttk.Treeview(mid, columns=cols, show="headings")
        for c, h, w in PAY2_COLUMNS:
            self.pay_tree2.heading(c, text=h)
            # 都不跟着窗口拉伸；「结果」列重绘时按内容量一遍（_render_payout_tree）
            self.pay_tree2.column(
                c, width=w, minwidth=(PAY2_RESULT_MIN if c == "result" else 40),
                anchor="w" if c in ("username", "result") else "center",
                stretch=False)
        # 整行上色（照 ③ 的三色风格）：待发 = 橙，本周期发过（或本次成功）= 绿，
        # 不发（不达标 / 未测）= 红。待发用橙是有用意的 —— 它是「还得你动手」的状态，
        # 跟「已经发完了」的绿、跟「发不了」的红都不该混成一个颜色。
        self.pay_tree2.tag_configure("ready", foreground=ROW_COLORS["untested"])
        self.pay_tree2.tag_configure("done", foreground=ROW_COLORS["pass"])
        self.pay_tree2.tag_configure("blocked", foreground=ROW_COLORS["fail"])
        self.pay_tree2.pack(side="left", fill="both", expand=True)
        ysb = ttk.Scrollbar(mid, orient="vertical", command=self.pay_tree2.yview)
        self.pay_tree2.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.pay_tree2.bind("<Button-1>", self._toggle_check)
        self.pay_menu2 = self._install_check_menu(
            self.pay_tree2, after=self._refresh_payout_summary,
            reset_cmd=self._reset_period)

        bot = ttk.Frame(f)
        bot.pack(fill="x", padx=8, pady=6)
        self.pay_summary2 = tk.StringVar(
            value="还没生成。先在上面选好周期，点「生成清单」（这一步只看不发）。")
        # ★ 汇总可能很长，**必须单独占一行**并按宽度折行 ——
        #   和按钮挤在同一行的话，字一长就把右边的按钮整块挤出可视区。
        self.pay_sum2_label = ttk.Label(bot, textvariable=self.pay_summary2,
                                        anchor="w", justify="left",
                                        wraplength=960)
        self.pay_sum2_label.pack(fill="x")

        def _rewrap(ev, lb=self.pay_sum2_label):
            w = max(240, ev.width - 6)
            if abs(float(lb.cget("wraplength") or 0) - w) > 8:
                lb.configure(wraplength=w)

        self.pay_sum2_label.bind("<Configure>", _rewrap)

        row = ttk.Frame(bot)
        row.pack(fill="x", pady=(4, 0))
        # 同上：pack(side="right") 越 pack 越靠左。
        # 目标（从左到右）：[全选] [全不选] [重置周期] [发放]
        self.btn_run = ttk.Button(row, text="发放", command=self.job_execute,
                                  state="disabled")
        self.btn_run.pack(side="right", padx=10)
        ttk.Button(row, text="重置周期",
                   command=self._reset_period).pack(side="right", padx=4)
        ttk.Button(row, text="全不选", command=lambda: self._check_all(
            False, self.pay_tree2, self._refresh_payout_summary)).pack(
            side="right", padx=4)
        ttk.Button(row, text="全选", command=lambda: self._check_all(
            True, self.pay_tree2, self._refresh_payout_summary)).pack(
            side="right")

    # ---- 勾选框：两个表格（③ 算工资 / ④ 清单）共用同一套交互

    def _toggle_check_pay(self, event):
        """③ 表格：#1 是序号，#2 才是勾选框。"""
        self._toggle_row(self.pay_tree, event)

    def _toggle_check(self, event):
        """④ 表格同款。"""
        self._toggle_row(self.pay_tree2, event, self._refresh_payout_summary)

    def _toggle_row(self, tree, event, after=None):
        if tree.identify_column(event.x) != "#2":      # #1 是序号
            return
        iid = tree.identify_row(event.y)
        if not iid:
            return
        vals = list(tree.item(iid, "values"))
        if len(vals) < 2:
            return
        vals[1] = "☐" if vals[1] == "☑" else "☑"
        tree.item(iid, values=vals)
        if after:
            after()

    def _check_all(self, on, tree=None, after=None):
        """整列打勾 / 取消（两个表格共用）。"""
        tree = tree if tree is not None else self.pay_tree2
        for iid in tree.get_children():
            vals = list(tree.item(iid, "values"))
            if len(vals) < 2:
                continue
            vals[1] = "☑" if on else "☐"
            tree.item(iid, values=vals)
        # 只有 ④ 的勾选才影响「能不能发」，③ 的勾选不用去动那句汇总
        if after is None and tree is self.pay_tree2:
            after = self._refresh_payout_summary
        if after:
            after()

    def _install_check_menu(self, tree, after=None, reset_cmd=None):
        """
        表格右键菜单：勾选 / 取消勾选 / 反选 / 全局反选（④ 再加「重置周期」）。

        前三个作用对象 = 当前选中的行；如果右键那条**不在**选中集里，
        就只改这一条（和资源管理器一个习惯）。
        「全局反选」无视选中集，整张表所有行一起翻 —— 勾选状态只存在表格里。
        reset_cmd 给了才会多出「重置周期」，它同样认这一套目标行规则。
        """
        menu = tk.Menu(tree, tearoff=0)
        state = {"row": None}

        def targets():
            iids = list(tree.selection())
            row = state.get("row")
            if row and row not in iids:
                iids = [row]
            return iids

        def apply(mode):
            self._flip_checks(tree, targets(), mode)
            if after:
                after()

        def apply_all(mode):
            self._flip_checks(tree, tree.get_children(), mode)
            if after:
                after()

        menu.add_command(label="勾选", command=lambda: apply("on"))
        menu.add_command(label="取消勾选", command=lambda: apply("off"))
        menu.add_command(label="反选", command=lambda: apply("flip"))
        menu.add_command(label="全局反选", command=lambda: apply_all("flip"))
        if reset_cmd is not None:
            menu.add_separator()
            menu.add_command(label="重置周期",
                             command=lambda: reset_cmd(targets()))

        def popup(event):
            state["row"] = tree.identify_row(event.y)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        tree.bind("<Button-3>", popup)
        tree.bind("<Button-2>", popup)          # macOS 的右键
        return menu

    @staticmethod
    def _flip_checks(tree, iids, mode):
        for iid in iids:
            vals = list(tree.item(iid, "values"))
            if len(vals) < 2:
                continue
            if mode == "on":
                vals[1] = "☑"
            elif mode == "off":
                vals[1] = "☐"
            else:
                vals[1] = "☐" if vals[1] == "☑" else "☑"
            tree.item(iid, values=vals)

    def _refresh_payout_summary(self):
        if not self.pay_tree2.get_children():
            return                    # ④ 还没生成清单，别动那句初始提示
        try:
            limit = self._pay_freq()
            self._period_err = ""
        except ValueError as e:
            limit = 1
            self._period_err = str(e)
        picked = self._picked_items()
        total = sum(i["amount"] for i in picked)
        msg = (f"已勾选 {len(picked)} 人，应赠合计 {total:,}"
               f"（每人本周期可领 {limit} 次）")
        part = [i for i in picked if 0 < int(i.get("paid_times") or 0) < limit]
        full = [i for i in picked if int(i.get("paid_times") or 0) >= limit]
        bad = [i for i in picked if i.get("status") != "达标"]
        if part:
            msg += f"　[!] 里面有 {len(part)} 人本周期已发过（这次会再发一笔）"
        if full:
            msg += (f"　[!] 里面有 {len(full)} 人已发满 {limit} 次"
                    "（会被跳过；要重发先「重置周期」）")
        if bad:
            msg += f"　[!] 里面有 {len(bad)} 人不达标 / 未测"
        # 能不能真发 = 勾了人 + 表单配全 + 勾的人里还有没发满的，缺哪样说哪样
        block = self._payout_block(picked)
        if not block and picked:
            msg += "（点「发放」会先弹确认框核对金额）"
        if block:
            msg += f"　— 还不能发：{block}"
        self.pay_summary2.set(msg)
        if self.btn_run is not None:
            self.btn_run.configure(state="disabled" if block else "normal")

    def _payout_block(self, picked):
        """返回「为什么现在不能发」，能发就返回 None。"""
        if getattr(self, "_period_err", ""):
            return self._period_err      # 「每周期发放次数」填得不对
        problems = payout_mod.validate_gift_form(self.cfg.get("gift_form"))
        if problems:
            return "赠送表单没配全（见日志）"
        if not picked:
            return "一个人都没勾"
        limit = self._period_limit()
        if not [i for i in picked if int(i.get("paid_times") or 0) < limit]:
            return f"勾上的人本周期都发满了 {limit} 次 —— 要重发先「重置周期」"
        return None

    def _picked_items(self):
        """④ 清单里打了勾的人。状态只认表格本身，没有第二份副本。"""
        picked = []
        for iid in self.pay_tree2.get_children():
            vals = self.pay_tree2.item(iid, "values")
            if len(vals) > 1 and vals[1] == "☑":
                picked.append(self.items[int(iid)])
        return picked

    def _pay2_state(self, it):
        """
        「状态」列要显示的文字 —— 这个人**本周期**已经发了几次：

          还没发过      → 待发
          发了没发满    → 已发 n 次
          发满设定次数  → 发完
        """
        return payout_mod.pay_state(it.get("paid_times"), self._period_limit())

    def _pay2_tag(self, it):
        """
        ④ 清单的行色（照 ③ 的三色风格）：

          本次结果 = 成功   → 绿 done      这一笔真的发出去了
          本周期发过        → 绿 done      账上已经有他了（含「发完」）
          待发             → 橙 ready     还得你动手（发送中、发失败也在这档）
          不发             → 红 blocked   不达标 / 未测，不该给他发

        按「更新后的这一行」现算，不认调用方传的 tag —— 这样「成功 → 变绿」
        不管从哪条路回填都成立。
        """
        if (it.get("result") or "") == "成功":
            return "done"
        if it.get("status") != "达标":
            return "blocked"
        return "done" if it.get("paid_times") else "ready"

    def _render_payout_tree(self, keep_checks=False):
        """
        按 self.items 重绘 ④ 清单。

        行色：待发橙 / 本周期发过·本次成功绿 / 不发红（见 _pay2_tag）。
        勾选：keep_checks=True 时沿用表格里**已勾的那些 uid**（重置 / 改周期键
        重绘用），否则用清单给的 default_pick（达标 + 本周期没发满才默认勾）。
        列：状态 = 本周期已发几次（待发 / 已发 n 次 / 发完），结果 = 本次发放结果。
        """
        t = self.pay_tree2
        old_uids = set()
        if keep_checks:
            for iid in t.get_children():
                vals = t.item(iid, "values")
                j = int(iid) if str(iid).isdigit() else -1
                if (len(vals) > 1 and vals[1] == "☑"
                        and 0 <= j < len(self.items)):
                    old_uids.add(self.items[j]["uid"])
        t.delete(*t.get_children())
        for i, it in enumerate(self.items):
            iid = str(i)
            # 勾选只认表格本身（没有第二份副本），重绘时按 uid 认回来 ——
            # 不能按行号认：清单顺序一变，勾就会落到别人头上（发钱的事，不能赌）
            pick = (it["uid"] in old_uids if keep_checks
                    else bool(it.get("default_pick", True)))
            state = self._pay2_state(it) if it.get("status") == "达标" else "不发"
            it["state"] = state
            t.insert("", "end", iid=iid, tags=(self._pay2_tag(it),), values=(
                i + 1, "☑" if pick else "☐", it["uid"], it["plan"] or "",
                it["username"] or "-", it["salary"] or "",
                f"{it['amount']:,}", it["status"], state,
                it.get("result") or ""))
        # 全列按内容自适应：「已发 2 次」比「待发」宽，长报错也比短的长 ——
        # 写死宽度不是白占一长条就是被切掉（超长到上限就拖横向滚动条）
        self._fit_pay2_cols()

    def _fit_pay2_cols(self):
        """④ 清单全列按内容自适应：表头 + 每行都量一遍。"""
        t = self.pay_tree2
        keys = [c for c, _, _ in PAY2_COLUMNS]
        texts = {k: [t.heading(k)["text"]] for k in keys}
        for iid in t.get_children():
            for k, v in zip(keys, t.item(iid, "values")):
                texts[k].append(v)
        for k in keys:
            lo, hi = {"state": (PAY2_STATE_MIN, PAY2_STATE_MAX),
                      "result": (PAY2_RESULT_MIN, PAY2_RESULT_MAX)}.get(
                          k, (40, PAY_COL_MAX))
            t.column(k, width=fit_column_width(texts[k], lo, hi))

    def _payout_row_state(self, idx, state, result, tag=None):
        """
        ④ 清单里单行的发放刷新（后台线程发队列，主线程刷）。

        state 传 None 表示「按台账现算」—— 每成功一笔，paid_times 加一，
        状态就从「待发」变成「已发 1 次」，发满设定次数就是「发完」。
        """
        t = self.pay_tree2
        iid = str(idx)
        if not t.exists(iid):
            return
        vals = list(t.item(iid, "values"))
        if len(vals) < len(PAY2_COLUMNS):
            return
        if idx < len(self.items):
            it = self.items[idx]
            it["result"] = result
            if state is None:
                state = self._pay2_state(it) if it.get("status") == "达标" else "不发"
            it["state"] = state
            tag = self._pay2_tag(it)          # 成功 → 当场变绿（见 _pay2_tag）
        vals[PAY2_STATE_IDX] = state if state is not None else vals[PAY2_STATE_IDX]
        vals[PAY2_RESULT_IDX] = result
        t.item(iid, values=vals, tags=(tag,))
        self._fit_pay2_cols()       # 状态 / 结果变长了就把列宽跟着撑开

    def _reset_period(self, iids=None):
        """
        重置周期 —— 把选中的人在本周期的发放记录作废（追加 reset，历史一条不删）。

        效果：他们的「本周期已发次数」归零（状态回「待发」），于是又能各领
        界面上那个「每 [单位] [N] 次」那么多次。只动这些人，别人一个字不改。
        """
        if not self.items:
            messagebox.showinfo("还没有清单", "先点上面的「生成清单」。")
            return
        if iids is None:
            iids = list(self.pay_tree2.selection())
        if not iids:
            messagebox.showinfo("没选中",
                                "先在清单里选中要重置的人（可按住 Ctrl 多选）。")
            return
        idxs = sorted({int(x) for x in iids if str(x).isdigit()})
        rows = [i for i in idxs
                if i < len(self.items) and self.items[i].get("uid")]
        if not rows:
            messagebox.showinfo("没什么可重置", "选中的行里没有可重置的人。")
            return

        targets = [self.items[i] for i in rows]
        led = self._ledger_path()
        limit = self._period_limit()
        detail = "".join(
            f"　· {t['username'] or t['uid']}（uid {t['uid']}）"
            f"　本周期已发 {int(t.get('paid_times') or 0)} 次\n" for t in targets)
        if not self._ask_ok(
                "重置周期",
                f"周期：{self.period}\n台账：{led}\n\n"
                f"要重置这 {len(targets)} 个人在本周期的记录：\n{detail}\n"
                f"重置后他们回到「待发」，又能各领 {limit} 次。\n"
                "只动这些人，别人一个字不改；台账是追加式的，历史一条不删。",
                ok_text="重置"):
            return

        rec = ledger_mod.reset_period(
            led, self.period, uids=[t["uid"] for t in targets],
            note="GUI 重置周期")
        for t in targets:
            t["paid"] = False
            t["paid_times"] = 0
            t["paid_at"] = ""
            t["result"] = ""          # 重置 = 回到没发过，这次的「成功/失败」清掉
            t["state"] = "待发"
        self._render_payout_tree(keep_checks=True)
        self._refresh_payout_summary()
        self.log(f"[OK] 已重置 {len(targets)} 人的本周期记录（{rec['at']}）—— "
                 f"他们又能各领 {limit} 次；台账只多了一条 reset，历史没删。")
        for t in targets:
            self.log(f"     {t['username'] or t['uid']}（uid {t['uid']}）回到「待发」")

    # -------------------------------------------------- 后台任务

    def run(self, title, fn, on_done=None, need_config=True, on_error=None):
        """
        跑一个后台任务。

        ★ tkinter 的控件只能在主线程里读 —— 所以进线程前先把界面上的值
        快照成普通 dict（self.snap），worker 只认快照，不碰控件。
        配置也在主线程里先存好，保证磁盘上的 config.json 就是界面上的样子。
        """
        if self.busy:
            messagebox.showinfo("正在忙", "上一件事还没做完，等它结束。")
            return
        if need_config and not self.save_config(silent=True):
            return
        if need_config and self.settings is None:
            messagebox.showerror("配置不对", "配置校验没过，看日志。")
            return
        self.snap = self._snapshot()

        self.busy = True
        self.pb.configure(mode="indeterminate")
        self.pb.start(60)
        self.status.set(f"{title} …")
        self.log("")
        self.log("=" * 66)
        self.log(title)

        def work():
            try:
                res = fn(self.log, self._progress, self.snap)
                self.q.put(("done", (title, on_done, res)))
            except Exception as e:                      # noqa: BLE001
                self.q.put(("error", (title, e, traceback.format_exc(), on_error)))

        threading.Thread(target=work, daemon=True).start()

    def _snapshot(self):
        """把界面上的值拷成普通 dict，供后台线程安全读取。"""
        s = {}
        for k, var in self.v.items():
            try:
                s[k] = var.get()
            except Exception:                          # noqa: BLE001
                s[k] = None
        return s

    def _progress(self, i, n, text=""):
        self.q.put(("progress", (i, n, str(text))))

    def _prog1(self, text):
        """③ 页自己的过程行（算工资 / 选中重算用）。"""
        self.q.put(("prog1", str(text)))

    def _prog2(self, text):
        """④ 页自己的过程行（真实发放用）。"""
        self.q.put(("prog2", str(text)))

    def log(self, msg=""):
        self.q.put(("log", str(msg)))

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "progress":
                    # 只动底部状态栏 + 进度条。各页签的过程行走各自的通道
                    # （③ = prog1，④ = prog2），互不串门。
                    i, n, text = payload
                    self.status.set(text or f"进行中 {i}/{n}")
                    self.pb.configure(mode="determinate", maximum=max(n, 1),
                                      value=min(i, n))
                elif kind == "prog1":
                    if self.pay_progress is not None:
                        self.pay_progress.set(str(payload))
                elif kind == "payreset":
                    self._payreset_rows(payload)
                elif kind == "payrow":
                    self._payupdate_row(payload)
                elif kind == "payrow2":
                    self._payout_row_state(*payload)
                elif kind == "prog2":
                    self.pay_progress2.set(str(payload))
                elif kind == "done":
                    title, cb, res = payload
                    self._finish()
                    if cb:
                        cb(res)
                elif kind == "error":
                    title, e, tb, cb = payload
                    self._finish()
                    self._append(tb)
                    self.log(f"[x] {title} 失败：{e}")
                    if cb:
                        cb(e)
                    messagebox.showerror(f"{title} 失败", str(e))
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _finish(self):
        self.busy = False
        self.pb.stop()
        self.pb.configure(mode="indeterminate")
        self.status.set("就绪")

    def _append(self, text):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", text + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")
        self._log_to_file(text)

    # -------------------------------------------------- 各任务实现

    # ---- cookie

    def job_get_cookie(self, open_browser=True):
        """获取 cookie：先秒读一次剪贴板，没有就**立刻**打开系统默认浏览器。

        剪贴板只读一次（毫秒级），读不到马上开浏览器 + 弹引导浮窗。
        浮窗里「我复制好了」走 open_browser=False，不再重复开浏览器。
        """

        def work(log, prog, snap):
            cookie, msg = login_mod.clipboard_cookie_once()
            if cookie:
                problem = login_mod.cookie_problem(cookie)
                if not problem:
                    log("  [OK] 剪贴板里已经有 cookie，直接用")
                    return cookie
                log("  [x] " + problem)
                return None
            log("  " + msg)

            if not open_browser:
                return None
            base = (snap["base_url"] or "").strip()
            ep = nexus.Endpoints(base, self.cfg.get("endpoints"))
            login_url = ep.url("login") if ep.has("login") else base
            if login_url:
                log(f"  打开系统默认浏览器：{login_url}")
                try:
                    webbrowser.open(login_url)
                except Exception as e:              # noqa: BLE001
                    log(f"  （浏览器没开成：{e}）")
            return None

        def done(cookie):
            if cookie:
                self._after_cookie(cookie)
            else:
                self.show_cookie_guide()

        self.run("获取 cookie", work, done)

    def show_cookie_guide(self):
        """获取失败时的浮窗引导（居中显示）：怎么把 cookie 交出来。"""
        win = tk.Toplevel(self.root)
        win.title("手动提供 cookie")
        win.transient(self.root)
        win.attributes("-topmost", True)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="剪贴板里没找到可用的 cookie",
                  foreground="#a33").pack(anchor="w")
        ttk.Label(frm, text="按下面三步把 cookie 交给我：").pack(
            anchor="w", pady=(8, 4))
        base = self.v["base_url"].get().strip() or "站点首页"
        for line in (
                f"1. 在浏览器里登录站点：{base}",
                "2. 按 F12 → 「网络 / Network」→ 刷新页面",
                "3. 点第一个请求 → 「请求标头」→ 找到 Cookie: 开头的一整行，复制"):
            ttk.Label(frm, text=line).pack(anchor="w", pady=1)
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="我复制好了",
                   command=lambda: (win.destroy(),
                                    self.root.after(
                                        50, lambda: self.job_get_cookie(
                                            open_browser=False)))
                   ).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side="left")
        # 居中到主窗口
        self._center_on_root(win)

    def job_password(self):
        def work(log, prog, snap):
            base = (snap["base_url"] or "").strip()
            user = (snap["login_user"] or "").strip()
            pwd = snap["login_password"] or ""
            if not user or not pwd:
                raise ValueError("账号和密码都要填")
            ep = nexus.Endpoints(base, self.cfg.get("endpoints"))
            log(f"提交到 {ep.path('login')} 所在目录……")
            cookie, msg = login_mod.password_login(
                base, ep, user, pwd, login_form=self.cfg.get("login_form"))
            log("  " + msg)
            if not cookie:
                raise RuntimeError(msg)
            return cookie

        self.run("账号密码登录", work, self._after_cookie)

    def _after_cookie(self, cookie):
        # 最后一道防线：不管 cookie 从哪来的，解析不出「名字=值」就不写进配置
        problem = login_mod.cookie_problem(cookie)
        if problem:
            self.log(f"[x] {problem} —— 没写入配置")
            messagebox.showwarning(
                "cookie 不可用", problem + "\n\n"
                "请从 F12 → Network → Request Headers 复制 Cookie: 那一整行。")
            return
        self.v["cookie"].set(cookie)
        self.save_config(silent=True)
        self.log("[OK] cookie 已存进 config.json（文件在 .gitignore 里，不会外泄）")
        self.job_verify()

    def job_verify(self):
        def work(log, prog, snap):
            base = (snap["base_url"] or "").strip()
            cookie = (snap["cookie"] or "").strip()
            uid = _num(snap["uid"], "uid") or None
            if not cookie:
                raise RuntimeError("还没有 cookie，先点「获取cookie」或直接粘贴")
            ep = nexus.Endpoints(base, self.cfg.get("endpoints"))
            ok, got_uid, uname, msg = login_mod.verify(cookie, base, ep, uid)
            log(f"  验证：{msg}")
            if not ok:
                raise RuntimeError(msg)
            return got_uid, uname

        def done(res):
            uid, uname = res
            if uid:
                self.v["uid"].set(str(uid))
            self.log(f"[OK] 登录态有效：uid={uid}  用户名={uname}")
            self._set_whoami(uname)
            self.save_config(silent=True)
            # 弹窗给个明确的「成了」，别让人盯着日志找反馈
            self._popup("登录态有效",
                        f"uid = {uid}\n用户名 = {uname or '未取到'}\n\n"
                        "已写入 config.json。")

        self.run("验证登录态", work, done,
                 on_error=lambda e: self._set_whoami(None))

    def job_probe_gift(self):
        def work(log, prog, snap):
            base = (snap["base_url"] or "").strip()
            cookie = (snap["cookie"] or "").strip()
            ep = nexus.Endpoints(base, self.cfg.get("endpoints"))
            if not ep.has("gift_bonus"):
                raise RuntimeError("接口里没配 gift_bonus")
            sess = nexus.Session(base, cookie,
                                 timeout=int(_num(snap["timeout"], "超时") or 25))
            url = ep.url("gift_bonus")
            log(f"抓取 {url} ……")
            st, html, final = sess.request(ep.path("gift_bonus"))
            log(f"  HTTP {st}，{len(html)} 字符")
            if st != 200 or nexus.looks_like_login(html):
                raise RuntimeError(f"拿不到赠送页（HTTP {st}）")
            spec, notes = payout_mod.suggest_gift_form(html, final or url, ep=ep)
            if spec is None:
                raise RuntimeError("；".join(notes))
            return spec, notes

        def done(res):
            spec, notes = res
            for n in notes:
                self.log("  " + n)
            self.v["gift_action"].set(spec["action"] or "")
            self.v["gift_user"].set(spec["fields"]["username"] or "")
            self.v["gift_amount"].set(spec["fields"]["amount"] or "")
            self.v["gift_msg"].set(spec["fields"]["message"] or "")
            # 成功判据只在空着的时候补默认值，绝不覆盖你手工核过的
            filled = ""
            if not self.v["gift_success_url"].get().strip():
                self.v["gift_success_url"].set(
                    spec.get("success_url") or payout_mod.DEFAULT_SUCCESS_URL)
                filled = self.v["gift_success_url"].get()
            if not self.v["gift_dup_url"].get().strip():
                self.v["gift_dup_url"].set(payout_mod.DUPLICATE_URL)
            self.save_config(silent=True)
            self.log("[OK] 赠送表单已填进「② 考核与方案」页并保存"
                     + (f"（成功URL后缀补上默认值 {filled!r}）" if filled else ""))
            messagebox.showinfo(
                "探测完成",
                "已认出的字段填进了「② 考核与方案」页。\n\n"
                f"成功URL后缀现在是：{self.v['gift_success_url'].get() or '(空)'}\n"
                "判定只看落点 URL（成功响应里没有任何文字）。\n"
                "换站/换模板请自己核一遍：手工送 1 点魔力值，看送出后页面跳到哪里；"
                "清空 = 拒绝真实发放。")

        self.run("探测赠送表单", work, done)

    # ---- 算工资

    def _load_members(self, log, snap):
        p = Path((snap["roster"] or "").strip())
        if not p.exists():
            raise RuntimeError(f"找不到考核表 {p}")
        members = roster_mod.load_roster(p)
        log(f"  读到 {len(members)} 人")
        return members

    def job_payroll_selected(self):
        """
        重抓勾选的行 —— **不看原来有没有数据，一律重新联网抓一次**，再算这些人。

        和「联网计算」只差范围：这个只动勾选的行，其余行一个字段都不碰。
        抓不到就按老规矩清空实测列、记「未测」（不留旧数据，别让过期数字
        冒充刚抓到的）；没有 cookie 时抓不了，这时**不动**实测列，只重算。
        改完门槛 / 税率后用，或者想刷新某几个人的数据时用。
        """
        picked = self._pay_checked_rows() or self.pay_tree.selection()
        if not picked:
            messagebox.showinfo(
                "没选中", "先在「选」列勾选要重算的行（和④页一样点格子打勾）。")
            return
        if not self.rows or not self.members:
            messagebox.showinfo("没有数据", "先「联网计算」一次。")
            return
        # 界面行 → 工资行 → 成员（row_no 对上号）
        idxs = sorted(self.pay_tree.index(iid) for iid in picked)
        rownos = {self.rows[i]["row_no"] for i in idxs}
        members = [m for m in self.members if m.row_no in rownos]
        if not members:
            messagebox.showinfo("没有数据", "选中的行找不到对应成员，重新算一次全表吧。")
            return

        def work(log, prog, snap):
            base = (snap["base_url"] or "").strip()
            cookie = (snap["cookie"] or "").strip()
            if not cookie or not base:
                # 抓不了就不能动实测列 —— 那会把好数据清成「未测」
                log(f"[!] 没 cookie，{len(members)} 人没法现抓 —— 拿到 cookie 再点一次；"
                    "这次只拿已有数据重算")
            else:
                timeout = int(_num(snap["timeout"], "超时") or 25)
                ep = nexus.Endpoints(base, self.cfg.get("endpoints"))
                sess = nexus.Session(base, cookie, timeout=timeout)
                workers = self._measure_workers()
                log(f"  重抓这 {len(members)} 人（每人约 1 秒"
                    + (f"，{workers} 路并发" if workers > 1 else "") + "）……")
                ok, fail = settle_mod.refresh_measurements(
                    members, self.settings["metrics"], sess, ep, delay=1.0,
                    log=log,
                    progress=lambda i, n, m, text="":
                    (prog(i, n, text), self._prog1(text or f"{i}/{n}")),
                    snapshot_dir=ROOT / "samples", workers=workers,
                    session_factory=lambda: nexus.Session(base, cookie,
                                                          timeout=timeout))
                log(f"  重抓完成：成功 {ok} · 失败 {fail}")
                if fail:
                    log(f"[!] {fail} 人没抓到 —— 实测列已清空、记「未测」，别当成达标")
            log(f"  重算 {len(members)} 人……")
            rows2, errs = settle_mod.build_payroll(members, self.settings)
            for e in errs:
                log("[x] " + e)
            return rows2, idxs

        def done(res):
            rows2, idxs = res
            new_by_row = {r["row_no"]: r for r in rows2}
            for i in idxs:
                old = self.rows[i]
                if old["row_no"] in new_by_row:
                    self.rows[i] = new_by_row[old["row_no"]]
            self._fill_pay_tree()
            self._set_pay_summary()
            self._write_roster_back()
            self.pay_progress.set(f"选中重算完成：{len(rows2)} 人（{len(idxs)} 行已更新）")
            self.log(f"[OK] 选中重算完成：{len(rows2)} 人")
            self._autocheck_bad()

        self.run("选中重算", work, done)

    def _measure_workers(self):
        """联网刷新并发几路：配置 measure_workers，默认 4，1 = 退回串行。"""
        try:
            n = int(float(self.cfg.get("measure_workers", 4)))
        except (TypeError, ValueError):
            n = 4
        return max(1, min(n, 16))

    def job_payroll_online(self):
        def work(log, prog, snap):
            base = (snap["base_url"] or "").strip()
            cookie = (snap["cookie"] or "").strip()
            if not cookie:
                raise RuntimeError("还没 cookie，先在上面拿一个")
            timeout = int(_num(snap["timeout"], "超时") or 25)
            ep = nexus.Endpoints(base, self.cfg.get("endpoints"))
            sess = nexus.Session(base, cookie, timeout=timeout)
            members = self._load_members(log, snap)
            workers = self._measure_workers()
            # 先摆一版占位表，之后每抓完一人刷一行 —— 界面始终在动
            self.q.put(("payreset", members))

            def prog_cb(i, n, m, text=""):
                prog(i, n, text)
                self._prog1(text or f"{i}/{n}")

            log(f"  逐人抓做种汇总（{len(members)} 人，每次请求间隔 1 秒"
                + (f"，{workers} 路并发" if workers > 1 else "") + "）……")
            ok, fail = settle_mod.refresh_measurements(
                members, self.settings["metrics"], sess, ep, delay=1.0, log=log,
                progress=prog_cb, snapshot_dir=ROOT / "samples", workers=workers,
                session_factory=lambda: nexus.Session(base, cookie, timeout=timeout),
                on_member=lambda m: self.q.put(("payrow", m)))
            log(f"  成功 {ok} 人，失败 {fail} 人")
            if fail:
                log("[!] 失败的人会显示「未测」，不会被当成达标 —— 别放过")
            return members, f"联网计算完成：成功 {ok} 人 · 失败 {fail} 人 · 共 {len(members)} 人"

        self.run("联网计算", work, self._after_payroll)

    def _after_payroll(self, res):
        members, note = res
        self.members = members
        self.rows, errors = settle_mod.build_payroll(members, self.settings)
        for e in errors:
            self.log("[x] " + e)
        self.columns = settle_mod.build_columns(self.settings["metrics"])
        self._fill_pay_tree()
        self._set_pay_summary()
        if note and self.pay_progress is not None:
            self.pay_progress.set(note)       # 保留一行总结，不自动清空
        self._write_roster_back()
        gift = sum(r["gift"] or 0 for r in self.rows)
        self.log(f"[OK] 工资表算好了：{len(self.rows)} 人，应赠合计 {gift:,}")
        if any(r["status"] == "未测" for r in self.rows):
            self.log("[!] 有人没采集到数据（未测 ≠ 达标，不会自动发钱）")
        self._autocheck_bad()

    def _autocheck_bad(self):
        """跑完自动勾上「未测 / 不达标」的行 —— 接着点「选中重算」就把这些人重抓一遍。"""
        n = self._check_rows_by_status(("未测", "不达标"))
        if n:
            self.log(f"[i] 已自动勾选 {n} 个未测 / 不达标的行 ——"
                     "点「选中重算」会把这些行重新联网抓一遍")

    def _set_pay_summary(self):
        ok = sum(1 for r in self.rows if r["status"] == "达标")
        bad = sum(1 for r in self.rows if r["status"] == "不达标")
        unt = sum(1 for r in self.rows if r["status"] == "未测")
        gift = sum(r["gift"] or 0 for r in self.rows)
        recv = sum(r["salary"] or 0 for r in self.rows)
        self.pay_summary.set(
            f"{len(self.rows)} 人 · 达标 {ok} / 不达标 {bad} / 未测 {unt}　|　"
            f"组员实收合计 {recv:,}　应赠合计 {gift:,}　税收损耗 {gift - recv:,}")

    _PAY_EDIT_COLS = ("plan_id", "username", "check_raw")
    _PAY_EDIT_NAMES = {"plan_id": "方案", "username": "用户名",
                       "check_raw": "检查日期"}

    def _write_roster_back(self, force=False):
        """实测结果直接写回考核表 —— roster.csv 就是缓存（用户名 / 检查日期
        都是站点现抓的），下次打开自动载入，不再另存 payroll_cache.csv。
        没有任何实测进账时跳过（force=True 强制写，双击编辑后用）。
        """
        p = Path((self.v["roster"].get() or "").strip()) \
            if "roster" in self.v else None
        if not p or not p.exists():
            return
        if p.suffix.lower() != ".csv":
            self.log(f"[!] {p.name} 不是 csv，不回写（xlsx 只读；要当缓存请转存 csv）")
            return
        if not force and not any(m.check_date for m in self.members):
            self.log("[i] 这次没有新的实测进账，考核表不用更新")
            return
        try:
            n = roster_mod.write_back(p, self.members)
        except Exception as e:                              # noqa: BLE001
            self.log(f"[x] 写回 {p.name} 失败：{e}")
            return
        if n:
            self.log(f"[OK] 实测结果已写回 {p.name}（{n} 人）—— 下次打开直接是这份")

    def _edit_pay_cell(self, event):
        """③ 双击就地编辑（方案 / 用户名 / 检查日期）—— 改完重算该行并写回考核表。
        UID 是身份不给改；实测 / 工资那些列是现算的，改「检查日期」就行。
        """
        if self.busy or self.settings is None:
            return
        iid = self.pay_tree.identify_row(event.y)
        col = self.pay_tree.identify_column(event.x)        # "#1" 起
        if not iid or col in ("", "#1", "#2"):
            return
        keys = ["no", "pick"] + [k for k, _ in self.columns]
        key = keys[int(col[1:]) - 1]
        if key not in self._PAY_EDIT_COLS:
            return
        i = self.pay_tree.index(iid)
        if i >= len(self.rows):
            return
        m = next((x for x in self.members
                  if x.row_no == self.rows[i]["row_no"]), None)
        if m is None:
            return
        bbox = self.pay_tree.bbox(iid, col)
        if not bbox:
            return
        old = self.pay_tree.set(iid, key)

        ent = tk.Entry(self.pay_tree, justify="center")
        ent.place(x=bbox[0], y=bbox[1], width=bbox[2], height=bbox[3])
        ent.insert(0, old)
        ent.focus_set()
        ent.select_range(0, "end")

        def commit(_e=None):
            val = ent.get().strip()
            ent.destroy()
            if val == old:
                return
            if key == "plan_id":
                m.plan_id = val.upper().replace(" ", "")
            elif key == "username":
                m.username = val
            else:                       # 检查日期：手填实测（裸数字也认）
                m.check_raw = val
                chk = roster_mod.parse_check_cell(val)
                m.check_date = chk["date"]
                m.measured_bytes = chk["size_bytes"]
                m.measured_tb = chk["size_tb"]
                m.measured_count = chk["count"]
                m.measure_error = ""
            self._payupdate_row(m)      # 重算 + 原地刷新这一行（勾选不动）
            self._write_roster_back(force=True)
            self.log(f"[OK] uid {m.uid} {self._PAY_EDIT_NAMES[key]}"
                     f" → {val or '(空)'}（已写回考核表）")

        ent.bind("<Return>", commit)
        ent.bind("<FocusOut>", commit)
        ent.bind("<Escape>", lambda _e: ent.destroy())

    def _fill_pay_tree(self):
        t = self.pay_tree
        keys = ["no", "pick"] + [k for k, _ in self.columns]
        t.configure(columns=keys)
        t.heading("no", text="序")
        t.column("no", width=44, anchor="center", stretch=False)
        t.heading("pick", text="选")
        t.column("pick", width=40, anchor="center", stretch=False)
        for k, h in self.columns:
            t.heading(k, text=h)
            # 谁都不跟着窗口拉伸，宽度都先按列宽表给一版；
            # 全列宽度在插完行之后按内容现量一遍（见 _fit_pay_cols）。
            # 整表比窗口宽就拖下面的横向滚动条（一直挂着，不是没有）。
            t.column(k, width=PAY_COL_WIDTH.get(k, 100), minwidth=40,
                     anchor="w" if k in PAY_COL_LEFT else "center",
                     stretch=False)
        # 整行上色：达标绿 / 不达标红 / 未测橙（一眼看出谁该发、谁数据没抓到）
        for tag, color in ROW_COLORS.items():
            t.tag_configure(tag, foreground=color)
        t.delete(*t.get_children())
        self._iid_by_rowno = {}
        for i, r in enumerate(self.rows, 1):
            iid = t.insert("", "end", tags=(status_tag(r["status"]),),
                           values=[i, "☐"] + [_cell(r.get(k)) for k, _ in self.columns])
            self._iid_by_rowno[r["row_no"]] = iid
        self._fit_pay_cols()

    def _fit_pay_cols(self):
        """③ 全列按内容自适应：表头 + 每行都量一遍 —— 内容短的列不白占屏，
        长的给到上限（「结果」列上限更高），再长拖横向滚动条。"""
        if not self.columns:
            return
        t = self.pay_tree
        keys = ["no", "pick"] + [k for k, _ in self.columns]
        texts = {k: [t.heading(k)["text"]] for k in keys}
        for iid in t.get_children():
            for k, v in zip(keys, t.item(iid, "values")):
                texts[k].append(v)
        for k in keys:
            lo, hi = ((PAY_NOTE_MIN, PAY_NOTE_MAX) if k == "note"
                      else (40, PAY_COL_MAX))
            t.column(k, width=fit_column_width(texts[k], lo, hi))

    def _pay_checked_rows(self):
        out = []
        for iid in self.pay_tree.get_children():
            vals = self.pay_tree.item(iid, "values")
            if len(vals) > 1 and vals[1] == "☑":
                out.append(iid)
        return out

    def _check_rows_by_status(self, statuses):
        """跑完自动勾上这些状态的行 —— 未测 / 不达标的重抓一遍就点「选中重算」。"""
        n = 0
        for r in self.rows:
            if r["status"] not in statuses:
                continue
            iid = self._iid_by_rowno.get(r["row_no"])
            if iid is None:
                continue
            vals = list(self.pay_tree.item(iid, "values"))
            if len(vals) > 1 and vals[1] != "☑":
                vals[1] = "☑"
                self.pay_tree.item(iid, values=vals)
                n += 1
        return n

    def _payupdate_row(self, m):
        """联网计算边抓边刷：一个人的实测回来就把那一行更新掉。"""
        if not self.rows:
            return
        for i, r in enumerate(self.rows):
            if r["row_no"] == m.row_no:
                row, _ = settle_mod.build_payroll([m], self.settings)
                if row:
                    self.rows[i] = row[0]
                    iid = self._iid_by_rowno.get(m.row_no)
                    if iid is not None:
                        old = self.pay_tree.item(iid, "values")
                        pick = old[1] if len(old) > 1 else "☐"
                        self.pay_tree.item(
                            iid, tags=(status_tag(row[0]["status"]),),
                            values=[i + 1, pick]
                            + [_cell(row[0].get(k)) for k, _ in self.columns])
                break
        self._fit_pay_cols()
        self._set_pay_summary()

    def _payreset_rows(self, members):
        """联网计算开始前先摆一版占位表（未测），之后逐行刷新。"""
        self.members = members
        self.columns = settle_mod.build_columns(self.settings["metrics"])
        self.rows, _ = settle_mod.build_payroll(members, self.settings)
        self._fill_pay_tree()
        self._set_pay_summary()

    # ---- 发放

    def job_plan_payout(self):
        def work(log, prog, snap):
            period = (snap["period"] or "").strip() or self.period
            if not self.rows:
                raise RuntimeError("先在第③页算一次工资")
            led = self._ledger_path()
            # 界面一律把不达标 / 未测 / 本周期已发过的人列出来（发满的默认不勾）——
            # 看得见才好判断，发不发由人勾。CLI 那边由配置决定要不要出现。
            # 限额是**每人 N 次**（界面上那个「每 [单位] [N] 次」），不做逐人去重：
            # 点第二次「发放」时同一批人还是勾着的（这就是「每周 2 次」）。
            items, skipped, quota = payout_mod.plan_payout(
                self.rows, self.settings, led, period,
                include_unqualified=True, include_untested=True)
            log(f"  本周期每人可领 {quota['limit']} 次：已发 {quota['sent']} 笔 / "
                f"{quota['total']:,}，{quota['done']} 人发满、"
                f"{quota['partial']} 人发了一部分、{quota['fresh']} 人还没发")
            for name, why in skipped:
                log(f"  跳过 {name:<16} {why}")
            return period, items, quota

        def done(res):
            period, items, quota = res
            self.period = period
            self.items = items
            self._render_payout_tree()
            if not items:
                self.pay_summary2.set("没有要发的人（方案里没配月薪？）")
                self.btn_run.configure(state="disabled")
                return
            for p in payout_mod.validate_gift_form(self.cfg.get("gift_form")):
                self.log("[x] " + p)              # 为什么还点不了「发放」，日志里说清
            # 能不能发、按钮亮不亮，由 _refresh_payout_summary 统一算（勾选一变就重算）
            self._refresh_payout_summary()
            self.log(f"[OK] 清单 {len(items)} 人，"
                     f"应赠合计 {sum(x['amount'] for x in items):,}"
                     f"；本周期每人可领 {quota['limit']} 次，"
                     f"{quota['done']} 人已发满"
                     + (f"、{quota['partial']} 人已发一部分"
                        if quota["partial"] else "")
                     + f"、{quota['fresh']} 人未发")
            self.log(f"     台账：{self._ledger_path()}")

        self.run("生成清单（只看不发）", work, done)

    def job_execute(self):
        picked = self._picked_items()
        if not picked:
            messagebox.showinfo("没勾人", "上面一个人都没勾选。")
            return
        total = sum(i["amount"] for i in picked)
        if not self._confirm_dialog(len(picked), total, self.period):
            self.log("[i] 已取消真实发放，一个字都没发。")
            return

        period = self.period

        def work(log, prog, snap):
            base = (snap["base_url"] or "").strip()
            cookie = (snap["cookie"] or "").strip()
            ep = nexus.Endpoints(base, self.cfg.get("endpoints"))
            sess = nexus.Session(base, cookie,
                                 timeout=int(_num(snap["timeout"], "超时") or 25))
            # 不做任何预检 —— 直接一笔一笔试着发。成没成看每笔的落点 URL，
            # 判不出来就按失败记账，比「先探测一次再决定发不发」直接得多。
            log("直接开跑，不做预检（每笔的落点 URL 会说明成没成）……")
            led = self._ledger_path()
            # 逐人刷新 ④ 表格的「状态 / 结果」两列 + 过程行（照 ③ 边抓边刷的做法）
            uid2idx = {it.get("uid"): i for i, it in enumerate(self.items)}

            def prog_cb(phase, i, n, item):
                idx = uid2idx.get(item.get("uid"))
                if idx is not None:
                    it = self.items[idx]
                    if phase == "sent":
                        # 台账里已经多了一笔 —— 内存这一份也加上，行状态当场变
                        # （「已发 1 次」→「已发 2 次」→「发完」）
                        it["paid_times"] = int(it.get("paid_times") or 0) + 1
                        it["paid"] = True
                    # 「状态」列传 None = 按更新后的已发次数现算（见 _payout_row_state）；
                    # 「结果」列 = 这一轮这一笔的结果。两列分开：
                    # 失败的人状态还是「待发」（可以再点一次续发），不会假装成功。
                    res = {"sending": "发送中…", "sent": "成功",
                           "failed": "失败", "skipped": "跳过"}.get(phase, "…")
                    # tag 传 None：行色由 _payout_row_state 按更新后的这一行现算
                    self.q.put(("payrow2", (idx, None, res, None)))
                self._prog2(f"[{i}/{n}] {item.get('username', '')} "
                            f"{'正在发送…' if phase == 'sending' else phase}")
                prog(i, n, "")          # 只刷底部状态栏 + 进度条

            stats = payout_mod.execute(
                picked, self.settings, ep, sess, led, period,
                spec=self.cfg.get("gift_form"), dry_run=False,
                delay=None, log=log, fetch_form=True,
                snapshot_dir=ROOT / "samples", progress=prog_cb)
            return stats, led

        def done(res):
            stats, led = res
            # ★ 发完**只在原地改表**：把每行的「状态 / 结果」填好，勾选一个都不动。
            self.pay_progress2.set(
                f"发放完成：成功 {stats['sent']} · 失败 {stats['failed']}"
                f" · 跳过 {stats['skipped_full']}")
            self.log("")
            self.log(f"完成：成功 {stats['sent']}，失败 {stats['failed']}，"
                     f"跳过 {stats['skipped_full']}")
            self.log(f"实发合计：{stats['amount']:,}")
            # 台账重对一遍：每人的「本周期已发次数 / 状态」都刷新（发满的就变「发完」），
            # 然后原地重绘 —— 勾选按 uid 认回来，「结果」列跟着 item 一起留着。
            self._sync_items_from_ledger()
            self._render_payout_tree(keep_checks=True)
            self._refresh_payout_summary()
            limit = self._period_limit()
            left = [i for i in picked
                    if ledger_mod.times_of(led, period, i["uid"]) < limit]
            if left:
                shown = ", ".join(f"{x['username']}(uid {x['uid']})"
                                  for x in left[:8])
                self.log(f"[!] 有 {len(left)} 人本周期还没发满 {limit} 次："
                         f"{shown}" + ("…" if len(left) > 8 else ""))
                self.log("    · 站点上确实收到了 → python payout.py --mark-paid <uid> 补记，"
                         "千万别直接重发（会真的再送一次）")
                self.log("    · 确定没送出去 → 直接再点一次「发放」续发")
                self._tally_popup(
                    "发放结果", stats,
                    note=f"有 {len(left)} 人本周期（{period}）还没发满 {limit} 次。\n\n"
                         "先核对：这笔在站点上到底收到没有？（看清单「结果」列）\n"
                         "· 收到了 → 命令行 python payout.py --mark-paid <uid> 补记，\n"
                         "  别直接重发（会真的再送一次）\n"
                         "· 没收到 → 直接再点一次「发放」续发\n\n"
                         "失败原因见清单「结果」列和日志，响应快照在 samples/ 里。",
                    warn=True)
                return
            self._tally_popup(
                "发放结果", stats,
                note=f"本周期（{period}）勾的这 {len(picked)} 人，每人最多 {limit} 次，"
                     "都已经发满了。\n"
                     "要让他们再领，先在清单里选中（可 Ctrl 多选）右键「重置周期」，"
                     "或用下面的「重置周期」按钮。\n"
                     "明细看清单里的「状态」「结果」两列。")

        self.run("真实发放", work, done)

    def _tally_popup(self, title, stats, note="", warn=False):
        """
        发放结果弹窗：成功 / 失败 / 跳过 三个数字各用一个颜色摆出来。

        三个数分开染色，是因为它们**性质完全不同** —— 成功是真发出去的钱，
        失败和跳过都没发（失败 = 判不出或站点拒绝，跳过 = 台账里已经有了）。
        扫一眼颜色就知道这次到底发成了几笔，不用去数日志。
        """
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.grab_set()
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=18)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=("⚠ " if warn else "") + title,
                  foreground=("#a26500" if warn else "#1f4e79"),
                  font=("", 11, "bold")).pack(anchor="w")

        row = ttk.Frame(frm)
        row.pack(anchor="w", pady=(12, 2))
        for lab, key, color in (("成功", "sent", ROW_COLORS["pass"]),
                                ("失败", "failed", ROW_COLORS["fail"]),
                                ("跳过", "skipped_full", "#6b6b6b")):
            cell = ttk.Frame(row)
            cell.pack(side="left", padx=(0, 26))
            ttk.Label(cell, text=lab, foreground="#444").pack(anchor="w")
            ttk.Label(cell, text=f"{stats.get(key, 0)} 个", foreground=color,
                      font=("", 15, "bold")).pack(anchor="w")
        ttk.Label(frm, text=f"实发合计 {stats.get('amount', 0):,} 魔力值",
                  foreground="#1f4e79").pack(anchor="w", pady=(4, 0))
        if stats.get("sent") or stats.get("failed") or stats.get("skipped_full"):
            ttk.Label(frm, text=TALLY_HINT, foreground="#666",
                      justify="left", wraplength=520).pack(anchor="w",
                                                           pady=(10, 0))
        if note:
            ttk.Label(frm, text=note, justify="left",
                      wraplength=520).pack(anchor="w", pady=(12, 0))
        ttk.Button(frm, text="确定", command=win.destroy).pack(pady=(14, 0))
        win.bind("<Return>", lambda e: win.destroy())
        win.bind("<Escape>", lambda e: win.destroy())
        self._center_on_root(win)
        self.root.wait_window(win)

    def _popup(self, title, msg, warn=False):
        """
        居中的提示弹窗（modal）。

        messagebox 的位置是系统说了算，总歪在屏幕角上；
        自己画的 Toplevel 才能居中到主窗口。
        """
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=18)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=("⚠ " if warn else "") + title,
                  foreground=("#a26500" if warn else "#1f4e79"),
                  font=("", 10, "bold")).pack(anchor="w")
        ttk.Label(frm, text=msg, justify="left").pack(anchor="w", pady=(8, 12))
        ttk.Button(frm, text="确定", command=win.destroy).pack()
        win.grab_set()
        self._center_on_root(win)
        self.root.wait_window(win)

    def _center_on_root(self, win):
        """把 Toplevel 摆到主窗口正中。"""
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = self.root.winfo_x() + max(0, (self.root.winfo_width() - w) // 2)
        y = self.root.winfo_y() + max(0, (self.root.winfo_height() - h) // 2)
        win.geometry(f"+{x}+{y}")

    def _ask_ok(self, title, msg, ok_text="确定"):
        """居中的确认框（确定 / 取消），返回 bool。同样自绘才居得中。"""
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="⚠ " + title, foreground="#a26500",
                  font=("", 10, "bold")).pack(anchor="w")
        ttk.Label(frm, text=msg, justify="left").pack(anchor="w", pady=(8, 14))
        row = ttk.Frame(frm)
        row.pack(fill="x")
        result = {"ok": False}

        def go():
            result["ok"] = True
            dlg.destroy()

        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="right", padx=6)
        ttk.Button(row, text=ok_text, command=go).pack(side="right")
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        self._center_on_root(dlg)
        self.root.wait_window(dlg)
        return result["ok"]

    def _confirm_dialog(self, n, total, period):
        """
        真实发放前的确认：把周期、人数、应赠合计摆出来
        —— 合计**标红**，就是要你亲眼看一遍这个数字；点「确认发放」即可，
        不用输入任何东西。
        """
        dlg = tk.Toplevel(self.root)
        dlg.title("确认真实发放")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="这一下是真的发钱，发出去了收不回来。",
                  foreground="#b3261e", font=("", 11, "bold")).pack(anchor="w")

        info = ttk.Frame(frm)
        info.pack(anchor="w", pady=(10, 2))
        for lab, val, hot in (("周期", period, False),
                              ("人数", f"{n} 人", False),
                              ("应赠合计", f"{total:,} 魔力值", True)):
            r = ttk.Frame(info)
            r.pack(anchor="w", pady=1)
            ttk.Label(r, text=f"{lab}：", width=9, anchor="e").pack(side="left")
            ttk.Label(r, text=val, foreground="#b3261e" if hot else "",
                      font=("", 10, "bold") if hot else ("", 10)).pack(side="left")
        ttk.Label(frm, text=f"每人本周期（{period}）最多发 {self._period_limit()} 次；"
                            "已发满的会自动跳过（要重发先「重置周期」）。\n"
                            "中途失败不算成功，可以直接再点一次续发。",
                  foreground="#666", justify="left").pack(anchor="w", pady=(10, 12))

        row = ttk.Frame(frm)
        row.pack(fill="x")
        result = {"ok": False}

        def go():
            result["ok"] = True
            dlg.destroy()

        ttk.Button(row, text="取消", command=dlg.destroy).pack(side="right", padx=6)
        ttk.Button(row, text="确认发放", command=go).pack(side="right")
        dlg.bind("<Return>", lambda e: go())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        self._center_on_root(dlg)
        self.root.wait_window(dlg)
        return result["ok"]

    def _ledger_path(self):
        p = Path(self.cfg.get("payout", {}).get("ledger")
                 or ledger_mod.DEFAULT_LEDGER)
        return p if p.is_absolute() else ROOT / p

    def _period_limit(self):
        """本周期允许发几次 —— 认**界面上**那个「每 [月] [N] 次」（填错按 1 显示）。

        以前这里读的是 config.json 里的旧值：界面上刚把 1 改成 2，台账弹窗还写
        「0/1 次」，看着就是「改了不生效」（用户真实反馈）。次数只有一个来源 = 界面。
        """
        try:
            return self._pay_freq()
        except ValueError:
            return 1

    def job_ledger(self):
        limit = self._period_limit()    # 主线程先把界面上的次数读出来（线程里不能读控件）

        def work(log, prog, snap):
            led = self._ledger_path()
            log(f"  台账文件：{led}")
            if not led.exists():
                log("  还没有台账（第一次发放时会自动建）")
                return led, []
            rows = periods_of(led)
            for r in rows:
                log(f"  {r['period']}  {r['users']} 人 / {r['sent']} 笔 / "
                    f"{r['total']:,}  单人最多 {r['max_times']}/{limit} 次  "
                    f"失败 {r['failed']} 人")
            return led, rows

        def done(res):
            led, rows = res
            if not rows:
                self.pay_summary2.set(
                    f"台账 {led.name} 是空的（第一次真实发放时自动建）")
                self._popup("台账状态",
                            f"台账文件：{led}\n\n还没有任何记录 —— "
                            "第一次真实发放时会自动建。")
                return
            lines = []
            for r in rows:
                lines.append(
                    f"{r['period']}　{r['users']} 人　{r['sent']} 笔　"
                    f"{r['total']:,}　单人最多 {r['max_times']}/{limit} 次　"
                    f"失败 {r['failed']} 人")
            self.pay_summary2.set("　|　".join(lines[:2]))
            self._popup("台账状态", f"台账文件：{led}\n\n" + "\n".join(lines))

        self.run("查台账", work, done, need_config=False)


# ============================================================

def main():
    if not HAVE_TK:
        print("[x] 这个 Python 没带 tkinter，界面起不来。")
        print("    不用装任何东西 —— 只是你用的这个 Python 构建没编进去。")
        print("    · 官方 python.org 装的 Python 默认都带 tkinter，换个解释器即可")
        print("    · 或者直接用命令行版，功能一模一样：")
        print("        python probe.py        探测站点")
        print("        python settle.py       算工资")
        print("        python payout.py       看清单 / 发放")
        return 1

    # 配置永远落 config.json（缺了由 _bootstrap_files 从模板补），
    # 绝不能拿 resolve_config_path 的回退值 —— 那会指到模板文件上，
    # 「保存配置」就把真实站点和 cookie 写进 config.example.json 了。
    cfg_path = ROOT / nexus.CONFIG_NAME
    root = tk.Tk()
    App(root, cfg_path)
    root.mainloop()
    return 0


def selftest():
    print("=" * 66)
    print("GUI 模块自测：不碰界面的那部分逻辑")
    print("=" * 66)
    fails = []

    def check(label, got, want):
        ok = got == want
        print(f"  [{'OK' if ok else 'XX'}] {label}: {got}" + ("" if ok else f"  (期望 {want})"))
        if not ok:
            fails.append(f"{label}: 期望 {want}，实际 {got}")

    def check_true(label, got):
        check(label, bool(got), True)

    import tempfile
    print("\n-- 发放状态：待发 / 已发 n 次 / 发完 --")
    check("一次没发 → 待发", payout_mod.pay_state(0, 2), "待发")
    check("发了 1 次（上限 2）→ 已发 1 次", payout_mod.pay_state(1, 2), "已发 1 次")
    check("发满 2 次 → 发完", payout_mod.pay_state(2, 2), "发完")
    check("超了也还是发完", payout_mod.pay_state(5, 2), "发完")
    check("上限 1 时发 1 次就是发完", payout_mod.pay_state(1, 1), "发完")

    print("\n-- 每人 N 次：发过的人照样进清单（不逐人去重）--")
    with tempfile.TemporaryDirectory() as td0:
        led0 = Path(td0) / "l.jsonl"
        set0 = {"payout": {"rounds_per_period": 2}}
        rows0 = [
            {"uid": 101, "username": "user001", "plan_id": "3T",
             "status": "达标", "gift": 222227, "salary": 200000},
            {"uid": 102, "username": "user002", "plan_id": "3T",
             "status": "达标", "gift": 222227, "salary": 200000},
        ]
        items0, _sk0, q0 = payout_mod.plan_payout(rows0, set0, led0, "2026-09")
        check("谁都没发 → 两人都默认勾上",
              [i["default_pick"] for i in items0], [True, True])
        check("quota：都还没发",
              (q0["fresh"], q0["partial"], q0["done"]), (2, 0, 0))

        ledger_mod.record_gift(led0, "2026-09", 101, "user001", 222227, "3T")
        items0, _sk0, q0 = payout_mod.plan_payout(rows0, set0, led0, "2026-09")
        check("发过的人还在清单里（不去重）",
              [i["uid"] for i in items0], [101, 102])
        check("他的本周期已发次数是 1", items0[0]["paid_times"], 1)
        check("状态「已发 1 次」", items0[0]["state"], "已发 1 次")
        check("没发满 → 还是默认勾上（这就是「每周 2 次」）",
              items0[0]["default_pick"], True)

        ledger_mod.record_gift(led0, "2026-09", 101, "user001", 222227, "3T")
        items0, _sk0, q0 = payout_mod.plan_payout(rows0, set0, led0, "2026-09")
        check("发满 2 次 → 状态「发完」", items0[0]["state"], "发完")
        check("发满的人列出来、默认不勾", items0[0]["default_pick"], False)
        check("quota 数得对", (q0["done"], q0["fresh"]), (1, 1))

    print("\n-- 重置周期：只作废列到的人，历史一条不删 --")
    with tempfile.TemporaryDirectory() as td2:
        led2 = Path(td2) / "l.jsonl"
        ledger_mod.record_gift(led2, "2026-09", 101, "user001", 222227, "3T")
        ledger_mod.record_gift(led2, "2026-09", 101, "user001", 222227, "3T")
        ledger_mod.record_gift(led2, "2026-09", 102, "user002", 222227, "3T")
        check("重置前 101 发了 2 次",
              ledger_mod.times_of(led2, "2026-09", 101), 2)
        ledger_mod.reset_period(led2, "2026-09", [101], note="GUI 重置周期")
        check("重置后 101 归零", ledger_mod.times_of(led2, "2026-09", 101), 0)
        check("别人（102）一个字没动",
              ledger_mod.times_of(led2, "2026-09", 102), 1)
        check("历史记录一条没少（3 条 gift + 1 条 reset，reset 只是追加）",
              len(ledger_mod.read_ledger(led2)), 4)
        check("重置后 101 状态回「待发」（又能领 2 次）",
              payout_mod.pay_state(
                  ledger_mod.times_of(led2, "2026-09", 101), 2), "待发")

    print("\n-- 考核口径复选框 --")
    check("都不选 → 空", metrics_from_flags(False, False), [])
    check("只选体积", metrics_from_flags(True, False), ["volume"])
    check("只选数量", metrics_from_flags(False, True), ["count"])
    check("都选（顺序固定 volume 在前）", metrics_from_flags(True, True),
          ["volume", "count"])

    print("\n-- 方案表 → 配置数组 --")
    rows = [("3T", "3", "300", "200000"), ("6T", "6", "600", "400000")]
    plans = plans_from_rows(rows, ["volume"])
    check("只考体积时不写 min_count", plans, [
        {"id": "3T", "min_volume_tb": 3, "salary": 200000},
        {"id": "6T", "min_volume_tb": 6, "salary": 400000}])
    plans = plans_from_rows(rows, ["count"])
    check("只考数量时不写 min_volume_tb",
          plans[0], {"id": "3T", "min_count": 300, "salary": 200000})
    plans = plans_from_rows(rows, ["volume", "count"])
    check("两项都考时都写", plans[0],
          {"id": "3T", "min_volume_tb": 3, "min_count": 300, "salary": 200000})
    check("小数门槛保留", plans_from_rows([("3T", "3.5", "0", "200000")],
                                          ["volume"])[0]["min_volume_tb"], 3.5)
    try:
        plans_from_rows([("", "", "", "")], ["volume"])
        check("全空的方案表要报错", "没报错", "ValueError")
    except ValueError:
        check("全空的方案表要报错", "ValueError", "ValueError")
    try:
        plans_from_rows([], ["volume"])
        check("空方案表要报错", "没报错", "ValueError")
    except ValueError:
        check("空方案表要报错", "ValueError", "ValueError")
    try:
        plans_from_rows([("3T", "abc", "", "200000")], ["volume"])
        check("门槛不是数字要报错", "没报错", "ValueError")
    except ValueError:
        check("门槛不是数字要报错", "ValueError", "ValueError")

    print("\n-- cookie 校验（不按名字筛，整行照收）--")
    check("老式 c_secure_* 可用",
          login_mod.cookie_problem(
              "c_secure_uid=MTAwMDE=; c_secure_pass=zzz; c_secure_login=bm90aGluZw=="),
          "")
    check("空串要报出来", bool(login_mod.cookie_problem("")), True)
    check("纯空白要报出来", bool(login_mod.cookie_problem("   \n")), True)
    check("全是空值要报出来",
          bool(login_mod.cookie_problem("a=; b=")), True)
    check("整段 Headers 粘进来也能认",
          login_mod.cookie_problem(
              "GET / HTTP/1.1\r\nCookie: c_secure_uid=a; c_secure_pass=b\r\n"), "")
    # 新式站点的 cookie 名可能不是 c_secure_*，不能被筛掉
    newsite = ("c_secure_pass=eyJ1c2VyX2lkIjoiMTAwMDkiLCJleHBpcmVzIjoxNzk3MTQ4NzY0fS5kZWFkYmVlZmRlYWRi"
               "ZWVmZGVhZGJlZWZkZWFkYmVlZmRlYWRiZWVmZGVhZGJlZWZkZWFkYmVlZmRlYWRiZWVm; _qimei_uuid42=00112233445566778899aabbccddeeff; "
               "XSRF-TOKEN=eyJpdiI6IjAwMDAwMDAwMDAwMDAwMDAwMDAwMDA9PSJ9; "
               "nexusphp_session=eyJpdiI6IjExMTExMTExMTExMTExMTExMTExMTE9PSJ9")
    check("新式站点（nexusphp_session）整行照收",
          login_mod.cookie_problem(newsite), "")
    check_true("解析结果还带着 nexusphp_session",
               "nexusphp_session=" in nexus.parse_cookie_string(newsite))
    check_true("c_secure_pass 也在",
               "c_secure_pass=eyJ1c2VyX2lk" in nexus.parse_cookie_string(newsite))

    print("\n-- 抄表用的制表符文本 --")
    columns = [("uid", "UID"), ("username", "用户名"), ("measured_tb", "实测体积(TB)"),
               ("status", "考核"), ("gift", "应赠送")]
    rows2 = [{"uid": 10001, "username": "user001", "measured_tb": 3.56,
              "status": "达标", "gift": 222227},
             {"uid": 10002, "username": "user002", "measured_tb": None,
              "status": "未测", "gift": None}]
    txt = rows_to_tsv(rows2, columns)
    lines = txt.splitlines()
    check("表头", lines[0], "UID\t用户名\t实测体积(TB)\t考核\t应赠送")
    check("第一行", lines[1], "10001\tuser001\t3.560\t达标\t222227")
    check_true("空值留空而不是 None", lines[2].endswith("未测\t"))

    print("\n-- 台账周期汇总 --")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        led = Path(td) / "l.jsonl"
        check("没有台账时不炸", periods_of(led), [])
        ledger_mod.record_gift(led, "2026-08", 1, "user001", 222227, "3T")
        ledger_mod.record_gift(led, "2026-09", 2, "user002", 444449, "6T")
        ledger_mod.record_gift(led, "2026-09", 3, "user003", 444449, "6T")
        # 只有失败记录的人 = 最终没发成功
        ledger_mod.record_gift(led, "2026-09", 4, "user004", 222227, "3T",
                               status="fail")
        ledger_mod.record_gift(led, "2026-09", 4, "user004", 222227, "3T",
                               status="fail")
        rows3 = periods_of(led)
        check("周期数", len(rows3), 2)
        check("新周期在前", [r["period"] for r in rows3], ["2026-09", "2026-08"])
        check("发出去的人数按 uid 去重", rows3[0]["users"], 2)
        check("发出去的笔数", rows3[0]["sent"], 2)
        check("失败人数只算最终没成功的", rows3[0]["failed"], 1)
        check("合计只算成功的", rows3[0]["total"], 444449 * 2)
        check("单人最多发了几次", rows3[0]["max_times"], 1)

        # 同一人又发了一笔（每人 N 次里的第二笔）→ 笔数 +1、单人最多次数变 2
        ledger_mod.record_gift(led, "2026-09", 2, "user002", 444449, "6T")
        check("又发一笔 → 笔数 +1", periods_of(led)[0]["sent"], 3)
        check("单人最多次数变 2", periods_of(led)[0]["max_times"], 2)
        check("人数不受影响（还是 2 人）", periods_of(led)[0]["users"], 2)

        ledger_mod.reset_period(led, "2026-09", [2], note="重置")
        check("重置 user002 → 他不在统计里了", periods_of(led)[0]["users"], 1)
        check("重置后笔数也跟着少", periods_of(led)[0]["sent"], 1)
        check("重置后合计只剩没被重置的那个人",
              periods_of(led)[0]["total"], 444449)

    print("\n-- tkinter 可用性 --")
    check("这个解释器有没有 tkinter", HAVE_TK, True)

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
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    sys.exit(main())
