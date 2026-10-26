# -*- coding: utf-8 -*-
"""构建 Windows 可执行文件（PyInstaller onefile）并打成分发包。

用法：
    python build.py             # 构建 dist/ 安装目录（exe + 随包文件）并打包 zip
    python build.py --exe-only  # 只出 exe（不铺随包文件、不打 zip）
    python build.py --clean     # 清空中间产物后重新构建

发行包的形态（设计意图：让用户一眼看出「不是单个 exe 就能跑的程序」）：
    SuperStar-<版本>.exe  主程序（文件名带完整版本号，取自 version.txt；
                          预发布形如 SuperStar-1.0.0-rc.1.exe，后缀不可丢）
    SuperStar-<版本>-win64.zip  分发压缩包（同名规则）
    config_win.ini       默认配置（随包自带，升级时覆盖 —— 用户不要改）
                         用户配置 config_user.ini 由程序首次启动生成，不随包分发（防覆盖）
    chaoxing.log         运行日志（随包自带的空文件，追加写入）
    progress_cache\\      进度缓存 / 账号 / cookies（随包自带的空目录）
    使用说明.txt
    LICENSE              GPL-3.0 许可文本（二进制分发须随附）

发行包排除演示后端 demo_chaoxing 与 gui.selftest：界面不出现「演示」入口，
`--demo` / `--selftest` 直接被拒绝（退出码 3）。演示与自检只在源码目录运行，
即 `python run.py --demo` / `python run.py --selftest`。
"""
import os
import re
import shutil
import sys
import zipfile

try:  # 允许测试直接导入本模块的函数，而无需安装 PyInstaller
    import PyInstaller.__main__ as _pyinstaller
except ImportError:  # pragma: no cover
    _pyinstaller = None

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = "version.txt"
APP_NAME = "SuperStar"
# 版本号格式：X.Y.Z 或 X.Y.Z-<预发布>（如 1.0.0-rc.1 / 1.2.0-rc3 / 2.0.0-beta.2）
# 预发布后缀必须写进 version.txt —— exe 名、zip 名、Release tag 全部由它推导，
# 否则预览版会和同号正式版重名（历史坑：tag 是 v1.0.0-rc.1，包却叫 SuperStar-1.0.0.exe）。
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")
# 只在源码/测试时使用的模块，发行包一律排除
TEST_ONLY_MODULES = ("demo_chaoxing", "gui.selftest")

CONFIG_NAME = "config_win.ini"
LOG_NAME = "chaoxing.log"
DATA_DIRNAME = "progress_cache"
README_NAME = "使用说明.txt"
LICENSE_NAME = "LICENSE"


def parse_version(text: str, source: str = VERSION_FILE) -> str:
    """校验并规范化版本号文本（首行去空白）。

    合法：``1.0.0`` / ``1.0.0-rc.1`` / ``1.2.0-rc3`` / ``2.0.0-beta.2``。
    预发布后缀原样保留，绝不在产物名里丢弃。
    """
    version = (text or "").strip()
    if not version:
        raise SystemExit(f"版本文件为空：{source}")
    if not VERSION_RE.fullmatch(version):
        raise SystemExit(
            f"版本号格式应为 X.Y.Z 或 X.Y.Z-<预发布>（如 1.0.0-rc.1），实际为：{version!r}（{source}）"
        )
    return version


def read_version(root: str = ROOT_DIR) -> str:
    """版本号唯一来源：项目根目录 ``version.txt``（取首行并去空白）。

    exe 文件名、zip 文件名、GitHub Release tag 全部由它推导；
    改版本号（含 -rc 等预发布后缀）只改这一个文件。
    """
    path = os.path.join(root, VERSION_FILE)
    try:
        with open(path, encoding="utf-8") as fp:
            raw = fp.readline()
    except FileNotFoundError:
        raise SystemExit(f"缺少版本文件：{path}")
    return parse_version(raw, path)


def is_prerelease(version: str) -> bool:
    """带预发布后缀（``1.0.0-rc.1``）即为预发布。"""
    return "-" in version


def exe_name(version: str) -> str:
    return f"{APP_NAME}-{version}.exe"


def zip_name(version: str) -> str:
    return f"{APP_NAME}-{version}-win64.zip"


VERSION = read_version()
EXE_NAME = exe_name(VERSION)
ZIP_NAME = zip_name(VERSION)

# 分发文档直接取自项目主目录的静态文件，构建脚本不再生成内容


def build_exe(root: str, clean: bool = False) -> str:
    if _pyinstaller is None:
        raise SystemExit("未安装 pyinstaller：pip install pyinstaller")
    sep = ";" if os.name == "nt" else ":"
    args = [
        os.path.join(root, "run.py"),
        # --name 决定 spec 文件名与中间产物名：统一叫 SuperStar（一次性中间产物不带版本号），
        # 最终 exe 在构建后重命名为 SuperStar-<版本>.exe
        "--name", APP_NAME,
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--distpath", os.path.join(root, "dist"),
        "--workpath", os.path.join(root, "build"),
        # spec 是构建中间产物：统一写进 build/SuperStar.spec，项目根不再堆 *.spec
        "--specpath", os.path.join(root, "build"),
        # 注意：config_win.ini 不再内嵌进 exe —— 它必须由压缩包随附（作为默认配置基准），
        # 否则「只拷 exe」也能跑，就失去了「不能单文件运行」的约束。
        "--add-data", sep.join([os.path.join(root, "resource"), "resource"]),
        "--hidden-import", "api",
        "--hidden-import", "gui",
        "--hidden-import", "engine",
        "--hidden-import", "account_store",
        "--hidden-import", "paths",
        "--hidden-import", "config_store",
        # GUI 无控制台兜底（缺失标准流会让 tqdm 崩刷课线程）
        "--hidden-import", "safeio",
        "--hidden-import", "fontTools.ttLib",
        # AI 答题（provider=AI）依赖，api/answer.py 里是 try-import，显式声明更稳
        "--hidden-import", "openai",
        "--hidden-import", "httpx",
        "--hidden-import", "pydantic",
    ]
    if clean:
        args.append("--clean")

    # 排除项：测试专用模块 + 与本项目无关的重量级包（保证单文件体积最小）
    # 注意：openai / httpx / pydantic 必须保留 —— provider=AI（OpenAI 兼容端点，
    # 含 DeepSeek 官方 API）依赖它们，api/answer.py 是 try-import，缺库则该提供方不可用。
    excludes = list(TEST_ONLY_MODULES) + [
        "PySide6", "shiboken6", "PIL", "numpy", "pandas", "matplotlib",
        "IPython", "pytest", "tkinter.test", "test",
    ]
    for module in excludes:
        args += ["--exclude-module", module]

    _pyinstaller.run(args)
    # PyInstaller 产物固定叫 SuperStar.exe，这里重命名为带版本号的分发名
    built = os.path.join(root, "dist", f"{APP_NAME}.exe")
    exe = os.path.join(root, "dist", EXE_NAME)
    if not os.path.exists(built):
        raise SystemExit(f"构建失败：未生成 {built}")
    if os.path.abspath(built) != os.path.abspath(exe):
        os.replace(built, exe)
    return exe


def stage_install(root: str, target_dir: str, exe: str) -> "list[str]":
    """把发行包所需的全部文件铺到 ``target_dir``（= 用户解压后看到的文件夹）。

    返回铺好的条目名列表，供测试断言与构建日志使用。
    """
    os.makedirs(target_dir, exist_ok=True)

    dst_exe = os.path.join(target_dir, EXE_NAME)
    if os.path.abspath(exe) != os.path.abspath(dst_exe):   # dist/ 铺装时源即目标，跳过
        shutil.copy2(exe, dst_exe)
    shutil.copy2(os.path.join(root, CONFIG_NAME), os.path.join(target_dir, CONFIG_NAME))
    # 空日志文件：程序追加写入，用户不会看到「运行后突然多出一个文件」
    with open(os.path.join(target_dir, LOG_NAME), "w", encoding="utf-8"):
        pass
    os.makedirs(os.path.join(target_dir, DATA_DIRNAME), exist_ok=True)
    # 使用说明为主目录的静态文件，直接随包复制，不在构建时生成
    doc_src = os.path.join(root, README_NAME)
    if not os.path.exists(doc_src):
        raise SystemExit(f"缺少分发文档：{doc_src}")
    shutil.copy2(doc_src, os.path.join(target_dir, README_NAME))
    # 开源协议：GPL-3.0 要求二进制分发时随附许可文本
    license_src = os.path.join(root, LICENSE_NAME)
    if not os.path.exists(license_src):
        raise SystemExit(f"缺少开源协议文件：{license_src}")
    shutil.copy2(license_src, os.path.join(target_dir, LICENSE_NAME))

    return sorted([EXE_NAME, CONFIG_NAME, LOG_NAME, DATA_DIRNAME + os.sep,
                   README_NAME, LICENSE_NAME])


def make_zip(root: str, exe: str) -> str:
    """由 stage_install 的产物打包；zip 内不再套 ``SuperStar-<版本>/`` 顶层目录。

    解压即主目录（解压器默认会按 zip 名建一层文件夹，用户看到的就是程序文件夹）。
    """
    zip_path = os.path.join(root, "dist", ZIP_NAME)
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)
    stage = os.path.join(root, "build", "dist_stage")
    shutil.rmtree(stage, ignore_errors=True)
    stage_install(root, stage, exe)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # 空目录不会被自动收录，必须显式写目录条目
        info = zipfile.ZipInfo(DATA_DIRNAME + "/")
        info.external_attr = (0o40755 << 16) | 0x10
        zf.writestr(info, b"")
        for name in sorted(os.listdir(stage)):
            full = os.path.join(stage, name)
            if os.path.isfile(full):
                zf.write(full, arcname=name)
    shutil.rmtree(stage, ignore_errors=True)
    return zip_path


def _force_utf8_stdio() -> None:
    """Windows 旧主机下 stdout 可能是 cp1252/cp936，打印中文随包文件名会 UnicodeEncodeError。

    典型场景：GitHub Actions 的 windows-latest（Python 3.12 hostedtoolcache）。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


def _ensure_tkinter() -> None:
    """构建需要可用的 tkinter（GUI 运行时依赖）。

    系统 Python 3.13 在部分环境 tkinter 损坏，PyInstaller 会静默把它排除出 exe，
    导致打出的程序在导入 gui.app 时崩溃。这里提前显式报错，避免产出坏包。
    """
    try:
        import tkinter  # noqa: F401
    except Exception as exc:  # 多为 ImportError
        raise SystemExit(
            "当前 Python 缺少可用的 tkinter，无法构建 GUI 程序。\n"
            "请使用项目自带的 .venv-build 环境（Python 3.12，自带 tkinter）执行构建：\n"
            "    .venv-build\\Scripts\\python.exe build.py\n"
            f"原始错误：{exc}"
        )


def main() -> None:
    root = os.path.dirname(os.path.abspath(__file__))
    _force_utf8_stdio()
    _ensure_tkinter()
    print(f"VERSION={VERSION} (from {VERSION_FILE})")
    print(f"PRERELEASE={str(is_prerelease(VERSION)).lower()}")
    exe = build_exe(root, clean="--clean" in sys.argv)
    size = os.path.getsize(exe) / 1024 / 1024
    print(f"BUILD_DONE={exe}  size={size:.1f}MB")

    if "--exe-only" in sys.argv:
        return

    # dist/ 本身即「解压后的文件夹」：exe + 随包文件一起铺开
    staged = stage_install(root, os.path.join(root, "dist"), exe)
    print(f"STAGE_DONE=dist/ {staged}")

    zip_path = make_zip(root, exe)
    zsize = os.path.getsize(zip_path) / 1024 / 1024
    print(f"ZIP_DONE={zip_path}  size={zsize:.1f}MB")


if __name__ == "__main__":
    main()
