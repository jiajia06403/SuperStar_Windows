# -*- coding: utf-8 -*-
# layer: slow  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""路径约定测试：发行包不往 exe 目录里凭空写文件。

校验三件事：
  1. 运行期文件都固定在「程序目录」下（打包态 = exe 所在目录）；
  2. 打包态路径解析不创建目录、不校验（没有启动告警，注意事项写在随包使用说明里）；
  3. 构建产物（解压后的文件夹 / zip）只带 config_win.ini 与说明/协议，
     不预置 chaoxing.log、progress_cache/ 这类运行期产物（首次运行时由程序自建）；
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
# exe 文件名恒定（不带版本号），这里统一推导
EXE_NAME = getattr(build, "EXE_NAME", f"{build.APP_NAME}.exe")


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


def _fake_app_dir(tmp: str) -> str:
    """造一个 onedir 产物目录：SuperStar.exe + runtime/（bootloader 按 exe 同级找运行时）。"""
    app = os.path.join(tmp, "app")
    os.makedirs(app, exist_ok=True)
    with open(os.path.join(app, build.EXE_NAME), "wb") as fp:
        fp.write(b"MZ")
    runtime = os.path.join(app, build.RUNTIME_DIRNAME)
    os.makedirs(os.path.join(runtime, "sub"), exist_ok=True)
    with open(os.path.join(runtime, "python312.dll"), "wb") as fp:
        fp.write(b"\x00")
    with open(os.path.join(runtime, "sub", "data.bin"), "wb") as fp:
        fp.write(b"\x01")
    return app


def test_stage_install_contents():
    with tempfile.TemporaryDirectory() as tmp:
        app = _fake_app_dir(tmp)
        target = os.path.join(tmp, "pkg")
        entries = build.stage_install(ROOT, target, app)

        assert build.EXE_NAME in entries, entries
        assert build.EXE_NAME == f"{build.APP_NAME}.exe", "exe 文件名不得带版本号"
        assert os.path.isfile(os.path.join(target, build.EXE_NAME))
        assert build.CONFIG_NAME in entries
        assert build.README_NAME in entries
        assert build.LICENSE_NAME in entries
        # 运行期产物不预置：首次运行时由程序自己创建。
        # （预置空 chaoxing.log 会在覆盖升级时把用户已有日志清成 0 字节）
        assert paths.LOG_NAME not in entries, entries
        assert not os.path.exists(os.path.join(target, paths.LOG_NAME))
        assert not os.path.exists(os.path.join(target, paths.DATA_DIRNAME))
        # 运行时目录必须与 exe 平铺同级：bootloader 只在 exe 同级找它
        assert build.RUNTIME_DIRNAME + os.sep in entries, entries
        runtime = os.path.join(target, build.RUNTIME_DIRNAME)
        assert os.path.isdir(runtime)
        assert os.path.isfile(os.path.join(runtime, "python312.dll"))
        assert os.path.isfile(os.path.join(runtime, "sub", "data.bin"))
        with open(os.path.join(target, build.README_NAME), encoding="utf8") as fp:
            text = fp.read()
        assert "完整解压" in text, "使用说明必须提示解压整个文件夹后运行"
        assert "progress_cache" in text
        # GPL-3.0：二进制分发必须随附完整许可文本
        with open(os.path.join(target, build.LICENSE_NAME), encoding="utf8") as fp:
            lic = fp.read()
        assert "GNU GENERAL PUBLIC LICENSE" in lic
        assert "Version 3, 29 June 2007" in lic


# 允许预发布后缀：1.0.0 / 1.0.0-rc.1 / 1.2.0-rc3
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")


def test_version_file_is_single_source():
    """版本号只存在于 version.txt：构建脚本里不得再硬编码。"""
    version = build.read_version(ROOT)
    assert VERSION_RE.fullmatch(version), f"版本号格式应为 X.Y.Z[-预发布]：{version!r}"
    assert version == build.VERSION
    assert build.EXE_NAME == f"{build.APP_NAME}.exe"
    with open(os.path.join(ROOT, "build.py"), encoding="utf8") as fp:
        src = fp.read()
    assert 'VERSION = "' not in src, "build.py 不应再硬编码版本号"
    assert build.VERSION_FILE in src


def test_prerelease_suffix_kept_in_zip_name():
    """预发布后缀（1.0.0-rc.1）必须出现在 zip 名里，否则预览版与正式版重名；
    exe 文件名恒为 SuperStar.exe（不带版本号），版本区分交给 zip 名承担。"""
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, build.VERSION_FILE), "w", encoding="utf8") as fp:
            fp.write("1.2.0-rc.1\n")
        version = build.read_version(tmp)
        assert version == "1.2.0-rc.1", version
        assert build.is_prerelease(version)
        assert build.exe_name() == "SuperStar.exe"
        assert build.zip_name(version) == "SuperStar-1.2.0-rc.1-win64.zip"
        # 正式版不带后缀，且不能被判成预发布
        assert not build.is_prerelease("1.2.0")
        assert build.exe_name() == "SuperStar.exe"
    # 非法版本号直接拒绝（避免 rc 后缀被静默丢弃）
    for bad in ("1.0", "v1.0.0", "1.0.0-rc 1", "1.0.0_1", ""):
        try:
            build.parse_version(bad)
        except SystemExit:
            pass
        else:
            raise AssertionError(f"非法版本号应被拒绝：{bad!r}")


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
        # 安装目录 = onedir 产物目录（PyInstaller 直接落在这里）
        install = _fake_app_dir(tmp)
        build.stage_install(root, install, install)

        zip_path = build.make_zip(root, install)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        # zip 内不得再套一层顶层目录：解压即得主目录
        for expect in (build.EXE_NAME, build.CONFIG_NAME, build.README_NAME,
                       build.LICENSE_NAME):
            assert expect in names, f"zip 缺少 {expect}: {names}"
        assert not any(n.startswith("dist_stage") for n in names), names
        # 顶层只能是这几个条目（runtime/ 是唯一的子目录）
        top = {n.rstrip("/") for n in names if "/" not in n.rstrip("/")}
        assert top == {build.EXE_NAME, build.CONFIG_NAME, build.README_NAME,
                       build.LICENSE_NAME}, top
        # 运行时目录整棵子树都要进 zip（缺文件 exe 起不来）
        runtime = build.RUNTIME_DIRNAME + "/"
        assert any(n.startswith(runtime) for n in names), names
        assert f"{runtime}python312.dll" in names, names
        assert f"{runtime}sub/data.bin" in names, names


def test_zip_excludes_runtime_artifacts():
    """打包目录被跑过一次后留下的日志/数据/用户配置，绝不能进 zip。

    否则「解压覆盖升级」会拿本机跑测试时的旧日志、旧账号、旧题库盖掉用户自己的数据。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "proj")
        os.makedirs(root)
        with open(os.path.join(root, build.CONFIG_NAME), "w", encoding="utf8") as fp:
            fp.write("[tiku]\nprovider=\n")
        shutil.copy2(os.path.join(ROOT, build.README_NAME),
                     os.path.join(root, build.README_NAME))
        shutil.copy2(os.path.join(ROOT, build.LICENSE_NAME),
                     os.path.join(root, build.LICENSE_NAME))
        install = _fake_app_dir(tmp)
        build.stage_install(root, install, install)
        # 模拟「产物目录被运行过」：产生日志、数据目录（含账号）、用户配置
        with open(os.path.join(install, paths.LOG_NAME), "w", encoding="utf8") as fp:
            fp.write("本机跑过留下的日志\n")
        data_dir = os.path.join(install, paths.DATA_DIRNAME)
        os.makedirs(os.path.join(data_dir, "local_bank"), exist_ok=True)
        with open(os.path.join(data_dir, "accounts.json"), "w", encoding="utf8") as fp:
            fp.write("{}")
        with open(os.path.join(install, paths.USER_CONFIG_NAME), "w", encoding="utf8") as fp:
            fp.write("[tiku]\nprovider=ai\n")

        with zipfile.ZipFile(build.make_zip(root, install)) as zf:
            names = zf.namelist()
        leaked = [n for n in names
                  if n.split("/")[0] in (paths.LOG_NAME, paths.DATA_DIRNAME,
                                         paths.USER_CONFIG_NAME)]
        assert not leaked, f"运行期产物被打进 zip 了: {leaked}"
        # 正常内容仍在
        assert build.EXE_NAME in names and build.CONFIG_NAME in names, names


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
