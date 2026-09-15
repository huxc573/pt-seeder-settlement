#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NexusPHP 通用工具库 —— HTTP 会话 + 页面解析

纯 Python 标准库，零第三方依赖。
拷到任何装了 Python 3.8+ 的机器直接能用。

关键事实（来自 NexusPHP 源码 + sunerpy/pt-tools 参考实现）：

1. 体积用 mksize() / mksize_compact() 格式化
   - 除数一律 1024，进位阈值 1000×1024^n
   - 后缀可能是全称或短写：KB/MB/GB/TB/PB 或 K/M/G/T/P
     正则：[KMGTP]?i?B?   ← B 可选，这就是会出现 "51.51 G" 的原因
   - 字节为 0 时输出 "0.00 KB"；页面也可能直接显示 "0"

2. 做种汇总行有至少三种写法
   - "4893 条记录 | 总大小：51.510 TB"
   - "94条记录，共计2.756 TB"
   - "10 | 100 GB"
   解析不到汇总时，回退到逐行累加体积

3. 用户详情页的字段用 <td class="rowhead">标签</td><td class="rowfollow">值</td> 配对
"""

import base64
import http.cookiejar
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from html import unescape as _html_unescape
from html.parser import HTMLParser
from pathlib import Path

__all__ = [
    "Session", "Endpoints", "load_config", "resolve_config_path",
    "parse_size", "human_size",
    "parse_seeding_summary", "parse_seeding_rows", "parse_userdetails",
    "extract_forms", "looks_like_login", "html_to_text",
    "parse_cookie_string", "decode_c_secure_uid", "guess_uid_from_html",
    "DEFAULT_ENDPOINTS", "DEFAULT_COOKIE_NAMES",
    "strip_json_comments", "loads_jsonc", "read_jsonc", "write_jsonc",
    "patch_jsonc_field", "login_form_path",
]

CONFIG_NAME = "config.json"
CONFIG_EXAMPLE_NAME = "config.example.json"

# 打包（PyInstaller）后的两个根，开发态二者相同：
#   RES_ROOT = 只读资源（VERSION / samples / 模板）→ 解包目录 _MEIPASS
#   DATA_ROOT = 用户数据（config.json / roster.csv / 台账 / 输出）→ exe 旁边
FROZEN = bool(getattr(sys, "frozen", False))
RES_ROOT = Path(getattr(sys, "_MEIPASS", "")) if FROZEN \
    else Path(__file__).resolve().parent
DATA_ROOT = Path(sys.executable).resolve().parent if FROZEN else RES_ROOT

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ============================================================
# 体积解析
# ============================================================

# NexusPHP 的单位后缀一律大写，所以这里**故意不加 re.I**。
# 加了 re.I 会把 "2160p" 里的 p 当成 PB，把种子名解析成一个天文数字。
# 末尾 (?![A-Za-z]) 保证单位后面不再跟字母。
_SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)*)\s*([KMGTP](?:i?B)?|B)(?![A-Za-z])")
_BARE_RE = re.compile(r"(\d+(?:\.\d+)?)")

_UNIT_FACTOR = {
    "": 1, "B": 1,
    "K": 1024, "KB": 1024, "KIB": 1024,
    "M": 1024 ** 2, "MB": 1024 ** 2, "MIB": 1024 ** 2,
    "G": 1024 ** 3, "GB": 1024 ** 3, "GIB": 1024 ** 3,
    "T": 1024 ** 4, "TB": 1024 ** 4, "TIB": 1024 ** 4,
    "P": 1024 ** 5, "PB": 1024 ** 5, "PIB": 1024 ** 5,
}

_HUMAN_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]


def parse_size(text, require_unit=False):
    """
    把 NexusPHP 的体积文本解析成字节数（int）。

    >>> parse_size("51.510 TB")
    56635843946741
    >>> parse_size("512 G")          # 短写，B 省略（NexusPHP 确实会这样输出）
    549755813888
    >>> parse_size("0.00 KB")
    0
    >>> parse_size("0")
    0
    >>> parse_size("2", require_unit=True) is None
    True
    >>> parse_size("Some.Movie.2024.2160p.WEB-DL") is None
    True

    require_unit=True 时后缀必须非空 —— 用于从表格行里挑体积列，
    避免把「做种人数」这种纯数字误当成体积。
    """
    if not text:
        return None
    s = text.replace(",", "").replace("\u00a0", " ").replace("\u3000", " ").strip()

    m = _SIZE_RE.search(s)
    if m:
        unit = m.group(2).upper()
        return int(float(m.group(1)) * _UNIT_FACTOR.get(unit, 1))

    if require_unit:
        return None

    # 无单位兜底：只接受「去掉前导标签后整串就是一个数字」，如 "0" / "总大小：0"
    cand = re.sub(r"^[^\d]*", "", s).strip()
    m = _BARE_RE.fullmatch(cand)
    if m:
        return int(float(m.group(1)))
    return None


def human_size(n):
    """字节数转人类可读（二进制单位）。"""
    if n is None:
        return "-"
    f = float(n)
    i = 0
    while f >= 1024 and i < len(_HUMAN_UNITS) - 1:
        f /= 1024
        i += 1
    return f"{f:.2f} {_HUMAN_UNITS[i]}"


# ============================================================
# HTML → 文本
# ============================================================

_BLOCK_TAGS = {"br", "tr", "div", "p", "li", "table", "h1", "h2", "h3"}
_CELL_TAGS = {"td", "th"}


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS or tag in _CELL_TAGS:
            self._parts.append("\n" if tag in _BLOCK_TAGS else "\t")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        self._parts.append(data)

    def text(self):
        return "".join(self._parts)


def html_to_text(html):
    """HTML 转纯文本；<br> 当换行，<td> 当制表符。"""
    if not html:
        return ""
    p = _TextParser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    raw = p.text()
    lines = []
    for line in raw.split("\n"):
        line = " ".join(line.replace("\t", " ").split())
        if line:
            lines.append(line)
    return "\n".join(lines)


# ============================================================
# 做种汇总行解析
# ============================================================

_COUNT_RE = re.compile(r"(\d[\d,]*)\s*条记录")

# 一个「体积 token」：优先带单位（全称或短写），否则退化为纯数字
_SIZE_TOKEN = r"([\d.,]+\s*[KMGTP](?:i?B)?|[\d.,]+\s*B|[\d.,]+)"

_SIZE_PATTERNS = [
    re.compile(r"总大小\s*[:：]?\s*" + _SIZE_TOKEN),
    re.compile(r"共计\s*[:：]?\s*" + _SIZE_TOKEN),
    re.compile(r"总计\s*[:：]?\s*" + _SIZE_TOKEN),
    re.compile(r"合计\s*[:：]?\s*" + _SIZE_TOKEN),
]


def parse_seeding_summary(html_or_text):
    """
    从做种列表页里解析汇总行，返回 (做种数量, 总字节数)。
    任一项解析不到就是 None。

    覆盖三种已知写法：
      "4893 条记录 | 总大小：51.510 TB"
      "94条记录，共计2.756 TB"          （数字可能带 <b> 标签）
      "10 | 100 GB"
    """
    if not html_or_text:
        return None, None

    # 带标签的原文里，数字可能被 <b> 包着，先做一次宽松清理
    text = html_to_text(html_or_text)

    count = None
    size = None

    # --- 数量 ---
    m = _COUNT_RE.search(text)
    if m:
        count = int(m.group(1).replace(",", ""))

    # --- 体积 ---
    for pat in _SIZE_PATTERNS:
        m = pat.search(text)
        if m:
            size = parse_size(m.group(1))
            if size is not None:
                break

    # --- 管道符写法兜底："N | X" 或 "N 条记录 | 总大小：X" ---
    if count is None or size is None:
        for line in text.split("\n"):
            if "|" not in line:
                continue
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) < 2:
                continue
            if count is None:
                mc = re.search(r"(\d[\d,]*)", parts[0])
                if mc:
                    count = int(mc.group(1).replace(",", ""))
            if size is None:
                sc = None
                for p in parts[1:]:
                    sc = parse_size(p)
                    if sc is not None:
                        break
                if sc is not None:
                    size = sc
            if count is not None and size is not None:
                break

    return count, size


# ============================================================
# 个人详情页里的做种汇总（详情页本身就有「N 条记录 | 总大小」，
# 不需要再去抓做种列表接口）
# ============================================================

_SUMMARY_WORDS = ("总大小", "共计", "总计", "合计")


# 站点自己说「没有记录」= 真的 0 条，不是抓取失败。两者必须分清：
# 前者是准确的实测值（0 必然不达标），后者是没拿到数据（未测，数据列留空）。
# 0 做种的账号，做种列表接口会原样回「没有记录」四个字。
_NO_RECORD_RE = re.compile(r"(?:没有|沒有|无|無|暂无|暫無)[^\n。；;]{0,8}(?:记录|記錄)")


def says_no_record(text):
    """响应里是不是明确写着「没有记录」（没有记录 / 没有做种记录 / 暂无记录……）。"""
    if not text:
        return False
    return bool(_NO_RECORD_RE.search(html_to_text(text) or text))


def parse_userdetails_seeding(html):
    """
    从个人详情页解析做种汇总，返回 (做种数量, 总字节数)。

    **只在含「条记录」或「总大小/共计/总计/合计」的行上找，绝不整页扫** ——
    详情页满页都是体积数字（上传量、下载量、分享率……），整页扫必错。
    """
    if not html:
        return None, None
    text = html_to_text(html)
    if not text:
        return None, None
    lines = text.split("\n")

    candidates = []                       # (行号, count, size)
    for i, line in enumerate(lines):
        if "条记录" not in line and not any(w in line for w in _SUMMARY_WORDS):
            continue
        # 体积可能和数量在同一段，也可能被模板折到相邻行 → 连着下一行一起看
        window = line + "\n" + (lines[i + 1] if i + 1 < len(lines) else "")
        count = size = None
        m = _COUNT_RE.search(window)
        if m:
            count = int(m.group(1).replace(",", ""))
        for pat in _SIZE_PATTERNS:
            m2 = pat.search(window)
            if m2:
                size = parse_size(m2.group(1))
                if size is not None:
                    break
        if count is not None or size is not None:
            candidates.append((i, count, size))
            if count is not None and size is not None:
                break                     # 数量和体积都齐了，就是这行

    if not candidates:
        return None, None
    # 优先挑数量+体积都全的；都只有一半时取第一个
    both = [c for c in candidates if c[1] is not None and c[2] is not None]
    _, count, size = (both or candidates)[0]
    return count, size


# ============================================================
# 做种列表逐行解析（汇总行解析不到时的回退）
# ============================================================

class _RowParser(HTMLParser):
    """把 HTML 里的表格行抽成 [[cell, cell, ...], ...]"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            if self._row is None:
                self._row = []
        elif tag in ("td", "th"):
            if self._row is not None and self._cell is None:
                self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            self._cell = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


_HEADER_WORDS = {"大小", "size", "名称", "name", "种子", "类型", "type", "标题", "title"}


def parse_seeding_rows(html):
    """
    逐行解析做种列表，返回 [{"name":..., "size_bytes":..., "cells":[...]}, ...]

    不写死列索引：每行从左往右找第一个"带单位的体积"单元格当种子大小。
    这样即使站点调整了列顺序也不容易崩。
    """
    if not html:
        return []
    p = _RowParser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        return []

    out = []
    for cells in p.rows:
        if len(cells) < 3:
            continue
        # 跳过表头
        if any(c.strip().lower() in _HEADER_WORDS for c in cells[:3]):
            continue
        size = None
        for c in cells:
            s = parse_size(c, require_unit=True)
            if s is not None:
                size = s
                break
        if size is None:
            continue
        name = max(cells, key=len) if cells else ""
        out.append({"name": name, "size_bytes": size, "cells": cells})
    return out


# ============================================================
# 用户详情页解析
# ============================================================

class _KVRowParser(HTMLParser):
    """提取 <td class="rowhead">标签</td><td class="rowfollow">值</td> 配对"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pairs = []
        self._row = None
        self._cell = None
        self._cell_is_head = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            if self._row is None:
                self._row = []
        elif tag == "td":
            if self._row is not None and self._cell is None:
                a = dict(attrs)
                cls = (a.get("class") or "")
                self._cell_is_head = "rowhead" in cls
                self._cell = []

    def handle_endtag(self, tag):
        if tag == "tr" and self._row is not None:
            self._flush()
            self._row = None
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._row.append((" ".join("".join(self._cell).split()), self._cell_is_head))
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def _flush(self):
        if not self._row:
            return
        for i, (text, is_head) in enumerate(self._row):
            if is_head and text:
                for text2, is_head2 in self._row[i + 1:]:
                    if not is_head2 and text2:
                        self.pairs.append((text, text2))
                        break


FIELD_ALIASES = {
    "username": ["用户名", "使用者名稱", "username", "user name"],
    "uploaded": ["上传量", "上傳量", "uploaded"],
    "downloaded": ["下载量", "下載量", "downloaded"],
    "ratio": ["分享率", "share ratio", "ratio"],
    "seedbonus": ["魔力值", "魔力", "karma points", "bonus"],
    "invites": ["邀请", "邀請", "invites"],
    "class": ["等级", "等級", "class"],
    "joined": ["加入日期", "注册日期", "join date", "joined"],
    "last_access": ["最近动向", "上次访问", "上次訪問", "last access"],
}


def parse_userdetails(html):
    """
    宽松解析用户详情页，返回 {字段: 文本值}。
    既走 rowhead/rowfollow 配对，也退化到整页文本正则扫描。
    """
    out = {}
    if not html:
        return out

    p = _KVRowParser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        p = None

    pairs = dict(p.pairs) if p else {}
    if pairs:
        out["_pairs"] = pairs

    text = html_to_text(html)
    for field, aliases in FIELD_ALIASES.items():
        value = None
        for alias in aliases:
            for k, v in pairs.items():
                if alias.lower() in k.lower():
                    value = v
                    break
            if value:
                break
        if value is None:
            for alias in aliases:
                m = re.search(re.escape(alias) + r"\s*[:：]\s*([^\n]+)", text)
                if m:
                    value = m.group(1).strip()
                    break
        if value:
            out[field] = value
    return out


# ============================================================
# 表单提取（用于探测 /mybonus.php 的赠送表单）
# ============================================================

class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self._cur = None
        self._sel = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur = {
                "action": a.get("action", ""),
                "method": (a.get("method") or "get").lower(),
                "name": a.get("name", ""),
                "id": a.get("id", ""),
                "fields": [],
            }
        elif tag == "input" and self._cur is not None:
            self._cur["fields"].append({
                "tag": "input",
                "type": (a.get("type") or "text").lower(),
                "name": a.get("name", ""),
                "id": a.get("id", ""),
                "value": a.get("value", ""),
                "checked": "checked" in a,
            })
        elif tag == "select" and self._cur is not None:
            self._sel = {"tag": "select", "name": a.get("name", ""), "id": a.get("id", ""), "options": []}
        elif tag == "option" and self._sel is not None:
            self._sel["options"].append({"value": a.get("value", ""), "selected": "selected" in a})
        elif tag == "textarea" and self._cur is not None:
            self._cur["fields"].append({
                "tag": "textarea", "name": a.get("name", ""), "id": a.get("id", ""),
            })

    def handle_endtag(self, tag):
        if tag == "form" and self._cur is not None:
            self.forms.append(self._cur)
            self._cur = None
        elif tag == "select" and self._sel is not None and self._cur is not None:
            self._cur["fields"].append(self._sel)
            self._sel = None


def extract_forms(html):
    """提取页面里所有表单的 action / method / 字段清单。"""
    if not html:
        return []
    p = _FormParser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        return []
    return p.forms


# ============================================================
# 登录态判断
# ============================================================

_LOGIN_HINTS = ("takelogin.php", "请登录", "請登錄", "请先登录", "用户名或密码错误", "登陆", "登錄")


def looks_like_login(html):
    """粗判返回的是不是登录页（cookie 失效）。"""
    if not html:
        return True
    low = html.lower()
    if "takelogin.php" in low:
        return True
    if "login.php" in low and 'name="password"' in low:
        return True
    return any(k in html for k in _LOGIN_HINTS)


# ============================================================
# HTTP 会话（urllib + cookiejar）
# ============================================================

def _seed_jar(jar, base_url, cookie_string):
    parts = urllib.parse.urlsplit(base_url)
    host = parts.hostname or ""
    secure = parts.scheme == "https"
    for item in cookie_string.split(";"):
        item = item.strip()
        if not item:
            continue
        name, sep, value = item.partition("=")
        if not sep:
            continue
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=name.strip(), value=value.strip(),
            port=None, port_specified=False,
            domain=host, domain_specified=False, domain_initial_dot=False,
            path="/", path_specified=True,
            secure=secure, expires=None, discard=True,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        ))


class Session:
    """带 cookie 的 HTTP 会话。cookie 直接从浏览器复制粘贴即可。"""

    def __init__(self, base_url, cookie="", timeout=25, user_agent=DEFAULT_UA):
        self.base = str(base_url).rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent
        self.jar = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx),
        )
        if cookie:
            _seed_jar(self.jar, self.base, cookie)

    def request(self, path, params=None, data=None, referer=None):
        """返回 (status_code, body_text, final_url)。"""
        url = path if path.startswith(("http://", "https://")) else self.base + "/" + path.lstrip("/")
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

        body = None
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": referer or (self.base + "/"),
        }
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, _decode(raw, resp.headers.get_content_charset()), resp.geturl()
        except urllib.error.HTTPError as e:
            raw = e.read() if e.fp else b""
            return e.code, _decode(raw, None), url
        except Exception as e:
            return 0, f"__EXCEPTION__ {type(e).__name__}: {e}", url

    def get(self, path, params=None, referer=None):
        return self.request(path, params=params, referer=referer)

    def post(self, path, data, referer=None):
        return self.request(path, data=data, referer=referer)


def _decode(raw, charset=None):
    for enc in (charset, "utf-8", "gb18030", "latin-1"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


# ============================================================
# 站点适配层 —— 每个站点的路径都不一样，全部可配置
# ============================================================

# 经典 NexusPHP 的默认路径（相对站点根目录）。
# 各站的二次开发版本常有差异，所以 config.json 里的 endpoints 会覆盖这里。
DEFAULT_ENDPOINTS = {
    "login":        "login.php",
    "user_details": "userdetails.php?id={uid}",
    "seeding_list": "getusertorrentlistajax.php?userid={uid}&type=seeding&page={page}",
    "gift_bonus":   "mybonus.php",
}

DEFAULT_COOKIE_NAMES = ["c_secure_uid", "c_secure_pass", "c_secure_login"]


class Endpoints:
    """
    把「站点地址 + 路径模板」拼成可用的 URL。

    模板里可用占位符：{uid} {page}
    - 以 http(s):// 开头 → 原样返回
    - 以 / 开头        → 相对**站点根域**（适用于后台与主站不同目录的情况）
    - 其他            → 相对 base_url（base_url 可以带子目录，如 https://host/nexusphp）
    """

    def __init__(self, base_url, endpoints=None):
        self.base = str(base_url or "").rstrip("/")
        parts = urllib.parse.urlsplit(self.base)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.map = dict(DEFAULT_ENDPOINTS)
        for k, v in (endpoints or {}).items():
            if v is not None:
                self.map[k] = v

    def has(self, key):
        return bool(self.map.get(key))

    def path(self, key, **kw):
        tpl = self.map.get(key)
        if not tpl:
            raise KeyError(
                f"配置里没有接口 {key!r}。在 config.json 的 endpoints 里补上"
                f"（各站路径不一样，没有通用默认值）")
        vals = {"uid": "", "page": 1}
        vals.update({k: v for k, v in kw.items() if v is not None})
        return tpl.format(**vals)

    def url(self, key, **kw):
        p = self.path(key, **kw)
        if p.startswith(("http://", "https://")):
            return p
        if p.startswith("/"):
            return self.origin + p
        return self.base + "/" + p

    def get(self, session, key, params=None, **kw):
        """按接口名发 GET（模板已带参数时 params 传 None）。"""
        return session.get(self.path(key, **kw), params=params)


# ============================================================
# cookie 字符串解析
# ============================================================

_COOKIE_PAIR_RE = re.compile(r"([A-Za-z0-9_\-\.]+)\s*=\s*([^;]*)")
# 从整段 DevTools 请求头里扒出 Cookie 行
_COOKIE_HEADER_RE = re.compile(r"^\s*cookie\s*:\s*(.+)$", re.I | re.M)


def parse_cookie_string(text, cookie_names=None):
    """
    从任意文本里解析出 cookie 串。

    能吃下三种输入：
      1. `document.cookie` 那种 "a=1; b=2"
      2. DevTools 请求头整段粘贴（自动找 Cookie: 那一行）
      3. 只写了 c_secure_uid / c_secure_pass / c_secure_login 三行

    >>> parse_cookie_string("c_secure_uid=MTIz; c_secure_pass=abc")
    'c_secure_uid=MTIz; c_secure_pass=abc'
    >>> parse_cookie_string("Cookie: c_secure_uid=MTIz; c_secure_login=x")
    'c_secure_uid=MTIz; c_secure_login=x'
    """
    if not text:
        return ""
    names = list(cookie_names or DEFAULT_COOKIE_NAMES)

    candidates = [text]
    m = _COOKIE_HEADER_RE.search(text)
    if m:
        candidates.insert(0, m.group(1))

    best, best_hits = "", -1
    for cand in candidates:
        pairs = _COOKIE_PAIR_RE.findall(cand)
        keep = [(k, v) for k, v in pairs if not k.lower().startswith(("path", "expires",
                                                                     "domain", "max-age",
                                                                     "samesite", "secure",
                                                                     "httponly"))]
        if not keep:
            continue
        hits = sum(1 for k, _ in keep if k in names)
        if hits > best_hits:
            best = "; ".join(f"{k}={v.strip()}" for k, v in keep if v.strip() != "" or k in names)
            best_hits = hits
    return best


def decode_c_secure_uid(value):
    """
    NexusPHP 把 uid 放进 c_secure_uid 时是 base64 编码的。
    解出数字 uid；解不出返回 None。

    >>> decode_c_secure_uid("MTAwMDE=")
    10001
    >>> decode_c_secure_uid("") is None
    True
    """
    if not value:
        return None
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), validate=False)
    except Exception:
        return None
    text = raw.decode("latin-1", errors="replace")
    m = re.match(r"(\d{1,10})", text)
    if not m:
        return None
    n = int(m.group(1))
    return n if 0 < n < 10 ** 9 else None


_UID_IN_LINK_RE = re.compile(r"userdetails\.php\?id=(\d+)")

# <a href="...userdetails.php?id=10009"...>名字</a>
# href 的引号单双都要认 —— 有的模板就写成 href='userdetails.php?id=…'
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
_HREF_RE = re.compile(
    r"href\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.I)

# NexusPHP 详情页的标准结构：<h1>用户名</h1> —— 有些模板没有「用户名」rowhead
_H1_RE = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.S | re.I)

# h1 里这些是栏目标题，不是用户名（只拦完整短语 —— 用户名里也可能带
# 「用户」两个字，见 probe 自测的用例）
_GENERIC_H1_RE = re.compile(
    r"用户详情|详细信息|个人资料|用户资料|用户信息|个人中心|控制面板|"
    r"profile|details", re.I)


def _anchor_text(s):
    return _html_unescape(re.sub(r"<[^>]+>", "", s)).strip()


def guess_username_from_html(html, uid):
    """
    从页面里认出用户名，按可靠性兜底：

    1. 链接到 userdetails.php?id=uid 的锚点文字（自己看自己的页，导航栏
       上的名字链接指向自己 —— 用户改名后页面永远最新）；
    2. <h1> —— NexusPHP 详情页的标题就是用户名。

    ★ 第 2 条是逐人刷新的关键：管理员看**别人**的详情页时，导航栏上的名字
      链接指向的是管理员自己，页面上不一定还有指向对方 uid 的链接 ——
      只靠锚点会一个名字都认不出。同一锚点出现多次时取出现最多的那个。
    """
    if not html or not uid:
        return None
    names = {}
    for m in _ANCHOR_RE.finditer(html):
        href = _HREF_RE.search(m.group(1) or "")
        if not href:
            continue
        val = href.group(1) or href.group(2) or href.group(3) or ""
        mm = _UID_IN_LINK_RE.search(val)
        if not mm or mm.group(1) != str(uid):
            continue
        name = _anchor_text(m.group(2))
        # 太长的基本是「发消息给 xxx」这类带上下文的文本；空的（纯图标）也不要
        if not name or len(name) > 40:
            continue
        names[name] = names.get(name, 0) + 1
    if names:
        return max(names.items(), key=lambda kv: kv[1])[0]

    for m in _H1_RE.finditer(html):
        name = _anchor_text(m.group(1))
        if name and len(name) <= 40 and not _GENERIC_H1_RE.search(name):
            return name
    return None


def guess_uid_from_html(html):
    """从页面里出现最多的 userdetails.php?id=N 猜当前登录者的 uid。"""
    if not html:
        return None
    hits = _UID_IN_LINK_RE.findall(html)
    if not hits:
        return None
    counts = {}
    for h in hits:
        counts[h] = counts.get(h, 0) + 1
    return int(max(counts.items(), key=lambda kv: kv[1])[0])


# ============================================================
# JSONC —— 让 config.json 能写注释
# ============================================================
#
# 标准 JSON 不允许注释，但一份没人看得懂的配置等于没有配置。
# 所以这里自己做一个宽容的解析器：
#
#   - 支持 // 行注释 与 /* */ 块注释（字符串内部的 // 不动，
#     所以 "base_url": "https://x" 不会被误伤）
#   - 顺带容忍尾随逗号（手改配置时最容易多打的那个逗号）
#
# 写回配置时用 patch_jsonc_field()：只替换某个键的值，**其余文本原样保留**，
# 注释不会因为你跑了一次 login.py 就消失。

def strip_json_comments(text):
    """
    去掉 JSON 里的注释和尾随逗号，保留字符串内部的一切。

    >>> strip_json_comments('{"a": 1}  // 注释')
    '{"a": 1}  '
    >>> strip_json_comments('{"u": "https://x/y"} // 尾注')
    '{"u": "https://x/y"} '
    >>> strip_json_comments('{"a": [1, 2,]}')
    '{"a": [1, 2]}'
    """
    if not text:
        return text

    out = []
    i, n = 0, len(text)
    in_str = False
    quote = ""

    while i < n:
        ch = text[i]

        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                in_str = False
            i += 1
            continue

        if ch == '"':
            in_str = True
            quote = ch
            out.append(ch)
            i += 1
            continue

        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if nxt == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    if text[i] == "\n":
                        out.append("\n")     # 保住行号
                    i += 1
                i += 2
                continue

        out.append(ch)
        i += 1

    clean = "".join(out)
    # 尾随逗号：,] 或 ,}（逗号与括号之间只允许空白）
    return re.sub(r",(\s*[\]}])", r"\1", clean)


def loads_jsonc(text):
    """解析带注释的 JSON。注释有语法错误时抛 json.JSONDecodeError。"""
    return json.loads(strip_json_comments(text))


def read_jsonc(path):
    """读一个 JSONC 文件。"""
    return loads_jsonc(Path(path).read_text(encoding="utf-8"))


def write_jsonc(path, text):
    """原样写回（用于保注释的文本级修改）。"""
    p = Path(path)
    p.write_text(text, encoding="utf-8")
    return p


_VALUE_KEY_RE = re.compile(r'"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"\s*:\s*')


def _string_mask(text):
    """
    给每个字符打标：True = 这个字符处在字符串字面量内部。

    用来避免把 `"seeding_list": "...userid={uid}..."` 里的 uid 之类
    误当成一个键。字符串里的转义也一起标上。
    """
    mask = bytearray(len(text))
    in_str = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                mask[i] = 1
                if i + 1 < n:
                    mask[i + 1] = 1
                i += 2
                continue
            if ch == '"':
                in_str = False          # 收尾引号本身不算字符串内容
                i += 1
                continue
            mask[i] = 1
            i += 1
            continue
        if ch == '"':
            in_str = True               # 起始引号本身不算字符串内容
        i += 1
    return mask


def _skip_ws_comments(text, i):
    """跳过空白与 // 行注释、/* */ 块注释，返回第一个有效字符的下标。"""
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            if text[i + 1] == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if text[i + 1] == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2
                continue
        break
    return i


def _scan_value_end(text, start):
    """
    从值的第一个字符开始，返回值的结束下标（不含）。

    标量到行尾 / 逗号 / 右括号为止 —— **在注释前就要停**，
    否则会把值后面的同行注释一起吃掉（注释会凭空少一行）。
    """
    n = len(text)
    i = _skip_ws_comments(text, start)
    if i >= n:
        return n
    if text[i] not in "{[":
        j = i
        in_str = False
        while j < n:
            ch = text[j]
            if in_str:
                if ch == "\\":
                    j += 2
                    continue
                if ch == '"':
                    in_str = False
                j += 1
                continue
            if ch == '"':
                in_str = True
            elif ch in ",}]\n":
                return j
            elif ch == "/" and j + 1 < n and text[j + 1] in "/*":
                return j
            j += 1
        return n
    depth = 0
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
        elif ch == "/" and i + 1 < n and text[i + 1] in "/*":
            i = _skip_ws_comments(text, i)
            continue
        i += 1
    return n


def _format_json(value, indent="", step="  "):
    """把值排成和周围一致的缩进。短标量数组保持单行，读起来清爽。"""
    if isinstance(value, dict):
        if not value:
            return "{}"
        inner = indent + step
        body = ",\n".join(
            f"{inner}{json.dumps(k, ensure_ascii=False)}: "
            f"{_format_json(v, inner, step)}"
            for k, v in value.items())
        return "{\n" + body + "\n" + indent + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(not isinstance(x, (dict, list)) for x in value):
            one = json.dumps(value, ensure_ascii=False)
            if len(one) <= 72:
                return one
        inner = indent + step
        body = ",\n".join(f"{inner}{_format_json(x, inner, step)}" for x in value)
        return "[\n" + body + "\n" + indent + "]"
    return json.dumps(value, ensure_ascii=False)


def patch_jsonc_value(text, path, value):
    """
    只替换**指定键的值**，其余文本（含注释、缩进、空行）原样保留。

    path 支持点号路径，例如 `"assessment.metrics"`；
    value 支持标量、数组、对象（会在原缩进上排好版）。

    同一个层级里同名键必须唯一，否则拒绝改动 —— 宁可不改，也不要改错地方。
    """
    keys = [k for k in str(path).split(".") if k]
    if not keys:
        raise KeyError("空的键路径")

    mask = _string_mask(text)
    lo, hi = 0, len(text)

    for depth, key in enumerate(keys):
        hits = [m for m in _VALUE_KEY_RE.finditer(text, lo, hi)
                if m.group("key") == key and not mask[m.start()]]
        where = ".".join(keys[:depth + 1])
        if not hits:
            raise KeyError(f"配置里没有键 {where!r}")
        if len(hits) > 1:
            raise KeyError(f"配置里 {where!r} 匹配到 {len(hits)} 处，"
                           f"不确定改哪个，已放弃改动")

        m = hits[0]
        vstart = _skip_ws_comments(text, m.end())
        if depth == len(keys) - 1:
            vend = _scan_value_end(text, vstart)
            line_start = text.rfind("\n", 0, m.start()) + 1
            indent = re.match(r"[ \t]*", text[line_start:m.start()]).group(0)
            return (text[:vstart] + _format_json(value, indent) + text[vend:])

        # 还要往里走：把搜索范围收窄到这一层对象的花括号内
        if vstart >= len(text) or text[vstart] != "{":
            raise KeyError(f"配置里 {where!r} 不是对象，没法继续找 {key!r} 的子键")
        lo, hi = vstart + 1, _scan_value_end(text, vstart) - 1

    return text


def patch_jsonc_field(text, key, value):
    """
    只替换顶层某个键的值，其余文本（含注释、缩进、空行）**原样保留**。

    要求该键在文本里只出现一次，避免误改嵌套结构里的同名字段。

    >>> t = '{\\n  // 说明\\n  "uid": 0,\\n  "x": 1\\n}'
    >>> print(patch_jsonc_field(t, "uid", 10001))
    {
      // 说明
      "uid": 10001,
      "x": 1
    }
    """
    return patch_jsonc_value(text, key, value)


def login_form_path(ep):
    """
    NexusPHP 登录表单真正提交的地址是 takelogin.php，
    login.php 只是展示表单。两者不一定同目录，按 login 推导。
    """
    try:
        p = ep.path("login")
    except KeyError:
        return "takelogin.php"
    head, sep, tail = p.rpartition("/")
    return (head + sep if sep else "") + re.sub(r"login\.php", "takelogin.php", tail or p)


# ============================================================
# 配置加载 —— 全部走一个 config.json
# ============================================================

REQUIRED_KEYS = ("base_url", "plans")


def resolve_config_path(config_path=None, root=None):
    """
    定位配置文件。

    config.json 不存在时回退到 config.example.json —— 这样 clone 下来
    不填任何东西也能直接跑 `python settle.py`（离线计薪不需要 cookie）。
    返回 (Path, 是否用的是模板)。
    """
    root = Path(root or Path(__file__).resolve().parent)
    if config_path:
        p = Path(config_path)
        if p.exists():
            return p, False
        ex = p.with_name(CONFIG_EXAMPLE_NAME)
        if not ex.exists():
            ex = RES_ROOT / CONFIG_EXAMPLE_NAME      # 打包后模板在资源目录
        if ex.exists():
            return ex, True
        return p, False

    real, ex = root / CONFIG_NAME, root / CONFIG_EXAMPLE_NAME
    if real.exists():
        return real, False
    if not ex.exists():
        ex = RES_ROOT / CONFIG_EXAMPLE_NAME          # 打包后模板在资源目录
    if ex.exists():
        return ex, True
    return real, False


def load_config(config_path=None, root=None, require_cookie=False):
    """
    读 config.json，返回 (config, endpoints, session)。

    config.json 一个文件管全部：站点地址、接口路径、cookie、考核口径、
    方案与薪资、税率。cookie 为空时不报错 —— 离线计薪用不着它。
    """
    p, is_example = resolve_config_path(config_path, root)
    if not p.exists():
        raise FileNotFoundError(
            f"找不到 {p}\n"
            f"复制 {CONFIG_EXAMPLE_NAME} 为 {CONFIG_NAME} 再改，或直接跑 python login.py")

    try:
        cfg = read_jsonc(p)
    except json.JSONDecodeError as e:
        raise ValueError(f"{p} 不是合法 JSON（已按 JSONC 处理，允许 // 与 /* */ 注释）：{e}") from e

    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        raise ValueError(f"{p} 缺少必需字段：{', '.join(missing)}")

    ep = Endpoints(cfg["base_url"], cfg.get("endpoints"))

    cookie = cfg.get("cookie", "")
    if require_cookie and not cookie:
        raise ValueError(
            f"{p} 里没有 cookie。先跑 python login.py 拿一个"
            + ("（当前用的是 config.example.json 模板，先复制成 config.json）"
               if is_example else ""))

    sess = Session(cfg["base_url"], cookie,
                   timeout=float(cfg.get("timeout_seconds", 25)))
    return cfg, ep, sess


def cookie_names_of(cfg):
    return list(cfg.get("cookie_names") or DEFAULT_COOKIE_NAMES)

