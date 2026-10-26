# -*- coding: utf-8 -*-
"""界面尺寸度量工具：所有「按字体实测算尺寸」的逻辑集中在这里（不写死像素）。

项目约定（见 .workbuddy/memory/MEMORY.md）：UI 尺寸不写死像素 —— 行高、列宽按
``TkDefaultFont`` 实测；长文本用 ``ellipsize`` 截断 + ``<Configure>`` 重绘。
本模块是该约定的唯一实现处：``gui/app.py`` 与（将来）``gui/theme.py`` 都从这里取尺寸，
避免同一套二分截断在两处各写一遍。

字体句柄拿不到时（Tk 尚未初始化 / 已销毁）一律返回安全兜底值，不抛异常，
保证在无头或窗口销毁过程中被调用也不会炸。
"""
import tkinter as tk
import tkinter.font as tkfont

# 单字符省略号：比 "..." 省一半宽度，且不会被折行切断
ELLIPSIS = "…"

# 章节「状态」列 = 最长状态文本实测宽度 + 内边距。
# 内边距 20px 既要盖住单元格内边距，也要满足测试要求的 `need + 12`。
STATE_COL_PAD = 20

# 章节名列最小宽度：再窄就读不出章节名了
TITLE_MIN_W = 220

# 课程树（show="tree"）的 #0 列：最小宽度防止分栏被拖到极窄时列宽归零
COURSE_MIN_W = 200

# 兜底行高：拿不到字体时按 16px 算
FALLBACK_LINE_H = 16


def font_of():
    """当前默认字体句柄；拿不到返回 None。"""
    try:
        return tkfont.nametofont("TkDefaultFont")
    except (tk.TclError, RuntimeError):
        return None


def line_height() -> int:
    """当前字体的行高（Treeview rowheight、提示条高度都按它算）。"""
    font = font_of()
    if font is None:
        return FALLBACK_LINE_H
    try:
        return font.metrics("linespace")
    except tk.TclError:
        return FALLBACK_LINE_H


def measure(text) -> int:
    """文本在当前字体下的像素宽度。"""
    font = font_of()
    if font is None:
        return 0
    try:
        return font.measure(str(text or ""))
    except tk.TclError:
        return 0


def ellipsize(text, avail_px: int) -> str:
    """按像素宽度截断并补省略号；放得下则原样返回（不改变语义）。

    二分找最长前缀使 ``measure(前缀 + "…") <= avail_px``。
    """
    text = str(text or "")
    if not text:
        return text
    font = font_of()
    if font is None or avail_px <= 0:
        return text
    try:
        if font.measure(text) <= avail_px:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid] + ELLIPSIS) <= avail_px:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo].rstrip() + ELLIPSIS
    except tk.TclError:
        return text
