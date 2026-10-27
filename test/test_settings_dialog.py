# -*- coding: utf-8 -*-
# layer: gui  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""答题设置窗口的行为测试（真实 Tk 窗口，无需账号/网络）。

校验三条约定：
  1. 下拉切换「答案提供方式」后，下方**只显示该提供方真正会读取的配置项**；
  2. 保存写进用户配置；换提供方再保存不会清掉别家的值；
  3. 窗口高度随内容收放，且内容不被窗口底边裁切。
"""
import os
import sys
import tempfile
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate, annotate_exc  # noqa: E402

import config_store  # noqa: E402
import paths  # noqa: E402
from gui.app import SettingsDialog  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeAppDir:
    """把程序目录临时指向 tmp，模拟「解压后的安装目录」。"""

    def __init__(self, tmp: str):
        self.tmp = tmp
        self._old = None

    def __enter__(self):
        self._old = (getattr(sys, "frozen", None), sys.executable)
        sys.frozen = True
        sys.executable = os.path.join(self.tmp, "SuperStar-1.2.3.exe")
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
    """复制仓库里的默认配置，保证测试跑的是真实默认值。"""
    with open(os.path.join(ROOT, paths.CONFIG_NAME), encoding="utf8") as fp:
        text = fp.read()
    with open(os.path.join(tmp, paths.CONFIG_NAME), "w", encoding="utf8") as fp:
        fp.write(text)


def _open(root: tk.Misc):
    dlg = SettingsDialog(root)
    dlg.update()
    return dlg


def _select(dlg, name: str) -> None:
    dlg.provider_var.set(SettingsDialog.DISABLED_LABEL if name == "" else name)
    dlg._on_provider_change()
    dlg.update()


def _entry_at(dlg, provider: str, key: str):
    """取某个提供方面板里某配置项对应的输入框控件（按 grid 行号定位）。"""
    from tkinter import ttk
    fields = [f[0] for f in SettingsDialog.PROVIDER_FIELDS[provider]]
    row = fields.index(key)
    for widget in dlg.panels[provider].winfo_children():
        if isinstance(widget, ttk.Entry) and int(widget.grid_info().get("row", -1)) == row:
            return widget
    raise AssertionError(f"{provider} 面板里找不到 {key} 的输入框")


def _value(dlg, key: str) -> str:
    kind, var = dlg.vars[key]
    value = var.get()
    return ("true" if value else "false") if kind == "bool" else str(value)


def test_only_relevant_fields_shown():
    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dir(tmp)
        with _FakeAppDir(tmp):
            root = tk.Tk()
            try:
                dlg = _open(root)
                for name in SettingsDialog.PROVIDERS:
                    _select(dlg, name)
                    visible = [k for k, p in dlg.panels.items() if p.winfo_manager()]
                    assert visible == [name], f"{name} 应只显示自己的面板，实际 {visible}"
                    assert dlg.specific_frame.winfo_manager(), f"{name} 的专属设置区应可见"
                    keys = [f[0] for f in SettingsDialog.PROVIDER_FIELDS[name]]
                    for key in keys:
                        assert key in dlg.vars, f"{name} 缺少配置项 {key}"

                # 停用：不显示任何提供方面板，只剩通用项
                _select(dlg, "")
                assert not [p for p in dlg.panels.values() if p.winfo_manager()]
                assert not dlg.specific_frame.winfo_manager(), "停用时专属区应隐藏"
                dlg.destroy()
            finally:
                root.destroy()


def test_save_keeps_other_providers_values():
    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dir(tmp)
        with _FakeAppDir(tmp):
            root = tk.Tk()
            try:
                dlg = _open(root)
                _select(dlg, "SiliconFlow")
                dlg.vars["siliconflow_key"][1].set("sk-sf")
                dlg._on_save()
                assert dlg.result["provider"] == "SiliconFlow", dlg.result

                dlg2 = _open(root)
                _select(dlg2, "DeepSeek")
                # 切到 DeepSeek 不应清掉硅基流动已填的 Key
                assert _value(dlg2, "siliconflow_key") == "sk-sf", "切换提供方不应丢已填内容"
                dlg2.vars["deepseek_key"][1].set("sk-ds")
                dlg2._on_save()

                cfg = config_store.load_section("tiku")
                assert cfg["provider"] == "DeepSeek", cfg
                assert cfg["deepseek_key"] == "sk-ds", cfg
                assert cfg["siliconflow_key"] == "sk-sf", cfg
                assert cfg["ignore_api_key"] == "false", "非 AI 方式下该开关应被关掉"
                for key in ("true_list", "false_list", "cover_rate", "work_retry"):
                    assert key in cfg, f"通用项 {key} 丢了"
                dlg2.destroy()
            finally:
                root.destroy()


def test_shared_field_edits_are_kept():
    """同一配置键（最小请求间隔 / HTTP 代理）出现在多个面板时，改一处别的面板也要跟着变。"""
    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dir(tmp)
        with _FakeAppDir(tmp):
            root = tk.Tk()
            try:
                dlg = _open(root)
                _select(dlg, "AI")
                # 真实控件编辑（而不是直接改变量）：能抓出「两个面板各建了一份变量」的 bug
                entry = _entry_at(dlg, "AI", "min_interval_seconds")
                entry.delete(0, "end")
                entry.insert(0, "7")
                _select(dlg, "SiliconFlow")
                assert _value(dlg, "min_interval_seconds") == "7", \
                    f"AI 面板的编辑没同步到 SiliconFlow，实际 {_value(dlg, 'min_interval_seconds')!r}"
                _select(dlg, "DeepSeek")
                entry = _entry_at(dlg, "DeepSeek", "http_proxy")
                entry.delete(0, "end")
                entry.insert(0, "http://127.0.0.1:7890")
                _select(dlg, "AI")
                assert _value(dlg, "http_proxy") == "http://127.0.0.1:7890", "代理设置未共用"
                dlg.destroy()
            finally:
                root.destroy()


def test_save_keeps_hidden_provider_defaults_empty():
    """保存当前提供方时，不得把隐藏面板的预填默认值写入配置。"""
    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dir(tmp)
        with _FakeAppDir(tmp):
            root = tk.Tk()
            try:
                dlg = _open(root)
                _select(dlg, "SiliconFlow")
                dlg._on_save()
                cfg = config_store.load_section("tiku")
                assert cfg["endpoint"] == "", "保存 SiliconFlow 不应注入 AI 的 OpenAI 地址"
                assert cfg["model"] == "", "保存 SiliconFlow 不应注入 AI 的默认模型"
            finally:
                root.destroy()


def test_video_log_interval_saved_to_common():
    """[common] video_log_interval：界面默认值取自 config_win.ini，可改；保存写进用户配置且不影响 [tiku]。"""
    from unittest import mock

    import gui.app as app_mod

    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dir(tmp)
        with _FakeAppDir(tmp):
            root = tk.Tk()
            try:
                dlg = _open(root)
                expected = config_store.load_default().get(
                    "common", "video_log_interval", fallback="")
                assert _value(dlg, "video_log_interval") == expected, \
                    f"视频日志间隔默认值应来自默认配置 {expected!r}，实际 {_value(dlg, 'video_log_interval')!r}"
                dlg.vars["video_log_interval"][1].set("5")
                dlg._on_save()
                assert dlg.common_result["video_log_interval"] == "5"
                cfg = config_store.load_section("common")
                assert cfg["video_log_interval"] == "5", cfg
                assert config_store.load_section("tiku").get("provider") is not None, \
                    "保存 [common] 不应丢掉 [tiku]"

                # 非数字不落盘（弹窗提示，测试里替换掉避免阻塞）
                dlg2 = _open(root)
                dlg2.vars["video_log_interval"][1].set("abc")
                with mock.patch.object(app_mod.messagebox, "showerror"):
                    dlg2._on_save()
                assert dlg2.result is None, "非法间隔不应保存"
                assert config_store.load_section("common")["video_log_interval"] == "5"
                dlg2.destroy()
            finally:
                root.destroy()


def test_video_finish_percent_saved_to_common():
    """[common] video_finish_percent：默认 90、可改、越界或非数字不落盘。"""
    from unittest import mock

    import gui.app as app_mod

    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dir(tmp)
        with _FakeAppDir(tmp):
            root = tk.Tk()
            try:
                dlg = _open(root)
                expected = config_store.load_default().get(
                    "common", "video_finish_percent", fallback="")
                assert _value(dlg, "video_finish_percent") == expected, \
                    f"观看进度要求默认值应来自默认配置 {expected!r}，" \
                    f"实际 {_value(dlg, 'video_finish_percent')!r}"
                assert expected == "90", expected

                dlg.vars["video_finish_percent"][1].set("95")
                dlg._on_save()
                assert dlg.common_result["video_finish_percent"] == "95"
                cfg = config_store.load_section("common")
                assert cfg["video_finish_percent"] == "95", cfg

                # 越界（89/101）与非整数都不落盘
                for bad in ("89", "101", "-1", "abc"):
                    dlg2 = _open(root)
                    dlg2.vars["video_finish_percent"][1].set(bad)
                    with mock.patch.object(app_mod.messagebox, "showerror"):
                        dlg2._on_save()
                    assert dlg2.result is None, f"{bad} 不应保存"
                    assert config_store.load_section("common")["video_finish_percent"] == "95"
                    dlg2.destroy()
            finally:
                root.destroy()


def test_submit_forced_and_verify_follows_local_bank():
    """提交答题恒为 true、核验判分跟随本地题库开关；三者都不再有独立界面入口。"""
    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dir(tmp)
        with _FakeAppDir(tmp):
            root = tk.Tk()
            try:
                dlg = _open(root)
                _select(dlg, "AI")
                for key in ("submit", "true_list", "false_list", "local_bank_verify"):
                    assert key not in dlg.vars, f"配置项 {key} 不应再出现在界面上"

                # 关掉本地题库 -> 既不核验也不写
                dlg.vars["local_bank"][1].set(False)
                dlg._on_save()
                cfg = config_store.load_section("tiku")
                assert cfg["submit"] == "true", "答题应始终提交"
                assert cfg["local_bank"] == "false" and cfg["local_bank_verify"] == "false", cfg

                # 打开本地题库 -> 提交后自动核验
                dlg2 = _open(root)
                _select(dlg2, "AI")
                dlg2.vars["local_bank"][1].set(True)
                dlg2._on_save()
                cfg = config_store.load_section("tiku")
                assert cfg["local_bank"] == "true" and cfg["local_bank_verify"] == "true", cfg
                # 判断题词表虽无输入框，仍原样保留在配置里（答题逻辑要用）
                assert cfg.get("true_list") and cfg.get("false_list"), cfg
                dlg2.destroy()
            finally:
                root.destroy()


def test_provider_selector_sits_above_specific_panel():
    """答案提供方式下拉紧贴在「专属设置」上方，而不是单独放在窗口顶部。"""
    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dir(tmp)
        with _FakeAppDir(tmp):
            root = tk.Tk()
            try:
                dlg = _open(root)
                for name in SettingsDialog.PROVIDERS:
                    _select(dlg, name)
                    dlg.update_idletasks()
                    box = dlg.provider_box
                    assert box.winfo_y() < dlg.specific_frame.winfo_y(), \
                        f"{name}：提供方式下拉应在专属设置区上方"
                    assert dlg.specific_frame.winfo_y() - (box.winfo_y() + box.winfo_height()) <= 16, \
                        f"{name}：下拉与专属设置之间不应再夹别的区块"
                dlg.destroy()
            finally:
                root.destroy()


def test_layout_not_clipped():
    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dir(tmp)
        with _FakeAppDir(tmp):
            root = tk.Tk()
            try:
                dlg = _open(root)
                body = dlg.winfo_children()[0]
                for name in SettingsDialog.PROVIDERS:
                    _select(dlg, name)
                    dlg.update_idletasks()
                    bottom = max(k.winfo_y() + k.winfo_height() for k in body.winfo_children()
                                 if k.winfo_manager())
                    assert bottom <= dlg.winfo_height(), \
                        f"{name} 内容被窗口裁切（内容底 {bottom} > 窗口高 {dlg.winfo_height()}）"
                dlg.destroy()
            finally:
                root.destroy()


def test_user_resize_survives_provider_switch():
    """用户手动拉宽后切换提供方：_fit 只增不减，不再把拉开的宽度打回去。"""
    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dir(tmp)
        with _FakeAppDir(tmp):
            root = tk.Tk()
            try:
                dlg = _open(root)
                dlg.geometry(f"{dlg.winfo_width() + 160}x{dlg.winfo_height()}")
                dlg.update()
                dlg.update()
                wide = dlg.winfo_width()
                assert dlg._user_sized, "手动改过尺寸后应被标记为 user-sized"
                _select(dlg, "TikuAdapter")
                dlg.update_idletasks()
                assert dlg.winfo_width() >= wide - 2, \
                    f"切提供方把用户拉开的宽度打回了: {wide} -> {dlg.winfo_width()}"
                assert int(dlg.hint_lbl.cget("wraplength")) >= wide - 60, \
                    "提示文字的换行宽度应按新宽度重算"
                dlg.destroy()
            finally:
                root.destroy()


def _wait_test_done(dlg, timeout=5.0) -> None:
    """泵 Tk 事件循环，直到连通性测试结束（后台线程 + 队列回主线程）。"""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        dlg.update()
        if not dlg._testing:
            return
        time.sleep(0.02)
    raise AssertionError(f"连通性测试未在 {timeout}s 内结束：{dlg.test_state.get()!r}")


def test_conn_test_button_shows_result():
    """点「测试连通性」：按钮右侧显示结果，测试期间按钮禁用、结束后恢复。"""
    import api.answer as answer_mod
    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dir(tmp)
        with _FakeAppDir(tmp):
            root = tk.Tk()
            captured = {}
            origin = answer_mod.test_tiku_connection

            def _fake(conf):
                captured.update(conf)
                return True, "连接正常（测试桩）"

            answer_mod.test_tiku_connection = _fake
            try:
                dlg = _open(root)
                _select(dlg, "AI")
                dlg.vars["key"][1].set("sk-ui")     # 未保存的界面值也要参与测试
                assert dlg.test_row.winfo_manager(), "选择提供方后应显示测试行"

                dlg._on_test()
                assert "测试中" in dlg.test_state.get(), dlg.test_state.get()
                assert dlg.test_btn.instate(["disabled"]), "测试期间按钮应禁用"
                _wait_test_done(dlg)
                text = dlg.test_state.get()
                assert "测试通过" in text and "连接正常" in text, text
                assert dlg.test_btn.instate(["!disabled"]), "测试结束后按钮应恢复"
                assert dlg.test_lbl.winfo_x() > dlg.test_btn.winfo_x() + dlg.test_btn.winfo_width(), \
                    "结果标签应位于按钮右侧"
                assert captured.get("key") == "sk-ui", f"测试应使用界面未保存的值：{captured}"
                assert captured.get("provider") == "AI", captured

                answer_mod.test_tiku_connection = lambda conf: (False, "未填写 API Key")
                dlg._on_test()
                _wait_test_done(dlg)
                assert "测试失败" in dlg.test_state.get(), dlg.test_state.get()
                dlg.destroy()
            finally:
                answer_mod.test_tiku_connection = origin
                root.destroy()


def test_conn_test_row_hidden_when_disabled():
    """停用题库时没有可测的服务：整行（按钮 + 结果）隐藏。"""
    with tempfile.TemporaryDirectory() as tmp:
        _prepare_dir(tmp)
        with _FakeAppDir(tmp):
            root = tk.Tk()
            try:
                dlg = _open(root)
                _select(dlg, "")
                assert not dlg.test_row.winfo_manager(), "停用时测试行应隐藏"
                _select(dlg, "TikuAdapter")
                assert dlg.test_row.winfo_manager(), "选择提供方后测试行应回来"
                # 切回停用：进行中的测试结果作废（代数 +1）
                gen = dlg._test_gen
                _select(dlg, "")
                assert dlg._test_gen > gen, "切换提供方应作废旧测试"
                dlg.destroy()
            finally:
                root.destroy()


if __name__ == "__main__":
    cases = [
        test_only_relevant_fields_shown,
        test_save_keeps_other_providers_values,
        test_shared_field_edits_are_kept,
        test_save_keeps_hidden_provider_defaults_empty,
        test_video_log_interval_saved_to_common,
        test_video_finish_percent_saved_to_common,
        test_submit_forced_and_verify_follows_local_bank,
        test_provider_selector_sits_above_specific_panel,
        test_layout_not_clipped,
        test_user_resize_survives_provider_switch,
        test_conn_test_button_shows_result,
        test_conn_test_row_hidden_when_disabled,
    ]
    failed = []
    for case in cases:
        try:
            case()
            print(f"PASS  {case.__name__}")
        except AssertionError as exc:
            failed.append(case.__name__)
            print(f"FAIL  {case.__name__}: {exc}")
            annotate_exc(f"test_settings_dialog.py::{case.__name__}")
        except Exception:  # noqa: BLE001
            failed.append(case.__name__)
            annotate_exc(f"test_settings_dialog.py::{case.__name__}")
    if failed:
        annotate("test_settings_dialog.py", "失败用例: " + ", ".join(failed))
        sys.exit(1)
    sys.exit(0)
