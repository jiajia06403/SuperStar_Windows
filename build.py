# -*- coding: utf-8 -*-
"""构建 Windows 可执行文件（PyInstaller onedir）并打成分发包。

用法：
    python build.py             # 构建 dist/ 安装目录（exe + 运行时目录 + 随包文件）并打包 zip
    python build.py --exe-only  # 只出 onedir 产物目录（build/onedir/SuperStar/）
    python build.py --clean     # 清空中间产物后重新构建

发行包的形态（设计意图：程序本来就需要一整个文件夹，不该压成单个 exe）：

    SuperStar-<版本>-win64.zip  分发压缩包（文件名带完整版本号，取自 version.txt；
                          预发布形如 SuperStar-1.0.0-rc.1-win64.zip，后缀不可丢）
    SuperStar.exe       主程序（文件名不带版本号，恒为 SuperStar.exe）
    runtime\\            运行时目录（Python 运行时 / 依赖库 / 资源 —— exe 启动必需）
    config_win.ini      默认配置（随包自带，升级时覆盖 —— 用户不要改）
                        用户配置 config_user.ini 由程序首次启动生成，不随包分发（防覆盖）
    （chaoxing.log 与 progress_cache\\ 不随包：运行期由程序自己创建，
      包里不预置空文件/空目录 —— 覆盖升级时不会出现「空日志盖掉旧日志」）
    使用说明.txt
    LICENSE             GPL-3.0 许可文本（二进制分发须随附）

**为什么是 onedir 而不是 onefile**：onefile 每次启动都要把上千个文件解压到
%TEMP%，换台电脑首次启动时这批新文件还要被 Defender 逐个扫描 —— 这是
「首次启动要好几秒」的主因。onedir 直接就地加载，免掉解包与首轮全量扫描。
代价是不能再只拷一个 exe（只拷 exe 会报 "Failed to load Python DLL"，已写进使用说明）。

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
# 预发布后缀必须写进 version.txt —— zip 名、Release tag 全部由它推导，
# 否则预览版会和同号正式版重名（历史坑：tag 是 v1.0.0-rc.1，zip 却叫 SuperStar-1.0.0-win64.zip）。
# 注意：exe 文件名恒为 SuperStar.exe（不带版本号），版本区分交给 zip 名承担。
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?")
# 只在源码/测试时使用的模块，发行包一律排除
TEST_ONLY_MODULES = ("demo_chaoxing", "gui.selftest")

CONFIG_NAME = "config_win.ini"
README_NAME = "使用说明.txt"
LICENSE_NAME = "LICENSE"
# 运行期产物（日志 / 数据目录 / 用户配置）：不预置空文件或空目录，也不进 zip。
# 名字与 paths.py 保持单一来源 —— 打包目录被跑过一次后就带着本机数据，不能打进去。
try:
    import paths as _paths
    RUNTIME_ARTIFACTS = (_paths.LOG_NAME, _paths.DATA_DIRNAME, _paths.USER_CONFIG_NAME)
except Exception:  # noqa: BLE001  独立运行/裁剪环境下退回字面量
    RUNTIME_ARTIFACTS = ("chaoxing.log", "progress_cache", "config_user.ini")
# onedir 的运行时目录名（PyInstaller 6 默认叫 _internal，这里换成更易懂的 runtime）
# 目录里是 Python 运行时、依赖库与内嵌资源 —— exe 启动时按「exe 同级」找它，
# 缺了就会报 "Failed to load Python DLL"，因此它必须与 exe 一起分发。
RUNTIME_DIRNAME = "runtime"
# 安装目录名（= 产物目录名）：dist/SuperStar/ 即用户解压后看到的那个文件夹
INSTALL_DIRNAME = APP_NAME


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


def exe_name() -> str:
    """主程序文件名（不带版本号，恒为 SuperStar.exe）。"""
    return f"{APP_NAME}.exe"


def zip_name(version: str) -> str:
    return f"{APP_NAME}-{version}-win64.zip"


VERSION = read_version()
EXE_NAME = exe_name()
ZIP_NAME = zip_name(VERSION)

# 分发文档直接取自项目主目录的静态文件，构建脚本不再生成内容


def _retire_previous_install(root: str) -> "str | None":
    """把上一次的 ``dist/SuperStar/`` 整目录改名挪开，返回挪到哪了（无旧产物返回 None）。

    PyInstaller 的 COLLECT 在落盘前会**先清空**产物目录，一次删掉上千个文件；
    这类批量删除会被宿主/安全软件的「批量删除防护」拦下，构建随后静默失败
    （日志里有 ``Removing dir ...`` 却没有 ``COLLECT completed``，zip 也不更新）。
    目录改名是同盘瞬时操作、不算删除，PyInstaller 见目录不存在直接从零新建。
    """
    import time

    app_dir = os.path.join(root, "dist", INSTALL_DIRNAME)
    if not os.path.isdir(app_dir):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    trash = os.path.join(root, "dist", f"_prev_{INSTALL_DIRNAME}_{stamp}")
    try:
        os.replace(app_dir, trash)   # 同盘 rename：不复制、不删除
        print(f"PREV_INSTALL_MOVED={trash}")
        print("  （旧产物已挪开留档，确认新版可用后可自行删除该目录）")
        return trash
    except OSError as exc:  # noqa: BLE001  挪不动就让 PyInstaller 自己处理
        print(f"PREV_INSTALL_KEEP={app_dir} ({exc})")
        return None


def build_exe(root: str, clean: bool = False) -> str:
    """构建 onedir 产物，返回安装目录（含 ``SuperStar.exe`` 与 ``runtime/``）。

    onedir 而非 onefile：onefile 每次启动都要把上千个文件解压到 %TEMP%，
    换机首次运行时这批新文件还要被杀毒软件逐个扫描。onedir 直接就地加载，
    免掉解包与首轮全量扫描 —— 这是「首次启动慢」的主要来源。
    """
    if _pyinstaller is None:
        raise SystemExit("未安装 pyinstaller：pip install pyinstaller")
    sep = ";" if os.name == "nt" else ":"
    args = [
        os.path.join(root, "run.py"),
        # --name 决定产物目录名：统一叫 SuperStar（内含 SuperStar.exe）
        "--name", APP_NAME,
        "--onedir",
        # 运行时目录改名 runtime（默认 _internal）：对用户更易读，
        # 且 bootloader 按这个名字在 exe 同级找运行时，缺了就起不来
        "--contents-directory", RUNTIME_DIRNAME,
        "--windowed",
        "--noconfirm",
        # 产物直接落在 dist/<name>/ —— 不再经过中间目录，避免上千文件的重复复制
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
        # 显式禁用 UPX：onefile 时代是 no-op（本机/CI 无 upx），但 onedir 下
        # UPX 会压缩每个 dll/pyd，运行时反而要逐个解压回内存，白白拖慢启动
        "--noupx",
    ]
    if clean:
        args.append("--clean")

    # 排除项：测试专用模块 + 与本项目无关的重量级包（保证安装目录最小）
    # 注意：openai / httpx / pydantic 必须保留 —— provider=AI（OpenAI 兼容端点，
    # 含 DeepSeek 官方 API）依赖它们，api/answer.py 是 try-import，缺库则该提供方不可用。
    excludes = list(TEST_ONLY_MODULES) + [
        "PySide6", "shiboken6", "PIL", "numpy", "pandas", "matplotlib",
        "IPython", "pytest", "tkinter.test", "test",
    ]
    for module in excludes:
        args += ["--exclude-module", module]

    # 产物目录已在 main() 里由 _retire_previous_install() 整目录改名挪开，
    # PyInstaller 到这里见目录不存在，直接从零新建（不走 _make_clean_directory 的批量删除）。
    install_dir = os.path.join(root, "dist", APP_NAME)
    _pyinstaller.run(args)

    # onedir 产物：<distpath>/<name>/{SuperStar.exe, runtime/}
    exe = os.path.join(install_dir, EXE_NAME)
    if not os.path.exists(exe):
        raise SystemExit(f"构建失败：未生成 {exe}")
    runtime = os.path.join(install_dir, RUNTIME_DIRNAME)
    if not os.path.isdir(runtime):
        raise SystemExit(f"构建失败：未生成运行时目录 {runtime}")
    return install_dir


def stage_install(root: str, target_dir: str, app_dir: str) -> "list[str]":
    """把 onedir 产物 + 随包文件铺到 ``target_dir``（= 用户解压后看到的文件夹）。

    onedir 产物（``SuperStar.exe`` + ``runtime/``）由 PyInstaller 落在 ``app_dir``：
      - ``target_dir == app_dir``（正式构建）：exe 与 runtime 原地不动，只补随包文件；
      - ``target_dir != app_dir``（自检/测试）：把 exe 与 runtime 整棵树拷过去。
    两种情况都不做删除操作。

    布局刻意做成「exe 与 runtime/ 平铺在同一层」：bootloader 按 exe 所在目录
    找运行时目录，所以两者必须同级；这样运行期文件（config/log/cache）也仍在
    exe 顶层，``paths.app_dir()`` 的语义完全不变。

    返回铺好的条目名列表，供测试断言与构建日志使用。
    """
    os.makedirs(target_dir, exist_ok=True)
    same_dir = os.path.abspath(target_dir) == os.path.abspath(app_dir)

    # 主程序
    src_exe = os.path.join(app_dir, EXE_NAME)
    dst_exe = os.path.join(target_dir, EXE_NAME)
    if not same_dir and os.path.exists(src_exe):
        shutil.copy2(src_exe, dst_exe)

    # 运行时目录（Python 运行时 / 依赖库 / 内嵌资源）
    src_runtime = os.path.join(app_dir, RUNTIME_DIRNAME)
    dst_runtime = os.path.join(target_dir, RUNTIME_DIRNAME)
    if not same_dir and os.path.isdir(src_runtime):
        # dirs_exist_ok：覆盖式拷贝，不删除任何东西
        shutil.copytree(src_runtime, dst_runtime, dirs_exist_ok=True)

    shutil.copy2(os.path.join(root, CONFIG_NAME), os.path.join(target_dir, CONFIG_NAME))
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

    return sorted([EXE_NAME, RUNTIME_DIRNAME + os.sep, CONFIG_NAME,
                   README_NAME, LICENSE_NAME])


def make_zip(root: str, install_dir: str) -> str:
    """把安装目录打包成 zip；zip 内不再套 ``SuperStar-<版本>/`` 顶层目录。

    解压即主目录（解压器默认会按 zip 名建一层文件夹，用户看到的就是程序文件夹）。
    直接从安装目录读取（不再复制一份中间目录）—— 运行时目录上千个文件，
    复制 + 删除既慢又容易触发批量删除拦截。
    """
    zip_path = os.path.join(root, "dist", ZIP_NAME)
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in sorted(os.listdir(install_dir)):
            # 运行期才产生的产物（日志 / 数据目录 / 用户配置）绝不进包：
            # 产物目录被跑过一次后这些可能带着本机的日志、账号、题库，
            # 打进去会让「解压升级」覆盖掉用户自己的数据。
            if name in RUNTIME_ARTIFACTS:
                continue
            full = os.path.join(install_dir, name)
            if os.path.isfile(full):
                zf.write(full, arcname=name)
            elif os.path.isdir(full):
                # 运行时目录：整棵子树按相对路径写入（arcname 用 / 分隔，跨平台一致）
                for base, _dirs, files in os.walk(full):
                    for fname in sorted(files):
                        fpath = os.path.join(base, fname)
                        arc = os.path.relpath(fpath, install_dir).replace(os.sep, "/")
                        zf.write(fpath, arcname=arc)
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


def _dir_size(path: str) -> float:
    """目录总大小（MB）：onedir 下 exe 本身很小，体积主要在运行时目录里。"""
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total / 1024 / 1024


def main() -> None:
    root = os.path.dirname(os.path.abspath(__file__))
    _force_utf8_stdio()
    _ensure_tkinter()
    print(f"VERSION={VERSION} (from {VERSION_FILE})")
    print(f"PRERELEASE={str(is_prerelease(VERSION)).lower()}")
    # 先把上一次的产物目录整目录改名挪开：PyInstaller 落盘前会批量删除它，
    # 大批量删除会触发宿主/安全软件的防护，导致构建静默失败。
    _retire_previous_install(root)
    app_dir = build_exe(root, clean="--clean" in sys.argv)
    print(f"BUILD_DONE={app_dir}  size={_dir_size(app_dir):.1f}MB")

    if "--exe-only" in sys.argv:
        # onedir 没有「只要一个 exe」的概念：产物就是整个应用目录
        return

    # dist/SuperStar/ 即「解压后的文件夹」：exe + runtime/ + 随包文件
    staged = stage_install(root, app_dir, app_dir)
    print(f"STAGE_DONE=dist/{INSTALL_DIRNAME}/ {staged}")

    # 清理 onefile 时代的遗留产物：dist/SuperStar.exe（旧单文件 exe）
    legacy_exe = os.path.join(root, "dist", EXE_NAME)
    if os.path.isfile(legacy_exe):
        os.remove(legacy_exe)

    zip_path = make_zip(root, app_dir)
    zsize = os.path.getsize(zip_path) / 1024 / 1024
    print(f"ZIP_DONE={zip_path}  size={zsize:.1f}MB")


if __name__ == "__main__":
    main()
