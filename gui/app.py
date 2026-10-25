# -*- coding: utf-8 -*-
"""SuperStar 桌面客户端 —— tkinter/ttk 原生界面（零 GUI 第三方依赖）。

设计目标：轻量 + 简洁。
  - 不使用 PySide6/Qt，界面完全基于 Python 标准库 tkinter（Windows 原生控件，
    体积仅 Tcl/Tk 运行时，替代了原先约 60 MB 的 Qt 依赖）。
  - 引擎运行在后台线程，事件经 ``queue.Queue`` 回到 Tk 主线程（Tk 非线程安全）。
  - 紧凑三段式布局：账号与控制栏 / 课程与章节 / 运行日志。

界面只调用 engine.StudyEngine 的公开接口，业务逻辑与 UI 保持解耦。
"""
import configparser
import ctypes
import json
import os
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

from api.logger import logger
from engine import StudyEngine
from paths import config_path

APP_TITLE = "SuperStar · 学习通刷课"
MAX_LOG_LINES = 1500          # 日志面板最大行数，超出裁剪，避免长跑占内存
PUMP_MS = 60                  # 主线程轮询队列间隔（毫秒）
MAX_EVENTS_PER_TICK = 300     # 单次最多处理事件数，防止日志洪泛卡界面

STATE_TEXT = {
    "logged_in": "已登录", "switched": "已切换账号", "logging": "登录中",
    "running": "刷课中", "stopping": "停止中", "idle": "空闲",
    "backtrack": "回退重做", "polling": "等待章节开放",
    "read_progress": "已读取进度", "ok": "就绪", "error": "错误", "warn": "提示",
}

# 章节状态 -> (展示文本, 前景色)
CHAPTER_STYLE = {
    "done": ("完成", "#2e7d32"),
    "pending": ("待学", "#424242"),
    "studying": ("进行", "#ef6c00"),
    "error": ("失败", "#c62828"),
}


# --------------------------------------------------------------------------- #
# 环境准备
# --------------------------------------------------------------------------- #
def demo_available() -> bool:
    """演示/自检能力是否随包分发。

    发行包在构建时排除 ``demo_chaoxing`` 模块，此处导入失败即视为不可用；
    测试包包含该模块，因此可启用「演示」按钮与 ``--demo``/``--selftest``。
    """
    global _DEMO_AVAILABLE
    if _DEMO_AVAILABLE is None:
        try:
            import demo_chaoxing  # noqa: F401
            _DEMO_AVAILABLE = True
        except Exception:  # noqa: BLE001
            _DEMO_AVAILABLE = False
    return _DEMO_AVAILABLE


_DEMO_AVAILABLE: "bool | None" = None


def _enable_dpi_awareness() -> None:
    """开启 DPI 感知，避免高分屏下字体模糊（失败静默）。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def _apply_cjk_font(root: tk.Tk) -> str:
    """把默认字体切换到系统 CJK 字体，返回所用字体族名。"""
    try:
        families = set(tkfont.families(root))
    except Exception:  # noqa: BLE001
        return ""
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "SimHei", "SimSun"):
        if name in families:
            for font_name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                try:
                    tkfont.nametofont(font_name).configure(family=name, size=9)
                except Exception:  # noqa: BLE001
                    pass
            return name
    return ""


def resource_path(*parts: str) -> str:
    """定位随程序分发的资源（兼容 PyInstaller 打包后的 _MEIPASS）。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def load_tiku_config() -> dict:
    """读取随包分发的 config_win.ini；不存在则返回空（题库停用）。

    查找顺序：程序目录（压缩包自带，用户可改）> 打包内嵌副本 > 开发态项目根。
    """
    name = "config_win.ini"
    candidates = [config_path()]
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(os.path.join(meipass, name))
    candidates.append(resource_path(name))
    for path in candidates:
        if os.path.exists(path):
            cfg = configparser.ConfigParser()
            try:
                cfg.read(path, encoding="utf8")
            except Exception:  # noqa: BLE001
                continue
            return dict(cfg["tiku"]) if cfg.has_section("tiku") else {}
    return {}


def tiku_config_path() -> str:
    """返回可写的题库配置文件路径（程序目录下的 config_win.ini，压缩包自带）。"""
    return config_path()


# --------------------------------------------------------------------------- #
# 登录对话框
# --------------------------------------------------------------------------- #
class LoginDialog(tk.Toplevel):
    """手机号/密码登录窗口；结果放在 ``self.result``（None 表示取消）。

    ``result`` 为 ``(username, password, use_cookies, remember)``。
    ``remember`` 为 True 时账号（密码经 DPAPI 加密）写入本机账号文件，下次启动自动登录。
    """

    def __init__(self, master: tk.Misc, username: str = "", password: str = "",
                 remember: bool = True):
        super().__init__(master)
        self.title("登录超星学习通")
        self.resizable(False, False)
        self.result = None

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="手机号").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.user_var = tk.StringVar(value=username)
        ttk.Entry(body, textvariable=self.user_var, width=32).grid(row=1, column=0, sticky="ew")

        ttk.Label(body, text="密码").grid(row=2, column=0, sticky="w", pady=(10, 4))
        self.pass_var = tk.StringVar(value=password)
        pw = ttk.Entry(body, textvariable=self.pass_var, show="*", width=32)
        pw.grid(row=3, column=0, sticky="ew")

        self.cookie_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            body, text="使用 progress_cache\\cookies.txt 登录（忽略账号密码）",
            variable=self.cookie_var,
        ).grid(row=4, column=0, sticky="w", pady=(10, 0))

        self.remember_var = tk.BooleanVar(value=remember)
        ttk.Checkbutton(
            body, text="记住账号与密码（本机加密保存，下次启动自动登录）",
            variable=self.remember_var,
        ).grid(row=5, column=0, sticky="w", pady=(4, 0))

        btns = ttk.Frame(body)
        btns.grid(row=6, column=0, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="登录", command=self._on_ok).pack(side="left")

        self.transient(master)
        self.grab_set()
        self.bind("<Return>", lambda _e: self._on_ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._center(master)

    def _center(self, master: tk.Misc) -> None:
        self.update_idletasks()
        try:
            x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
            y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except Exception:  # noqa: BLE001
            pass

    def _on_ok(self) -> None:
        self.result = (
            self.user_var.get().strip(),
            self.pass_var.get(),
            self.cookie_var.get(),
            self.remember_var.get(),
        )
        self.destroy()


# --------------------------------------------------------------------------- #
# 词库 / AI 答题设置对话框
# --------------------------------------------------------------------------- #
class SettingsDialog(tk.Toplevel):
    """词库 / AI 答题设置窗口；结果放在 ``self.result``（None 表示取消）。

    仅编辑 ``[tiku]`` 段，其余段（如 ``[notification]``）原样保留。
    """

    PROVIDERS = ["TikuYanxi", "TikuLike", "TikuAdapter", "AI", "SiliconFlow"]
    DEFAULTS = {
        "provider": "SiliconFlow",
        "submit": "true",
        "cover_rate": "0",
        "delay": "1.0",
        "siliconflow_key": "",
        "siliconflow_model": "deepseek-ai/DeepSeek-V3.2",
        "siliconflow_endpoint": "https://api.siliconflow.cn/v1/chat/completions",
        "min_interval_seconds": "3",
        "http_proxy": "",
        "work_retry": "1",
        "true_list": "正确,对,√,是",
        "false_list": "错误,错,×,否,不对,不正确",
    }
    # (key, 标签, show)；show 用于密码框
    TEXT_FIELDS = (
        ("cover_rate", "最低覆盖率"),
        ("delay", "搜题间隔(秒)"),
        ("work_retry", "不合格重答次数"),
        ("true_list", "判断题正确项"),
        ("false_list", "判断题错误项"),
        ("siliconflow_key", "API Key", "*"),
        ("siliconflow_model", "模型"),
        ("siliconflow_endpoint", "接口地址"),
        ("min_interval_seconds", "最小请求间隔(秒)"),
        ("http_proxy", "HTTP 代理(可留空)"),
    )

    def __init__(self, master: tk.Misc):
        super().__init__(master)
        self.title("词库 / AI 答题设置")
        self.resizable(False, False)
        self.result = None

        cfg = {}
        path = tiku_config_path()
        if os.path.exists(path):
            parser = configparser.ConfigParser()
            try:
                parser.read(path, encoding="utf8")
                if parser.has_section("tiku"):
                    cfg = dict(parser["tiku"])
            except Exception:  # noqa: BLE001
                pass
        self._vals = {k: cfg.get(k, v) for k, v in self.DEFAULTS.items()}

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        self.provider_var = tk.StringVar(value=self._vals["provider"])
        ttk.Label(body, text="题库提供方").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Combobox(body, textvariable=self.provider_var, values=self.PROVIDERS,
                     width=22, state="readonly").grid(row=0, column=1, sticky="w", padx=(8, 0), pady=2)

        self.submit_var = tk.BooleanVar(value=self._vals["submit"].lower() == "true")
        ttk.Checkbutton(body, text="提交答题（取消勾选则仅保存不提交）",
                        variable=self.submit_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 4))

        self.vars: "dict[str, tk.StringVar]" = {}
        row = 2
        for item in self.TEXT_FIELDS:
            key, label, *rest = item
            show = rest[0] if rest else None
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=self._vals[key])
            self.vars[key] = var
            ttk.Entry(body, textvariable=var, width=46, show=show).grid(
                row=row, column=1, sticky="ew", padx=(8, 0), pady=2)
            row += 1

        sep_row = row
        ttk.Separator(body, orient="horizontal").grid(
            row=sep_row, column=0, columnspan=2, sticky="ew", pady=(10, 6))
        ttk.Label(
            body, text="说明：SiliconFlow / AI 共用地址·Key·模型；AI=OpenAI 兼容端点（如 DeepSeek 官方）。其余忽略。",
            style="Status.TLabel",
        ).grid(row=sep_row + 1, column=0, columnspan=2, sticky="w", pady=(0, 2))

        btns = ttk.Frame(body)
        btns.grid(row=sep_row + 2, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="保存", command=self._on_save).pack(side="left")

        self.transient(master)
        self.grab_set()
        self.bind("<Escape>", lambda _e: self.destroy())
        self._center(master)

    def _center(self, master: tk.Misc) -> None:
        self.update_idletasks()
        try:
            x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
            y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except Exception:  # noqa: BLE001
            pass

    def _on_save(self) -> None:
        cfg = dict(self._vals)
        cfg["provider"] = self.provider_var.get()
        cfg["submit"] = "true" if self.submit_var.get() else "false"
        for key, var in self.vars.items():
            cfg[key] = var.get().strip()
        self._save(cfg)
        self.result = cfg
        self.destroy()

    def _save(self, tiku_cfg: dict) -> None:
        path = tiku_config_path()
        parser = configparser.ConfigParser()
        if os.path.exists(path):
            try:
                parser.read(path, encoding="utf8")
            except Exception:  # noqa: BLE001
                pass
        if not parser.has_section("tiku"):
            parser.add_section("tiku")
        for k, v in tiku_cfg.items():
            parser.set("tiku", k, str(v))
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf8") as fp:
                parser.write(fp)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", f"无法写入配置文件：{exc}", parent=self)


# --------------------------------------------------------------------------- #
# 主界面
# --------------------------------------------------------------------------- #
class App:
    """主窗口控制器：界面构建 + 引擎事件编排。"""

    def __init__(self, master: tk.Tk, cache_dir: str | None = None):
        self.root = master
        self.engine = StudyEngine() if cache_dir is None else StudyEngine(cache_dir=cache_dir)
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.courses: list[dict] = []
        self.current_course = None
        self.checked: set[str] = set()       # 勾选的 courseId
        self._chapter_rows: dict[str, str] = {}   # point_id -> tree iid
        self._log_sink_id = None
        self._loading_cid = None                  # 正在读取章节的课程（去重）
        self._acc_uids: dict[str, str] = {}       # 账号下拉显示文本 -> uid
        self.silent = False                       # True 时不弹模态框（截图/自检）

        self._build_ui()
        self._wire_engine()
        self._install_log_sink()
        # 先把已保存账号填进下拉框（无需等待自动登录结果）
        self._ev_accounts(list(self.engine.accounts.keys()))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(PUMP_MS, self._pump)
        # 启动后尝试用本机已保存的账号自动登录（自检/无账号时静默跳过）
        self.root.after(300, self._auto_login)

    # ---------------- 引擎事件 ----------------
    def _wire_engine(self) -> None:
        e = self.engine
        q = self.events.put
        e.on(e.PROGRESS, lambda cid, pid, st: q(("progress", cid, pid, st)))
        e.on(e.CHAPTER, lambda cid, i, total, title: q(("chapter", cid, i, total, title)))
        e.on(e.STATUS, lambda st, msg: q(("status", st, msg)))
        e.on(e.ACCOUNTS, lambda uids: q(("accounts", uids)))
        e.on(e.FINISHED, lambda: q(("finished",)))

    def _install_log_sink(self) -> None:
        self._log_sink_id = logger.add(
            lambda m: self.events.put(("log", m.strip())),
            level="INFO", format="{message}", enqueue=True,
        )

    def _pump(self) -> None:
        """主线程消费引擎事件队列。"""
        try:
            for _ in range(MAX_EVENTS_PER_TICK):
                kind, *args = self.events.get_nowait()
                handler = getattr(self, f"_ev_{kind}", None)
                if handler:
                    handler(*args)
        except queue.Empty:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"界面事件处理异常: {exc}")
        finally:
            self.root.after(PUMP_MS, self._pump)

    # ---------------- 界面构建 ----------------
    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Treeview", rowheight=22)
        style.configure("Status.TLabel", foreground="#455a64")

        outer = ttk.Frame(self.root, padding=(10, 8))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=3, minsize=180)
        outer.rowconfigure(3, weight=2, minsize=150)

        self._build_toolbar(outer).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        # 中部：课程 / 章节
        body = ttk.Panedwindow(outer, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew")
        body.add(self._build_course_pane(body), weight=1)
        body.add(self._build_chapter_pane(body), weight=2)

        self.status_var = tk.StringVar(value="未登录 · 请先添加账号或进入演示模式")
        ttk.Label(outer, textvariable=self.status_var, style="Status.TLabel", anchor="w").grid(
            row=2, column=0, sticky="ew", pady=(6, 4)
        )

        self._build_log_pane(outer).grid(row=3, column=0, sticky="nsew")

    def _build_toolbar(self, parent: tk.Misc) -> ttk.Frame:
        bar = ttk.Frame(parent)
        left = ttk.Frame(bar)
        left.pack(side="left")

        ttk.Label(left, text="账号").pack(side="left")
        self.acc_var = tk.StringVar()
        self.acc_combo = ttk.Combobox(left, textvariable=self.acc_var, width=16, state="readonly")
        self.acc_combo.pack(side="left", padx=(4, 6))
        ttk.Button(left, text="添加账号", width=9, command=self.on_add_account).pack(side="left")
        ttk.Button(left, text="切换", width=6, command=self.on_switch_account).pack(side="left", padx=4)
        ttk.Button(left, text="答题设置", width=9, command=self.on_open_settings).pack(side="left", padx=4)
        if demo_available():
            # 仅测试包出现：发行包已排除 demo_chaoxing 模块
            ttk.Button(left, text="演示", width=6, command=self.on_demo).pack(side="left")

        right = ttk.Frame(bar)
        right.pack(side="right")
        ttk.Label(right, text="倍速").pack(side="left")
        self.speed_var = tk.StringVar(value="1.0")
        ttk.Spinbox(right, from_=1.0, to=2.0, increment=0.5, width=4,
                    textvariable=self.speed_var).pack(side="left", padx=(4, 10))
        ttk.Label(right, text="并发").pack(side="left")
        self.jobs_var = tk.StringVar(value="1")
        ttk.Spinbox(right, from_=1, to=8, width=4, textvariable=self.jobs_var).pack(side="left", padx=(4, 12))

        self.read_btn = ttk.Button(right, text="读取进度", width=9, command=self.on_read_progress)
        self.read_btn.pack(side="left", padx=(0, 6))
        self.start_btn = ttk.Button(right, text="开始刷课", width=10, command=self.on_start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(right, text="停止", width=6, command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        return bar

    def _build_course_pane(self, parent: tk.Misc) -> ttk.Widget:
        box = ttk.Labelframe(parent, text="课程（点击切换勾选）", padding=6)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)

        self.course_tree = ttk.Treeview(box, show="tree", selectmode="browse")
        self.course_tree.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(box, orient="vertical", command=self.course_tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.course_tree.configure(yscrollcommand=bar.set)
        self.course_tree.tag_configure("checked", foreground="#1565c0")
        self.course_tree.bind("<Button-1>", self._on_course_click)
        self.course_tree.bind("<<TreeviewSelect>>", self._on_course_select)

        tools = ttk.Frame(box)
        tools.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(tools, text="全选", width=6, command=lambda: self._set_all_checked(True)).pack(side="left")
        ttk.Button(tools, text="清空", width=6, command=lambda: self._set_all_checked(False)).pack(side="left", padx=4)
        ttk.Button(tools, text="刷新列表", width=10, command=self.on_refresh_courses).pack(side="right")
        return box

    def _build_chapter_pane(self, parent: tk.Misc) -> ttk.Widget:
        box = ttk.Labelframe(parent, text="章节进度", padding=6)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)

        self.chapter_tree = ttk.Treeview(
            box, columns=("state", "title"), show="headings", selectmode="browse"
        )
        self.chapter_tree.heading("state", text="状态")
        self.chapter_tree.heading("title", text="章节")
        self.chapter_tree.column("state", width=56, anchor="center", stretch=False)
        self.chapter_tree.column("title", width=360, anchor="w")
        for key, (_text, color) in CHAPTER_STYLE.items():
            self.chapter_tree.tag_configure(key, foreground=color)
        self.chapter_tree.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(box, orient="vertical", command=self.chapter_tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.chapter_tree.configure(yscrollcommand=bar.set)

        tools = ttk.Frame(box)
        tools.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.chapter_hint = ttk.Label(tools, text="未选择课程", style="Status.TLabel")
        self.chapter_hint.pack(side="left")
        ttk.Button(tools, text="刷新章节", width=10, command=self.on_refresh_chapters).pack(side="right")
        return box

    def _build_log_pane(self, parent: tk.Misc) -> ttk.Widget:
        box = ttk.Labelframe(parent, text="运行日志", padding=6)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)

        self.log_text = tk.Text(box, height=6, wrap="none", state="disabled",
                                borderwidth=0, highlightthickness=0)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(box, orient="vertical", command=self.log_text.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=bar.set)
        self.log_text.tag_configure("warn", foreground="#ef6c00")
        self.log_text.tag_configure("error", foreground="#c62828")
        return box

    # ---------------- 通用 UI 动作 ----------------
    def log(self, message: str) -> None:
        tag = "error" if ("ERROR" in message or "失败" in message) else (
            "warn" if ("WARNING" in message or "警告" in message) else "")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n", tag)
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{lines - MAX_LOG_LINES + 1}.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def set_status(self, state: str, message: str = "") -> None:
        text = STATE_TEXT.get(state, state)
        self.status_var.set(f"{text} · {message}" if message else text)
        if state in ("idle", "error"):
            self._set_running(False)

    def _set_running(self, running: bool) -> None:
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")

    def _tips(self, text: str) -> None:
        if not self.silent:
            messagebox.showinfo("提示", text, parent=self.root)

    # ---------------- 账号 ----------------
    def on_add_account(self) -> None:
        # 已保存过账号时直接回填，通常点一下「登录」即可
        saved = self.engine.saved_account() or {}
        dlg = LoginDialog(
            self.root,
            username=saved.get("username", ""),
            password=saved.get("password", ""),
        )
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        user, pwd, use_cookie, remember = dlg.result
        if not use_cookie and not (user and pwd):
            self._tips("请填写手机号和密码，或勾选 cookies 登录")
            return
        threading.Thread(
            target=self._do_login,
            args=(user, pwd, use_cookie, load_tiku_config(), remember),
            daemon=True,
        ).start()

    def _do_login(self, user: str, pwd: str, use_cookie: bool, tiku: dict,
                  remember: bool = True) -> None:
        try:
            self.events.put(("status", "logging", f"登录中 {user or '(cookies)'}"))
            self.engine.login(user, pwd, use_cookies=use_cookie,
                              tiku_config=tiku, remember=remember)
            self.events.put(("courses", self.engine.list_courses()))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("status", "error", f"登录失败: {exc}"))

    def _auto_login(self) -> None:
        """启动时用本机已保存的账号自动登录；无可用账号则静默跳过。"""
        self._ev_accounts(list(self.engine.accounts.keys()))
        if self.engine.chaoxing is not None:
            return
        if self.engine.saved_account() is None:
            if self.engine.accounts:
                # 只记住了账号名、没存密码（用户当时未勾选「记住密码」）
                self.set_status("warn", "已保存账号但未保存密码，请点「添加账号」登录一次")
            return
        threading.Thread(target=self._do_auto_login, daemon=True).start()

    def _do_auto_login(self) -> None:
        try:
            self.events.put(("status", "logging", "自动登录中…"))
            uid = self.engine.auto_login(load_tiku_config())
            if uid:
                self.events.put(("log", f"【自动登录】已使用本机保存的账号登录（uid={uid}）"))
                self.events.put(("courses", self.engine.list_courses()))
        except Exception as exc:  # noqa: BLE001
            self.events.put((
                "status", "warn",
                f"自动登录失败: {exc}（可在「添加账号」里重新登录一次）",
            ))

    def on_switch_account(self) -> None:
        uid = self._acc_uids.get(self.acc_combo.get(), "")
        if not uid:
            uid = self.acc_combo.get().replace("uid:", "").strip()
        if not uid:
            return
        if self.engine.is_running:
            self._tips("请先停止当前刷课再切换账号")
            return
        threading.Thread(target=self._do_switch, args=(uid,), daemon=True).start()

    def _do_switch(self, uid: str) -> None:
        try:
            # 必须带上题库配置，否则切换后题库会因缺配置被停用
            self.engine.switch_account(uid, load_tiku_config())
            self.events.put(("courses", self.engine.list_courses()))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("status", "error", f"切换失败: {exc}"))

    def on_demo(self) -> None:
        if not demo_available():
            return
        threading.Thread(target=self.load_demo, daemon=True).start()

    def load_demo(self) -> None:
        """加载本地假后端（无需账号/网络），可直接跑完整流程。仅测试包可用。"""
        if not demo_available():
            return
        try:
            self.engine.login_demo()
            self.events.put(("log", "【演示模式】已加载本地假后端，无需账号即可预览全流程"))
            self.events.put(("courses", self.engine.list_courses()))
            self.events.put(("status", "logged_in", "演示模式已就绪"))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("status", "error", f"演示模式加载失败: {exc}"))

    def on_open_settings(self) -> None:
        """打开词库 / AI 答题设置窗口；保存后即时刷新已登录账号的题库配置。"""
        dlg = SettingsDialog(self.root)
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        cfg = dlg.result
        if self.engine.chaoxing is not None:
            try:
                self.engine.apply_tiku_config(cfg)
                self.log("【设置】题库/AI 答题配置已即时生效")
            except Exception as exc:  # noqa: BLE001
                self.log(f"【设置】配置应用失败（将在下次登录生效）: {exc}")
        self._tips("题库/AI 答题设置已保存（config_win.ini）")

    # ---------------- 课程列表 ----------------
    def _acc_label(self, uid: str) -> str:
        """账号下拉显示文本：有手机号就显示「手机号 · uid」，否则只显示 uid。"""
        acc = self.engine.accounts.get(str(uid)) or {}
        name = str(acc.get("username") or "").strip()
        return f"{name} · {uid}" if name and name != str(uid) else str(uid)

    def _ev_accounts(self, uids) -> None:
        uids = [str(u) for u in (uids or [])]
        self._acc_uids = {self._acc_label(u): u for u in uids}
        self.acc_combo.configure(values=list(self._acc_uids.keys()))
        if not uids:
            self.acc_combo.set("")
            return
        current = self.engine.current_uid or self.engine.last_uid or uids[-1]
        if str(current) not in uids:
            current = uids[-1]
        self.acc_combo.set(self._acc_label(str(current)))

    # ---------------- 课程勾选持久化 ----------------
    def _selection_path(self) -> str:
        return os.path.join(str(self.engine.cache.base_dir), "selection.json")

    def _load_selection(self) -> "dict[str, list[str]]":
        path = self._selection_path()
        if os.path.exists(path):
            try:
                with open(path, encoding="utf8") as fp:
                    data = json.load(fp)
                    return data if isinstance(data, dict) else {}
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _save_selection(self) -> None:
        uid = self.engine.current_uid
        if not uid:
            return
        data = self._load_selection()
        data[uid] = sorted(self.checked)
        try:
            with open(self._selection_path(), "w", encoding="utf8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            logger.debug("保存课程勾选状态失败")

    def _apply_saved_selection(self) -> None:
        uid = self.engine.current_uid
        if not uid:
            return
        saved = set(self._load_selection().get(uid, []))
        available = {str(c["courseId"]) for c in self.courses}
        for cid in saved & available:
            self.checked.add(cid)
            course = next(c for c in self.courses if str(c["courseId"]) == cid)
            self.course_tree.item(cid, text=self._course_label(cid, course["title"]),
                                  tags=("checked",))

    def _ev_courses(self, courses: list) -> None:
        self.courses = courses
        self.course_tree.delete(*self.course_tree.get_children())
        self.checked.clear()
        for course in courses:
            cid = str(course["courseId"])
            self.course_tree.insert("", "end", iid=cid, text=self._course_label(cid, course["title"]))
        self._apply_saved_selection()
        if courses:
            first = str(courses[0]["courseId"])
            self.course_tree.selection_set(first)
            self._select_course(first)
        self.set_status("ok", f"共 {len(courses)} 门课程")

    def _course_label(self, cid: str, title: str) -> str:
        return f"{'[√]' if cid in self.checked else '[  ]'} {title}"

    def _on_course_click(self, event) -> None:
        iid = self.course_tree.identify_row(event.y)
        if not iid:
            return
        title = self._course_title(iid)
        if iid in self.checked:
            self.checked.discard(iid)
        else:
            self.checked.add(iid)
        self.course_tree.item(iid, text=self._course_label(iid, title),
                              tags=("checked",) if iid in self.checked else ())
        self._select_course(iid)
        self._save_selection()

    def _on_course_select(self, _event=None) -> None:
        sel = self.course_tree.selection()
        if sel:
            self._select_course(sel[0])

    def _course_title(self, cid: str) -> str:
        course = next((c for c in self.courses if str(c["courseId"]) == cid), None)
        return course["title"] if course else cid

    def _set_all_checked(self, value: bool) -> None:
        self.checked = {str(c["courseId"]) for c in self.courses} if value else set()
        for cid in self.course_tree.get_children():
            self.course_tree.item(
                cid, text=self._course_label(cid, self._course_title(cid)),
                tags=("checked",) if cid in self.checked else (),
            )
        self._save_selection()

    def on_refresh_courses(self) -> None:
        if self.engine.chaoxing is None:
            return self._tips("请先登录")
        threading.Thread(target=self._refresh_courses, daemon=True).start()

    def _refresh_courses(self) -> None:
        try:
            self.events.put(("courses", self.engine.list_courses()))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("status", "error", f"读取课程失败: {exc}"))

    # ---------------- 章节 ----------------
    def _select_course(self, cid: str) -> None:
        course = next((c for c in self.courses if str(c["courseId"]) == cid), None)
        if not course or self._loading_cid == cid:
            return
        self._loading_cid = cid
        self.current_course = course
        self.chapter_hint.configure(text=f"当前课程：{course['title']} · 读取中…")
        threading.Thread(target=self._refresh_chapters, args=(course,), daemon=True).start()

    def _refresh_chapters(self, course: dict) -> None:
        try:
            points = self.engine.get_course_points(course)
        except Exception as exc:  # noqa: BLE001
            self.events.put(("status", "error", f"读取章节失败: {exc}"))
            return
        self.events.put(("points", course, points))

    def _ev_points(self, course: dict, points: list) -> None:
        cid = str(course["courseId"])
        self._loading_cid = None
        self.chapter_tree.delete(*self.chapter_tree.get_children())
        self._chapter_rows.clear()
        done = 0
        for point in points:
            pid = str(point["id"])
            # 章节状态以服务端为准：平台判定「未完成/需重新作答」时不能显示为已完成
            state = "done" if point.get("has_finished") else "pending"
            done += state == "done"
            self.chapter_tree.insert("", "end", iid=pid, values=self._chapter_values(state, point["title"]),
                                     tags=(state,))
            self._chapter_rows[pid] = pid
        self.chapter_hint.configure(text=f"{course['title']} · 已完成 {done}/{len(points)} 章")

    @staticmethod
    def _chapter_values(state: str, title: str) -> tuple:
        return CHAPTER_STYLE.get(state, CHAPTER_STYLE["pending"])[0], title

    def on_refresh_chapters(self) -> None:
        if not self.current_course:
            return self._tips("请先在左侧选择一门课程")
        self._refresh_chapters_async(self.current_course)

    def _refresh_chapters_async(self, course: dict) -> None:
        threading.Thread(target=self._refresh_chapters, args=(course,), daemon=True).start()

    def on_read_progress(self) -> None:
        if not self.current_course:
            return self._tips("请先在左侧选择一门课程")
        threading.Thread(target=self._do_read_progress, args=(self.current_course,), daemon=True).start()

    def _do_read_progress(self, course: dict) -> None:
        try:
            self.events.put(("status", "read_progress", f"课程 {course['title']}"))
            points = self.engine.read_progress(course)
            self.events.put(("points", course, points))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("status", "error", f"读取进度失败: {exc}"))

    # ---------------- 开始 / 停止 ----------------
    def on_start(self) -> None:
        if self.engine.chaoxing is None:
            return self._tips("请先登录（或点击「演示」）")
        selected = [c for c in self.courses if str(c["courseId"]) in self.checked]
        if not selected:
            return self._tips("请至少勾选一门课程")
        try:
            speed = float(self.speed_var.get())
            jobs = int(float(self.jobs_var.get()))
        except ValueError:
            return self._tips("倍速/并发参数无效")
        self._set_running(True)
        self.engine.start_study(selected, speed=speed, jobs=max(1, jobs))

    def on_stop(self) -> None:
        self.engine.stop()

    # ---------------- 引擎事件处理（主线程） ----------------
    def _ev_log(self, message: str) -> None:
        self.log(message)

    def _ev_status(self, state: str, message: str) -> None:
        self.set_status(state, message)

    def _ev_chapter(self, cid: str, index: int, total: int, title: str) -> None:
        self.status_var.set(f"学习中 {index + 1}/{total} · {title}")

    def _ev_progress(self, cid: str, pid: str, status: str) -> None:
        if not self.current_course or cid != str(self.current_course["courseId"]):
            return
        if pid in self._chapter_rows:
            title = self.chapter_tree.set(pid, "title")
            self.chapter_tree.item(pid, values=self._chapter_values(status, title), tags=(status,))
        self._update_chapter_hint()

    def _update_chapter_hint(self) -> None:
        children = self.chapter_tree.get_children()
        if not children or not self.current_course:
            return
        done_text = CHAPTER_STYLE["done"][0]
        done = sum(1 for p in children if self.chapter_tree.set(p, "state") == done_text)
        self.chapter_hint.configure(
            text=f"{self.current_course['title']} · 已完成 {done}/{len(children)} 章"
        )

    def _ev_finished(self) -> None:
        self._set_running(False)
        self.set_status("idle", "任务已全部结束")
        if not self.silent:
            messagebox.showinfo("完成", "刷课任务已全部结束", parent=self.root)

    # ---------------- 关闭 ----------------
    def on_close(self) -> None:
        if self.engine.is_running:
            if not self.silent and not messagebox.askokcancel(
                "退出", "正在刷课，确定退出吗？", parent=self.root
            ):
                return
            self.engine.stop()
        if self._log_sink_id is not None:
            try:
                logger.remove(self._log_sink_id)
            except Exception:  # noqa: BLE001
                pass
        self.root.destroy()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build(demo: bool = False, cache_dir: str | None = None) -> App:
    """创建主窗口（不进入事件循环），供 main / 自检复用。

    ``cache_dir`` 为空时使用程序目录下的 ``progress_cache/``（压缩包自带）。
    ``demo`` 仅在测试包（内置 demo_chaoxing）中可用。
    """
    if demo and not demo_available():
        raise RuntimeError("当前发行包未内置演示模式（仅测试包支持）")
    _enable_dpi_awareness()
    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("980x660")
    root.minsize(840, 560)
    _apply_cjk_font(root)
    app = App(root, cache_dir=cache_dir)
    if demo:
        app.load_demo()
    return app


def main(demo: bool = False) -> None:
    app = build(demo=demo)
    app.root.mainloop()
