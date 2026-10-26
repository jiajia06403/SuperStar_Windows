# -*- coding: utf-8 -*-
"""界面主题：Win11 观感的浅色 / 深色配色，可跟随 Windows 系统设置。

三层职责：
  1. 判定：读注册表 ``AppsUseLightTheme`` 判断系统当前是深色还是浅色；
  2. 上色：用 ttk 的 ``clam`` 主题重建一套 Win11 观感（扁平、细边框、强调色、
     紧凑内边距），并同步 tk 原生控件（日志 Text、窗口底色、下拉列表）；
  3. 跟随：定时轮询注册表，系统切主题时自动重刷（仅 ``mode=system`` 时）。

另外用 DWM 接口把**标题栏**也刷成对应深浅 —— 否则客户区变黑了、标题栏还是白的；
顺带开 Win11 的圆角窗口边框。

文字颜色（章节状态、日志告警）也定义在这里：深色背景下原来的深绿/深红不可读，
必须换成提亮过的同色系，因此它们和配色表一起按主题成套给。
"""
import ctypes
import sys
import tkinter as tk
import tkinter.font as tkfont
import weakref
from tkinter import ttk

MODE_SYSTEM, MODE_LIGHT, MODE_DARK = "system", "light", "dark"
MODES = (MODE_SYSTEM, MODE_LIGHT, MODE_DARK)
MODE_LABELS = {MODE_SYSTEM: "跟随系统", MODE_LIGHT: "浅色", MODE_DARK: "深色"}
_LABEL_TO_MODE = {label: mode for mode, label in MODE_LABELS.items()}

WATCH_MS = 2000               # 轮询系统主题的间隔；注册表读取开销极小

# Win11 配色：浅色取 Fluent 2 亮色，深色取 Fluent 2 暗色（背景 #202020 系）
PALETTES = {
    "light": {
        "bg": "#f3f3f3",            # 窗口底
        "card": "#fbfbfb",          # 按钮 / 表头 / 次级面板
        "field": "#ffffff",         # 输入框、列表底
        "border": "#e2e2e2",
        "border_strong": "#cfcfcf",
        "text": "#1b1b1b",
        "text_dim": "#616161",
        "accent": "#0f6cbd",
        "accent_hover": "#1a7ac9",
        "accent_pressed": "#0c5a9e",
        "accent_text": "#ffffff",
        "hover": "#f0f0f0",
        "pressed": "#e4e4e4",
        "select": "#cce4f7",
        "select_text": "#101010",
        "scroll": "#c8c8c8",
        "ok": "#0f7b0f",
        "warn": "#9d5d00",
        "error": "#c42b1c",
    },
    "dark": {
        "bg": "#202020",
        "card": "#2b2b2b",
        "field": "#2d2d2d",
        "border": "#3a3a3a",
        "border_strong": "#4d4d4d",
        "text": "#f2f2f2",
        "text_dim": "#b0b0b0",
        "accent": "#4cc2ff",
        "accent_hover": "#63caff",
        "accent_pressed": "#3aa9e2",
        "accent_text": "#062b3a",
        "hover": "#303030",
        "pressed": "#3a3a3a",
        "select": "#2a4a63",
        "select_text": "#ffffff",
        "scroll": "#4a4a4a",
        "ok": "#6ccb5f",
        "warn": "#f0b45a",
        "error": "#ff99a4",
    },
}

# 章节状态字体色（键与 gui.app.CHAPTER_STYLE 一致）
CHAPTER_COLORS = {
    "light": {
        "done": "#2e7d32", "pending": "#424242", "not_open": "#6a1b9a",
        "studying": "#ef6c00", "error": "#c62828",
    },
    "dark": {
        "done": "#6ccb5f", "pending": "#d0d0d0", "not_open": "#c39aef",
        "studying": "#ffb74d", "error": "#ff8a80",
    },
}

_ACTIVE = "light"          # 当前生效的主题名（无 ThemeManager 时的兜底）
_MANAGER: "ThemeManager | None" = None
_COMBO_FIX_INSTALLED = False   # combobox 焦点修正是否已挂上（类绑定是解释器级的）


# --------------------------------------------------------------------------- #
# 模式解析
# --------------------------------------------------------------------------- #
def normalize_mode(value) -> str:
    """把配置值 / 界面文本统一成 ``system|light|dark``；认不出按「跟随系统」。"""
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered in MODES:
        return lowered
    return _LABEL_TO_MODE.get(text, MODE_SYSTEM)


def label_of(mode) -> str:
    """模式 -> 界面显示文本。"""
    return MODE_LABELS[normalize_mode(mode)]


def mode_from_label(label) -> str:
    """界面显示文本 -> 模式（也接受 dark/light 这类原始值）。"""
    return normalize_mode(label)


def system_prefers_dark() -> bool:
    """Windows 当前是否处于深色模式（非 Windows / 读不到时按浅色）。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) == 0
    except Exception:  # noqa: BLE001 - 注册表缺失/权限异常一律按浅色
        return False


def resolve(mode) -> str:
    """模式 -> 实际生效的主题名（``system`` 时才去问系统）。"""
    mode = normalize_mode(mode)
    if mode == MODE_DARK:
        return "dark"
    if mode == MODE_LIGHT:
        return "light"
    return "dark" if system_prefers_dark() else "light"


def current() -> str:
    """当前生效的主题名（``light`` / ``dark``）。"""
    return _ACTIVE


def palette(name: "str | None" = None) -> dict:
    return PALETTES.get(name or _ACTIVE, PALETTES["light"])


def chapter_colors(name: "str | None" = None) -> dict:
    return CHAPTER_COLORS.get(name or _ACTIVE, CHAPTER_COLORS["light"])


# --------------------------------------------------------------------------- #
# 标题栏（DWM）
# --------------------------------------------------------------------------- #
DWMWA_USE_IMMERSIVE_DARK_MODE = 20      # Win10 2004+；更早的版本是 19
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
DWMWA_WINDOW_CORNER_PREFERENCE = 33     # Win11：窗口圆角
DWMWCP_ROUND = 2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x0001, 0x0002, 0x0004
SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0010, 0x0020


def _hwnd(win: tk.Misc) -> int:
    """取 Tk 窗口对应的顶层 HWND（``winfo_id()`` 给的是子窗口）。

    64 位下必须显式声明 argtypes/restype，否则句柄会被截成 32 位
    （历史坑：``OverflowError: int too long to convert``）。
    """
    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [ctypes.c_void_p]
    user32.GetParent.restype = ctypes.c_void_p
    hwnd = user32.GetParent(ctypes.c_void_p(win.winfo_id()))
    return int(hwnd or win.winfo_id())


def apply_titlebar(win: tk.Misc, name: "str | None" = None) -> None:
    """把窗口标题栏刷成主题色，并开 Win11 圆角；任何一步失败都静默跳过。"""
    if sys.platform != "win32":
        return
    theme_name = name or _ACTIVE
    try:
        hwnd = _hwnd(win)
        if not hwnd:
            return
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                              ctypes.c_void_p, ctypes.c_uint]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long

        def _set(attr: int, value: int) -> bool:
            data = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), ctypes.c_uint(attr),
                                             ctypes.byref(data), ctypes.sizeof(data)) == 0

        want_dark = 1 if theme_name == "dark" else 0
        if not _set(DWMWA_USE_IMMERSIVE_DARK_MODE, want_dark):
            _set(DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, want_dark)
        _set(DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)

        # 切主题后非客户区不会自动重绘，强制重算一次边框
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos.restype = ctypes.c_int
        user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                            | SWP_NOACTIVATE | SWP_FRAMECHANGED)
    except Exception:  # noqa: BLE001 - 标题栏只是观感，失败不影响功能
        pass


# --------------------------------------------------------------------------- #
# 管理器
# --------------------------------------------------------------------------- #
class ThemeManager:
    """一套 ttk 样式的持有者：``apply()`` 换主题，``start_watch()`` 跟随系统。"""

    def __init__(self, root: tk.Tk):
        global _MANAGER
        self.root = root
        self.style = ttk.Style(root)
        self.mode = MODE_SYSTEM
        self.theme = resolve(MODE_SYSTEM)
        self._listeners: "list" = []
        self._windows: "list[weakref.ref]" = []
        self._watch_id = None
        self._system_dark = system_prefers_dark()
        _MANAGER = self
        self.register(root)

    # ---------------- 生命周期 ----------------
    def apply(self, mode=None) -> str:
        """套用主题（``mode`` 为空则沿用当前模式），返回实际生效的主题名。"""
        if mode is not None:
            self.mode = normalize_mode(mode)
        name = resolve(self.mode)
        self.theme = name
        self._system_dark = system_prefers_dark()
        globals()["_ACTIVE"] = name
        self._apply_styles()
        self._apply_tk_defaults()
        for win in self._alive_windows():
            apply_titlebar(win, name)
        for listener in list(self._listeners):
            try:
                listener(name)
            except Exception:  # noqa: BLE001 - 单个监听器出错不影响其余
                pass
        return name

    def add_listener(self, listener) -> None:
        """主题变化回调：``listener(theme_name)``，供 tk 原生控件重上色。"""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def register(self, win: tk.Misc) -> None:
        """登记窗口：立即刷标题栏，并在每次显示时补刷（Toplevel 上屏后才拿得到 HWND）。"""
        self._windows = [ref for ref in self._windows if ref() is not None]
        ref = weakref.ref(win)
        self._windows.append(ref)
        apply_titlebar(win, self.theme)
        try:
            win.bind("<Map>", lambda _e, r=ref: self._restyle(r), add="+")
        except tk.TclError:
            pass

    def start_watch(self, interval_ms: int = WATCH_MS) -> None:
        """开始跟随系统主题（仅 ``mode=system`` 时生效）。"""
        self.stop_watch()
        self._watch(interval_ms)

    def stop_watch(self) -> None:
        if self._watch_id is not None:
            try:
                self.root.after_cancel(self._watch_id)
            except tk.TclError:
                pass
            self._watch_id = None

    # ---------------- 内部 ----------------
    def _alive_windows(self) -> "list[tk.Misc]":
        alive = []
        for ref in list(self._windows):
            win = ref()
            if win is None:
                continue
            try:
                if not win.winfo_exists():
                    continue
            except tk.TclError:
                continue
            alive.append(win)
        return alive

    def _restyle(self, ref: "weakref.ref") -> None:
        win = ref()
        if win is not None:
            apply_titlebar(win, self.theme)

    def _watch(self, interval_ms: int) -> None:
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        if self.mode == MODE_SYSTEM:
            dark = system_prefers_dark()
            if dark != self._system_dark:        # 系统切了浅色/深色 -> 重刷整套样式
                self.apply(MODE_SYSTEM)
        try:
            self._watch_id = self.root.after(interval_ms, lambda: self._watch(interval_ms))
        except tk.TclError:
            self._watch_id = None

    def _apply_tk_defaults(self) -> None:
        """tk 原生控件与弹出列表（Combobox 下拉）不走 ttk，得单独上色。"""
        p = palette()
        try:
            self.root.configure(bg=p["bg"])
        except tk.TclError:
            return
        add = self.root.option_add
        add("*TCombobox*Listbox.background", p["field"])
        add("*TCombobox*Listbox.foreground", p["text"])
        add("*TCombobox*Listbox.selectBackground", p["accent"])
        add("*TCombobox*Listbox.selectForeground", p["accent_text"])
        add("*TCombobox*Listbox.borderWidth", 0)

    def _apply_styles(self) -> None:
        """在 clam 基础上重配所有用到的控件样式（clam 才允许改配色）。"""
        p = palette()
        style = self.style
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        try:
            line_h = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        except tk.TclError:
            line_h = 15

        # 兜底：所有控件先用窗口底色 + 正文色，避免出现「白底黑字」的漏网之鱼
        style.configure(".", background=p["bg"], foreground=p["text"],
                        fieldbackground=p["field"], bordercolor=p["border"],
                        lightcolor=p["bg"], darkcolor=p["bg"], troughcolor=p["bg"],
                        focuscolor=p["bg"], selectbackground=p["select"],
                        selectforeground=p["select_text"])

        style.configure("TFrame", background=p["bg"])
        style.configure("Card.TFrame", background=p["card"])
        style.configure("TLabel", background=p["bg"], foreground=p["text"])
        style.configure("Card.TLabel", background=p["card"], foreground=p["text"])
        style.configure("Status.TLabel", background=p["bg"], foreground=p["text_dim"])
        # 登录框里的行内提示（显示在「取消 / 登录」按钮左侧）：红色=出错，灰色=进行中
        style.configure("Error.TLabel", background=p["bg"], foreground=p["error"])
        style.configure("Info.TLabel", background=p["bg"], foreground=p["text_dim"])
        style.configure("TSeparator", background=p["border"])
        style.configure("TPanedwindow", background=p["bg"])
        style.configure("Sash", background=p["bg"], gripcount=0, sashthickness=6)

        # ---- 分组框（Win11 的「设置分组」观感：底色同窗口，靠 1px 细边框分隔）----
        style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"],
                        lightcolor=p["bg"], darkcolor=p["bg"], relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=p["bg"], foreground=p["text_dim"],
                        padding=(0, 0, 6, 0))

        # ---- 按钮 ----
        style.configure("TButton", background=p["card"], foreground=p["text"],
                        bordercolor=p["border"], lightcolor=p["card"], darkcolor=p["card"],
                        focuscolor=p["bg"], relief="flat", padding=(9, 3), anchor="center")
        style.map(
            "TButton",
            background=[("disabled", p["card"]), ("pressed", p["pressed"]), ("active", p["hover"])],
            foreground=[("disabled", p["text_dim"])],
            bordercolor=[("pressed", p["border_strong"]), ("active", p["border_strong"]),
                         ("disabled", p["border"])],
            lightcolor=[("pressed", p["pressed"]), ("active", p["hover"]), ("disabled", p["card"])],
            darkcolor=[("pressed", p["pressed"]), ("active", p["hover"]), ("disabled", p["card"])],
        )
        # 主操作（开始刷课）：Win11 的强调色实心按钮
        style.configure("Accent.TButton", background=p["accent"], foreground=p["accent_text"],
                        bordercolor=p["accent"], lightcolor=p["accent"], darkcolor=p["accent"],
                        focuscolor=p["bg"], relief="flat", padding=(10, 3), anchor="center")
        style.map(
            "Accent.TButton",
            background=[("disabled", p["card"]), ("pressed", p["accent_pressed"]),
                        ("active", p["accent_hover"])],
            foreground=[("disabled", p["text_dim"]), ("!disabled", p["accent_text"])],
            bordercolor=[("disabled", p["border"]), ("pressed", p["accent_pressed"]),
                         ("active", p["accent_hover"])],
            lightcolor=[("disabled", p["card"]), ("pressed", p["accent_pressed"]),
                        ("active", p["accent_hover"])],
            darkcolor=[("disabled", p["card"]), ("pressed", p["accent_pressed"]),
                       ("active", p["accent_hover"])],
        )

        # ---- 输入类：白/深输入框 + 聚焦时强调色描边 ----
        field_map = dict(
            fieldbackground=[("readonly", p["card"]), ("disabled", p["bg"])],
            foreground=[("readonly", p["text"]), ("disabled", p["text_dim"])],
            bordercolor=[("focus", p["accent"]), ("!focus", p["border"])],
            lightcolor=[("focus", p["accent"]), ("!focus", p["border"])],
            darkcolor=[("focus", p["accent"]), ("!focus", p["border"])],
        )
        style.configure("TEntry", fieldbackground=p["field"], foreground=p["text"],
                        bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
                        insertcolor=p["text"], padding=(6, 3), relief="flat",
                        selectbackground=p["select"], selectforeground=p["select_text"])
        style.map("TEntry", **field_map)

        style.configure("TSpinbox", fieldbackground=p["field"], foreground=p["text"],
                        background=p["card"], arrowcolor=p["text_dim"],
                        bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
                        insertcolor=p["text"], padding=(6, 3), arrowsize=12, relief="flat",
                        selectbackground=p["select"], selectforeground=p["select_text"])
        style.map("TSpinbox", background=[("active", p["hover"])],
                  arrowcolor=[("active", p["accent"])], **field_map)

        style.configure("TCombobox", fieldbackground=p["field"], foreground=p["text"],
                        background=p["card"], arrowcolor=p["text_dim"],
                        bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
                        padding=(6, 3), arrowsize=12,
                        selectbackground=p["field"], selectforeground=p["text"])
        style.map(
            "TCombobox",
            # readonly 状态下若沿用 selectbackground 会出现「整条被选中」的蓝底
            fieldbackground=[("readonly", p["field"]), ("disabled", p["bg"])],
            selectbackground=[("readonly", p["field"])],
            selectforeground=[("readonly", p["text"])],
            foreground=[("readonly", p["text"]), ("disabled", p["text_dim"])],
            background=[("active", p["hover"]), ("pressed", p["pressed"])],
            arrowcolor=[("active", p["accent"])],
            bordercolor=[("focus", p["accent"]), ("!focus", p["border"])],
            lightcolor=[("focus", p["accent"]), ("!focus", p["border"])],
            darkcolor=[("focus", p["accent"]), ("!focus", p["border"])],
        )

        # ---- 勾选 / 单选：选中态用强调色块 + 反色勾 ----
        for base in ("TCheckbutton", "TRadiobutton"):
            style.configure(base, background=p["bg"], foreground=p["text"],
                            indicatorbackground=p["field"], indicatorforeground=p["text"],
                            indicatorrelief="flat", focuscolor=p["bg"], padding=(2, 2))
            style.map(
                base,
                background=[("active", p["bg"])],
                foreground=[("disabled", p["text_dim"])],
                indicatorbackground=[("disabled", p["bg"]), ("selected", p["accent"]),
                                     ("active", p["hover"]), ("!selected", p["field"])],
                indicatorforeground=[("selected", p["accent_text"])],
            )

        # ---- 列表 ----
        style.configure("Treeview", background=p["field"], fieldbackground=p["field"],
                        foreground=p["text"], bordercolor=p["border"], borderwidth=0,
                        lightcolor=p["field"], darkcolor=p["field"],
                        rowheight=max(22, line_h + 6))
        style.map("Treeview",
                  background=[("selected", p["select"])],
                  foreground=[("selected", p["select_text"])])
        style.configure("Treeview.Heading", background=p["card"], foreground=p["text_dim"],
                        bordercolor=p["border"], lightcolor=p["card"], darkcolor=p["card"],
                        relief="flat", padding=(6, 4))
        style.map("Treeview.Heading", background=[("active", p["hover"])])

        # ---- 滚动条：细、无立体感 ----
        for orient in ("Vertical", "Horizontal"):
            style.configure(f"{orient}.TScrollbar", background=p["scroll"],
                            troughcolor=p["bg"], bordercolor=p["bg"], arrowcolor=p["text_dim"],
                            lightcolor=p["scroll"], darkcolor=p["scroll"],
                            relief="flat", arrowsize=10, width=12)
            style.map(f"{orient}.TScrollbar",
                      background=[("active", p["text_dim"]), ("pressed", p["text_dim"])],
                      arrowcolor=[("active", p["accent"])])


def _hand_off_focus(root: tk.Misc, cb_path: str, x: int, y: int) -> None:
    """把焦点从 ``cb_path`` 这个 combobox 上摘掉（下拉被点外部关掉之后）。

    ``x/y`` 是那下点击的屏幕坐标：焦点优先交给光标底下的控件（课程列表 / 日志框
    之类），它不吃焦点时才退给所在窗口。
    """
    try:
        focused = root.focus_get()
    except (tk.TclError, AttributeError):
        return
    if focused is None or str(focused) != cb_path:
        return                                  # 焦点已经不在它身上了
    target = None
    try:
        target = root.winfo_containing(x, y)
    except tk.TclError:
        target = None
    if target is not None:
        if str(target) == cb_path:
            return                              # 又点了一下自己（收起下拉）：焦点留着
        try:
            target.focus_set()
        except tk.TclError:
            pass
    try:
        still = root.focus_get()
    except (tk.TclError, AttributeError):
        return
    if still is not None and str(still) == cb_path:
        try:                                    # 光标下的控件不吃焦点：退给所在窗口
            focused.winfo_toplevel().focus_set()
        except tk.TclError:
            pass


def install_combobox_focus_fix(root: tk.Misc) -> None:
    """修掉「点开下拉、再点别处关掉后 combobox 仍亮着强调色描边」。

    ttk::combobox 展开时会给 popdown 上全局 grab：外面那一下点击被吞掉（只用来
    Unpost），焦点随后又自动回到 combobox —— 可用户点的是别处，于是描边一直亮着，
    看着像卡住了。这里在 popdown 收到 ``<Button>``（即「点外部关闭」）之后把焦点
    交给光标底下的控件；它不吃焦点就退给所在窗口。

    只对「点击关闭」生效：选中项（``LBSelected`` 里显式 ``focus $cb``）和 Esc 取消
    都保留焦点在 combobox 上，跟 Windows 原生一致。
    """
    global _COMBO_FIX_INSTALLED
    if _COMBO_FIX_INSTALLED:
        return                                  # 类绑定是解释器级的，装一次够了

    def _on_dismiss(event=None):
        try:
            cb_path = root.tk.call("winfo", "parent", str(event.widget))
        except (tk.TclError, AttributeError):
            return
        x, y = getattr(event, "x_root", None), getattr(event, "y_root", None)
        if x is None:
            try:
                x, y = root.winfo_pointerxy()   # 兜底：拿不到事件坐标就用当前光标位置
            except tk.TclError:
                return
        root.after_idle(lambda: _hand_off_focus(root, cb_path, x, y))

    try:
        root.bind_class("ComboboxPopdown", "<Button>", _on_dismiss, add="+")
        _COMBO_FIX_INSTALLED = True
    except tk.TclError:
        pass


def manager() -> "ThemeManager | None":
    return _MANAGER


def register_window(win: tk.Misc) -> None:
    """给新建的 Toplevel 套上主题（无管理器时静默跳过，便于单测直接建窗口）。"""
    if _MANAGER is not None:
        _MANAGER.register(win)
