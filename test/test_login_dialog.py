# -*- coding: utf-8 -*-
# layer: gui  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""登录框「行内报错 + 重弹」的行为测试（真实 Tk 窗口，无需账号/网络）。

校验四条约定：
  1. 缺账号/密码时点「登录」：不弹独立消息框，错误显示在「取消 / 登录」**左侧**，
     窗口保留（不关闭），「登录」按钮不被禁用；
  2. 登录失败（引擎抛错）：错误同样显示在两个按钮左侧，窗口保留、光标回到密码框；
  3. 超长错误文案按像素截断加省略号，不会把窗口撑宽；
  4. 自动登录失败 -> 重弹登录框，原因显示在按钮左侧。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate, annotate_exc  # noqa: E402

import tkinter as tk  # noqa: E402
from tkinter import messagebox  # noqa: E402

from gui.app import LoginDialog  # noqa: E402


def _no_messagebox(monkey_calls: list) -> None:
    """把独立消息框全部拦下来（需求：不允许再弹「密码错误」弹窗）。"""
    for name in ("showinfo", "showerror", "showwarning"):
        def _fake(*_a, _name=name, **_kw):
            monkey_calls.append(_name)
            return "ok"
        setattr(messagebox, name, _fake)


def _assert_inline_left_of_buttons(dlg: LoginDialog, expect_text: str) -> None:
    """错误提示必须显示在「取消 / 登录」两个按钮的左侧、同一行。"""
    dlg.update_idletasks()
    holder = dlg.msg_holder
    cancel = dlg.cancel_btn
    ok = dlg.ok_btn
    text = dlg.msg_label.cget("text")
    assert expect_text in text, f"行内提示内容不对: {text!r}（期望包含 {expect_text!r}）"
    assert holder.winfo_x() < cancel.winfo_x(), \
        f"提示区不在「取消」按钮左侧: holder={holder.winfo_x()} cancel={cancel.winfo_x()}"
    assert cancel.winfo_x() < ok.winfo_x(), "「取消」应在「登录」左侧"
    # 同一行：纵向中心基本对齐
    hy = holder.winfo_rooty() + holder.winfo_height() / 2
    cy = cancel.winfo_rooty() + cancel.winfo_height() / 2
    assert abs(hy - cy) <= 4, f"提示与按钮不在同一行: {hy} vs {cy}"


def test_missing_credentials_inline_error():
    """缺账号/密码：行内报错，不弹窗、不关窗、「登录」按钮保持可用。"""
    calls: list = []
    origin = {name: getattr(messagebox, name) for name in ("showinfo", "showerror", "showwarning")}
    _no_messagebox(calls)
    root = tk.Tk()
    try:
        dlg = LoginDialog(root)
        dlg.update()
        dlg._on_ok()
        dlg.update()
        assert not calls, f"不应弹独立消息框: {calls}"
        assert dlg.winfo_exists(), "缺账号密码时窗口不应被关闭"
        _assert_inline_left_of_buttons(dlg, "请填写手机号和密码")
        assert str(dlg.ok_btn["state"]) != "disabled", "校验失败后「登录」按钮应保持可用"
        dlg.destroy()
    finally:
        for name, fn in origin.items():
            setattr(messagebox, name, fn)
        root.destroy()


def test_login_failure_keeps_window_and_shows_error():
    """登录失败：错误显示在两个按钮左侧，窗口保留、光标回密码框。"""
    calls: list = []
    origin = {name: getattr(messagebox, name) for name in ("showinfo", "showerror", "showwarning")}
    _no_messagebox(calls)
    root = tk.Tk()
    try:
        dlg = LoginDialog(root, username="18200000000", password="wrong",
                          on_submit=lambda *_a: None)
        dlg.update()
        # 模拟「点登录 -> 后台线程失败 -> 主线程回填错误」
        dlg._on_ok()
        dlg.update()
        assert dlg.winfo_exists(), "提交登录后窗口不应立刻消失"
        assert str(dlg.ok_btn["state"]) == "disabled", "登录进行中应禁用「登录」按钮"

        dlg.set_busy(False)
        dlg.show_error("账号或密码错误")
        dlg.focus_password()
        dlg.update()
        assert not calls, f"不应弹独立消息框: {calls}"
        assert dlg.winfo_exists(), "登录失败后窗口应保留"
        _assert_inline_left_of_buttons(dlg, "账号或密码错误")
        assert str(dlg.ok_btn["state"]) != "disabled", "失败后「登录」按钮应恢复"
        assert dlg.pass_entry.focus_get() is dlg.pass_entry, "失败后光标应回到密码框"
        dlg.destroy()
    finally:
        for name, fn in origin.items():
            setattr(messagebox, name, fn)
        root.destroy()


def test_long_error_text_is_truncated():
    """超长错误文案按像素截断加省略号，且不把窗口撑宽。"""
    root = tk.Tk()
    try:
        dlg = LoginDialog(root)
        dlg.update()
        width_before = dlg.winfo_width()
        dlg.show_error("账号或密码错误，请确认手机号与密码后重试，若忘记密码请使用验证码登录" * 3)
        dlg.update()
        text = dlg.msg_label.cget("text")
        assert text.endswith("…"), f"超长提示未截断: {text!r}"
        assert dlg.winfo_width() <= width_before + 1, \
            f"提示把窗口撑宽了: {width_before} -> {dlg.winfo_width()}"
        dlg.destroy()
    finally:
        root.destroy()


def test_login_dialog_is_resizable_and_ordered():
    """登录框可横向拉伸：输入框跟着变宽，按钮次序与行内提示位置不变。"""
    root = tk.Tk()
    try:
        dlg = LoginDialog(root)
        dlg.update()
        width_before = dlg.winfo_width()
        entry_before = dlg.user_entry.winfo_width()
        dlg.geometry(f"{width_before + 200}x{dlg.winfo_height()}")
        dlg.update()
        dlg.update()
        assert dlg.winfo_width() >= width_before + 180, \
            f"登录框应可横向拉伸: {width_before} -> {dlg.winfo_width()}"
        assert dlg.user_entry.winfo_width() > entry_before, "输入框应跟着窗口变宽"
        # 拉宽后提示区可以变宽，但按钮次序与同行关系不变
        dlg.show_error("账号或密码错误")
        dlg.update()
        _assert_inline_left_of_buttons(dlg, "账号或密码错误")
        assert dlg.msg_holder.winfo_width() <= 420, "提示区宽度应有上限，不能反过来顶宽窗口"
        dlg.destroy()
    finally:
        root.destroy()


def test_auto_login_failed_reopens_dialog():
    """自动登录失败：重弹登录框，原因显示在「取消 / 登录」左侧。"""
    from gui.app import build

    app = build(demo=True, cache_dir=tempfile.mkdtemp(prefix="ss_autologin_"))
    try:
        app.silent = False
        app.engine.accounts.clear()
        app.engine.current_uid = ""
        app._ev_auto_login_failed("账号或密码错误")
        app.root.update()
        dlg = app._login_dialog
        assert dlg is not None, "自动登录失败应重弹登录框"
        assert dlg.winfo_exists()
        _assert_inline_left_of_buttons(dlg, "账号或密码错误")
        dlg.destroy()
    finally:
        app.root.destroy()


class _FakeChaoxing:
    """假后端：登录成功后列课程要用到的最小接口。"""

    def get_course_list(self):
        return []


def test_login_failure_then_retry_succeeds():
    """完整闭环：登录失败 -> 行内报错、窗口保留 -> 改密码重试 -> 成功关窗。

    走真实链路：对话框 -> 后台线程 -> 引擎抛错 -> 事件队列 -> 主线程回填。
    """
    import time

    from api.exceptions import LoginError
    from gui.app import build

    app = build(demo=True, cache_dir=tempfile.mkdtemp(prefix="ss_retry_"))
    app.silent = True
    calls = {"n": 0}

    def fake_login(username, password, use_cookies=False, tiku_config=None, remember=True):
        calls["n"] += 1
        if password != "goodpass":
            raise LoginError("手机号或密码错误")
        app.engine.chaoxing = _FakeChaoxing()
        app.engine.current_uid = "U123"
        return "U123"

    app.engine.login = fake_login

    def pump_until(pred, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.root.update()
            if pred():
                return True
            time.sleep(0.02)
        return False

    try:
        app._open_login_dialog(username="18200000000", password="wrong")
        dlg = app._login_dialog
        app.root.update()
        assert dlg is not None and dlg.winfo_exists(), "登录框应已打开"

        dlg.pass_var.set("wrong")
        dlg._on_ok()
        assert str(dlg.ok_btn["state"]) == "disabled", "登录进行中应禁用「登录」按钮"

        # 后台线程失败 -> 事件队列 -> 主线程行内报错
        got = pump_until(lambda: dlg.msg_label.cget("text") not in ("", "登录中…"))
        assert got, "登录失败后错误没有回填到行内提示"
        _assert_inline_left_of_buttons(dlg, "账号或密码错误")
        assert dlg.winfo_exists(), "登录失败后窗口应保留"
        assert str(dlg.ok_btn["state"]) != "disabled", "失败后「登录」按钮应恢复"
        assert dlg.pass_entry.focus_get() is dlg.pass_entry, "失败后光标应回到密码框"

        # 改密码重试 -> 成功 -> 关窗
        dlg.pass_var.set("goodpass")
        dlg._on_ok()
        assert pump_until(lambda: not dlg.winfo_exists()), "登录成功后登录框应关闭"
        assert calls["n"] == 2, f"应重试一次登录，实际调用 {calls['n']} 次"
        assert app._login_dialog is None, "关闭后 App 侧的登录框引用应清空"
    finally:
        app.root.destroy()


if __name__ == "__main__":
    cases = [
        test_missing_credentials_inline_error,
        test_login_failure_keeps_window_and_shows_error,
        test_long_error_text_is_truncated,
        test_login_dialog_is_resizable_and_ordered,
        test_auto_login_failed_reopens_dialog,
        test_login_failure_then_retry_succeeds,
    ]
    failed = []
    for case in cases:
        try:
            case()
            print(f"PASS  {case.__name__}")
        except AssertionError as exc:
            failed.append(case.__name__)
            print(f"FAIL  {case.__name__}: {exc}")
            annotate_exc(f"test_login_dialog.py::{case.__name__}")
        except Exception:  # noqa: BLE001
            failed.append(case.__name__)
            annotate_exc(f"test_login_dialog.py::{case.__name__}")
    if failed:
        annotate("test_login_dialog.py", "失败用例: " + ", ".join(failed))
        sys.exit(1)
    sys.exit(0)
