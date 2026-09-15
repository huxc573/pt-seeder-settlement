#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
站点接口探测（纯标准库，零第三方依赖）

全程只读，不做任何写操作。做三件事：

  [1] 验证 cookie 是否有效，并解析个人页字段
  [2] 解析做种汇总行「N 条记录 | 总大小：X」—— 这是发工资的计薪依据
  [3] 探测赠送表单结构（action + 字段名），**不猜字段**

用法：
  python probe.py --selftest      # 先跑这个，离线自测解析逻辑
  python probe.py                 # 真实探测（需先跑 python login.py）
  python probe.py --uid 10003     # 换个 uid 探测（比如某个组员）
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import nexus

ROOT = nexus.DATA_ROOT
SAMPLES = ROOT / "samples"               # 快照落点（数据目录）
SAMPLE_RES = nexus.RES_ROOT / "samples"  # 只读样例（打包后在资源目录）

PROBE_ENDPOINTS = ("login", "user_details", "seeding_list", "gift_bonus")


def hr(title=""):
    print("=" * 62)
    if title:
        print(title)
        print("=" * 62)


# ============================================================
# 真实探测
# ============================================================

def probe(config_path, uid_override=None):
    SAMPLES.mkdir(exist_ok=True)

    cfg, ep, sess = nexus.load_config(config_path, ROOT, require_cookie=True)
    uid = uid_override or cfg.get("uid")

    metrics = (cfg.get("assessment") or {}).get("metrics") or ["volume"]
    if isinstance(metrics, str):
        metrics = [metrics]

    print(f"站点     : {ep.base}")
    print(f"探测 uid : {uid}")
    print(f"考核口径 : {' + '.join(metrics)}")

    print()
    hr("-- 接口清单 --")
    for key in PROBE_ENDPOINTS:
        if ep.has(key):
            print(f"  {key:<16} {ep.url(key, uid=uid, page=1)}")
        else:
            print(f"  {key:<16} [未配置]")

    # ---------------- [1] 个人页 ----------------
    print()
    hr("[1/3] 个人页 —— 验证登录态 + 解析字段")
    status, html, final_url = sess.request(ep.path("user_details", uid=uid))
    (SAMPLES / f"userdetails_{uid}.html").write_text(html, encoding="utf-8")
    print(f"HTTP {status}  |  {len(html)} 字符  |  已存 samples/userdetails_{uid}.html")

    if status != 200:
        print(f"[x] 请求失败，返回开头：{html[:300]}")
        return False
    if nexus.looks_like_login(html):
        print("[x] cookie 失效或未登录。")
        print("    跑 python login.py 重新拿一次（会自动打开浏览器）。")
        return False

    info = nexus.parse_userdetails(html)
    print("[OK] 登录态有效")
    for k in ("username", "class", "uploaded", "downloaded", "seedbonus", "invites"):
        print(f"    {k:<12}: {info.get(k)}")
    missing = [k for k in ("username", "uploaded", "seedbonus") if not info.get(k)]
    if missing:
        print(f"    [!] 未解析出：{missing} -> 把 samples/userdetails_{uid}.html 发我适配")

    # ---------------- [2] 做种汇总 ----------------
    print()
    hr("[2/3] 做种汇总 —— 计薪依据在这里")
    status, html2, _ = sess.request(ep.path("seeding_list", uid=uid, page=1))
    (SAMPLES / f"seeding_{uid}_p1.html").write_text(html2, encoding="utf-8")
    print(f"HTTP {status}  |  {len(html2)} 字符  |  已存 samples/seeding_{uid}_p1.html")

    count = size = None
    if status != 200 or nexus.looks_like_login(html2):
        print("[x] 请求失败或登录态失效")
    else:
        count, size = nexus.parse_seeding_summary(html2)
        if count is not None or size is not None:
            print(f"[OK] 汇总行解析成功：{count} 条记录 | 总大小 {nexus.human_size(size)}")
            print(f"     （原始值：count={count}, size_bytes={size}）")
            print(f"     -> 数量考核可用：{count} 个；体积考核可用：{size} 字节")
        else:
            print("[!] 没找到汇总行，回退逐行累加……")
            rows = nexus.parse_seeding_rows(html2)
            if rows:
                total = sum(r["size_bytes"] for r in rows)
                print(f"[OK] 逐行累加：{len(rows)} 行，合计 {nexus.human_size(total)}")
                for r in rows[:5]:
                    print(f"       - {nexus.human_size(r['size_bytes']):>10}  {r['name'][:44]}")
                print("     [!] 若行数少于实际，说明还有下一页，汇总行也可能不在第 1 页")
            else:
                print("[x] 汇总行和逐行都没解析到。")
                print("    -> 把 samples/ 里的 seeding_*.html 发我")

        page_text = nexus.html_to_text(html2)
        print()
        print("     页面可见文本（前 300 字，人工核对格式用）：")
        print("     " + page_text[:300].replace("\n", "\n     "))

    # ---------------- [3] 赠送表单 ----------------
    print()
    hr("[3/3] 赠送表单 —— 后面发放要 POST 的地方")
    gift_ok = False
    if not ep.has("gift_bonus"):
        print("[!] config.json 里没配 gift_bonus，跳过。")
    else:
        status, html3, _ = sess.request(ep.path("gift_bonus"))
        (SAMPLES / "mybonus.html").write_text(html3, encoding="utf-8")
        print(f"HTTP {status}  |  {len(html3)} 字符  |  已存 samples/mybonus.html")

        if status == 200 and not nexus.looks_like_login(html3):
            forms = nexus.extract_forms(html3)
            print(f"[OK] 找到 {len(forms)} 个表单")
            gift_like = []
            for i, f in enumerate(forms, 1):
                names = [x.get("name", "") for x in f["fields"] if x.get("name")]
                has_user = any("user" in n.lower() or "name" in n.lower() for n in names)
                has_bonus = any(("bonus" in n.lower() or "karma" in n.lower()
                                 or "point" in n.lower() or "amount" in n.lower()
                                 or "gift" in n.lower()) for n in names)
                print()
                print(f"  -- 表单 #{i}  action={f['action']!r}  method={f['method']}")
                for fld in f["fields"]:
                    if fld["tag"] == "select":
                        opts = ", ".join(o["value"] for o in fld.get("options", [])[:8])
                        print(f"       select   name={fld['name']!r}  options=[{opts}]")
                    else:
                        line = (f"       {fld['tag']:<8} type={fld.get('type',''):<8} "
                                f"name={fld['name']!r}")
                        if fld.get("id"):
                            line += f"  id={fld['id']!r}"
                        if fld.get("value"):
                            line += f"  value={fld['value']!r}"
                        print(line)
                if has_user and has_bonus:
                    gift_like.append((i, f))
            print()
            if gift_like:
                i, f = gift_like[0]
                gift_ok = True
                print(f"[OK] 赠送表单 = 表单 #{i}，提交目标 {f['action']!r}")
                import payout as _payout
                spec, notes = _payout.suggest_gift_form(html3, ep.url("gift_bonus"), ep=ep)
                print()
                print("     把下面这段粘进 config.json 的 gift_form（覆盖原来的空对象）：")
                print()
                snippet = json.dumps(spec, ensure_ascii=False, indent=2)
                for line in snippet.splitlines():
                    print("       " + line)
                print()
                for n in notes:
                    print("     [!] " + n)
                print()
                print("     ★ 别忘了先跑一次 dry-run 确认清单：")
                print("       python payout.py --period %s" % datetime.now().strftime("%Y-%m"))
            else:
                print("[!] 没自动认出赠送表单。可能是 JS 动态加载或字段名特殊。")
                print("    -> 把 samples/mybonus.html 发我")
        else:
            print(f"[x] HTTP {status} 或登录态失效")

    print()
    hr()
    print("探测结论：")
    print("  - 登录态    : OK")
    print("  - 计薪依据  : " + ("OK" if (count is not None or size is not None) else "需适配"))
    print("  - 赠送表单  : " + ("OK" if gift_ok else "需适配 / 未配置"))
    print("  -> 把 samples/ 里的 html 发我，我按你站点的真实结构把解析规则钉死")
    return True


# ============================================================
# 离线自测
# ============================================================

def selftest():
    hr("离线自测：解析逻辑 + 站点适配层")
    fails = []

    def check(label, got, want):
        ok = got == want
        print(f"  [{'OK' if ok else 'XX'}] {label}: {got}" + ("" if ok else f"  (期望 {want})"))
        if not ok:
            fails.append(f"{label}: 期望 {want}，实际 {got}")

    def check_true(label, got):
        check(label, bool(got), True)

    print("\n-- 体积解析（含短写单位）--")
    check("51.510 TB", nexus.parse_size("51.510 TB"), int(51.510 * 1024 ** 4))
    check("512 G（短写）", nexus.parse_size("512 G"), 512 * 1024 ** 3)
    check("2.756 T（短写）", nexus.parse_size("2.756 T"), int(2.756 * 1024 ** 4))
    check("1024 M（短写）", nexus.parse_size("1024 M"), 1024 * 1024 ** 2)
    check("0.00 KB", nexus.parse_size("0.00 KB"), 0)
    check("0", nexus.parse_size("0"), 0)
    check("1,024.00 MB（千分位）", nexus.parse_size("1,024.00 MB"), int(1024 * 1024 ** 2))
    check("纯数字被 require_unit 拒绝", nexus.parse_size("2", require_unit=True), None)
    check("种子名不误判", nexus.parse_size("Some.Movie.2024.2160p.WEB-DL"), None)

    print("\n-- 做种汇总行（三种已知写法）--")
    c, s = nexus.parse_seeding_summary("4893 条记录 | 总大小：51.510 TB")
    check("站点格式 count", c, 4893)
    check("站点格式 size", s, int(51.510 * 1024 ** 4))
    c, s = nexus.parse_seeding_summary("<b>94</b>条记录，共计<b>2.756 TB</b>")
    check("带 <b> 的 count", c, 94)
    c, s = nexus.parse_seeding_summary("10 | 100 GB")
    check("管道符 count", c, 10)
    check("管道符 size", s, 100 * 1024 ** 3)

    print("\n-- 用户详情页 --")
    f = SAMPLE_RES / "userdetails.sample.html"
    if f.exists():
        info = nexus.parse_userdetails(f.read_text(encoding="utf-8"))
        check("用户名", info.get("username"), "TestSeeder")
        check("魔力值", info.get("seedbonus"), "123,456.78")
    else:
        fails.append("缺少 userdetails.sample.html")

    print("\n-- 做种列表逐行回退 --")
    f = SAMPLE_RES / "seeding.sample.html"
    if f.exists():
        rows = nexus.parse_seeding_rows(f.read_text(encoding="utf-8"))
        check("行数", len(rows), 3)
        check("合计体积", sum(r["size_bytes"] for r in rows), 8 * 1024 ** 3)
    else:
        fails.append("缺少 seeding.sample.html")

    print("\n-- 赠送表单提取 --")
    f = SAMPLE_RES / "mybonus.sample.html"
    if f.exists():
        html = f.read_text(encoding="utf-8")
        forms = nexus.extract_forms(html)
        check("表单数", len(forms), 1)
        if forms:
            names = [x.get("name") for x in forms[0]["fields"]]
            check("含 username", "username" in names, True)
            check("含 seedbonus", "seedbonus" in names, True)

        # 从真实页面直接生成可粘贴的 gift_form 片段（probe 的主输出之一）
        import payout as _payout
        spec, notes = _payout.suggest_gift_form(
            html, "https://pt.example.com/mybonus.php",
            origin="https://pt.example.com")
        check_true("能自动认出赠送表单", bool(spec))
        if spec:
            check("action 补全为路径", spec["action"], "/mybonus.php")
            check("收礼人字段", spec["fields"]["username"], "username")
            check("金额字段", spec["fields"]["amount"], "seedbonus")
            check("留言字段", spec["fields"]["message"], "message")
            check("success_url 预填实测默认值（换站要自己核）",
                  spec["success_url"], _payout.DEFAULT_SUCCESS_URL)
            check("重复提交落点也预填了",
                  spec["duplicate_url"], _payout.DUPLICATE_URL)
            check_true("提示里说清了要人工核对落点",
                       any("换站" in n for n in notes))
            check_true("不再有 success_marker / failure_markers 那套文字判据",
                       "success_marker" not in spec and
                       "failure_markers" not in spec)
            check_true("生成的片段是合法 JSON",
                       isinstance(json.loads(json.dumps(spec, ensure_ascii=False)), dict))
    else:
        fails.append("缺少 mybonus.sample.html")

    print("\n-- 登录态识别 --")
    check("登录页", nexus.looks_like_login(
        '<form action="takelogin.php"><input name="password"></form>'), True)
    ud = SAMPLE_RES / "userdetails.sample.html"
    check("正常页", nexus.looks_like_login(
        ud.read_text(encoding="utf-8") if ud.exists() else "<html>ok</html>"), False)

    print("\n-- 站点适配层：URL 拼接 --")
    ep = nexus.Endpoints("https://pt.example.com/nexusphp", None)
    check("相对 base_url（带子目录）", ep.url("gift_bonus"),
          "https://pt.example.com/nexusphp/mybonus.php")
    check("带 uid 占位", ep.url("user_details", uid=10001),
          "https://pt.example.com/nexusphp/userdetails.php?id=10001")
    check("带 page 占位", ep.url("seeding_list", uid=1, page=3),
          "https://pt.example.com/nexusphp/getusertorrentlistajax.php?userid=1&type=seeding&page=3")
    ep2 = nexus.Endpoints("https://pt.example.com", {"user_details": "/custom/u/{uid}"})
    check("斜杠开头走根域", ep2.url("user_details", uid=10005),
          "https://pt.example.com/custom/u/10005")
    ep3 = nexus.Endpoints("https://pt.example.com",
                          {"login": "https://sso.example.org/login"})
    check("绝对 URL 原样返回", ep3.url("login"), "https://sso.example.org/login")
    check("覆盖默认值", ep3.map["gift_bonus"], "mybonus.php")

    print("\n-- 站点适配层：没配的接口要报错，不能瞎猜 --")
    ep4 = nexus.Endpoints("https://pt.example.com", {"gift_bonus": ""})
    try:
        ep4.url("gift_bonus")
        check("未配置应抛错", "没抛", "KeyError")
    except KeyError as e:
        check("未配置抛 KeyError 且提示去处", "config.json" in str(e), True)

    print("\n-- cookie 解析 --")
    check("document.cookie 形式",
          nexus.parse_cookie_string("c_secure_uid=MTIz; c_secure_pass=abc"),
          "c_secure_uid=MTIz; c_secure_pass=abc")
    check("从 Headers 里扒 Cookie 行",
          nexus.parse_cookie_string(
              "GET /mybonus.php HTTP/1.1\nHost: x\nCookie: c_secure_uid=MTIz; c_secure_login=yes\n"),
          "c_secure_uid=MTIz; c_secure_login=yes")
    check("丢掉 Path/Expires 之类",
          nexus.parse_cookie_string(
              "c_secure_uid=MTIz; Path=/; Expires=Wed, 21 Oct 2026 07:28:00 GMT"),
          "c_secure_uid=MTIz")
    check("空输入", nexus.parse_cookie_string(""), "")

    print("\n-- uid 反解 --")
    import base64 as _b64
    check("base64 解 uid", nexus.decode_c_secure_uid(_b64.b64encode(b"10001").decode()), 10001)
    check("解不出返回 None", nexus.decode_c_secure_uid("!!!not-base64!!!"), None)
    check("空值", nexus.decode_c_secure_uid(""), None)
    check("从页面猜 uid",
          nexus.guess_uid_from_html(
              '<a href="userdetails.php?id=10001">me</a>'
              '<a href="userdetails.php?id=10001">again</a>'
              '<a href="userdetails.php?id=999">other</a>'), 10001)

    print("\n-- 从锚点认用户名（用户改名后页面永远最新）--")
    anchor_html = ('<a href="userdetails.php?id=10001"><b>新名字</b></a>'
                   '<a href="userdetails.php?id=9999">别人</a>')
    check("锚点文字就是用户名", nexus.guess_username_from_html(anchor_html, 10001), "新名字")
    check("指定 uid 的锚点也认", nexus.guess_username_from_html(anchor_html, 9999), "别人")
    check("uid 对不上 → None", nexus.guess_username_from_html(anchor_html, 12345), None)
    check("空页 → None", nexus.guess_username_from_html("", 10001), None)
    check("没 uid → None", nexus.guess_username_from_html(anchor_html, None), None)

    print("\n-- 锚点 href 的单引号 / 无引号写法也要认 --")
    check("单引号 href",
          nexus.guess_username_from_html(
              "<a href='userdetails.php?id=10001'>单引号</a>", 10001), "单引号")
    check("无引号 href",
          nexus.guess_username_from_html(
              '<a href=userdetails.php?id=10001>无引号</a>', 10001), "无引号")
    check("绝对地址也认",
          nexus.guess_username_from_html(
              '<a href="https://pt.example.com/userdetails.php?id=10001">绝对</a>',
              10001), "绝对")

    print("\n-- 锚点认不出时退到 <h1>（看别人的详情页就是这种）--")
    # 管理员逐人刷新时看的是**别人**的页：导航栏的名字链接指向管理员自己，
    # 页面上没有指向对方 uid 的锚点 —— 必须靠 <h1> 兜底
    h1_html = ('<div id="info"><h1>H1用户</h1></div>'
               '<a href="userdetails.php?id=1">管理员自己</a>')
    check("h1 就是用户名", nexus.guess_username_from_html(h1_html, 10001), "H1用户")
    check("锚点优先于 h1",
          nexus.guess_username_from_html(
              '<a href="userdetails.php?id=10001">锚点优先</a><h1>H1用户</h1>', 10001),
          "锚点优先")
    check("栏目标题不算用户名",
          nexus.guess_username_from_html("<h1>用户详情</h1>", 10001), None)
    check("超长 h1 不要",
          nexus.guess_username_from_html("<h1>" + "x" * 60 + "</h1>", 10001), None)
    check("h1 带嵌套标签剥干净",
          nexus.guess_username_from_html("<h1><span class=xc>嵌套名</span></h1>", 10001),
          "嵌套名")

    print("\n-- 配置文件（JSONC：带注释也要能读）--")
    ex = ROOT / nexus.CONFIG_EXAMPLE_NAME
    if ex.exists():
        import settle as _settle
        text = ex.read_text(encoding="utf-8")
        check_true("模板里确实有注释", "//" in text)
        # 关键：模板是 JSONC，必须走 read_jsonc 而不是 stdlib json（曾在这里炸过）
        try:
            cfg = nexus.read_jsonc(ex)
        except Exception as e:                       # noqa: BLE001
            cfg = None
            fails.append(f"{nexus.CONFIG_EXAMPLE_NAME} 读不出来：{e}")
        check_true("read_jsonc 能读带注释的模板", isinstance(cfg, dict))
        if isinstance(cfg, dict):
            st = _settle.validate_settings(cfg, str(ex))
            check("模板能通过校验", bool(st["plans"]), True)
            check("模板 metric", st["metrics"], ["volume"])
            check("模板里没有真 cookie", text.count('"cookie": ""'), 1)
            hosts = sorted({p.split("/")[0].split('"')[0].split("'")[0].strip()
                            for p in text.replace("http://", " https://")
                            .split("https://")[1:]})
            check("模板里只用示例域名", hosts, ["host", "pt.example.com"])
            check("gift_form 默认留空（未探测过就禁止真发）",
                  cfg.get("gift_form", {}).get("action"), "")
        check("resolve_config_path 认模板",
              nexus.resolve_config_path(None, ROOT)[0].name in
              (nexus.CONFIG_NAME, nexus.CONFIG_EXAMPLE_NAME), True)

        print("\n-- 写配置：注释一行都不能少 --")
        before = text.count("//")
        out = text
        out = nexus.patch_jsonc_value(out, "assessment.metrics", ["volume", "count"])
        out = nexus.patch_jsonc_value(out, "plans", [
            {"id": "3T", "min_volume_tb": 3, "min_count": 300, "salary": 200000},
            {"id": "5T", "min_volume_tb": 5, "min_count": 500, "salary": 300000},
        ])
        out = nexus.patch_jsonc_value(out, "payout.tax_rate", 0.95)
        out = nexus.patch_jsonc_value(out, "payout.tax_flat", False)
        out = nexus.patch_jsonc_value(out, "base_url", "https://other.example.org")
        out = nexus.patch_jsonc_value(out, "gift_form", {
            "action": "mybonus.php",
            "fields": {"username": "username", "amount": "seedbonus",
                       "message": "message"},
            "success_url": "do=transfer"})
        out = nexus.patch_jsonc_value(out, "payout.rounds_per_period", 3)
        check("注释行数不变", out.count("//"), before)
        check_true("写回来还是合法 JSONC", isinstance(nexus.loads_jsonc(out), dict))
        written = nexus.loads_jsonc(out)
        check("改数组（metrics）", written["assessment"]["metrics"], ["volume", "count"])
        check("改对象数组（plans）", [p["id"] for p in written["plans"]], ["3T", "5T"])
        check("改嵌套标量（payout.tax_rate）", written["payout"]["tax_rate"], 0.95)
        check("改布尔", written["payout"]["tax_flat"], False)
        check("改次数（rounds_per_period）", written["payout"]["rounds_per_period"], 3)
        check("改字符串", written["base_url"], "https://other.example.org")
        check("写整个 gift_form", written["gift_form"]["fields"]["amount"], "seedbonus")
        check_true("同层同键唯一，改的是顶层那个",
                   written["endpoints"]["user_details"] ==
                   "userdetails.php?id={uid}")
        # 键不存在 → 明确报错，绝不闷声改错地方
        try:
            nexus.patch_jsonc_value(text, "no_such_key", 1)
            check("不认识键要报错", "没报错", "KeyError")
        except KeyError:
            check("不认识键要报错", "KeyError", "KeyError")
        # 行尾注释不能被值替换吃掉
        probe_txt = '{\n  "uid": 0,   // 我的 uid\n  "x": 1\n}'
        patched = nexus.patch_jsonc_value(probe_txt, "uid", 10001)
        check_true("行尾注释没被吃掉", "// 我的 uid" in patched)
        check("行尾注释还在原位", nexus.loads_jsonc(patched)["uid"], 10001)
    else:
        fails.append(f"缺少 {nexus.CONFIG_EXAMPLE_NAME}")

    print()
    hr()
    if fails:
        print("自测失败：")
        for x in fails:
            print("  x", x)
        return False
    print("自测全部通过 ✅")
    return True


# ============================================================

def main():
    ap = argparse.ArgumentParser(description="站点接口探测")
    ap.add_argument("--selftest", action="store_true", help="离线自测解析逻辑，不访问网络")
    ap.add_argument("--config", default=str(ROOT / nexus.CONFIG_NAME), help="配置文件")
    ap.add_argument("--uid", type=int, default=None, help="覆盖配置里的 uid")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if selftest() else 1)

    try:
        ok = probe(args.config, args.uid)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"[x] {e}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
