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


def test_gui_chapter_shows_in_progress_while_studying():
    """正在处理的章节显示「进行中」，处理完/刷新列表后回到完成/待学。"""
    from gui.app import CHAPTER_STYLE, build

    app = build(demo=True, cache_dir=tempfile.mkdtemp(prefix="ss_chstudying_"))
    app.silent = True
    try:
        app.engine.login_demo()
        course = next(c for c in app.engine.list_courses() if c["courseId"] == "C1001")
        points = [
            {"id": "pt1", "title": "第一章", "has_finished": True},
            {"id": "pt2", "title": "第二章", "has_finished": False},
            {"id": "pt3", "title": "第三章", "has_finished": False},
        ]
        app.current_course = course
        app._ev_points(course, points)
        app.root.update_idletasks()

        tree = app.chapter_tree
        studying_text = CHAPTER_STYLE["studying"][0]
        pending_text = CHAPTER_STYLE["pending"][0]
        assert studying_text == "进行中"
        assert tree.set("pt2", "state") == pending_text, "未开始的章节应显示「待学」"

        # 引擎开始处理第二章：该行切「进行中」，其余行不动
        app._ev_chapter("C1001", 1, 3, "第二章")
        assert tree.set("pt2", "state") == studying_text
        assert tree.set("pt3", "state") == pending_text

        # 处理完成：该行切「完成」，「进行中」标记撤销
        app._ev_progress("C1001", "pt2", "done")
        assert tree.set("pt2", "state") == CHAPTER_STYLE["done"][0]
        assert app._studying_pid is None

        # 下一章开始：上一行的「进行中」不残留
        app._ev_chapter("C1001", 2, 3, "第三章")
        assert tree.set("pt3", "state") == studying_text
        assert tree.set("pt2", "state") == CHAPTER_STYLE["done"][0], "上一章不应保持「进行中」"

        # 刷课结束：所有「进行中」复位
        app._ev_finished()
        assert tree.set("pt3", "state") == pending_text
        assert app._studying_pid is None

        # 提示文本不再统计「未开放」
        assert "未开放" not in app.chapter_hint.cget("text"), app.chapter_hint.cget("text")
        assert "已完成 2/3 章" in app.chapter_hint.cget("text"), app.chapter_hint.cget("text")
    finally:
        app.root.destroy()


def test_gui_course_shows_in_progress_while_studying():
    """左侧课程列表把正在处理的课程标「进行中」：可并行多门，结束/切课后清除。"""
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

        app._ev_course(cid1, "start")
        app._ev_chapter(cid1, 0, 5, "第一章")
        assert "进行中" in app.course_tree.item(cid1, "text")
        # cid1 同时也是「选中课程」：单选下整行蓝底优先，故 tag 是 selected
        assert "selected" in app.course_tree.item(cid1, "tags")

        # 另一门课也开始处理：两门同时为「进行中」，未选中的那门用橙色文字标出
        app._ev_course(cid2, "start")
        app._ev_chapter(cid2, 0, 3, "Unit 1")
        assert "进行中" in app.course_tree.item(cid1, "text")
        assert "进行中" in app.course_tree.item(cid2, "text")
        assert "studying" in app.course_tree.item(cid2, "tags")

        # 第一门课处理完：只有它复位
        app._ev_course(cid1, "end")
        assert "进行中" not in app.course_tree.item(cid1, "text")
        assert "studying" not in app.course_tree.item(cid1, "tags")
        assert "进行中" in app.course_tree.item(cid2, "text")

        # 刷课结束：标记全部清除
        app._ev_finished()
        assert "进行中" not in app.course_tree.item(cid2, "text")
        assert app.course_tree.item(cid2, "tags") in ((), "")
    finally:
        app.root.destroy()


def test_gui_course_switch_blocked_while_running():
    """单选 + 刷课中拒绝切课：点其他课程无反应，只在标题右侧短暂提示。"""
    import types

    from gui.app import COURSE_PANE_TITLE, COURSE_SWITCH_BLOCKED, build

    app = build(demo=True, cache_dir=tempfile.mkdtemp(prefix="ss_switch_"))
    app.silent = True
    try:
        app.engine.login_demo()
        courses = app.engine.list_courses()
        app._ev_courses(courses)
        app.root.update_idletasks()
        cid1 = str(courses[0]["courseId"])
        cid2 = str(courses[1]["courseId"])
        assert app.selected_cid == cid1, f"默认应选中第一门，实际 {app.selected_cid}"
        assert app.current_course and str(app.current_course["courseId"]) == cid1

        # 让界面认为「正在刷课」（只改 is_running 的判据，不真跑引擎）
        class _Alive:
            def is_alive(self):
                return True

        app.engine._thread = _Alive()
        # 命中测试由 Tk 做（窗口未上屏时 bbox 为空），这里直接指定「点在第二行」
        orig_identify = app.course_tree.identify_row
        app.course_tree.identify_row = lambda _y: cid2
        try:
            app._on_course_click(types.SimpleNamespace(y=10))

            assert app.selected_cid == cid1, "刷课中点了别的课，选中态不能改变"
            assert str(app.current_course["courseId"]) == cid1, "章节面板不能切走"
            assert COURSE_SWITCH_BLOCKED in app._course_box.cget("text"), \
                f"标题右侧应出现提示，实际 {app._course_box.cget('text')!r}"

            # 键盘上下键同理：选中被拨回原课程
            app.course_tree.selection_set(cid2)
            app._on_course_select()
            assert app.selected_cid == cid1
            assert cid1 in app.course_tree.selection()

            # 刷课结束后即可正常切换
            app.engine._thread = None
            app._on_course_click(types.SimpleNamespace(y=10))
            assert app.selected_cid == cid2
        finally:
            app.course_tree.identify_row = orig_identify
        app._flash_course_hint("x", ms=1)
        app.root.update()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and app._course_box.cget("text") != COURSE_PANE_TITLE:
            app.root.update()
            time.sleep(0.02)
        assert app._course_box.cget("text") == COURSE_PANE_TITLE, "提示应在超时后自动消失"
    finally:
        app.root.destroy()


def test_gui_manual_prompt_acks_engine():
    """「需手动」弹窗：界面收到 PROMPT 后弹框，关闭即 ack，引擎才开始计时。

    静默模式（自检/截图）不弹框但必须照常 ack，否则引擎线程会永远卡在等确认。
    """
    import threading

    from gui.app import build

    app = build(demo=True, cache_dir=tempfile.mkdtemp(prefix="ss_prompt_"))
    app.silent = True
    ack = threading.Event()
    app.engine._manual_acks.append(ack)      # 模拟引擎线程正阻塞等这个确认
    try:
        # 走真实链路：引擎事件 -> 队列 -> 主线程弹窗 -> 关闭后 ack
        app.engine._emit(app.engine.PROMPT, "上一章", "当前章", 300)
        assert _pump_until(app, lambda: ack.is_set(), timeout=5), \
            "弹窗关闭后必须 ack，否则刷课线程卡死"
        assert app._manual_prompt_open is False and not app._manual_prompts

        # 排队：同时有两条请求时逐个处理，每条都要放行
        ack2, ack3 = threading.Event(), threading.Event()
        app.engine._manual_acks.extend([ack2, ack3])
        app.engine._emit(app.engine.PROMPT, "甲章", "课后章", 300)
        app.engine._emit(app.engine.PROMPT, "乙章", "课后章", 300)
        assert _pump_until(app, lambda: ack2.is_set() and ack3.is_set(), timeout=5), \
            f"排队的两条请求都应被放行: {ack2.is_set()}/{ack3.is_set()}"
    finally:
        app.engine._manual_acks.clear()
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

        # 1) 状态列必须放得下最长的状态文本（进行中）
        need = max(font.measure(t) for t, _c in CHAPTER_STYLE.values())
        col_w = app.chapter_tree.column("state", "width")
        assert col_w >= need + 12, f"状态列 {col_w}px 放不下 {need}px 的文本"

        # 2) 章节提示超宽时要加省略号，且不超出控件
        app._set_hint("上海市大学生安全教育（2026级） · 已完成 2/244 章 · 进行中 1 章" * 3)
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
        test_gui_chapter_shows_in_progress_while_studying()
        print("PASS  test_gui_chapter_shows_in_progress_while_studying")
        test_gui_course_shows_in_progress_while_studying()
        print("PASS  test_gui_course_shows_in_progress_while_studying")
        test_gui_course_switch_blocked_while_running()
        print("PASS  test_gui_course_switch_blocked_while_running")
        test_gui_manual_prompt_acks_engine()
        print("PASS  test_gui_manual_prompt_acks_engine")
        test_gui_text_not_clipped()
        print("PASS  test_gui_text_not_clipped")
        test_log_panel_hides_noise()
        print("PASS  test_log_panel_hides_noise")
        sys.exit(0)
    except AssertionError as exc:
        print(f"FAIL  test_gui_smoke: {exc}")
        annotate_exc("test_gui_smoke.py::test_gui_chapter_shows_in_progress_while_studying")
        sys.exit(1)
    except Exception:  # noqa: BLE001
        annotate_exc("test_gui_smoke.py::unexpected")
        raise
