# -*- coding: utf-8 -*-
# layer: core  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""无控制台（GUI 打包态）下的 stdio 容错回归测试。

背景：PyInstaller --windowed / pythonw 启动时 sys.stdout、sys.stderr 均为 None，
tqdm 默认往 sys.stderr 写进度条，首次刷新就抛
``AttributeError: 'NoneType' object has no attribute 'write'``，
刷课线程（engine.process_job -> api.base.study_video）直接中断。

验证：
  - safeio.ensure_stdio() 能把缺失的标准流补齐；
  - api.base._progress_stream() 在无控制台时仍返回可写流，tqdm 可正常刷新；
  - api.logger 的 sink（tqdm.write）在无控制台时不抛异常。
"""
import os
import sys
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate_exc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 直接在本进程改 sys.stdout 也行，但 tqdm / loguru 内部会持有引用，
# 用子进程隔离更接近「真 GUI 进程」的环境。


def _run_child(body: str, imports: str = "") -> tuple:
    code = "import sys\n" \
           "sys.stdout = sys.stderr = None\n" \
           f"{imports}\n" \
           f"{body}\n"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stderr or "").strip(), (proc.stdout or "").strip()


def test_tqdm_progress_without_console():
    """没有控制台时 tqdm 进度条必须能正常构造 + 刷新。"""
    code, err, _ = _run_child(
        "from api.base import _progress_stream\n"
        "from tqdm import tqdm\n"
        "pbar = tqdm(total=100, initial=0, desc='x', file=_progress_stream())\n"
        "pbar.n = 50\n"
        "pbar.refresh()\n"
        "pbar.close()\n"
        "assert pbar.fp is not None, 'tqdm 输出流为空'\n"
    )
    assert code == 0, f"无控制台下 tqdm 刷新失败: {err}"


def test_logger_sink_without_console():
    """没有控制台时写日志不得抛异常（logger sink 走 tqdm.write）。"""
    code, err, _ = _run_child(
        "from api.logger import logger\n"
        "logger.info('无控制台日志')\n"
        "assert sys.stderr is not None, 'ensure_stdio 未补齐 stderr'\n",
        imports="import sys",
    )
    assert code == 0, f"无控制台下写日志失败: {err}"


def test_ensure_stdio_replaces_none():
    """ensure_stdio 补齐 None 流，且不动已有流。"""
    code, err, _ = _run_child(
        "import sys\n"
        "from safeio import ensure_stdio, null_writer\n"
        "sentinel = null_writer()\n"
        "sys.stdout = None\n"
        "sys.stderr = sentinel\n"
        "ensure_stdio()\n"
        "assert sys.stdout is not None, 'stdout 未补齐'\n"
        "assert sys.stderr is sentinel, 'stderr 被误替换'\n"
        "nw = null_writer()\n"
        "assert nw is sentinel, 'null_writer 未复用同一实例'\n"
    )
    assert code == 0, f"ensure_stdio 行为异常: {err}"


def test_run_entry_ensure_stdio():
    """run.py 的 _force_utf8_stdio 必须在导入任何业务模块之前补齐标准流。"""
    code, err, _ = _run_child(
        "import sys\n"
        "import run\n"
        "run._force_utf8_stdio()\n"
        "assert sys.stderr is not None, 'stderr 仍为 None'\n"
        "assert sys.stdout is not None, 'stdout 仍为 None'\n"
    )
    assert code == 0, f"run.py 入口未补齐标准流: {err}"


def _main():
    tests = [
        test_tqdm_progress_without_console,
        test_logger_sink_without_console,
        test_ensure_stdio_replaces_none,
        test_run_entry_ensure_stdio,
    ]
    failed = []
    for fn in tests:
        name = fn.__name__
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {name}: {e}")
            annotate_exc(f"test_stdio_safety.py::{name}")
            failed.append(name)
    assert not failed, f"失败用例: {failed}"


if __name__ == "__main__":
    try:
        _main()
        sys.exit(0)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  {e}")
        sys.exit(1)
