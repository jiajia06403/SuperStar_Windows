# -*- coding: utf-8 -*-
"""本地测试调度器（CI 不跑，见 .github/workflows/release.yml）。

默认只跑 **core 层**（纯逻辑单测，无窗口、无网络，约 45 秒）；
GUI / 慢速用例按需显式开启，避免每次改动都全量执行。

用法：
    python test/run_test.py              # core 层（默认，日常改完跑这个）
    python test/run_test.py --gui        # 只跑 GUI 层（需带 tkinter 的解释器）
    python test/run_test.py --slow       # 只跑慢速/构建相关用例
    python test/run_test.py --all        # 全量（core + gui + slow）
    python test/run_test.py --dist       # 追加校验发行 exe 拒绝 --selftest（退出码 3）
    python test/run_test.py --list       # 只打印分层清单，不执行

分层靠测试文件首部的 `# layer: core|gui|slow` 标记（缺省视为 core），
新增测试文件默认进 core —— 不会因为忘记登记而被漏跑。
归档的一次性脚本放在 test/archive/，不参与本调度器。
"""
import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 脚本目录是 test/，需用项目根才能 import build / gui 等顶层模块
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "test"))

from ci_annotate import force_utf8_stdio  # noqa: E402

LAYERS = ("core", "gui", "slow")
TAG_RE = "layer:"


def discover():
    """{层名: [测试文件名]}，按文件首部标记归层，缺省 core。"""
    groups = {name: [] for name in LAYERS}
    for path in sorted(glob.glob(os.path.join(ROOT, "test", "test_*.py"))):
        layer = "core"
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh.readlines()[:15]:
                if TAG_RE in line:
                    tag = line.split(TAG_RE, 1)[1].strip().split()[0]
                    if tag in groups:
                        layer = tag
                    break
        groups[layer].append(os.path.basename(path))
    return groups


def _has_tk(python):
    try:
        proc = subprocess.run([python, "-c", "import tkinter"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc.returncode == 0
    except OSError:
        return False


def pick_gui_python():
    """GUI 用例要真窗口：系统 Python 可能不带 tkinter，回落到 .venv-build。"""
    venv = os.path.join(ROOT, ".venv-build", "Scripts", "python.exe")
    for candidate in (sys.executable, venv):
        if os.path.isfile(candidate) and _has_tk(candidate):
            return candidate
    return None


def _run(args, label, python):
    print(f"\n--- {label} ---")
    proc = subprocess.run([python] + args, cwd=ROOT)
    return proc.returncode == 0


def _dist_exe():
    """dist/ 下的发行 exe（onedir：dist/SuperStar/SuperStar.exe）。"""
    import build  # 版本号唯一来源：version.txt
    exe = os.path.join(ROOT, "dist", build.INSTALL_DIRNAME, build.EXE_NAME)
    return exe if os.path.isfile(exe) else None


def main(argv):
    force_utf8_stdio()
    groups = discover()

    if "--list" in argv:
        for name in LAYERS:
            print(f"[{name}] {len(groups[name])} 个")
            for test in groups[name]:
                print(f"  - {test}")
        return 0

    if "--all" in argv:
        selected = list(LAYERS)
    else:
        selected = [name for name in LAYERS if f"--{name}" in argv] or ["core"]

    failed, skipped = [], []
    for layer in selected:
        python = sys.executable
        if layer == "gui":
            python = pick_gui_python()
            if python is None:
                print("\n--- gui ---\nSKIP: 当前解释器与 .venv-build 都没有 tkinter，"
                      "装 tkinter 或用 .venv-build/Scripts/python.exe 再跑")
                skipped.append("gui")
                continue
        for name in groups[layer]:
            if not _run([os.path.join("test", name)], f"{layer}/{name}", python):
                failed.append(name)

    if "--dist" in argv:
        exe = _dist_exe()
        if exe is None:
            print("\n--- dist selftest ---\nSKIP: 未找到 dist 下的发行 exe，先跑 python build.py")
        else:
            proc = subprocess.run([exe, "--selftest"], cwd=ROOT)
            ok = proc.returncode == 3
            print(f"\n--- dist selftest ---\n{'PASS' if ok else 'FAIL'}"
                  f"  {os.path.basename(exe)} --selftest 退出码 {proc.returncode}（期望 3）")
            if not ok:
                failed.append("dist selftest")

    layers = "+".join(selected)
    print(f"\n[{layers}] " + ("ALL PASS" if not failed else "FAILED: " + ", ".join(failed)))
    if skipped:
        print("SKIPPED: " + ", ".join(skipped))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
