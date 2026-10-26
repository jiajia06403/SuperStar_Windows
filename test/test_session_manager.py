# -*- coding: utf-8 -*-
# layer: core  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""SessionManager 单例测试（无网络、无窗口）。

覆盖回归点（原实现把建会话写在 ``__init__`` 里，每次 ``SessionManager()`` 都会重建）：
  1. ``get_session()`` 恒返回同一个 Session 实例；
  2. 会话内 cookie 不会被磁盘 cookies.txt 的旧值覆盖（会话漂移）；
  3. ``update_cookies()`` 后同一会话可见新 cookie；
  4. 默认 timeout 的 partial 只包一层（不层层叠加）；
  5. ``clear_session()`` 丢弃旧会话，下次访问重建（旧连接池被关闭）。
"""
import functools
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate_exc  # noqa: E402

import requests  # noqa: E402

import api.base as base  # noqa: E402
from api.base import SessionManager  # noqa: E402


def test_clear_session_rebuilds():
    old = SessionManager.get_session()
    old.cookies.set("OLD", "1")
    SessionManager.clear_session()
    with mock.patch.object(base, "use_cookies", return_value={"NEW": "1"}):
        new = SessionManager.get_session()
    assert new is not old, "clear_session() 后仍拿到旧会话"
    assert new.cookies.get("OLD") is None
    assert new.cookies.get("NEW") == "1"
    assert isinstance(new.request, functools.partial), "重建后默认 timeout 丢了"


def test_get_session_is_singleton():
    a = SessionManager.get_session()
    b = SessionManager.get_session()
    assert a is b, "get_session() 返回了不同实例（__init__ 又建了一次 Session）"
    assert SessionManager() is SessionManager()
    assert SessionManager()._session is SessionManager()._session
    assert isinstance(a, requests.Session)


def test_session_cookies_not_overwritten_by_disk():
    session = SessionManager.get_session()
    session.cookies.set("X", "1")
    again = SessionManager.get_session()
    assert again is session
    assert again.cookies.get("X") == "1", "再次取会话时 cookie 丢了（磁盘值覆盖了服务端 Set-Cookie）"


def test_timeout_partial_wrapped_once():
    session = SessionManager.get_session()
    SessionManager.get_instance()          # 再构造一次，不应重复包裹
    SessionManager.get_session()
    request = session.request
    assert isinstance(request, functools.partial), "默认 timeout 未生效"
    assert not isinstance(request.func, functools.partial), "partial 被叠加了多层"
    assert request.keywords.get("timeout") == 5
    assert request.func.__func__ is requests.Session.request
    adapter = session.get_adapter("https://passport2.chaoxing.com/")
    assert adapter.max_retries.total == 10, "adapter 重试次数被改坏了"


def test_update_cookies_visible_on_same_session():
    session = SessionManager.get_session()
    with mock.patch.object(base, "use_cookies", return_value={"_uid": "123", "fid": "9"}):
        SessionManager.update_cookies()
    assert session.cookies.get("_uid") == "123"
    assert session.cookies.get("fid") == "9"


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
            annotate_exc(f"test_session_manager.py::{fn.__name__}")
    print(f"\n{'ALL PASS' if failed == 0 else str(failed) + ' FAILED'}")
    sys.exit(1 if failed else 0)
