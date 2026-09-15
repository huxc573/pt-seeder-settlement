#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cookie 获取向导 —— 纯 Python 标准库，零第三方依赖。

两条路：

    python login.py --password         # 账号密码登录（浏览器开着也能用，最省事）
    python login.py --paste            # 等剪贴板（DevTools 复制一次 Cookie 头）

其他：

    python login.py --verify           # 只验证 config.json 里现有的 cookie
    python login.py --no-save          # 验完不写盘
    python login.py --no-browser       # 不自动打开浏览器

不带参数 = 先账密（配置里存了就直登），不行再走剪贴板向导。
"""

import argparse
import re
import sys
import time
import webbrowser
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import clipboard
import nexus

ROOT = nexus.DATA_ROOT
PLACEHOLDER_HOSTS = ("example.com", "example.org")

# 顶层可写字段（patch_jsonc_field 要求全文件唯一，所以名字都带前缀避开嵌套同名字段）
PATCHABLE = ("cookie", "uid", "login_user", "login_password")


def hr(t=""):
    print("-" * 68)
    if t:
        print(t)
        print("-" * 68)


def cookie_problem(cookie):
    """
    检查粘贴来的 cookie 能不能用。返回 "" = 能用，否则给人话原因。

    **不按 cookie 名筛** —— 各站名字五花八门（老 NexusPHP 是 c_secure_*，
    新的是 nexusphp_session / XSRF-TOKEN，还有站点完全自定义）。
    用户从站点复制来的整行照收，只要里面真有「名字=值」对就行。
    """
    if not (cookie or "").strip():
        return "cookie 是空的"
    pairs = nexus.parse_cookie_string(cookie)
    if not pairs or "=" not in pairs:
        return ("没解析出任何 cookie（要复制 F12 → Network → Request Headers "
                "里 Cookie: 那一整行）")
    return ""


# ============================================================
# 配置读写（保注释）
# ============================================================

def load_or_seed(cfg_path):
    """config.json 不存在就从 config.example.json 复制一份（连注释一起）。"""
    p = Path(cfg_path)
    seeded = False
    if not p.exists():
        ex = p.with_name(nexus.CONFIG_EXAMPLE_NAME)
        p.write_text(ex.read_text(encoding="utf-8") if ex.exists() else "{}\n",
                     encoding="utf-8")
        seeded = True
    try:
        cfg = nexus.read_jsonc(p)
    except Exception as e:
        sys.exit(f"[x] {p} 读不出来：{e}")
    return cfg, p, seeded


def save_fields(path, updates):
    """
    只改指定字段的值，**其余文本原样保留** —— 注释不会丢。

    用文本级替换（nexus.patch_jsonc_field）而不是 json.loads→dumps，
    后者会把你的注释和排版全冲掉。
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    changed = []
    for k, v in updates.items():
        if v is None:
            continue
        try:
            new = nexus.patch_jsonc_field(text, k, v)
        except KeyError as e:
            # 老配置里没有这个键，就在第一个 } 前补一行
            print(f"    [!] {e}，跳过（可手工补上）")
            continue
        if new != text:
            changed.append(k)
        text = new
    p.write_text(text, encoding="utf-8")
    return p, changed


# ============================================================
# 校验
# ============================================================

def verify(cookie, base_url, ep, uid=None, timeout=25):
    """用 cookie 请求个人页，返回 (ok, uid, username, msg)。"""
    sess = nexus.Session(base_url, cookie, timeout=timeout)
    if uid:
        path = ep.path("user_details", uid=uid)
    else:
        path = ep.path("user_details", uid="").split("?")[0]

    status, html, final = sess.request(path)
    if status != 200:
        return False, uid, None, f"HTTP {status}（{final}）"
    if nexus.looks_like_login(html):
        return False, uid, None, "被跳回登录页 —— cookie 无效或已过期"

    info = nexus.parse_userdetails(html)
    username = info.get("username")

    h2 = None
    if not uid:
        uid = nexus.guess_uid_from_html(html)
        if uid:
            s2, h2, _ = sess.request(ep.path("user_details", uid=uid))
            if s2 == 200 and not nexus.looks_like_login(h2):
                info2 = nexus.parse_userdetails(h2)
                if info2.get("username"):
                    username = info2["username"]

    # rowhead/别名都认不出用户名时（模板不写「用户名」标签），退回
    # 「指向自己详情页的链接文字」—— 用户改名后页面永远是最新的
    if not username and uid:
        username = nexus.guess_username_from_html(html, uid) or \
            nexus.guess_username_from_html(h2, uid)
    return True, uid, username, "OK"


def cookies_from_jar(jar, names):
    """从 cookiejar 里挑出目标 cookie，拼成字符串。"""
    want = set(names)
    got = {}
    for c in jar:
        if c.name in want and c.value:
            got[c.name] = c.value
    return "; ".join(f"{k}={got[k]}" for k in sorted(got))


# ============================================================
# 取 cookie：① 账号密码登录
# ============================================================

def _guess_login_form(sess, ep, login_form):
    """
    返回 (action, 用户名字段, 密码字段)。
    优先用配置；配置没有就去抓登录页，从真实表单里认字段名。
    """
    lf = login_form or {}
    fields = dict(lf.get("fields") or {})
    action = lf.get("action") or ""
    fu = fields.get("username") or ""
    fp = fields.get("password") or ""

    if action and fu and fp:
        return action, fu, fp

    try:
        status, html, _ = sess.get(ep.path("login"))
    except Exception:
        html, status = "", 0

    if status == 200 and html:
        for f in nexus.extract_forms(html):
            ftypes = {x.get("type") for x in f["fields"]}
            fnames = {x.get("name") for x in f["fields"]}
            # 认「密码框」要看 type，不能看字段名 ——
            # 站点完全可能把它叫 pwd_field / userpass 之类。
            if "password" not in ftypes and "password" not in fnames:
                continue
            action = action or f.get("action") or ""
            for x in f["fields"]:
                n = x.get("name") or ""
                if not n:
                    continue
                if x.get("type") == "password" and not fp:
                    fp = n
                elif x.get("type") in ("text", "email") and not fu:
                    fu = n
            break

    action = action or nexus.login_form_path(ep)
    return action, fu or "username", fp or "password"


def password_login(base_url, ep, username, password, login_form=None, timeout=25):
    """
    直接 POST 登录表单拿 cookie。

    返回 (cookie_string, msg)。cookie_string 非空即成功。
    这条路**不需要接触浏览器**，所以浏览器开着也照样能用。
    """
    sess = nexus.Session(base_url, "", timeout=timeout)
    action, fu, fp = _guess_login_form(sess, ep, login_form)

    data = {fu: username, fp: password}
    status, html, final = sess.post(action, data, referer=ep.url("login") if ep.has("login") else base_url)

    names = nexus.cookie_names_of({})
    cookie = cookies_from_jar(sess.jar, names)
    if not cookie:
        # 有些版本 cookie 名不完全一样，退一步拿全部
        cookie = cookies_from_jar(sess.jar, [c.name for c in sess.jar])

    if not cookie:
        if status == 0:
            return "", f"请求失败：{html[:120]}"
        if nexus.looks_like_login(html):
            return "", ("登录被拒（多半是账号或密码不对），"
                        f"提交到 {action}，HTTP {status}")
        return "", f"登录后没拿到 cookie（提交到 {action}，HTTP {status}）"
    return cookie, f"登录成功（提交到 {action}，字段 {fu}/{fp}）"


# ============================================================
# 取 cookie：② 剪贴板
# ============================================================

def clipboard_cookie_once():
    """
    秒读**一次**剪贴板（不重试、不开浏览器）。给 GUI 用 —— 点了按钮
    就要立刻有反应，绝不能让用户等好几秒才看到浏览器弹出来。

    返回 (cookie, msg)。cookie 为空表示剪贴板里现在没有可用的 cookie。
    """
    if not clipboard.clipboard_supported():
        return "", "这个系统上没有可用的剪贴板接口（Linux 需要 xclip 或 xsel）"
    try:
        txt = clipboard.read_clipboard() or ""
    except Exception:
        txt = ""
    if not txt.strip():
        return "", "剪贴板是空的 —— 先去浏览器复制 Cookie: 那一整行"
    cand = nexus.parse_cookie_string(txt)
    if not cand:
        return "", "剪贴板内容认不出 cookie（要 F12 → Network 里 Cookie: 那一整行）"
    return cand, "从剪贴板读到 cookie"


def from_clipboard_wizard(names, timeout):
    if not clipboard.clipboard_supported():
        print("[!] 这个系统上没有可用的剪贴板接口（Linux 需要 xclip 或 xsel）。")
        return ""

    print()
    hr("请在浏览器里完成下面 4 步")
    print("  1. 登录站点")
    print("  2. 按 F12 打开开发者工具，切到 Network（网络）标签")
    print("  3. 按 F5 刷新页面，点左侧任意一个请求")
    print("  4. 右侧找到 Request Headers（请求标头），把 Cookie: 那一行的值整段复制")
    print()
    print("  · 整个 Headers 区块粘过来也行，脚本自己找 Cookie 行")
    print("  · 必须从 Network 复制 —— 那里才包含 HttpOnly 的 cookie")
    print()
    print(f"  等剪贴板内容……（最多 {timeout} 秒，Ctrl+C 可退出）")

    deadline = time.time() + timeout
    tick = 0
    while time.time() < deadline:
        try:
            txt = clipboard.read_clipboard()
        except Exception:
            txt = ""
        if txt:
            cand = nexus.parse_cookie_string(txt)
            if cand:
                print()
                print("  [OK] 从剪贴板抓到了 cookie")
                return cand
        tick += 1
        if tick % 10 == 0:
            print(f"       …还在等（剩 {int(deadline - time.time())} 秒）")
        time.sleep(1)
    print()
    print("[x] 超时，没等到剪贴板里有 cookie。")
    return ""


def _ask(prompt, options):
    """有终端才问；非交互环境直接选默认（第一个）。"""
    if not sys.stdin or not sys.stdin.isatty():
        return options[0][0]
    print()
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}. {label}")
    while True:
        try:
            raw = input(f"{prompt} [1-{len(options)}，回车=1] ").strip()
        except (EOFError, KeyboardInterrupt):
            return options[0][0]
        if not raw:
            return options[0][0]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print("  输入不对，再来一次。")


# ============================================================

def main():
    ap = argparse.ArgumentParser(
        description="PT 保种组工具 · cookie 向导（零依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="不带参数 = 先账密（配置里存了就直登），不行再走剪贴板向导。")
    ap.add_argument("--config", default=str(ROOT / nexus.CONFIG_NAME))
    ap.add_argument("--password", action="store_true", help="走账号密码登录")
    ap.add_argument("--user", default=None, help="账号（配合 --password，不写就交互输入）")
    ap.add_argument("--save-password", action="store_true",
                    help="把账号密码写进 config.json（明文，config.json 不会提交）")
    ap.add_argument("--paste", action="store_true", help="等剪贴板")
    ap.add_argument("--verify", action="store_true", help="只验证现有 cookie")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--no-save", action="store_true", help="只验证，不写配置")
    ap.add_argument("--timeout", type=int, default=180, help="等剪贴板的秒数")
    args = ap.parse_args()

    cfg, cfg_path, seeded = load_or_seed(args.config)

    base_url = (cfg.get("base_url") or "").strip()
    if not base_url:
        sys.exit(f"[x] {cfg_path} 里没写 base_url。填上站点地址再来。")
    if any(h in base_url for h in PLACEHOLDER_HOSTS):
        sys.exit(f"[x] base_url 还是模板里的占位地址 {base_url}\n"
                 f"    把它改成你的真实站点地址再跑。")

    ep = nexus.Endpoints(base_url, cfg.get("endpoints"))
    names = nexus.cookie_names_of(cfg)

    print("=" * 68)
    print("PT 保种组工具 · cookie 向导")
    print("=" * 68)
    print(f"配置     : {cfg_path}" + ("   （已从模板新建）" if seeded else ""))
    print(f"站点地址 : {base_url}")
    print(f"cookie 名: {', '.join(names)}")

    # ---------- 只验证 ----------
    if args.verify:
        cookie = cfg.get("cookie", "")
        if not cookie:
            sys.exit(f"[x] {cfg_path} 里没有 cookie")
        ok, uid, uname, msg = verify(cookie, base_url, ep, cfg.get("uid") or None)
        print()
        print(("[OK] " if ok else "[x] ") + msg)
        if ok:
            print(f"     uid={uid}  用户名={uname}")
            print(f"     当前剩余有效期以站点为准，失效后重跑 python login.py")
        sys.exit(0 if ok else 1)

    cookie = ""
    source = ""

    # ---------- ① 账号密码 ----------
    if args.password or args.paste:
        pass
    else:
        cfg_user = cfg.get("login_user") or ""
        cfg_pass = cfg.get("login_password") or ""
        if cfg_user and cfg_pass:
            args.password = True          # 配置里存过账密，直登
        else:
            choice = _ask("下一步怎么取 cookie？", [
                ("password", "用账号密码登录（推荐，不用管浏览器）"),
                ("clipboard", "从剪贴板粘贴 cookie"),
            ])
            if choice == "password":
                args.password = True
            else:
                args.paste = True

    # ---------- ② 账号密码 ----------
    if not cookie and args.password:
        user = args.user or cfg.get("login_user") or ""
        pwd = cfg.get("login_password") or ""
        if not user and sys.stdin and sys.stdin.isatty():
            try:
                user = input("  账号: ").strip()
            except (EOFError, KeyboardInterrupt):
                user = ""
        if not pwd and sys.stdin and sys.stdin.isatty():
            try:
                import getpass
                pwd = getpass.getpass("  密码（输入时不显示）: ")
            except (EOFError, KeyboardInterrupt, Exception):
                pwd = ""
        if user and pwd:
            print()
            print(f"用账号密码登录……（账号 {user}）")
            cookie, msg = password_login(base_url, ep, user, pwd,
                                        login_form=cfg.get("login_form"),
                                        timeout=float(cfg.get("timeout_seconds", 25)))
            print(("  [OK] " if cookie else "  [x] ") + msg)
            if cookie:
                source = "账号密码"
        else:
            print("  [x] 没给账号或密码，跳过")

    # ---------- ② 剪贴板 ----------
    if not cookie and args.paste:
        if not args.no_browser:
            login_url = ep.url("login") if ep.has("login") else base_url
            print()
            print(f"正在打开默认浏览器 → {login_url}")
            try:
                webbrowser.open(login_url)
            except Exception as e:
                print(f"[!] 打开浏览器失败（{e}），手动访问上面的地址")
        cookie = from_clipboard_wizard(names, args.timeout)
        if cookie:
            source = "剪贴板"

    if not cookie:
        print()
        print("[x] 三条路都没拿到 cookie。")
        print("    最省事的顺序：① 用账号密码：python login.py --password")
        print("                  ② 复制一次：  python login.py --paste")
        sys.exit(1)

    # ---------- 验证 ----------
    print()
    hr("验证 cookie")
    print(f"  来源   : {source}")
    print(f"  长度   : {len(cookie)} 字符")
    print(f"  预览   : {cookie[:60]}{'…' if len(cookie) > 60 else ''}")

    m = re.search(r"c_secure_uid=([^;]+)", cookie)
    uid_hint = nexus.decode_c_secure_uid(m.group(1)) if m else None
    if uid_hint:
        print(f"  c_secure_uid 解出 uid = {uid_hint}")

    ok, uid, uname, msg = verify(cookie, base_url, ep, uid_hint or cfg.get("uid") or None)
    if not ok:
        print()
        print(f"[x] 验证失败：{msg}")
        print("    cookie 可能不完整或已过期，重跑一次向导。")
        sys.exit(1)

    print()
    print("[OK] cookie 有效")
    print(f"     uid    = {uid}")
    print(f"     用户名 = {uname}")

    # ---------- 落盘 ----------
    if args.no_save:
        print()
        print("（--no-save，没有写盘）")
        sys.exit(0)

    updates = {"cookie": cookie}
    if uid:
        updates["uid"] = uid
    if args.password:
        user = args.user or cfg.get("login_user") or ""
        if user:
            updates["login_user"] = user

    try:
        p, changed = save_fields(cfg_path, updates)
    except Exception as e:
        sys.exit(f"[x] 写配置失败：{e}")

    print()
    hr("已写入")
    print(f"  {p}")
    print(f"  改动字段：{', '.join(changed) or '（无变化）'}")
    print("  注释和排版原样保留。")
    print()
    print("接下来：")
    print("  python probe.py                 # 确认接口结构（只读）")
    print("  python settle.py                # 出工资表（离线）")
    print("  python gui.py                   # 打开图形界面")
    sys.exit(0)


if __name__ == "__main__":
    main()
