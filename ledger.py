#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发放台账 —— **每人**每周期允许领 N 次。纯标准库。

（2026-09-15 用户口径：界面「每 [周] [2] 次」= 这个周期里**每个人**都能被发 2 次。）

两种记录（append-only JSONL，一行一条，坏了也只坏一行）：

    {"v":1,"type":"gift", "period":"2026-09","uid":10001,"username":"x",
     "amount":222227,"plan":"3T","status":"ok","at":"2026-09-15T10:00:00"}
    {"v":1,"type":"reset","period":"2026-09","uids":[10001,10002],"at":"...",
     "note":"..."}

★ 判断「还能不能再发」只有一个依据：**这个 uid 在本周期已经发出去几笔**
  （status=ok 的 gift 条数，被 reset 作废的不算）。

    没发满 N 次 → 会被再发一次（再点一次「发放」就是了 —— 这就是「每周 2 次」）
    发满 N 次   → 跳过、状态显示「发完」，要重发先「重置周期」

  **不做逐人去重**：同一个人只要没发满，每次点「发放」都会真的再发一笔。
  重复点、重跑命令、开两个进程，都按这个数算 —— payout.execute 每笔发出前
  都重新数一遍，所以能超发的只剩「把 N 调大」和「自己点重置周期」。

gift   逐笔记录（status=ok 才算真发出去；同一个人一个周期最多 N 条）
reset  「重置周期」：把列出的 uid 在**本周期**的记录全部作废（次数归零），
       状态回到「待发」，于是又能领 N 次。追加，不删历史。

    python ledger.py                                     # 看当前周期状态
    python ledger.py --period 2026-09 --limit 2
    python ledger.py --reset-period --period 2026-09 --uid 10001,10002
    python ledger.py --selftest
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

__all__ = [
    "read_ledger", "append_record", "period_state", "times_of",
    "record_gift", "reset_period",
]

DEFAULT_LEDGER = "payout_ledger.jsonl"


# ============================================================
# 读
# ============================================================

def read_ledger(path):
    """
    读台账，返回记录列表（按写入顺序）。

    坏行不抛错 —— 一行写坏不该让整个工具瘫掉，跳过即可。
    """
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _as_records(path_or_records):
    return (path_or_records if isinstance(path_or_records, list)
            else read_ledger(path_or_records))


def _alive(records, period=None):
    """
    顺序扫一遍台账，返回**仍然算数**的 gift 记录。

    reset 记录把列出的 uid 先前的 gift 全部作废（次数归零）；重置之后又发的，
    以新的为准。没列到的 uid 一个字都不动。
    台账只追加不删除：历史留着可查，只是不再算数。
    """
    gifts, dead = [], set()
    for r in records:
        if period is not None and r.get("period") != period:
            continue
        t = r.get("type")
        if t == "reset":
            for u in (r.get("uids") or []):
                dead.add(str(u))
            gifts = [g for g in gifts if str(g.get("uid")) not in dead]
        elif t == "gift":
            dead.discard(str(r.get("uid")))      # 重置后又发过 → 又算数了
            gifts.append(r)
    return gifts


def _counts(gifts):
    """gift 记录 → {uid: {"times","amount","last_at","username"}}（只算 ok 的）"""
    uids = {}
    for r in gifts:
        if r.get("status") != "ok":
            continue
        k = str(r.get("uid"))
        info = uids.setdefault(k, {"times": 0, "amount": 0, "last_at": "",
                                   "username": ""})
        info["times"] += 1
        info["amount"] += int(r.get("amount") or 0)
        info["last_at"] = r.get("at") or info["last_at"]
        if r.get("username"):
            info["username"] = r["username"]
    return uids


def period_state(path, period):
    """
    本周期台账状态。

    返回 dict：
      period   周期键
      sent     本周期累计发出去**几笔**（同一个人可以多条）
      total    本周期累计已发金额
      failed   有过失败记录、但一笔都没成功的人数
      uids     {uid字符串: {"times": 本周期已发次数, "amount": 合计,
                            "last_at": 最后一次时间, "username": 用户名}}
      records  本周期所有记录（含 reset，原始顺序）

    ★ 「这个人本周期领了几次」= uids[uid]["times"] —— 界面的「已发 n 次 / 发完」、
      发放前「发满就不发」的判断，都看这个数；每人的上限在
      配置 payout.rounds_per_period（界面「每 [单位] [N] 次」）。
    """
    recs = [r for r in read_ledger(path) if r.get("period") == period]
    gifts = _alive(recs, period)
    uids = _counts(gifts)

    # 「人数」要按 uid 去重 —— 同一个人重跑几次会留下多条失败记录，
    # 不去重会把 1 个人报成 5 个。
    failed_uids = []
    for r in gifts:
        k = str(r.get("uid"))
        if r.get("status") != "ok" and k not in uids and k not in failed_uids:
            failed_uids.append(k)

    return {
        "period": period,
        "sent": sum(v["times"] for v in uids.values()),
        "total": sum(v["amount"] for v in uids.values()),
        "failed": len(failed_uids),
        "uids": uids,
        "records": recs,
    }


def times_of(path_or_records, period, uid):
    """这个 uid 在本周期**已经发出去几笔**（被 reset 作废的不算）。"""
    k = str(uid)
    return sum(1 for r in _alive(_as_records(path_or_records), period)
               if str(r.get("uid")) == k and r.get("status") == "ok")


# ============================================================
# 写
# ============================================================

def append_record(path, record):
    """追加一条。O_APPEND 语义，崩了最多丢最后一行。"""
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    rec = dict(record)
    rec.setdefault("v", 1)
    rec.setdefault("at", datetime.now().isoformat(timespec="seconds"))
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def record_gift(path, period, uid, username, amount, plan="", status="ok", note=""):
    return append_record(path, {
        "type": "gift", "period": period, "uid": uid, "username": username,
        "amount": int(amount), "plan": plan, "status": status, "note": note,
    })


def reset_period(path, period, uids, note=""):
    """
    重置周期 —— 把列出的 uid 在**本周期**的发放记录全部作废，追加一条记录。

    效果：他们的「本周期已发次数」归零（状态回「待发」），于是又能各领
    N 次（N = 界面上的「每 [单位] [N] 次」）。没列到的 uid 一个字都不动，
    历史一条不删 —— 只是这些版本不再算数。

    要「重发某个人」就只重置他，不用动别人。
    返回写入的那条记录。
    """
    us = []
    for u in (uids or []):
        s = str(u).strip()
        if s and s not in us:
            us.append(s)
    return append_record(path, {
        "type": "reset", "period": period, "uids": us, "note": note,
    })


# ============================================================
# 自测
# ============================================================

def selftest():
    import tempfile

    print("=" * 66)
    print("台账自测：每人 N 次 / 重置周期 / 逐笔记账 / 坏行容错")
    print("=" * 66)
    fails = []

    def check(label, got, want):
        ok = got == want
        print(f"  [{'OK' if ok else 'XX'}] {label}: {got}" + ("" if ok else f"  (期望 {want})"))
        if not ok:
            fails.append(f"{label}: 期望 {want}，实际 {got}")

    with tempfile.TemporaryDirectory() as td:
        led = Path(td) / "l.jsonl"
        P = "2026-09"

        check("空台账", read_ledger(led), [])
        st = period_state(led, P)
        check("空台账：没发过", (st["sent"], st["uids"], st["total"]), (0, {}, 0))
        check("空台账查次数", times_of(led, P, 101), 0)

        # ---- 逐笔记账：同一个人可以有多条（限额是每人 N 次）----
        print("\n-- 逐笔记账（同一个人本周期可以有 N 条）--")
        record_gift(led, P, 101, "user001", 222227, "3T")
        record_gift(led, P, 102, "user002", 222227, "3T")
        st = period_state(led, P)
        check("2 笔 / 2 人", (st["sent"], len(st["uids"])), (2, 2))
        check("每人 1 次", st["uids"]["101"]["times"], 1)
        check("金额合计", st["total"], 222227 * 2)
        check("换个月份查不到", period_state(led, "2026-10")["sent"], 0)
        check("times_of 认 uid", times_of(led, P, 101), 1)
        check("times_of 不认别人的周期", times_of(led, "2026-10", 101), 0)

        record_gift(led, P, 101, "user001", 222227, "3T")     # 同一个人第 2 笔
        st = period_state(led, P)
        check("同一个人第 2 笔照记（不去重）", st["uids"]["101"]["times"], 2)
        check("总笔数 3", st["sent"], 3)
        check("金额把两笔都算上", st["total"], 222227 * 3)
        check("最后发放时间留着", bool(st["uids"]["101"]["last_at"]), True)

        # 失败记录不算已发
        record_gift(led, P, 103, "user003", 222227, "3T", status="fail", note="HTTP 500")
        st = period_state(led, P)
        check("失败不算已发", "103" in st["uids"], False)
        check("失败人数", st["failed"], 1)
        check("成功笔数不受失败影响", st["sent"], 3)
        check("失败也不占次数", times_of(led, P, 103), 0)

        # ---- 重置周期：只动列出的 uid ----
        print("\n-- 重置周期（append-only，别人一个字不动）--")
        n_before = len(read_ledger(led))
        reset_period(led, P, uids=[101], note="这个人本周期要重发")
        st = period_state(led, P)
        check("101 次数归零", times_of(led, P, 101), 0)
        check("101 从名单里消失", "101" in st["uids"], False)
        check("102 不受影响", st["uids"]["102"]["times"], 1)
        check("总笔数减 2（101 那两笔没了）", st["sent"], 1)
        check("金额跟着减", st["total"], 222227)
        check("重置不删历史（文件里只多一条 reset）",
              len(read_ledger(led)), n_before + 1)
        check("101 那两条 gift 还在文件里（只是不算数）",
              len([r for r in read_ledger(led)
                   if r.get("type") == "gift" and str(r.get("uid")) == "101"]), 2)

        print("\n-- 重置后重发 → 重新从 1 开始数 --")
        record_gift(led, P, 101, "user001", 222227, "3T")
        check("101 回到 1 次", times_of(led, P, 101), 1)
        check("总笔数 2", period_state(led, P)["sent"], 2)
        record_gift(led, P, 101, "user001", 222227, "3T")
        check("第 2 次照记（上限由调用方管）", times_of(led, P, 101), 2)
        reset_period(led, P, uids=[101, 102], note="两个人一起重置")
        st = period_state(led, P)
        check("两个人都归零", [times_of(led, P, u) for u in (101, 102)], [0, 0])
        check("本周期一笔都不算了", (st["sent"], st["total"]), (0, 0))
        check("reset 记录两条都在",
              len([r for r in read_ledger(led) if r.get("type") == "reset"]), 2)
        check("空 uids 的重置不会误伤别人", st["uids"], {})

        # 坏行容错
        print("\n-- 坏行容错 --")
        with open(led, "a", encoding="utf-8") as f:
            f.write("这不是 json\n")
            f.write("\n")
            f.write('{"v":1,"type":"gift","period":"2026-09","uid":105,'
                    '"username":"u5","amount":1,"status":"ok"}\n')
        st = period_state(led, P)
        check("坏行被跳过，好行还在", st["uids"]["105"]["times"], 1)
        check("坏行不影响总笔数", st["sent"], 1)

        # 目录不存在要能建
        deep = Path(td) / "a" / "b" / "c.jsonl"
        append_record(deep, {"type": "gift", "period": P, "uid": 1})
        check("能自动建目录", deep.exists(), True)

    # ---- 单独一块：失败人数要按 uid 去重（重跑会留多条失败记录）----
    print("\n-- 失败人数按 uid 去重 --")
    with tempfile.TemporaryDirectory() as td:
        led2 = Path(td) / "l.jsonl"
        PERIOD_B = "2026-09"
        for _ in range(3):        # 同一个人被重跑 3 次，每次都失败
            record_gift(led2, PERIOD_B, 201, "user001", 222227, "3T",
                        status="fail", note="HTTP 500")
        record_gift(led2, PERIOD_B, 202, "user002", 222227, "3T",
                    status="fail", note="HTTP 500")
        st = period_state(led2, PERIOD_B)
        check("3 条失败记录 = 1 个人", st["failed"], 1 + 1)
        check("失败不占成功名额", st["sent"], 0)

        record_gift(led2, PERIOD_B, 201, "user001", 222227, "3T", status="ok")
        st = period_state(led2, PERIOD_B)
        check("失败后又补发成功 → 不再算失败", st["failed"], 1)
        check("补发成功算 1 笔", st["sent"], 1)
        check("补发成功的金额进合计", st["total"], 222227)

    print()
    print("=" * 66)
    if fails:
        print("自测失败：")
        for x in fails:
            print("  x", x)
        return False
    print("自测全部通过 ✅")
    return True


def main():
    ap = argparse.ArgumentParser(description="发放台账")
    ap.add_argument("--ledger", default=DEFAULT_LEDGER)
    ap.add_argument("--period", default=datetime.now().strftime("%Y-%m"))
    ap.add_argument("--limit", type=int, default=1, metavar="N",
                    help="每人本周期能领几次（默认 1，和 config.json 的 "
                         "payout.rounds_per_period 对齐）—— 只用来显示「发完」")
    ap.add_argument("--reset-period", action="store_true",
                    help="重置周期：把 --uid 列出的人在**本周期**的记录全部作废，"
                         "他们又能领 N 次。历史一条不删，只追加一条 reset")
    ap.add_argument("--uid", default=None,
                    help="配合 --reset-period：要重置的 uid（逗号分隔，必填）")
    ap.add_argument("--note", default="人工重置周期",
                    help="配合 --reset-period 的备注")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if selftest() else 1)

    limit = max(1, args.limit)
    if args.reset_period:
        uids = [x.strip() for x in (args.uid or "").split(",") if x.strip()]
        if not uids:
            sys.exit("[x] 要重置谁？加上 --uid 10001,10002（逗号分隔）")
        rec = reset_period(args.ledger, args.period, uids=uids, note=args.note)
        print(f"[OK] 已重置 {args.period} 的 {len(uids)} 人（{rec['at']}）—— "
              f"他们在本周期的记录作废，又能各领 {limit} 次。")
        print("     台账一条不删，只是那些版本不再算数。")
        print()

    st = period_state(args.ledger, args.period)
    print(f"台账文件 : {args.ledger}   "
          f"{'存在' if Path(args.ledger).exists() else '（还没有）'}")
    print(f"周期     : {args.period}    每人上限：{limit} 次")
    print(f"已发成功 : {st['sent']} 笔，合计 {st['total']:,}，"
          f"涉及 {len(st['uids'])} 人")
    print(f"有失败   : {st['failed']} 人")
    full = [u for u, v in st["uids"].items() if v["times"] >= limit]
    print(f"已发满   : {len(full)} 人（本周期不再发；要重发先重置这些人）")

    if st["uids"]:
        print()
        print(f"{'UID':>8}  {'用户名':<16} {'已发次数':>8} {'合计':>12} "
              f"{'还能领':>6} 最后发放")
        print("-" * 78)
        for uid, info in sorted(st["uids"].items(),
                                key=lambda kv: (-kv[1]["times"], kv[0])):
            left = max(0, limit - info["times"])
            print(f"{uid:>8}  {str(info['username'])[:16]:<16} "
                  f"{info['times']:>8} {info['amount']:>12,} "
                  f"{left:>6} {info['last_at']}")
    sys.exit(0)


if __name__ == "__main__":
    main()
