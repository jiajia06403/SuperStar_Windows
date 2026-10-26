# -*- coding: utf-8 -*-
"""GUI 冒烟测试：在真实 Tk 窗口中跑一遍演示流程，校验控件状态。

无需账号与网络（使用 DemoChaoxing 假后端 + 临时进度缓存）。
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate, annotate_exc  # noqa: E402

from gui.selftest import run_selftest  # noqa: E402


def test_gui_demo_flow():
    stats = run_selftest()
    assert stats.get("passed") is True, f"界面自检未通过: {stats}"
    assert stats["courses"] == 3, f"课程数异常: {stats['courses']}"
    assert stats["done"] == stats["chapters"] > 0, f"章节完成数与展示不一致: {stats}"
    assert stats["log_lines"] > 0, "日志面板为空"


def test_gui_chapter_not_open_display():
    """「读取进度」遇到的未开放章节显示为「未开放」，不是「待学」。"""
    from gui.app import CHAPTER_STYLE, build

    app = build(demo=True, cache_dir=tempfile.mkdtemp(prefix="ss_notopen_"))
    app.silent = True
    try:
        app.engine.login_demo(not_open_ids={"pt4"})
        course = next(c for c in app.engine.list_courses() if c["courseId"] == "C1001")
        points = app.engine.read_progress(course)
        app.current_course = course
        app._ev_points(course, points)
        app.root.update_idletasks()

        tree = app.chapter_tree
        not_open_text, pending_text = CHAPTER_STYLE["not_open"][0], CHAPTER_STYLE["pending"][0]
        assert tree.set("pt4", "state") == not_open_text, "未开放章节应显示「未开放」"
        assert tree.set("pt2", "state") == pending_text, "已开放未完成章节应保持「待学」"
        assert tree.set("pt1", "state") == CHAPTER_STYLE["done"][0]
        assert "未开放 1 章" in app.chapter_hint.cget("text"), app.chapter_hint.cget("text")

        # 刷课途中发现未开放时，该行要即时切到「未开放」
        app._ev_progress("C1001", "pt2", "not_open")
        assert tree.set("pt2", "state") == not_open_text
        assert "未开放 2 章" in app.chapter_hint.cget("text"), app.chapter_hint.cget("text")
    finally:
        app.root.destroy()


def _pump_until(app, predicate, timeout=20.0):
    """在真实窗口里推进事件循环，直到 predicate() 成立或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.root.update()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_gui_read_progress_is_async():
    """「读取进度」异步跑：按钮立即禁用、探测过程界面可刷新、结束后按钮恢复。"""
    from gui.app import CHAPTER_STYLE, build

    app = build(demo=True, cache_dir=tempfile.mkdtemp(prefix="ss_async_"))
    app.silent = True
    try:
        app.engine.login_demo(not_open_ids={"pt4", "pt5"})
        app.current_course = next(c for c in app.engine.list_courses() if c["courseId"] == "C1001")
        app.on_read_progress()
        assert str(app.read_btn["state"]) == "disabled", "读取期间应禁用「读取进度」按钮"
        # 重复点击不得再起一个任务
        app.on_read_progress()

        assert _pump_until(app, lambda: not app._reading), "读取任务未在超时内结束"
        tree = app.chapter_tree
        assert tree.set("pt4", "state") == CHAPTER_STYLE["not_open"][0]
        assert tree.set("pt5", "state") == CHAPTER_STYLE["not_open"][0]
        assert tree.set("pt2", "state") == CHAPTER_STYLE["pending"][0]
        assert "未开放 2 章" in app.chapter_hint.cget("text"), app.chapter_hint.cget("text")
        assert str(app.read_btn["state"]) != "disabled", "读取结束后按钮应恢复可用"
        assert "未开放 2 章" in app.status_var.get(), app.status_var.get()

        # 切换课程要能中断正在进行的读取（按钮状态随之复位）
        app.on_read_progress()
        app._select_course("C1002")
        assert not app._reading, "切课程后旧的读取任务应被作废"
    finally:
        app.root.destroy()


if __name__ == "__main__":
    try:
        test_gui_demo_flow()
        print("PASS  test_gui_demo_flow")
        test_gui_chapter_not_open_display()
        print("PASS  test_gui_chapter_not_open_display")
        test_gui_read_progress_is_async()
        print("PASS  test_gui_read_progress_is_async")
        sys.exit(0)
    except AssertionError as exc:
        print(f"FAIL  test_gui_smoke: {exc}")
        annotate_exc("test_gui_smoke.py::test_gui_chapter_not_open_display")
        sys.exit(1)
    except Exception:  # noqa: BLE001
        annotate_exc("test_gui_smoke.py::unexpected")
        raise
