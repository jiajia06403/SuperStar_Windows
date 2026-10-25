# -*- coding: utf-8 -*-
"""路径约定测试：发行包不往 exe 目录里凭空写文件。

校验三件事：
  1. 运行期文件都固定在「程序目录」下（打包态 = exe 所在目录）；
  2. 打包态路径解析不创建目录、不校验（没有启动告警，注意事项写在随包使用说明里）；
  3. 构建产物（解压后的文件夹 / zip）自带 config_win.ini、chaoxing.log、progress_cache/；
  4. 版本号唯一来源是 version.txt（exe / zip / release tag 均由它推导）。
"""
import os
import re
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate, annotate_exc  # noqa: E402

import paths  # noqa: E402
import build  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# exe 文件名与 zip 顶层目录随版本号变化（version.txt 是唯一来源），这里统一推导
EXE_NAME = getattr(build, "EXE_NAME", f"{build.APP_NAME}.exe")
ZIP_PREFIX = f"{build.APP_NAME}-{build.VERSION}/"


def _fake_frozen(exe_path: str):
    """临时把解释器伪装成 PyInstaller 打包态。"""
    old_frozen, old_exe = getattr(sys, "frozen", None), sys.executable
    sys.frozen = True
    sys.executable = exe_path
    return old_frozen, old_exe


def _restore_frozen(old):
    old_frozen, old_exe = old
    if old_frozen is None:
        del sys.frozen
    else:
        sys.frozen = old_frozen
    sys.executable = old_exe


def test_dev_paths():
    assert paths.app_dir() == ROOT, paths.app_dir()
    assert paths.config_path() == os.path.join(ROOT, "config_win.ini")
    assert paths.log_path() == os.path.join(ROOT, "chaoxing.log")
    assert paths.data_dir() == os.path.join(ROOT, "progress_cache")
    assert paths.cookies_path() == os.path.join(ROOT, "progress_cache", "cookies.txt")


def test_frozen_paths():
    """打包态：所有运行期文件都定位到 exe 所在目录（不校验、不告警）。"""
    with tempfile.TemporaryDirectory() as tmp:
        exe = os.path.join(tmp, build.EXE_NAME)
        open(exe, "wb").close()
        old = _fake_frozen(exe)
        try:
            assert paths.app_dir() == tmp
            assert paths.config_path() == os.path.join(tmp, "config_win.ini")
            assert paths.log_path() == os.path.join(tmp, "chaoxing.log")
            assert paths.data_dir() == os.path.join(tmp, "progress_cache")
            assert paths.cookies_path() == os.path.join(tmp, "progress_cache", "cookies.txt")
            # 目录不存在时也只解析路径，不擅自创建（避免凭空在 exe 旁建文件夹）
            assert not os.path.exists(paths.data_dir())
        finally:
            _restore_frozen(old)


def test_stage_install_contents():
    with tempfile.TemporaryDirectory() as tmp:
        exe = os.path.join(tmp, "SuperStar.exe")
        open(exe, "wb").close()
        target = os.path.join(tmp, "pkg")
        entries = build.stage_install(ROOT, target, exe)

        assert build.EXE_NAME in entries, entries
        assert build.VERSION in build.EXE_NAME, "exe 文件名必须带版本号"
        assert os.path.isfile(os.path.join(target, build.EXE_NAME))
        assert build.CONFIG_NAME in entries
        assert build.LOG_NAME in entries
        assert build.DATA_DIRNAME + os.sep in entries
        assert build.README_NAME in entries
        assert build.LICENSE_NAME in entries
        assert os.path.isdir(os.path.join(target, build.DATA_DIRNAME))
        assert os.path.getsize(os.path.join(target, build.LOG_NAME)) == 0
        with open(os.path.join(target, build.README_NAME), encoding="utf8") as fp:
            text = fp.read()
        assert "完整解压" in text, "使用说明必须提示解压整个文件夹后运行"
        assert "progress_cache" in text
        # GPL-3.0：二进制分发必须随附完整许可文本
        with open(os.path.join(target, build.LICENSE_NAME), encoding="utf8") as fp:
            lic = fp.read()
        assert "GNU GENERAL PUBLIC LICENSE" in lic
        assert "Version 3, 29 June 2007" in lic


def test_version_file_is_single_source():
    """版本号只存在于 version.txt：构建脚本里不得再硬编码。"""
    version = build.read_version(ROOT)
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"版本号格式应为 X.Y.Z：{version!r}"
    assert version == build.VERSION
    assert build.EXE_NAME == f"{build.APP_NAME}-{version}.exe"
    with open(os.path.join(ROOT, "build.py"), encoding="utf8") as fp:
        src = fp.read()
    assert 'VERSION = "' not in src, "build.py 不应再硬编码版本号"
    assert build.VERSION_FILE in src


def test_missing_version_file_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            build.read_version(tmp)
        except SystemExit as exc:
            assert build.VERSION_FILE in str(exc), exc
        else:
            raise AssertionError("缺少 version.txt 时应直接报错退出")


def test_zip_layout():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "proj")
        os.makedirs(root)
        with open(os.path.join(root, build.CONFIG_NAME), "w", encoding="utf8") as fp:
            fp.write("[tiku]\nprovider=\n")
        # 使用说明为主目录静态文件，构建时直接随包复制
        shutil.copy2(os.path.join(ROOT, build.README_NAME),
                     os.path.join(root, build.README_NAME))
        shutil.copy2(os.path.join(ROOT, build.LICENSE_NAME),
                     os.path.join(root, build.LICENSE_NAME))
        exe = os.path.join(tmp, "SuperStar.exe")
        open(exe, "wb").close()

        zip_path = build.make_zip(root, exe)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        prefix = f"{build.APP_NAME}-{build.VERSION}/"
        for expect in (build.EXE_NAME, build.CONFIG_NAME, build.LOG_NAME,
                       build.DATA_DIRNAME + "/", build.README_NAME,
                       build.LICENSE_NAME):
            assert prefix + expect in names, f"zip 缺少 {expect}: {names}"
        assert not any(n.startswith("dist_stage") for n in names), names


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
            annotate_exc(f"test_paths.py::{fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: unexpected")
            annotate_exc(f"test_paths.py::{fn.__name__}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
