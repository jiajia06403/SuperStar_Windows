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
from gui import theme

APP_TITLE = "SuperStar · 学习通刷课"
MAX_LOG_LINES = 1500          # 日志面板最大行数，超出裁剪，避免长跑占内存
PUMP_MS = 60                  # 主线程轮询队列间隔（毫秒）
MAX_EVENTS_PER_TICK = 300     # 单次最多处理事件数，防止日志洪泛卡界面

# 账号下拉最后一项：选中即触发「添加账号」登录框（代替原独立按钮）
ADD_ACCOUNT_LABEL = "+ 添加账号"

STATE_TEXT = {
    "logged_in": "已登录", "switched": "已切换账号", "logging": "登录中",
    "running": "刷课中", "stopping": "停止中", "idle": "空闲",
    "backtrack": "回退重做", "polling": "等待章节开放", "reanswer": "答题不合格重答",
    "read_progress": "已读取进度", "reading": "读取进度", "ok": "就绪", "error": "错误", "warn": "提示",
}

# 章节状态 -> (展示文本, 浅色主题下的前景色)
# 深色主题的前景色见 gui/theme.py 的 CHAPTER_COLORS：这里的颜色只是初值，
# 界面每次套主题都会用 theme.chapter_colors() 重新 tag_configure 一遍。
CHAPTER_STYLE = {
    "done": ("完成", "#2e7d32"),
    "pending": ("待学", "#424242"),
    "not_open": ("未开放", "#6a1b9a"),
    "studying": ("进行", "#ef6c00"),
    "error": ("失败", "#c62828"),
}


# 运行日志面板只显示用户看得懂的进度与结果；答题明细、题库/Token 等技术细节
# 仍然写入 chaoxing.log（排查用），只是不往界面上刷屏。
LOG_PANEL_HIDE = (
    # 答题明细：一次作业几十行「题目 -> 答案」，用户看不懂也没必要看
    "填写答案为", "获取到答案", "获取答案", "随机选择", "随机题",
    "答案类型与题目类型不符", "题库覆盖率", "未取到判分信息", "本地题库写入",
    "本地题库命中", "本次没有可写入的正确答案", "章节检测题库覆盖率",
    "本次不记录题库", "本次不更新本地题库", "已获取到所有信息",
    # 网络 / Token / 解码等内部实现细节
    "TOKEN查询次数不足", "使用Token", "剩余查询次数", "响应消息", "连接检查",
    "课程章节读取成功", "章节任务点读取成功", "课程列表读取完毕",
    "续播进度单位换算", "未找到字体文件", "初始化字体", "null headers",
    "Unknown card type", "未能回读章节检测状态",
)


def _panel_filter(record) -> bool:
    """界面日志面板的过滤器：命中黑名单的技术日志只写文件，不显示。"""
    if record["level"].no >= logger.level("ERROR").no:
        return True          # 真正的错误必须让用户看到
    message = record["message"]
    return not any(key in message for key in LOG_PANEL_HIDE)


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


def load_ui_config() -> dict:
    """生效的外观配置（[ui] 段：theme = system / light / dark）。"""
    try:
        return load_section("ui")
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- #
# 倍速 / 并发：界面取值与持久化
# --------------------------------------------------------------------------- #
SPEED_MIN, SPEED_MAX = 1.0, 2.0
JOBS_MIN, JOBS_MAX = 1, 8
DEFAULT_SPEED, DEFAULT_JOBS = 2.0, 4

# 登录框「取消 / 登录」按钮左侧的行内提示区宽度（像素）。
# 必须给足：最小可用文案「账号或密码错误」约 84px，最长的一条
# 「cookies 已失效，请更新 cookies」约 182px，窄了会被截成「账号或密码…」等于没提示。
MSG_AREA_WIDTH = 210


def _ellipsize_text(text: str, avail_px: int) -> str:
    """按像素宽度截断文本并补省略号（行内提示用，避免把窗口撑宽）。"""
    text = str(text or "")
    if not text:
        return ""
    try:
        font = tkfont.nametofont("TkDefaultFont")
    except tk.TclError:
        return text
    if avail_px <= 0 or font.measure(text) <= avail_px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + "…") <= avail_px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


def _ensure_msg_styles(win: tk.Misc) -> None:
    """确保行内提示用的 ttk 样式存在（对话框可能先于 ThemeManager 创建）。"""
    style = ttk.Style(win)
    p = theme.palette()
    style.configure("Error.TLabel", background=p["bg"], foreground=p["error"])
    style.configure("Info.TLabel", background=p["bg"], foreground=p["text_dim"])


def _login_error_text(exc: Exception) -> str:
    """把登录异常转成简短的中文提示（显示在「取消 / 登录」左侧）。"""
    text = str(exc).strip()
    if not text:
        return "登录失败"
    # 平台返回的常见文案偏啰嗦，压缩成一行短句
    mapping = (
        ("手机号或密码错误", "账号或密码错误"),
        ("密码错误", "账号或密码错误"),
        ("账号不存在", "账号不存在"),
        ("验证码", "验证码错误，请重试"),
        ("cookies 已失效", "cookies 已失效，请更新 cookies"),
        ("网络", "网络异常，请稍后重试"),
    )
    for needle, brief in mapping:
        if needle in text:
            return brief
    # 未识别的原文：按长度粗裁一次，最终还会由 _ellipsize_text 按像素截断
    return text if len(text) <= 40 else text[:39] + "…"


def parse_speed(text) -> "float | None":
    """把界面文本解析成合法倍速：越界取边界，非法返回 None。"""
    try:
        number = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    if number != number:          # NaN
        return None
    return min(SPEED_MAX, max(SPEED_MIN, number))


def parse_jobs(text) -> "int | None":
    """把界面文本解析成合法并发数：越界取边界，非法返回 None。"""
    try:
        number = int(float(str(text).strip()))
    except (TypeError, ValueError):
        return None
    return min(JOBS_MAX, max(JOBS_MIN, number))


def fmt_speed(speed: float) -> str:
    """倍速显示成 1.0 / 1.5 / 2.0 这种一位小数（与 Spinbox 步进一致）。"""
    return f"{speed:.1f}"


def saved_run_params() -> "tuple[float, int]":
    """上次保存的倍速 / 并发（缺失或非法时回落默认值）。"""
    common = load_common_config()
    speed = parse_speed(common.get("speed"))
    jobs = parse_jobs(common.get("jobs"))
    return (DEFAULT_SPEED if speed is None else speed,
            DEFAULT_JOBS if jobs is None else jobs)


def tiku_config_path() -> str:
    """返回可写的题库配置文件路径（用户配置 config_user.ini，升级解压不会覆盖）。"""
    return user_path()


# --------------------------------------------------------------------------- #
# 主题：界面取值与持久化
# --------------------------------------------------------------------------- #
def saved_theme_mode() -> str:
    """用户配置里的主题模式（``system`` / ``light`` / ``dark``，默认跟随系统）。"""
    try:
        return theme.normalize_mode(load_section("ui").get("theme", ""))
    except Exception:  # noqa: BLE001
        return theme.MODE_SYSTEM


# --------------------------------------------------------------------------- #
# 登录对话框
# --------------------------------------------------------------------------- #
class LoginDialog(tk.Toplevel):
    """手机号/密码登录窗口；结果放在 ``self.result``（None 表示取消）。

    ``result`` 为 ``(username, password, use_cookies, remember)``。
    ``remember`` 为 True 时账号（密码经 DPAPI 加密）写入本机账号文件，下次启动自动登录。

    错误提示不另弹对话框：账号/密码为空或登录失败时，错误信息直接显示在
    「取消 / 登录」两个按钮**左侧**（``msg_label``），窗口保留，用户可直接改密码重试。
    """

    def __init__(self, master: tk.Misc, username: str = "", password: str = "",
                 remember: bool = True, error: str = "", on_submit=None):
        super().__init__(master)
        self.title("登录超星学习通")
        self.resizable(False, False)
        self.result = None
        self.on_closed = None            # 窗口关闭回调（App 用它作废在途登录结果）
        self._on_submit = on_submit
        self._busy = False
        _ensure_msg_styles(self)

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="手机号").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.user_var = tk.StringVar(value=username)
        self.user_entry = ttk.Entry(body, textvariable=self.user_var, width=32)
        self.user_entry.grid(row=1, column=0, sticky="ew")

        ttk.Label(body, text="密码").grid(row=2, column=0, sticky="w", pady=(10, 4))
        self.pass_var = tk.StringVar(value=password)
        self.pass_entry = ttk.Entry(body, textvariable=self.pass_var, show="*", width=32)
        self.pass_entry.grid(row=3, column=0, sticky="ew")

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
        # 行内提示区：固定宽度、夹在「取消 / 登录」两个按钮左侧。
        # 宽度按输入框实际像素算，保证出现提示时窗口不会被撑宽。
        self.msg_holder = ttk.Frame(btns, width=160, height=24)
        self.msg_holder.pack_propagate(False)
        self.msg_holder.pack(side="left", padx=(0, 12))
        self.msg_label = ttk.Label(self.msg_holder, text="", style="Info.TLabel", anchor="w")
        self.msg_label.pack(fill="both", expand=True)
        self.cancel_btn = ttk.Button(btns, text="取消", command=self.destroy)
        self.cancel_btn.pack(side="left", padx=(0, 8))
        self.ok_btn = ttk.Button(btns, text="登录", command=self._on_ok)
        self.ok_btn.pack(side="left")

        self.transient(master)
        self.grab_set()
        self.bind("<Return>", lambda _e: self._on_ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._center(master)
        theme.register_window(self)

        self.update_idletasks()
        self._fit_msg_area(body)
        if error:
            self.show_error(error)

    # ---------------- 行内提示 ----------------
    def _fit_msg_area(self, body: tk.Misc) -> None:
        """固定提示区宽度：出错误时窗口宽度不变，短文案完整显示、长文案加省略号。

        不能按「输入框剩余宽度」反推 —— 按钮本身就要占掉 ~180px，反推出来的
        宽度连「账号或密码错误」（~84px）都放不下，文案会被截成「账号或密码…」。
        """
        try:
            width = max(int(MSG_AREA_WIDTH),
                        int(self.user_entry.winfo_reqwidth()) // 3)
            self.msg_holder.configure(width=width)
            self._msg_budget = max(40, width - 6)
        except Exception:  # noqa: BLE001
            self._msg_budget = MSG_AREA_WIDTH - 6

    def _set_msg(self, text: str, style: str) -> None:
        self.msg_label.configure(text=_ellipsize_text(text, self._msg_budget), style=style)

    def show_error(self, message: str) -> None:
        """行内显示错误（红色），不弹模态框；窗口保留，用户可直接改密码重试。"""
        self._set_msg(str(message or "登录失败"), "Error.TLabel")

    def show_info(self, message: str) -> None:
        """行内显示中性提示（灰色）。"""
        self._set_msg(str(message or ""), "Info.TLabel")

    def set_busy(self, busy: bool) -> None:
        """登录进行中：禁用「登录」按钮，避免重复提交。"""
        self._busy = bool(busy)
        self.ok_btn.configure(state="disabled" if busy else "normal")

    def focus_password(self) -> None:
        """失败后把光标放回密码框，用户可以直接改密码重试。

        Toplevel 自己没拿到键盘焦点时，``focus_set()`` 对其子控件无效，
        所以先 ``focus_force()`` 把焦点抢到本对话框，再落到密码框。
        """
        try:
            self.focus_force()
            self.pass_entry.focus_set()
            self.pass_entry.focus_force()
        except Exception:  # noqa: BLE001
            pass

    def succeed(self) -> None:
        """登录成功：记录结果并关闭窗口。"""
        self.result = (
            self.user_var.get().strip(),
            self.pass_var.get(),
            self.cookie_var.get(),
            self.remember_var.get(),
        )
        self.destroy()

    def destroy(self) -> None:
        """关闭时通知宿主（App），避免残留的登录框/在途结果影响后续状态。"""
        try:
            super().destroy()
        finally:
            callback, self.on_closed = self.on_closed, None
            if callback is not None:
                try:
                    callback(self)
                except Exception:  # noqa: BLE001
                    pass

    # ---------------- 交互 ----------------
    def _center(self, master: tk.Misc) -> None:
        self.update_idletasks()
        try:
            x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
            y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except Exception:  # noqa: BLE001
            pass

    def _on_ok(self) -> None:
        if self._busy:
            return
        user = self.user_var.get().strip()
        pwd = self.pass_var.get()
        use_cookie = self.cookie_var.get()
        remember = self.remember_var.get()
        if not use_cookie and not (user and pwd):
            # 账号/密码缺失：行内提示，不弹框、不关窗口
            # （勾选 cookies 的说明就在上方勾选项里，这里不再重复长句）
            self.show_error("请填写手机号和密码")
            return
        self.show_info("登录中…")
        self.set_busy(True)
        if self._on_submit is not None:
            self._on_submit(self, user, pwd, use_cookie, remember)
        else:
            self.result = (user, pwd, use_cookie, remember)
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
        ("work_retry", "不合格重答次数（0 不重答；因此导致下章未开放时无限重答）", "text"),
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
    RUN_DEFAULTS = {"video_log_interval": "10"}
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
        "work_retry": "3",
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
        self.ui_result = None
        self.vars: "dict[str, tuple[str, tk.Variable]]" = {}
        self.panels: "dict[str, ttk.Frame]" = {}
        self._positioned = False
        self._testing = False
        self._test_gen = 0

        cfg = load_tiku_config()
        common_cfg = load_common_config()
        ui_cfg = load_ui_config()
        self._base = dict(cfg)                 # 原样保留未在界面出现的键
        self._common_base = dict(common_cfg)   # [common] 段同理
        self._ui_base = dict(ui_cfg)           # [ui] 段同理
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

        # 外观：主题（跟随系统 / 浅色 / 深色）。放最前面，因为它是全局选项。
        self.ui_frame = ttk.LabelFrame(body, text="外观", padding=(10, 6))
        self.ui_frame.pack(fill="x", pady=(10, 0))
        self.ui_frame.columnconfigure(1, weight=1)
        ttk.Label(self.ui_frame, text="主题").grid(row=0, column=0, sticky="w", pady=2)
        self.theme_var = tk.StringVar(value=theme.label_of(ui_cfg.get("theme")))
        self.theme_cb = ttk.Combobox(
            self.ui_frame, textvariable=self.theme_var, width=14, state="readonly",
            values=[theme.MODE_LABELS[m] for m in theme.MODES])
        self.theme_cb.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=2)
        # 选主题即实时预览：主窗口与设置窗同步换肤，不必点保存
        self.theme_cb.bind("<<ComboboxSelected>>", lambda _e: self._on_theme_preview())
        # 说明压在主题下拉右边同一行：设置窗口已经很高，不再单独占一行
        ttk.Label(self.ui_frame, text="跟随系统 = 随 Windows 深浅色自动切换",
                  style="Status.TLabel").grid(row=0, column=2, sticky="w", padx=(12, 0))

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

        # 保存结果提示（行内，不弹窗）：成功无需提示（关闭即知），失败/参数非法在此给出
        self.save_var = tk.StringVar()
        self.save_lbl = ttk.Label(body, textvariable=self.save_var, style="Status.TLabel")
        self.save_lbl.pack(fill="x", pady=(8, 0))

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(4, 0))
        self.btns = btns
        ttk.Label(btns, text="保存后立即作用于当前登录账号", style="Status.TLabel").pack(side="left")
        ttk.Button(btns, text="本地题库…", command=self._open_bank).pack(side="left", padx=(10, 0))
        ttk.Button(btns, text="保存", command=self._on_save).pack(side="right")
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=(0, 8))

        # 打开前的主窗口主题：点「取消」时回退预览，点「保存」则保留（已落盘）
        self._saved = False
        mgr = theme.manager()
        self._theme_orig_mode = mgr.mode if mgr is not None else None

        self.transient(master)
        try:
            self.grab_set()
        except tk.TclError:      # 窗口尚未上屏时抓焦点会报错，不影响使用
            pass
        self.bind("<Escape>", lambda _e: self.destroy())
        self._on_provider_change()
        self._center(master)
        theme.register_window(self)

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

    # ---------------- 主题实时预览 ----------------
    def _on_theme_preview(self) -> None:
        """下拉选中即实时换肤（主窗口 + 设置窗同步），不必点保存即可看到效果。"""
        mode = theme.mode_from_label(self.theme_var.get())
        mgr = theme.manager()
        if mgr is None:
            return
        try:
            mgr.apply(mode)
        except tk.TclError:      # 窗口已销毁等极端情况，忽略
            pass

    def destroy(self) -> None:
        """点「取消」/ESC 时回退预览到的主题；点「保存」已落盘则保留。"""
        if not self._saved and self._theme_orig_mode is not None:
            mgr = theme.manager()
            if mgr is not None and mgr.mode != self._theme_orig_mode:
                try:
                    mgr.apply(self._theme_orig_mode)
                except tk.TclError:
                    pass
        super().destroy()

    # ---------------- 连通性测试 ----------------
    def _set_test_result(self, text: str, ok: "bool | None" = None) -> None:
        """在按钮右侧显示测试结果；ok=None 表示中性/进行中。"""
        p = theme.palette()
        color = p["ok"] if ok else (p["error"] if ok is False else p["text_dim"])
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

    def _collect_ui(self) -> dict:
        """把界面当前值汇总成一份完整的 ``[ui]`` 配置（未显示的键原样保留）。"""
        cfg = dict(self._ui_base)
        cfg["theme"] = theme.mode_from_label(self.theme_var.get())
        return cfg

    def _on_save(self) -> None:
        provider = self._current_provider()
        cfg = self._collect_config(provider)
        common_cfg = self._collect_common()
        ui_cfg = self._collect_ui()
        interval = str(common_cfg.get("video_log_interval", "")).strip()
        if interval and not interval.isdigit():
            self.save_var.set("视频状态日志间隔请填整数秒（0 表示关闭）。")
            return
        if self._save({"tiku": cfg, "common": common_cfg, "ui": ui_cfg}):
            self.result = cfg
            self.common_result = common_cfg
            self.ui_result = ui_cfg
            self._saved = True
            self.destroy()

    def _save(self, sections: dict) -> bool:
        """按段写入用户配置（只写 config_user.ini，保留注释与其他段）。"""
        try:
            for section, values in sections.items():
                save_section(section, values)
            return True
        except Exception as exc:  # noqa: BLE001
            self.save_var.set(f"保存失败：{exc}")
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

    题库按课程存放（``<根目录>/<课程ID>.json``），课程相同则题目和答案相同，
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
                  text=("题库按课程存放：<课程ID>.json，只存平台确认过的正确答案，所有账号共用。"
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
        theme.register_window(self)

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
        self._studying_cid = None                 # 正在刷课的课程（courseId），界面标「进行中」
        self._collect_gen = 0                     # 「采集题库」代数：递增即中止
        self._collecting = False                  # 是否正在采集题库
        self._stopping = False                    # 是否正在「停止中」（按钮反馈用）
        self._acc_uids: dict[str, str] = {}       # 账号下拉显示文本 -> uid
        self._login_dialog = None                 # 当前打开的登录框（行内报错用）
        self._login_gen = 0                       # 登录代数：登录框关闭/重开即作废旧结果
        self.silent = False                       # True 时不弹模态框（截图/自检）
        # 自检/测试传了临时 cache_dir：只读配置、不写用户配置，避免污染源码目录
        self._can_save_config = sync_config

        # 主题要在建控件之前定下来：ttk 样式对已存在的控件同样生效，
        # 但先上色可以避免首帧闪一下白底。
        self.theme = theme.ThemeManager(self.root)
        self.theme.apply(saved_theme_mode())

        self._build_ui()
        self.theme.add_listener(self._on_theme_changed)
        self._on_theme_changed(self.theme.theme)   # tk 原生控件先按当前主题上色
        self.theme.start_watch()                   # 跟随系统：系统切深色时自动重刷
        self._wire_engine()
        self._install_log_sink()
        if sync_config:
            self._sync_user_config()
        # 先把已保存账号填进下拉框（无需等待自动登录结果）
        self._ev_accounts(list(self.engine.accounts.keys()))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Destroy>", self._on_root_destroy, add="+")
        self._pump_id = self.root.after(PUMP_MS, self._pump)
        # 启动后尝试用本机已保存的账号自动登录（自检/无账号时静默跳过）
        self._auto_login_id = self.root.after(300, self._auto_login)

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
            level="INFO", format="{message}", enqueue=True, filter=_panel_filter,
        )

    def _pump(self) -> None:
        """主线程消费引擎事件队列。"""
        self._pump_id = None
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
            try:
                self._pump_id = self.root.after(PUMP_MS, self._pump)
            except tk.TclError:          # 窗口已销毁
                self._pump_id = None

    def _on_root_destroy(self, event) -> None:
        """窗口销毁时撤掉待触发的定时器。

        不撤的话，进程里再建新窗口时 Tcl 会用旧解释器去执行残留的 after 脚本，
        在 stderr 刷 ``invalid command name ..._pump``（测试里连续建窗口必现）。
        """
        if event.widget is not self.root:
            return
        for attr in ("_pump_id", "_auto_login_id"):
            pending = getattr(self, attr, None)
            if pending is not None:
                try:
                    self.root.after_cancel(pending)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        try:
            self.theme.stop_watch()
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 界面构建 ----------------
    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        # 行高/列宽都按当前字体实测，避免高 DPI（125%/150% 缩放）下
        # 固定 22px 行高与 56px 状态列把汉字裁掉。
        self._ui_font = tkfont.nametofont("TkDefaultFont")
        self._line_h = self._ui_font.metrics("linespace")
        style.configure("Treeview", rowheight=max(22, self._line_h + 6))

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

        self.status_var = tk.StringVar(value="未登录 · 请在账号下拉选择「+ 添加账号」登录，或进入演示模式")
        # 不用 textvariable：状态文本含课程名时可能比窗口还宽，需要按实际宽度
        # 截断加省略号，否则会顶到窗口右边界被硬切（看起来像「字没显示完」）。
        self._status_full = self.status_var.get()
        self.status_label = ttk.Label(outer, text=self._status_full, style="Status.TLabel",
                                      anchor="w")
        self.status_label.grid(row=2, column=0, sticky="ew", pady=(6, 4))
        self.status_label.bind("<Configure>", lambda _e: self._paint_status())

        self._build_log_pane(outer).grid(row=3, column=0, sticky="nsew")

    def _build_toolbar(self, parent: tk.Misc) -> ttk.Frame:
        bar = ttk.Frame(parent)
        # 用 grid 而不是 pack：窗口窄（或系统 125% 缩放）时右侧那一组放不下，
        # 会自动折到第二行，而不是被窗口右边界直接切掉。
        bar.columnconfigure(1, weight=1)
        left = ttk.Frame(bar)
        left.grid(row=0, column=0, sticky="w")

        ttk.Label(left, text="账号").pack(side="left")
        self.acc_var = tk.StringVar()
        self.acc_combo = ttk.Combobox(left, textvariable=self.acc_var, width=16, state="readonly")
        self.acc_combo.pack(side="left", padx=(4, 6))
        # 选中其他账号 = 自动切换；选中末尾「+ 添加账号」= 触发登录框（无需独立按钮）
        self.acc_combo.bind("<<ComboboxSelected>>", self._on_acc_selected)
        ttk.Button(left, text="设置", width=6, command=self.on_open_settings).pack(side="left", padx=4)
        if demo_available():
            # 仅测试包出现：发行包已排除 demo_chaoxing 模块
            ttk.Button(left, text="演示", width=6, command=self.on_demo).pack(side="left")

        right = ttk.Frame(bar)
        right.grid(row=0, column=1, sticky="e")
        ttk.Label(right, text="倍速").pack(side="left")
        speed0, jobs0 = saved_run_params()
        self.speed_var = tk.StringVar(value=fmt_speed(speed0))
        ttk.Spinbox(right, from_=SPEED_MIN, to=SPEED_MAX, increment=0.5, width=4,
                    textvariable=self.speed_var).pack(side="left", padx=(4, 10))
        ttk.Label(right, text="并发").pack(side="left")
        self.jobs_var = tk.StringVar(value=str(jobs0))
        ttk.Spinbox(right, from_=JOBS_MIN, to=JOBS_MAX, width=4,
                    textvariable=self.jobs_var).pack(side="left", padx=(4, 12))

        self.read_btn = ttk.Button(right, text="读取进度", width=9, command=self.on_read_progress)
        self.read_btn.pack(side="left", padx=(0, 6))
        self.collect_btn = ttk.Button(right, text="采集题库", width=9, command=self.on_collect_bank)
        self.collect_btn.pack(side="left", padx=(0, 6))
        self.action_btn = ttk.Button(right, text="开始刷课", width=12, style="Accent.TButton",
                                     command=self.on_action)
        self.action_btn.pack(side="left")

        self._toolbar = (bar, left, right)
        bar.bind("<Configure>", self._reflow_toolbar)
        return bar

    def _reflow_toolbar(self, _event=None) -> None:
        """工具栏放不下一行时把右侧操作组折到第二行（窄窗口 / 高 DPI 缩放）。"""
        bar, left, right = self._toolbar
        avail = bar.winfo_width()
        if avail <= 1:
            return
        need = left.winfo_reqwidth() + right.winfo_reqwidth() + 12
        row = 1 if need > avail else 0
        if int(right.grid_info().get("row", 0)) == row:
            return                       # 已在目标行：直接返回，避免 Configure 无限递归
        if row == 0:
            right.grid_configure(row=0, column=1, columnspan=1, sticky="e", pady=0)
        else:
            right.grid_configure(row=1, column=0, columnspan=2, sticky="e", pady=(6, 0))

    def toolbar_fits(self) -> bool:
        """工具栏是否完整可见（自检用：右侧那一组不得越出工具栏边界）。"""
        bar, _left, right = self._toolbar
        if bar.winfo_width() <= 1:
            return True
        right_edge = right.winfo_rootx() + right.winfo_width()
        bar_edge = bar.winfo_rootx() + bar.winfo_width()
        bottom = right.winfo_rooty() + right.winfo_height()
        bar_bottom = bar.winfo_rooty() + bar.winfo_height()
        return right_edge <= bar_edge + 1 and bottom <= bar_bottom + 1

    def _build_course_pane(self, parent: tk.Misc) -> ttk.Widget:
        box = ttk.Labelframe(parent, text="课程（点击切换勾选）", padding=6)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)

        self.course_tree = ttk.Treeview(box, show="tree", selectmode="browse")
        self.course_tree.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(box, orient="vertical", command=self.course_tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        # 课程名较长时靠横向滚动条查看完整标题，而不是在列边界处被截断
        xbar = ttk.Scrollbar(box, orient="horizontal", command=self.course_tree.xview)
        xbar.grid(row=1, column=0, sticky="ew")
        self.course_tree.configure(yscrollcommand=bar.set, xscrollcommand=xbar.set)
        self.course_tree.tag_configure("checked", foreground="#1565c0")
        self.course_tree.tag_configure("studying", foreground="#ef6c00")
        self.course_tree.bind("<Button-1>", self._on_course_click)
        self.course_tree.bind("<<TreeviewSelect>>", self._on_course_select)

        tools = ttk.Frame(box)
        tools.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
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
        # 状态列宽按最长状态文本（未开放）实测，另留单元格内边距；
        # 固定 56px 在 125% 缩放下会把「未开放」裁掉半个字。
        state_w = max(self._ui_font.measure(t) for t, _c in CHAPTER_STYLE.values()) + 20
        self.chapter_tree.column("state", width=state_w, minwidth=state_w, stretch=False)
        self.chapter_tree.column("title", width=360, minwidth=220, anchor="w")
        for key, (_text, color) in CHAPTER_STYLE.items():
            self.chapter_tree.tag_configure(key, foreground=color)
        self.chapter_tree.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(box, orient="vertical", command=self.chapter_tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.chapter_tree.configure(yscrollcommand=bar.set)

        tools = ttk.Frame(box)
        tools.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        tools.columnconfigure(0, weight=1)
        self._hint_full = "未选择课程"
        # 提示文本用 place 填满固定高度的占位框：这样长文本不会把 pane 请求宽度撑大
        # （撑大后左侧课程列表会被挤窄），截断后的省略号也能正常显示。
        hint_holder = ttk.Frame(tools, height=self._line_h + 8)
        hint_holder.grid(row=0, column=0, sticky="ew")
        hint_holder.grid_propagate(False)
        self.chapter_hint = ttk.Label(hint_holder, text=self._hint_full, style="Status.TLabel",
                                      anchor="w")
        self.chapter_hint.place(x=0, y=0, relwidth=1.0, relheight=1.0)
        # 按钮优先占位，提示占剩下的宽度并自行截断
        self.refresh_chapters_btn = ttk.Button(tools, text="刷新章节", width=10,
                                               command=self.on_refresh_chapters)
        self.refresh_chapters_btn.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.chapter_hint.bind("<Configure>", lambda _e: self._paint_hint())
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
        # 日志里有很长的状态行（视频进度/课程名），wrap=none 时必须给横向滚动条，
        # 否则右侧内容直接被窗口边界切掉、无法查看。
        xbar = ttk.Scrollbar(box, orient="horizontal", command=self.log_text.xview)
        xbar.grid(row=1, column=0, sticky="ew")
        self.log_text.configure(yscrollcommand=bar.set, xscrollcommand=xbar.set)
        self.log_text.tag_configure("warn", foreground="#ef6c00")
        self.log_text.tag_configure("error", foreground="#c62828")
        return box

    # ---------------- 主题 ----------------
    def _on_theme_changed(self, name: str) -> None:
        """主题切换后重上 tk 原生控件与彩色文本的前景色。

        ttk 的配色由 ``theme.ThemeManager`` 直接改样式表；但 ``tk.Text`` 和
        两个 Treeview 的 tag 颜色不在 ttk 管辖范围，必须在这里手动同步，
        否则深色背景下会出现「白底日志」「深绿章节名看不清」这类问题。
        """
        p = theme.palette(name)
        for state, colors in theme.chapter_colors(name).items():
            if state in CHAPTER_STYLE:
                self.chapter_tree.tag_configure(state, foreground=colors)
        self.course_tree.tag_configure("checked", foreground=p["accent"])
        self.course_tree.tag_configure("studying", foreground=theme.chapter_colors(name)["studying"])
        self.log_text.configure(
            bg=p["field"], fg=p["text"], insertbackground=p["text"],
            selectbackground=p["select"], selectforeground=p["select_text"])
        for tag in ("warn", "error"):
            self.log_text.tag_configure(tag, foreground=p[tag])

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
        # see("end") 会把视口横向带到行尾；日志保持左对齐阅读更顺
        self.log_text.xview_moveto(0.0)

    def set_status(self, state: str, message: str = "") -> None:
        text = STATE_TEXT.get(state, state)
        self._set_status_text(f"{text} · {message}" if message else text)
        if state in ("idle", "error"):
            self._refresh_action()

    def _set_status_text(self, full: str) -> None:
        """状态栏文本：原文保留在 status_var，显示时按窗口宽度截断。"""
        self.status_var.set(full)
        self._status_full = full
        self._paint_status()

    def _ellipsize(self, text: str, avail_px: int) -> str:
        """把文本裁剪到不超过 avail_px 像素，末尾补省略号。"""
        font = getattr(self, "_ui_font", None) or tkfont.nametofont("TkDefaultFont")
        if avail_px <= 0 or font.measure(text) <= avail_px:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid] + "…") <= avail_px:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo].rstrip() + "…"

    def _paint_status(self) -> None:
        label = getattr(self, "status_label", None)
        if label is None:
            return
        full = getattr(self, "_status_full", "")
        shown = full
        width = label.winfo_width()
        if width > 1:
            shown = self._ellipsize(full, width - 4)
        if label.cget("text") != shown:
            label.configure(text=shown)

    def _refresh_action(self) -> None:
        """单一「开始/停止」按钮：空闲显示开始，刷课或采集中显示停止。"""
        if self._stopping:
            self.action_btn.configure(text="停止中…", state="disabled")
            return
        busy = bool(getattr(self.engine, "is_running", False)) or bool(self._collecting)
        self.action_btn.configure(
            text="停止" if busy else "开始刷课",
            state="normal",
        )
        self.collect_btn.configure(state="disabled" if busy else "normal")

    def _tips(self, text: str) -> None:
        if not self.silent:
            messagebox.showinfo("提示", text, parent=self.root)

    # ---------------- 账号 ----------------
    def on_add_account(self) -> None:
        """打开登录框；已保存过账号时直接回填，通常点一下「登录」即可。"""
        saved = self.engine.saved_account() or {}
        self._open_login_dialog(
            username=saved.get("username", ""),
            password=saved.get("password", ""),
        )

    def _open_login_dialog(self, username: str = "", password: str = "",
                           error: str = "") -> None:
        """（重）弹登录框。

        ``error`` 非空时直接把原因显示在「取消 / 登录」两个按钮左侧（行内提示），
        不再弹独立的消息框；用户可直接改密码重试。
        """
        old = self._login_dialog
        if old is not None and old.winfo_exists():
            # 已经开着：就地更新提示，不重复弹窗
            old.set_busy(False)
            if error:
                old.show_error(error)
            old.lift()
            old.focus_force()
            return
        self._login_gen += 1
        dlg = LoginDialog(
            self.root,
            username=username,
            password=password,
            error=error,
            on_submit=self._submit_login,
        )
        dlg.on_closed = self._on_login_dialog_closed
        self._login_dialog = dlg

    def _on_login_dialog_closed(self, dlg) -> None:
        """登录框被关闭（取消/成功/被替换）：作废在途的登录结果，避免误弹新框。"""
        if self._login_dialog is dlg:
            self._login_dialog = None
        self._login_gen += 1

    def _submit_login(self, dlg, user: str, pwd: str, use_cookie: bool,
                      remember: bool) -> None:
        """登录框点「登录」：起后台线程执行登录，结果经事件队列回到主线程。"""
        gen = self._login_gen
        threading.Thread(
            target=self._do_login,
            args=(gen, user, pwd, use_cookie, load_tiku_config(), remember),
            daemon=True,
        ).start()

    def _do_login(self, gen: int, user: str, pwd: str, use_cookie: bool,
                  tiku: dict, remember: bool = True) -> None:
        try:
            self.events.put(("status", "logging", f"登录中 {user or '(cookies)'}"))
            self.engine.login(user, pwd, use_cookies=use_cookie,
                              tiku_config=tiku, remember=remember)
            self.events.put(("courses", self.engine.list_courses()))
            self.events.put(("login_result", gen, True, ""))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("status", "error", f"登录失败: {exc}"))
            self.events.put(("login_result", gen, False, _login_error_text(exc)))

    def _auto_login(self) -> None:
        """启动时用本机已保存的账号自动登录；无可用账号则静默跳过。"""
        self._ev_accounts(list(self.engine.accounts.keys()))
        if self.engine.chaoxing is not None:
            return
        saved = self.engine.saved_account()
        if saved is None:
            if self.engine.accounts:
                # 只记住了账号名、没存密码（用户当时未勾选「记住密码」）-> 重弹登录框
                acc = self.engine.accounts.get(str(self.engine.last_uid)) or {}
                self.set_status("warn", "已保存账号但未保存密码，请重新登录")
                self._reopen_login_dialog(
                    username=str(acc.get("username") or ""),
                    error="已保存账号但未保存密码，请输入密码登录",
                )
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
                f"自动登录失败: {exc}（可在账号下拉选择「+ 添加账号」重新登录）",
            ))
            # 记住的账号/密码不对 -> 重弹登录框，让用户直接改密码
            self.events.put(("auto_login_failed", _login_error_text(exc)))

    def _reopen_login_dialog(self, username: str = "", error: str = "") -> None:
        """重弹登录框并带上错误原因（原因显示在「取消 / 登录」左侧）。"""
        if self.silent:
            return
        self._open_login_dialog(username=username, error=error)

    def _on_acc_selected(self, _event=None) -> None:
        """下拉选中回调：选中「添加账号」弹登录框；选中其他账号自动切换。"""
        sel = (self.acc_combo.get() or "").strip()
        if sel == ADD_ACCOUNT_LABEL:
            # 先让下拉回到当前账号，再弹登录框
            self._reset_acc_combo()
            self.on_add_account()
            return
        uid = self._acc_uids.get(sel, "")
        if not uid:
            return
        current = str(self.engine.current_uid or self.engine.last_uid or "")
        if str(uid) == current:
            return  # 选的就是当前账号，无需切换
        if self.engine.is_running:
            self._tips("请先停止当前刷课再切换账号")
            self._reset_acc_combo()
            return
        # 先把下拉复位到当前账号；切换成功后 ACCOUNTS 事件会重新选中新账号，
        # 若切换失败则停留当前账号（由 _do_switch 的异常路径保证不误显示）。
        self._reset_acc_combo()
        threading.Thread(target=self._do_switch, args=(uid,), daemon=True).start()

    def _reset_acc_combo(self) -> None:
        """把账号下拉复位到「当前/最近账号」，避免异步切换失败或弹窗后残留错误项。"""
        cur = self.engine.current_uid or self.engine.last_uid
        cur = str(cur) if cur else ""
        if cur and cur in self.engine.accounts:
            self.acc_combo.set(self._acc_label(cur))
        elif self._acc_uids:
            self.acc_combo.set(next(iter(self._acc_uids.keys())))
        else:
            self.acc_combo.set("")

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
        """打开设置窗口（答题 / 视频日志 / 外观）；保存后即时生效。"""
        dlg = SettingsDialog(self.root)
        self.root.wait_window(dlg)
        if not dlg.result:
            return
        cfg = dlg.result
        # 外观：主题可能变了，立即重刷界面（跟随系统时 mode 未变则无需动作）
        mode = (dlg.ui_result or {}).get("theme")
        if mode is not None and theme.normalize_mode(mode) != self.theme.mode:
            self.theme.apply(mode)
            self.log(f"【设置】界面主题已切换：{theme.label_of(mode)}")
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
        self.log(f"【设置】已保存（{USER_NAME}）")

    # ---------------- 课程列表 ----------------
    def _acc_label(self, uid: str) -> str:
        """账号下拉显示文本：有手机号就显示「手机号 · uid」，否则只显示 uid。"""
        acc = self.engine.accounts.get(str(uid)) or {}
        name = str(acc.get("username") or "").strip()
        return f"{name} · {uid}" if name and name != str(uid) else str(uid)

    def _ev_accounts(self, uids) -> None:
        uids = [str(u) for u in (uids or [])]
        self._acc_uids = {self._acc_label(u): u for u in uids}
        # 末尾固定追加「添加账号」项，选中即触发登录框（见 _on_acc_selected）
        values = list(self._acc_uids.keys())
        values.append(ADD_ACCOUNT_LABEL)
        self.acc_combo.configure(values=values)
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

    def _set_course_studying(self, cid: str) -> None:
        """左侧课程列表把正在刷的课程标为「进行中」。"""
        course = next((c for c in self.courses if str(c["courseId"]) == cid), None)
        if not course:
            return
        label = f"{self._course_label(cid, course['title'])} · 进行中"
        self.course_tree.item(cid, text=label, tags=("studying",))

    def _clear_course_studying(self, cid: str) -> None:
        """还原课程列表条目的文本与勾选态（去掉「进行中」标记）。"""
        course = next((c for c in self.courses if str(c["courseId"]) == cid), None)
        if not course:
            return
        self.course_tree.item(
            cid, text=self._course_label(cid, course["title"]),
            tags=("checked",) if cid in self.checked else (),
        )

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
        self._set_hint(f"当前课程：{course['title']} · 读取中…")
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
        self._set_hint(self._chapter_hint_text(course["title"]))

    @staticmethod
    def _chapter_state(point: dict) -> str:
        if point.get("has_finished"):
            return "done"
        if point.get("not_open"):
            return "not_open"
        return "pending"

    def _set_hint(self, text: str) -> None:
        """设置章节区提示文本（超宽时按像素截断加省略号）。"""
        self._hint_full = text
        self._paint_hint()

    def _paint_hint(self) -> None:
        """把提示文本裁剪到控件实际可用宽度，末尾补省略号，避免被右边界切掉。"""
        full = getattr(self, "_hint_full", "")
        if not full or not hasattr(self, "chapter_hint"):
            return
        width = self.chapter_hint.winfo_width()
        shown = full
        if width > 1:
            shown = self._ellipsize(full, max(40, width - 4))
        if self.chapter_hint.cget("text") != shown:
            self.chapter_hint.configure(text=shown)

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
        """在后台读取进度：取服务端完成状态渲染章节列表。

        ``gen`` 用于作废：用户切课程 / 开始刷课时旧任务自行退出，不再改界面。
        """
        def stale() -> bool:
            return gen != self._read_gen

        try:
            self.events.put(("status", "reading", f"{course['title']}（读取进度）"))
            points = self.engine.refresh_progress(course)
            if stale():
                return
            self.events.put(("points", course, points))
            self.events.put(("status", "read_progress", f"课程 {course['title']}"))
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
        self._refresh_action()
        threading.Thread(target=self._do_collect, args=(selected, gen), daemon=True).start()

    def _do_collect(self, courses: list, gen: int) -> None:
        """后台采集：逐门课遍历章节 -> 读作业页 -> 入库；用代数判断是否被中止。"""
        from api import collect as collect_mod

        def stale() -> bool:
            return gen != self._collect_gen

        summary = {"learned": 0, "read": 0, "unreadable": 0, "courses": 0, "stopped": False,
                   "unsubmitted": 0, "ungraded": 0, "known": 0, "undecided": 0,
                   "closed": 0}

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
                summary["unsubmitted"] += int(stats.get("unsubmitted") or 0)
                summary["ungraded"] += int(stats.get("ungraded") or 0)
                summary["known"] += int(stats.get("known") or 0)
                summary["undecided"] += int(stats.get("undecided") or 0)
                summary["closed"] += int(stats.get("closed") or 0)
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
        self._refresh_action()
        text = f"读完 {summary.get('read', 0)} 份作业，收录 {summary.get('learned', 0)} 题"
        extra = []
        if summary.get("known"):
            extra.append(f"题库已有 {summary['known']} 题")
        if summary.get("undecided"):
            extra.append(f"平台没判对也没给答案 {summary['undecided']} 题")
        if extra:
            text += "（" + "、".join(extra) + "）"
        reasons = []
        if summary.get("unsubmitted"):
            reasons.append(f"{summary['unsubmitted']} 份未提交")
        if summary.get("ungraded"):
            reasons.append(f"{summary['ungraded']} 份未批阅/未公布答案")
        if summary.get("closed"):
            reasons.append(f"{summary['closed']} 份作业已结束")
        if summary.get("unreadable"):
            reasons.append(f"{summary['unreadable']} 份无法识别")
        if reasons:
            text += "，跳过 " + "、".join(reasons)
        if summary.get("stopped"):
            text += "，已中断"
        self.set_status("idle", "采集题库完成 · " + text)
        if not self.silent:
            messagebox.showinfo("采集完成", text, parent=self.root)

    # ---------------- 开始 / 停止 ----------------
    def _save_run_params(self) -> None:
        """把界面上的倍速/并发写进用户配置（下次启动自动恢复）。

        写失败不影响刷课：配置只是「记住上次取值」，不值得因此中断任务。
        自检/测试（临时 cache_dir）不写盘，避免在源码目录生成 config_user.ini。
        """
        if not self._can_save_config:
            return
        speed = parse_speed(self.speed_var.get())
        jobs = parse_jobs(self.jobs_var.get())
        if speed is None or jobs is None:
            return
        try:
            save_section("common", {"speed": fmt_speed(speed), "jobs": str(jobs)})
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"保存倍速/并发失败: {exc}")

    def on_start(self) -> None:
        if self.engine.chaoxing is None:
            return self._tips("请先登录（或点击「演示」）")
        selected = [c for c in self.courses if str(c["courseId"]) in self.checked]
        if not selected:
            return self._tips("请至少勾选一门课程")
        speed = parse_speed(self.speed_var.get())
        jobs = parse_jobs(self.jobs_var.get())
        if speed is None or jobs is None:
            return self._tips("倍速/并发参数无效")
        # 越界输入按边界值纠正并回填界面，让用户看到实际生效的取值
        self.speed_var.set(fmt_speed(speed))
        self.jobs_var.set(str(jobs))
        self._save_run_params()
        self._cancel_reading()       # 刷课期间不再后台探测进度，避免抢请求/抢状态
        self._stopping = False
        if self._studying_cid:       # 清理上一次可能残留的「进行中」标记
            self._clear_course_studying(self._studying_cid)
            self._studying_cid = None
        self.engine.start_study(selected, speed=speed, jobs=jobs)
        self._refresh_action()

    def on_action(self) -> None:
        if self._stopping or self.engine.is_running or self._collecting:
            self.on_stop()
        else:
            self.on_start()

    def on_stop(self) -> None:
        # 采集题库时「停止」只作废采集（不动刷课状态机）
        if self._collecting:
            self._collect_gen += 1
            self.set_status("idle", "已请求停止采集…")
            return
        # 立即把按钮切到「停止中…」给出「已收到」的反馈；线程收尾后会经 idle 状态统一恢复
        if not self._stopping:
            self._stopping = True
            self._refresh_action()
        self.engine.stop()

    # ---------------- 引擎事件处理（主线程） ----------------
    def _ev_log(self, message: str) -> None:
        self.log(message)

    def _ev_login_result(self, gen: int, ok: bool, message: str) -> None:
        """登录结果（主线程）：成功关窗，失败在「取消 / 登录」左侧行内报错。"""
        if gen != self._login_gen:
            return          # 登录框已被关闭/替换，这次结果作废
        dlg = self._login_dialog
        if dlg is None:
            return
        if ok:
            dlg.succeed()
            return
        dlg.set_busy(False)
        dlg.show_error(message or "登录失败")
        dlg.focus_password()

    def _ev_auto_login_failed(self, message: str) -> None:
        """自动登录失败（记住的账号/密码不对）：重弹登录框，原因行内显示。

        不弹独立消息框：登录框会重新弹出，用户直接在「取消 / 登录」左侧
        看到失败原因，改完密码点「登录」即可重试。
        """
        acc = self.engine.accounts.get(str(self.engine.last_uid)) or {}
        username = str(acc.get("username") or "")
        if not username:
            saved = self.engine.saved_account() or {}
            username = str(saved.get("username") or "")
        self._reopen_login_dialog(username=username, error=message or "登录失败")

    def _ev_status(self, state: str, message: str) -> None:
        self.set_status(state, message)

    def _ev_chapter(self, cid: str, index: int, total: int, title: str) -> None:
        self._set_status_text(f"学习中 {index + 1}/{total} · {title}")
        # 正在刷的课程在左侧列表标「进行中」；切到下一门课时自动复位上一门
        if cid != self._studying_cid:
            if self._studying_cid:
                self._clear_course_studying(self._studying_cid)
            self._studying_cid = cid
            self._set_course_studying(cid)

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
        self._set_hint(self._chapter_hint_text(self.current_course["title"]))

    def _ev_finished(self) -> None:
        self._stopping = False
        if self._studying_cid:
            self._clear_course_studying(self._studying_cid)
            self._studying_cid = None
        self._refresh_action()
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
        self._save_run_params()      # 只改倍速/并发没点开始也要记住
        try:
            self.theme.stop_watch()
        except Exception:  # noqa: BLE001
            pass
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
