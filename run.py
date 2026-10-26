# -*- coding: utf-8 -*-
"""SuperStar 桌面客户端启动入口。

用法（发行包只支持第 1 种）：
    SuperStar-<版本>.exe        # 正常启动
    SuperStar-<版本>.exe --demo     # 演示模式（仅测试包）
    SuperStar-<版本>.exe --selftest # 界面自检（仅测试包）

演示/自检依赖内置的 demo_chaoxing 模块，发行包在构建时已排除该模块，
因此相关参数在发行包中会直接以退出码 3 拒绝。

发行包按「整个文件夹」分发（exe + config_win.ini + chaoxing.log + progress_cache/），
注意事项写在随包的使用说明.txt 里；程序本身不校验、不弹窗。
"""
import os
import sys

EXIT_DEMO_UNAVAILABLE = 3


try:  # 必须在任何 GUI / api 模块导入之前补齐标准流
    from safeio import ensure_stdio
except Exception:  # noqa: BLE001
    ensure_stdio = None


def _force_utf8_stdio() -> None:
    """统一 stdio 编码：旧 Windows 控制台用 cp1252 等本地代码页，打印中文会 UnicodeEncodeError。

    发行包是 GUI（--windowed），从控制台启动时 stdio 仍可能是 None，统一容错处理。
    """
    if ensure_stdio is not None:
        ensure_stdio()
    for stream in (sys.stderr, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


def _tell(msg: str) -> None:
    """把提示写到 stderr；写不进去（无控制台 / 编码异常）也不得影响退出码。"""
    try:
        if sys.stderr is not None:
            sys.stderr.write(msg)
            sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def _exe_name() -> str:
    """当前可执行文件名（打包态带版本号；源码态给出版本化示例名）。"""
    if getattr(sys, "frozen", False):
        return os.path.basename(sys.executable)
    return "SuperStar-<版本>.exe"


def main() -> None:
    _force_utf8_stdio()
    args = sys.argv[1:]
    wants_demo = "--demo" in args or "--selftest" in args

    if wants_demo:
        from gui.app import demo_available
        if not demo_available():
            # 退出码必须稳定为 3：写提示失败（无控制台/编码问题）也不能改变它
            _tell("当前为发行包，未内置演示/自检能力。\n")
            sys.exit(EXIT_DEMO_UNAVAILABLE)

    if "--selftest" in args:
        from gui.selftest import run_selftest
        stats = run_selftest()
        _tell(f"SELFTEST={'PASS' if stats.get('passed') else 'FAIL'} {stats}\n")
        sys.exit(0 if stats.get("passed") else 1)

    from gui.app import main as gui_main
    gui_main(demo="--demo" in args)


if __name__ == "__main__":
    main()
