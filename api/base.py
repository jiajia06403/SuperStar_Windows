# -*- coding: utf-8 -*-
import functools
import inspect
import os
import random
import re
import sys
import threading
import time
from enum import Enum
from hashlib import md5
from typing import Callable, Self, Optional, Literal

import requests
from loguru import logger
from requests import RequestException
from requests.adapters import HTTPAdapter
from tqdm import tqdm

from api.answer import *
from api.answer_check import cut
from api.cipher import AESCipher
from api.config import GlobalConst as gc
from api.cookies import save_cookies, use_cookies
from api.decode import (
    decode_course_list,
    decode_course_point,
    decode_course_card,
    decode_course_folder,
    decode_questions_info,
)
from api.exceptions import MaxRetryExceeded, StudyRequestError
from api import cancel, local_bank, work_verify, wrong_answers


def _progress_stream():
    """tqdm 的输出目标。

    GUI 打包态（PyInstaller --windowed）没有控制台，sys.stderr / sys.stdout 都是 None，
    直接构造 tqdm 会在首次刷新时抛 AttributeError（NoneType has no attribute 'write'），
    刷课线程会因此中断。这里保证返回一个可写流。
    """
    try:
        from safeio import ensure_stdio, safe_stream
        ensure_stdio()
        return safe_stream(sys.stderr, sys.stdout)
    except Exception:  # noqa: BLE001
        return open(os.devnull, "w", encoding="utf-8", errors="replace")


def get_timestamp():
    return str(int(time.time() * 1000))


def _tiku_query(tiku, q_info: dict, course: str = "", course_id: str = "") -> Optional[str]:
    """查题：把课程信息**显式**传给题库（本地题库靠它定位课程文件）。

    自定义题库可能还是旧签名 ``query(q_info)``，这里按签名判断：
    认得 ``course`` 就带上，认不得就退回单参数调用（本地题库查询退化为
    「跨文件找」，仍正确，只是少了当前课程优先）。
    """
    if not course and not course_id:
        return tiku.query(q_info)
    query = getattr(tiku, "query", None)
    if query is None:
        return None
    try:
        params = inspect.signature(query).parameters
        accepts = "course" in params or "course_id" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):  # 内置/无法内省 -> 保守退回旧签名
        accepts = False
    if accepts:
        return query(q_info, course=course, course_id=course_id)
    return query(q_info)


def _wrong_keys(q_info: dict) -> set:
    """这道题被平台判错过的答案键（按选项正文比对，选项乱序也对得上）。"""
    try:
        return wrong_answers.known_keys(str(q_info.get("title") or ""),
                                        str(q_info.get("type") or ""),
                                        str(q_info.get("options") or ""))
    except Exception as exc:  # noqa: BLE001 - 错题本异常不能影响正常作答
        logger.error(f"错题本查询异常：{type(exc).__name__}: {exc}")
        return set()


def avoid_judged_wrong(q_info: dict, answer: str) -> str:
    """提交形态兜底：这个答案已被平台判错 -> 换成没判错过的。

    题库 / 缓存 / AI / 随机四条来源都要过这一关：重做错题时**绝不原样再交同一个
    错答案**，否则平台只会再判一次不合格（「重答却还是同一批答案」的死循环根因）。
    换不出来（所有选项都判错过 / 填空题没有排除法）就原样返回，由平台判分再来一轮。
    """
    if not answer:
        return answer
    known = _wrong_keys(q_info)
    if not known:
        return answer
    title = str(q_info.get("title") or "")
    qtype = str(q_info.get("type") or "")
    options = str(q_info.get("options") or "")
    try:
        if local_bank.answer_key(qtype, answer, options) not in known:
            return answer
    except Exception as exc:  # noqa: BLE001
        logger.error(f"错题比对异常：{type(exc).__name__}: {exc}")
        return answer
    try:
        alt = wrong_answers.pick_submit_answer(qtype, options, known, answer)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"排除法换答案异常：{type(exc).__name__}: {exc}")
        alt = None
    if not alt:
        if str(qtype or "").strip().lower() in ("single", "multiple", "judgement"):
            logger.warning(f"该题所有可提交的答案都已被平台判错，无法再换答案：{title}")
        else:
            logger.info(f"该题型没有排除法可用，原样提交：{title}")
        return answer
    logger.warning(f"答案已被平台判错，改用排除法：{title} {answer} -> {alt}")
    return alt


def _clip_text(text, limit: int) -> str:
    """截断过长文本（章节名/视频名），保证状态日志一行能看完。"""
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _fmt_duration(seconds: float) -> str:
    """秒数 -> 「1时02分03秒」/「2分03秒」/「45秒」。"""
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}时{minutes:02d}分{secs:02d}秒"
    if minutes:
        return f"{minutes}分{secs:02d}秒"
    return f"{secs}秒"


def _normalize_play_time(raw, duration) -> int:
    """把任务卡片里的 playTime 归一到「秒」。

    学习通同一份知识卡片里的 playTime 单位并不稳定：多数是秒，也有接口/账号返回毫秒
    （实测出现过 1252s 的视频 playTime=75000，即 75s 的毫秒值）。单位只能靠真实时长做上界推断：
    - 不超过时长 → 当秒用；
    - 略超时长（≤1.01 倍）→ 服务端取整，按看完处理；
    - 远超时长，但 //1000 后不超过时长 → 当毫秒，换算成秒；
    - 都不成立 → 脏数据，归零重播（绝不能当成「已看完」，否则会瞬间跳过任务点）。

    返回 0..duration 之间的整数秒。
    """
    total = int(duration or 0)
    try:
        value = int(float(raw or 0))
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    if total <= 0:
        return 0
    if value <= total:
        return value
    if value <= total * 1.01:      # 服务端取整导致略超时长，按看完处理
        return total
    if value // 1000 <= total:     # 毫秒值（远大于时长，不可能是秒）
        return value // 1000
    logger.warning("playTime 异常（{}s 的视频报 {}），已按 0 处理", total, value)
    return 0


# 视频任务点「观看进度要求」：播到该比例后开始「复查」——
# 平台此时若仍未判通过，就继续再看 VIDEO_FINISH_RECHECK_SECONDS 秒再上报，
# 如此循环直到平台判通过（isPassed）或整段看完，绝不因为「比例到了」而中断观看。
# 取值范围限定 90~100（低于 90 没有意义且容易被手改成「不看也过」）；
# 100 = 旧行为（从结尾开始复查）。
# 由引擎按 [common] video_finish_percent 写入 self.video_finish_percent。
DEFAULT_VIDEO_FINISH_PERCENT = 90
MIN_VIDEO_FINISH_PERCENT = 90
MAX_VIDEO_FINISH_PERCENT = 100

# 到达观看比例后平台仍未判通过时的复查间隔（**真实秒**）。
# 含义：不中断观看，继续再看 5 秒再上报一次，如此循环，直到平台判通过或整段看完。
# 用真实时间而不是播放位置：倍速下「看 5 秒」才符合用户直觉。
VIDEO_FINISH_RECHECK_SECONDS = 5.0


def _clamp_finish_percent(value: float) -> float:
    return min(float(MAX_VIDEO_FINISH_PERCENT),
               max(float(MIN_VIDEO_FINISH_PERCENT), float(value)))


def normalize_finish_percent(raw, default: float = DEFAULT_VIDEO_FINISH_PERCENT) -> float:
    """把任意来源的观看比例收敛到 90~100；空值/非法值回落 ``default``。

    边界收敛而不是抛错：脏数据绝不能让任务点变成「必须看到 N 倍时长」而永久卡死，
    也不会低于 90%（那等于允许不看就判完成）。
    """
    text = "" if raw is None else str(raw).strip()
    if text == "":
        return _clamp_finish_percent(default)
    try:
        value = float(text)
    except (TypeError, ValueError):
        logger.warning("观看进度百分比取值非法，已使用默认值 {}: {!r}", default, raw)
        return _clamp_finish_percent(default)
    return _clamp_finish_percent(value)


def _video_finish_target(duration, percent) -> int:
    """观看进度达标所需的秒数：``duration * percent%``，收敛到 ``0..duration``。

    返回 duration 表示与旧行为一致（必须看完）。percent 的下界是 90（见
    :data:`MIN_VIDEO_FINISH_PERCENT`），所以这里恒为正数。
    """
    total = int(duration or 0)
    if total <= 0:
        return 0
    pct = normalize_finish_percent(percent)
    return min(total, max(1, int(round(total * pct / 100.0))))


# 任务点请求的重试策略：最多 3 次、指数退避（1s -> 2s）
# （测试里可下调 REQUEST_BACKOFF_BASE 加速；两者都是模块级常量，改一处即全局生效）
REQUEST_MAX_RETRIES = 3
REQUEST_BACKOFF_BASE = 1.0


def _request_json(session, method, url, *, what, params=None, data=None, headers=None,
                  max_retries: Optional[int] = None, base_delay: Optional[float] = None):
    """带状态码检查 + 有限重试的 JSON 请求（任务点级别的网络护栏）。

    - 非 200 / 网络异常 / 响应不是合法 JSON 都会重试，最多 ``max_retries`` 次，
      退避间隔指数增长（base_delay, 2*base_delay, ...）；
    - **每次重试前都会 ``cancel.check()``**：点了「停止」立刻中断，不会继续发请求、
      也不会在退避里睡满；
    - 重试耗尽抛出 :class:`StudyRequestError`，由上层收敛为 ``StudyResult.ERROR``，
      单点失败不至于穿透到刷课主循环。

    注意：不要在这里传 ``timeout`` —— SessionManager 已用
    ``functools.partial(request, timeout=5)`` 固定，再传会触发重复关键字实参报错。
    """
    retries = max(1, int(max_retries if max_retries is not None else REQUEST_MAX_RETRIES))
    delay0 = max(0.0, float(base_delay if base_delay is not None else REQUEST_BACKOFF_BASE))
    last_reason = ""
    for attempt in range(1, retries + 1):
        if attempt > 1:
            cancel.check()
            delay = delay0 * (2 ** (attempt - 2))
            logger.warning(f"{what}失败，{delay:g}s 后重试({attempt}/{retries}): {last_reason}")
            if cancel.wait(delay):
                raise cancel.Cancelled()
        try:
            if method == "post":
                resp = session.post(url, data=data, params=params, headers=headers)
            else:
                resp = session.get(url, params=params, headers=headers)
        except RequestException as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
            continue
        status = getattr(resp, "status_code", None)
        if status != 200:
            last_reason = f"HTTP {status}: {str(getattr(resp, 'text', '') or '')[:200]}"
            continue
        try:
            return resp.json()
        except ValueError as exc:
            last_reason = f"响应不是合法 JSON: {exc}"
            continue
    raise StudyRequestError(f"{what}失败（已重试 {retries} 次）: {last_reason}")


# 平台**阻断提交**的响应文案特征。两种情况都意味着「继续提交下去没有意义」：
#   1) 作业重做次数用尽（msg 里带「重做次数过多」）；
#   2) 平台弹出图形验证码 / 人机验证（实测「提交失败，重做次数过多！」的真实原因
#      往往就是验证码拦截，msg 具有误导性，所以两类一起当阻断处理）。
# 只有命中这些词才算「不可恢复」，普通网络错误 / 覆盖率不足都不算 ——
# 误判会让整门课被过早放弃，所以宁可少判（继续走原有的重答流程）。
_WORK_BLOCK_MARKS = (
    ("重做次数", "作业的重做次数已用尽"),
    ("次数过多", "作业的重做次数已用尽"),
    ("次数已用尽", "作业的重做次数已用尽"),
    ("次数用完", "作业的重做次数已用尽"),
    ("超过重做", "作业的重做次数已用尽"),
    ("不能重做", "平台不允许重做"),
    ("不允许重做", "平台不允许重做"),
    ("不能再做", "平台不允许重做"),
    ("无法再次提交", "平台不允许重做"),
    ("验证码", "平台要求图形验证码"),
    ("人机验证", "平台要求图形验证码"),
    ("安全验证", "平台要求图形验证码"),
    ("滑动验证", "平台要求图形验证码"),
    ("verify", "平台要求图形验证码"),
)
# 命中后提示用户怎么处理（GUI 面板/日志都看得到）
CAPTCHA_HINT = "请到浏览器打开该课程手动完成验证码（或稍后重试），本次刷课已停止"


def work_block_reason(msg) -> str:
    """``msg`` 是否表示「平台阻断提交」；返回可读原因，未命中返回空串。"""
    text = str(msg or "")
    if not text:
        return ""
    low = text.lower()
    for mark, reason in _WORK_BLOCK_MARKS:
        if mark in text or mark.lower() in low:
            return reason
    return ""


# 兼容旧名（历史调用方/测试用的是这个语义）
def work_retry_refused(msg) -> bool:
    return bool(work_block_reason(msg))




class SessionManager:
    """进程内唯一的 ``requests.Session`` 持有者（单例 + 惰性构造）。

    坑（已修）：``__new__`` 只能保证「实例对象」唯一，``__init__`` 在每次
    ``SessionManager()`` 时都会照常执行。原实现把建会话写在 ``__init__`` 里，
    于是 ``get_session()`` 每调用一次就：新建 Session → 重挂 adapter →
    再包一层 timeout partial → 从 cookies.txt 重新 load 一遍。
    后果是连接池 / TLS 每请求重建，且服务端刚下发的 Set-Cookie 会被磁盘上的
    旧值覆盖（会话漂移，403 / 验证码的诱因之一）。

    现在构造逻辑下沉到 ``_build()``，由锁保护且只跑一次；``__init__`` 只走
    一次属性判断的空转。语义不变：``update_cookies()`` 仍是把磁盘 cookie 合并进
    当前会话，``clear_session()`` 仍是丢弃当前会话（下次访问时重建）。
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._session = None
                    cls._instance = instance
        return cls._instance

    def __init__(self):
        self._build()

    def _build(self) -> requests.Session:
        """创建会话（只执行一次）；返回当前会话。"""
        session = getattr(self, "_session", None)
        if session is not None:
            return session
        with self._lock:
            session = getattr(self, "_session", None)
            if session is not None:
                return session

            session = requests.Session()
            session.mount("https://", HTTPAdapter(max_retries=10))
            session.mount("http://", HTTPAdapter(max_retries=10))
            # timeout 只允许包一层：partial 叠加会让每次请求多一层无谓封装
            if not isinstance(getattr(session, "request", None), functools.partial):
                session.request = functools.partial(session.request, timeout=5)
            # For debug purposes
            # session.verify=False
            session.headers.clear()
            session.headers.update(gc.HEADERS)
            session.cookies.update(use_cookies())
            self._session = session
            return session

    @classmethod
    def get_instance(cls) -> Self:
        instance = cls()
        instance._build()
        return instance

    @classmethod
    def get_session(cls) -> requests.Session:
        return cls.get_instance()._build()

    @classmethod
    def update_cookies(cls):
        """把 cookies.txt 的内容合并进当前会话（不删会话里已有的其他 cookie）。"""
        cls.get_session().cookies.update(use_cookies())

    @classmethod
    def clear_session(cls):
        """丢弃当前会话并关闭其连接池，下次访问时按当前磁盘 cookie 重建。"""
        with cls._lock:
            instance = cls._instance
            session = getattr(instance, "_session", None) if instance else None
            if instance is not None:
                instance._session = None
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass


class Account:
    username = None
    password = None
    last_login = None
    isSuccess = None

    def __init__(self, _username, _password):
        self.username = _username
        self.password = _password


class RateLimiter:
    """按线程隔离的速率限制器。

    旧实现用「一把全局锁 + 单个 last_call」把整个引擎的所有请求串行化，
    导致「任务点并发 > 1」时 N 个 worker 全卡在同一把锁上、等价于串行，
    并发完全失效。SuperStar 安卓版用 jobs=5 仍能提速，正是因为**每路各自
    限速、彼此并行**。

    现在 ``last_call`` 下沉到 ``threading.local``：每个 worker 线程按
    ``call_interval`` 独立限速、互不阻塞。单路 pacing 语义不变（仍是「每条
    请求间隔 ≥ call_interval」这一道反验证码闸），但 5 路任务点真正并行，
    与安卓行为一致。
    """

    def __init__(self, call_interval):
        self.call_interval = call_interval
        self._local = threading.local()

    def limit_rate(self, random_time=False, random_min=0.0, random_max=1.0):
        if random_time:
            time.sleep(random.uniform(random_min, random_max))
        now = time.time()
        last = getattr(self._local, "last_call", None)
        if last is None:
            # 本线程首次调用：仅打点，不等待（避免首条请求被无谓延后）
            self._local.last_call = now
            return
        elapsed = now - last
        if elapsed <= self.call_interval:
            time.sleep(self.call_interval - elapsed)
            self._local.last_call = time.time()
            return
        self._local.last_call = now


class StudyResult(Enum):
    SUCCESS = 0
    FORBIDDEN = 1  # 403
    ERROR = 2
    TIMEOUT = 3
    # 已保存但未提交（题库覆盖率不足），平台侧仍需「重新作答」，
    # 因此不能视为本章已完成，交由上层重试后再提交。
    UNSUBMITTED = 4
    # 平台明确**拒绝重答**（返回「提交失败，重做次数过多」之类）：作业的重做次数已用尽，
    # 再提交一万次也是同一个结果。上层必须据此「放弃本章」，绝不能再回退重答/轮询解锁。
    REFUSED = 5

    def is_success(self):
        return self == StudyResult.SUCCESS
    def is_failure(self):
        return self != StudyResult.SUCCESS

class Chaoxing:
    # 章节任务卡片序号：服务端按 num=0..N 分页给任务卡片（一个章节可能有多个卡片，
    # 老师在小节里「加一张卡片」就会 +1）。绝大多数章节只有 1 张，逐个探测到上限为止。
    CARD_NUMS_ALL = "0123456"
    # 只探测第一张卡片：判断「章节是否开放」不需要其余卡片 —— 未开放时第 0 张就会
    # 返回「章节未开放」，已开放时第 0 张返回正常卡片数据。
    CARD_NUMS_FIRST = "0"
    # 整章读取时，连续多少张「没有任何任务点」的卡片就认为后面没有了。
    # 给 1 张以上的容忍度是因为多卡片章节里可能掺有纯文本卡 / 已全部通过的卡片，
    # 宁可多请求一张，也不能像 stop_early 那样一遇空卡就停而漏掉后面的任务点。
    CARD_MAX_EMPTY = 2
    def __init__(self, account: Account = None, tiku: Tiku = None, **kwargs):
        self.account = account
        self.cipher = AESCipher()
        self.tiku = tiku
        self.kwargs = kwargs
        self.rollback_times = 0
        self.rate_limiter = RateLimiter(0.5) # 其他接口速率限制比较松
        self.video_log_limiter = RateLimiter(2) # 上报进度极其容易卡验证码，限制2s一次
        # 视频状态日志：每 N 秒在日志输出一条「章节号 + 进度百分比 + 预计剩余」，0 = 关闭。
        # 由引擎按配置（config_win.ini [common] video_log_interval）写入。
        self.video_log_interval: float = 0
        # 视频任务点的「观看进度要求」（90~100，默认 90）：播到该比例后开始每 5 秒
        # 复查一次；平台未判通过就继续看，直到通过或看完。100 = 旧行为（从结尾开始复查）。
        # 由引擎按配置（config_win.ini [common] video_finish_percent）写入。
        self.video_finish_percent: float = float(DEFAULT_VIDEO_FINISH_PERCENT)
        # 当前章节上下文（引擎在进入章节前写入）：章节序号/总数/名称，供视频日志显示。
        # **按线程隔离**：章节并发时每章的「章节 x/y」写在各自 worker 线程里，
        # 不会互相覆盖；读不到本线程上下文时才回落到下面这份共享默认值。
        # 对外仍然是 ``self.video_context``（见下面的 property），调用方无需改动。
        self._video_context_default: dict = {}
        self._video_context_local = threading.local()
        # 最近一次**真正提交**的作答明细（引擎回读发现平台要求重新作答时，用它作废这批答案）
        self.last_submitted: list[dict] = []
        # 这批答案的指纹（标题+答案）：两轮完全一样时说明重答不会带来任何新信息，
        # 引擎据此停止「无限重答」（见 engine._redo_prev_until_open）
        self.last_submitted_digest: str = ""
        # 这批答案的**逐题判分**（{qid: True/False/{ok, answer}}，已对齐到提交明细的 qid）：
        # 作废/隔离答案时据此保留「平台已判对」的题，避免下次重做把做对的题再搜一遍。
        self.last_verdicts: dict = {}
        # 平台**拒绝重答**的原因（非空 = 本次作业的重做次数已用尽）。
        # 引擎读到它必须停止「回退重答 / 轮询下一章解锁」—— 那条路只会死循环。
        self.work_refused_reason: str = ""
        # 最近一次作答的「答案来源」统计：{"total": 题数, "found": 题库/AI/缓存命中的题数}。
        # found=0 意味着整份作业全靠随机兜底 —— 再重答也不会变出正确答案，
        # 引擎据此停止「无效重答」，避免把平台的重做次数耗光。
        self.last_answer_stats: dict = {}
        # 最近一次判分统计：{"right": 判对题数, "wrong": 判错题数, "fixed": 拿到正确答案的题数}。
        # 引擎用它判断「这一轮重答到底有没有进展」，没有进展就不再重答。
        self.last_verdict_stats: dict = {}

        # 章节检测的「答案来源 / 判分 / 拒绝原因」三类统计原本直接写在实例上。
        # 任务点并发 > 1 时，同一章节若含多个 workid 任务点，它们会在不同 worker
        # 线程里并发写这些实例字段，后写覆盖先写 -> 引擎读到错误结论（误判 REFUSED /
        # 误判 found==0 而提前 UNVERIFIED）。安卓版 jobs=5 能稳定跑，靠的就是并发安全。
        # 这里把它们改成**按 jobid 隔离**写入（见 set_work_stat），引擎在批次结束后
        # 再 merge_work_stats 合并回上面的实例字段（合并发生在 worker 线程 join 之后、
        # 章节线程上，无竞争）。字典本身用一把锁保护，防止多 worker 同时写入。
        self._work_stats_lock = threading.Lock()
        self._work_stats_by_job: dict = {}  # jobid -> {"answer": {...}, "verdict": {...}, "refused": str}

    # ---- 视频日志的章节上下文（线程隔离，章节并发时不串台） ----
    @property
    def video_context(self) -> dict:
        """当前线程的章节上下文；本线程没写过时回落到共享默认值。"""
        ctx = getattr(self._video_context_local, "ctx", None)
        return ctx if ctx is not None else self._video_context_default

    @video_context.setter
    def video_context(self, value) -> None:
        """只在**当前线程**写入上下文（并发章节各写各的）。"""
        self._video_context_local.ctx = dict(value or {})

    # ---- 章节检测统计：按 jobid 隔离写入，避免任务点并发时互相覆盖 ----
    def set_work_stat(self, jobid, *, answer: "dict | None" = None,
                      verdict: "dict | None" = None,
                      refused: "str | None" = None) -> None:
        """把某个 workid 任务点的统计写到「按 jobid 隔离」的存储里。

        在 worker 线程里并发调用也安全：每路写自己的 jobid，不互相覆盖。
        引擎在批次结束后用 :meth:`merge_work_stats` 把本章各 workid 合并回实例字段。
        """
        if jobid is None:
            return
        with self._work_stats_lock:
            d = self._work_stats_by_job.setdefault(str(jobid), {})
            if answer is not None:
                d["answer"] = dict(answer)
            if verdict is not None:
                d["verdict"] = dict(verdict)
            if refused is not None:
                d["refused"] = refused

    def clear_work_stats(self, jobids: "list | None" = None) -> None:
        """清空「按 jobid 隔离」的统计。

        ``jobids`` 给定时只清这些 workid（并发章节各自清各自的任务点，互不干扰）；
        ``None`` 时清全部（向后兼容）。
        """
        with self._work_stats_lock:
            if jobids is None:
                self._work_stats_by_job.clear()
            else:
                for j in jobids:
                    self._work_stats_by_job.pop(str(j), None)

    def merge_work_stats(self, jobids: list) -> None:
        """把本章各 workid 的统计合并回实例级字段，供引擎在批次结束后读取。

        合并发生在 worker 线程 join 之后、章节线程上，不存在并发竞争。
        多个 workid 时：题数/判分求和；任一 workid 被平台拒绝则取它的原因。
        """
        answer_total = answer_found = 0
        right = wrong = fixed = 0
        refused = ""
        with self._work_stats_lock:
            for j in (jobids or []):
                d = self._work_stats_by_job.get(str(j))
                if not d:
                    continue
                a = d.get("answer")
                if isinstance(a, dict):
                    answer_total += int(a.get("total") or 0)
                    answer_found += int(a.get("found") or 0)
                v = d.get("verdict")
                if isinstance(v, dict):
                    right += int(v.get("right") or 0)
                    wrong += int(v.get("wrong") or 0)
                    fixed += int(v.get("fixed") or 0)
                r = d.get("refused")
                if r:
                    refused = r
        self.last_answer_stats = {"total": answer_total, "found": answer_found}
        self.last_verdict_stats = {"right": right, "wrong": wrong, "fixed": fixed}
        self.work_refused_reason = refused

    def login(self, login_with_cookies=False):
        if login_with_cookies:
            logger.info("Logging in with cookies")
            SessionManager.update_cookies()
            logger.debug(f"Logged in with cookies: {SessionManager.get_instance()._session.cookies}")
            if not self._validate_cookie_session():
                logger.warning("Cookie 登录校验失败，尝试使用账号密码重新登录")
                if self.account and self.account.username and self.account.password:
                    return self.login(login_with_cookies=False)
                return {"status": False, "msg": "cookies 已失效，请更新 cookies 或提供账号密码"}
            logger.info("登录成功...")
            return {"status": True, "msg": "登录成功"}

        _session = requests.Session()
        _url = "https://passport2.chaoxing.com/fanyalogin"
        _data = {
            "fid": "-1",
            "uname": self.cipher.encrypt(self.account.username),
            "password": self.cipher.encrypt(self.account.password),
            "refer": "https%3A%2F%2Fi.chaoxing.com",
            "t": True,
            "forbidotherlogin": 0,
            "validate": "",
            "doubleFactorLogin": 0,
            "independentId": 0,
        }
        logger.trace("正在尝试登录...")
        resp = _session.post(_url, headers=gc.HEADERS, data=_data)
        if resp and resp.json()["status"] == True:
            save_cookies(_session)
            SessionManager.update_cookies()
            logger.info("登录成功...")
            return {"status": True, "msg": "登录成功"}
        else:
            return {"status": False, "msg": str(resp.json()["msg2"])}

    def _validate_cookie_session(self) -> bool:
        session = SessionManager.get_instance()._session
        if not session.cookies.get("_uid"):
            return False

        test_session = requests.Session()
        test_session.headers.update(gc.HEADERS)
        test_session.cookies.update(session.cookies.get_dict())

        try:
            resp = test_session.post(
                "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata",
                data={"courseType": 1, "courseFolderId": 0, "query": "", "superstarClass": 0},
                timeout=8,
            )
        except RequestException as exc:
            logger.debug("Cookie validation request failed: {}", exc)
            return False

        if resp.status_code != 200:
            return False

        if "passport2.chaoxing.com" in resp.text or "login" in resp.text.lower():
            return False

        return True

    def get_fid(self):
        _session = SessionManager.get_session()
        return _session.cookies.get("fid")

    def get_uid(self):
        s = SessionManager.get_session()
        if "_uid" in s.cookies:
            return s.cookies["_uid"]
        if "UID" in s.cookies:
            return s.cookies["UID"]
        raise ValueError("Cannot get uid !")

    def get_course_list(self):
        _session = SessionManager.get_session()
        _url = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata"
        _data = {"courseType": 1, "courseFolderId": 0, "query": "", "superstarClass": 0}
        logger.trace("正在读取所有的课程列表...")

        # 接口突然抽风, 增加headers
        # 有可能只是referer的问题
        _headers = {
            "Referer": "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction?moocDomain=https://mooc1-1.chaoxing.com/mooc-ans",
        }
        _resp = _session.post(_url, headers=_headers, data=_data)
        # logger.trace(f"原始课程列表内容:\n{_resp.text}")
        logger.info("课程列表读取完毕...")
        course_list = decode_course_list(_resp.text)

        _interaction_url = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction"
        _interaction_resp = _session.get(_interaction_url)
        course_folder = decode_course_folder(_interaction_resp.text)
        for folder in course_folder:
            _data = {
                "courseType": 1,
                "courseFolderId": folder["id"],
                "query": "",
                "superstarClass": 0,
            }
            _resp = _session.post(_url, data=_data)
            course_list += decode_course_list(_resp.text)
        return course_list

    def get_course_point(self, _courseid, _clazzid, _cpi):
        _session = SessionManager.get_session()
        _url = f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?courseid={_courseid}&clazzid={_clazzid}&cpi={_cpi}&ut=s"
        logger.trace("开始读取课程所有章节...")
        _resp = _session.get(_url)
        # logger.trace(f"原始章节列表内容:\n{_resp.text}")
        logger.info("课程章节读取成功...")
        return decode_course_point(_resp.text)

    def get_job_list(self, course: dict, point: dict,
                     stop_early: bool = False,
                     nums: Optional[str] = None,
                     stop_when: Optional[Callable[[list, dict], bool]] = None,
                     track_cards: bool = False,
                     mark_empty: bool = True) -> tuple[list[dict], dict]:
        """读取章节任务点。

        三个「少请求」开关（风控考虑：原来每章固定打满 7 张卡片，且一次调用只限流一次）：

        - ``nums``：只请求给定的卡片序号（``None`` = 全部 ``CARD_NUMS_ALL``）。
          比如查「章节是否开放」只需要 ``CARD_NUMS_FIRST``，回读章节检测状态只需要
          上次读到 workid 的那几张卡片。``nums`` 不等于全集时视为**部分读取**：
          拿不到任务点也不会被当成「空章节」（不打空页、不写 ``empty``）——
          因为我们根本没看全，断言「本章没有任务点」是错的。
        - ``stop_when(job_slice, card_info)``：命中即返回，不再请求后续卡片。
        - ``track_cards``：在 ``job_info["card_of_job"]`` 里给出
          ``{jobid: 卡片序号}``，供调用方下回只回读相关卡片。

        ``stop_early``：一遇到空卡片就停（采集题库时用）。整章刷课不用它，改由
        ``CARD_MAX_EMPTY`` 容忍若干张空卡后再收手，避免漏掉多卡片章节的任务点。

        ``mark_empty``：空章节是否上报``studentstudyAjax``把它标记成已完成。
        **采集题库必须传 False**：那是写操作（会改服务端进度），且每章刷一条
        「空页面任务完成」日志，把采集真正有用的信息挤出界面日志窗口。

        失败态（读不到任务点）在 ``job_info`` 里用 ``unreadable=True`` 标记，
        **不再返回空列表冒充成功**：非 200、掉登录/验证码/403 页、mArg 解析失败
        都会命中。真正「本章没有任务点」会走 study_emptypage 并带 ``empty=True``。
        章节未开放仍为 ``notOpen=True``（语义不变）。
        """
        _session = SessionManager.get_session()
        job_list: list[dict] = []
        job_info: dict = {}
        card_of_job: dict[str, str] = {}
        readable = False     # 是否至少有一张卡片被成功解析
        empty_run = 0        # 连续空卡片计数

        card_nums = self.CARD_NUMS_ALL if nums is None else str(nums)
        if not card_nums:
            card_nums = self.CARD_NUMS_ALL
        # 只看了一部分卡片：不能据此断言「本章没有任务点」
        partial = card_nums != self.CARD_NUMS_ALL

        self.rate_limiter.limit_rate()
        cards_params = {
            "clazzid": course["clazzId"],
            "courseid": course["courseId"],
            "knowledgeid": point["id"],
            "ut": "s",
            "cpi": course["cpi"],
            "v": "2025-0424-1038-3",
            "mooc2": 1
        }

        # 学习界面任务卡片数, 很少有3个的, 但是对于章节解锁任务点少一个都不行, 可以从API /mooc-ans/mycourse/studentstudyAjax获取值, 或者干脆直接加, 但二者都会造成额外的请求
        for _card_index, _possible_num in enumerate(card_nums):
            if _card_index:
                # 逐张限流（同一个 rate_limiter、同样的 0.5s 语义）：过去一次调用只在
                # 开头限流一次，7 张卡片等于一口气连发 7 个请求，很容易撞风控。
                self.rate_limiter.limit_rate()

            logger.trace("开始读取章节所有任务点...")

            cards_params.update({"num": _possible_num})
            _resp = _session.get("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards", params=cards_params)
            if _resp.status_code != 200:
                logger.error(f"未知错误: {_resp.status_code} 正在跳过")
                logger.error(_resp.text)
                return [], {"unreadable": True}

            _job_list, _job_info = decode_course_card(_resp.text)
            if _job_info.get("notOpen", False):
                # 直接返回, 节省一次请求
                logger.info("该章节未开放")
                return [], _job_info

            if _job_info.get("unreadable", False):
                # 卡片页不可读（掉登录 / 验证码 / 403 / 解析失败），没有任务点可言
                logger.warning(f"章节任务点卡片 {_possible_num} 读取失败（可能是掉登录或风控页）")
                empty_run = 0
                if stop_early and _possible_num != card_nums[0]:
                    break
                continue

            readable = True
            if track_cards:
                for _job in _job_list:
                    _jobid = str(_job.get("jobid") or _job.get("id") or "")
                    if _jobid:
                        card_of_job.setdefault(_jobid, _possible_num)

            job_list += _job_list
            job_info.update(_job_info)

            if stop_when is not None and stop_when(_job_list, dict(_job_info)):
                # 命中即收手：后续卡片不用再请求了
                logger.trace(f"任务点读取在第 {_possible_num} 张卡片命中，停止后续请求")
                break

            if not _job_list:
                empty_run += 1
                # 卡片序号从 0 连续排列：第 1 张之后还空就说明没有更多卡片了
                if stop_early and _possible_num != card_nums[0]:
                    break
                if empty_run >= self.CARD_MAX_EMPTY:
                    logger.trace(f"连续 {empty_run} 张卡片没有任务点，判定后面没有卡片了")
                    break
            else:
                empty_run = 0

        if track_cards:
            job_info["card_of_job"] = card_of_job

        if not job_list:
            if not readable:
                # 一张卡片都没解析成功：绝不能当成「本章无任务点」，否则上层会把
                # 整章记为已完成（一次网络抖动就永久跳过该章）。
                logger.error("章节任务点读取失败：未能解析出任何卡片数据")
                return [], {"unreadable": True}
            if partial:
                # 只看了部分卡片就没任务点：什么都不做，交回上层按「没读到」自决。
                # 这里绝不能走 study_emptypage —— 那等于凭半份数据宣称本章无任务点。
                logger.debug(f"部分卡片读取(nums={card_nums})未取到任务点，跳过空章节判定")
                return job_list, job_info
            # 确实是空章节（卡片全解析成功但没有任务点），按空页处理
            if mark_empty:
                self.study_emptypage(course, point)
            else:
                # 只读调用方（采集题库）：只标记「空」，不写服务端进度
                logger.debug(f"只读模式：跳过空页面任务上报 -> {point.get('title')}")
            job_info["empty"] = True
        # logger.trace(f"原始任务点列表内容:\n{_resp.text}")
        logger.info("章节任务点读取成功...")

        return job_list, job_info

    def get_enc(self, clazzId, jobid, objectId, playingTime, duration, userid):
        return md5(
            f"[{clazzId}][{userid}][{jobid}][{objectId}][{playingTime * 1000}][d_yHJ!$pdA~5][{duration * 1000}][0_{duration}]".encode()
        ).hexdigest()

    def video_progress_log(
            self,
            _session,
            _course,
            _job,
            _job_info,
            _dtoken,
            _duration,
            _playingTime,
            _type: str = "Video",
            headers: Optional[dict] = None,
    ) -> tuple[bool, int]:

        if headers is None:
            logger.warning("null headers")
            headers = gc.VIDEO_HEADERS

        self.video_log_limiter.limit_rate(random_time=True, random_max=2)

        if "courseId" in _job["otherinfo"]:
            logger.error(_job["otherinfo"])
            raise RuntimeError("this is not possible")

        enc = self.get_enc(_course["clazzId"], _job["jobid"], _job["objectid"], _playingTime, _duration, self.get_uid())
        params = {
            "clazzId": _course["clazzId"],
            "playingTime": _playingTime,
            "duration": _duration,
            "clipTime": f"0_{_duration}",
            "objectId": _job["objectid"],
            "otherInfo": _job["otherinfo"],
            "courseId": _course["courseId"],
            "jobid": _job["jobid"],
            "userid": self.get_uid(),
            "isdrag": "3",
            "view": "pc",
            "enc": enc,
            "dtype": _type
        }

        _url = (
            f"https://mooc1.chaoxing.com/mooc-ans/multimedia/log/a/"
            f"{_course['cpi']}/"
            f"{_dtoken}"
        )


        face_capture_enc = _job["videoFaceCaptureEnc"]
        att_duration = _job["attDuration"]
        att_duration_enc = _job["attDurationEnc"]

        if face_capture_enc:
            params["videoFaceCaptureEnc"] = face_capture_enc
        if att_duration:
            params["attDuration"] = att_duration
        if att_duration_enc:
            params["attDurationEnc"] = att_duration_enc

        rt = _job['rt']
        if not rt:
            rt_search = re.search(r"-rt_([1d])", _job['otherinfo'])
            if rt_search:
                rt_char = rt_search.group(1)
                rt = "0.9" if rt_char == "d" else "1"
                logger.trace(f"Got rt from otherinfo: {rt}")

        if rt:
            logger.trace(f"Got rt: {rt}")
            params.update({"rt": rt,
                           "_t": get_timestamp()})
            resp = _session.get(_url, params=params, headers=headers)
        else:
            logger.warning("Failed to get rt")
            for rt in [0.9, 1]:
                params.update({"rt": rt,
                               "_t": get_timestamp()})
                resp = _session.get(_url, params=params, headers=headers)
                if resp.status_code == 200:
                    logger.trace(resp.text)
                    return resp.json()["isPassed"], 200
                #elif resp.ok:
                #    # TODO: 处理验证码
                #    pass
                elif resp.status_code == 403:
                    logger.warning("出现403报错, 正常尝试切换rt")

                else:
                    logger.warning("未知错误 jobid={}, status_code={}, 摘要:\n{}",
                                   _job.get("jobid"),
                                   resp.status_code,
                                   resp.text[:200]
                    )
                    break

        if resp.status_code == 200:
            logger.trace(resp.text)
            return resp.json()["isPassed"], 200

        elif resp.status_code == 403:
            logger.debug(
                "视频进度上报返回403, jobid={}, 摘要={}",
                _job.get("jobid"),
                resp.text[:200],
            )

            # 若出现两个rt参数都返回403的情况, 则跳过当前任务
            logger.error("出现403报错, 尝试修复无效, 正在跳过当前任务点...")
            logger.error("请求url: {}", resp.url)
            logger.error("请求头: {}", dict(_session.headers) | headers)
            return False, 403

        logger.error(f"未知错误: {resp.status_code}")
        logger.error("请求url:", resp.url)
        logger.error("请求头：", dict(_session.headers) | headers)
        return False, resp.status_code


    def _refresh_video_status(self, session: requests.Session, job: dict, _type: Literal["Video", "Audio"]) -> Optional[dict]:
        self.rate_limiter.limit_rate(random_time=True, random_max=0.2)
        headers = gc.VIDEO_HEADERS if _type == "Video" else gc.AUDIO_HEADERS
        info_url = (
            f"https://mooc1.chaoxing.com/ananas/status/{job['objectid']}?"
            f"k={self.get_fid()}&flag=normal"
        )
        try:
            resp = session.get(info_url, timeout=8, headers=headers)
        except RequestException as exc:
            logger.debug("刷新视频状态失败: {}", exc)
            return None

        if resp.status_code != 200:
            logger.debug("刷新视频状态返回码异常: {}"% resp.status_code)
            logger.debug(resp.text)
            return None

        try:
            data = resp.json()
        except ValueError as exc:
            logger.debug("解析视频状态响应失败: {}", exc)
            return None

        if data.get("status") == "success":
            return data

        return None

    def _recover_after_forbidden(self, session: requests.Session, job: dict, _type: Literal["Video", "Audio"]):
        SessionManager.update_cookies()
        refreshed = self._refresh_video_status(session, job, _type)
        if refreshed:
            return refreshed

        # FIXME: Temporarily disabled for multithreading support
        if False and self.account and self.account.username and self.account.password:
            login_result = self.login(login_with_cookies=False)
            if login_result.get("status"):
                SessionManager.update_cookies()
                return self._refresh_video_status(session, job, _type)
            logger.warning("账号密码登录失败: {}", login_result.get("msg"))

        return None


    def _video_status_line(self, _job: dict, play_time: float, duration: int,
                           _speed: float) -> str:
        """构造一行视频状态日志：进度/剩余 → 章节号 → 任务名。

        计时信息（百分比、已播/总时长、预计剩余）放在最前面：日志面板里这一行
        往往比窗口还宽，放在末尾时会被右边界裁掉，看起来像「计时不走」。
        """
        total = max(int(duration or 0), 1)
        percent = min(100.0, max(0.0, float(play_time)) * 100.0 / total)
        speed = float(_speed or 1.0) or 1.0
        parts = [
            f"{percent:.1f}% ({int(play_time)}/{total}s)",
            f"剩余约{_fmt_duration((total - play_time) / speed)}（{speed:g}x）",
        ]

        ctx = getattr(self, "video_context", None) or {}
        index, count = ctx.get("chapter_index"), ctx.get("chapter_total")
        if index and count:
            head = f"章节 {index}/{count}"
            title = _clip_text(ctx.get("chapter_title"), 16)
            if title:
                head += f"《{title}》"
            parts.append(head)

        name = _clip_text(_job.get("name") or _job.get("title"), 18)
        if name:
            parts.append(f"任务 {name}")
        return "视频进度 | " + " | ".join(parts)

    def _log_video_status(self, _job: dict, play_time: float, duration: int,
                          _speed: float) -> None:
        """输出一条视频状态日志（章节号/进度百分比等）；异常不得打断刷课。"""
        try:
            logger.info(self._video_status_line(_job, play_time, duration, _speed))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"视频状态日志输出失败: {exc}")

    def study_video(self, _course, _job, _job_info, _speed: float = 1.0, _type: Literal["Video", "Audio"] = "Video") -> StudyResult:
        """视频任务点。

        元数据请求走 :func:`_request_json`：状态码异常/网络异常/脏 JSON 最多重试 3 次，
        仍失败抛 :class:`StudyRequestError`，由 ``engine.process_job`` 收敛成
        ``StudyResult.ERROR``（只丢这一个任务点，不中断整章/整轮）。
        """
        _session = SessionManager.get_session()

        headers = gc.VIDEO_HEADERS if _type == "Video" else gc.AUDIO_HEADERS
        _info_url = f"https://mooc1.chaoxing.com/ananas/status/{_job['objectid']}?k={self.get_fid()}&flag=normal"
        # 状态码检查 + 有限重试都在这里；耗尽后抛 StudyRequestError（不在这里吞掉，
        # 交给上层统一按 ERROR 处理，保证「单点失败 != 整轮中止」）
        _video_info = _request_json(_session, "get", _info_url, headers=headers,
                                    what=f"读取视频信息(objectId={_job.get('objectid')})")

        if _video_info.get("status") != "success":
            logger.error(f"Unknown status: {_video_info.get('status')}")
            return StudyResult.ERROR

        _dtoken = _video_info["dtoken"]

        _crc = _video_info["crc"]
        _key = _video_info["key"]

        # Time in the real world: last_iter, gc.THRESHOLD
        # Time in the video (can be scaled with the speed factor): duration, play_time, last_log_time, wait_time

        duration = int(_video_info["duration"])
        # playTime 单位不保证是秒（部分卡片给毫秒），统一按 duration 推断并归一，
        # 否则会出现「已进行 75000s / 总时长 1252s」这种 100% 假进度，进而瞬间跳过任务点。
        raw_play_time = _job.get("playTime")
        play_time = _normalize_play_time(raw_play_time, duration)
        try:
            raw_int = int(float(raw_play_time or 0))
        except (TypeError, ValueError):
            raw_int = 0
        if play_time and play_time != raw_int:
            logger.info("续播进度单位换算: {} -> {}s（总时长 {}s）", raw_play_time, play_time, duration)
        last_log_time = 0
        last_iter = time.time()
        wait_time = int(random.uniform(30, 90))

        logger.info(f"开始任务: {_job['name']}, 总时长: {duration}s, 已进行: {play_time}s")

        # 观看进度要求：播到 finish_percent（默认 90%）后进入「复查」阶段 ——
        # 平台若此时仍未判通过，**绝不中断观看**：继续再看 VIDEO_FINISH_RECHECK_SECONDS 秒
        # 再上报一次，如此循环，直到平台返回 isPassed（任务完成）或整段看完。
        # 100% 时目标点就是结尾，效果等同旧行为（结尾反复上报直到通过）。
        finish_percent = normalize_finish_percent(
            getattr(self, "video_finish_percent", DEFAULT_VIDEO_FINISH_PERCENT))
        target_time = _video_finish_target(duration, finish_percent)
        finish_full = target_time >= duration
        if finish_full:
            logger.debug("观看进度要求：{}%（需完整看完）", f"{finish_percent:g}")
        else:
            logger.info(
                "观看进度要求: {:g}%，即看到 {}s（总时长 {}s）后开始复查；"
                "平台未判通过就继续观看并每 {:g} 秒复查一次",
                finish_percent, target_time, duration, VIDEO_FINISH_RECHECK_SECONDS,
            )

        pbar = tqdm(total=duration, initial=play_time, desc=_job["name"],
                    unit_scale=True, bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt}',
                    file=_progress_stream())

        forbidden_retry = 0
        max_forbidden_retry = 2

        # 视频状态日志（每 N 秒一条：章节号 + 进度百分比 + 预计剩余），0 表示关闭。
        # 用真实时间计时而不是 play_time，倍速下「每 30 秒」才符合用户直觉。
        try:
            status_interval = max(0.0, float(getattr(self, "video_log_interval", 0) or 0))
        except (TypeError, ValueError):
            status_interval = 0.0
        last_status = time.time()
        last_status_play = play_time     # 上次输出的播放位置：进度没前进就不重复刷日志

        try:
            cancel.check()
            # 只上报一次：过去这里连着发两条（playTime 续播点 + duration），第一条的
            # (passed, state) 立刻被第二条覆盖，唯一的差别只是多了一次网络请求
            # （服务端多记一次续播位置、本地多等一次 video_log_limiter 随机退避）。
            # 生效的判定始终来自这条 duration 上报，删掉被覆盖的那条不改变任何后续逻辑。
            passed, state = self.video_progress_log(_session, _course, _job, _job_info, _dtoken, duration, duration, _type, headers=headers)

            if passed:
                logger.info("任务瞬间完成: {}", _job['name'])
                return StudyResult.SUCCESS

            if status_interval > 0:      # 开播先报一条，立刻能看到章节归属与续播进度
                self._log_video_status(_job, play_time, duration, _speed)

            # 达标复查状态：达标点只提示一次；达标后每 5 秒复查一次，直到通过或看完
            target_reported = False
            end_reported = False
            next_recheck = 0.0

            while not passed:
                # 停止按钮必须能中断视频循环：一个视频动辄几十分钟，
                # 只在章节边界检查会让界面长时间卡在「停止中」。
                cancel.check()
                # 上报时机三选一：常规周期（wait_time）｜达标点必报一次｜达标后每 5 秒复查。
                # Sometimes the last request needs to be sent several times to complete the task
                due = play_time - last_log_time >= wait_time
                if not due and not target_reported and play_time >= target_time:
                    due = True
                if not due and next_recheck and time.time() >= next_recheck:
                    due = True
                if due:

                    passed, state = self.video_progress_log(_session, _course, _job, _job_info, _dtoken, duration,
                                                            int(play_time), _type, headers=headers)

                    if state == 403:
                        if forbidden_retry >= max_forbidden_retry:
                            logger.warning("403重试失败, 跳过当前任务")
                            return StudyResult.FORBIDDEN
                        forbidden_retry += 1
                        logger.warning(
                            "出现403报错, 正在尝试刷新会话状态 (第{}次)",
                            forbidden_retry,
                        )
                        time.sleep(random.uniform(2, 4))
                        refreshed_meta = self._recover_after_forbidden(_session, _job, _type)
                        if refreshed_meta:
                            # FIXME: Maybe it should be considered an error if those keys aren't present in the refreshed meta, so we perhaps shouldn't use get()
                            _dtoken = refreshed_meta.get("dtoken", _dtoken)
                            _duration = int(refreshed_meta.get("duration", duration) or duration)
                            play_time = _normalize_play_time(
                                refreshed_meta.get("playTime", play_time), _duration
                            )

                            logger.debug("Refreshed token: {}, duration: {}, play time: {}", _dtoken, _duration, play_time)
                            continue

                    elif not passed and state != 200:
                        return StudyResult.ERROR

                    # 已看完仍不通过：提示一次，之后继续按复查间隔上报
                    # （平台偶尔要在结尾多收几次进度才认，不能在这里中断观看）
                    if not passed and play_time >= duration and not end_reported:
                        end_reported = True
                        logger.warning(
                            "已完整看完仍未判定通过，继续每 {:g} 秒复查一次: {}",
                            VIDEO_FINISH_RECHECK_SECONDS, _job["name"],
                        )

                    # 到达设定比例（默认 90%）：不中断观看，改成 5 秒一次的复查节奏。
                    # 平台若接受（isPassed=true）循环自然结束；不接受就继续看，直到看完。
                    if play_time >= target_time:
                        first_hit = not target_reported
                        target_reported = True
                        if not passed:
                            next_recheck = time.time() + max(0.0, VIDEO_FINISH_RECHECK_SECONDS)
                            if first_hit and play_time < duration:
                                logger.info(
                                    "观看比例已达 {:g}%（{}/{}s），平台尚未判定完成，"
                                    "继续观看并每 {:g} 秒复查一次: {}",
                                    finish_percent, int(play_time), duration,
                                    VIDEO_FINISH_RECHECK_SECONDS, _job["name"],
                                )

                    wait_time = int(random.uniform(30, 90))
                    last_log_time = play_time

                dt = (time.time() - last_iter) * _speed # Since uploading the progress takes time, we assume that the video is still playing in the background, so manually calculate the time elapsed is required
                last_iter = time.time()
                play_time = min(duration, play_time+dt)

                pbar.n = int(play_time)
                pbar.refresh()
                if status_interval > 0 and (time.time() - last_status) >= status_interval \
                        and int(play_time) > int(last_status_play):
                    last_status = time.time()
                    last_status_play = play_time
                    self._log_video_status(_job, play_time, duration, _speed)
                time.sleep(gc.THRESHOLD)

            logger.info("任务完成: {}", _job['name'])
            return StudyResult.SUCCESS
        except cancel.Cancelled:
            logger.info(f"收到停止信号，中断任务: {_job['name']}（已播 {int(play_time)}/{duration}s）")
            raise
        finally:
            try:
                pbar.close()
            except Exception:  # noqa: BLE001  进度条关闭失败不影响主流程
                pass

    def study_document(self, _course, _job) -> StudyResult:
        """
        Study a document in Chaoxing platform.

        This method makes a GET request to fetch document information for a given course and job.

        Args:
            _course (dict): Dictionary containing course information with keys:
                - courseId: ID of the course
                - clazzId: ID of the class
            _job (dict): Dictionary containing job information with keys:
                - jobid: ID of the job
                - otherinfo: String containing node information
                - jtoken: Authentication token for the job

        Returns:
            requests.Response: Response object from the GET request

        Note:
            This method requires the following helper functions:
            - init_session(): To initialize a new session
            - get_timestamp(): To get current timestamp
            - re module for regular expression matching
        """
        _session = SessionManager.get_session()
        _url = f"https://mooc1.chaoxing.com/ananas/job/document?jobid={_job['jobid']}&knowledgeid={re.findall(r'nodeId_(.*?)-', _job['otherinfo'])[0]}&courseid={_course['courseId']}&clazzid={_course['clazzId']}&jtoken={_job['jtoken']}&_dc={get_timestamp()}"
        _resp = _session.get(_url)
        if _resp.status_code != 200:
            return StudyResult.ERROR
        else:
            return StudyResult.SUCCESS

    def study_work(self, _course, _job, _job_info) -> StudyResult:
        # FIXME: 这一块可以单独搞一个类出来了，方法里面又套方法，每一次调用都会创建新的方法，十分浪费
        # 题库停用（provider 留空 / 未配置）= 不答作业；本地题库只做加速层，不单独启用，
        # 避免「没收录的题随机作答并提交」把作业做烂。
        if self.tiku.DISABLE or not self.tiku:
            return StudyResult.SUCCESS
        # 本地题库按课程存放（同一门课的题目和答案对所有账号都一样）：
        # 查询时优先查这门课的文件，入库也写进这门课的文件。
        # 课程信息**逐次显式传参**（不再写进程级 set_scope）：多线程刷课时
        # 同章节多个作业并发，全局作用域会被互相覆盖，答案会写错课程文件。
        _course_title = str(_course.get("title") or "") if isinstance(_course, dict) else ""
        _course_id = str(_course.get("courseId") or "") if isinstance(_course, dict) else ""
        _ORIGIN_HTML_CONTENT = ""  # 用于配合输出网页源码, 帮助修复#391错误

        def multi_select_count(available_options: int) -> int:
            """多选题随机选几个（沿用原加权策略：2 个最常见，最多 4 个）。"""
            if available_options <= 1:
                return max(0, available_options)
            max_possible = min(4, available_options)
            min_possible = min(2, available_options)
            weights_map = {
                2: [1.0],
                3: [0.3, 0.7],
                4: [0.1, 0.5, 0.4],
                5: [0.1, 0.4, 0.3, 0.2],
            }
            weights = weights_map.get(max_possible, [0.3, 0.4, 0.3])
            possible_counts = list(range(min_possible, max_possible + 1))
            weights = weights[:len(possible_counts)]
            weights_sum = sum(weights)
            if weights_sum > 0:
                weights = [w / weights_sum for w in weights]
            return random.choices(possible_counts, weights=weights, k=1)[0]

        def random_answer(options: str) -> str:
            answer = ""
            if not options:
                return answer

            # 随机也要排除「平台判错过的选项」：否则判断题这种 2 选 1 的题有一半概率
            # 把同一个错答案再交一遍，重答等于原地踏步（多选题按同一档选项数换组合）。
            known = _wrong_keys(q)
            if known:
                size = None
                if q["type"] == "multiple":
                    size = multi_select_count(len(local_bank.option_lines(options))) or None
                try:
                    picked = wrong_answers.pick_submit_answer(q["type"], options, known,
                                                              "", size=size)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"排除法挑答案异常：{type(exc).__name__}: {exc}")
                    picked = None
                if picked:
                    logger.info(f"随机选择（已避开平台判错的答案）-> {picked}")
                    return picked
                if str(q.get("type") or "").strip().lower() in ("single", "multiple",
                                                                "judgement"):
                    logger.warning("随机作答：这道题的所有答案组合都已被平台判错，"
                                   "只能照旧随机")
                else:
                    logger.debug("随机作答：该题型没有排除法，按普通随机处理")

            if q["type"] == "multiple":
                logger.debug(f"当前选项列表[cut前] -> {options}")
                _op_list = multi_cut(options)
                logger.debug(f"当前选项列表[cut后] -> {_op_list}")

                if not _op_list:
                    logger.error(
                        "选项为空, 未能正确提取题目选项信息! 请反馈并提供以上信息"
                    )
                    return answer

                select_count = multi_select_count(len(_op_list))
                selected_options = random.sample(_op_list, select_count) if select_count > 0 else []

                for option in selected_options:
                    answer += option[:1]  # 取首字为答案，例如A或B

                answer = "".join(sorted(answer))
            elif q["type"] == "single":
                lines = [ln for ln in options.split("\n") if ln.strip()]
                if known:
                    lines = [ln for ln in lines
                             if local_bank.answer_key(
                                 "single", local_bank.option_letter(ln) or ln, options
                             ) not in known]
                if lines:
                    answer = random.choice(lines)[:1]  # 取首字为答案, 例如A或B
            # 判断题处理
            elif q["type"] == "judgement":
                # answer = self.tiku.jugement_select(_answer)
                answer = "true" if random.choice([True, False]) else "false"
            logger.info(f"随机选择 -> {answer}")
            return answer

        def multi_cut(answer: str):
            """
            将多选题答案字符串按特定字符进行切割, 并返回切割后的答案列表

            参数:
            answer(str): 多选题答案字符串.

            返回:
            list[str]: 切割后的答案列表,如果无法切割, 则返回默认的选项列表None

            注意:
            如果无法从网页中提取题目信息,将记录警告日志并返回None
            """
            # cut_char = [',','，','|','\n','\r','\t','#','*','-','_','+','@','~','/','\\','.','&',' ']    # 多选答案切割符
            # ',' 在常规被正确划分的, 选项中出现, 导致 multi_cut 无法正确划分选项 #391
            # IndexError: Cannot choose from an empty sequence #391
            # 同时为了避免没有考虑到的 case, 应该先按照 '\n' 匹配, 匹配不到再按照其他字符匹配
            cut_char = [
                "\n",
                ",",
                "，",
                "|",
                "\r",
                "\t",
                "#",
                "*",
                "-",
                "_",
                "+",
                "@",
                "~",
                "/",
                "\\",
                ".",
                "&",
                " ",
                "、",
            ]  # 多选答案切割符
            res = cut(answer)
            if res is None:
                logger.warning(
                    f"未能从网页中提取题目信息, 以下为相关信息：\n\t{answer}\n\n{_ORIGIN_HTML_CONTENT}\n"
                )  # 尝试输出网页内容和选项信息
                logger.warning("未能正确提取题目选项信息! 请反馈并提供以上信息")
                return None
            else:
                return res

        def clean_res(res):
            cleaned_res = []
            if isinstance(res, str):
                res = [res]
            for c in res:
                # 仅在字符串长度大于1时才尝试去除开头的字母编号，防止误删单个字母答案
                cleaned = re.sub(r'^[A-Za-z]|[.,!?;:，。！？；：]', '', c) if len(c) > 1 else c
                cleaned_res.append(cleaned.strip())

            return cleaned_res

        def is_subsequence(a, o):
            iter_o = iter(o)
            return all(c in iter_o for c in a)

        # FIXME: Use tenacity for retrying
        def with_retry(max_retries=3, delay=1):
            def decorator(func):
                def wrapper(*args, **kwargs):
                    retries = 0
                    while retries < max_retries:
                        try:
                            _resp = func(*args, **kwargs)

                            # 未创建完成该测验则不进行答题，目前遇到的情况是未创建完成等同于没题目
                            if '教师未创建完成该测验' in _resp.text:
                                raise PermissionError("教师未创建完成该测验")

                            questions = decode_questions_info(_resp.text)

                            if _resp.status_code == 200 and questions.get("questions"):
                                return (_resp, questions)

                            logger.warning(
                                f"无效响应 (Code: {getattr(_resp, 'status_code', 'Unknown')}), 重试中... ({retries + 1}/{max_retries})")

                        except requests.exceptions.RequestException as e:
                            logger.warning(f"请求失败: {str(e)[:50]}, 重试中... ({retries + 1}/{max_retries})")
                        retries += 1
                        time.sleep(delay * (2 ** retries))
                    raise MaxRetryExceeded(f"超过最大重试次数 ({max_retries})")

                return wrapper

            return decorator

        # 学习通这里根据参数差异能重定向至两个不同接口, 需要定向至https://mooc1.chaoxing.com/mooc-ans/workHandle/handle
        _session = SessionManager.get_session()

        _url = work_verify.WORK_URL
        # 抓题、提交后核验、采集已完成作业共用同一组参数（见 api.work_verify.build_work_params）
        work_params = work_verify.build_work_params(_course, _job, _job_info)

        @with_retry(max_retries=3, delay=1)
        def fetch_response():
            return _session.get(_url, params=work_params)

        final_resp = {}
        questions = {}

        try:
            final_resp, questions = fetch_response()
        except Exception as e:
            logger.error(f"请求失败: {e}")
            return StudyResult.ERROR

        _ORIGIN_HTML_CONTENT = final_resp.text  # 用于配合输出网页源码, 帮助修复#391错误

        # 搜题
        total_questions = len(questions["questions"])
        found_answers = 0
        # 本次的作答明细，提交成功后交给本地题库（判对存本次答案，判错存平台正确答案）
        submitted: list[dict] = []
        # 大模型类题库（AI / DeepSeek / SiliconFlow）：整个任务只在这里做一次可用性检查，
        # 连不通则本题及后续题目直接随机作答，不再逐题请求大模型（见 Tiku.ensure_available）。
        _ensure = getattr(self.tiku, "ensure_available", None)
        if callable(_ensure) and not _ensure():
            logger.error(
                "大模型不可用，本章所有题目将随机作答"
                f"（如需恢复：修正配置后在「设置」里保存一次）: {getattr(self.tiku, 'name', '题库')}"
            )
        for q in questions["questions"]:
            # 停止时不再逐题作答：作业还没提交，中断是安全的（不会留下半份提交）
            cancel.check()
            logger.debug(f"当前题目信息 -> {q}")
            # 添加搜题延迟 #428 - 默认0s延迟
            query_delay = self.kwargs.get("query_delay", 0)
            time.sleep(query_delay)
            res = _tiku_query(self.tiku, q, _course_title, _course_id)
            answer = ""
            if not res:
                # 随机答题
                answer = random_answer(q["options"])
                q[f'answerSource{q["id"]}'] = "random"
            else:
                # 根据响应结果选择答案
                if q["type"] == "multiple":
                    # 多选处理
                    options_list = multi_cut(q["options"])
                    res_list = multi_cut(res)
                    if res_list is not None and options_list is not None:
                        for _a in clean_res(res_list):
                            for o in options_list:
                                if (
                                        is_subsequence(_a, o)  # 去掉各种符号和前面ABCD的答案应当是选项的子序列
                                ):
                                    answer += o[:1]
                                    break # 找到匹配项后立即停止，防止重复添加
                        # 对答案进行排序, 否则会提交失败
                        answer = "".join(sorted(answer))
                    # else 如果分割失败那么就直接到下面去随机选
                elif q["type"] == "single":
                    # 单选也进行切割，主要是防止返回的答案有异常字符
                    options_list = multi_cut(q["options"])
                    if options_list is not None:
                        t_res = clean_res(res)
                        for o in options_list:
                            if is_subsequence(t_res[0], o):
                                answer = o[:1]
                                break
                elif q["type"] == "judgement":
                    answer = "true" if self.tiku.judgement_select(res) else "false"
                elif q["type"] == "completion":
                    if isinstance(res, list):
                        answer = "".join(res)
                    elif isinstance(res, str):
                        answer = res
                else:
                    # 其他类型直接使用答案 （目前仅知有简答题，待补充处理）
                    answer = res

                if not answer:  # 检查 answer 是否为空
                    logger.warning(f"找到答案但答案未能匹配 -> {res}\t随机选择答案")
                    answer = random_answer(q["options"])  # 如果为空，则随机选择答案
                    q[f'answerSource{q["id"]}'] = "random"
                else:
                    logger.info(f"成功获取到答案：{answer}")
                    q[f'answerSource{q["id"]}'] = "cover"
                    found_answers += 1
            # 提交前最后一道闸：不管答案来自题库 / 缓存 / AI / 随机，撞到「平台判过错
            # 的答案」一律换掉。重做错题时这是唯一能保证「不再交同一个错答案」的地方。
            answer = avoid_judged_wrong(q, answer)
            # 填充答案
            q["answerField"][f'answer{q["id"]}'] = answer
            logger.info(f'{q["title"]} 填写答案为 {answer}')
            submitted.append({
                "qid": q["id"],
                "title": q["title"],
                "type": q["type"],
                "options": q["options"],
                "answer": answer,
            })
        cover_rate = (found_answers / total_questions) * 100
        logger.info(f"章节检测题库覆盖率： {cover_rate:.0f}%")
        # 记下本轮答案的来源：found=0 表示整份作业全靠随机兜底，重答不会变出正确答案。
        # 同时写到按 jobid 隔离的存储，任务点并发时不会被其它 workid 覆盖。
        self.last_answer_stats = {"total": total_questions, "found": found_answers}
        self.set_work_stat(_job.get("jobid"), answer={"total": total_questions, "found": found_answers})
        # 提交模式  现在与题库绑定,留空直接提交, 1保存但不提交
        # need_reanswer: 因覆盖率不足而仅保存草稿、未真正提交，平台侧会要求「重新作答」
        need_reanswer = False
        if self.tiku.get_submit_params() == "1":
            # 用户显式配置「仅保存不提交」，按配置执行（视为已完成，不做重试）
            questions["pyFlag"] = "1"
        elif cover_rate >= self.tiku.COVER_RATE * 100:
            questions["pyFlag"] = ""
        elif self.rollback_times >= 1:
            # 已回退重做过一次仍未达到覆盖率，按当前答案提交，避免永远卡在「未提交」
            questions["pyFlag"] = ""
            logger.info(f"题库覆盖率仍低于{self.tiku.COVER_RATE * 100:.0f}%，已重试，按当前答案提交")
        else:
            # 覆盖率不足：仅保存草稿不提交，返回 UNSUBMITTED 让上层重新作答后再提交
            questions["pyFlag"] = "1"
            need_reanswer = True
            logger.warning(
                f"章节检测题库覆盖率低于{self.tiku.COVER_RATE * 100:.0f}%，本次仅保存不提交，将重新作答"
            )
        # 组建提交表单
        if questions["pyFlag"] == "1":
            for q in questions["questions"]:
                questions.update(
                    {
                        f'answer{q["id"]}':
                            q["answerField"][f'answer{q["id"]}'] if q[f'answerSource{q["id"]}'] == "cover" else '',
                        f'answertype{q["id"]}': q["answerField"][f'answertype{q["id"]}'],
                    }
                )
        else:
            for q in questions["questions"]:
                questions.update(
                    {
                        f'answer{q["id"]}': q["answerField"][f'answer{q["id"]}'],
                        f'answertype{q["id"]}': q["answerField"][f'answertype{q["id"]}'],
                    }
                )

        del questions["questions"]

        # 提交/保存请求：状态码检查 + 有限重试（同 study_video 的元数据请求），
        # 失败抛 StudyRequestError，由上层按 ERROR 处理 —— 一次网络抖动不该废掉整轮刷课。
        try:
            res_json = _request_json(
                _session, "post",
                "https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew",
                data=questions,
                headers={
                    "Host": "mooc1.chaoxing.com",
                    "sec-ch-ua-platform": '"Windows"',
                    "X-Requested-With": "XMLHttpRequest",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "sec-ch-ua": '"Microsoft Edge";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "sec-ch-ua-mobile": "?0",
                    "Origin": "https://mooc1.chaoxing.com",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Dest": "empty",
                    # "Referer": "https://mooc1.chaoxing.com/mooc-ans/work/doHomeWorkNew?courseId=246831735&workAnswerId=52680423&workId=37778125&api=1&knowledgeid=913820156&classId=107515845&oldWorkId=07647c38d8de4c648a9277c5bed7075a&jobid=work-07647c38d8de4c648a9277c5bed7075a&type=&isphone=false&submit=false&enc=1d826aab06d44a1198fc983ed3d243b1&cpi=338350298&mooc2=1&skipHeader=true&originJobId=work-07647c38d8de4c648a9277c5bed7075a",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,ja;q=0.5",
                },
                what="章节检测提交",
            )
        except StudyRequestError as exc:
            # 验证码通常是「HTML 页面」而不是 JSON：重试耗尽后异常文本里会带着页面片段。
            # 命中验证码特征就按「平台阻断」处理，别再让上层当成网络抖动反复重试。
            reason = work_block_reason(str(exc))
            if reason:
                self.work_refused_reason = f"{reason}（{exc}）"
                self.set_work_stat(_job.get("jobid"), refused=self.work_refused_reason)
                logger.error(f'{action}答题被平台阻断：{reason}。{CAPTCHA_HINT}')
                return StudyResult.REFUSED
            raise

        action = "提交" if questions["pyFlag"] == "" else "保存"
        if res_json.get("status"):
            # 面板只看这一条汇总；逐题的「题目 -> 答案」明细在 debug 级别
            logger.info(f'章节检测已{action}（共 {total_questions} 题，'
                        f'题库命中 {found_answers} 题）')
        else:
            msg = res_json.get("msg")
            reason = work_block_reason(msg)
            if reason:
                # 平台阻断提交（图形验证码 / 重做次数用尽）：这是**终局**，不是可重试的
                # 失败。再提交既不会成功、还会把验证码/风控越惹越凶，必须交给引擎停手。
                self.work_refused_reason = f"{reason}（平台返回：{msg}）"
                self.set_work_stat(_job.get("jobid"), refused=self.work_refused_reason)
                logger.error(
                    f'{action}答题被平台阻断：{reason}。'
                    f'{"平台提示：" + str(msg) + "；" if msg else ""}{CAPTCHA_HINT}'
                )
                return StudyResult.REFUSED
            logger.error(f'{action}答题失败 -> {msg}')
            return StudyResult.ERROR

        if need_reanswer:
            # 覆盖率不足 -> 本次只保存了草稿，未提交。
            # 返回非 SUCCESS，避免上层把「未提交/需重新作答」误判为本章已完成；
            # 同时递增回退计数，下次重试时（rollback_times>=1）直接按当前答案提交。
            self.rollback_times += 1
            return StudyResult.UNSUBMITTED

        # 真正提交成功后重置回退计数，使后续章节仍按覆盖率阈值判断
        if questions["pyFlag"] == "":
            self.rollback_times = 0
            self.work_refused_reason = ""     # 新的作业能正常提交，旧的拒绝记录作废
            self.set_work_stat(_job.get("jobid"), refused="")
            # 记住这批答案：引擎回读发现平台仍要重新作答时，用它作废这批答案。
            # 没有这一步，重答会命中同一个缓存答案 -> 再判一次不合格 -> 无限循环。
            self.last_submitted = [dict(item) for item in submitted]
            self.last_submitted_digest = self._digest_of(submitted)
            self.last_verdicts = {}         # 上一批判分不能留给这一批用
            # 按平台判分同步：判错的答案作废（错题本 + 清缓存 + 删题库条目），
            # 只有确认正确的才写进题库（见 _sync_after_submit 的口径）
            self._sync_after_submit(_course, submitted, work_params)
        return StudyResult.SUCCESS

    @staticmethod
    def _digest_of(submitted: list) -> str:
        """作答明细的指纹（标题 + 答案），用于判断两轮重答有没有变化。"""
        try:
            raw = "\x1f".join(f"{it.get('title')}={it.get('answer')}" for it in (submitted or []))
            return md5(raw.encode("utf8")).hexdigest()
        except Exception:  # noqa: BLE001
            return ""

    def _judged_right_qids_of(self, items: list) -> set:
        """给定一批作答明细，返回其中**平台已判对**的 qid 集合（没有判分则空集）。"""
        verdicts = getattr(self, "last_verdicts", None) or {}
        if not verdicts:
            return set()
        right = set()
        for item in items or []:
            if self._judged(item, local_bank.verdict_of(verdicts, item.get("qid"))) is True:
                right.add(str(item.get("qid")))
        return right

    def discard_last_submission(self) -> int:
        """平台判定「需要重新作答」-> 作废这批答案的缓存，下一轮强制重新搜题/请求 AI。

        逐题的判分信息拿不到时（平台未批阅 / 页面结构不认识）这是唯一的破环手段：
        不清理缓存的话，重答会原样命中同一批答案，平台只会再判一次不合格。

        **平台已判对的题不动**：那些答案已经是确定正确的，作废只会让下一轮白白
        重新请求题库提供方（甚至换成一个更差的答案），把正确率越刷越低。
        """
        items = list(getattr(self, "last_submitted", None) or [])
        right = self._judged_right_qids_of(items)   # 必须在清空判分之前取
        self.last_submitted = []
        self.last_submitted_digest = ""
        self.last_verdicts = {}
        if not items:
            return 0
        cache_dao = CacheDAO()
        dropped = kept = 0
        for item in items:
            if str(item.get("qid")) in right:
                kept += 1
                continue
            title = str(item.get("title") or "")
            if title and cache_dao.remove_cache(title):
                dropped += 1
        if dropped or kept:
            suffix = f"，{kept} 题平台已判对、保留原答案" if kept else ""
            logger.warning(f"平台要求重新作答：已作废 {dropped} 题的缓存答案"
                           f"{suffix}，下一轮只重搜未通过的题")
        return dropped

    def quarantine_last_submission(self) -> int:
        """这批答案**原样再交一次仍被判不通过** -> 整批作废（记错题本 + 清缓存 + 删题库同款条目）。

        与 :meth:`discard_last_submission` 的区别：
          * ``discard_last_submission``：平台刚否掉，只清缓存，让下一轮重新搜题；
          * 本方法：连题库里的答案也一并作废 —— 因为「同一批答案交了两轮还是不过」
            已经证明这批答案无效，留着只会让下一次刷课再原样提交一遍（用户反馈的
            「存在错误的作业虽然放弃了但答案还保留着」）。

        **平台已判对的题同样不动**：作业按正确率判及格时（例如 5 题需 ≥90%），
        一道题错了整份就过不了，但另外几道是对的 —— 把它们一起作废，下次重做只能
        全部重新搜题，正确率反而更难回到及格线。

        返回记入错题本的条数。
        """
        items = list(getattr(self, "last_submitted", None) or [])
        right = self._judged_right_qids_of(items)   # 必须在清空判分之前取
        self.last_submitted = []
        self.last_submitted_digest = ""
        self.last_verdicts = {}
        if not items:
            return 0
        bank = None
        try:
            bank = local_bank.get_bank()
        except Exception:  # noqa: BLE001
            bank = None
        cache_dao = CacheDAO()
        marked = kept = 0
        for item in items:
            if str(item.get("qid")) in right:
                kept += 1
                continue
            title = str(item.get("title") or "")
            qtype = str(item.get("type") or "")
            options = str(item.get("options") or "")
            answer = str(item.get("answer") or "")
            if wrong_answers.record_wrong(title, qtype, options, answer):
                marked += 1
            if title:
                cache_dao.remove_cache(title)
            if bank is not None and answer:
                bank.drop_answer(title, qtype, options, answer)
        if items:
            suffix = f"，{kept} 题平台已判对、保留原答案" if kept else ""
            logger.warning(
                f"这批答案原样提交仍被判未通过，已作废（{len(items)} 题中 {marked} 题记入错题本、"
                f"清缓存并删除题库中的同款答案{suffix}），下一轮重新搜题"
            )
        return marked

    def _sync_after_submit(self, course, submitted: list, work_params: dict) -> None:
        """提交后按平台判分结果同步错题本与本地题库。

        一次核验、两处用途（避免重复请求作业页）：
          * 判错 -> 记错题本（重答时避开该答案）+ 清 cache.json + 删题库里那条错答案；
          * 判对 -> 收录本次答案；判错但平台公布了正确答案 -> 收录正确答案。
        拿不到判分信息时**什么都不做**（不猜），由引擎侧的「作废缓存」兜底。
        """
        if not submitted:
            return
        page: dict = {}
        try:
            verdicts = work_verify.verify_work(SessionManager.get_session(), work_params,
                                               page_out=page) or {}
        except Exception as exc:  # noqa: BLE001 - 核验失败不能中断刷课
            logger.warning(f"作业判分核验异常：{type(exc).__name__}: {exc}")
            return
        if not verdicts:
            logger.info("未取到判分信息，本次不记录错题也不更新题库（不猜答案）")
            return
        # 作答页与成绩页的题目 id 不一定同源：按题干把判分对齐到**提交明细的 qid**，
        # 否则明明「对 4 / 错 1」，本地题库却一题都匹配不上（下次重做全部重搜）。
        try:
            verdicts = work_verify.align_verdicts(verdicts, submitted, page.get("html") or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"判分结果对齐异常（按原样使用）：{type(exc).__name__}: {exc}")
        self.last_verdicts = dict(verdicts or {})
        # 记录判分统计：引擎用它判断「这一轮重答有没有进展」。判对题数没涨、
        # 也没拿到新的正确答案 -> 再答一百轮都是同一个结果，必须停下来。
        try:
            right, wrong, fixed = work_verify._counts(self.last_verdicts)
            self.last_verdict_stats = {"right": right, "wrong": wrong, "fixed": fixed}
            self.set_work_stat(work_params.get("jobid"), verdict={"right": right, "wrong": wrong, "fixed": fixed})
        except Exception as exc:  # noqa: BLE001 - 统计失败不影响主流程
            logger.debug(f"判分统计失败: {type(exc).__name__}: {exc}")
            self.last_verdict_stats = {}
            self.set_work_stat(work_params.get("jobid"), verdict={})
        try:
            self._record_rejected(submitted, verdicts)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"错题记录异常：{type(exc).__name__}: {exc}")
        try:
            self._learn_work_answers(course, submitted, work_params, verdicts=verdicts)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"本地题库入库异常：{type(exc).__name__}: {exc}")

    # ---------------- 判分结果 -> 错题本 ----------------
    @staticmethod
    def _judged(item: dict, verdict) -> Optional[bool]:
        """这道题被判对（True）/ 判错（False）/ 说不清（None）。"""
        if verdict is None:
            return None
        if isinstance(verdict, bool):
            return verdict
        qtype = str(item.get("type") or "")
        options = str(item.get("options") or "")
        if isinstance(verdict, str):
            correct = verdict.strip()
            if not correct:
                return None
            same = local_bank.answer_key(qtype, item.get("answer"), options) == \
                local_bank.answer_key(qtype, correct, options)
            return same
        if isinstance(verdict, dict):
            ok = verdict.get("ok")
            if ok is True:
                return True
            if ok is False:
                return False
            correct = str(verdict.get("answer") or "").strip()
            if correct:
                # 页面只给了正确答案：和我提交的一致就算对
                mine = local_bank.answer_key(qtype, item.get("answer"), options)
                right = local_bank.answer_key(qtype, correct,
                                              str(verdict.get("options") or "").strip() or options)
                return bool(mine) and mine == right
        return None

    def _record_rejected(self, submitted: list, verdicts: dict) -> None:
        """把被判错/未通过的答案记进错题本，并清掉缓存与题库里的错答案。

        错题本记录的是平台**判过对**的答案（答对时清掉该题记录），重答时：
        题库/缓存里被判错的答案会被跳过，AI 提示词里也会排除这些选项。
        """
        bank = None
        try:
            bank = local_bank.get_bank()
        except Exception:  # noqa: BLE001
            bank = None
        cache_dao = CacheDAO()
        wrong = cleared = 0
        for item in submitted:
            verdict = local_bank.verdict_of(verdicts, item.get("qid"))
            judged = self._judged(item, verdict)
            title = str(item.get("title") or "")
            qtype = str(item.get("type") or "")
            options = str(item.get("options") or "")
            answer = str(item.get("answer") or "")
            if judged is False:
                # 被判错：记住这个答案（重答时避开）+ 作废缓存 + 删题库错条目。
                # 「包含错误答案的作答不要入库，只有确认正确的再入库」就靠这三步。
                if wrong_answers.record_wrong(title, qtype, options, answer):
                    logger.warning(f"平台判错，已记入错题本：{title} -> {answer}")
                wrong += 1
                if title:
                    cache_dao.remove_cache(title)
                if bank is not None and answer:
                    bank.drop_answer(title, qtype, options, answer)
            elif judged is True:
                # 判对了：这道题的错答记录可以清掉（已经知道对的答案了）
                try:
                    book = wrong_answers.get_book()
                    fp = wrong_answers.fingerprint_for(title, qtype, options)
                    if book.clear(fp):
                        cleared += 1
                except Exception:  # noqa: BLE001
                    pass
        if wrong:
            logger.warning(f"本次 {wrong} 题被平台判错：已作废缓存答案并清除题库中的错误条目"
                           f"（重答时将重新搜题/请求 AI）")
        if cleared:
            logger.info(f"{cleared} 题答对，已清除错题记录")

    def _learn_work_answers(self, course, submitted: list, work_params: dict,
                            verdicts: "dict | None" = None) -> None:
        """按平台判分结果同步本地题库（补缺失 + 纠正答错的题）。

        口径：
          1. 本地题库关闭，或 ``local_bank_verify=false``（不做核验）-> 什么都不做；
          2. **核验整份作业**：只有核验才能发现「用题库答案作答但被判错」的题，
             所以不能再用「库里已有答案」的题集做短路；
          3. 判对 -> 收录本次答案；判错但页面给了「正确答案」-> 收录正确答案；
          4. 库里已有答案且与平台一致 -> 跳过（不重复写）；
             与平台不一致 -> **覆写纠正**（这就是题库答错后的自愈路径）；
          5. 判错且拿不到正确答案 -> 不写（不知道对的是什么，绝不猜）。

        题库只存正确答案，写入位置：``<题库根>/<课程ID>.json``（全账号共享）。
        """
        try:
            bank = local_bank.get_bank()
            if not bank.usable or not bank.verify or not submitted:
                return
            title = str(course.get("title") or "") if isinstance(course, dict) else ""
            course_id = str(course.get("courseId") or "") if isinstance(course, dict) else ""
            known = bank.known(submitted, title, course_id)
            # verdicts 由 _sync_after_submit 传进来（一次核验两处用，别重复请求作业页）
            if verdicts is None:
                verdicts = work_verify.verify_work(SessionManager.get_session(),
                                                   work_params) or None
            if not verdicts:
                logger.info("未取到判分信息，本次不更新本地题库（不猜答案）")
                return
            learner = getattr(self.tiku, "learn_work_answers", None)
            if not callable(learner):
                return
            try:
                learned = learner(submitted, verdicts, course=title, course_id=course_id)
            except TypeError:          # 兼容外部自定义题库没跟进新签名
                learned = learner(submitted, verdicts, course=title)
            stats = dict(getattr(bank, "last_stats", None) or {})
            if learned:
                detail = []
                if stats.get("right"):
                    detail.append(f"{stats['right']} 题平台判对")
                if stats.get("fixed"):
                    detail.append(f"{stats['fixed']} 题按平台正确答案写入")
                if stats.get("corrected"):
                    detail.append(f"{stats['corrected']} 题纠正了库里的错误答案")
                if stats.get("skipped"):
                    detail.append(f"{stats['skipped']} 题库里已一致、未重写")
                logger.info(f"本地题库写入 {learned} 题正确答案"
                            f"（{'，'.join(detail) or '正确答案'}；题库原有 {known} 题命中，"
                            f"共 {bank.stats().get('total', 0)} 题）")
            else:
                logger.info(f"本次没有需要写入的正确答案（{self._learn_skip_reason(stats)}）")
        except Exception as exc:  # noqa: BLE001 - 入库失败绝不能中断刷课
            logger.error(f"本地题库入库流程异常：{type(exc).__name__}: {exc}")

    @staticmethod
    def _learn_skip_reason(stats: dict) -> str:
        """把「0 题入库」的原因说清楚（判分拿到没、对上了没、还是答案本来就一样）。"""
        stats = stats or {}
        parts = []
        if stats.get("skipped"):
            parts.append(f"题库里已有同答案 {stats['skipped']} 题")
        if stats.get("known_elsewhere"):
            parts.append(f"别的课程文件已有同答案 {stats['known_elsewhere']} 题")
        if stats.get("judged_wrong"):
            parts.append(f"判错且页面没给正确答案 {stats['judged_wrong']} 题")
        if stats.get("unmatched"):
            parts.append(f"判分与题目对不上 {stats['unmatched']} 题")
        if stats.get("invalid"):
            parts.append(f"答案无法归一 {stats['invalid']} 题")
        if stats.get("empty_answer"):
            parts.append(f"作答为空 {stats['empty_answer']} 题")
        if parts:
            return "，".join(parts)
        return stats.get("reason") or "无可用判分信息（不猜答案）"

    def study_read(self, _course, _job, _job_info) -> StudyResult:
        """
        阅读任务学习, 仅完成任务点, 并不增长时长
        """
        _session = SessionManager.get_session()
        _resp = _session.get(
            url="https://mooc1.chaoxing.com/ananas/job/readv2",
            params={
                "jobid": _job["jobid"],
                "knowledgeid": _job_info["knowledgeid"],
                "jtoken": _job["jtoken"],
                "courseid": _course["courseId"],
                "clazzid": _course["clazzId"],
            },
        )
        if _resp.status_code != 200:
            logger.error(f"阅读任务学习失败 -> [{_resp.status_code}]{_resp.text}")
            return StudyResult.ERROR
        else:
            logger.info(f"阅读任务完成: {_job.get('name') or _job.get('title') or ''}".rstrip(": "))
            return StudyResult.SUCCESS

    def study_emptypage(self, _course, point):
        _session = SessionManager.get_session()
        # &cpi=0&verificationcode=&mooc2=1&microTopicId=0&editorPreview=0
        _resp = _session.get(
            url="https://mooc1.chaoxing.com/mooc-ans/mycourse/studentstudyAjax",
            params={
                "courseId": _course["courseId"],
                "clazzid": _course["clazzId"],
                "chapterId": point["id"],
                "cpi": _course["cpi"],
                "verificationcode": "",
                "mooc2": 1,
                "microTopicId": 0,
                "editorPreview": 0,
            },
        )
        if _resp.status_code != 200:
            logger.error(f"空页面任务失败 -> [{_resp.status_code}]{point['title']}")
            return StudyResult.ERROR
        else:
            logger.info(f"空页面任务完成 -> {point['title']}")
            return StudyResult.SUCCESS
