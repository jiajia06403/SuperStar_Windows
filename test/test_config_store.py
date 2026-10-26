# -*- coding: utf-8 -*-
# layer: core  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""配置防覆盖与升级校验测试（config_store）。

注：本文件不校验「仓库默认配置（config_win.ini）的具体取值」——默认取值会在构建/迭代中调整，
避免测试与默认文件写死耦合。以下事项仍被覆盖：
  1. 升级后：新增键补齐、已删除键转入 [obsolete]、取值非法回落默认值；
  2. 迁移幂等：重复校验不再产生变化；
  3. 读取为「默认配置打底 + 用户配置覆盖」，只写用户配置（默认配置不变）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate, annotate_exc  # noqa: E402

import config_store  # noqa: E402
import paths  # noqa: E402

DEFAULT_V1 = """\
; 默认配置 v1（注释应被保留）
[tiku]
provider = SiliconFlow
submit = true
cover_rate = 0
delay = 1.0
siliconflow_key =

[notification]
provider =
url =
"""

DEFAULT_V2 = """\
; 默认配置 v2：新增 work_retry，删除 delay
[tiku]
provider = SiliconFlow
submit = true
cover_rate = 0
work_retry = 1
siliconflow_key =

[notification]
provider =
url =
"""


class _FakeAppDir:
    """把程序目录临时指向 tmp，模拟「解压后的安装目录」。"""

    def __init__(self, tmp: str):
        self.tmp = tmp
        self._old = None

    def __enter__(self):
        self._old = (getattr(sys, "frozen", None), sys.executable)
        sys.frozen = True
        sys.executable = os.path.join(self.tmp, "SuperStar-1.2.3.exe")
        return self.tmp

    def __exit__(self, *exc):
        old_frozen, old_exe = self._old
        if old_frozen is None:
            del sys.frozen
        else:
            sys.frozen = old_frozen
        sys.executable = old_exe
        return False


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf8") as fp:
        fp.write(text)


def _read(path: str) -> str:
    with open(path, encoding="utf8") as fp:
        return fp.read()


def test_upgrade_does_not_overwrite_user_values():
    """「解压覆盖默认配置」= 直接重写 config_win.ini；用户填的值必须还在。"""
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, paths.CONFIG_NAME), DEFAULT_V1)
        with _FakeAppDir(tmp):
            config_store.sync_with_default()
            config_store.save_section("tiku", {"provider": "AI",
                                               "siliconflow_key": "sk-my-key"})
            # 模拟升级：解压覆盖默认配置
            _write(os.path.join(tmp, paths.CONFIG_NAME), DEFAULT_V2)
            report = config_store.sync_with_default()
            assert report["created"] is False, report
            assert report["added"]["tiku"] == ["work_retry"], report
            assert report["obsolete"]["tiku"] == ["delay"], report
            cfg = config_store.load_section("tiku")
            assert cfg["provider"] == "AI", cfg          # 用户值保住
            assert cfg["siliconflow_key"] == "sk-my-key", cfg
            assert cfg["work_retry"] == "1", cfg         # 新增项补为默认值
            assert "delay" not in cfg, cfg               # 已移除项不再生效
            # 旧值保留在 [obsolete]，键名形如 原段.键
            text = _read(config_store.user_path())
            assert "[obsolete]" in text and "tiku.delay" in text, text
            assert "sk-my-key" in text


def test_invalid_values_fall_back_to_default():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, paths.CONFIG_NAME), DEFAULT_V2)
        with _FakeAppDir(tmp):
            config_store.sync_with_default()
            config_store.save_section("tiku", {"cover_rate": "abc",
                                               "submit": "yes",
                                               "work_retry": "-2"})
            report = config_store.sync_with_default()
            fixed = report["fixed"]["tiku"]
            assert fixed["cover_rate"] == ("abc", "0"), fixed
            assert fixed["submit"] == ("yes", "true"), fixed   # 回落到默认配置的取值
            assert fixed["work_retry"] == ("-2", "1"), fixed
            cfg = config_store.load_section("tiku")
            assert cfg["cover_rate"] == "0" and cfg["submit"] == "true"
            assert cfg["work_retry"] == "1"
            # 空值合法（留空即停用），不得被改写
            config_store.save_section("tiku", {"provider": ""})
            assert config_store.sync_with_default()["fixed"] == {}
            assert config_store.load_section("tiku")["provider"] == ""


def test_migration_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, paths.CONFIG_NAME), DEFAULT_V1)
        with _FakeAppDir(tmp):
            config_store.sync_with_default()
            _write(os.path.join(tmp, paths.CONFIG_NAME), DEFAULT_V2)
            first = config_store.sync_with_default()
            assert first["changed"] is True, first
            snapshot = _read(config_store.user_path())
            second = config_store.sync_with_default()
            assert second["changed"] is False, second
            assert _read(config_store.user_path()) == snapshot


def test_default_config_never_written():
    with tempfile.TemporaryDirectory() as tmp:
        default_file = os.path.join(tmp, paths.CONFIG_NAME)
        _write(default_file, DEFAULT_V2)
        before = _read(default_file)
        with _FakeAppDir(tmp):
            config_store.sync_with_default()
            config_store.save_section("tiku", {"provider": "TikuLike"})
            config_store.save_section("notification", {"url": "https://example.com"})
            assert _read(default_file) == before, "默认配置必须保持只读"
            user_text = _read(config_store.user_path())
        assert "TikuLike" in user_text and "https://example.com" in user_text, user_text


def test_load_falls_back_when_user_config_missing():
    """用户配置被删：不自动重建（无副作用），仍能读到默认配置。"""
    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, paths.CONFIG_NAME), DEFAULT_V2)
        with _FakeAppDir(tmp):
            cfg = config_store.load_section("tiku")
            assert cfg["provider"] == "SiliconFlow", cfg
            assert not os.path.exists(config_store.user_path())


def test_api_fallback_reads_user_config():
    """GUI/引擎不传配置时，api 层的兜底读取也走用户配置（不再是 cwd/config.ini）。"""
    from api.answer import Tiku
    from api.notification import NotificationService

    with tempfile.TemporaryDirectory() as tmp:
        _write(os.path.join(tmp, paths.CONFIG_NAME), DEFAULT_V2)
        with _FakeAppDir(tmp):
            config_store.sync_with_default()
            config_store.save_section("tiku", {"provider": "AI"})
            conf = Tiku()._get_conf()
            assert conf is not None and conf["provider"] == "AI", conf
            noti = NotificationService.__subclasses__()[0]() if NotificationService.__subclasses__() else None
            if noti is not None:
                nconf = noti._load_config_from_file()
                assert nconf is not None and "provider" in nconf, nconf


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {fn.__name__}: {exc}")
            annotate_exc(f"test_config_store.py::{fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: unexpected")
            annotate_exc(f"test_config_store.py::{fn.__name__}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
