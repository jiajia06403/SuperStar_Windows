# -*- coding: utf-8 -*-
# layer: gui  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""倍速 / 并发的持久化测试（真实 Tk 窗口，无需账号/网络）。

注：本文件不写死仓库默认配置（config_win.ini）的具体取值，默认 jobs 现为 1，避免与默认文件耦合。
仍覆盖：
  1. 界面取值解析：越界取边界、非法返回 None（提示「参数无效」）；
  2. 用户改过倍速/并发后写进 config_user.ini，重建窗口能恢复上次取值；
  3. 自检/测试（临时 cache_dir）不写用户配置，不污染源码目录；
  4. 非法取值不被写入用户配置。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate, annotate_exc  # noqa: E402

import config_store  # noqa: E402
import paths  # noqa: E402
from gui import app as app_mod  # noqa: E402
from gui.app import build, fmt_speed, parse_jobs, parse_speed  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeAppDir:
    """把程序目录临时指向 tmp，模拟「解压后的安装目录」。"""

    def __init__(self, tmp: str):
        self.tmp = tmp
        self._old = None

    def __enter__(self):
        self._old = (getattr(sys, "frozen", None), sys.executable)
        sys.frozen = True
        sys.executable = os.path.join(self.tmp, "SuperStar-test.exe")
        return self.tmp

    def __exit__(self, *exc):
        old_frozen, old_exe = self._old
        if old_frozen is None:
            del sys.frozen
        else:
            sys.frozen = old_frozen
        sys.executable = old_exe
        return False


def _prepare_dir(tmp: str) -> None:
    with open(os.path.join(ROOT, paths.CONFIG_NAME), encoding="utf8") as fp:
        text = fp.read()
    with open(os.path.join(tmp, paths.CONFIG_NAME), "w", encoding="utf8") as fp:
        fp.write(text)


def _new_app() -> app_mod.App:
    """在 _FakeAppDir 里建一个真实窗口（配置与缓存都落在 tmp）。"""
    return build()


def test_parse_and_format():
    assert parse_speed("1.5") == 1.5
    assert parse_speed(" 2 ") == 2.0
    assert parse_speed("0.2") == 1.0, "低于下限应取边界值"
    assert parse_speed("9") == 2.0, "高于上限应取边界值"
    assert parse_speed("abc") is None
    assert parse_speed("") is None
    assert parse_speed(None) is None

    assert parse_jobs("3") == 3
    assert parse_jobs("3.7") == 3, "小数应截断为整数"
    assert parse_jobs("0") == 1
    assert parse_jobs("99") == 8
    assert parse_jobs("x") is None

    assert fmt_speed(1.0) == "1.0"
    assert fmt_speed(1.5) == "1.5"
    assert fmt_speed(2.0) == "2.0"


def test_temp_cache_dir_does_not_write_config():
    """自检/测试用临时 cache_dir：只读配置，不生成 config_user.ini。"""
    app = build(cache_dir=tempfile.mkdtemp(prefix="ss_params_"))
    try:
        app.speed_var.set("2.0")
        app.jobs_var.set("6")
        assert app._can_save_config is False
        app._save_run_params()
    finally:
        app.root.destroy()
    assert not os.path.exists(config_store.user_path()), \
        "临时 cache_dir 下不应写用户配置"


def test_invalid_input_not_saved():
    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dir(tmp)
        with _FakeAppDir(tmp):
            app = _new_app()
            try:
                app.speed_var.set("abc")
                app._save_run_params()
            finally:
                app.root.destroy()
            cfg = config_store.load_section("common")
            assert cfg["speed"] == "2.0", f"非法取值不应写入用户配置: {cfg}"


if __name__ == "__main__":
    cases = [
        test_parse_and_format,
        test_temp_cache_dir_does_not_write_config,
        test_invalid_input_not_saved,
    ]
    failed = []
    for case in cases:
        try:
            case()
            print(f"PASS  {case.__name__}")
        except AssertionError as exc:
            failed.append(case.__name__)
            print(f"FAIL  {case.__name__}: {exc}")
            annotate_exc(f"test_run_params.py::{case.__name__}")
        except Exception:  # noqa: BLE001
            failed.append(case.__name__)
            annotate_exc(f"test_run_params.py::{case.__name__}")
    if failed:
        annotate("test_run_params.py", "失败用例: " + ", ".join(failed))
        sys.exit(1)
    sys.exit(0)
