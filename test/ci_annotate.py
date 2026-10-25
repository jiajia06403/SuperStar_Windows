# -*- coding: utf-8 -*-
"""CI 失败详情上报：把断言失败/异常堆栈写进 GitHub Actions Annotations。

测试脚本各自打印的 stdout 在 Actions 日志里能被看到，但失败往往只留下一个退出码。
这里额外输出一行 ``::error::`` workflow command，GitHub 会把它提升为 Annotations，
无需翻日志即可知道「哪个用例、报了什么」。
"""
import os
import sys
import traceback


def force_utf8_stdio() -> None:
    """统一 stdio 编码：Windows 旧控制台用 cp1252 等本地代码页，打印中文日志/文件名会 UnicodeEncodeError。

    CI runner 上 Python 的 stdout 默认就是 cp1252，测试脚本里的中文日志行会直接把进程带崩。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


# 任何 import 本模块的测试脚本都先修一遍 stdio（Windows 控制台编码不可控）
force_utf8_stdio()


def annotate(title: str, text: str) -> None:
    """写一条 error 注解（title 为短标题，text 可多行，会被转义成单行）。"""
    msg = text.strip().replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if msg:
        msg = msg[:2000]
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::error title={title}::{msg}")
    else:
        print(f"[error] {title}\n{text.strip()}")


def annotate_exc(title: str) -> None:
    """把当前正在处理的异常转为一条 error 注解。"""
    annotate(title, traceback.format_exc())
