# -*- coding: utf-8 -*-
# layer: gui  # core=默认必跑 / gui=真窗口 / slow=仅 --all
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
    """刷课时发现的未开放章节显示为「未开放」（done/pending 不受影响）。"""
    from gui.app import CHAPTER_STYLE, build

    app = build(demo=True, cache_dir=tempfile.mkdtemp(prefix="ss_notopen_"))
    app.silent = True
    try:
        app.engine.login_demo()
        course = next(c for c in app.engine.list_courses() if c["courseId"] == "C1001")
        # 模拟「读取进度」/ 勾选课程得到的章节列表（pt4 由服务端判定为未开放）
        points = [
            {"id": "pt1", "title": "第一章", "has_finished": True},
            {"id": "pt2", "title": "第二章", "has_finished": False},
            {"id": "pt4", "title": "第四章", "has_finished": False, "not_open": True},
        ]
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


def test_gui_course_shows_in_progress_while_studying():
    """刷课时左侧课程列表把当前课程标为「进行中」，切课自动复位，结束清除。"""
    from gui.app import build

    app = build(demo=True, cache_dir=tempfile.mkdtemp(prefix="ss_studying_"))
    app.silent = True
    try:
        app.engine.login_demo()
        courses = app.engine.list_courses()
        app._ev_courses(courses)
        app.root.update_idletasks()

        cid1 = str(courses[0]["courseId"])
        cid2 = str(courses[1]["courseId"])

        app._ev_chapter(cid1, 0, 5, "第一章")
        assert "进行中" in app.course_tree.item(cid1, "text")
        assert "studying" in app.course_tree.item(cid1, "tags")

        # 切到下一门课：上一门复位、当前门标「进行中」
        app._ev_chapter(cid2, 0, 3, "Unit 1")
        assert "进行中" not in app.course_tree.item(cid1, "text")
        assert "进行中" in app.course_tree.item(cid2, "text")
        assert "studying" in app.course_tree.item(cid2, "tags")

        # 刷课结束：标记清除
        app._ev_finished()
        assert "进行中" not in app.course_tree.item(cid2, "text")
        assert "studying" not in app.course_tree.item(cid2, "tags")
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


def test_gui_text_not_clipped():
    """回归：状态列 / 状态栏 / 章节提示 / 日志长文本都要完整可见（高 DPI 也不裁字）。"""
    import tkinter.font as tkfont

    from gui.app import CHAPTER_STYLE, build

    app = build(demo=True, cache_dir=tempfile.mkdtemp(prefix="ss_clip_"))
    app.silent = True
    try:
        app.root.geometry("900x600")     # 偏窄窗口，模拟同学的小屏
        app.root.update_idletasks()
        app.root.update()
        font = tkfont.nametofont("TkDefaultFont")

        # 1) 状态列必须放得下最长的状态文本（未开放）
        need = max(font.measure(t) for t, _c in CHAPTER_STYLE.values())
        col_w = app.chapter_tree.column("state", "width")
        assert col_w >= need + 12, f"状态列 {col_w}px 放不下 {need}px 的文本"

        # 2) 章节提示超宽时要加省略号，且不超出控件
        app._set_hint("上海市大学生安全教育（2026级） · 已完成 2/244 章 · 未开放 90 章" * 3)
        app.root.update_idletasks()
        shown = app.chapter_hint.cget("text")
        assert shown.endswith("…"), f"超长提示未截断: {shown!r}"
        assert font.measure(shown) <= app.chapter_hint.winfo_width() + 2, shown

        # 3) 状态栏同理，但 status_var 里保留原文
        app._set_status_text("学习中 3/244 · " + "很长的章节标题" * 20)
        app.root.update_idletasks()
        shown = app.status_label.cget("text")
        assert shown.endswith("…"), f"超长状态未截断: {shown!r}"
        assert app.status_var.get().startswith("学习中"), app.status_var.get()

        # 4) 日志长行要能横向滚动查看（wrap=none 时必须配横向滚动条）
        app.log("视频进度 | " + "超长日志内容" * 60)
        app.root.update_idletasks()
        x0, x1 = app.log_text.xview()
        assert x1 - x0 < 1.0, "长日志行应有横向滚动条"
        assert app.log_text.xview()[0] == 0.0, "日志应保持左对齐显示"
    finally:
        app.root.destroy()


def test_log_panel_hides_noise():
    """日志面板只留用户看得懂的进度/结果：答题明细、题库/Token 细节只写文件。"""
    from loguru import logger as _logger

    from gui.app import build

    app = build(demo=True, cache_dir=tempfile.mkdtemp(prefix="ss_log_"))
    app.silent = True
    captured: "list[str]" = []
    sink = _logger.add(lambda m: captured.append(m), level="TRACE", format="{message}")
    try:
        app.root.update()
        app.log_text.configure(state="normal")
        app.log_text.delete("1.0", "end")
        app.log_text.configure(state="disabled")

        noise = [
            "【多选题】大学生安全教育应当满足哪些要求？（ ） 填写答案为 BCD",
            "成功获取到答案：ABCD",
            "随机选择 -> true",
            "章节检测题库覆盖率： 0%",
            "从缓存中获取答案：xxx -> A",
            "使用Token ...abcde 查询成功，剩余次数: 99",
            "课程章节读取成功...",
            "未找到字体文件，可能是未加密的题目不进行解密",
        ]
        keep = [
            "开始学习课程: 上海市大学生安全教育（2026级）（共 244 个章节）",
            "章节完成: 1.1 大学生安全教育概述",
            "视频进度 | 15.3% (92/601s) | 剩余约4分15秒（2x） | 章节 3/244",
            "章节检测已提交（共 10 题，题库命中 8 题）",
        ]
        for msg in noise + keep:
            _logger.info(msg)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            app.root.update()
            panel = app.log_text.get("1.0", "end")
            if all(m in panel for m in keep):
                break
            time.sleep(0.03)

        panel = app.log_text.get("1.0", "end")
        for msg in keep:
            assert msg in panel, f"正常日志被隐藏了: {msg}"
        for msg in noise:
            assert msg not in panel, f"技术噪音出现在面板上: {msg}"
        # 噪音并没有被丢弃：文件侧（TRACE）照样完整记录，便于排查
        file_side = "\n".join(captured)
        for msg in noise:
            assert msg in file_side, f"噪音应写入日志文件: {msg}"
    finally:
        _logger.remove(sink)
        app.root.destroy()


if __name__ == "__main__":
    try:
        test_gui_demo_flow()
        print("PASS  test_gui_demo_flow")
        test_gui_chapter_not_open_display()
        print("PASS  test_gui_chapter_not_open_display")
        test_gui_course_shows_in_progress_while_studying()
        print("PASS  test_gui_course_shows_in_progress_while_studying")
        test_gui_text_not_clipped()
        print("PASS  test_gui_text_not_clipped")
        test_log_panel_hides_noise()
        print("PASS  test_log_panel_hides_noise")
        sys.exit(0)
    except AssertionError as exc:
        print(f"FAIL  test_gui_smoke: {exc}")
        annotate_exc("test_gui_smoke.py::test_gui_chapter_not_open_display")
        sys.exit(1)
    except Exception:  # noqa: BLE001
        annotate_exc("test_gui_smoke.py::unexpected")
        raise
