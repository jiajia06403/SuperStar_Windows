# -*- coding: utf-8 -*-
# layer: gui  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""响应式布局回归：主窗口在多档尺寸下都不裁切、列宽随可用宽度伸缩。

覆盖的改造点：
  1. 工具栏在任何档位都完整可见（窄窗口自动折行）；
  2. 章节「状态」列始终放得下最长的状态文本；
  3. 章节「章节」列随可用宽度伸缩（拖窄分栏后会收缩，不再常驻横向滚动条）；
  4. 状态栏 / 章节提示超宽时截断加省略号，且 status_var 保留原文；
  5. 日志长行仍可横向滚动查看。
"""
import os
import sys
import tempfile
import time
import tkinter.font as tkfont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate, annotate_exc  # noqa: E402

from gui.app import CHAPTER_STYLE, build  # noqa: E402

# 档位：最小窗口 / 同学反馈的小屏 / 默认尺寸 / 大屏
SIZES = ((840, 560), (900, 600), (980, 660), (1320, 760))


def _pump(app, rounds: int = 6, gap: float = 0.05) -> None:
    """推进 Tk 事件循环并留出时间，让列宽重算的 after(60) 定时器真正跑完。"""
    for _ in range(rounds):
        app.root.update()
        time.sleep(gap)
    app.root.update()


def _check(app, width: int, height: int, font) -> None:
    tag = f"{width}x{height}"
    app.root.geometry(f"{width}x{height}")
    _pump(app)

    # 1) 工具栏必须完整可见
    assert app.toolbar_fits(), f"{tag}: 工具栏被窗口裁切"

    # 2) 状态列放得下最长的状态文本
    need = max(font.measure(t) for t, _c in CHAPTER_STYLE.values())
    state_w = int(app.chapter_tree.column("state", "width"))
    assert state_w >= need + 12, f"{tag}: 状态列 {state_w}px 放不下 {need}px 的文本"

    # 3) 章节列收缩到可用宽度内（分栏被拖窄时也要收缩，而不是停在初始的 360px）
    tree_w = app.chapter_tree.winfo_width()
    title_w = int(app.chapter_tree.column("title", "width"))
    limit = max(220, tree_w - state_w + 2)
    assert title_w <= limit, f"{tag}: 章节列 {title_w}px 超出可用宽度（上限 {limit}px）"

    # 4) 提示 / 状态栏超宽时截断，且不超出控件宽度
    app._set_hint("上海市大学生安全教育（2026级） · 已完成 2/244 章 · 未开放 90 章" * 3)
    app._set_status_text("学习中 3/244 · " + "很长的章节标题" * 20)
    _pump(app, rounds=2)
    for name, widget in (("hint", app.chapter_hint), ("status", app.status_label)):
        shown = widget.cget("text")
        assert shown.endswith("…"), f"{tag}/{name}: 超长文本未截断: {shown!r}"
        assert font.measure(shown) <= widget.winfo_width() + 2, \
            f"{tag}/{name}: 文本超出控件宽度: {shown!r}"
    assert app.status_var.get().startswith("学习中"), "status_var 必须保留原文"

    # 5) 日志长行可横向滚动
    app.log("视频进度 | " + "超长日志内容" * 60)
    _pump(app, rounds=2)
    x0, x1 = app.log_text.xview()
    assert x1 - x0 < 1.0, f"{tag}: 长日志行应有横向滚动条"


def test_responsive_across_sizes():
    """四档尺寸 + 最大化：布局都不裁切，列宽随可用宽度伸缩。"""
    app = build(demo=True, cache_dir=tempfile.mkdtemp(prefix="ss_resp_"))
    app.silent = True
    try:
        font = tkfont.nametofont("TkDefaultFont")
        for width, height in SIZES:
            _check(app, width, height, font)

        # 最大化档
        app.root.state("zoomed")
        _pump(app)
        assert app.toolbar_fits(), "最大化: 工具栏被裁切"
        assert int(app.chapter_tree.column("title", "width")) > 360, \
            "最大化: 章节列应随窗口变宽"

        # 分栏拖到最窄：章节列收缩到最小宽度，不再撑出横向滚动条
        app.root.state("normal")
        app.root.geometry("980x660")
        _pump(app)
        app._body.sashpos(0, app._body.winfo_width() - 240)
        _pump(app)
        state_w = int(app.chapter_tree.column("state", "width"))
        title_w = int(app.chapter_tree.column("title", "width"))
        tree_w = app.chapter_tree.winfo_width()
        assert title_w <= max(220, tree_w - state_w + 2), \
            f"分栏拖窄后章节列未收缩: {title_w}px / 可用 {tree_w - state_w}px"
    finally:
        app.root.destroy()


if __name__ == "__main__":
    try:
        test_responsive_across_sizes()
        print("PASS  test_responsive_across_sizes")
        sys.exit(0)
    except AssertionError as exc:
        print(f"FAIL  test_responsive_across_sizes: {exc}")
        annotate_exc("test_gui_responsive.py::test_responsive_across_sizes")
        sys.exit(1)
    except Exception:  # noqa: BLE001
        annotate_exc("test_gui_responsive.py::unexpected")
        raise
