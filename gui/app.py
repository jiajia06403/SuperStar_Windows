# -*- coding: utf-8 -*-
"""SuperStar 桌面客户端 —— tkinter/ttk 原生界面（零 GUI 第三方依赖）。

设计目标：轻量 + 简洁。
  - 不使用 PySide6/Qt，界面完全基于 Python 标准库 tkinter（Windows 原生控件，
    体积仅 Tcl/Tk 运行时，替代了原先约 60 MB 的 Qt 依赖）。
  - 引擎运行在后台线程，事件经 ``queue.Queue`` 回到 Tk 主线程（Tk 非线程安全）。
  - 紧凑三段式布局：账号与控制栏 / 课程与章节 / 运行日志。

界面只调用 engine.StudyEngine 的公开接口，业务逻辑与 UI 保持解耦。
"""
import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from api.logger import logger
from config_store import USER_NAME, load_section, save_section, sync_with_default, user_path
from engine import StudyEngine

APP_TITLE = "SuperStar · 学习通刷课"
MAX_LOG_LINES = 1500          # 日志面板最大行数，超出裁剪，避免长跑占内存
PUMP_MS = 60                  # 主线程轮询队列间隔（毫秒）
MAX_EVENTS_PER_TICK = 300     # 单次最多处理事件数，防止日志洪泛卡界面

STATE_TEXT = {
    "logged_in": "已登录", "switched": "已切换账号", "logging": "登录中",
    "running": "刷课中", "stopping": "停止中", "idle": "空闲",
    "backtrack": "回退重做", "polling": "等待章节开放",
    "read_progress": "已读取进度", "reading": "读取进度", "ok": "就绪", "error": "错误", "warn": "提示",
}

# 章节状态 -> (展示文本, 前景色)
CHAPTER_STYLE = {
    "done": ("完成", "#2e7d32"),
    "pending": ("待学", "#424242"),
    "not_open": ("未开放", "#6a1b9a"),
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


def load_tiku_config() -> dict:
    """生效的题库配置：随包默认配置打底 + 用户配置覆盖；都没有则返回空（题库停用）。"""
    try:
        return load_section("tiku")
    except Exception:  # noqa: BLE001
        return {}


def load_common_config() -> dict:
    """生效的通用配置（[common] 段：视频状态日志间隔等）。"""
    try:
        return load_section("common")
    except Exception:  # noqa: BLE001
        return {}


def tiku_config_path() -> str:
    """返回可写的题库配置文件路径（用户配置 config_user.ini，升级解压不会覆盖）。"""
    return user_path()


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
# 设置对话框（答题 / 视频日志）
# --------------------------------------------------------------------------- #
class SettingsDialog(tk.Toplevel):
    """设置窗口（答题 + 视频日志）；结果放在 ``self.result``（None 表示取消）。

    编辑 ``[tiku]`` 与 ``[common]`` 两段，其余段（如 ``[notification]``）原样保留：
      - ``[tiku]``  -> 答案提供方与其配置（``self.result``）
      - ``[common]`` -> 视频状态日志间隔（``self.common_result``）

    交互约定（对齐安卓端的做法）：
      1. 上方下拉选「答案提供方式」，下方只显示该提供方真正会读取的配置项
         （言溪/LIKE 要 TOKEN、TikuAdapter 要服务地址、大模型要地址+Key+模型）；
      2. AI / DeepSeek / SiliconFlow 三种大模型方式各用各的配置键，
         各自预填自己的默认地址与模型（OpenAI 官方 / deepseek-chat / DeepSeek-V3.2）；
      3. 切换提供方只切换显示，不清空已填内容；未填过的项按该提供方的默认值回填。
    """

    DISABLED_LABEL = "（停用：只刷课，不答题）"
    PROVIDERS = ("TikuYanxi", "TikuLike", "TikuAdapter", "AI", "DeepSeek", "SiliconFlow")

    HINTS = {
        "": "题库已停用：只完成视频/文档/阅读任务点，章节检测与作业不自动答题。",
        "TikuYanxi": "言溪题库：只需 TOKEN（https://tk.enncy.cn/ 获取），多个用英文逗号分隔，按顺序使用。",
        "TikuLike": "LIKE 知识库：需要 TOKEN（https://www.datam.site/ 获取）；可分别开关联网搜索与图片识别。",
        "TikuAdapter": "TikuAdapter：填自建服务的查询地址，通常形如 http://127.0.0.1:5000/api/search。",
        "AI": "AI（任意 OpenAI 兼容端点）：接口地址填 base_url，如 https://api.openai.com/v1；"
              "本地/免鉴权端点可勾选「忽略 API Key」。",
        "DeepSeek": "DeepSeek 官方：地址与模型已预填，只需填 API Key（https://platform.deepseek.com/）。",
        "SiliconFlow": "硅基流动：地址与模型已预填，只需填 API Key（https://cloud.siliconflow.cn/account/ak）。",
    }

    # (key, 标签, 类型)；类型：text / secret（密码框）/ bool（勾选框）
    COMMON_FIELDS = (
        ("submit", "提交答题（取消勾选则仅保存不提交）", "bool"),
        ("cover_rate", "最低题库覆盖率（0 表示不要求）", "text"),
        ("delay", "搜题间隔(秒)", "text"),
        ("work_retry", "不合格重答次数（0 不重答）", "text"),
        ("true_list", "判断题正确项", "text"),
        ("false_list", "判断题错误项", "text"),
        ("local_bank", "启用本地题库（只存正确答案，按课程入库，重做/换账号直接读）", "bool"),
        ("local_bank_verify", "提交后核验判分（补缺失 + 纠正答错的题；关闭 = 题库只读）", "bool"),
    )
    # 大模型方式共用的小项（同一份配置键，切换方式时值保留）
    LLM_SHARED_FIELDS = (
        ("min_interval_seconds", "最小请求间隔(秒)", "text"),
        ("http_proxy", "HTTP 代理(可留空)", "text"),
    )
    # [common] 段：观看视频时的状态日志（章节号 / 进度百分比 / 预计剩余）
    RUN_FIELDS = (
        ("video_log_interval", "视频状态日志间隔(秒，0 = 不输出)", "text"),
    )
    RUN_DEFAULTS = {"video_log_interval": "30"}
    PROVIDER_FIELDS = {
        "TikuYanxi": (
            ("tokens", "言溪 TOKEN（多个用英文逗号分隔）", "secret"),
        ),
        "TikuLike": (
            ("tokens", "LIKE 知识库 TOKEN（多个用英文逗号分隔）", "secret"),
            ("likeapi_model", "LIKE 模型名", "text"),
            ("likeapi_search", "启用联网搜索", "bool"),
            ("likeapi_vision", "启用视觉能力（识别图片题）", "bool"),
            ("likeapi_retry", "请求失败自动重试", "bool"),
            ("likeapi_retry_times", "最大重试次数", "text"),
        ),
        "TikuAdapter": (
            ("url", "TikuAdapter 服务地址", "text"),
        ),
        "AI": (
            ("endpoint", "接口地址(base_url)", "text"),
            ("key", "API Key", "secret"),
            ("model", "模型", "text"),
            ("ignore_api_key", "忽略 API Key（自建/免鉴权端点）", "bool"),
        ) + LLM_SHARED_FIELDS,
        "DeepSeek": (
            ("deepseek_endpoint", "接口地址(base_url)", "text"),
            ("deepseek_key", "API Key", "secret"),
            ("deepseek_model", "模型", "text"),
        ) + LLM_SHARED_FIELDS,
        "SiliconFlow": (
            ("siliconflow_endpoint", "接口地址(含 /chat/completions)", "text"),
            ("siliconflow_key", "API Key", "secret"),
            ("siliconflow_model", "模型", "text"),
            ("min_interval_seconds", "最小请求间隔(秒)", "text"),
        ),
    }
    COMMON_DEFAULTS = {
        "submit": "true",
        "cover_rate": "0",
        "delay": "1.0",
        "work_retry": "1",
        "true_list": "正确,对,√,是",
        "false_list": "错误,错,×,否,不对,不正确",
        "local_bank": "true",
        "local_bank_verify": "true",
    }
    # 各提供方专属默认值：决定「切换方式后预填什么」
    PROVIDER_DEFAULTS = {
        "TikuYanxi": {"tokens": ""},
        "TikuLike": {
            "likeapi_search": "false",
            "likeapi_vision": "true",
            "likeapi_model": "glm-4.5-air",
            "likeapi_retry": "true",
            "likeapi_retry_times": "3",
        },
        "TikuAdapter": {"url": ""},
        "AI": {
            "endpoint": "https://api.openai.com/v1",
            "key": "",
            "model": "gpt-3.5-turbo",
            "ignore_api_key": "false",
            "min_interval_seconds": "3",
            "http_proxy": "",
        },
        "DeepSeek": {
            "deepseek_endpoint": "https://api.deepseek.com/v1",
            "deepseek_key": "",
            "deepseek_model": "deepseek-chat",
        },
        "SiliconFlow": {
            "siliconflow_endpoint": "https://api.siliconflow.cn/v1/chat/completions",
            "siliconflow_key": "",
            "siliconflow_model": "deepseek-ai/DeepSeek-V3.2",
        },
    }

    def __init__(self, master: tk.Misc):
        super().__init__(master)
        self.title("设置")
        self.result = None
        self.common_result = None
        self.vars: "dict[str, tuple[str, tk.Variable]]" = {}
        self.panels: "dict[str, ttk.Frame]" = {}
        self._positioned = False
        self._testing = False
        self._test_gen = 0

        cfg = load_tiku_config()
        common_cfg = load_common_config()
        self._base = dict(cfg)                 # 原样保留未在界面出现的键
        self._common_base = dict(common_cfg)   # [common] 段同理
        self._values = self._resolve_values(cfg, common_cfg)

        body = ttk.Frame(self, padding=(14, 12))
        body.pack(fill="both", expand=True)

        head = ttk.Frame(body)
        head.pack(fill="x")
        ttk.Label(head, text="答案提供方式").pack(side="left")
        current = str(cfg.get("provider", "") or "").strip()
        self.provider_var = tk.StringVar(
            value=current if current in self.PROVIDERS else self.DISABLED_LABEL)
        self.provider_cb = ttk.Combobox(
            head, textvariable=self.provider_var, width=26, state="readonly",
            values=(self.DISABLED_LABEL,) + self.PROVIDERS)
        self.provider_cb.pack(side="left", padx=(8, 0))
        self.provider_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_provider_change())

        self.hint_var = tk.StringVar()
        self.hint_lbl = ttk.Label(body, textvariable=self.hint_var, style="Status.TLabel",
                                  wraplength=560, justify="left")
        self.hint_lbl.pack(fill="x", pady=(8, 0))

        self.common_frame = ttk.LabelFrame(body, text="通用设置（所有方式都生效）", padding=(10, 6))
        self.common_frame.pack(fill="x", pady=(10, 0))
        self._build_fields(self.common_frame, self.COMMON_FIELDS)

        # [common] 段：与答题无关的运行参数（观看视频时的状态日志）
        self.run_frame = ttk.LabelFrame(body, text="视频日志", padding=(10, 6))
        self.run_frame.pack(fill="x", pady=(10, 0))
        self._build_fields(self.run_frame, self.RUN_FIELDS)
        ttk.Label(
            self.run_frame,
            text="观看视频时按此间隔在日志输出：章节号 / 视频名 / 进度百分比 / 预计剩余。",
            style="Status.TLabel",
        ).grid(row=len(self.RUN_FIELDS), column=0, columnspan=2, sticky="w", pady=(2, 0))

        self.specific_frame = ttk.LabelFrame(body, text="提供方专属设置", padding=(10, 6))
        for name in self.PROVIDERS:
            panel = ttk.Frame(self.specific_frame)
            self.panels[name] = panel
            self._build_fields(panel, self.PROVIDER_FIELDS[name])
        # 宽度取所有提供方里最宽的一档，避免切换时窗口横向跳动（高度仍按内容收放）
        self.update_idletasks()
        self._width = max([self.winfo_reqwidth()] + [p.winfo_reqwidth() + 40 for p in self.panels.values()])

        # 连通性测试：按钮在左，结果紧随其后（测试在后台线程跑，结果经队列回到主线程）
        self.test_row = ttk.Frame(body)
        self.test_row.pack(fill="x", pady=(10, 0))
        self.test_btn = ttk.Button(self.test_row, text="测试连通性", width=12, command=self._on_test)
        self.test_btn.pack(side="left")
        self.test_state = tk.StringVar(value="")
        self.test_lbl = ttk.Label(self.test_row, textvariable=self.test_state, style="Status.TLabel",
                                  wraplength=460, justify="left")
        self.test_lbl.pack(side="left", padx=(10, 0))

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        self.btns = btns
        ttk.Label(btns, text="保存后立即作用于当前登录账号", style="Status.TLabel").pack(side="left")
        ttk.Button(btns, text="本地题库…", command=self._open_bank).pack(side="left", padx=(10, 0))
        ttk.Button(btns, text="保存", command=self._on_save).pack(side="right")
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=(0, 8))

        self.transient(master)
        try:
            self.grab_set()
        except tk.TclError:      # 窗口尚未上屏时抓焦点会报错，不影响使用
            pass
        self.bind("<Escape>", lambda _e: self.destroy())
        self._on_provider_change()
        self._center(master)

    # ---------------- 初始化 ----------------
    @classmethod
    def _resolve_values(cls, cfg: dict, common_cfg: "dict | None" = None) -> dict:
        """初始值 = 提供方默认值打底 + 配置文件里的非空取值覆盖（[tiku] 与 [common] 都算）。"""
        values: dict = dict(cls.COMMON_DEFAULTS)
        values.update(cls.RUN_DEFAULTS)
        for name in cls.PROVIDERS:
            for key, value in cls.PROVIDER_DEFAULTS.get(name, {}).items():
                values.setdefault(key, value)
        merged = dict(common_cfg or {})
        merged.update(cfg or {})
        for key in list(values):
            raw = str(merged.get(key, "") or "").strip()
            if raw:
                values[key] = raw
        return values

    def _build_fields(self, parent: tk.Misc, fields) -> None:
        parent.columnconfigure(1, weight=1)
        for row, (key, label, kind) in enumerate(fields):
            if key not in self.vars:          # 首次出现：按初值建变量
                current = str(self._values.get(key, "") or "")
                if kind == "bool":
                    var = tk.BooleanVar(value=current.strip().lower() in ("true", "1", "yes", "on"))
                else:
                    var = tk.StringVar(value=current)
                self.vars[key] = (kind, var)
            # 同一个配置键可能出现在多个面板（如最小请求间隔）：必须共用一个变量，
            # 否则两边各改各的，保存时只有后建的那份生效。
            kind, var = self.vars[key]
            if kind == "bool":
                ttk.Checkbutton(parent, text=label, variable=var).grid(
                    row=row, column=0, columnspan=2, sticky="w", pady=1)
            else:
                ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
                ttk.Entry(parent, textvariable=var, width=44,
                          show="*" if kind == "secret" else "").grid(
                    row=row, column=1, sticky="ew", padx=(8, 0), pady=2)

    # ---------------- 提供方切换 ----------------
    def _current_provider(self) -> str:
        text = self.provider_var.get().strip()
        return "" if text == self.DISABLED_LABEL else text

    def _on_provider_change(self) -> None:
        """只显示当前提供方会读取的项；窗口高度随内容收放。"""
        name = self._current_provider()
        self.hint_var.set(self.HINTS.get(name, ""))
        for key, panel in self.panels.items():
            if key == name:
                panel.pack(fill="x")
            else:
                panel.pack_forget()
        if name:
            self.specific_frame.configure(text=f"{name} 专属设置")
            if not self.specific_frame.winfo_manager():
                self.specific_frame.pack(fill="x", pady=(10, 0), after=self.run_frame)
        else:
            self.specific_frame.pack_forget()
        # 停用状态下没有可测试的服务，整行（按钮 + 结果）隐藏
        if name:
            if not self.test_row.winfo_manager():
                self.test_row.pack(fill="x", pady=(10, 0), before=self.btns)
        else:
            self.test_row.pack_forget()
        # 换提供方：作废进行中的测试并清空结果
        self._test_gen += 1
        self._testing = False
        try:
            self.test_btn.state(["!disabled"])
        except tk.TclError:
            pass
        self._set_test_result("")
        self._fit()

    # ---------------- 连通性测试 ----------------
    def _set_test_result(self, text: str, ok: "bool | None" = None) -> None:
        """在按钮右侧显示测试结果；ok=None 表示中性/进行中。"""
        color = "#2e7d32" if ok else ("#c62828" if ok is False else "#616161")
        self.test_state.set(text)
        try:
            self.test_lbl.configure(foreground=color)
        except tk.TclError:          # 窗口已销毁
            pass

    def _on_test(self) -> None:
        """用界面上当前填的值（不必先保存）在后台线程做一次连通性探测。"""
        provider = self._current_provider()
        if not provider or self._testing:
            return
        cfg = self._collect_config(provider)
        box: "queue.Queue" = queue.Queue()      # 每次测试独立队列，避免串到上一次的结果
        self._testing = True
        self._test_gen += 1
        gen = self._test_gen
        try:
            self.test_btn.state(["disabled"])
        except tk.TclError:
            return
        self._set_test_result("测试中…")
        threading.Thread(target=self._test_worker, args=(cfg, box),
                         name="tiku-conn-test", daemon=True).start()
        self.after(120, lambda: self._poll_test(gen, box))

    @staticmethod
    def _test_worker(cfg: dict, box: "queue.Queue") -> None:
        """后台线程：只做网络探测，结果放队列（Tk 控件只能主线程碰）。"""
        try:
            from api.answer import test_tiku_connection
            box.put(test_tiku_connection(cfg))
        except Exception as exc:  # noqa: BLE001
            box.put((False, f"{type(exc).__name__}: {exc}"))

    def _poll_test(self, gen: int, box: "queue.Queue") -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if gen != self._test_gen:        # 期间切换了提供方式，结果作废
            return
        try:
            ok, msg = box.get_nowait()
        except queue.Empty:
            self.after(120, lambda: self._poll_test(gen, box))
            return
        self._testing = False
        try:
            self.test_btn.state(["!disabled"])
        except tk.TclError:
            return
        self._set_test_result(("测试通过 · " if ok else "测试失败 · ") + str(msg), bool(ok))
    # ---------------- 窗口尺寸 / 位置 ----------------
    def _fit(self) -> None:
        """按当前显示的项重设窗口大小，避免切到项少的提供方时留下大片空白。"""
        width = max(self._width, 560)
        # 提示文字按窗口宽度换行：先定换行宽度，能量出准确高度
        self.hint_lbl.configure(wraplength=max(width - 48, 320))
        self.test_lbl.configure(wraplength=max(width - 150, 260))
        self.update_idletasks()
        height = self.winfo_reqheight()
        if self._positioned:
            self.geometry(f"{width}x{height}+{self.winfo_x()}+{self.winfo_y()}")
        else:
            self.geometry(f"{width}x{height}")

    def _center(self, master: tk.Misc) -> None:
        self.update_idletasks()
        try:
            x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
            y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except Exception:  # noqa: BLE001 - 居中失败不影响使用
            return
        self._positioned = True

    # ---------------- 保存 ----------------
    def _collect_config(self, provider: str) -> dict:
        """把界面当前值（含未保存的改动）汇总成一份完整的 ``[tiku]`` 配置。"""
        cfg = dict(self._base)            # 隐藏提供方的旧值原样保留，不写入其界面默认值
        cfg["provider"] = provider
        keys = [key for key, _label, _kind in self.COMMON_FIELDS]
        if provider:
            keys.extend(key for key, _label, _kind in self.PROVIDER_FIELDS[provider])
        for key in dict.fromkeys(keys):
            kind, var = self.vars[key]
            cfg[key] = ("true" if var.get() else "false") if kind == "bool" else str(var.get()).strip()
        # 「忽略 API Key」是 AI 专属开关：换个方式就关掉，避免留下失效开关
        if provider != "AI":
            cfg["ignore_api_key"] = "false"
        return cfg

    def _collect_common(self) -> dict:
        """把界面当前值汇总成一份完整的 ``[common]`` 配置（未显示的键原样保留）。"""
        cfg = dict(self._common_base)
        for key, _label, kind in self.RUN_FIELDS:
            kind, var = self.vars[key]
            cfg[key] = ("true" if var.get() else "false") if kind == "bool" else str(var.get()).strip()
        return cfg

    def _on_save(self) -> None:
        provider = self._current_provider()
        cfg = self._collect_config(provider)
        common_cfg = self._collect_common()
        interval = str(common_cfg.get("video_log_interval", "")).strip()
        if interval and not interval.isdigit():
            messagebox.showerror(
                "参数无效", "视频状态日志间隔请填整数秒（0 表示关闭）。", parent=self)
            return
        if self._save({"tiku": cfg, "common": common_cfg}):
            self.result = cfg
            self.common_result = common_cfg
            self.destroy()

    def _save(self, sections: dict) -> bool:
        """按段写入用户配置（只写 config_user.ini，保留注释与其他段）。"""
        try:
            for section, values in sections.items():
                save_section(section, values)
            return True
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", f"无法写入配置文件：{exc}", parent=self)
            return False

    def _open_bank(self) -> None:
        """打开本地题库管理（用界面上当前的开关值，不必先保存）。"""
        cfg = dict(self._base)
        for key in ("local_bank", "local_bank_verify"):
            kind, var = self.vars[key]
            cfg[key] = "true" if var.get() else "false"
        for key in ("true_list", "false_list"):
            kind, var = self.vars[key]
            cfg[key] = str(var.get()).strip()
        LocalBankDialog(self, cfg).show()


class LocalBankDialog(tk.Toplevel):
    """本地题库管理：查看统计 / 导出 / 导入合并 / 清空 / 打开目录。

    题库按课程存放（``<根目录>/<课程>.json``），课程相同则题目和答案相同，
    因此所有账号共用一份。这里只操作题库文件本身；「开 / 关 / 是否核验」
    在「设置」里配置，这里会把当前生效状态显示出来，避免用户以为改这里就能停用。
    """

    def __init__(self, master: tk.Misc, conf: dict):
        super().__init__(master)
        self.title("本地题库")
        self._conf = dict(conf or {})
        self.bank = self._make_bank()
        self.vars: "dict[str, tk.StringVar]" = {}

        body = ttk.Frame(self, padding=(14, 12))
        body.pack(fill="both", expand=True)

        info = ttk.LabelFrame(body, text="状态", padding=(10, 6))
        info.pack(fill="x")
        self._add_row(info, 0, "题库目录", readonly=True)
        self._add_row(info, 1, "开关状态", readonly=True)

        stat = ttk.LabelFrame(body, text="统计", padding=(10, 6))
        stat.pack(fill="x", pady=(10, 0))
        self._add_row(stat, 0, "课程文件", readonly=True)
        self._add_row(stat, 1, "题目数量", readonly=True)
        self._add_row(stat, 2, "题型分布", readonly=True)
        self._add_row(stat, 3, "占用空间", readonly=True)
        self._add_row(stat, 4, "最近更新", readonly=True)

        ttk.Label(body,
                  text=("题库按课程存放：<课程>.json，只存平台确认过的正确答案，所有账号共用。"
                        "导入会把对方题库合并进来（同题取确认次数更高的答案），不会覆盖其余内容。"),
                  style="Status.TLabel", wraplength=560, justify="left").pack(fill="x", pady=(8, 0))

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="清空…", command=self._on_clear).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="导入合并…", command=self._on_import).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="导出…", command=self._on_export).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="打开目录", command=self._on_open_dir).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="刷新", command=self._refresh).pack(side="left")

        self.bind("<Escape>", lambda _e: self.destroy())
        self._refresh()
        self.geometry("640x380")

    # ---------------- 构建 ----------------
    def _make_bank(self):
        from api.local_bank import BankStore
        return BankStore.from_config(self._conf)

    def _add_row(self, parent: tk.Misc, row: int, label: str,
                 readonly: bool = True) -> tk.StringVar:
        var = tk.StringVar()
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        entry = ttk.Entry(parent, textvariable=var, width=64)
        entry.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=2)
        if readonly:                      # 只读但可选中复制（路径要能拷出来）
            entry.state(["readonly"])
        self.vars[label] = var
        return var

    # ---------------- 数据 ----------------
    def _refresh(self) -> None:
        try:
            stats = self.bank.stats()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("读取失败", f"无法读取本地题库：{exc}", parent=self)
            return
        self.vars["题库目录"].set(stats["root"])
        self.vars["开关状态"].set(
            ("启用" if stats["enabled"] else "已停用")
            + ("，提交后核验判分（补缺失、纠正答错的题）" if stats["verify"] else "，只读不写")
        )
        self.vars["课程文件"].set(
            f"{stats.get('files', 0)} 个课程文件 / {stats['total']} 题"
            if stats.get("files") else "暂无")
        self.vars["题目数量"].set(f"{stats['total']} 题")
        by_type = stats.get("by_type") or {}
        names = {"single": "单选", "multiple": "多选", "judgement": "判断",
                 "completion": "填空", "shortanswer": "简答", "unknown": "未知"}
        self.vars["题型分布"].set(
            " / ".join(f"{names.get(k, k)} {v}" for k, v in sorted(by_type.items())) or "暂无")
        self.vars["占用空间"].set(_human_size(stats.get("size", 0)))
        updated = int(stats.get("updated") or 0)
        self.vars["最近更新"].set(
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated)) if updated else "—")

    # ---------------- 操作 ----------------
    def _on_open_dir(self) -> None:
        """在资源管理器里打开题库目录（方便按课程查看与备份）。"""
        root = self.bank.stats().get("root") or ""
        try:
            os.makedirs(root, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(root)          # noqa: S606 - Windows 资源管理器
            else:
                import subprocess
                subprocess.Popen(["xdg-open", root])  # noqa: S603
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打开失败", f"无法打开目录：\n{root}\n{exc}", parent=self)

    def _on_export(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title="导出本地题库", defaultextension=".json",
            initialfile=time.strftime("本地题库-%Y%m%d.json"),
            filetypes=[("题库文件", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            result = self.bank.export_to(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        messagebox.showinfo(
            "导出完成",
            f"已导出 {result['total']} 题（{result.get('files', 0)} 个课程文件）到：\n{path}",
            parent=self)

    def _on_import(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="选择要合并进来的题库文件",
            filetypes=[("题库文件", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            result = self.bank.import_from(path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导入失败", str(exc), parent=self)
            return
        messagebox.showinfo(
            "导入完成",
            f"新增 {result['added']} 题，合并 {result['merged']} 题，\n"
            f"涉及 {result.get('files', 0)} 个课程文件，当前共 {result['total']} 题。",
            parent=self)
        self._refresh()

    def _on_clear(self) -> None:
        total = self.bank.stats().get("total", 0)
        if not messagebox.askyesno(
                "确认清空",
                f"将删除本地题库里的全部 {total} 道题，此操作不可撤销。\n"
                "建议先「导出…」备份一份。是否继续？", parent=self):
            return
        try:
            removed = self.bank.clear()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("清空失败", str(exc), parent=self)
            return
        messagebox.showinfo("已清空", f"已移除 {removed} 道题。", parent=self)
        self._refresh()

    # ---------------- 显示 ----------------
    def show(self) -> None:
        try:
            self.transient(self.master)
            self.grab_set()
        except tk.TclError:
            pass
        self.wait_window(self)

    def destroy(self) -> None:
        try:                              # 把焦点还给设置窗口，避免设置窗口点不动
            self.grab_release()
            self.master.grab_set()
        except Exception:  # noqa: BLE001
            pass
        super().destroy()


def _human_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# --------------------------------------------------------------------------- #
# 主界面
# --------------------------------------------------------------------------- #
class App:
    """主窗口控制器：界面构建 + 引擎事件编排。"""

    def __init__(self, master: tk.Tk, cache_dir: str | None = None,
                 sync_config: bool = True):
        self.root = master
        self.engine = StudyEngine() if cache_dir is None else StudyEngine(cache_dir=cache_dir)
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.courses: list[dict] = []
        self.current_course = None
        self.checked: set[str] = set()       # 勾选的 courseId
        self._chapter_rows: dict[str, str] = {}   # point_id -> tree iid
        self._log_sink_id = None
        self._loading_cid = None                  # 正在读取章节的课程（去重）
        self._read_gen = 0                        # 「读取进度」代数：切换课程/开始刷课时作废旧任务
        self._reading = False                     # 是否正在读取/探测进度
        self._collect_gen = 0                     # 「采集题库」代数：递增即中止
        self._collecting = False                  # 是否正在采集题库
        self._acc_uids: dict[str, str] = {}       # 账号下拉显示文本 -> uid
        self.silent = False                       # True 时不弹模态框（截图/自检）

        self._build_ui()
        self._wire_engine()
        self._install_log_sink()
        if sync_config:
            self._sync_user_config()
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
        e.on(e.FINISHED, lambda *_: q(("finished",)))

    def _sync_user_config(self) -> None:
        """启动时拿最新的默认配置校验用户配置（升级后自动补齐/废弃/修正）。

        用户配置（config_user.ini）与随包默认配置（config_win.ini）分离：
        升级解压覆盖默认配置不会动到用户填过的取值。
        """
        try:
            report = sync_with_default()
        except Exception as exc:  # noqa: BLE001 - 配置问题不得拖垮启动
            self.log(f"【配置】校验失败，已按默认配置继续: {exc}")
            return
        if not report.get("changed"):
            return
        if report.get("created"):
            self.log(f"【配置】已生成用户配置 {USER_NAME}（升级解压不会覆盖它）")
        for section, keys in report.get("added", {}).items():
            self.log(f"【配置】新版本新增配置项已补齐 [{section}]：{'、'.join(keys)}")
        for section, keys in report.get("obsolete", {}).items():
            self.log(f"【配置】新版本已移除的配置项转入 [obsolete] [{section}]：{'、'.join(keys)}")
        for section, fixes in report.get("fixed", {}).items():
            for key, (old, new) in fixes.items():
                self.log(f"【配置】[{section}] {key} 取值非法（{old}），已回落为 {new}")

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
        ttk.Button(left, text="设置", width=6, command=self.on_open_settings).pack(side="left", padx=4)
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
        self.collect_btn = ttk.Button(right, text="采集题库", width=9, command=self.on_collect_bank)
        self.collect_btn.pack(side="left", padx=(0, 6))
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
        self.collect_btn.configure(state="disabled" if running else "normal")

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
        """打开设置窗口（答题 / 视频日志）；保存后即时作用于当前登录账号。"""
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
        # 视频状态日志间隔：重读 [common] 并推给当前后端（刷课中也能立即生效）
        try:
            interval = self.engine.apply_video_log_config()
            self.log("【设置】视频状态日志已关闭" if interval <= 0
                     else f"【设置】视频状态日志：每 {interval:g} 秒一条")
        except Exception as exc:  # noqa: BLE001
            self.log(f"【设置】视频状态日志配置应用失败: {exc}")
        self._tips(f"设置已保存（{USER_NAME}）")

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
        self._cancel_reading()       # 切课程后旧的探测结果不再适用
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
        for point in points:
            pid = str(point["id"])
            # 章节状态以服务端为准：平台判定「未完成/需重新作答」时不能显示为已完成；
            # 已探测到未开放的章节显示「未开放」，不要笼统地显示「待学」
            state = self._chapter_state(point)
            self.chapter_tree.insert("", "end", iid=pid, values=self._chapter_values(state, point["title"]),
                                     tags=(state,))
            self._chapter_rows[pid] = pid
        self.chapter_hint.configure(text=self._chapter_hint_text(course["title"]))

    @staticmethod
    def _chapter_state(point: dict) -> str:
        if point.get("has_finished"):
            return "done"
        if point.get("not_open"):
            return "not_open"
        return "pending"

    def _chapter_hint_text(self, title: str) -> str:
        children = self.chapter_tree.get_children()
        done = sum(1 for p in children if self.chapter_tree.set(p, "state") == CHAPTER_STYLE["done"][0])
        locked = sum(1 for p in children if self.chapter_tree.set(p, "state") == CHAPTER_STYLE["not_open"][0])
        text = f"{title} · 已完成 {done}/{len(children)} 章"
        if locked:
            text += f" · 未开放 {locked} 章"
        return text

    @staticmethod
    def _chapter_values(state: str, title: str) -> tuple:
        return CHAPTER_STYLE.get(state, CHAPTER_STYLE["pending"])[0], title

    def on_refresh_chapters(self) -> None:
        if not self.current_course:
            return self._tips("请先在左侧选择一门课程")
        self._cancel_reading()
        self._refresh_chapters_async(self.current_course)

    def _refresh_chapters_async(self, course: dict) -> None:
        threading.Thread(target=self._refresh_chapters, args=(course,), daemon=True).start()

    def on_read_progress(self) -> None:
        if not self.current_course:
            return self._tips("请先在左侧选择一门课程")
        if self._reading:
            return self._tips("正在读取进度，请稍候")
        if self.engine.is_running:
            return self._tips("刷课进行中，请先停止再读取进度")
        self._read_gen += 1
        gen = self._read_gen
        self._set_reading(True)
        threading.Thread(target=self._do_read_progress, args=(self.current_course, gen),
                         daemon=True).start()

    def _do_read_progress(self, course: dict, gen: int) -> None:
        """异步读取进度：先取完成状态（1 次请求）渲染，再后台逐章探测「未开放」并逐行刷新。

        探测是限速的（每章 0.5s），全部做完可能十几秒——所以不能等它跑完才刷新界面：
        界面先显示「完成 / 待学」，每探明一章就地把那一行改成「未开放」。
        ``gen`` 用于作废：用户切课程 / 开始刷课时旧任务自行退出，不再改界面。
        """
        cid = str(course["courseId"])

        def stale() -> bool:
            return gen != self._read_gen

        try:
            self.events.put(("status", "reading", f"{course['title']}（读取完成状态）"))
            points = self.engine.refresh_progress(course)
            if stale():
                return
            self.events.put(("points", course, points))

            def on_point(pid: str, not_open, idx: int, total: int) -> None:
                if stale() or not_open is None:
                    return
                self.events.put(("progress", cid, pid, "not_open" if not_open else "pending"))
                if idx == total or idx % 5 == 0:
                    self.events.put(("status", "reading",
                                     f"{course['title']}（探测未开放 {idx}/{total}）"))

            found = self.engine.probe_not_open(course, points, on_point=on_point,
                                               should_stop=stale)
            if stale():
                return
            if found:
                self.events.put(("log", f"【读取进度】{course['title']}：识别到 {found} 个未开放章节"))
            self.events.put(("status", "read_progress",
                             f"课程 {course['title']}" + (f"，未开放 {found} 章" if found else "")))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("status", "error", f"读取进度失败: {exc}"))
        finally:
            self.events.put(("read_done", gen))

    def _set_reading(self, reading: bool) -> None:
        self._reading = reading
        self.read_btn.configure(state="disabled" if reading else "normal")

    def _ev_read_done(self, gen: int) -> None:
        """读取任务结束（只认最新一代，旧任务的收尾不得动按钮）。"""
        if gen == self._read_gen:
            self._set_reading(False)

    def _cancel_reading(self) -> None:
        """作废正在进行的读取任务（切课程 / 开始刷课时调用）。"""
        if self._reading:
            self._read_gen += 1
            self._set_reading(False)

    # ---------------- 采集题库（只读已完成作业） ----------------
    def on_collect_bank(self) -> None:
        """把已完成作业的正确答案采集进本地题库（只读，不提交作业）。"""
        if self.engine.chaoxing is None:
            return self._tips("请先登录（或点击「演示」）")
        if self._collecting:
            return self._tips("正在采集题库，请稍候（可点「停止」中断）")
        if self.engine.is_running:
            return self._tips("刷课进行中，请先停止再采集题库")
        selected = [c for c in self.courses if str(c["courseId"]) in self.checked]
        if not selected and self.current_course:
            selected = [self.current_course]
        if not selected:
            return self._tips("请在左侧勾选课程（或先选中一门课程）")
        names = "、".join(str(c.get("title") or "") for c in selected[:3])
        if len(selected) > 3:
            names += " 等"
        if not self.silent and not messagebox.askokcancel(
            "采集题库",
            f"将从 {len(selected)} 门课程（{names}）已完成的作业里读取正确答案，\n"
            "写入本地题库。全程只读：不提交、不保存草稿、不修改作业。\n\n"
            "章节多的课程需要几分钟，期间可点「停止」中断（已采集的部分保留）。",
            parent=self.root,
        ):
            return
        self._cancel_reading()
        self._collect_gen += 1
        gen = self._collect_gen
        self._collecting = True
        self.start_btn.configure(state="disabled")
        self.collect_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        threading.Thread(target=self._do_collect, args=(selected, gen), daemon=True).start()

    def _do_collect(self, courses: list, gen: int) -> None:
        """后台采集：逐门课遍历章节 -> 读作业页 -> 入库；用代数判断是否被中止。"""
        from api import collect as collect_mod

        def stale() -> bool:
            return gen != self._collect_gen

        summary = {"learned": 0, "read": 0, "unreadable": 0, "courses": 0, "stopped": False}

        def on_event(event: dict) -> None:
            message = str(event.get("message") or "")
            if message:
                self.events.put(("log", message))

        try:
            for index, course in enumerate(courses, 1):
                if stale():
                    summary["stopped"] = True
                    break
                self.events.put(("status", "reading",
                                 f"采集题库 {index}/{len(courses)} · {course.get('title', '')}"))
                try:
                    points = self.engine.get_course_points(course)
                    stats = collect_mod.collect_course(
                        self.engine.chaoxing, course, points=points,
                        on_event=on_event, should_stop=stale)
                except Exception as exc:  # noqa: BLE001
                    self.events.put(("log", f"【采集】{course.get('title', '')} 失败：{exc}"))
                    continue
                summary["learned"] += int(stats.get("learned") or 0)
                summary["read"] += int(stats.get("read") or 0)
                summary["unreadable"] += int(stats.get("unreadable") or 0)
                summary["courses"] += 1
                if stats.get("stopped"):
                    summary["stopped"] = True
                    break
        except Exception as exc:  # noqa: BLE001
            self.events.put(("status", "error", f"采集失败: {exc}"))
        finally:
            self.events.put(("collect_done", gen, summary))

    def _ev_collect_done(self, gen: int, summary: dict) -> None:
        if gen != self._collect_gen:
            return
        self._collecting = False
        self.start_btn.configure(state="normal")
        self.collect_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        text = (f"读完 {summary.get('read', 0)} 份作业，收录 {summary.get('learned', 0)} 题"
                f"（无法识别 {summary.get('unreadable', 0)} 份）")
        if summary.get("stopped"):
            text += "，已中断"
        self.set_status("idle", "采集题库完成 · " + text)
        if not self.silent:
            messagebox.showinfo("采集完成", text, parent=self.root)

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
        self._cancel_reading()       # 刷课期间不再后台探测进度，避免抢请求/抢状态
        self._set_running(True)
        self.engine.start_study(selected, speed=speed, jobs=max(1, jobs))

    def on_stop(self) -> None:
        # 采集题库时「停止」只作废采集（不动刷课状态机）
        if self._collecting:
            self._collect_gen += 1
            self.stop_btn.configure(state="disabled")
            self.set_status("idle", "已请求停止采集…")
            return
        # 立即禁用按钮给出「已收到」的反馈；线程收尾后会经 idle 状态统一恢复
        self.stop_btn.configure(state="disabled")
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
        self.chapter_hint.configure(text=self._chapter_hint_text(self.current_course["title"]))

    def _ev_finished(self) -> None:
        self._set_running(False)
        if getattr(self.engine, "last_stopped", False):
            # 用户主动停止：不弹「完成」框（误导），只把状态与按钮恢复
            self.set_status("idle", "已停止")
            return
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

    ``cache_dir`` 为空时使用程序目录下的 ``progress_cache/``（压缩包自带）；
    指定临时目录（自检/测试）时不做配置迁移，避免在源码目录里生成用户配置。
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
    app = App(root, cache_dir=cache_dir, sync_config=cache_dir is None)
    if demo:
        app.load_demo()
    return app


def main(demo: bool = False) -> None:
    app = build(demo=demo)
    app.root.mainloop()
