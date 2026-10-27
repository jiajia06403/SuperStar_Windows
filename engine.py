# -*- coding: utf-8 -*-
"""
SuperStar Windows 引擎

在 SuperStar_R_Android 的 Python 核心(api/) 之上，提供：
  1. 按账号 uid 缓存完成进度；切换账号时同步切换缓存；提供手动「读取进度」接口
     以用服务端真实进度重新缓存。
     **章节是否完成一律以服务端为准**：本地缓存只是辅助记录，平台若仍要求重新作答
     （判分不合格、未提交等）会清除本地标记并重新处理，避免误判为已完成。
  2. 章节检测提交后回读任务点校验；平台判不合格时重新作答（单次刷课内次数由 work_retry 控制，
     默认 3），重答后仍不通过则保留为未完成（ChapterResult.UNVERIFIED），绝不标记完成。
  3. 切换章节时若发现章节未开放：
       - 回过头检查上一章节是否已经完成，若未完成则重新完成；
       - 若上一章因答题不合格一直未通过（因此本章永远解不了锁），每 reanswer_interval 秒
         再来一轮，但**重答必须能带来新信息**，否则立即停手：轮数上限 reanswer_rounds
         （默认 3）、整份作业一题答案都没搜到、判分连续两轮没有进展（判对题数不涨且
         拿不到正确答案）、提交的答案与上一轮完全相同 —— 任一命中即判定「再答也没用」；
       - 上一章已完成（或本就首章）则每 15 秒检查当前章节状态，直到开放或软件关闭；
       - 自动重答判定「过不了」之后（轮数到顶 / 一题没搜到 / 判分无进展 / 答案没变 /
         平台阻断提交 ``ChapterResult.NO_RETRY``，例如弹图形验证码），**不再立刻结束**：
         先发 ``PROMPT`` 事件让界面弹窗提示「需手动」，**用户关掉弹窗之后**才开始
         manual_wait 秒（默认 300）的计时，期间每 poll_interval 秒回读一次进度；
         做完了就自动接着刷，到点仍无变化才结束本课程
         （否则就是每 15 秒空转的死循环）。没有界面消费 ``PROMPT`` 时（CLI/测试/自检）
         不等待确认，直接开始计时。

  4. **异常隔离**：任务点 / 章节 / 课程三层各自兜底，单点网络或解析异常只会让
     该任务点（最多该章节、该课程）失败，绝不会穿透到主循环把整轮刷课结束掉：
       - ``process_job``：每种任务类型单独 try/except -> StudyResult.ERROR；
       - ``process_chapter``：except Exception -> ChapterResult.ERROR（Cancelled 优先、单独处理）；
       - ``StudyEngine._process_chapter`` / ``_run``：章节级、课程级再兜一层。
     只有用户停止（``cancel.Cancelled``）才会中断整轮。

  5. **并发**：课程之间仍然串行；并发发生在「单门课程内部」：
       - ``jobs``（界面「并发章节」）= 同时进行几个章节，默认 1；
       - ``point_jobs``（设置里的「任务点并发」）= 单章内同时刷几个任务点，默认 1；
       - **遇到「章节未开放」立即把本章之后的处理降为串行（jobs=1）**：
         未开放意味着解锁顺序被打断，再并发推进只会乱序抢进度，因此收束回
         逐章串行、沿用回退重做上一章 / 轮询解锁的既有语义。

本模块与 GUI 解耦：通过 on(event, cb) 注册回调，GUI 只负责把这些回调映射到界面。
"""
import json
import queue
import random
import threading
import time
import traceback
from concurrent.futures.thread import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Callable, Optional

from api import cancel
from api.answer import Tiku
from api.base import (CAPTCHA_HINT, DEFAULT_VIDEO_FINISH_PERCENT, Account, Chaoxing,
                      StudyResult, normalize_finish_percent)
from api.cancel import Cancelled
from api.exceptions import LoginError
from api.logger import logger
from api.live import Live
from api.live_process import LiveProcessor
from account_store import AccountStore, account_display_name
from paths import data_dir as _data_dir

# 视频状态日志默认间隔（秒）；0 表示不输出。可在 config_win.ini 的 [common] 段调整。
DEFAULT_VIDEO_LOG_INTERVAL = 10

# 章节检测「不合格重答」默认次数（[tiku] work_retry 缺省时使用）
DEFAULT_WORK_RETRY = 3

# 「上一章不合格导致本章未开放」时，回退重做上一章的最大轮数（[tiku] reanswer_rounds）。
# 不设上限就会出现日志里那种「继续重答(第 49 轮)」的空转：平台一直判不合格、
# 题库又换不出正确答案，每轮都在白白消耗平台的作业重做次数。
DEFAULT_REANSWER_ROUNDS = 3

# 自动重答判定「过不了」（轮数到顶 / 平台弹验证码 / 一题没搜到 / 判分无进展）之后，
# 留出用户手动在浏览器完成本章的窗口期（秒，[tiku] manual_wait）。
# 界面会先弹窗提示，**用户关掉弹窗之后**才开始计时，期间每 poll_interval 秒回读一次
# 服务端进度：用户真做完了就自动接着刷，到点仍没变化才判无解、结束本课程。
# 0 = 不等（立刻结束）。
DEFAULT_MANUAL_WAIT = 300

# 连续多少轮「判对题数没涨、也没拿到正确答案」就判定重答毫无进展（停止重答）
MAX_NO_PROGRESS_ROUNDS = 2

# 重答时允许「提交的答案与上一轮完全相同」的连续轮数。
# 第 1 次重复：先整批作废这批答案（记错题本 + 清缓存 + 删题库同款条目）再重答；
# 超过这个次数还换不出新答案，说明题库/AI 都已经没有别的可选答案，停止重答转轮询。
MAX_SAME_ANSWER_ROUNDS = 1

# 单章内任务点并发数默认/上限（界面 1~8，config_store.COMMON_RANGES 与之对齐）
DEFAULT_JOBS = 4
MAX_JOBS = 8

# 章节并发的默认值（界面「并发章节」）：默认 1 = 逐章串行，最稳。
DEFAULT_CHAPTER_JOBS = 1
MAX_CHAPTER_JOBS = MAX_JOBS

# 单章内任务点并发（设置里「任务点并发」）：默认 1 = 章内串行。
# 与章节并发是乘法关系，默认都为 1，避免一次开几十路请求撞风控。
DEFAULT_POINT_JOBS = 1
MAX_POINT_JOBS = 8


def normalize_jobs(jobs) -> int:
    """把任意来源的并发数收敛到 1~MAX_JOBS（非法/缺失回落 DEFAULT_JOBS）。"""
    try:
        value = int(float(str(jobs).strip()))
    except (TypeError, ValueError):
        return DEFAULT_JOBS
    return min(MAX_JOBS, max(1, value))


def normalize_point_jobs(jobs) -> int:
    """把「章内任务点并发」收敛到 1~MAX_POINT_JOBS（非法/缺失回落 1）。"""
    if jobs is None or str(jobs).strip() == "":
        return DEFAULT_POINT_JOBS
    try:
        value = int(float(str(jobs).strip()))
    except (TypeError, ValueError):
        return DEFAULT_POINT_JOBS
    return min(MAX_POINT_JOBS, max(1, value))


def point_jobs_from_config(default: int = DEFAULT_POINT_JOBS) -> int:
    """读取 [common] point_jobs（章内任务点并发）；缺省/非法回落 default。"""
    try:
        from config_store import load_section
        raw = load_section("common").get("point_jobs", "")
    except Exception as exc:  # noqa: BLE001 - 配置不可用不得影响刷课
        logger.debug(f"读取任务点并发配置失败，使用默认值: {exc}")
        return normalize_point_jobs(default)
    return normalize_point_jobs(raw if str(raw).strip() else default)


def video_log_interval_from_config(default: float = DEFAULT_VIDEO_LOG_INTERVAL) -> float:
    """读取 [common] video_log_interval（秒）；缺省/非法时回落到 default。"""
    try:
        from config_store import load_section
        raw = str(load_section("common").get("video_log_interval", "") or "").strip()
    except Exception as exc:  # noqa: BLE001 - 配置不可用不得影响刷课
        logger.debug(f"读取视频日志间隔配置失败，使用默认值: {exc}")
        return float(default)
    if raw == "":
        return float(default)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        logger.warning(f"video_log_interval 配置无效，已使用默认值 {default}: {raw!r}")
        return float(default)


def video_finish_percent_from_config(
        default: float = DEFAULT_VIDEO_FINISH_PERCENT) -> float:
    """读取 [common] video_finish_percent（90~100，默认 90）；缺省/非法回落 default。

    语义：播到该百分比后开始「复查」（每 5 秒一次）；平台未判通过就继续观看，
    直到通过或整段看完。100 = 恢复旧的「从结尾开始复查」。
    """
    try:
        from config_store import load_section
        raw = load_section("common").get("video_finish_percent", "")
    except Exception as exc:  # noqa: BLE001 - 配置不可用不得影响刷课
        logger.debug(f"读取观看进度百分比配置失败，使用默认值: {exc}")
        return min(100.0, max(0.0, float(default)))
    return normalize_finish_percent(raw, default)

# --------------------------------------------------------------------------- #
# 章节处理结果
# --------------------------------------------------------------------------- #
class ChapterResult(Enum):
    SUCCESS = 0
    ERROR = 1
    NOT_OPEN = 2
    PENDING = 3
    # 任务点都跑完了，但回读服务端仍显示「未通过/需重新作答」（例如章节检测判不合格）。
    # 与 ERROR 区分：不再由外层盲目重试（避免耗尽平台答题次数），且绝不能标记为已完成。
    UNVERIFIED = 4
    # 用户点了停止：中断当前章节，整轮刷课收尾（不重试、不计入失败）
    STOPPED = 5
    # 平台**拒绝重答**（「提交失败，重做次数过多」）：该作业的重做次数已用尽。
    # 与 UNVERIFIED 的本质区别是「不可能靠再答一次过关」，因此上层既不能重试，
    # 也不能回退重答/轮询下一章解锁 —— 后续章节永久锁死，必须结束本课程。
    NO_RETRY = 6


# --------------------------------------------------------------------------- #
# 单任务点处理（来自原 main.py，保持核心逻辑不变）
# --------------------------------------------------------------------------- #
def _guard_job(job: dict, course: dict, label: str, call: Callable[[], StudyResult]) -> StudyResult:
    """执行单个任务点调用，把异常收敛成 StudyResult.ERROR。

    这是「异常隔离」的第一道闸：一个任务点的网络/解析异常只能废掉它自己，
    同章其它任务点照跑（否则异常会经 ThreadPoolExecutor.map 冒到 _run，
    把整轮刷课直接结束掉）。

    停止信号（Cancelled）**不在这里收敛** —— 必须继续上抛，交给
    ``process_chapter`` 转成 ChapterResult.STOPPED，保持原有的停止语义。
    """
    try:
        return call()
    except Cancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"{label}异常，已跳过（同章其它任务点继续）: 课程: {course.get('title')} "
            f"任务ID: {job.get('jobid')} -> {type(exc).__name__}: {exc}"
        )
        logger.debug(traceback.format_exc())
        return StudyResult.ERROR


def process_job(chaoxing: Chaoxing, course: dict, job: dict, job_info: dict, speed: float) -> StudyResult:
    if job["type"] == "video":
        logger.trace(f"识别到视频任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        video_result = _guard_job(
            job, course, "视频任务",
            lambda: chaoxing.study_video(course, job, job_info, _speed=speed, _type="Video"),
        )
        if video_result.is_failure():
            logger.warning("当前任务非视频任务, 正在尝试音频任务解码")
            video_result = _guard_job(
                job, course, "音频任务",
                lambda: chaoxing.study_video(course, job, job_info, _speed=speed, _type="Audio"),
            )
        if video_result.is_failure():
            logger.warning(
                f"出现异常任务 -> 任务章节: {course['title']} 任务ID: {job['jobid']}, 已跳过"
            )
        return video_result
    elif job["type"] == "document":
        logger.trace(f"识别到文档任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        return _guard_job(job, course, "文档任务", lambda: chaoxing.study_document(course, job))
    elif job["type"] == "workid":
        logger.trace(f"识别到章节检测任务, 任务章节: {course['title']}")
        return _guard_job(job, course, "章节检测任务",
                          lambda: chaoxing.study_work(course, job, job_info))
    elif job["type"] == "read":
        logger.trace(f"识别到阅读任务, 任务章节: {course['title']}")
        return _guard_job(job, course, "阅读任务",
                          lambda: chaoxing.study_read(course, job, job_info))
    elif job["type"] == "live":
        logger.trace(f"识别到直播任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        try:
            defaults = {
                "userid": chaoxing.get_uid(),
                "clazzId": course.get("clazzId"),
                "knowledgeid": job_info.get("knowledgeid"),
            }
            live = Live(attachment=job, defaults=defaults, course_id=course.get("courseId"))
            thread = threading.Thread(target=LiveProcessor.run_live, args=(live, speed), daemon=True)
            thread.start()
            # 分段 join：停止时不必等直播循环跑完
            while thread.is_alive():
                thread.join(timeout=0.5)
                cancel.check()
            thread.join()
            return StudyResult.SUCCESS
        except Cancelled:
            # 停止信号必须原样上抛，不能被当成任务失败
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"处理直播任务时出错: {str(e)}")
            return StudyResult.ERROR

    logger.error(f"未知任务类型: {job['type']}")
    return StudyResult.ERROR


def _tiku_disabled(chaoxing: Chaoxing) -> bool:
    """题库是否停用（未配置题库时无法答题，此时不做回读重答以免白耗平台次数）。"""
    tiku = getattr(chaoxing, "tiku", None)
    return tiku is None or bool(getattr(tiku, "DISABLE", False))


def _job_unreadable(job_info) -> bool:
    """任务点读取失败标记（非 200 / 掉登录 / 风控页 / 解析失败）。"""
    return isinstance(job_info, dict) and bool(job_info.get("unreadable", False))


def _int_from_config(cfg: Optional[dict], key: str, default: int) -> int:
    """从 [tiku] 配置读整数值（缺失/非法时用 default，不抛异常）。"""
    if not cfg:
        return default
    raw = cfg.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(0, int(float(str(raw).strip())))
    except (TypeError, ValueError):
        logger.warning(f"{key} 配置无效，已忽略: {raw!r}")
        return default


def _no_answer_found(chaoxing: Chaoxing) -> bool:
    """本轮作答是否有题目「真的搜到了答案」（题库/缓存/AI/本地题库）。

    ``last_answer_stats.found == 0`` 表示整份作业全靠随机兜底：随机答案在
    「正确率判及格」的作业上几乎不可能通过，重答只是白耗平台的重做次数。
    """
    stats = getattr(chaoxing, "last_answer_stats", None) or {}
    try:
        total = int(stats.get("total") or 0)
        found = int(stats.get("found") or 0)
    except (TypeError, ValueError):
        return False
    return total > 0 and found == 0


def _verdict_stats(chaoxing: Chaoxing) -> dict:
    """最近一轮的判分统计（{} = 平台没给判分信息，无法判断有无进展）。"""
    stats = getattr(chaoxing, "last_verdict_stats", None)
    return dict(stats) if isinstance(stats, dict) else {}


def _work_card_nums(jobs, job_info) -> Optional[str]:
    """章节检测(workid)落在哪几张卡片上（``get_job_list(track_cards=True)`` 才有归属信息）。

    目的是复检时只回读这几张卡片：was-not-workid 的卡片不会因为重答而长出新的
    workid（哪张卡有无测验由老师排版决定），全量回扫一遍纯属浪费请求。
    拿不到归属信息时返回 ``None``，调用方退回全量读取。
    """
    card_of = job_info.get("card_of_job") if isinstance(job_info, dict) else None
    if not isinstance(card_of, dict) or not card_of:
        return None
    nums: list[str] = []
    for job in jobs or []:
        if not isinstance(job, dict) or job.get("type") != "workid":
            continue
        key = str(job.get("jobid") or job.get("id") or "")
        num = card_of.get(key)
        if num is not None and num not in nums:
            nums.append(str(num))
    return "".join(nums) if nums else None


def _work_still_pending(chaoxing: Chaoxing, course: dict, point: dict,
                        nums: Optional[str] = None) -> Optional[bool]:
    """提交答题后回读章节任务点，判断章节检测是否仍未通过。

    ``nums``：只回读这些卡片（通常是上次读到 workid 的那一张），缺省才会全扫。

    超星的任务点卡片只有在 ``isPassed`` 为真时才被解析器跳过，因此回读到的 job 列表里
    若仍存在 ``workid`` 任务，说明平台仍要求「重新作答」（判分不合格 / 未提交）。

    返回 True=仍需重做，False=已通过，None=无法判断（网络/解析异常 / 读不到任务点）。
    调用方**不得**把 None 当作已通过。
    """
    try:
        jobs, job_info = chaoxing.get_job_list(course, point, nums=nums)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"回读章节任务点失败，跳过完成校验: {e}")
        return None
    if not isinstance(jobs, list):
        return None
    if _job_unreadable(job_info):
        # 压根没读到任务点：无法判断通过与否，不能当成「已通过」
        return None
    if isinstance(job_info, dict) and job_info.get("notOpen", False):
        # 章节突然变回未开放，属于异常情况，不参与完成校验
        return None
    return any(j.get("type") == "workid" for j in jobs)


def _run_job_batch(chaoxing: Chaoxing, course: dict, jobs: list[dict], job_info: dict,
                   speed: float, max_workers: int) -> list[StudyResult]:
    """按并发上限跑完本章任务点（保持任务点原有顺序）。

    ``max_workers <= 1`` 时**纯串行、不建线程**：便于排查顺序问题，也把并发风控降到最低。
    并发 >1 时才用线程池；``executor.map`` 保证结果顺序与输入一致（乱序会让上层
    "任一失败即整章失败" 的判断难以复现）。
    """
    if max_workers <= 1:
        return [process_job(chaoxing, course, j, job_info, speed) for j in jobs]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(executor.map(
            lambda j: process_job(chaoxing, course, j, job_info, speed), jobs))


def process_chapter(chaoxing: Chaoxing, course: dict, point: dict, speed: float,
                    work_retry: int = DEFAULT_WORK_RETRY,
                    jobs: int = DEFAULT_JOBS) -> ChapterResult:
    """处理单个章节，返回 ChapterResult。

    ``work_retry``：章节检测提交后回读发现「仍未通过」时允许的重新作答次数。
    平台判分不合格（需要重新作答）时不会再被当作已完成。

    ``jobs``：本章内任务点的并发上限（1 = 串行）。由 ``StudyEngine.point_jobs``
    透传（「设置 → 视频学习 → 任务点并发」，默认 1）；取值范围收敛见
    :func:`normalize_point_jobs`。章节级的并发在 :meth:`StudyEngine._study_course`
    里做，不在本函数。

    并发安全性（8 并发评估，2026-10）：
      - ~~``api.local_bank.set_scope()`` 进程级全局作用域~~ **已移除**：课程归属改成
        ``course`` / ``course_id`` 显式传参（``Tiku.query`` -> ``BankStore.lookup``，
        ``learn_work_answers`` -> ``BankStore.learn``），并发作业各写各的课程文件。
        ``set_scope`` 仅作为串行调用方（CLI / 历史单测）的兼容入口保留，刷课与采集都不再用。
      - ``Tiku.query()`` 每次新建 ``CacheDAO()``，但锁已提升为**按缓存文件共享的模块级
        RLock**（``api.answer._cache_lock_for``）：指向同一份 cache.json 的实例共用一把锁，
        ``add_cache`` 的 read-modify-write 整段互斥，不再丢更新（回归见
        ``test/test_cache_concurrency.py``：8 线程各写 1 题，8 条都在）。
      - ``Tiku._failure_count`` / ``_unavailable_reason`` 无锁自增，最坏是误判一次
        "连续失败 -> 停用"，随后会被成功查询重置。
      - ``local_bank`` 自身读写走模块级 ``_LOCK``，并发安全。
      - 结论：1~8 并发均可安全使用，未发现会破坏刷课正确性的共享状态竞争。
    """
    logger.info(f'当前章节: {point["title"]}')
    if point.get("has_finished"):
        logger.info(f'章节：{point["title"]} 已完成所有任务点')
        return ChapterResult.SUCCESS

    try:
        work_retry = max(0, int(work_retry))
    except (TypeError, ValueError):
        work_retry = DEFAULT_WORK_RETRY
    # 注意：下面的局部变量 jobs 是任务点列表，并发上限必须先另存（否则被覆盖）
    max_workers = normalize_jobs(jobs)

    reanswer = 0
    first_read = True     # 首次读取任务点：空列表按「读取失败」处理
    # 进入章节先清掉上一章的「平台拒绝重答」标记与判分统计：
    # 本轮的结论只能来自本章，不能拿上一章的数据误判「有进展」。
    if hasattr(chaoxing, "work_refused_reason"):
        chaoxing.work_refused_reason = ""
    if hasattr(chaoxing, "last_answer_stats"):
        chaoxing.last_answer_stats = {}
    if hasattr(chaoxing, "last_verdict_stats"):
        chaoxing.last_verdict_stats = {}
    try:
        while True:
            cancel.check()
            chaoxing.rate_limiter.limit_rate(random_time=True, random_min=0, random_max=0.2)

            # 记下任务点来自哪张卡片：回读章节检测状态时可以只回读那几张，不必全扫
            jobs, job_info = chaoxing.get_job_list(course, point, track_cards=True)

            # 发现未开放章节
            if job_info.get("notOpen", False):
                return ChapterResult.NOT_OPEN

            # 取不到任务点（非 200 / 掉登录 / 风控页 / 解析失败）必须是失败态。
            # 空列表只在「确实没有任务点」时才算完成（get_job_list 会带 empty=True）；
            # 否则一次网络抖动就会把整章静默记为已完成并永久跳过。
            if _job_unreadable(job_info) or not isinstance(jobs, list) or (
                    first_read and not jobs and not job_info.get("empty", False)):
                logger.error(f"章节任务点读取失败，保留为未完成: {point['title']}")
                return ChapterResult.ERROR
            first_read = False

            job_results = _run_job_batch(chaoxing, course, jobs, job_info, speed, max_workers)

            # 平台拒绝重答（重做次数用尽）是终局：先于普通失败判断，否则会被
            # 当成可重试的 ERROR，进而回退重答 / 等下一章解锁 —— 那是死循环。
            if StudyResult.REFUSED in job_results:
                reason = getattr(chaoxing, "work_refused_reason", "") or "重做次数已用尽"
                logger.error(
                    f"平台拒绝重答，本章无法完成（{reason}）: {point['title']}"
                )
                return ChapterResult.NO_RETRY

            for result in job_results:
                if result.is_failure():
                    return ChapterResult.ERROR

            # 本章没有章节检测任务 -> 任务点全部跑完即视为完成
            if not any(j.get("type") == "workid" for j in jobs):
                return ChapterResult.SUCCESS

            # 题库停用（未配置题库）时无法答题，不做回读重答，避免白白消耗平台答题次数
            if _tiku_disabled(chaoxing):
                logger.info(f"题库未启用，跳过章节检测完成校验: {point['title']}")
                return ChapterResult.SUCCESS

            # 有章节检测：提交后回读服务端，确认平台真的接受了这次作答。
            # 平台判「不合格/需重新作答」时任务点仍为未通过，必须重新作答而不是草率算完成。
            # 只回读「本次真的存在 workid」的那几张卡片（拿不到归属信息才全扫）
            pending = _work_still_pending(chaoxing, course, point, nums=_work_card_nums(jobs, job_info))
            if pending is None:
                # 回读不到任务点 -> 无法确认通过，按「未通过」处理（绝不记为已完成）
                logger.warning(f"未能回读章节检测状态，按未通过处理: {point['title']}")
                pending = True
            if not pending:
                logger.info(f"回读确认章节检测已通过: {point['title']}")
                return ChapterResult.SUCCESS

            if reanswer >= work_retry:
                logger.error(
                    f"章节检测提交后平台仍判定未通过（需重新作答），已重答 {reanswer} 次，"
                    f"放弃并保留为未完成: {point['title']}"
                )
                return ChapterResult.UNVERIFIED

            # 整份作业一题都没搜到答案（全靠随机兜底）-> 重答换不出正确答案，
            # 只是白耗平台的作业重做次数。最多再给它 1 次机会（题库/AI 偶发失败）。
            if _no_answer_found(chaoxing) and reanswer >= 1:
                logger.error(
                    f"本章作业没有搜到任何答案（全靠随机作答），重答不会通过，"
                    f"停止重答并保留为未完成: {point['title']}"
                )
                return ChapterResult.UNVERIFIED

            reanswer += 1
            logger.warning(
                f"章节检测提交后平台仍显示未通过，重新作答({reanswer}/{work_retry}): {point['title']}"
            )
            # 平台已经明确否掉这批答案：作废它们的缓存，逼下一轮重新搜题/请求 AI。
            # 不做这一步就会一直提交同一批答案（AI 答错题后无限循环的根因）。
            discard = getattr(chaoxing, "discard_last_submission", None)
            if callable(discard):
                try:
                    discard()
                except Exception as exc:  # noqa: BLE001 - 作废失败不能中断重答
                    logger.warning(f"作废上轮答案缓存失败（继续重答）：{type(exc).__name__}: {exc}")
            if cancel.wait(min(3.0, 1.0 * reanswer)):
                raise Cancelled()
    except Cancelled:
        # 停止信号优先：单独处理，保持「中断整轮」的语义（上层收尾，不重试）
        logger.info(f"收到停止信号，中断章节处理: {point['title']}")
        return ChapterResult.STOPPED
    except Exception as exc:  # noqa: BLE001
        # 第二道闸：章节级兜底。单章异常一律记为 ERROR 交给上层重试/跳过，
        # 绝不能冒到 _run 把整轮刷课结束掉。
        logger.error(
            f"章节处理异常（已隔离，不影响其它章节）: {point['title']} "
            f"-> {type(exc).__name__}: {exc}"
        )
        logger.error(traceback.format_exc())
        return ChapterResult.ERROR


# --------------------------------------------------------------------------- #
# 进度缓存：按 uid 隔离
# --------------------------------------------------------------------------- #
class ProgressCache:
    """
    按账号 uid 缓存每门课程的章节完成状态。
    文件落盘于 <base_dir>/<uid>.json，结构：
        { "<courseId>": { "<pointId>": true, ... }, ... }
    """

    def __init__(self, base_dir: "str | None" = None):
        self.base_dir = Path(base_dir or _data_dir())
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._mem: dict[str, dict] = {}

    # ---- 路径 / 读写 ----
    def _path(self, uid: str) -> Path:
        safe = "".join(c for c in str(uid) if c.isalnum() or c in "-_")
        return self.base_dir / f"{safe or 'unknown'}.json"

    def load(self, uid: str) -> dict:
        with self._lock:
            if uid in self._mem:
                return self._mem[uid]
            p = self._path(uid)
            data: dict = {}
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf8"))
                except Exception:  # noqa: BLE001
                    logger.warning(f"进度缓存解析失败，使用空缓存: {p}")
                    data = {}
            self._mem[uid] = data
            return data

    def flush(self, uid: str) -> None:
        with self._lock:
            data = self._mem.get(uid, {})
            p = self._path(uid)
            tmp = p.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
                tmp.replace(p)
            except Exception as e:  # noqa: BLE001
                logger.error(f"写入进度缓存失败: {e}")
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass

    # ---- 查询 / 标记 ----
    def is_done(self, uid: str, course_id: str, point_id: str) -> bool:
        return bool(self.load(uid).get(str(course_id), {}).get(str(point_id), False))

    def set_done(self, uid: str, course_id: str, point_id: str, value: bool = True) -> None:
        """仅写入内存，不落盘（用于运行期高频更新）。"""
        with self._lock:
            data = self.load(uid)
            data.setdefault(str(course_id), {})[str(point_id)] = value

    def mark_done(self, uid: str, course_id: str, point_id: str) -> None:
        self.set_done(uid, course_id, point_id, True)
        self.flush(uid)

    # ---- 与服务端进度对齐 ----
    def sync_with_points(self, uid: str, course_id: str, points: list[dict]) -> None:
        """以服务端章节状态为准，双向对齐本地缓存。

        - 服务端「已完成」→ 本地标记完成
        - 服务端「未完成」→ **清除**本地完成标记

        后者是关键：本地标记只是在当次运行中「我们以为跑完了」，
        若平台侧仍要求重新作答（判分不合格 / 未提交），就必须以平台为准清掉标记，
        否则下次会误判为已完成而直接跳过。
        """
        if not uid:
            return
        cid = str(course_id)
        with self._lock:
            data = self.load(uid)
            course_map = data.setdefault(cid, {})
            changed = False
            for point in points:
                pid = str(point.get("id"))
                if point.get("has_finished"):
                    if course_map.get(pid) is not True:
                        course_map[pid] = True
                        changed = True
                elif course_map.get(pid):
                    course_map[pid] = False
                    changed = True
                    logger.warning(
                        f"服务端显示章节未完成，已清除本地完成记录: {point.get('title', pid)}"
                    )
            if changed:
                self.flush(uid)

    def read_progress_from_server(self, chaoxing: Chaoxing, course: dict,
                                  uid: Optional[str] = None) -> list[dict]:
        """
        手动「读取进度」：调用服务端接口读取课程章节的真实完成状态，
        并以此双向对齐该账号该课程的缓存（已完成标记完成，未完成清除标记）。
        返回章节列表（每项附带 cache_done，便于界面展示）。
        """
        uid = uid or chaoxing.get_uid()
        point_list = chaoxing.get_course_point(
            course["courseId"], course["clazzId"], course["cpi"]
        )
        points = point_list.get("points", [])
        self.sync_with_points(uid, course["courseId"], points)
        cid = str(course["courseId"])
        for point in points:
            point["cache_done"] = self.is_done(uid, cid, str(point.get("id")))
        logger.info(
            f"已同步学习进度：{course['title']}（完成 "
            f"{sum(1 for p in points if p.get('has_finished'))}/{len(points)} 章）"
        )
        return points


# --------------------------------------------------------------------------- #
# 学习引擎（与 GUI 解耦，通过回调上报事件）
# --------------------------------------------------------------------------- #
class StudyEngine:
    # 事件名
    LOG = "log"
    PROGRESS = "progress"          # (course_id, point_id, status)
    CHAPTER = "chapter"            # (course_id, index, total, title)
    COURSE = "course"              # (course_id, phase)；phase = "start" | "end"
    STATUS = "status"              # (state, message)
    ACCOUNTS = "accounts"          # (uid_list)
    FINISHED = "finished"          # (stopped: bool)；同时记录到 engine.last_stopped
    # (prev_title, cur_title, seconds)：需要用户手动处理，界面弹窗提示；
    # 用户关掉弹窗后必须调用 ack_manual()，引擎才真正开始 manual_wait 计时。
    PROMPT = "prompt"

    def __init__(self, cache_dir: "str | None" = None, poll_interval: int = 15,
                 reanswer_interval: float = 5.0):
        cache_dir = str(cache_dir or _data_dir())
        self.cache = ProgressCache(cache_dir)
        self.poll_interval = poll_interval
        # 「上一章答题不合格导致本章未开放」时两轮重答之间的间隔（秒）
        self.reanswer_interval = max(0.0, float(reanswer_interval))
        self.chaoxing: Optional[Chaoxing] = None
        self.current_uid: Optional[str] = None
        self.accounts: dict[str, dict] = {}      # uid -> {username, password, use_cookies}
        self._store = AccountStore(Path(cache_dir) / "accounts.json")
        self.accounts = self._store.load()       # 启动即恢复已保存账号
        self._handlers: dict[str, list[Callable]] = {}
        self._stop = False
        self.last_stopped = False   # 本轮是否被用户停止（FINISHED 时置位，供界面区分文案）
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        # 运行状态
        self.speed = 2.0
        # 「并发章节」：同一门课同时处理几个章节（界面取值）；1 = 逐章串行
        self.jobs = DEFAULT_CHAPTER_JOBS
        # 「任务点并发」：单个章节内部同时刷几个任务点（设置里的取值）；1 = 章内串行
        self.point_jobs = point_jobs_from_config()
        self.query_delay = 0
        self.work_retry = DEFAULT_WORK_RETRY   # 单次刷课内章节检测不合格的重新作答次数
        # 回退重做上一章的最大轮数（0/非法按默认值）：防止「题库换不出答案」时无限重答
        self.reanswer_rounds = DEFAULT_REANSWER_ROUNDS
        # 自动重答放弃后，等待用户手动完成本章的秒数（[tiku] manual_wait）
        self.manual_wait = DEFAULT_MANUAL_WAIT
        # 等待界面确认「需手动」提示的事件队列（FIFO：并发章节各等各的）
        self._manual_acks: list[threading.Event] = []
        # 视频状态日志间隔（秒，0=关闭）：观看视频时每 N 秒输出章节号/进度百分比等
        self.video_log_interval = video_log_interval_from_config()
        # 观看进度要求（90~100）：播到该比例后每 5 秒复查一次，未通过就继续看
        # （100 = 旧行为，从结尾开始复查）
        self.video_finish_percent = video_finish_percent_from_config()

    # ---- 事件订阅 ----
    def on(self, event: str, cb: Callable) -> None:
        self._handlers.setdefault(event, []).append(cb)

    def _emit(self, event: str, *args) -> None:
        for cb in self._handlers.get(event, []):
            try:
                cb(*args)
            except Exception:  # noqa: BLE001
                logger.debug(f"事件回调异常({event}): {traceback.format_exc()}")

    def ack_manual(self) -> None:
        """界面确认「需手动」提示已关闭：放行最早一个等待中的计时。

        必须由 **主线程** 在弹窗关闭后调用（引擎线程此时正阻塞在
        ``_await_manual_ack`` 里）。没有等待中的请求时是空操作。
        """
        with self._lock:
            pending = self._manual_acks.pop(0) if self._manual_acks else None
        if pending is not None:
            pending.set()

    # ---- 登录 / 账号切换 ----
    @staticmethod
    def _work_retry_from_config(tiku_config: Optional[dict]) -> Optional[int]:
        """从 [tiku] 配置读取章节检测重答次数（缺省/非法时返回 None）。"""
        if not tiku_config:
            return None
        raw = tiku_config.get("work_retry")
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return max(0, int(float(str(raw).strip())))
        except (TypeError, ValueError):
            logger.warning(f"work_retry 配置无效，已忽略: {raw!r}")
            return None

    def _apply_work_retry(self, tiku_config: Optional[dict]) -> None:
        value = self._work_retry_from_config(tiku_config)
        if value is not None:
            self.work_retry = value
        rounds = _int_from_config(tiku_config, "reanswer_rounds", DEFAULT_REANSWER_ROUNDS)
        # 至少 1 轮：允许 0 就等于「一题不做」，会让整门课直接放弃
        self.reanswer_rounds = max(1, rounds)
        self.manual_wait = _int_from_config(tiku_config, "manual_wait", DEFAULT_MANUAL_WAIT)

    def _build_chaoxing(self, username: str, password: str, use_cookies: bool,
                        tiku_config: Optional[dict] = None) -> Chaoxing:
        account = Account(username, password)
        tiku = Tiku()
        tiku.config_set(tiku_config or {})
        tiku = tiku.get_tiku_from_config()
        tiku.init_tiku()
        chaoxing = Chaoxing(account=account, tiku=tiku, query_delay=self.query_delay)
        self._push_video_config(chaoxing)
        return chaoxing

    # ---- 视频配置（状态日志间隔 + 观看进度要求）----
    def _push_video_config(self, chaoxing=None) -> None:
        """把视频相关配置写入后端实例：状态日志间隔（0=关闭）+ 观看进度百分比。"""
        target = chaoxing if chaoxing is not None else self.chaoxing
        if target is None:
            return
        try:
            target.video_log_interval = float(self.video_log_interval or 0)
        except (TypeError, ValueError):
            target.video_log_interval = float(DEFAULT_VIDEO_LOG_INTERVAL)
        target.video_finish_percent = normalize_finish_percent(self.video_finish_percent)

    # 兼容旧名（历史调用方/测试仍可能用 _push_video_log）
    _push_video_log = _push_video_config

    def apply_video_config(self) -> float:
        """重新读取 [common] 的视频配置并即时生效（设置保存后调用）。

        返回生效的观看进度百分比（90~100）。正在刷课时也会作用于当前视频循环。
        顺带刷新「任务点并发」（point_jobs）：设置里改完立即作用于后续章节。
        """
        self.video_log_interval = video_log_interval_from_config()
        self.video_finish_percent = video_finish_percent_from_config()
        self.point_jobs = point_jobs_from_config()
        self._push_video_config()
        return self.video_finish_percent

    def apply_video_log_config(self) -> float:
        """兼容入口：重读视频配置并返回状态日志间隔（秒，0=关闭）。"""
        self.apply_video_config()
        return self.video_log_interval

    def _set_video_context(self, point: dict, index: int, total: int) -> None:
        """记录当前章节（序号/总数/标题），供视频状态日志显示「第几章/共几章」。"""
        if self.chaoxing is None:
            return
        try:
            self.chaoxing.video_context = {
                "chapter_index": int(index),
                "chapter_total": int(total),
                "chapter_title": str(point.get("title") or ""),
            }
        except Exception as exc:  # noqa: BLE001 - 仅用于日志展示，失败无妨
            logger.debug(f"设置章节上下文失败: {exc}")

    def login(self, username: str, password: str, use_cookies: bool = False,
              tiku_config: Optional[dict] = None, remember: bool = True,
              name: Optional[str] = None, reject_existing: bool = False) -> str:
        """登录并返回当前账号 uid；同时建立/切换该 uid 的进度缓存。

        ``remember``：是否把账号（含密码，DPAPI 加密）写入本机账号文件，
        以便下次启动自动登录。False 时只记住账号名、不保存密码。
        ``name``：本地显示名称；缺省沿用该账号原有名称，再退回手机号 / uid。
        ``reject_existing``：新增账号时置 True —— 登录出来的 uid 已存在则报错，
        避免「重复添加同一个账号」把列表搞乱（自动登录 / 切换账号不要传它，
        那种场景下重复登录就是刷新凭据）。
        """
        chaoxing = self._build_chaoxing(username, password, use_cookies, tiku_config)
        state = chaoxing.login(login_with_cookies=use_cookies)
        if not state.get("status"):
            raise LoginError(state.get("msg", "登录失败"))
        self._apply_work_retry(tiku_config)
        uid = str(chaoxing.get_uid())
        with self._lock:
            if reject_existing and uid in self.accounts:
                raise LoginError(
                    f"该账号已在列表中（名称：{self.account_name(uid)}），无需重复添加"
                )
            resolved = self._resolve_name(name, uid, username)
            self.chaoxing = chaoxing
            self.current_uid = uid
            self.accounts[uid] = {
                "username": username,
                "name": resolved,
                "password": password if remember else "",
                "use_cookies": use_cookies,
            }
            self.cache.load(uid)  # 切换缓存
        self._persist_accounts(uid)
        logger.info(f"登录成功（名称：{resolved}）")
        self._emit(self.ACCOUNTS, list(self.accounts.keys()))
        self._emit(self.STATUS, "logged_in", f"uid={uid}")
        return uid

    def switch_account(self, uid: str, tiku_config: Optional[dict] = None) -> str:
        """切换到已登录过的账号：重新登录并切换进度缓存。

        账号来自本机账号文件，重启后仍可用；``tiku_config`` 必须重新传入，
        否则切换后题库会因缺少配置而被停用。
        """
        with self._lock:
            acc = self.accounts.get(str(uid))
            if not acc:
                raise LoginError(f"未找到账号缓存: {uid}")
            if self._thread and self._thread.is_alive():
                self.stop()
                self._thread.join(timeout=10)
            chaoxing = self._build_chaoxing(
                acc["username"], acc["password"], acc.get("use_cookies", False), tiku_config
            )
            state = chaoxing.login(login_with_cookies=acc.get("use_cookies", False))
            if not state.get("status"):
                raise LoginError(state.get("msg", "登录失败"))
            self._apply_work_retry(tiku_config)
            new_uid = chaoxing.get_uid()
            self.chaoxing = chaoxing
            self.current_uid = new_uid
            self.cache.load(new_uid)
        self._persist_accounts(new_uid)
        logger.info(f"已切换到账号 uid={new_uid}，进度缓存已切换")
        self._emit(self.ACCOUNTS, list(self.accounts.keys()))
        self._emit(self.STATUS, "switched", f"uid={new_uid}")
        return new_uid

    # ---- 已保存账号 ----
    def _persist_accounts(self, last_uid: Optional[str] = None) -> None:
        """把内存中的账号表写盘（密码加密），并记下最近使用的 uid。

        演示账号（persist=False）不入库。
        """
        with self._lock:
            data = {u: a for u, a in self.accounts.items() if a.get("persist", True)}
            self._store.save(data, str(last_uid or self.current_uid or ""))

    @property
    def last_uid(self) -> str:
        """最近一次登录的 uid（未登录时取账号文件里记录的）。"""
        return str(self.current_uid or self._store.last_uid or "")

    def saved_account(self, uid: Optional[str] = None) -> Optional[dict]:
        """返回可直接用于登录的已保存账号；缺密码且非 cookies 模式时返回 None。"""
        target = str(uid or self.last_uid or "")
        acc = self.accounts.get(target)
        if not acc:
            return None
        if not acc.get("use_cookies") and not (acc.get("username") and acc.get("password")):
            return None
        return {
            "uid": target,
            "username": acc.get("username", ""),
            "name": account_display_name(target, acc),
            "password": acc.get("password", ""),
            "use_cookies": bool(acc.get("use_cookies", False)),
        }

    # ---- 账号名称（本地显示名 / 重名判重）----
    def account_name(self, uid: str) -> str:
        """账号的本地显示名：自定义名称 > 手机号 > uid（永远非空，除非没这个账号）。"""
        uid = str(uid or "")
        return account_display_name(uid, self.accounts.get(uid) or {})

    def name_owner(self, name: str, exclude_uid: str = "") -> str:
        """返回占用该名称的账号 uid（没有则空串）。

        ``exclude_uid`` 用于改名：排除账号自己，否则「改成原名」会被判成重名。
        比较忽略大小写与首尾空白（中文名称等价于精确比较）。
        """
        target = str(name or "").strip().casefold()
        if not target:
            return ""
        skip = str(exclude_uid or "")
        for uid in list(self.accounts.keys()):
            if str(uid) == skip:
                continue
            if self.account_name(uid).casefold() == target:
                return str(uid)
        return ""

    def _resolve_name(self, name: Optional[str], uid: str, username: str = "") -> str:
        """确定账号名称：显式取名 > 原有名称 > uid（默认不展示手机号）；重名抛 ``LoginError``。"""
        target = str(name or "").strip()
        if not target:
            prev = self.accounts.get(str(uid)) or {}
            target = str(prev.get("name") or "").strip()
        if not target:
            target = str(uid)
        owner = self.name_owner(target, exclude_uid=uid)
        if owner:
            raise LoginError(f"名称「{target}」已被账号 {owner} 使用，请换一个")
        return target

    def rename_account(self, uid: str, name: str) -> str:
        """修改账号的本地名称并落盘，返回生效后的名称。

        名称不得与其他账号重名，冲突抛 ``LoginError``（界面据此行内报错，不改动任何数据）。
        """
        uid = str(uid or "")
        with self._lock:
            acc = self.accounts.get(uid)
            if acc is None:
                raise LoginError(f"未找到账号: {uid}")
            target = str(name or "").strip()
            if not target:                     # 留空 = 用该账号的默认名称
                target = account_display_name(uid, acc) or uid
            owner = self.name_owner(target, exclude_uid=uid)
            if owner:
                raise LoginError(f"名称「{target}」已被账号 {owner} 使用，请换一个")
            acc["name"] = target
        self._persist_accounts()
        logger.info(f"账号名称已改为「{target}」（uid={uid}）")
        self._emit(self.ACCOUNTS, list(self.accounts.keys()))
        return target

    def delete_account(self, uid: str) -> bool:
        """删除已保存的账号（连同其登录状态），返回「删掉的是否当前账号」。

        当前账号被删时清空 ``chaoxing`` / ``current_uid``，界面据此清空课程列表
        并回到「未登录」，避免删完后还用着已删除账号的会话。
        """
        uid = str(uid or "")
        with self._lock:
            if uid not in self.accounts:
                return False
            name = self.account_name(uid)
            self.accounts.pop(uid, None)
            dropped = uid == str(self.current_uid or "")
            if dropped:
                self.current_uid = None
                self.chaoxing = None
        self._persist_accounts("" if dropped else None)
        logger.info(f"已删除账号「{name}」（uid={uid}）")
        self._emit(self.ACCOUNTS, list(self.accounts.keys()))
        return dropped

    def auto_login(self, tiku_config: Optional[dict] = None) -> Optional[str]:
        """用最近一次的已保存账号自动登录；无可用账号时返回 None。"""
        acc = self.saved_account()
        if not acc:
            return None
        logger.info(f"使用已保存账号自动登录（uid={acc['uid']}）")
        return self.login(
            acc["username"], acc["password"],
            use_cookies=acc["use_cookies"], tiku_config=tiku_config, remember=True,
        )

    def login_demo(self, not_open_ids=()) -> str:
        """演示登录：加载本地假后端 DemoChaoxing，无需真实账号/网络。

        用于本地预览、GUI 联调与自检验证（仅测试包内置该后端）。行为与真实 login 一致：
        建立 chaoxing、设置 current_uid、登记账号、切换进度缓存并上报事件。

        ``not_open_ids``：把指定章节 id 造为「未开放」，便于验证解锁/轮询流程。
        """
        from demo_chaoxing import DemoChaoxing
        self.chaoxing = DemoChaoxing(not_open_ids=not_open_ids)
        self.current_uid = self.chaoxing.get_uid()
        self._push_video_config()
        with self._lock:
            self.accounts[self.current_uid] = {
                "username": "demo", "password": "", "use_cookies": False,
                "persist": False,   # 演示账号不写入账号文件
            }
            self.cache.load(self.current_uid)
        logger.info("演示模式已就绪")
        self._emit(self.ACCOUNTS, list(self.accounts.keys()))
        self._emit(self.STATUS, "logged_in", f"uid={self.current_uid} (演示)")
        return self.current_uid

    def apply_tiku_config(self, tiku_config: Optional[dict] = None) -> None:
        """在已登录状态下即时刷新题库/AI 配置（下次登录也会自动重载）。

        未登录（无 chaoxing 实例）时为空操作：配置已落盘，登录时会被读取。
        """
        with self._lock:
            if self.chaoxing is None:
                return
            tiku = Tiku()
            tiku.config_set(tiku_config or {})
            tiku = tiku.get_tiku_from_config()
            tiku.init_tiku()
            self.chaoxing.tiku = tiku
            self._apply_work_retry(tiku_config)

    def list_courses(self) -> list[dict]:
        assert self.chaoxing is not None, "请先登录"
        return self.chaoxing.get_course_list()

    def get_course_points(self, course: dict) -> list[dict]:
        """返回章节列表，并为每个章节附加 cache_done 标记（供界面展示）。

        取到服务端章节状态后会与本地缓存双向对齐：平台说未完成的章节会清掉本地
        「已完成」记录，避免界面/后续刷课把需重新作答的章节误判成已完成。
        """
        uid = self.current_uid
        point_list = self.chaoxing.get_course_point(
            course["courseId"], course["clazzId"], course["cpi"]
        )
        points = point_list.get("points", [])
        cid = str(course["courseId"])
        self.cache.sync_with_points(uid, cid, points)
        for p in points:
            p["cache_done"] = self.cache.is_done(uid, cid, str(p["id"]))
        return points

    def refresh_progress(self, course: dict, emit: bool = True) -> list[dict]:
        """从服务端读取完成状态并对齐缓存（单请求），供「读取进度」/ 勾选课程时渲染。

        ``emit=False`` 时不发逐章 PROGRESS（由调用方统一上报）。
        """
        assert self.chaoxing is not None, "请先登录"
        points = self.cache.read_progress_from_server(
            self.chaoxing, course, uid=self.current_uid
        )
        if emit:
            cid = str(course["courseId"])
            for p in points:
                self._emit(self.PROGRESS, cid, str(p["id"]), self._point_status(p))
        return points

    @staticmethod
    def _point_status(point: dict) -> str:
        """章节状态字符串：done / pending（界面按此渲染）。"""
        return "done" if point.get("has_finished") else "pending"

    # ---- 启动 / 停止 ----
    def start_study(self, course_list: list[dict], speed: float = 2.0,
                    jobs: int = DEFAULT_CHAPTER_JOBS,
                    point_jobs: "int | None" = None) -> None:
        """开始刷课。

        ``jobs``：同一门课「同时进行几个章节」（界面「并发章节」，1 = 逐章串行）。
        ``point_jobs``：单个章节内部「同时刷几个任务点」；None 表示读配置
        （[common] point_jobs），由「设置 → 视频学习」维护。
        """
        assert self.chaoxing is not None, "请先登录"
        self.speed = min(2.0, max(1.0, speed))
        self.jobs = normalize_jobs(jobs)
        self._stop = False
        self.last_stopped = False
        cancel.clear()          # 清掉上一轮的停止标记，否则新一轮刚开始就中断
        # 以配置文件为准刷新视频相关设置（用户可能直接改过 ini），并推给后端
        self.apply_video_config()
        if point_jobs is not None:
            self.point_jobs = normalize_point_jobs(point_jobs)
        logger.info(
            f"并发：同时进行 {self.jobs} 个章节 · 单章内 {self.point_jobs} 个任务点"
            f"（总并发上限约 {self.jobs * self.point_jobs}；遇到未开放章节会自动降为 1）"
        )
        if self.video_log_interval > 0:
            # 只是运行参数说明，界面上没必要占一行，写日志文件备查即可
            logger.debug(
                f"视频状态日志：每 {self.video_log_interval:g} 秒输出一次"
                f"（章节号 / 进度百分比 / 预计剩余；设 0 可关闭）"
            )
        else:
            logger.debug("视频状态日志已关闭（在「设置」里填间隔秒数即可开启）")
        if self.video_finish_percent >= 100:
            logger.debug("观看进度要求：100%（视频必须完整看完，与旧版本一致）")
        else:
            logger.debug(
                f"观看进度要求：{self.video_finish_percent:g}%"
                f"（播到该比例即判定任务点完成，可在「设置 → 视频学习」里改）"
            )
        self._thread = threading.Thread(
            target=self._run, args=(course_list,), daemon=True
        )
        self._thread.start()
        self._emit(self.STATUS, "running", "开始刷课")

    def stop(self) -> None:
        """请求停止：置标记 + 唤醒正在 sleep 的循环，让长任务点尽快中断。"""
        self._stop = True
        cancel.set()
        self._emit(self.STATUS, "stopping", "正在停止...")

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- 主循环 ----
    def _process_chapter(self, course: dict, point: dict) -> ChapterResult:
        """单章处理入口（课程级隔离）。

        ``process_chapter`` 自己已兜底，这里再包一层是为了让「某章异常」在任何情况下
        都只会变成 ERROR/STOPPED —— 异常一旦冒到课程循环，后面的章节就全都不跑了。
        """
        try:
            return process_chapter(self.chaoxing, course, point, self.speed,
                                   work_retry=self.work_retry, jobs=self.point_jobs)
        except Cancelled:
            return ChapterResult.STOPPED
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"章节处理异常（已隔离，继续下一章）: {point.get('title')} "
                f"-> {type(exc).__name__}: {exc}"
            )
            logger.error(traceback.format_exc())
            return ChapterResult.ERROR

    def _run(self, course_list: list[dict]) -> None:
        try:
            for course in course_list:
                if self._stop:
                    break
                cid = str(course.get("courseId"))
                # 课程级开始/结束事件：界面据此把「正在处理」的课程标为「进行中」。
                # 串行时同一时刻只有一门；将来若并行处理多门，各门都会各自保持「进行中」。
                self._emit(self.COURSE, cid, "start")
                try:
                    self._study_course(course)
                except Cancelled:
                    raise          # 停止信号交给外层统一收尾（FINISHED(stopped=True)）
                except Exception as exc:  # noqa: BLE001
                    # 课程级隔离：一门课读章节失败不该让后面选中的课全部不跑
                    logger.error(
                        f"课程处理异常（已跳过该课程，其余课程继续）: "
                        f"{course.get('title')} -> {type(exc).__name__}: {exc}"
                    )
                    logger.error(traceback.format_exc())
                finally:
                    self._emit(self.COURSE, cid, "end")
            if self.current_uid:
                self.cache.flush(self.current_uid)
            self.last_stopped = bool(self._stop)
            self._emit(self.FINISHED, self.last_stopped)
            self._emit(self.STATUS, "idle", "全部完成" if not self._stop else "已停止")
        except Cancelled:
            # 停止信号从深层任务点冒上来（视频/直播/答题），正常收尾而不是报错
            if self.current_uid:
                self.cache.flush(self.current_uid)
            self.last_stopped = True
            self._emit(self.FINISHED, True)
            self._emit(self.STATUS, "idle", "已停止")
        except Exception as e:  # noqa: BLE001
            logger.error(f"学习线程异常: {type(e).__name__}: {e}")
            logger.error(traceback.format_exc())
            self._emit(self.STATUS, "error", f"{type(e).__name__}: {e}")
        finally:
            self._stop = False
            cancel.clear()      # 线程退出后清标记，避免影响下一轮/读取进度

    def _study_course(self, course: dict) -> None:
        """处理一门课程：先按「章节并发」跑，遇到未开放章节自动降为串行。

        降级后**不回到并发**：未开放通常意味着「上一章没做完 / 答题不合格」，
        再并发推进只会乱序抢解锁，串行才符合平台的解锁顺序语义。
        """
        uid = self.current_uid
        cid = str(course["courseId"])
        point_list = self.chaoxing.get_course_point(
            course["courseId"], course["clazzId"], course["cpi"]
        )
        points = point_list.get("points", [])
        total = len(points)
        jobs = normalize_jobs(self.jobs)
        logger.info(
            f"开始学习课程: {course['title']}（共 {total} 个章节；"
            f"章节并发 {jobs} · 任务点并发 {self.point_jobs}）"
        )

        if total > 1 and jobs > 1 and not self._stop:
            degraded = self._study_course_parallel(course, points, cid, uid, jobs)
            if not degraded:
                if uid:
                    self.cache.flush(uid)
                return
            logger.warning(
                f"检测到「未开放」章节，本轮并发降为 1（串行续跑）: {course['title']}"
            )
            self._emit(self.STATUS, "degraded", "出现未开放章节，已改为串行")

        self._study_course_serial(course, points, cid, uid)
        if uid:
            self.cache.flush(uid)

    def _study_course_serial(self, course: dict, points: list[dict],
                             cid: str, uid: "str | None") -> None:
        """逐章串行处理（原 `_study_course` 主体，行为保持不变）。

        并发段跑完的章节已写回 ``point["has_finished"]`` 与进度缓存，
        所以这里从 i=0 扫一遍时会把它们直接跳过，等价于「从断点续跑」。
        """
        total = len(points)
        i = 0
        max_tries = 3
        while i < total:
            if self._stop:
                logger.info("收到停止信号，结束当前课程")
                return

            point = points[i]
            self._emit(self.CHAPTER, cid, i, total, point["title"])
            self._set_video_context(point, i + 1, total)   # 视频状态日志带上章节号

            pid = str(point["id"])

            # 以服务端状态为准：平台显示已完成才跳过。
            # 本地缓存只是「上次以为跑完了」的记录，平台若仍要求重新作答（判分不合格、
            # 未提交等），绝不能据此跳过，否则就会出现「明明要重做却判断已完成」。
            if point.get("has_finished"):
                if not self.cache.is_done(uid, cid, pid):
                    self.cache.mark_done(uid, cid, pid)
                logger.info(f"章节已完成: {point['title']}")
                self._emit(self.PROGRESS, cid, pid, "done")
                i += 1
                continue
            if self.cache.is_done(uid, cid, pid):
                logger.warning(
                    f"本地记录已完成但服务端仍显示未完成，重新作答: {point['title']}"
                )
                self.cache.set_done(uid, cid, pid, False)
                self.cache.flush(uid)
                self._emit(self.PROGRESS, cid, pid, "pending")

            result = self._process_chapter(course, point)

            if result == ChapterResult.STOPPED:
                # 停止信号已打断正在跑的任务点（视频/直播/答题），整轮收尾
                self._emit(self.PROGRESS, cid, pid, "pending")
                logger.info("收到停止信号，结束当前课程")
                return

            if result == ChapterResult.SUCCESS:
                point["has_finished"] = True
                self.cache.mark_done(uid, cid, pid)
                self._emit(self.PROGRESS, cid, pid, "done")
                logger.info(f"章节完成: {point['title']}")
                i += 1
                continue

            if result == ChapterResult.NOT_OPEN:
                # 「未开放」只在后台等待/回退重做，界面上仍显示「待学」
                self._emit(self.PROGRESS, cid, pid, "pending")
                opened = self._handle_not_open(course, points, i, cid, uid)
                if opened is True:
                    # 章节已开放，保持在 i 重新处理本章
                    continue
                if opened is None:
                    # 解锁的前提（上一章作业）已被平台阻断（图形验证码 / 重做次数用尽）：
                    # 本章及后续永久锁死，继续 15 秒一轮地等下去只会空转到天亮。
                    logger.error(
                        f"上一章节已被平台阻断（验证码 / 重做次数用尽），本章无法解锁，"
                        f"结束该课程的刷课: {point['title']}"
                    )
                    self._emit(self.PROGRESS, cid, pid, "error")
                    self._emit(self.STATUS, "blocked",
                               f"平台阻断提交，已停止等待解锁: {point['title']}")
                    return
                # 软件关闭 / 停止
                logger.info("章节未开放且已停止轮询，结束当前课程")
                return

            if result == ChapterResult.NO_RETRY:
                # 平台阻断提交（图形验证码 / 重做次数用尽）：验证码是**会话级**的，
                # 继续刷后面的章节一样会被拦，直接结束本课程并提示用户手动验证。
                point["work_refused"] = True
                reason = getattr(self.chaoxing, "work_refused_reason", "") or "平台阻断提交"
                logger.error(
                    f"平台阻断提交（{reason}），本章无法完成，停止该课程的刷课: {point['title']}"
                )
                logger.error(CAPTCHA_HINT)
                self._emit(self.PROGRESS, cid, pid, "error")
                self._emit(self.STATUS, "blocked",
                           f"平台阻断提交（{reason}）：{CAPTCHA_HINT}")
                return

            if result == ChapterResult.UNVERIFIED:
                # 任务点已提交但平台仍判未通过（需重新作答）。
                # 这里不原地死循环（避免耗尽平台答题次数），先继续下一章；
                # 若下一章因此一直未开放，_handle_not_open 会回退无限重答本章。
                logger.error(
                    f"章节仍未通过平台校验（需重新作答），保留为未完成: {point['title']}"
                )
                self._emit(self.PROGRESS, cid, pid, "error")
                i += 1
                continue

            # ERROR：重试若干次，仍失败则跳过，避免死循环
            tries = 1
            while tries < max_tries and not self._stop:
                logger.warning(f"章节处理出错，重试({tries}/{max_tries}): {point['title']}")
                if cancel.wait(2):
                    logger.info("收到停止信号，结束当前课程")
                    return
                r = self._process_chapter(course, point)
                if r == ChapterResult.STOPPED:
                    return
                if r == ChapterResult.SUCCESS:
                    point["has_finished"] = True
                    self.cache.mark_done(uid, cid, pid)
                    self._emit(self.PROGRESS, cid, pid, "done")
                    i += 1
                    break
                if r == ChapterResult.UNVERIFIED:
                    logger.error(f"章节仍未通过平台校验（需重新作答），保留为未完成: {point['title']}")
                    self._emit(self.PROGRESS, cid, pid, "error")
                    i += 1
                    break
                tries += 1
            else:
                if not self._stop:
                    logger.error(f"章节多次失败，跳过: {point['title']}")
                    self._emit(self.PROGRESS, cid, pid, "error")
                i += 1

    # ---- 章节并发（遇未开放自动降为串行） ----
    def _study_course_parallel(self, course: dict, points: list[dict],
                               cid: str, uid: "str | None", jobs: int) -> bool:
        """按 ``jobs`` 个 worker 并发处理章节，返回「是否需要转串行续跑」。

        约定：
          - 待办队列按章节序号升序取用（``PriorityQueue``），worker 数 = jobs；
          - 任一章节返回 ``NOT_OPEN``：立刻置 ``degraded``，其余 worker 不再领新章节，
            在跑的长任务点跑完后自然退出，由调用方转串行版本续跑；
          - 用户停止：``stopped`` 置位，所有 worker 尽快退出
            （``Cancelled`` 会被 ``_process_chapter`` 收敛成 ``ChapterResult.STOPPED``）。
        """
        total = len(points)
        pending: "queue.PriorityQueue[tuple[int, dict]]" = queue.PriorityQueue()
        for index, point in enumerate(points):
            pending.put((index, point))
        degraded = threading.Event()
        stopped = threading.Event()

        def worker() -> None:
            while not degraded.is_set() and not stopped.is_set():
                if self._stop:
                    stopped.set()
                    return
                try:
                    index, point = pending.get_nowait()
                except queue.Empty:
                    return
                try:
                    self._parallel_chapter(course, points, index, point, total,
                                           cid, uid, degraded, stopped)
                except Cancelled:
                    stopped.set()
                    return
                except Exception as exc:  # noqa: BLE001 - 单章异常不得影响其它章节
                    logger.error(
                        f"并发章节异常（已隔离，其它章节继续）: {point.get('title')} "
                        f"-> {type(exc).__name__}: {exc}"
                    )
                    logger.debug(traceback.format_exc())

        threads = [threading.Thread(target=worker, daemon=True, name=f"chapter-{k + 1}")
                   for k in range(jobs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return degraded.is_set() and not stopped.is_set() and not self._stop

    def _parallel_chapter(self, course: dict, points: list[dict], index: int,
                          point: dict, total: int, cid: str, uid: "str | None",
                          degraded: threading.Event, stopped: threading.Event) -> None:
        """并发 worker 里的单章处理（分支语义与串行版本保持一致）。"""
        pid = str(point["id"])
        self._emit(self.CHAPTER, cid, index, total, point["title"])
        # 视频日志的章节上下文按线程隔离（见 api.base.Chaoxing.video_context），
        # 并发处理多章时各章的「章节 x/y」不会互相覆盖。
        self._set_video_context(point, index + 1, total)

        if point.get("has_finished"):
            if uid and not self.cache.is_done(uid, cid, pid):
                self.cache.set_done(uid, cid, pid)
            logger.info(f"章节已完成: {point['title']}")
            self._emit(self.PROGRESS, cid, pid, "done")
            return
        if uid and self.cache.is_done(uid, cid, pid):
            logger.warning(f"本地记录已完成但服务端仍显示未完成，重新作答: {point['title']}")
            self.cache.set_done(uid, cid, pid, False)
            self._emit(self.PROGRESS, cid, pid, "pending")

        result = self._process_chapter(course, point)

        if result == ChapterResult.STOPPED:
            self._emit(self.PROGRESS, cid, pid, "pending")
            stopped.set()
            return
        if result == ChapterResult.SUCCESS:
            point["has_finished"] = True
            if uid:
                self.cache.set_done(uid, cid, pid)
            self._emit(self.PROGRESS, cid, pid, "done")
            logger.info(f"章节完成: {point['title']}")
            return
        if result == ChapterResult.NOT_OPEN:
            # 降级信号：不再并发，交给串行版本按原顺序等解锁 / 回退重做上一章
            self._emit(self.PROGRESS, cid, pid, "pending")
            degraded.set()
            return
        if result == ChapterResult.NO_RETRY:
            # 平台阻断提交（图形验证码 / 重做次数用尽）：验证码是会话级的，
            # 后面章节一样会被拦 —— 标记后降级串行，让串行版本结束本课程。
            point["work_refused"] = True
            reason = getattr(self.chaoxing, "work_refused_reason", "") or "平台阻断提交"
            logger.error(f"平台阻断提交（{reason}），本章无法完成: {point['title']}")
            self._emit(self.PROGRESS, cid, pid, "error")
            self._emit(self.STATUS, "blocked", f"平台阻断提交（{reason}）: {point['title']}")
            degraded.set()
            return
        if result == ChapterResult.UNVERIFIED:
            logger.error(f"章节仍未通过平台校验（需重新作答），保留为未完成: {point['title']}")
            self._emit(self.PROGRESS, cid, pid, "error")
            return

        # ERROR：与串行一致，最多重试 3 次，仍失败则跳过本章
        tries = 1
        while tries < 3 and not (self._stop or stopped.is_set() or degraded.is_set()):
            logger.warning(f"章节处理出错，重试({tries}/3): {point['title']}")
            if cancel.wait(2):
                stopped.set()
                return
            again = self._process_chapter(course, point)
            if again == ChapterResult.STOPPED:
                stopped.set()
                return
            if again == ChapterResult.SUCCESS:
                point["has_finished"] = True
                if uid:
                    self.cache.set_done(uid, cid, pid)
                self._emit(self.PROGRESS, cid, pid, "done")
                logger.info(f"章节完成: {point['title']}")
                return
            if again == ChapterResult.NOT_OPEN:
                self._emit(self.PROGRESS, cid, pid, "pending")
                degraded.set()
                return
            if again == ChapterResult.NO_RETRY:
                point["work_refused"] = True
                self._emit(self.PROGRESS, cid, pid, "error")
                return
            if again == ChapterResult.UNVERIFIED:
                self._emit(self.PROGRESS, cid, pid, "error")
                return
            tries += 1

        if not (self._stop or stopped.is_set()):
            logger.error(f"章节多次失败，跳过: {point['title']}")
            self._emit(self.PROGRESS, cid, pid, "error")

    def _handle_not_open(self, course: dict, points: list[dict], i: int,
                         cid: str, uid: str) -> Optional[bool]:
        """
        处理「章节未开放」，返回三态：

          * ``True``  —— 章节已开放（外层保持在 i 重新处理本章）；
          * ``False`` —— 用户停止 / 软件关闭（外层结束本课程）；
          * ``None``  —— **无解**：自动重答过不了，且 manual_wait 秒内用户也没手动做完，
            本章永远不会解锁。外层必须结束本课程，绝不能再 15 秒一轮地空转。

        分支：
          - 若存在上一章节且其未完成 -> 回过头重新完成上一章节（见 _redo_prev_until_open）。
            完成后检查当前章节是否已解锁：已解锁返回 True，否则转入轮询。
          - 否则（上一章节已完成，或本就是首章）-> 每 poll_interval 秒检查当前章节状态，
            直到开放（返回 True）或软件关闭/停止（返回 False）。
        """
        # 0) 前置短路：本章之前任意一章已被判定「重答也过不了」（平台拒绝重答 /
        #    重答无进展），解锁链已断，等下去毫无意义。
        #    这是「每 15 秒检查一次开放状态」变成死循环的根因修复点。
        blocked = [p for p in points[:i] if p.get("work_refused")]
        if blocked:
            last = blocked[-1]
            logger.error(
                f"前面的章节已判定无法通过（{last.get('title')}"
                f"{'：' + last['work_blocked_reason'] if last.get('work_blocked_reason') else ''}），"
                f"当前章节不会解锁，停止等待: {points[i].get('title')}"
            )
            return None

        # 1) 回退检查上一章节（以服务端状态为准；本次运行中已重做成功会置为 True）
        if i > 0 and not points[i - 1].get("has_finished"):
            return self._redo_prev_until_open(course, points, i, cid, uid)

        # 2) 上一章无问题（或本就是首章）：只能等平台解锁
        return self._poll_until_open(course, points, i)

    def _redo_prev_until_open(self, course: dict, points: list[dict], i: int,
                              cid: str, uid: str) -> Optional[bool]:
        """重做上一章节，直到当前章节解锁 / 只能轮询 / 用户停止。

        上一章被平台判「需重新作答」时本章永远不会解锁，重答是唯一出路 —— 因此每
        ``reanswer_interval`` 秒再来一轮。但**重答必须能带来新信息**，否则纯属空转：

          * **答案没变**（与上一轮完全相同）：先整批作废再试一次，仍无变化就停；
          * **一题答案都没搜到**（整份作业全靠随机）：随机过不了判及格的作业，直接停；
          * **判分没有进展**（判对题数没涨、也没拿到正确答案）连续 ``MAX_NO_PROGRESS_ROUNDS``
            轮：再答一百轮也是同一个结果，直接停；
          * **轮数上限** ``reanswer_rounds``：无论如何不超过这么多轮（平台的重做次数有限）。

        以上「停」都不再继续自动重答，改走 ``_wait_manual``：给用户 ``manual_wait`` 秒
        （默认 300）手动在浏览器里做完这一章，期间每 ``poll_interval`` 秒回读服务端进度；
        真做完了就自动继续刷（返回 True / 转轮询），到点仍无变化才判无解（返回 None，
        外层结束本课程）。平台明确拒绝重答（``ChapterResult.NO_RETRY``）同理。

        只有一种情况值得轮询：上一章已经重做通过、但老师还没开放下一章 —— 那是
        「等时间」，不是「等自己」。
        """
        prev = points[i - 1]
        title = prev["title"]
        cur_title = points[i]["title"]
        error_tries = max(1, int(self.work_retry or 1))    # 非答题类报错的重试上限
        max_rounds = max(1, int(self.reanswer_rounds or DEFAULT_REANSWER_ROUNDS))
        errors = 0
        attempt = 0
        # 上一轮提交的答案指纹：两轮答案完全一样说明重答没有产生任何新信息，
        # 再问平台一万次也还是同一个结果 —— 必须停下来，不能真的「无限重答」。
        last_digest = ""
        # 连续「提交的答案一模一样」的轮数：第 1 次先作废这批答案再重答，
        # 超过 MAX_SAME_ANSWER_ROUNDS 仍换不出新答案就停止重答。
        repeats = 0
        # 连续「判分没有进展」的轮数与历史最好判对数
        no_progress = 0
        best_right = 0
        while not self._stop:
            attempt += 1
            if attempt > max_rounds:
                return self._wait_manual(
                    course, points, i, prev, title, cur_title,
                    f"已重答 {max_rounds} 轮仍未通过平台校验（上限 reanswer_rounds={max_rounds}）",
                )
            if attempt == 1:
                logger.info(f"章节未开放，回退检查上一章节未完成，重新完成: {title}")
                self._emit(self.STATUS, "backtrack", f"回退重做: {title}")
            else:
                logger.warning(f"上一章节仍未通过平台校验，继续重答(第 {attempt} 轮): {title}")
            self._set_video_context(prev, i, len(points))

            r = self._process_chapter(course, prev)

            if r == ChapterResult.STOPPED:
                return False

            if r == ChapterResult.NO_RETRY:
                # 平台阻断提交（图形验证码 / 重做次数用尽）：自动重答已经走不通了。
                # 但用户在浏览器里过掉验证码、手动做完这一章完全可行 —— 因此不立刻判死，
                # 先等 manual_wait 秒（每 poll_interval 秒回读一次进度），仍无变化才结束本课程。
                reason = getattr(self.chaoxing, "work_refused_reason", "") or "平台阻断提交"
                self._emit(self.STATUS, "blocked", f"平台阻断提交，已停止重做: {title}")
                logger.error(f"上一章节提交被平台阻断（{reason}），自动重答无法继续: {title}")
                logger.error(CAPTCHA_HINT)
                return self._wait_manual(course, points, i, prev, title, cur_title, reason)

            if r == ChapterResult.SUCCESS:
                prev["has_finished"] = True
                self.cache.mark_done(uid, cid, str(prev["id"]))
                self._emit(self.PROGRESS, cid, str(prev["id"]), "done")
                logger.info(f"上一章节重新完成: {title}，检查当前章节是否解锁")
                # 重新检查当前章节是否已开放，再决定是否继续处理当前章节
                # 开放与否看第一张卡片就够了（未开放时第 0 张就返回「章节未开放」），
                # 没必要为一次状态确认把 7 张卡片全打一遍。
                try:
                    _, job_info = self.chaoxing.get_job_list(
                        course, points[i], nums=Chaoxing.CARD_NUMS_FIRST)
                except Exception:  # noqa: BLE001
                    logger.warning("检查章节开放状态失败，转为轮询当前章节")
                    job_info = {"notOpen": True}
                if _job_unreadable(job_info):
                    # 读不到任务点，无法确认已解锁：按仍未开放处理，继续轮询
                    logger.warning("检查章节开放状态失败（读不到任务点），转为轮询当前章节")
                    job_info = {"notOpen": True}
                if not job_info.get("notOpen", False):
                    logger.info(f"当前章节已解锁: {cur_title}")
                    return True
                logger.info(f"上一章节已完成但当前章节仍被锁，转为轮询: {cur_title}")
                break

            if r == ChapterResult.NOT_OPEN:
                logger.warning(f"上一章节也处于未开放状态，转为轮询当前章节: {title}")
                break

            if r == ChapterResult.UNVERIFIED:
                # 答题不合格：本章永远解不了锁，只能继续重答 —— 但只有「重答真的带来
                # 新信息」才有意义。下面几种情况直接停（且不再轮询，见 _stop_blocked）。
                digest = str(getattr(self.chaoxing, "last_submitted_digest", "") or "")
                stats = _verdict_stats(self.chaoxing)
                try:
                    right = int(stats.get("right") or 0)
                    fixed = int(stats.get("fixed") or 0)
                except (TypeError, ValueError):
                    right = fixed = 0

                # 1) 整份作业一题答案都没搜到（全靠随机兜底）：换不出正确答案
                if _no_answer_found(self.chaoxing):
                    return self._wait_manual(
                        course, points, i, prev, title, cur_title,
                        "上一章节作业一题答案都没搜到（全靠随机作答），重答不会通过",
                    )

                # 2) 判分没有进展：判对题数没涨、也没拿到正确答案 -> 结果不会变
                if stats:
                    if right <= best_right and not fixed:
                        no_progress += 1
                    else:
                        no_progress = 0
                    best_right = max(best_right, right)
                    if no_progress >= MAX_NO_PROGRESS_ROUNDS:
                        return self._wait_manual(
                            course, points, i, prev, title, cur_title,
                            f"上一章节连续 {no_progress} 轮判分没有进展（判对 {right} 题、"
                            f"没拿到正确答案），再重答也是同一个结果",
                        )

                # 3) 提交的答案与上一轮完全一样：同一批答案只会再判一次不合格
                if digest and digest == last_digest:
                    repeats += 1
                    if repeats > MAX_SAME_ANSWER_ROUNDS:
                        return self._wait_manual(
                            course, points, i, prev, title, cur_title,
                            f"上一章节连续 {repeats} 轮提交的答案完全一样（题库/AI 换不出"
                            f"新答案），再重答不会改变结果",
                        )
                    # 这批答案「原样交了两轮还是不过」= 已经被证明无效：
                    # 整批作废（记错题本 + 清缓存 + 删题库同款条目），逼下一轮换答案。
                    purge = getattr(self.chaoxing, "quarantine_last_submission", None)
                    if callable(purge):
                        try:
                            purge()
                        except Exception as exc:  # noqa: BLE001 - 作废失败不能中断重答
                            logger.warning(f"作废无效答案失败（继续重答）："
                                           f"{type(exc).__name__}: {exc}")
                    self._emit(self.STATUS, "reanswer", f"作废无效答案后重答: {title}")
                    if cancel.wait(self.reanswer_interval):
                        return False
                    continue
                repeats = 0
                last_digest = digest or last_digest
                logger.warning(
                    f"上一章节答题未通过平台校验（本章因而未开放），继续重答(第 {attempt} 轮): {title}"
                )
                self._emit(self.STATUS, "reanswer", f"答题不合格重答: {title}")
                if cancel.wait(self.reanswer_interval):
                    return False
                continue

            # ERROR：可能只是一次性网络抖动，重试几轮仍失败就退回轮询，避免卡死在死链上
            errors += 1
            if errors >= error_tries:
                logger.warning(f"上一章节重做连续出错 {errors} 次，转为轮询当前章节: {title}")
                break
            logger.warning(f"上一章节重做出错({errors}/{error_tries})，稍后重试: {title}")
            self._emit(self.STATUS, "backtrack", f"回退重做出错，重试: {title}")
            if cancel.wait(self.reanswer_interval):
                return False

        return self._poll_until_open(course, points, i)

    def _wait_manual(self, course: dict, points: list[dict], i: int, prev: dict,
                     title: str, cur_title: str, reason: str) -> Optional[bool]:
        """自动重答判定「过不了」后，留出用户手动完成本章的窗口期（默认 5 分钟）。

        平台弹图形验证码 / 重做次数用尽 / 题库换不出答案时，自动重答此后怎么试都不会过，
        但**用户自己在浏览器里做完这一章**是完全可行的（尤其验证码只能人工过）。
        所以不立刻判死，而是先弹窗提示（``PROMPT`` + ``ack_manual``，用户关掉弹窗
        之后才开始计时），再每 ``poll_interval`` 秒回读一次服务端进度：

          * 上一章被手动做完了 -> 检查当前章节是否解锁：已解锁返回 True（继续刷课），
            仍锁着（老师还没放行）则转 ``_poll_until_open``；
          * 超过 ``manual_wait`` 秒仍无变化 -> 判无解（None），结束本课程。

        ``manual_wait <= 0`` 表示不等，直接判定无解（旧行为，测试/CLI 用）。
        """
        seconds = max(0, int(self.manual_wait or 0))
        if seconds <= 0:
            return self._stop_blocked(prev, title, cur_title, reason)

        logger.warning(
            f"{reason}；已停止自动重答，请到浏览器里手动完成本章《{title}》。"
            f"程序每 {self.poll_interval} 秒检查一次进度，{seconds} 秒内仍无变化将结束本课程"
        )
        # 章节行标成红色「需手动」（界面 tag=manual），提示用户去浏览器处理
        cid = str(course.get("courseId", ""))
        pid = str(prev.get("id", ""))
        self._emit(self.PROGRESS, cid, pid, "manual")
        self._emit(self.STATUS, "manual_wait",
                   f"需手动完成: {title}")
        # 先等用户看完/关掉提示弹窗，再开始计时：用户还没看到提示就开始倒计时
        # 等于白等（关窗那一刻 5 分钟可能已经耗掉大半）。
        if not self._await_manual_ack(title, cur_title, seconds):
            return False
        self._emit(self.STATUS, "manual_wait",
                   f"等待手动完成（最多 {seconds}s）: {title}")
        deadline = time.monotonic() + seconds
        while not self._stop:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            if cancel.wait(min(self.poll_interval, remain)):
                return False
            if not self._refresh_points_inplace(course, points):
                continue
            if not prev.get("has_finished"):
                logger.info(f"等待手动完成中（剩余 {remain:.0f}s）: {title}")
                continue
            # 用户手动做完了：同步缓存/界面，再看本章是否已解锁
            self.cache.mark_done(self.current_uid, cid, pid)
            self._emit(self.PROGRESS, cid, pid, "done")
            logger.info(f"检测到章节已被手动完成: {title}")
            if self._chapter_open(course, points[i]):
                logger.info(f"当前章节已解锁，继续刷课: {cur_title}")
                self._emit(self.STATUS, "polling", f"章节已开放: {cur_title}")
                return True
            logger.info(f"章节已完成但当前章节仍被锁，转为轮询: {cur_title}")
            return self._poll_until_open(course, points, i)

        if self._stop:
            return False
        return self._stop_blocked(
            prev, title, cur_title,
            f"{reason}；已等待用户手动完成 {seconds} 秒仍无进展",
        )

    def _await_manual_ack(self, title: str, cur_title: str, seconds: int) -> bool:
        """发 ``PROMPT`` 事件让界面弹窗，等用户关掉弹窗后才返回 True（开始计时）。

        期间**不回读进度、不判断超时**——用户还没看到提示就倒计时，等于白等。
        没有界面注册 ``PROMPT`` 回调时（CLI / 测试 / 自检）直接返回 True，
        保持旧的「立刻开始计时」行为。用户点「停止」返回 False。
        """
        if not self._handlers.get(self.PROMPT):
            return True
        ack = threading.Event()
        with self._lock:
            self._manual_acks.append(ack)
        try:
            self._emit(self.PROMPT, title, cur_title, seconds)
            logger.info("已弹出「需手动」提示，等待用户关闭后开始计时")
            while not self._stop:
                if ack.wait(0.2):
                    logger.info(f"用户已关闭「需手动」提示，开始 {seconds} 秒计时: {title}")
                    return True
                if cancel.is_set():
                    logger.info("等待用户确认「需手动」提示期间收到停止请求")
                    return False
            return False
        finally:
            with self._lock:
                if ack in self._manual_acks:
                    self._manual_acks.remove(ack)

    def _refresh_points_inplace(self, course: dict, points: list[dict]) -> bool:
        """从服务端回读章节完成状态并写回 ``points``（成功返回 True）。"""
        try:
            point_list = self.chaoxing.get_course_point(
                course["courseId"], course["clazzId"], course["cpi"])
        except Exception as exc:  # noqa: BLE001 - 回读失败不该中断等待
            logger.warning(f"回读章节进度失败（{type(exc).__name__}: {exc}），稍后重试")
            return False
        fresh = point_list.get("points", []) if isinstance(point_list, dict) else []
        if not fresh:
            logger.warning("回读章节进度失败（返回空列表），稍后重试")
            return False
        by_id = {str(p.get("id")): p for p in fresh}
        for p in points:
            got = by_id.get(str(p.get("id")))
            if got is not None:
                p["has_finished"] = bool(got.get("has_finished"))
        self.cache.sync_with_points(self.current_uid, str(course.get("courseId", "")), fresh)
        return True

    def _chapter_open(self, course: dict, point: dict) -> bool:
        """当前章节是否已开放（只查第一张卡片；读不到任务点按「未开放」处理）。"""
        try:
            _, job_info = self.chaoxing.get_job_list(
                course, point, nums=Chaoxing.CARD_NUMS_FIRST)
        except Exception:  # noqa: BLE001
            logger.warning("检查章节开放状态失败，视为未开放")
            return False
        if _job_unreadable(job_info):
            logger.warning("检查章节开放状态失败（读不到任务点），视为未开放")
            return False
        return not job_info.get("notOpen", False)

    def _stop_blocked(self, prev: dict, title: str, cur_title: str, reason: str) -> None:
        """停止重答并结束本课程（返回值恒为 None）。

        走到这里说明「自动重答过不了」且「手动窗口期也没等来进展」——本章注定未完成，
        当前章节正是因为上一章没过才锁着，再每 15 秒查一次开放状态就是纯空转
        （日志里那种停不下来的「章节仍未开放，继续等待」）。
        标记 ``work_refused`` 让后续章节也立刻短路，不再重做也不再轮询。
        """
        prev["work_refused"] = True
        prev["work_blocked_reason"] = reason
        self._emit(self.STATUS, "blocked", f"停止重答: {title}")
        logger.error(f"{reason}；当前章节 {cur_title} 因而无法解锁，停止刷课（不再轮询）")
        return None

    def _poll_until_open(self, course: dict, points: list[dict], i: int) -> bool:
        """轮询当前章节，每 poll_interval 秒检查一次，直到开放（True）或停止（False）。"""
        title = points[i]["title"]
        logger.info(f"章节未开放，每 {self.poll_interval} 秒检查一次开放状态: {title}")
        self._emit(self.STATUS, "polling", f"等待开放: {title}")
        while not self._stop:
            if cancel.wait(self.poll_interval):   # 可被停止按钮立刻唤醒
                return False
            try:
                # 轮询只看第一张卡片：原来每 15 秒用 7 个请求确认一个开关状态
                _, job_info = self.chaoxing.get_job_list(
                    course, points[i], nums=Chaoxing.CARD_NUMS_FIRST)
            except Exception:  # noqa: BLE001
                logger.warning("检查章节开放状态失败，稍后重试")
                continue
            if _job_unreadable(job_info):
                # 读不到任务点不等于已开放，继续等
                logger.warning("检查章节开放状态失败（读不到任务点），稍后重试")
                continue
            if not job_info.get("notOpen", False):
                logger.info(f"章节已开放: {title}")
                return True
            logger.info(f"章节仍未开放，继续等待（{self.poll_interval}s 后再次检查）")
        return False


# --------------------------------------------------------------------------- #
# 命令行（headless / 测试用）
# --------------------------------------------------------------------------- #
def _cli_main() -> None:
    import argparse
    import configparser

    parser = argparse.ArgumentParser(description="SuperStar Windows 引擎(CLI)")
    parser.add_argument("-u", "--username", default=None)
    parser.add_argument("-p", "--password", default=None)
    parser.add_argument("-c", "--config", default=None)
    parser.add_argument("-l", "--list", default=None)
    parser.add_argument("-s", "--speed", type=float, default=1.0)
    parser.add_argument("-j", "--jobs", type=int, default=1)
    args = parser.parse_args()

    common, tiku, _ = ({}, {}, {})
    if args.config:
        cfg = configparser.ConfigParser()
        cfg.read(args.config, encoding="utf8")
        if cfg.has_section("common"):
            common = dict(cfg.items("common"))
        if cfg.has_section("tiku"):
            tiku = dict(cfg.items("tiku"))

    username = args.username or common.get("username")
    password = args.password or common.get("password")
    speed = float(args.speed if args.speed else common.get("speed", 1.0))
    jobs = int(args.jobs if args.jobs else common.get("jobs", 1))
    course_list = (
        [x.strip() for x in args.list.split(",") if x.strip()]
        if args.list else common.get("course_list")
    )

    eng = StudyEngine()
    eng.login(username, password, use_cookies=False, tiku_config=tiku)
    all_courses = eng.list_courses()
    selected = [c for c in all_courses if (not course_list or c["courseId"] in course_list)] or all_courses
    try:
        eng.start_study(selected, speed=speed, jobs=jobs)
        while eng.is_running:
            time.sleep(1)
    except KeyboardInterrupt:
        eng.stop()


if __name__ == "__main__":
    _cli_main()
