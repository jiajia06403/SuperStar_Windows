# -*- coding: utf-8 -*-
"""全局「停止」信号：让长耗时任务点能即时中断。

刷课跑在后台线程里，界面的「停止」只是置一个标记。像视频任务点动辄几十
分钟、直播按分钟循环，如果这些循环里不检查标记，点了停止界面就会一直卡在
「停止中」——用户看到的正是这个现象。

本模块提供一个进程级 ``threading.Event``：

    from api import cancel

    while not passed:
        cancel.check()          # 已停止 -> 抛 Cancelled，由上层收尾
        ...

引擎在 ``start_study`` 时 :func:`clear`，在 ``stop``（以及关窗）时 :func:`set`。
中断用异常而不是返回值，是因为调用链很深（process_job -> study_video），
逐层返回需要改动每一层的签名。
"""
import threading


class Cancelled(Exception):
    """收到停止信号。"""


_event = threading.Event()


def set() -> None:  # noqa: A001 - 与 threading.Event 保持同名，便于心智一致
    """请求停止（停止按钮 / 关闭窗口）。"""
    _event.set()


def clear() -> None:
    """开始新一轮刷课前清掉停止标记。"""
    _event.clear()


def is_set() -> bool:
    return _event.is_set()


def check() -> None:
    """已请求停止则抛 :class:`Cancelled`；循环里每轮调一次。"""
    if _event.is_set():
        raise Cancelled()


def wait(timeout: float) -> bool:
    """可中断的 sleep：返回 True 表示在等待期间被停止。"""
    return _event.wait(timeout)
