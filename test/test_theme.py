# -*- coding: utf-8 -*-
# layer: gui  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""界面主题测试（真实 Tk 窗口，无需账号/网络）。

校验五件事：
  1. 模式解析：system/light/dark 与界面文本「跟随系统/浅色/深色」互转，认不出按跟随系统；
  2. 两套配色都完整：键一致、正文与背景不同色，深色确实是深色；
     章节状态色必须覆盖 gui.app.CHAPTER_STYLE 里的每个状态（否则深色下会有看不清的行）；
  3. 切换主题后 ttk 样式与 tk 原生控件（日志 Text / 列表 tag）都真的换了颜色；
  4. 「跟随系统」：系统深浅色变化时自动重刷，锁定浅色/深色时不被系统带跑；
  5. 设置窗口 -> 外观：能选、能存进 config_user.ini，保存后主窗口立即换肤；
     窄窗口下工具栏自动折行，右侧按钮不被裁掉。
"""
import os
import sys
import tempfile
import tkinter as tk
from tkinter import ttk
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate, annotate_exc  # noqa: E402

import config_store  # noqa: E402
import paths  # noqa: E402
from gui import app as app_mod  # noqa: E402
from gui import theme  # noqa: E402
from gui.app import CHAPTER_STYLE, SettingsDialog, build  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeAppDir:
    """把程序目录临时指向 tmp，模拟「解压后的安装目录」。"""

    def __init__(self, tmp: str):
        self.tmp = tmp
        self._old = None

    def __enter__(self):
        self._old = (getattr(sys, "frozen", None), sys.executable)
        sys.frozen = True
        sys.executable = os.path.join(self.tmp, "SuperStar-test.exe")
        return self.tmp

    def __exit__(self, *exc):
        old_frozen, old_exe = self._old
        if old_frozen is None:
            del sys.frozen
        else:
            sys.frozen = old_frozen
        sys.executable = old_exe
        return False


def _prepare_dir(tmp: str) -> None:
    with open(os.path.join(ROOT, paths.CONFIG_NAME), encoding="utf8") as fp:
        text = fp.read()
    with open(os.path.join(tmp, paths.CONFIG_NAME), "w", encoding="utf8") as fp:
        fp.write(text)


def _app() -> app_mod.App:
    app = build(cache_dir=tempfile.mkdtemp(prefix="ss_theme_"))
    app.silent = True
    app.root.update()
    return app


def _sum_rgb(color: str) -> int:
    value = color.lstrip("#")
    return int(value[0:2], 16) + int(value[2:4], 16) + int(value[4:6], 16)


# --------------------------------------------------------------------------- #
# 1) 模式解析
# --------------------------------------------------------------------------- #
def test_mode_helpers():
    assert theme.normalize_mode("dark") == "dark"
    assert theme.normalize_mode(" DARK ") == "dark"
    assert theme.normalize_mode("深色") == "dark", "界面文本也要能反解"
    assert theme.normalize_mode("system") == "system"
    assert theme.normalize_mode("") == "system"
    assert theme.normalize_mode(None) == "system"
    assert theme.normalize_mode("乱写") == "system", "认不出就按跟随系统，不能崩"

    assert theme.label_of("light") == "浅色"
    assert theme.label_of("乱写") == "跟随系统"
    assert theme.mode_from_label("深色") == "dark"
    assert theme.mode_from_label("dark") == "dark"

    assert theme.resolve("light") == "light"
    assert theme.resolve("dark") == "dark"
    with mock.patch.object(theme, "system_prefers_dark", return_value=True):
        assert theme.resolve("system") == "dark"
    with mock.patch.object(theme, "system_prefers_dark", return_value=False):
        assert theme.resolve("system") == "light"
    assert isinstance(theme.system_prefers_dark(), bool)


# --------------------------------------------------------------------------- #
# 2) 配色表完整性
# --------------------------------------------------------------------------- #
def test_palettes_complete():
    light, dark = theme.PALETTES["light"], theme.PALETTES["dark"]
    assert set(light) == set(dark), "两套配色的键必须一致，否则切主题会 KeyError"
    for name, pal in (("light", light), ("dark", dark)):
        for key in ("bg", "card", "field", "border", "text", "text_dim",
                    "accent", "accent_text", "hover", "pressed", "select",
                    "select_text", "scroll", "ok", "warn", "error"):
            assert key in pal, f"{name} 配色缺 {key}"
            assert pal[key].startswith("#") and len(pal[key]) == 7, pal[key]
        assert pal["text"] != pal["bg"], f"{name} 正文与背景同色了"

    # 深色确实够深、浅色确实够浅（防止写反了还测不出来）
    assert _sum_rgb(dark["bg"]) < 200, dark["bg"]
    assert _sum_rgb(light["bg"]) > 600, light["bg"]

    # 章节状态色必须覆盖全部状态，否则某个状态在深色下会沿用浅色（看不清）
    for name in ("light", "dark"):
        assert set(theme.chapter_colors(name)) == set(CHAPTER_STYLE), \
            f"{name} 章节色缺项：{set(CHAPTER_STYLE) - set(theme.chapter_colors(name))}"
    assert theme.chapter_colors("dark")["done"] != theme.chapter_colors("light")["done"]


# --------------------------------------------------------------------------- #
# 3) 切换主题 -> 控件真的换色
# --------------------------------------------------------------------------- #
def test_apply_repaints_widgets():
    app = _app()
    try:
        light, dark = theme.palette("light"), theme.palette("dark")
        style = ttk.Style(app.root)

        app.theme.apply("light")
        app.root.update_idletasks()
        assert app.theme.mode == "light" and app.theme.theme == "light"
        assert app.log_text.cget("bg") == light["field"], app.log_text.cget("bg")
        assert style.lookup("TFrame", "background") == light["bg"]
        assert app.chapter_tree.tag_configure("done")["foreground"] == \
            theme.chapter_colors("light")["done"]

        app.theme.apply("dark")
        app.root.update_idletasks()
        assert app.theme.theme == "dark"
        assert app.log_text.cget("bg") == dark["field"], \
            f"深色下日志面板应换成深底，实际 {app.log_text.cget('bg')}"
        assert app.log_text.cget("fg") == dark["text"]
        assert style.lookup("TFrame", "background") == dark["bg"], "ttk 底色没跟着换"
        assert style.lookup("Treeview", "fieldbackground") == dark["field"]
        assert app.chapter_tree.tag_configure("done")["foreground"] == \
            theme.chapter_colors("dark")["done"], "章节状态色没跟着换"
        # Text 的 tag_configure 返回的是 (选项名, v1..v4) 元组，取值在最后一格
        assert app.log_text.tag_configure("error")["foreground"][-1] == dark["error"]
        assert app.course_tree.tag_configure("checked")["foreground"] == dark["accent"]

        # 标题栏（DWM）失败不影响功能：这里只要求调用不抛异常
        theme.apply_titlebar(app.root, "dark")
        theme.apply_titlebar(app.root, "light")
    finally:
        app.root.destroy()


# --------------------------------------------------------------------------- #
# 4) 跟随系统
# --------------------------------------------------------------------------- #
def test_follow_system_watch():
    app = _app()
    try:
        app.theme.apply("system")
        assert app.theme.mode == "system"
        app.theme.stop_watch()                 # 不要用真实定时器，手动触发一次

        # 系统切到深色：记录值与实际不一致 -> 自动重刷
        app.theme._system_dark = False
        with mock.patch.object(theme, "system_prefers_dark", return_value=True):
            app.theme._watch(600000)
        assert app.theme.theme == "dark", "系统变深色时应自动跟随"
        app.theme.stop_watch()

        app.theme._system_dark = True
        with mock.patch.object(theme, "system_prefers_dark", return_value=False):
            app.theme._watch(600000)
        assert app.theme.theme == "light", "系统变浅色时应自动跟随"
        app.theme.stop_watch()

        # 锁定浅色/深色时，系统怎么变都不跟着跑
        app.theme.apply("light")
        app.theme._system_dark = False
        with mock.patch.object(theme, "system_prefers_dark", return_value=True):
            app.theme._watch(600000)
        assert app.theme.theme == "light", "锁定浅色时不应被系统带成深色"
        app.theme.stop_watch()
    finally:
        app.root.destroy()


# --------------------------------------------------------------------------- #
# 5) 设置窗口 -> 外观
# --------------------------------------------------------------------------- #
def test_settings_dialog_theme_default_and_save():
    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dir(tmp)
        with _FakeAppDir(tmp):
            root = tk.Tk()
            try:
                dlg = SettingsDialog(root)
                dlg.update()
                assert dlg.theme_var.get() == "跟随系统", dlg.theme_var.get()
                assert dlg.ui_frame.winfo_manager(), "外观分组应可见"
                dlg.theme_var.set("深色")
                dlg._on_save()
                assert dlg.ui_result and dlg.ui_result["theme"] == "dark", dlg.ui_result
                assert config_store.load_section("ui")["theme"] == "dark"
                # 保存外观不应影响其他段
                assert config_store.load_section("common").get("speed") is not None
                assert config_store.load_section("tiku").get("provider") is not None

                dlg2 = SettingsDialog(root)
                dlg2.update()
                assert dlg2.theme_var.get() == "深色", "重开设置应回显已保存的主题"
                dlg2.destroy()
            finally:
                root.destroy()


def test_saved_theme_mode_reads_config():
    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dir(tmp)
        with _FakeAppDir(tmp):
            assert app_mod.saved_theme_mode() == "system", "默认应为跟随系统"
            config_store.save_section("ui", {"theme": "dark"})
            assert app_mod.saved_theme_mode() == "dark"
            config_store.save_section("ui", {"theme": "乱写"})
            assert app_mod.saved_theme_mode() == "system", "非法值应按跟随系统处理"


def test_settings_apply_theme_immediately():
    """保存设置后主窗口立即换肤（不必重启）。"""
    app = _app()
    try:
        assert app.theme.theme == "light" or app.theme.theme == "dark"

        class _FakeDialog(tk.Toplevel):
            def __init__(self, master):
                super().__init__(master)
                self.result = {"provider": ""}
                self.common_result = {}
                self.ui_result = {"theme": "dark"}
                self.after(10, self.destroy)

        with mock.patch.object(app_mod, "SettingsDialog", _FakeDialog):
            app.on_open_settings()
        assert app.theme.mode == "dark" and app.theme.theme == "dark", \
            "设置里选深色后主窗口应立即变深"
        assert any("界面主题" in line for line in app.log_text.get("1.0", "end").splitlines())
    finally:
        app.root.destroy()


def test_theme_combobox_preview_applies_immediately():
    """设置窗里选主题即实时换肤（主窗口同步），点「取消」则回退预览。"""
    app = _app()
    try:
        orig = app.theme.mode
        dlg = SettingsDialog(app.root)
        dlg.update()
        assert dlg._theme_orig_mode == orig, "应记录打开前的主题以便回退"
        # 选「深色」后不必点保存，主窗口就应变深
        dlg.theme_var.set("深色")
        dlg._on_theme_preview()
        assert app.theme.mode == "dark" and app.theme.theme == "dark", \
            "选深色后主窗口应立即变深"
        # 取消：回退到打开前的主题，不落盘
        dlg.destroy()
        assert app.theme.mode == orig, "取消应回退预览"
        assert config_store.load_section("ui").get("theme") != "dark", \
            "取消不应把预览写入配置"
    finally:
        app.root.destroy()


def test_theme_save_is_silent_no_popup():
    """保存配置不再用模态弹窗提示；成功只落盘并关闭。"""
    app = _app()
    try:
        dlg = SettingsDialog(app.root)
        dlg.update()
        # 模拟点保存：内部调用 _save（无 messagebox），并置 _saved 后销毁
        with mock.patch.object(dlg, "_save", return_value=True) as spied:
            dlg.theme_var.set("深色")
            dlg._on_save()
            spied.assert_called_once()
        assert dlg._saved is True, "保存成功应标记已保存（取消不再回退）"
        # 行为层面：on_open_settings 保存后不应再弹「设置已保存」信息框
        with mock.patch.object(app_mod, "SettingsDialog") as Dlg:
            class _Fake(tk.Toplevel):
                result = {"provider": ""}
                common_result = {}
                ui_result = {"theme": "light"}

                def __init__(self, master):
                    super().__init__(master)
                    self.after(10, self.destroy)
            Dlg.return_value = _Fake(app.root)
            with mock.patch.object(app, "_tips") as tips:
                app.on_open_settings()
                tips.assert_not_called(), "保存后不应再弹模态提示"
    finally:
        app.root.destroy()


def test_toolbar_reflows_and_never_clipped():
    """窄窗口：右侧操作组折到第二行；宽窗口：回到一行。两种情况下都完整可见。"""
    app = _app()
    try:
        app.root.geometry("840x560")
        app.root.update()
        app.root.update_idletasks()
        assert int(app._toolbar[2].grid_info().get("row", 0)) == 1, "窄窗口应折行"
        assert app.toolbar_fits(), "折行后仍应完整可见（不得越出工具栏边界）"

        app.root.geometry("1320x760")
        app.root.update()
        app.root.update_idletasks()
        assert int(app._toolbar[2].grid_info().get("row", 0)) == 0, "宽窗口应回到一行"
        assert app.toolbar_fits()
    finally:
        app.root.destroy()


if __name__ == "__main__":
    cases = [
        test_mode_helpers,
        test_palettes_complete,
        test_apply_repaints_widgets,
        test_follow_system_watch,
        test_settings_dialog_theme_default_and_save,
        test_saved_theme_mode_reads_config,
        test_settings_apply_theme_immediately,
        test_theme_combobox_preview_applies_immediately,
        test_theme_save_is_silent_no_popup,
        test_toolbar_reflows_and_never_clipped,
    ]
    failed = []
    for case in cases:
        try:
            case()
            print(f"PASS  {case.__name__}")
        except AssertionError as exc:
            failed.append(case.__name__)
            print(f"FAIL  {case.__name__}: {exc}")
            annotate_exc(f"test_theme.py::{case.__name__}")
        except Exception:  # noqa: BLE001
            failed.append(case.__name__)
            annotate_exc(f"test_theme.py::{case.__name__}")
    if failed:
        annotate("test_theme.py", "失败用例: " + ", ".join(failed))
        sys.exit(1)
    sys.exit(0)
