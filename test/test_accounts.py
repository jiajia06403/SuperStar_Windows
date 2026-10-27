# -*- coding: utf-8 -*-
# layer: core  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""账号层测试：本机账号文件落盘、本地名称（改名 / 判重）、登录名称解析、删除账号。

单独成文件（不塞进 test_engine.py）：改账号相关代码时只跑这一个文件即可，
`python test/run_test.py test_accounts.py`（可再 `-k 名称关键字` 只跑某个用例）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate, annotate_exc  # noqa: E402

from engine import StudyEngine  # noqa: E402


# --------------------------------------------------------------------------- #
# 账号持久化
# --------------------------------------------------------------------------- #
def test_account_store_roundtrip():
    """账号表落盘后能原样读回，密码不以明文出现在文件里。"""
    from account_store import AccountStore
    path = os.path.join(tempfile.mkdtemp(), "accounts.json")
    AccountStore(path).save({
        "u1": {"username": "13800000000", "password": "p@ss-1", "use_cookies": False},
        "u2": {"username": "13900000000", "password": "", "use_cookies": True},
    }, "u2")

    raw = open(path, encoding="utf8").read()
    assert "p@ss-1" not in raw, "密码不能明文落盘"

    store = AccountStore(path)
    acc = store.load()
    assert store.last_uid == "u2"
    assert acc["u1"]["password"] == "p@ss-1"
    assert acc["u2"]["password"] == ""
    assert acc["u2"]["use_cookies"] is True


def test_account_store_tolerates_bad_file():
    """账号文件损坏 / 版本异常时退回空表，不能影响启动。"""
    from account_store import AccountStore
    path = os.path.join(tempfile.mkdtemp(), "accounts.json")
    open(path, "w", encoding="utf8").write("{not-json")
    store = AccountStore(path)
    assert store.load() == {}
    assert store.last_uid == ""

    # last_uid 指向不存在的账号 -> 清空，避免自动登录登到不存在的账号
    open(path, "w", encoding="utf8").write(
        '{"version": 1, "last_uid": "ghost", "accounts": {"a": {"username": "x", "password": ""}}}'
    )
    store = AccountStore(path)
    store.load()
    assert store.last_uid == ""


def test_engine_remembers_account_across_restart():
    """登录过的账号重启后仍在，可自动登录；演示账号不入库。"""
    cache = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=cache)
    assert eng.accounts == {}
    assert eng.saved_account() is None

    TEST_USERNAME = "13800138000"

    eng.accounts["777"] = {"username": TEST_USERNAME, "password": "pw", "use_cookies": False}
    eng._persist_accounts("777")

    reboot = StudyEngine(cache_dir=cache)      # 模拟重启
    assert "777" in reboot.accounts
    assert reboot.last_uid == "777"
    acc = reboot.saved_account()
    assert acc and acc["username"] == TEST_USERNAME and acc["password"] == "pw"

    # remember=False：只记账号，不存密码 -> 视为不可自动登录
    reboot.accounts["888"] = {"username": "13900000000", "password": "", "use_cookies": False}
    reboot._persist_accounts("888")
    again = StudyEngine(cache_dir=cache)
    assert again.last_uid == "888"
    assert again.saved_account() is None, "没存密码不应尝试自动登录"

    # 演示账号不落盘
    demo_cache = tempfile.mkdtemp()
    demo = StudyEngine(cache_dir=demo_cache)
    demo.login_demo()
    assert not os.path.exists(os.path.join(demo_cache, "accounts.json"))
    assert demo.saved_account() is None


class _FakeLoginBackend:
    """登录桩：只提供 engine.login 用到的两个方法（不建网络会话）。"""

    def __init__(self, uid):
        self._uid = uid
        self.video_log_interval = 0
        self.video_finish_percent = 100

    def login(self, login_with_cookies=False):  # noqa: ARG002
        return {"status": True, "msg": "ok"}

    def get_uid(self):
        return self._uid


def test_account_local_name_and_dedupe():
    """本地名称：默认取手机号，可改名；重名（含他人默认名）一律拒绝。"""
    from api.exceptions import LoginError

    cache = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=cache)
    eng.accounts = {
        "A1": {"username": "13800000000", "password": "", "use_cookies": False},
        "B2": {"username": "13900000000", "password": "", "use_cookies": False},
        "C3": {"username": "", "password": "", "use_cookies": True},
    }

    # 未设名称 -> 默认只用 uid（不展示手机号）
    assert eng.account_name("A1") == "A1"

    # 改名并落盘
    assert eng.rename_account("A1", "小号") == "小号"
    assert StudyEngine(cache_dir=cache).account_name("A1") == "小号"

    # 与别人的自定义名 / 别人的默认名（uid）重名都要拒绝（忽略首尾空白）
    for bad in ("小号", " 小号 ", "C3"):
        try:
            eng.rename_account("B2", bad)
        except LoginError:
            pass
        else:
            raise AssertionError(f"重名未被拒绝: {bad!r}")
    assert eng.account_name("B2") == "B2", "改名失败不应改动原值"

    # 改成自己现有的名称不算重名
    assert eng.rename_account("A1", "小号") == "小号"


def test_login_name_resolution_and_duplicate_guard():
    """登录：可带名称；新增流程里 uid 重复 / 名称重复一律报错且不写入。"""
    from api.exceptions import LoginError

    eng = StudyEngine(cache_dir=tempfile.mkdtemp())
    eng._build_chaoxing = lambda *a, **k: _FakeLoginBackend("A1")

    assert eng.login("13800000000", "pw") == "A1"
    assert eng.account_name("A1") == "A1", "未取名时默认用 uid"

    # 同名 uid 重登：更新名称（切换账号/自动登录的刷新路径）
    eng.login("13800000000", "pw", name="小号")
    assert eng.account_name("A1") == "小号"

    # 「添加账号」流程：uid 已存在 -> 报错，且不改动已有账号
    try:
        eng.login("13800000000", "pw", name="另一个", reject_existing=True)
    except LoginError:
        pass
    else:
        raise AssertionError("重复 uid 未被拒绝")
    assert eng.account_name("A1") == "小号"

    # 新 uid 但名称与已有账号重名 -> 报错，且不登记新账号
    eng._build_chaoxing = lambda *a, **k: _FakeLoginBackend("C3")
    try:
        eng.login("13900000000", "pw", name="小号")
    except LoginError:
        pass
    else:
        raise AssertionError("重复名称未被拒绝")
    assert "C3" not in eng.accounts


def test_delete_account_clears_current_session():
    """删账号：删当前账号要清空会话与 last_uid；删别的账号不影响当前登录。"""
    cache = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=cache)
    eng.accounts = {
        "A1": {"username": "13800000000", "name": "小号", "password": "pw", "use_cookies": False},
        "B2": {"username": "13900000000", "password": "", "use_cookies": False},
        "C3": {"username": "", "password": "", "use_cookies": True},
    }
    eng.current_uid = "A1"
    eng._persist_accounts("A1")

    assert eng.delete_account("B2") is False
    assert eng.current_uid == "A1", "删别的账号不应退出当前登录"
    assert "B2" not in StudyEngine(cache_dir=cache).accounts

    assert eng.delete_account("A1") is True
    assert eng.current_uid is None and eng.chaoxing is None
    reboot = StudyEngine(cache_dir=cache)
    assert set(reboot.accounts) == {"C3"}, "只应剩下别的账号"
    assert reboot.last_uid == "", "当前账号删完后不应残留 last_uid"
    assert eng.delete_account("A1") is False, "重复删除是空操作"


CASES = [
    test_account_store_roundtrip,
    test_account_store_tolerates_bad_file,
    test_engine_remembers_account_across_restart,
    test_account_local_name_and_dedupe,
    test_login_name_resolution_and_duplicate_guard,
    test_delete_account_clears_current_session,
]


if __name__ == "__main__":
    failed = []
    for case in CASES:
        try:
            case()
            print(f"PASS  {case.__name__}")
        except AssertionError as exc:
            failed.append(case.__name__)
            print(f"FAIL  {case.__name__}: {exc}")
            annotate_exc(f"test_accounts.py::{case.__name__}")
        except Exception:  # noqa: BLE001
            failed.append(case.__name__)
            annotate_exc(f"test_accounts.py::{case.__name__}")
    if failed:
        annotate("test_accounts.py", "失败用例: " + ", ".join(failed))
        sys.exit(1)
    sys.exit(0)
