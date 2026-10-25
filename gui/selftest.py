# -*- coding: utf-8 -*-
"""界面自检：在演示模式下真实驱动整套 UI（建窗口 → 勾选课程 → 刷课 → 校验控件状态）。

用途：
  - ``SuperStar.exe --selftest``：验证打包产物里的界面与引擎接线可用；
  - ``python test/test_gui_smoke.py``：开发态回归。

不产生图片、不依赖第三方库；返回值中的 ``passed`` 表示断言是否全部成立。

时序说明（重要）：
  演示登录的结果是通过 ``queue.Queue`` 交给主线程事件泵渲染的，课程列表、章节状态
  （含「章节完成」标记的最后一帧）都晚于线程结束那一刻才落到界面上。
  因此自检不能用固定延时去猜「什么时候界面就绪」，必须轮询状态：
    ① 等课程列表上屏 → ② 才开始刷课 → ③ 等引擎停 → ④ 等事件队列排空并稳定 → ⑤ 校验。
  固定延时的版本在慢机器 / CI 上会偶发误判（界面还没更新就断言）。
"""
import tempfile
import time

from gui.app import CHAPTER_STYLE, build

DONE_TEXT = CHAPTER_STYLE["done"][0]

POLL_MS = 100          # 状态轮询间隔（毫秒）
SETTLE_TICKS = 3       # 队列排空后需连续稳定的轮数，确保最后几帧界面事件已处理
FAIL_LABEL = {
    "courses": "课程列表为空（演示登录未上屏）",
    "chapters": "章节列表为空",
    "done": "存在未完成章节",
    "log_lines": "日志面板为空",
    "start_enabled": "刷课结束后「开始刷课」未恢复可用",
    "running": "刷课结束后引擎仍在运行",
    "layout_ok": "界面布局被裁切（日志面板/列表不可见）",
}


def run_selftest(timeout_s: float = 30.0, poll_ms: int = POLL_MS) -> dict:
    """跑一次完整的演示流程并校验界面状态（使用临时进度缓存，不污染用户数据）。"""
    app = build(demo=True, cache_dir=tempfile.mkdtemp(prefix="ss_selftest_"))
    app.silent = True
    stats: dict = {}
    deadline = time.monotonic() + timeout_s
    started = False
    empty_ticks = 0

    def expired() -> bool:
        return time.monotonic() > deadline

    # ---------------- 最终校验 ----------------
    def verify() -> None:
        courses = app.course_tree.get_children()
        chapters = app.chapter_tree.get_children()
        done = sum(1 for p in chapters if app.chapter_tree.set(p, "state") == DONE_TEXT)
        mapped = bool(app.root.winfo_viewable())
        root_bottom = app.root.winfo_rooty() + app.root.winfo_height()
        log_bottom = app.log_text.winfo_rooty() + app.log_text.winfo_height()
        stats.update({
            "courses": len(courses),
            "checked": len(app.checked),
            "chapters": len(chapters),
            "done": done,
            "log_lines": int(app.log_text.index("end-1c").split(".")[0]),
            "status": app.status_var.get(),
            "running": app.engine.is_running,
            "start_enabled": str(app.start_btn["state"]) != "disabled",
            # 布局完整性：日志面板不得被窗口底边裁切，两个列表需有可见高度
            "layout_boxes": (
                app.course_tree.winfo_height(),
                app.chapter_tree.winfo_height(),
                log_bottom - root_bottom,
            ),
            "mapped": mapped,
        })
        layout_boxes = stats["layout_boxes"]
        if mapped:
            stats["layout_ok"] = bool(layout_boxes[0] > 80 and layout_boxes[1] > 80
                                      and layout_boxes[2] <= 0)
        else:
            # 无头环境下窗口从未真正上屏，量到的高度无意义，跳过布局断言
            stats["layout_ok"] = True
            stats["layout_skipped"] = True

        checks = {
            "courses": stats["courses"] > 0,
            "chapters": stats["chapters"] > 0,
            "done": stats["done"] == stats["chapters"],
            "log_lines": stats["log_lines"] > 0,
            "start_enabled": stats["start_enabled"],
            "running": not stats["running"],
            "layout_ok": stats["layout_ok"],
        }
        stats["failed_checks"] = [FAIL_LABEL[k] for k, ok in checks.items() if not ok]
        stats["passed"] = not stats["failed_checks"]
        app.root.destroy()

    # ---------------- 状态机轮询 ----------------
    def tick() -> None:
        nonlocal started, empty_ticks

        # ① 等课程列表上屏后再全选开刷（演示登录结果经事件泵渲染，不能按固定延时猜）
        if not started:
            app.root.update_idletasks()
            if app.courses and app.course_tree.get_children():
                app._set_all_checked(True)
                stats["checked"] = len(app.checked)
                app.on_start()
                started = True
                empty_ticks = 0
            elif expired():
                started = True  # 超时也进下一步，verify 会如实记录失败原因
            app.root.after(poll_ms, tick)
            return

        # ② 等刷课线程结束
        if app.engine.is_running:
            empty_ticks = 0
            if expired():
                app.engine.stop()
            app.root.after(poll_ms, tick)
            return

        # ③ 线程结束 ≠ 界面已更新：等事件队列排空并稳定几轮，再读界面状态
        if app.events.empty():
            empty_ticks += 1
        else:
            empty_ticks = 0
        if empty_ticks >= SETTLE_TICKS or expired():
            verify()
            return
        app.root.after(poll_ms, tick)

    app.root.after(poll_ms, tick)
    app.root.mainloop()
    return stats
