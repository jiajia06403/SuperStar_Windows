# -*- coding: utf-8 -*-
"""本地一键跑全部测试（CI 不跑，见 .github/workflows/release.yml）。

用法：
    python test/run_test.py            # 源码态四套测试
    python test/run_test.py --dist     # 额外校验发行 exe 拒绝 --selftest（退出码 3）

每个 test_*.py 自身即以退出码表示成败，这里只是统一调度与汇总。
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
TESTS = [os.path.basename(p) for p in sorted(glob.glob(os.path.join(ROOT, "test", "test_*.py")))]


def _run(args, label):
    print(f"\n--- {label} ---")
    proc = subprocess.run([sys.executable] + args, cwd=ROOT)
    return proc.returncode == 0


def _dist_exe():
    """dist/ 下形如 SuperStar-<ver>.exe 的构建产物（无则返回 None）。"""
    import build  # 版本号唯一来源：version.txt
    exe = os.path.join(ROOT, "dist", build.EXE_NAME)
    return exe if os.path.isfile(exe) else None


def main(argv):
    force_utf8_stdio()
    failed = []
    for name in TESTS:
        if not _run([os.path.join("test", name)], name):
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

    print("\n" + ("ALL PASS" if not failed else "FAILED: " + ", ".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
