#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地模拟站点集成测试 —— 不碰真实站点，也不需要联网。

起一个 127.0.0.1 上的假 NexusPHP，验证真实 HTTP 链路：
  · cookie → 登录态判定 → uid 识别
  · 账号密码登录（POST takelogin.php，服务端 Set-Cookie）
  · 登录表单字段自动识别
  · 做种汇总行的真实抓取与解析
  · 写配置时注释不丢

各脚本里的 --selftest 只测纯函数，测不到这一层，所以单独放一个。

    python tests/selftest_http.py
"""

import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import login
import nexus

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"

GOOD = "c_secure_uid=MTAwMDE=; c_secure_pass=zzz"
USERNAME, PASSWORD = "tester", "s3cret"

PROFILE = (SAMPLES / "userdetails.sample.html").read_text(encoding="utf-8")
# 同一页、但「当前做种」里没有汇总行 —— 验证走做种列表兜底的路径。
# 用户名也换成别的（NoSummaryUser），不然 20001 会被详情页改名成 TestSeeder，
# 分不清改名到底来自哪一页。
PROFILE_NOSUM = (PROFILE
                 .replace("50 条记录 | 总大小：6.012 TB", "—")
                 .replace("<b>TestSeeder</b>", "<b>NoSummaryUser</b>"))
BLANK_PROFILE = "<html><body>这个用户没有做种记录</body></html>"
SEEDING_SUMMARY = (SAMPLES / "seeding_summary.sample.html").read_text(encoding="utf-8")
SEEDING_ROWS = (SAMPLES / "seeding.sample.html").read_text(encoding="utf-8")
GIFT_PAGE = (SAMPLES / "mybonus.sample.html").read_text(encoding="utf-8")
SELF_PAGE = (
    "<html><body>"
    '<a href="userdetails.php?id=10001">我</a>'
    '<a href="userdetails.php?id=10001">资料</a>'
    '<a href="userdetails.php?id=999">别人</a>'
    "</body></html>"
)
LOGIN_PAGE = ('<form method="post" action="takelogin.php">'
              '<input type="text" name="username">'
              '<input type="password" name="pwd_field">'
              '<input type="checkbox" name="keeplogged">'
              "</form>")
OK_PAGE = "<html><body>登录成功 <a href='index.php'>首页</a></body></html>"
BAD_PAGE = "<html><body>用户名或密码错误</body></html>"

fails = []
posts = []
gifts = []          # 服务端收到的「成功赠送」：(username, amount, message)
gift_tries = []     # 服务端收到的所有赠送请求（含失败），用来数重试次数
flaky = {}          # uid → 详情页被请求的次数（模拟「前两次 500，第三次才正常」）


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, cookie=None, code=200):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8"))
        posts.append((self.path, form))
        if self.path.startswith("/takelogin.php"):
            if form.get("username", [""])[0] == USERNAME and \
               form.get("pwd_field", [""])[0] == PASSWORD:
                self._send(OK_PAGE,
                           cookie="c_secure_uid=MTAwMDE=; Path=/; HttpOnly")
            else:
                self._send(BAD_PAGE)
            return
        if self.path.startswith("/mybonus.php"):
            # 假 NexusPHP 的赠送接口。先验登录态 —— 实际站点也是这么干的。
            if "c_secure_uid=MTAwMDE=" not in self.headers.get("Cookie", ""):
                self._send(LOGIN_PAGE)
                return
            # ★ 站点要这个隐藏字段，不带就不认（不给机会「静默失败却算成功」）
            if form.get("action", [""])[0] != "gift":
                self._send("<html><body>错误：缺少 action 字段，无法处理</body></html>")
                return
            u = form.get("username", [""])[0]
            amt = form.get("seedbonus", [""])[0]
            msg = form.get("message", [""])[0]
            gift_tries.append((u, amt))
            if u == "poor":                      # 模拟「魔力值不足」这类业务失败
                self._send("<html><body>错误：你的魔力值不足，赠送失败</body></html>")
                return
            if u == "toofast":
                # 模拟站点限速页：**没有跳转**，也没有任何可判定的落点。
                # 判据只看落点 URL，所以这属于「判不出来」→ 记失败、
                # 而且**绝不自动重试**（重试可能就是重复发钱）。
                self._send("<html><body><b>系统限制</b> 10 秒内只能点击交换按钮一次！"
                           "</body></html>")
                return
            if u == "silent":
                # 模拟站点：成功响应里**没有**任何成功/失败文字，
                # 只有一行 JS 跳转到 do=transfer —— 考「落点 URL 判定」这条腿
                gifts.append((u, int(amt), msg))
                self._send("<html><body>G值: 10,000,000.0"
                           "<script>window.location.href = "
                           "'http://127.0.0.1/mybonus.php?do=transfer';</script>"
                           "</body></html>")
                return
            if u == "dup":
                # 模拟 10 秒内重复点：站点判重复，跳 do=duplicated —— 确定没送出，
                # 考「重复跳转 = 可安全重试」这条腿
                self._send("<html><body>G值: 10,000,000.0"
                           "<script>window.location.href = "
                           "'http://127.0.0.1/mybonus.php?do=duplicated';</script>"
                           "</body></html>")
                return
            if u and amt.isdigit():
                gifts.append((u, int(amt), msg))
                # 成功响应里一个字都没有，只有一行跳转 —— 假站点照抄这个行为。
                # 并故意在页面上塞一个「错误」：判据不看文字，所以不会误杀。
                self._send("<html><body>（如有错误请联系管理员）"
                           "<script>window.location.href = "
                           "'http://127.0.0.1/mybonus.php?do=transfer';</script>"
                           "</body></html>")
            else:
                self._send("<html><body>错误：参数不对</body></html>")
            return
        self._send("<html>?</html>")

    def do_GET(self):
        cookie = self.headers.get("Cookie", "")
        if self.path.startswith("/login.php"):
            self._send(LOGIN_PAGE)
            return
        if "c_secure_uid=MTAwMDE=" not in cookie:
            self._send(LOGIN_PAGE)                 # 没登录态 → 装成登录页
            return
        if self.path.startswith("/mybonus.php"):
            self._send(GIFT_PAGE)                  # 赠送页（带隐藏字段 action=gift）
        elif self.path.startswith("/userdetails.php?id="):
            # 10001 → 详情页自带做种汇总；
            # 20001 / 30003 / 40004 → 详情页没有汇总行（走做种列表兜底）；
            # 50005 → 前两次 500、第三次 200（验证瞬时失败自动重试）；
            # 60006 → 一直 502（验证「抓不到 = 未测」，绝不悄悄写 0）；
            # 其他 uid → 空页。
            if "id=50005" in self.path:
                flaky["50005"] = flaky.get("50005", 0) + 1
                if flaky["50005"] <= 2:
                    self._send("<html><body>站点打嗝</body></html>", code=500)
                else:
                    self._send(PROFILE)
            elif "id=60006" in self.path:
                self._send("<html><body>站点打嗝</body></html>", code=502)
            elif "id=10001" in self.path:
                self._send(PROFILE)
            elif self.path.split("id=", 1)[1][:5] in ("20001", "30003", "40004"):
                self._send(PROFILE_NOSUM)
            else:
                self._send(BLANK_PROFILE)
        elif self.path.startswith("/userdetails.php"):
            self._send(SELF_PAGE)                  # 裸个人页 → 靠链接猜 uid
        elif self.path.startswith("/getusertorrentlistajax.php"):
            # 10001 给「带汇总行」的响应；20001 给「没有汇总行」的（验证逐行累加回退）；
            # 30003 原样回「没有记录」（0 做种账号就是这么回的）→ 必须按 0 记，不是未测；
            # 40004 回一页认不出来的东西 → 必须记未测，数据列留空。
            if "userid=20001" in self.path:
                self._send(SEEDING_ROWS)
            elif "userid=10001" in self.path:
                self._send(SEEDING_SUMMARY)
            elif "userid=40004" in self.path:
                self._send("<html><body>系统维护中</body></html>")
            else:
                self._send("没有记录")
        else:
            self._send("<html>?</html>")

    def log_message(self, *a):
        pass


def check(label, got, want):
    ok = got == want
    print(f"  [{'OK' if ok else 'XX'}] {label}: {got}" + ("" if ok else f"  (期望 {want})"))
    if not ok:
        fails.append(f"{label}: 期望 {want}，实际 {got}")


def check_true(label, got):
    check(label, bool(got), True)


def main():
    # ThreadingHTTPServer：界面那边联网刷新是并发抓的，单线程服务端会把
    # 请求排成一队，测不出真实行为
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    base = f"http://127.0.0.1:{port}"
    ep = nexus.Endpoints(base, None)

    print("=" * 66)
    print(f"本地模拟站点集成测试    {base}")
    print("=" * 66)

    print("\n-- 有效 cookie + 已知 uid --")
    ok, uid, uname, msg = login.verify(GOOD, base, ep, 10001)
    check("验证通过", ok, True)
    check("uid", uid, 10001)
    check("用户名", uname, "TestSeeder")

    print("\n-- 有效 cookie + 未知 uid（走页面猜测）--")
    ok, uid, uname, msg = login.verify(GOOD, base, ep, None)
    check("验证通过", ok, True)
    check("猜到的 uid", uid, 10001)
    check("用户名", uname, "TestSeeder")

    print("\n-- 无效 cookie 必须被识破 --")
    ok, uid, uname, msg = login.verify("c_secure_uid=YQ==; c_secure_pass=bad",
                                       base, ep, 10001)
    check("验证失败", ok, False)
    check_true("提示是登录页", "登录页" in msg)

    print("\n-- c_secure_uid 反解 uid --")
    check("base64 → uid", nexus.decode_c_secure_uid("MTAwMDE="), 10001)

    print("\n-- 模拟从 DevTools Network 复制的整段 --")
    pasted = ("GET /mybonus.php HTTP/1.1\r\nHost: x\r\n"
              "Cookie: c_secure_uid=MTAwMDE=; c_secure_pass=zzz; "
              "c_secure_login=bm90aGluZw==\r\n")
    parsed = nexus.parse_cookie_string(pasted)
    check("解析出 cookie", parsed,
          "c_secure_uid=MTAwMDE=; c_secure_pass=zzz; c_secure_login=bm90aGluZw==")
    ok, uid, uname, msg = login.verify(parsed, base, ep,
                                       nexus.decode_c_secure_uid("MTAwMDE="))
    check("解析结果能通过验证", ok, True)

    print("\n-- 账号密码登录（浏览器开着也能走这条路）--")
    good_cookie, msg = login.password_login(base, ep, USERNAME, PASSWORD)
    check_true("拿到 cookie", good_cookie)
    check_true("cookie 含 c_secure_uid", "c_secure_uid=" in good_cookie)
    print(f"       {msg}")
    ok, uid, uname, _ = login.verify(good_cookie, base, ep, 10001)
    check("登出来的 cookie 能过验证", ok, True)
    if ok:
        check("uid", uid, 10001)

    print("\n-- 登录表单字段要自动认出来（本站是 pwd_field，不是 password）--")
    s = nexus.Session(base, "")
    action, fu, fp = login._guess_login_form(s, ep, None)
    check("action", action, "takelogin.php")
    check("用户名字段", fu, "username")
    check("密码字段靠 type=password 认出", fp, "pwd_field")

    print("\n-- 配置里写死字段时以配置为准 --")
    action, fu, fp = login._guess_login_form(
        s, ep, {"action": "custom.php", "fields": {"username": "u", "password": "p"}})
    check("action 用配置", action, "custom.php")
    check("字段用配置", (fu, fp), ("u", "p"))

    print("\n-- 密码错了必须被拒 --")
    cookie, msg = login.password_login(base, ep, USERNAME, "wrong")
    check("拿不到 cookie", cookie, "")
    check_true("提示被拒", "拒" in msg or "不对" in msg)

    print("\n-- 个人详情页里的做种汇总（不碰做种列表接口）--")
    sess = nexus.Session(base, GOOD)
    st, html, _ = sess.get(ep.path("user_details", uid=10001))
    check("HTTP", st, 200)
    count, size = nexus.parse_userdetails_seeding(html)
    check("详情页汇总·数量", count, 50)
    check("详情页汇总·体积(字节)", size, round(6.012 * 1024 ** 4))
    check_true("没把「上传量 2.00 TB」当成做种体积",
               size != 2 * 1024 ** 4 and size != 500 * 1024 ** 3)
    print(f"       {count} 条 | {size / 1024**4:.3f} TB")

    print("\n-- 做种列表接口的汇总行解析（兜底路径用的）--")
    st, html, _ = sess.get(ep.path("seeding_list", uid=10001, page=1))
    check("HTTP", st, 200)
    count, size = nexus.parse_seeding_summary(html)
    check("汇总行·数量", count, 4893)
    check("汇总行·体积(字节)", size, 56635843946741)
    print(f"       {count} 条 | {size / 1024**4:.3f} TB")

    print("\n-- 没有汇总行时，逐行累加必须兜住 --")
    st2, html2, _ = sess.request(ep.path("seeding_list", uid=20001, page=1))
    c2, s2 = nexus.parse_seeding_summary(html2)
    check("汇总行解析不出来", (c2, s2), (None, None))
    rows = nexus.parse_seeding_rows(html2)
    check("逐行读到 3 行", len(rows), 3)
    check("逐行累加 = 8 GB", sum(r["size_bytes"] for r in rows), 8 * 1024 ** 3)

    import settle as settle_mod
    from roster import Member

    print("\n-- 「站点说没有记录」和「没抓到」必须分清 --")
    check("没有记录", nexus.says_no_record("没有记录"), True)
    check("没有做种记录（换个说法也算）",
          nexus.says_no_record("<html>这个用户没有做种记录</html>"), True)
    check("暂无记录", nexus.says_no_record("暂无记录"), True)
    check("有数据的页面不算", nexus.says_no_record(SEEDING_SUMMARY), False)
    check("空响应不算", nexus.says_no_record(""), False)
    check("认不出的页面不算",
          nexus.says_no_record("<html><body>系统维护中</body></html>"), False)

    print("\n-- 瞬时失败自动重试（只读请求，重试是安全的）--")
    class _Flaky:
        def __init__(self, seq):
            self.seq, self.calls = list(seq), 0

        def request(self, path, params=None, data=None, referer=None):
            self.calls += 1
            # 用完就一直是最后一条（模拟「站点一直挂着」）
            code, body = self.seq[min(self.calls - 1, len(self.seq) - 1)]
            return code, body, path

    f = _Flaky([(0, "__EXCEPTION__ URLError: SSL 断了"), (500, "打嗝"),
                (200, "<html>到了</html>")])
    st, body, _ = settle_mod.fetch_with_retry(f, "userdetails.php", backoff=0)
    check("重试到第三次才成功", (st, f.calls), (200, 3))
    f2 = _Flaky([(0, "__EXCEPTION__ URLError: 断了")])
    st2, body2, _ = settle_mod.fetch_with_retry(f2, "x", attempts=3, backoff=0)
    check("一路失败 → 返回最后一次的失败", (st2, f2.calls), (0, 3))
    f3 = _Flaky([(404, "没有这页"), (200, "ok")])
    st3, _, _ = settle_mod.fetch_with_retry(f3, "x", backoff=0)
    check("404 不重试（重试没用）", (st3, f3.calls), (404, 1))
    check_true("异常文本缩成一句人话（去掉内部标记）",
               "__EXCEPTION__" not in
               settle_mod.brief_reason("__EXCEPTION__ URLError: 断了"))
    check_true("太长会截断",
               len(settle_mod.brief_reason("x" * 300)) < 90)

    print("\n-- 整条刷新链路（详情页汇总 / 列表页兜底 / 重试 / 未测）--")
    members = [
        Member(uid=10001, username="user001", plan_id="3T"),   # 详情页汇总
        Member(uid=20001, username="user002", plan_id="3T"),   # 没汇总 → 列表页逐行兜底
        Member(uid=None, username="user003", plan_id="3T"),    # 无 uid → 跳过
        Member(uid=10001, username="", plan_id="3T"),          # 表里没填用户名的人
        Member(uid=30003, username="user004", plan_id="3T"),   # 站点说「没有记录」→ 真 0
        Member(uid=40004, username="user005", plan_id="3T"),   # 认不出 → 未测
        Member(uid=50005, username="user006", plan_id="3T"),   # 前两次 500 → 重试救回
        Member(uid=60006, username="user007", plan_id="3T"),   # 一直 502 → 未测
    ]
    okn, failn = settle_mod.refresh_measurements(members, ["volume"], sess, ep,
                                                 delay=0, backoff=0)
    check("成功数", okn, 5)
    check("失败数", failn, 2)
    check("详情页汇总 → 6.012 TB", round(members[0].measured_tb, 3), 6.012)
    check("详情页没汇总 → 列表页逐行兜底 8 GB",
          members[1].measured_bytes, 8 * 1024 ** 3)
    check("无 uid 的人不动", members[2].measured_bytes, None)
    check("站点说「没有记录」→ 按 0 记（真·0 做种）", members[4].measured_bytes, 0)
    check("0 做种也回写检查日期", members[4].check_raw.endswith("0.000T(0)"), True)
    check("站点说「没有记录」不算失败", (members[4].measure_error, okn >= 5), ("", True))
    check("抓不到 → 实测列留空（不再悄悄写 0）", members[5].measured_bytes, None)
    check("抓不到 → 检查日期也留空", (members[5].check_raw, members[5].check_date),
          ("", None))
    check_true("原因写进 measure_error：认不出汇总行",
               "认不出" in members[5].measure_error)
    check("瞬时 500 自动重试 → 第 3 次拿到数据", round(members[6].measured_tb, 3), 6.012)
    check("重试成功后不留错误", members[6].measure_error, "")
    check("一直 502 → 未测，实测留空", members[7].measured_bytes, None)
    check_true("原因点名 HTTP 502", "502" in members[7].measure_error)
    import re as _re
    check_true("回写字形如 日期-6.012T(数量)",
               bool(_re.search(r"\d{6}-6\.012T\(\d+\)$", members[0].check_raw)))
    # 用户名以站点为准：详情页上是 TestSeeder，表里写的 user001 被改名
    check("表里写的旧名被站点改名", members[0].username, "TestSeeder")
    check("没填用户名的行被补上", members[3].username, "TestSeeder")

    print("\n-- 逐人进度回调 + 页面快照 --")
    seen = []
    with tempfile.TemporaryDirectory() as td2:
        ms2 = [Member(uid=10001, username="", plan_id="3T"),
               Member(uid=None, username="noid", plan_id="3T")]
        okn, failn = settle_mod.refresh_measurements(
            ms2, ["volume"], sess, ep, delay=0,
            progress=lambda i, n, m, text="": seen.append((i, n, m.uid, text)),
            snapshot_dir=Path(td2))
        check("进度按人报（成功的多报一次带数据）", len(seen), 3)
        check("进度序号", [s[0] for s in seen], [1, 1, 2])
        check("进度总数", [s[1] for s in seen], [2, 2, 2])
        check("进度带成员", [s[2] for s in seen], [10001, 10001, None])
        check_true("抓完那次带实测值", "6.012" in (seen[1][3] or ""))
        check_true("跳过 uid 的人也报了进度", bool(seen[2][3]))
        snaps = sorted(p.name for p in Path(td2).glob("debug_userdetails_*.html"))
        check_true("详情页快照已存（第一张总是存）", len(snaps) >= 1)
        check_true("快照内容是原样 HTML", b"TestSeeder" in
                   (Path(td2) / snaps[0]).read_bytes() if snaps else False)

    print("\n-- 并发抓取（workers>1）：结果和串行一样，回调照旧 --")
    ms3 = [Member(uid=10001, username="", plan_id="3T"),
           Member(uid=20001, username="", plan_id="3T"),
           Member(uid=30003, username="", plan_id="3T"),
           Member(uid=None, username="noid", plan_id="3T")]
    done3 = []
    okn, failn = settle_mod.refresh_measurements(
        ms3, ["volume"], sess, ep, delay=0, workers=4, backoff=0,
        session_factory=lambda: nexus.Session(base, GOOD),
        on_member=done3.append)
    check("并发：成功 3 人", okn, 3)
    check("并发：失败 0 人", failn, 0)
    check("并发：每个人都回填了", [m.measured_bytes is not None for m in ms3],
          [True, True, True, False])
    check("并发：实测值和串行一致（详情页 6.012 TB）",
          round(ms3[0].measured_tb, 3), 6.012)
    check("并发：站点说「没有记录」的按 0", ms3[2].measured_bytes, 0)
    check("并发：on_member 每人一次（含无 uid 的那个）", len(done3), 4)
    check("并发：on_member 带回来的就是本人",
          sorted(str(m.uid) for m in done3),
          sorted(str(m.uid) for m in ms3))

    print("\n-- 写配置时注释不能丢 --")
    with tempfile.TemporaryDirectory() as td:
        cfgp = Path(td) / "config.json"
        cfgp.write_text((ROOT / "config.example.json").read_text(encoding="utf-8"),
                        encoding="utf-8")
        before = cfgp.read_text(encoding="utf-8")
        p, changed = login.save_fields(cfgp, {"cookie": good_cookie, "uid": 10001})
        after = p.read_text(encoding="utf-8")
        check_true("注释还在", "// 站点地址" in after or "// ====" in after)
        check("注释行数不变", after.count("//"), before.count("//"))
        check("cookie 已写入", nexus.read_jsonc(cfgp)["cookie"], good_cookie)
        check("uid 已写入", nexus.read_jsonc(cfgp)["uid"], 10001)
        check("改动字段报告正确", sorted(changed), ["cookie", "uid"])
        check_true("其余字段没被动", nexus.read_jsonc(cfgp)["plans"][0]["id"] == "3T")

    # ==========================================================
    # 端到端发放：工资表 → 清单 → 真发 → 发满跳过 → 失败 → 重置周期
    # ==========================================================
    print("\n-- 端到端发放：真发两笔 --")
    import ledger as ledger_mod
    import payout as payout_mod

    TB = 1024 ** 4

    def mk(uid, name, plan, tb):
        m = Member(uid=uid, username=name, plan_id=plan)
        m.measured_bytes = int(tb * TB)
        m.measured_tb = tb
        return m

    settings = settle_mod.validate_settings({
        "assessment": {"metrics": ["volume"]},
        "plans": [{"id": "3T", "min_volume_tb": 3, "salary": 200000},
                  {"id": "6T", "min_volume_tb": 6, "salary": 400000}],
        "payout": {"tax_rate": 0.9, "tax_flat": 4, "interval_seconds": 0,
                   "message_template": "保种组 {period} · {plan_id}"},
    }, "<selftest>")

    spec = {
        "action": base + "/mybonus.php",
        "fields": {"username": "username", "amount": "seedbonus", "message": "message"},
        "success_url": payout_mod.DEFAULT_SUCCESS_URL,
        "duplicate_url": payout_mod.DUPLICATE_URL,
    }
    check("表单规格配全 → 允许真发", payout_mod.validate_gift_form(spec), [])

    s = nexus.Session(base, GOOD)
    live = payout_mod.resolve_form_target(ep, s)
    check_true("赠送表单能现抓", bool(live))
    if live:
        check("现抓到的提交目标", live["action"], "/mybonus.php")
        check("现抓到隐藏字段 action=gift", live["hidden"], {"action": "gift"})

    print("\n-- 赠送页认字段：成功URL后缀预填实测默认值，换站要人核对 --")
    guess, notes = payout_mod.suggest_gift_form(GIFT_PAGE, base + "/mybonus.php",
                                                ep=ep)
    check("认出的收礼人字段", guess["fields"]["username"], "username")
    check("认出的金额字段", guess["fields"]["amount"], "seedbonus")
    check("认出的留言字段", guess["fields"]["message"], "message")
    check("提交目标补成相对路径", guess["action"], "/mybonus.php")
    check("success_url 预填默认值", guess["success_url"],
          payout_mod.DEFAULT_SUCCESS_URL)
    check_true("提示里说清换站要自己核",
               any("换站" in n for n in notes))
    bad_guess, bad_notes = payout_mod.suggest_gift_form("<html>啥也没有</html>", base)
    check("页面上没表单 → 返回 None", bad_guess, None)
    check_true("并给出下一步", any("F12" in n or "表单" in n for n in bad_notes))

    members = [mk(10001, "user001", "3T", 4.0),
               mk(20001, "user002", "6T", 7.0)]
    rows, errors = settle_mod.build_payroll(members, settings)
    check("工资表无错误", errors, [])
    check("月薪→应赠 反算（20W）", [r["gift"] for r in rows][0], 222227)
    check("月薪→应赠 反算（40W）", [r["gift"] for r in rows][1], 444449)

    with tempfile.TemporaryDirectory() as td:
        led = Path(td) / "ledger.jsonl"
        quiet = (lambda *a: None)

        items, skipped, quota = payout_mod.plan_payout(rows, settings, led,
                                                      "2026-09")
        check("待发 2 人", [i["username"] for i in items], ["user001", "user002"])
        check("两人都还没发过", quota["fresh"], 2)
        check("默认每人每周期 1 次", quota["limit"], 1)

        fresh = nexus.Session(base, GOOD)
        st = payout_mod.execute(items, settings, ep, fresh, led, "2026-09",
                                spec=spec, dry_run=False, delay=0, log=quiet)
        check("真发成功 2 笔", st["sent"], 2)
        check("失败 0 笔", st["failed"], 0)
        check("实发合计 = 应赠合计", st["amount"], 222227 + 444449)
        check("服务端收到 2 笔赠送", len(gifts), 2)
        check("金额与顺序正确", [g[1] for g in gifts], [222227, 444449])
        check("留言带周期和方案", gifts[0][2], "保种组 2026-09 · 3T")
        check_true("隐藏字段 action=gift 真的发出去了",
                   any(p[0].startswith("/mybonus.php") and
                       p[1].get("action", [""])[0] == "gift" for p in posts))

        print("\n-- 重跑同一条命令：默认每人 1 次 → 发满的一律跳过（防多发）--")
        before = len(gifts)
        st2 = payout_mod.execute(items, settings, ep, fresh, led, "2026-09",
                                 spec=spec, dry_run=False, delay=0, log=quiet)
        check("重跑发出 0 笔", st2["sent"], 0)
        check("两人都因「本周期已发满 1 次」被跳过", st2["skipped_full"], 2)
        check("服务端没收到新请求", len(gifts), before)

        print("\n-- 发过的人照样进清单（不逐人去重），状态显示「发完」--")
        items2, skipped2, quota2 = payout_mod.plan_payout(rows, settings, led,
                                                          "2026-09")
        check("两个人还在清单里", [i["username"] for i in items2],
              ["user001", "user002"])
        check("跳过名单是空的（没有「已发过就剔除」这回事）", skipped2, [])
        check("都已发满", (quota2["done"], quota2["fresh"]), (2, 0))
        check("状态「发完」", [i["state"] for i in items2], ["发完", "发完"])
        check("发满的人默认不勾", [i["default_pick"] for i in items2],
              [False, False])

        print("\n-- 业务失败不能被记成成功 --")
        bad = [mk(30001, "poor", "3T", 4.0)]
        rows_bad, _ = settle_mod.build_payroll(bad, settings)
        items_bad, _, _ = payout_mod.plan_payout(rows_bad, settings, led, "2026-10")
        st3 = payout_mod.execute(items_bad, settings, ep, fresh, led, "2026-10",
                                 spec=spec, dry_run=False, delay=0, retries=0,
                                 log=quiet)
        check("记 1 笔失败", st3["failed"], 1)
        check("没记成成功", st3["sent"], 0)
        check("服务端没有成功记录", len(gifts), 2)
        check_true("失败原因报出落点",
                   "落点 URL" in st3["results"][0]["why"])
        check("台账里查不到成功记录",
              ledger_mod.times_of(led, "2026-10", 30001), 0)
        st10 = ledger_mod.period_state(led, "2026-10")
        check("该周期成功发出 0 笔", st10["sent"], 0)
        check("该周期失败 1 人", st10["failed"], 1)

        print("\n-- 站点要隐藏字段，配漏了就必须失败（而不是假装成功）--")
        nohidden = dict(spec, action="/mybonus.php")
        rows4, _ = settle_mod.build_payroll([mk(50001, "user010", "3T", 4.0)], settings)
        items4, _, _ = payout_mod.plan_payout(rows4, settings, led, "2026-12")
        before2 = len(gifts)
        st5 = payout_mod.execute(items4, settings, ep, fresh, led, "2026-12",
                                 spec=nohidden, dry_run=False, delay=0, retries=0,
                                 fetch_form=False, log=quiet)
        check("没带隐藏字段 → 失败", st5["failed"], 1)
        check("服务端没收到成功赠送", len(gifts), before2)
        check("台账里没有成功记录",
              ledger_mod.times_of(led, "2026-12", 50001), 0)

        print("\n-- 站点限速页（没跳转、落点判不出来）→ 记失败且不重试 --")
        rows_lim, _ = settle_mod.build_payroll([mk(60001, "toofast", "3T", 4.0)],
                                               settings)
        items_lim, _, _ = payout_mod.plan_payout(rows_lim, settings, led, "2027-01")
        tries0, gifts0 = len(gift_tries), len(gifts)
        st_lim = payout_mod.execute(items_lim, settings, ep, fresh, led, "2027-01",
                                    spec=spec, dry_run=False, delay=0, retries=2,
                                    log=quiet)
        check("限速页 → 记失败（不是成功）", (st_lim["failed"], st_lim["sent"]), (1, 0))
        check("判不出来就不敢重发：只请求了 1 次", len(gift_tries) - tries0, 1)
        check("重试也没成功，服务端无成功记录", len(gifts) - gifts0, 0)
        check_true("理由说清落点不是成功页",
                   "落点 URL" in st_lim["results"][0]["why"])
        check("失败只记账：台账里查不到成功记录",
              ledger_mod.times_of(led, "2027-01", 60001), 0)

        print("\n-- 成功响应：一个字都没有，靠落点 URL do=transfer 判定 --")
        rows_sil, _ = settle_mod.build_payroll([mk(80001, "silent", "3T", 4.0)],
                                               settings)
        items_sil, _, _ = payout_mod.plan_payout(rows_sil, settings, led, "2027-03")
        gifts0 = len(gifts)
        st_sil = payout_mod.execute(items_sil, settings, ep, fresh, led, "2027-03",
                                    spec=spec, dry_run=False, delay=0, retries=0,
                                    log=quiet)
        check("响应没文字但跳转到 do=transfer → 算成功", st_sil["sent"], 1)
        check("服务端真的收到了", len(gifts) - gifts0, 1)
        check_true("成功理由点名跳转",
                   "do=transfer" in st_sil["results"][0]["why"])
        check("台账记了 1 次成功",
              ledger_mod.times_of(led, "2027-03", 80001), 1)

        print("\n-- 重复提交（跳 do=duplicated）= 确定没送出，允许重试 --")
        rows_stk, _ = settle_mod.build_payroll([mk(81001, "dup", "3T", 4.0)],
                                               settings)
        items_stk, _, _ = payout_mod.plan_payout(rows_stk, settings, led, "2027-04")
        tries0, gifts0 = len(gift_tries), len(gifts)
        st_stk = payout_mod.execute(items_stk, settings, ep, fresh, led, "2027-04",
                                    spec=spec, dry_run=False, delay=0, retries=2,
                                    log=quiet)
        check("重复跳转 → 记失败", (st_stk["failed"], st_stk["sent"]), (1, 0))
        check("确定没提交 → 重试了 1+2 次", len(gift_tries) - tries0, 3)
        check("服务端始终没收到成功赠送", len(gifts) - gifts0, 0)
        check_true("失败理由说清「重复提交」",
                   "重复提交" in st_stk["results"][0]["why"])
        check("台账里没有成功记录",
              ledger_mod.times_of(led, "2027-04", 81001), 0)

        print("\n-- 成功URL后缀被清空 → 拒绝真发（安全闸仍在）--")
        all_off = dict(spec, success_url="")
        check_true("清空要报出来",
                   any("success_url" in p
                       for p in payout_mod.validate_gift_form(all_off)))
        check("不写 success_url 就走默认值，不拦",
              payout_mod.validate_gift_form(
                  {k: v for k, v in spec.items() if k != "success_url"}), [])

        print("\n-- 判据配错（认不出成功落点）→ 记失败、而且一次都不许重试 --")
        rows_amb, _ = settle_mod.build_payroll([mk(70001, "user011", "3T", 4.0)],
                                               settings)
        items_amb, _, _ = payout_mod.plan_payout(rows_amb, settings, led, "2027-02")
        # 故意把成功后缀写成站点上根本没有的串 —— 模拟「判据配错」这种最危险的情况
        wrong = dict(spec, success_url="do=这段站点上根本没有")
        tries0, gifts0 = len(gift_tries), len(gifts)
        st_amb = payout_mod.execute(items_amb, settings, ep, fresh, led, "2027-02",
                                    spec=wrong, dry_run=False, delay=0, retries=2,
                                    log=quiet)
        check("认不出成功落点 → 记失败", st_amb["failed"], 1)
        check("只请求了 1 次，没有自动重试", len(gift_tries) - tries0, 1)
        check("服务端其实收到了这笔（所以绝不能自动重发）", len(gifts) - gifts0, 1)
        check("只有「重复提交」才重试",
              payout_mod.retryable_failure(
                  "站点判定重复提交（跳到 do=duplicated）—— 确定没送出"), True)
        check("限速页那句不再算判据（页面上没有落点）",
              payout_mod.retryable_failure(
                  "系统限制 10 秒内只能点击交换按钮一次"), False)
        check("网络中断不重试",
              payout_mod.retryable_failure("网络错误：timed out"), False)

        print("\n-- 台账快照：人数 / 笔数 / 金额 --")
        st9 = ledger_mod.period_state(led, "2026-09")
        check("2026-09 快照：2 人 2 笔 / 666676",
              (len(st9["uids"]), st9["sent"], st9["total"]), (2, 2, 666676))
        check("每人各 1 次",
              sorted(v["times"] for v in st9["uids"].values()), [1, 1])

        print("\n-- 每人每周期 N 次：同一个人点两次「发放」就真领两次（整条链路）--")
        settings_r = settle_mod.validate_settings({
            "assessment": {"metrics": ["volume"]},
            "plans": [{"id": "3T", "min_volume_tb": 3, "salary": 200000}],
            "payout": {"tax_rate": 0.9, "tax_flat": 4, "interval_seconds": 0,
                       "rounds_per_period": 2,
                       "message_template": "保种组 {period} · {plan_id}"},
        }, "<selftest>")
        P_R = "2027-05"
        rows_r, _ = settle_mod.build_payroll([mk(90001, "user021", "3T", 4.0)],
                                             settings_r)

        it_r1, _, q_r1 = payout_mod.plan_payout(rows_r, settings_r, led, P_R)
        check("第 1 次：还没发过 → 默认勾上",
              ([i["default_pick"] for i in it_r1], q_r1["fresh"]), ([True], 1))
        g0 = len(gifts)
        st_r1 = payout_mod.execute(it_r1, settings_r, ep, fresh, led, P_R,
                                   spec=spec, dry_run=False, delay=0, log=quiet)
        check("第 1 次真发出 1 笔", st_r1["sent"], 1)
        check("服务端收到 1 笔", len(gifts) - g0, 1)

        # ★ 关键：不做逐人去重 —— 同一周期同一个人，再点一次就是真的再发一笔
        it_r2, _, q_r2 = payout_mod.plan_payout(rows_r, settings_r, led, P_R)
        check("第 2 次：人还在清单里（不做逐人去重）",
              [i["username"] for i in it_r2], ["user021"])
        check("已发 1 次 / 上限 2 次 → 状态「已发 1 次」",
              it_r2[0]["state"], "已发 1 次")
        check("没发满 → 还是默认勾上", [i["default_pick"] for i in it_r2], [True])
        st_r2 = payout_mod.execute(it_r2, settings_r, ep, fresh, led, P_R,
                                   spec=spec, dry_run=False, delay=0, log=quiet)
        check("第 2 次又真发出 1 笔（同一个人领到第 2 次）", st_r2["sent"], 1)
        check("服务端一共收到 2 笔", len(gifts) - g0, 2)
        st_r = ledger_mod.period_state(led, P_R)
        check("台账累计 2 笔 / 金额翻倍",
              (st_r["sent"], st_r["total"]), (2, st_r1["amount"] * 2))
        check("同一个 uid 记了 2 次",
              ledger_mod.times_of(led, P_R, 90001), 2)

        # 发满 2 次 → 第 3 次点「发放」会被逐笔跳过，一次都不会多
        it_r3, _, q_r3 = payout_mod.plan_payout(rows_r, settings_r, led, P_R)
        check("发满 → 状态「发完」", it_r3[0]["state"], "发完")
        check("发满 → 默认不勾", [i["default_pick"] for i in it_r3], [False])
        check("quota：1 人发满 / 0 人没发", (q_r3["done"], q_r3["fresh"]), (1, 0))
        tries_r = len(gift_tries)
        st_r3 = payout_mod.execute(it_r3, settings_r, ep, fresh, led, P_R,
                                   spec=spec, dry_run=False, delay=0, log=quiet)
        check("第 3 次：一笔都不发",
              (st_r3["sent"], st_r3["skipped_full"]), (0, 1))
        check("连请求都没发出去", len(gift_tries) - tries_r, 0)
        check("服务端还是只有 2 笔", len(gifts) - g0, 2)

        # 「重置周期」→ 这个人归零，又能各领 2 次；历史一条不删
        n_before = len(ledger_mod.read_ledger(led))
        ledger_mod.reset_period(led, P_R, [90001], note="集成测试重置")
        check("重置后已发次数归零", ledger_mod.times_of(led, P_R, 90001), 0)
        check("台账只多了一条 reset（历史没删）",
              len(ledger_mod.read_ledger(led)), n_before + 1)
        it_r4, _, _ = payout_mod.plan_payout(rows_r, settings_r, led, P_R)
        check("重置后又回到「待发」+ 默认勾上",
              (it_r4[0]["state"], it_r4[0]["default_pick"]), ("待发", True))
        st_r4 = payout_mod.execute(it_r4, settings_r, ep, fresh, led, P_R,
                                   spec=spec, dry_run=False, delay=0, log=quiet)
        check("重置后真的又能发出 1 笔", st_r4["sent"], 1)
        check("服务端累计 3 笔（2 + 重置后 1）", len(gifts) - g0, 3)

        print("\n-- cookie 失效时一笔都不发 --")
        dead = nexus.Session(base, "c_secure_uid=YQ==; c_secure_pass=bad")
        rows3, _ = settle_mod.build_payroll([mk(40001, "user009", "3T", 4.0)], settings)
        items3, _, _ = payout_mod.plan_payout(rows3, settings, led, "2026-11")
        before = len(gifts)
        st4 = payout_mod.execute(items3, settings, ep, dead, led, "2026-11",
                                 spec=spec, dry_run=False, delay=0, retries=0,
                                 log=quiet)
        check("一笔都没发成", st4["sent"], 0)
        check("服务端没收到", len(gifts), before)
        check_true("识别为登录态失效",
                   "登录" in st4["results"][0]["why"])

    srv.shutdown()
    print()
    print("=" * 66)
    if fails:
        print("测试失败：")
        for x in fails:
            print("  x", x)
        return False
    print("集成测试全部通过 ✅")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
