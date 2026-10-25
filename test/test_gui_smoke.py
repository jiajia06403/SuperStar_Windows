# -*- coding: utf-8 -*-
"""GUI 冒烟测试：在真实 Tk 窗口中跑一遍演示流程，校验控件状态。

无需账号与网络（使用 DemoChaoxing 假后端 + 临时进度缓存）。
"""
import os
import sys

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


if __name__ == "__main__":
    try:
        test_gui_demo_flow()
        print("PASS  test_gui_demo_flow")
        sys.exit(0)
    except AssertionError as exc:
        print(f"FAIL  test_gui_demo_flow: {exc}")
        annotate_exc("test_gui_smoke.py::test_gui_demo_flow")
        sys.exit(1)
    except Exception:  # noqa: BLE001
        annotate_exc("test_gui_smoke.py::unexpected")
        raise
