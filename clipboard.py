#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
剪贴板读取 —— 纯 Python 标准库，零第三方依赖。

给 login.py / 图形界面取 cookie 用：用户在浏览器 DevTools 里复制一次
Cookie 头，这里从系统剪贴板把它读出来。100% 可靠，不碰浏览器内部。
"""

import shutil
import subprocess
import sys

__all__ = ["read_clipboard", "clipboard_supported"]

IS_WIN = sys.platform.startswith("win")


# ============================================================
# 剪贴板
# ============================================================

def _clipboard_windows():
    import ctypes
    from ctypes import wintypes

    u32 = ctypes.windll.user32
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CF_UNICODETEXT = 13

    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.OpenClipboard.restype = wintypes.BOOL
    u32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    u32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    u32.GetClipboardData.argtypes = [wintypes.UINT]
    u32.GetClipboardData.restype = wintypes.HANDLE
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

    if not u32.OpenClipboard(None):
        return ""
    try:
        if not u32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        handle = u32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = k32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            k32.GlobalUnlock(handle)
    finally:
        u32.CloseClipboard()


def _run(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.decode("utf-8", errors="replace")
    except Exception:
        pass
    return ""


def read_clipboard():
    """读系统剪贴板文本；读不到返回空串。"""
    if IS_WIN:
        try:
            return _clipboard_windows() or ""
        except Exception:
            return ""
    if sys.platform == "darwin":
        return _run(["pbpaste"])
    for cmd in (["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"]):
        txt = _run(cmd)
        if txt:
            return txt
    return ""


def clipboard_supported():
    if IS_WIN or sys.platform == "darwin":
        return True
    return bool(shutil.which("xclip") or shutil.which("xsel"))
