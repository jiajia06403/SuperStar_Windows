# -*- coding: utf-8 -*-
# layer: gui  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""账号界面测试：添加账号的名称输入框 / 判重 / 不预填账密 / 账号设置（改名、删除）。

只跑本文件：`python test/run_test.py test_account_dialog.py`
只跑某个用例：`python test/run_test.py test_account_dialog.py -k 重名`
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate, annotate_exc  # noqa: E402

import tkinter as tk  # noqa: E402
from tkinter import messagebox  # noqa: E402

from gui.app import AccountDialog, build  # noqa: E402


def _app_with_account(name: str = "小号"):
    """建一个带单个已保存账号的界面（不发网络请求，也不载入演示课程）。"""
    app = build(cache_dir=tempfile.mkdtemp(prefix="ss_acc_"))
    app.silent = True
    app.engine.accounts = {
        "A1": {"username": "13800000000", "name": name, "password": "pw",
               "use_cookies": False},
    }
    app.engine.current_uid = "A1"
    app._ev_accounts(list(app.engine.accounts.keys()))
    return app


def test_add_account_dialog_has_empty_credentials_and_name():
    """添加账号：账密不预填当前账号，名称默认留空（引擎默认用 uid）。"""
    app = _app_with_account()
    try:
        app.on_add_account()
        app.root.update()
        dlg = app._login_dialog
        assert dlg is not None and dlg.winfo_exists(), "添加账号应弹出登录框"
        assert dlg.user_var.get() == "", "添加账号不应预填手机号"
        assert dlg.pass_var.get() == "", "添加账号不应预填密码"
        assert dlg.name_var.get() == "", "名称默认留空 = 默认用 uid"
        assert dlg.add_only is True, "添加账号流程要拒绝重复 uid"
        dlg.destroy()
    finally:
        app.root.destroy()


def test_add_account_rejects_duplicate_name_inline():
    """名称与已有账号重名：行内报错、不提交（不必等一次网络登录失败）。"""
    app = _app_with_account(name="小号")
    submitted = []
    try:
        app._open_login_dialog(add_only=True)
        app.root.update()
        dlg = app._login_dialog
        dlg._on_submit = lambda *a: submitted.append(a)
        dlg.name_var.set("  小号 ")          # 首尾空白要忽略
        dlg.user_var.set("13900000000")
        dlg.pass_var.set("pw")
        dlg._on_ok()
        app.root.update()
        assert not submitted, "重名不应提交登录"
        assert "已被其他账号使用" in dlg.msg_label.cget("text")
        assert dlg.winfo_exists(), "重名时窗口应保留"
        assert str(dlg.ok_btn["state"]) != "disabled", "重名不该把按钮卡在忙碌态"
        dlg.destroy()
    finally:
        app.root.destroy()


def test_account_dialog_rename_and_duplicate_guard():
    """账号设置：重名行内拒绝，正常名称返回 save 结果。"""
    root = tk.Tk()
    try:
        dlg = AccountDialog(root, uid="A1", name="小号", username="13800000000",
                            taken_names=["另一个账号"])
        dlg.update()
        assert dlg.name_var.get() == "小号", "应预填当前名称"

        dlg.name_var.set("另一个账号")
        dlg._on_save()
        dlg.update()
        assert dlg.winfo_exists(), "重名不应关闭窗口"
        assert "已被其他账号使用" in dlg.msg_label.cget("text")
        assert dlg.result is None

        dlg.name_var.set("新名字")
        dlg._on_save()
        assert dlg.result == ("save", "新名字")

        dlg = AccountDialog(root, uid="A2", name="", username="")
        dlg.update()
        assert dlg.uid == "A2"
        dlg.destroy()
    finally:
        root.destroy()


def test_account_dialog_delete_returns_action():
    """删除账号：确认后返回 delete 结果，取消则无结果。"""
    origin = messagebox.askyesno
    root = tk.Tk()
    try:
        messagebox.askyesno = lambda *a, **k: False
        dlg = AccountDialog(root, uid="A1", name="小号")
        dlg.update()
        dlg._on_delete()
        assert dlg.result is None, "取消确认不应删除"
        assert dlg.winfo_exists()

        messagebox.askyesno = lambda *a, **k: True
        dlg._on_delete()
        assert dlg.result == ("delete", "")
    finally:
        messagebox.askyesno = origin
        root.destroy()


def test_rename_updates_combo_label_and_persists():
    """改名后下拉显示新名称，且落盘（重启仍在）。"""
    app = _app_with_account(name="小号")
    try:
        label = app._acc_label("A1")
        assert label == "小号 · A1", f"下拉文案应含本地名称: {label}"
        app.engine.rename_account("A1", "学习号")
        app._ev_accounts(list(app.engine.accounts.keys()))
        app.root.update()
        assert app.acc_var.get() == "学习号 · A1", app.acc_var.get()
        assert app.engine.account_name("A1") == "学习号"
    finally:
        app.root.destroy()


def test_delete_current_account_clears_ui():
    """删除当前账号：登录状态与课程/章节列表一起清空，下拉只剩「+ 添加账号」。"""
    app = _app_with_account(name="小号")
    try:
        app.course_tree.insert("", "end", iid="c1", text="某课程")
        app._delete_account("A1")
        app.root.update()
        assert not app.engine.accounts, "账号应从账号表移除"
        assert not app.course_tree.get_children(), "课程列表应清空"
        assert "已删除" in (app.status_var.get() or ""), app.status_var.get()
        assert app.acc_var.get() == "", "没有账号时下拉应为空"
        assert "+ 添加账号" in app.acc_combo.cget("values")
    finally:
        app.root.destroy()


if __name__ == "__main__":
    cases = [
        test_add_account_dialog_has_empty_credentials_and_name,
        test_add_account_rejects_duplicate_name_inline,
        test_account_dialog_rename_and_duplicate_guard,
        test_account_dialog_delete_returns_action,
        test_rename_updates_combo_label_and_persists,
        test_delete_current_account_clears_ui,
    ]
    failed = []
    for case in cases:
        try:
            case()
            print(f"PASS  {case.__name__}")
        except AssertionError as exc:
            failed.append(case.__name__)
            print(f"FAIL  {case.__name__}: {exc}")
            annotate_exc(f"test_account_dialog.py::{case.__name__}")
        except Exception:  # noqa: BLE001
            failed.append(case.__name__)
            annotate_exc(f"test_account_dialog.py::{case.__name__}")
    if failed:
        annotate("test_account_dialog.py", "失败用例: " + ", ".join(failed))
        sys.exit(1)
    sys.exit(0)
