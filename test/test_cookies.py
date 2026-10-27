# -*- coding: utf-8 -*-
# layer: core  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""cookies 落盘测试。

发行包不再预置 progress_cache/ 空目录（首次运行才由程序创建），
因此 ``save_cookies`` 必须自己兜底建目录 —— 否则全新解压后第一次登录就抛
``FileNotFoundError``，且异常发生在登录回调里，用户只看到「登录失败」。

覆盖：
  1. 数据目录不存在时 save_cookies 自动创建目录并写入（本次修复点）；
  2. 写入后 use_cookies 能原样读回；
  3. 文件缺失时 use_cookies 返回空字典而不抛异常；
  4. 重复保存覆盖旧内容（不追加、不残留分号）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate_exc  # noqa: E402

from api import cookies  # noqa: E402
from api.config import GlobalConst  # noqa: E402


class _FakeSession:
    """只提供 save_cookies 需要的最小接口：.cookies.items()。"""

    def __init__(self, pairs):
        self.cookies = dict(pairs)


def _with_cookies_path(path):
    """把 GlobalConst.COOKIES_PATH 指向临时文件，返回原值以便还原。"""
    origin = GlobalConst.COOKIES_PATH
    GlobalConst.COOKIES_PATH = path
    return origin


def test_save_cookies_creates_missing_dir():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "progress_cache", "cookies.txt")
        assert not os.path.isdir(os.path.dirname(target))
        origin = _with_cookies_path(target)
        try:
            cookies.save_cookies(_FakeSession([("uid", "123"), ("_d", "abc")]))
        finally:
            GlobalConst.COOKIES_PATH = origin
        assert os.path.isfile(target), "数据目录不存在时应自动创建并写入"
        with open(target, encoding="utf8") as fp:
            assert fp.read() == "uid=123;_d=abc"


def test_save_then_use_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "pc", "cookies.txt")
        origin = _with_cookies_path(target)
        try:
            cookies.save_cookies(_FakeSession([("a", "1"), ("b", "2")]))
            got = cookies.use_cookies()
        finally:
            GlobalConst.COOKIES_PATH = origin
        assert got == {"a": "1", "b": "2"}, got


def test_use_cookies_missing_file_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "nope", "cookies.txt")
        origin = _with_cookies_path(target)
        try:
            assert cookies.use_cookies() == {}
        finally:
            GlobalConst.COOKIES_PATH = origin


def test_save_cookies_overwrites_stale_content():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "pc", "cookies.txt")
        os.makedirs(os.path.dirname(target))
        with open(target, "w", encoding="utf8") as fp:
            fp.write("old=value;junk")
        origin = _with_cookies_path(target)
        try:
            cookies.save_cookies(_FakeSession([("new", "9")]))
            got = cookies.use_cookies()
        finally:
            GlobalConst.COOKIES_PATH = origin
        assert got == {"new": "9"}, got


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
            annotate_exc(f"test_cookies.py::{fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: unexpected")
            annotate_exc(f"test_cookies.py::{fn.__name__}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
