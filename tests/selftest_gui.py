#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
界面层自测（要真起一次 tkinter）——

`pt-seeder-settlement.py --selftest` 只跑**纯逻辑**（不碰 ttk），
所以列宽、行色、「状态」列跟着台账刷新、按钮亮不亮这些**界面层**的问题
它一个都测不到。这个脚本补上：真的建一次 App，选中 ④ 页，量出来。

    python tests/selftest_gui.py

跑的内容：
  · ④ 清单列宽：整表放得进窗口、状态/结果两列按内容自适应、都不 stretch
  · 发放状态走查：待发 → 已发 n 次 → 发完 →「重置周期」→ 待发
  · 台账是唯一依据：往台账记一笔，界面自己跟着变（不用重新生成清单）
  · 改「每周期 N 次」边打边生效；填错就说清原因、按钮变灰
  · 勾选只存在表格里：重绘按 uid 认回来，不会落到别人头上

★ 弹窗类方法（_popup / _ask_ok / _tally_popup）都是真 Toplevel + wait_window，
  这里必须打桩，否则脚本会卡死。
★ 量之前必须先 nb.select —— 没映射的页签宽度是 1、x 全是 0，量出来纯属噪音。

没带 tkinter 的解释器会直接跳过（返回 0），不算失败。
"""
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:                  # tests/ 里也能 import 根目录的模块
    sys.path.insert(0, str(ROOT))
fails = []


def check(label, got, want):
    ok = got == want
    print("  [%s] %s: %r" % ("OK" if ok else "XX", label, got)
          + ("" if ok else "  (期望 %r)" % (want,)))
    if not ok:
        fails.append(label)
    return ok


def check_true(label, got):
    return check(label, bool(got), True)


def load_gui():
    spec = importlib.util.spec_from_file_location(
        "gui_under_test", ROOT / "pt-seeder-settlement.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gui_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    print("=" * 66)
    print("界面层自测：真起一次 tkinter，量出来（不是猜）")
    print("=" * 66)

    gui = load_gui()
    if not gui.HAVE_TK:
        print("\n这个解释器没带 tkinter —— 跳过界面层自测（不算失败）。")
        print("换官方 python.org 装的解释器再跑：python tests/selftest_gui.py")
        return True

    import tkinter as tk

    td = tempfile.TemporaryDirectory()
    tmp = Path(td.name)
    # 用脱敏模板当配置（不含任何真实域名 / cookie），台账另指到临时目录
    shutil.copy(ROOT / "config.example.json", tmp / "config.json")
    led = tmp / "ledger.jsonl"

    root = tk.Tk()
    try:
        app = gui.App(root, tmp / "config.json")
    except Exception as e:                                  # noqa: BLE001
        print("\n[x] 界面起不来：%r" % (e,))
        root.destroy()
        td.cleanup()
        return False

    # 弹窗全部打桩（真 Toplevel + wait_window 会把脚本卡死）
    app._popup = lambda *a, **k: None
    app._ask_ok = lambda *a, **k: True
    app._tally_popup = lambda *a, **k: None
    app._ledger_path = lambda: led
    # 还有几处走的是 tkinter 原生 messagebox（同样是阻塞的模态框），一起静音
    for _fn in ("showinfo", "showerror", "showwarning"):
        setattr(gui.messagebox, _fn, lambda *a, **k: None)

    # 脱敏模板里的 gift_form 是空的（故意留白让人自己填）。这里先验一下
    # 「没配全就不让发」，再配一份最小的，后面的走查才有意义。
    app.cfg.pop("gift_form", None)
    print("\n-- 赠送表单没配全 → 拒绝真发 --")
    check("拦住并说清缺什么",
          app._payout_block([{"paid_times": 0, "status": "达标", "amount": 1}]),
          "赠送表单没配全（见日志）")
    app.cfg["gift_form"] = {
        "action": "/mybonus.php",
        "fields": {"username": "username", "amount": "seedbonus",
                   "message": "message"},
    }
    check("配全了就不再抱怨表单（此时一个人都没勾，报的就是这个）",
          app._payout_block([]), "一个人都没勾")

    print("\n-- ④ 页签与列宽 --")
    idx = None
    for i in range(len(app.nb.tabs())):
        if "发放" in app.nb.tab(i, "text"):
            idx = i
            break
    if not check_true("找得到「发放与台账」页签", idx is not None):
        root.destroy()
        td.cleanup()
        return False
    app.nb.select(idx)          # ★ 不 select 就没映射，量出来全是噪音
    root.update()

    app.v["pay_freq"].set("2")          # 每周期每人 2 次
    app.period = "2026-09"
    app.v["period"].set("2026-09")
    app.items = [
        {"uid": 10001, "username": "user001", "plan": "3T", "salary": "200,000",
         "amount": 222227, "status": "达标", "paid_times": 0, "paid": False,
         "state": "待发", "default_pick": True, "result": ""},
        {"uid": 10002, "username": "user002", "plan": "6T", "salary": "400,000",
         "amount": 444449, "status": "达标", "paid_times": 0, "paid": False,
         "state": "待发", "default_pick": True, "result": ""},
    ]

    def row(idx_):
        v = list(app.pay_tree2.item(str(idx_), "values"))
        return {"state": v[gui.PAY2_STATE_IDX], "result": v[gui.PAY2_RESULT_IDX],
                "pick": v[1], "tag": app.pay_tree2.item(str(idx_), "tags")}

    print("\n-- 谁都没发 --")
    app._render_payout_tree()
    app._refresh_payout_summary()
    root.update()
    check("两人都是「待发」", [row(0)["state"], row(1)["state"]],
          ["待发", "待发"])
    check("两个都默认勾上", [row(0)["pick"], row(1)["pick"]], ["☑", "☑"])
    check("没被拦住（能发）", app._payout_block(app._picked_items()), None)
    total_w = sum(app.pay_tree2.column(c, "width") for c, _, _ in gui.PAY2_COLUMNS)
    print("     列宽合计 %s，窗口宽 %s" % (total_w, app.pay_tree2.winfo_width()))
    check_true("整表放得进窗口（不用横向滚动）",
               total_w <= app.pay_tree2.winfo_width())
    check("状态/结果两列都不 stretch（否则白占一长条）",
          [app.pay_tree2.column("state", "stretch"),
           app.pay_tree2.column("result", "stretch")], [False, False])

    print("\n-- 台账记 1 笔（模拟发放成功）→ 界面自己跟着变 --")
    gui.ledger_mod.record_gift(led, "2026-09", 10001, "user001", 222227, "3T")
    app._sync_items_from_ledger()
    app._render_payout_tree(keep_checks=True)
    app._refresh_payout_summary()
    root.update()
    check("10001 = 已发 1 次", row(0)["state"], "已发 1 次")
    check("10002 还是待发", row(1)["state"], "待发")
    check("已发过的行是绿的", row(0)["tag"], ("done",))
    check("勾选按 uid 认回来（没被别人顶掉）",
          [row(0)["pick"], row(1)["pick"]], ["☑", "☑"])
    check("没发满 → 还能发", app._payout_block(app._picked_items()), None)

    print("\n-- 再记 1 笔（发满 2 次）--")
    gui.ledger_mod.record_gift(led, "2026-09", 10001, "user001", 222227, "3T")
    app._sync_items_from_ledger()
    app._render_payout_tree(keep_checks=True)
    app._refresh_payout_summary()
    root.update()
    check("10001 = 发完", row(0)["state"], "发完")
    check("发满的人也列出来（勾还在，发不发由人定）", row(0)["pick"], "☑")
    check("10002 没发满 → 仍可发", app._payout_block(app._picked_items()), None)

    print("\n-- 只勾发满的那个人 → 必须拦住并说清原因 --")
    app.pay_tree2.item("1", values=[2, "☐", 10002, "6T", "user002", "400,000",
                                    "444,449", "达标", "待发", ""])
    app._refresh_payout_summary()
    root.update()
    check("拦住并给出下一步", app._payout_block(app._picked_items()),
          "勾上的人本周期都发满了 2 次 —— 要重发先「重置周期」")
    check("「发放」按钮变灰", str(app.btn_run["state"]), "disabled")

    print("\n--「重置周期」→ 只作废选中的那个人，历史一条不删 --")
    gui.ledger_mod.record_gift(led, "2026-09", 10002, "user002", 444449, "6T")
    n_before = len(gui.ledger_mod.read_ledger(led))
    app._sync_items_from_ledger()
    app._reset_period(["0"])                # 只重置第 1 行
    root.update()
    check("重置后 10001 = 待发", row(0)["state"], "待发")
    check("重置后他的「结果」列也清空", row(0)["result"], "")
    check("别人（10002 = 已发 1 次）一个字没动", row(1)["state"], "已发 1 次")
    check("台账只多了一条 reset（历史没删）",
          len(gui.ledger_mod.read_ledger(led)), n_before + 1)
    check("历史里的 gift 还是 3 条",
          len([r for r in gui.ledger_mod.read_ledger(led)
               if r["type"] == "gift"]), 3)

    print("\n-- 改「每周期 N 次」→ 边打边生效（不用重新生成清单）--")
    app.v["pay_freq"].set("3")
    root.update()
    check("上限调到 3 → 10001 回「待发」", row(0)["state"], "待发")
    check("10002（已发 1 次）仍显示「已发 1 次」", row(1)["state"], "已发 1 次")
    app.v["pay_freq"].set("1")
    root.update()
    check("上限调到 1 → 10002 变「发完」", row(1)["state"], "发完")
    app.v["pay_freq"].set("abc")
    root.update()
    blk = app._payout_block(app._picked_items()) or ""
    check_true("填了非数字 → 说清填错了什么",
               "每周期发放次数" in blk and "abc" in blk)
    app.v["pay_freq"].set("0")
    root.update()
    blk = app._payout_block(app._picked_items()) or ""
    check_true("填 0 → 也拦下来，并说清要 1 或更大", "1 或更大" in blk)
    app.v["pay_freq"].set("20")
    root.update()

    print("\n-- 状态列宽度：内容变长就自己量宽（夹在区间里）--")
    w_short = app.pay_tree2.column("state", "width")
    check("列宽 = 当前最长那条内容量出来的宽度",
          w_short, gui.fit_column_width([i.get("state") for i in app.items],
                                        gui.PAY2_STATE_MIN, gui.PAY2_STATE_MAX))
    for _ in range(10):                     # 10002 攒到 11 次 →「已发 11 次」
        gui.ledger_mod.record_gift(led, "2026-09", 10002, "user002", 444449, "6T")
    app._sync_items_from_ledger()
    app._render_payout_tree(keep_checks=True)
    root.update()
    w_long = app.pay_tree2.column("state", "width")
    print("     内容 %r → 列宽 %s（原来 %s）" % (row(1)["state"], w_long, w_short))
    check_true("列跟着变宽了", w_long > w_short)
    check_true("不会超过上限（超了拖横向滚动条）", w_long <= gui.PAY2_STATE_MAX)

    print("\n-- 行色三态（照 ③ 页的规矩）--")
    app.items[1]["status"] = "未测"
    app._render_payout_tree()
    root.update()
    check("未测 = 橙（blocked）", row(1)["tag"], ("blocked",))
    check("未测的行「状态」显示「不发」", row(1)["state"], "不发")

    print("\n-- ③「选中重算」：勾了谁就重抓谁 --")
    # ③ 页自己一套假数据 + 假抓取，只验一件事：点下按钮以后，哪些人真的被联网抓了。
    # 回归点：**不能**只抓「实测列为空」的人 —— 那样不达标（有数据）的人
    # 永远只拿旧数据重算一遍，而界面承诺的是「只重抓这些人」。
    class FakeMember:
        def __init__(self, row_no, uid, name, tb):
            self.row_no = row_no
            self.uid = uid
            self.username = name
            self.plan_id = "3T"
            self.measured_bytes = None if tb is None else int(tb * 1e12)
            self.measured_tb = tb
            self.measured_count = None
            self.measure_error = ""
            self.check_date = None
            self.check_raw = ""

    def fake_row(row_no, status, tb, note):
        return {"row_no": row_no, "uid": 10000 + row_no, "plan_id": "3T",
                "username": "user%03d" % row_no, "check_raw": "260915",
                "measured_tb": tb, "measured_count": None,
                "quota_text": "3.0 TB", "status": status,
                "salary": 200000, "gift": 222227 if status == "达标" else 0,
                "gap_text": "", "note": note}

    app.members = [FakeMember(1, 10001, "user001", 5.0),      # 有数据 · 达标
                   FakeMember(2, 10002, "user002", 0.1),      # 有数据 · 不达标
                   FakeMember(3, 10003, "user003", None)]     # 没数据 · 未测
    app.rows = [fake_row(1, "达标", 5.0, "达标"),
                fake_row(2, "不达标", 0.1, "不达标：差 2.9 TB"),
                fake_row(3, "未测", None, "未测：上次没抓到")]
    app.columns = gui.settle_mod.build_columns(["volume"])
    app.v["base_url"].set("http://pt.example.com")
    app.v["cookie"].set("sess=selftest")        # 有 cookie 才走得到「现抓」那一支

    fake = {"fetched": None, "recalc": None}
    real_refresh = gui.settle_mod.refresh_measurements
    real_payroll = gui.settle_mod.build_payroll
    real_run = app.run

    def fake_refresh(members, *a, **k):
        fake["fetched"] = [m.username for m in members]
        return len(members), 0

    def fake_payroll(members, settings):
        fake["recalc"] = [m.username for m in members]
        want = {m.row_no for m in members}
        return [r for r in app.rows if r["row_no"] in want], []

    gui.settle_mod.refresh_measurements = fake_refresh
    gui.settle_mod.build_payroll = fake_payroll
    # run 改成同步：work 当场跑完、done 当场收，不用等线程
    app.run = lambda title, fn, on_done=None, **k: (
        on_done(fn(lambda *a: None, lambda *a, **k2: None, app._snapshot()))
        if on_done else
        fn(lambda *a: None, lambda *a, **k2: None, app._snapshot()))

    def tick(*rows):
        """只勾选给定的第 n 行（1 起）。重绘后 IID 会换，所以每次都重取。"""
        for iid in app.pay_tree.get_children():
            v = list(app.pay_tree.item(iid, "values"))
            v[1] = "☐"
            app.pay_tree.item(iid, values=v)
        kids = app.pay_tree.get_children()
        for n in rows:
            v = list(app.pay_tree.item(kids[n - 1], "values"))
            v[1] = "☑"
            app.pay_tree.item(kids[n - 1], values=v)

    def click():
        fake["fetched"] = fake["recalc"] = None
        app.job_payroll_selected()
        root.update()
        return fake["fetched"], fake["recalc"]

    app._fill_pay_tree()
    app.pay_tree.selection_remove(app.pay_tree.selection())
    root.update()

    tick(1)
    check("勾 1 号（有数据 · 达标）→ 照样重新联网抓",
          click(), (["user001"], ["user001"]))
    tick(2)
    check("勾 2 号（有数据 · 不达标）→ 也重抓",
          click(), (["user002"], ["user002"]))
    tick(3)
    check("勾 3 号（没数据 · 未测）→ 重抓", click(),
          (["user003"], ["user003"]))
    tick(1, 3)
    check("勾 1 + 3 号 → 只抓这两个，中间那个一行都不碰",
          click(), (["user001", "user003"], ["user001", "user003"]))
    tick(1, 2, 3)
    check("全勾 → 三个人都重抓", click(),
          (["user001", "user002", "user003"], ["user001", "user002", "user003"]))
    tick()
    app.pay_tree.selection_remove(app.pay_tree.selection())
    check("一个都不勾 → 什么都不抓", click(), (None, None))
    check("重算过的行还留在表里（没被清空）",
          len(app.pay_tree.get_children()), 3)

    app.v["cookie"].set("")
    tick(1)
    check("没 cookie → 抓不了，但也不许白清数据（只拿已有数据重算）",
          click(), (None, ["user001"]))
    app.v["cookie"].set("sess=selftest")

    gui.settle_mod.refresh_measurements = real_refresh
    gui.settle_mod.build_payroll = real_payroll
    app.run = real_run

    root.destroy()
    td.cleanup()

    print()
    print("=" * 66)
    if fails:
        print("界面层自测失败：")
        for f in fails:
            print("  x", f)
        return False
    print("界面层自测全部通过 ✅")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
