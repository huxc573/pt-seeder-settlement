#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
考核表读取器 —— 支持 .csv / .xlsx，纯 Python 标准库，零第三方依赖。

.xlsx 本质是个 zip，里面是 XML。所以 zipfile + xml.etree 就能读，
不需要 openpyxl。

对外只暴露两个函数：

    read_table(path)        -> [[cell, ...], ...]        原始二维表
    load_roster(path)       -> [Member, ...]             按表头映射后的成员列表

表头识别是**按名字**而不是按列号 —— 你调整列顺序、加备注列都不会崩。
"""

import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

__all__ = ["Member", "read_table", "load_roster", "write_back",
           "parse_check_cell", "HEADER_ALIASES"]

_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_COL_RE = re.compile(r"([A-Z]+)")


# ============================================================
# 基础工具
# ============================================================

def _col_index(ref):
    """'C7' -> 2（0-based 列号）"""
    m = _COL_RE.match(ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _clean(s):
    return (s or "").replace("\u00a0", " ").replace("\u3000", " ").strip()


# ============================================================
# .xlsx 读取
# ============================================================

def _read_shared_strings(zf):
    shared = []
    try:
        raw = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return shared
    root = ET.fromstring(raw)
    for si in root.findall(_XLSX_NS + "si"):
        shared.append("".join(t.text or "" for t in si.iter(_XLSX_NS + "t")))
    return shared


def _sheet_names(zf):
    """按 workbook.xml 里的顺序返回 worksheet 的内部路径。"""
    try:
        root = ET.fromstring(zf.read("xl/workbook.xml"))
    except KeyError:
        return []
    rels = {}
    try:
        rroot = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rroot:
            rels[rel.get("Id")] = rel.get("Target")
    except KeyError:
        pass

    names = []
    rid_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    for sh in root.iter(_XLSX_NS + "sheet"):
        target = rels.get(sh.get(rid_attr))
        if not target:
            continue
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        names.append((sh.get("name") or "", target))
    return names


def _read_xlsx(path, sheet=None):
    with zipfile.ZipFile(path) as zf:
        shared = _read_shared_strings(zf)
        sheets = _sheet_names(zf)
        if not sheets:
            sheets = [(n, n) for n in sorted(
                x for x in zf.namelist()
                if re.match(r"xl/worksheets/sheet\d+\.xml$", x))]

        target = sheets[0][1] if sheets else None
        if sheet is not None:
            if isinstance(sheet, int):
                if 0 <= sheet < len(sheets):
                    target = sheets[sheet][1]
            else:
                for nm, t in sheets:
                    if nm == sheet:
                        target = t
                        break
        if not target:
            return []

        root = ET.fromstring(zf.read(target))

    rows = []
    for row in root.iter(_XLSX_NS + "row"):
        cells = {}
        for c in row.findall(_XLSX_NS + "c"):
            idx = _col_index(c.get("r"))
            ctype = c.get("t")
            v = c.find(_XLSX_NS + "v")
            if ctype == "s" and v is not None and v.text:
                try:
                    val = shared[int(v.text)]
                except (ValueError, IndexError):
                    val = ""
            elif ctype == "inlineStr":
                is_el = c.find(_XLSX_NS + "is")
                val = "".join(t.text or "" for t in is_el.iter(_XLSX_NS + "t")) if is_el is not None else ""
            else:
                val = v.text if v is not None else ""
            cells[idx] = _clean(val)
        if not cells:
            continue
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return rows


# ============================================================
# .csv 读取
# ============================================================

def _read_csv(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")

    # WPS / Excel 存的 CSV 是 \r\n 换行：不去掉，csv 会报
    # 「new-line character seen in unquoted field - do you need to open
    #   the file with newline=''?」 —— 整个考核表直接读不了
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    sample = text[:4096]
    delim = ","
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",\t;").delimiter
    except Exception:
        if sample.count("\t") > sample.count(","):
            delim = "\t"

    rows = []
    for rec in csv.reader(io.StringIO(text), delimiter=delim):
        rows.append([_clean(c) for c in rec])
    return rows


def read_table(path, sheet=None):
    """读 .xlsx / .csv / .tsv，返回二维字符串表。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到表格文件：{p}")

    # 先看**内容**再认扩展名：xlsx 本质是 zip，开头固定 PK\x03\x4。
    # WPS/Excel「另存为」时经常把 xlsx 存成 .csv 扩展名（或反过来），
    # 只认扩展名就会拿 zip 当文本读出一屏乱码。
    try:
        with open(p, "rb") as fh:
            magic = fh.read(4)
    except OSError:
        magic = b""
    if magic[:2] == b"PK":
        return _read_xlsx(p, sheet=sheet)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        return _read_xlsx(p, sheet=sheet)
    return _read_csv(p)


# ============================================================
# 表头映射
# ============================================================

HEADER_ALIASES = {
    "uid":      ["uid", "用户id", "用户 id", "id", "用户编号"],
    "username": ["用户名", "使用者", "账号", "帳號", "username", "user"],
    "plan":     ["方案", "套餐", "plan", "档位", "級別", "级别"],
    "check":    ["检查日期", "檢查日期", "检查", "日期", "考核", "体积", "总大小", "大小"],
    "count":    ["做种数量", "种子数量", "数量", "条数", "种子数", "count"],
    "note":     ["备注", "備註", "note", "remark"],
}


def _find_header(rows):
    """返回 (行号, {字段: 列号})，找不到返回 (None, {})

    最低要求只有「UID」列 —— 用户名和方案都可选：
    用户名在算工资联网刷新时从详情页自动取（用户可能改名，表里写了也以站点为准）。
    """
    for i, row in enumerate(rows[:10]):
        mapping = {}
        for j, cell in enumerate(row):
            low = cell.lower()
            for fld, aliases in HEADER_ALIASES.items():
                if fld in mapping:
                    continue
                if any(low == a or (len(a) >= 3 and a in low) for a in aliases):
                    mapping[fld] = j
                    break
        if "uid" in mapping:
            return i, mapping
    return None, {}


# ============================================================
# 考核单元格：「260815-3.560 TB」→ (日期, 体积) / 「260815-4893 条」→ (日期, 数量)
# ============================================================

# 日期必须是独立 token：(?<!\d)(?!\d) 两个边界，避免把 "3.560" 里的 "3.56" 当日期。
_DATE_RE = re.compile(r"(?<!\d)(\d{8}|\d{6}|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})(?!\d)")

# 体积必须带单位。**故意不加 re.I** —— 加了会把种子名 "2160p" 的 p 当 PB。
_SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)*)\s*([KMGTP](?:i?B)?)(?![A-Za-z])")
_UNIT_FACTOR = {
    "K": 1024, "KB": 1024, "KIB": 1024,
    "M": 1024 ** 2, "MB": 1024 ** 2, "MIB": 1024 ** 2,
    "G": 1024 ** 3, "GB": 1024 ** 3, "GIB": 1024 ** 3,
    "T": 1024 ** 4, "TB": 1024 ** 4, "TIB": 1024 ** 4,
    "P": 1024 ** 5, "PB": 1024 ** 5, "PIB": 1024 ** 5,
}
_TB = 1024 ** 4

# 显式数量：4893 条 / 4893个 / (1839)（新检查日期格 260919-5.725T(1839) 的尾巴）
_COUNT_RE = re.compile(r"(?:(\d[\d,]*)\s*(?:条|個|个)|\((\d[\d,]*)\))")
# 裸数字兜底
_BARE_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_check_cell(text):
    """
    解析考核单元格。三种写法都认：

    >>> c = parse_check_cell('260815-3.560 TB')
    >>> c['date'], round(c['size_tb'], 3), c['count']
    ('260815', 3.56, None)

    >>> c = parse_check_cell('260815-4893 条')
    >>> c['date'], c['size_bytes'], c['count']
    ('260815', None, 4893)

    >>> c = parse_check_cell('260815-3.560 TB / 4893个')['count']
    4893

    >>> parse_check_cell('')['size_bytes'] is None
    True
    >>> parse_check_cell('no-data')['count'] is None
    True

    裸数字兜底规则（没写单位也没写「条」时）：
      带小数点 → 当体积，按 TB 算
      纯整数   → 当数量
    这样 `260815-4893` 和 `260815-3.560` 都能读对。
    """
    out = {"date": None, "size_bytes": None, "size_tb": None,
           "count": None, "raw": text or ""}
    if not text:
        return out

    rest = _clean(text)

    md = _DATE_RE.search(rest)
    if md:
        out["date"] = md.group(1)
        rest = rest[:md.start()] + " " + rest[md.end():]

    ms = _SIZE_RE.search(rest)
    if ms:
        unit = ms.group(2).upper()
        n = int(float(ms.group(1).replace(",", "")) * _UNIT_FACTOR.get(unit, 1))
        out["size_bytes"] = n
        out["size_tb"] = n / _TB
        rest = rest[:ms.start()] + " " + rest[ms.end():]

    mc = _COUNT_RE.search(rest)
    if mc:
        tok = mc.group(1) or mc.group(2)
        out["count"] = int(tok.replace(",", ""))
        rest = rest[:mc.start()] + " " + rest[mc.end():]

    # 裸数字兜底：只在还什么都没解析出来、且残余里只有一个数字时才认
    if out["size_bytes"] is None and out["count"] is None:
        nums = _BARE_NUM_RE.findall(rest)
        if len(nums) == 1:
            tok = nums[0]
            if "." in tok:
                n = int(float(tok) * _TB)
                out["size_bytes"] = n
                out["size_tb"] = n / _TB
            else:
                out["count"] = int(tok)

    return out


# ============================================================
# 成员对象
# ============================================================

@dataclass
class Member:
    uid: Optional[int]
    username: str
    plan_id: str
    check_raw: str = ""            # 「检查日期」列的原始文本，原样带进工资表
    check_date: Optional[str] = None
    measured_bytes: Optional[int] = None
    measured_tb: Optional[float] = None
    measured_count: Optional[int] = None
    measure_error: str = ""        # 联网刷新失败的原因（会写进工资表「结果」列）
    note: str = ""
    row_no: int = 0
    extra: dict = field(default_factory=dict)


def load_roster(path, sheet=None):
    """
    读考核表，返回 [Member, ...]。

    表头按名字匹配，列顺序随便动。最低要求：有「UID」列；推荐顺序
    「UID | 方案 | 用户名 | 检查日期 | 结果」。
    **用户名可以不填** —— 算工资联网刷新时会从详情页自动取（用户可能改名），
    表里写了也只当参考，以站点页面为准。
    体积/数量从「检查日期」列的复合单元格里解析（`260815-3.560 TB`）；
    也支持单开一列「做种数量」。
    """
    rows = read_table(path, sheet=sheet)
    if not rows:
        raise ValueError(f"{path} 是空表")

    hrow, mapping = _find_header(rows)
    if hrow is None:
        raise ValueError(
            "没找到表头行。至少需要「UID」列（推荐：UID | 方案 | 用户名 | 检查日期）。\n"
            f"实际读到的前 3 行：{rows[:3]}"
        )

    members = []
    for i, row in enumerate(rows[hrow + 1:], start=hrow + 1):
        def cell(fld):
            j = mapping.get(fld)
            return row[j] if j is not None and j < len(row) else ""

        username = cell("username")
        ui = cell("uid")
        plan = cell("plan")
        if not username and not ui:
            continue
        if username in ("用户名",) or ui.lower() == "uid":
            continue

        try:
            uid = int(re.sub(r"[^\d]", "", ui)) if ui else None
        except ValueError:
            uid = None

        raw = cell("check")
        chk = parse_check_cell(raw)

        count = chk["count"]
        if count is None:
            n = _BARE_NUM_RE.findall(cell("count").replace(",", ""))
            if len(n) == 1 and "." not in n[0]:
                count = int(n[0])

        members.append(Member(
            uid=uid,
            username=username,
            plan_id=plan.upper().replace(" ", ""),
            check_raw=raw,
            check_date=chk["date"],
            measured_bytes=chk["size_bytes"],
            measured_tb=chk["size_tb"],
            measured_count=count,
            note=cell("note"),
            row_no=i + 1,
        ))
    return members


def write_back(path, members, log=None):
    """把成员的最新值（用户名 / 方案 / 检查日期 / 备注）写回考核表。

    **只支持 .csv** —— xlsx 只读（纯标准库不造 xlsx 写入器），要回写先转存 csv。
    按 **UID 对位**（不按行号：空行、列序变化、多出的行都不怕），文件里
    没出现的人不动；只改用户名 / 方案 / 检查日期 / 备注 这四列，表里
    多出来的任何列原样保留。返回写回的人数。

    联网抓失败的行（measure_error 有值且 check_raw 为空）**不写**检查日期 ——
    别把表里原有的手填数据清掉。编码跟着原文件走（WPS 的「CSV」是 GBK、
    「CSV UTF-8」带 BOM —— 读法与 `_read_csv` 一致，写回**不换编码**）。
    """
    p = Path(path)
    if p.suffix.lower() != ".csv":
        raise ValueError(f"{p.name} 不是 .csv —— xlsx 只读，回写前先转存成 csv")

    # 编码探测：和 _read_csv 同一条链，写回时用同一个
    raw = p.read_bytes()
    enc = None
    for enc_try in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            raw.decode(enc_try)
            enc = enc_try
            break
        except UnicodeDecodeError:
            continue
    if enc is None:
        enc = "utf-8"

    table = _read_csv(p)
    if not table:
        raise ValueError(f"{p} 是空表")
    hrow, mapping = _find_header(table)
    if hrow is None:
        raise ValueError(f"{p} 没找到表头行（至少要有 UID 列）")

    by_uid = {}                                   # uid -> 文件行下标
    for ri in range(hrow + 1, len(table)):
        row = table[ri]
        j = mapping.get("uid")
        if j is None or j >= len(row):
            continue
        digits = "".join(ch for ch in row[j] if ch.isdigit())
        if digits:
            by_uid.setdefault(int(digits), ri)

    def put(row, fld, value):
        j = mapping.get(fld)
        if j is None:
            return
        while j >= len(row):
            row.append("")
        row[j] = value

    n = 0
    for m in members:
        ri = by_uid.get(m.uid)
        if ri is None:
            continue
        r = table[ri]
        keep_check = bool(m.measure_error) and not m.check_raw
        put(r, "username", m.username or "")
        put(r, "plan", m.plan_id or "")
        if not keep_check:
            put(r, "check", m.check_raw or "")
        put(r, "note", m.note or "")
        n += 1

    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerows(table)
    p.write_bytes(buf.getvalue().encode(enc))
    if log:
        log(f"  写回 {p.name}：{n} 人")
    return n
