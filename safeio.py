# -*- coding: utf-8 -*-
"""GUI 态 stdio 容错。

打包成 GUI（PyInstaller --windowed / pythonw）时进程没有控制台，
``sys.stdout`` / ``sys.stderr`` 会是 ``None``。任何直接写它们的库
（tqdm 默认把进度条写到 sys.stderr）都会抛
``AttributeError: 'NoneType' object has no attribute 'write'``，
进而中断刷课线程。

本模块提供两个兜底：
    ensure_stdio()  进程启动时调用，把缺失的标准流换成指向 os.devnull 的空写对象
    safe_stream()   取第一个可用的标准流，都没有时退回空写对象

两处都用 os.devnull（而不是 StringIO），避免长时间运行把日志堆在内存里。
"""
import io
import os
import sys

_NULL_WRITER = None


def null_writer():
    """一个「写了就丢」的文本流，进程内复用同一个实例。"""
    global _NULL_WRITER
    if _NULL_WRITER is None:
        try:
            _NULL_WRITER = open(os.devnull, "w", encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001  devnull 不可用时退回内存流
            _NULL_WRITER = io.StringIO()
    return _NULL_WRITER


def ensure_stdio() -> None:
    """把为 None 的标准流补齐，使第三方库不会因为「没有控制台」而崩溃。

    幂等；只对当前缺失的流生效，已有控制台时不改动任何东西。
    """
    replacement = None
    for name in ("stdin", "stdout", "stderr"):
        if getattr(sys, name, None) is not None:
            continue
        if name == "stdin":
            try:
                replacement = open(os.devnull, "r", encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                replacement = io.StringIO()
            setattr(sys, name, replacement)
            replacement = None
            continue
        if replacement is None:
            replacement = null_writer()
        setattr(sys, name, replacement)


def safe_stream(*streams):
    """返回第一个非 None 的流；全都没有则返回空写对象。"""
    for stream in streams:
        if stream is not None:
            return stream
    return null_writer()
