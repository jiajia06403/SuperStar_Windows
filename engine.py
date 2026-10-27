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
       - **若上一章因答题不合格一直未通过（因此本章永远解不了锁），重答不设次数上限**，
         每 reanswer_interval 秒再来一轮，直到平台判通过或用户停止；
         每次重答前都会作废上轮答案的缓存（并在错题本里避开被判错的选项），
         保证提交的答案真的换了；一旦某轮答案与上轮完全相同（换不出新答案），
         说明再重答也不会有不同结果，此时停止重答转轮询；
       - 上一章已完成（或本就首章）则每 15 秒检查当前章节状态，直到开放或软件关闭。

  4. **异常隔离**：任务点 / 章节 / 课程三层各自兜底，单点网络或解析异常只会让
     该任务点（最多该章节、该课程）失败，绝不会穿透到主循环把整轮刷课结束掉：
       - ``process_job``：每种任务类型单独 try/except -> StudyResult.ERROR；
       - ``process_chapter``：except Exception -> ChapterResult.ERROR（Cancelled 优先、单独处理）；
       - ``StudyEngine._process_chapter`` / ``_run``：章节级、课程级再兜一层。
     只有用户停止（``cancel.Cancelled``）才会中断整轮。

本模块与 GUI 解耦：通过 on(event, cb) 注册回调，GUI 只负责把这些回调映射到界面。
"""
import json
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
from api.base import Account, Chaoxing, StudyResult
from api.cancel import Cancelled
from api.exceptions import LoginError
from api.logger import logger
from api.live import Live
from api.live_process import LiveProcessor
from account_store import AccountStore
from paths import data_dir as _data_dir

# 视频状态日志默认间隔（秒）；0 表示不输出。可在 config_win.ini 的 [common] 段调整。
DEFAULT_VIDEO_LOG_INTERVAL = 10

# 章节检测「不合格重答」默认次数（[tiku] work_retry 缺省时使用）
DEFAULT_WORK_RETRY = 3

# 重答时允许「提交的答案与上一轮完全相同」的连续轮数。
# 第 1 次重复：先整批作废这批答案（记错题本 + 清缓存 + 删题库同款条目）再重答；
# 超过这个次数还换不出新答案，说明题库/AI 都已经没有别的可选答案，停止重答转轮询。
MAX_SAME_ANSWER_ROUNDS = 1

# 单章内任务点并发数默认/上限（界面 1~8，config_store.COMMON_RANGES 与之对齐）
DEFAULT_JOBS = 4
MAX_JOBS = 8


def normalize_jobs(jobs) -> int:
    """把任意来源的并发数收敛到 1~MAX_JOBS（非法/缺失回落 DEFAULT_JOBS）。"""
    try:
        value = int(float(str(jobs).strip()))
    except (TypeError, ValueError):
        return DEFAULT_JOBS
    return min(MAX_JOBS, max(1, value))


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

    ``jobs``：本章内任务点的并发上限（1 = 串行）。透传自 ``StudyEngine.start_study``，
    不再硬编码；取值范围收敛见 :func:`normalize_jobs`。

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
    STATUS = "status"              # (state, message)
    ACCOUNTS = "accounts"          # (uid_list)
    FINISHED = "finished"          # (stopped: bool)；同时记录到 engine.last_stopped

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
        # 已知「未开放」章节：courseId -> {pointId}（仅供界面展示，不落盘）
        self._not_open: dict[str, set[str]] = {}
        # 运行状态
        self.speed = 2.0
        self.jobs = DEFAULT_JOBS
        self.query_delay = 0
        self.work_retry = DEFAULT_WORK_RETRY   # 单次刷课内章节检测不合格的重新作答次数
        # 视频状态日志间隔（秒，0=关闭）：观看视频时每 N 秒输出章节号/进度百分比等
        self.video_log_interval = video_log_interval_from_config()

    # ---- 事件订阅 ----
    def on(self, event: str, cb: Callable) -> None:
        self._handlers.setdefault(event, []).append(cb)

    def _emit(self, event: str, *args) -> None:
        for cb in self._handlers.get(event, []):
            try:
                cb(*args)
            except Exception:  # noqa: BLE001
                logger.debug(f"事件回调异常({event}): {traceback.format_exc()}")

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

    def _build_chaoxing(self, username: str, password: str, use_cookies: bool,
                        tiku_config: Optional[dict] = None) -> Chaoxing:
        account = Account(username, password)
        tiku = Tiku()
        tiku.config_set(tiku_config or {})
        tiku = tiku.get_tiku_from_config()
        tiku.init_tiku()
        chaoxing = Chaoxing(account=account, tiku=tiku, query_delay=self.query_delay)
        self._push_video_log(chaoxing)
        return chaoxing

    # ---- 视频状态日志 ----
    def _push_video_log(self, chaoxing=None) -> None:
        """把视频状态日志间隔写入后端实例（0=关闭）。"""
        target = chaoxing if chaoxing is not None else self.chaoxing
        if target is None:
            return
        try:
            target.video_log_interval = float(self.video_log_interval or 0)
        except (TypeError, ValueError):
            target.video_log_interval = float(DEFAULT_VIDEO_LOG_INTERVAL)

    def apply_video_log_config(self) -> float:
        """重新读取 [common] 的视频日志间隔并即时生效（设置保存后调用）。

        返回生效的间隔秒数；0 表示关闭。正在刷课时也会作用于当前视频循环。
        """
        self.video_log_interval = video_log_interval_from_config()
        self._push_video_log()
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
              tiku_config: Optional[dict] = None, remember: bool = True) -> str:
        """登录并返回当前账号 uid；同时建立/切换该 uid 的进度缓存。

        ``remember``：是否把账号（含密码，DPAPI 加密）写入本机账号文件，
        以便下次启动自动登录。False 时只记住账号名、不保存密码。
        """
        chaoxing = self._build_chaoxing(username, password, use_cookies, tiku_config)
        state = chaoxing.login(login_with_cookies=use_cookies)
        if not state.get("status"):
            raise LoginError(state.get("msg", "登录失败"))
        self._apply_work_retry(tiku_config)
        uid = chaoxing.get_uid()
        with self._lock:
            self.chaoxing = chaoxing
            self.current_uid = uid
            self.accounts[uid] = {
                "username": username,
                "password": password if remember else "",
                "use_cookies": use_cookies,
            }
            self.cache.load(uid)  # 切换缓存
        self._persist_accounts(uid)
        logger.info("登录成功")
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
            "password": acc.get("password", ""),
            "use_cookies": bool(acc.get("use_cookies", False)),
        }

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

        ``not_open_ids``：把指定章节 id 造为「未开放」，便于验证该状态的展示。
        """
        from demo_chaoxing import DemoChaoxing
        self.chaoxing = DemoChaoxing(not_open_ids=not_open_ids)
        self.current_uid = self.chaoxing.get_uid()
        self._push_video_log()
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
        刷课时由服务端判定为未开放的章节会附带 ``not_open`` 标记（界面显示「未开放」）。
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
        self._apply_not_open(course, points)
        return points

    # ---- 「未开放」章节标记 ----
    def _apply_not_open(self, course: dict, points: list[dict]) -> None:
        """把已探测到的「未开放」状态打到章节上（已完成章节不再算未开放）。"""
        locked = self._not_open.get(str(course["courseId"]), set())
        for p in points:
            pid = str(p.get("id"))
            if p.get("has_finished"):
                locked.discard(pid)
                continue
            if pid in locked:
                p["not_open"] = True

    def refresh_progress(self, course: dict, emit: bool = True) -> list[dict]:
        """从服务端读取完成状态并对齐缓存（单请求），供「读取进度」/ 勾选课程时渲染。

        ``emit=False`` 时不发逐章 PROGRESS（由调用方统一上报）。
        """
        assert self.chaoxing is not None, "请先登录"
        points = self.cache.read_progress_from_server(
            self.chaoxing, course, uid=self.current_uid
        )
        self._apply_not_open(course, points)   # 沿用上次探测结果，避免状态闪烁
        if emit:
            cid = str(course["courseId"])
            for p in points:
                self._emit(self.PROGRESS, cid, str(p["id"]), self._point_status(p))
        return points

    @staticmethod
    def _point_status(point: dict) -> str:
        """章节状态字符串：done / not_open / pending（界面按此渲染）。"""
        if point.get("has_finished"):
            return "done"
        if point.get("not_open"):
            return "not_open"
        return "pending"

    # ---- 启动 / 停止 ----
    def start_study(self, course_list: list[dict], speed: float = 2.0,
                    jobs: int = DEFAULT_JOBS) -> None:
        assert self.chaoxing is not None, "请先登录"
        self.speed = min(2.0, max(1.0, speed))
        self.jobs = normalize_jobs(jobs)
        self._stop = False
        self.last_stopped = False
        cancel.clear()          # 清掉上一轮的停止标记，否则新一轮刚开始就中断
        # 以配置文件为准刷新视频状态日志间隔（用户可能直接改过 ini），并推给后端
        self.apply_video_log_config()
        if self.video_log_interval > 0:
            # 只是运行参数说明，界面上没必要占一行，写日志文件备查即可
            logger.debug(
                f"视频状态日志：每 {self.video_log_interval:g} 秒输出一次"
                f"（章节号 / 进度百分比 / 预计剩余；设 0 可关闭）"
            )
        else:
            logger.debug("视频状态日志已关闭（在「设置」里填间隔秒数即可开启）")
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
                                   work_retry=self.work_retry, jobs=self.jobs)
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
        uid = self.current_uid
        cid = str(course["courseId"])
        point_list = self.chaoxing.get_course_point(
            course["courseId"], course["clazzId"], course["cpi"]
        )
        points = point_list.get("points", [])
        total = len(points)
        locked = self._not_open.setdefault(cid, set())
        logger.info(f"开始学习课程: {course['title']}（共 {total} 个章节）")

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
                locked.discard(pid)
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
                locked.discard(pid)
                self.cache.mark_done(uid, cid, pid)
                self._emit(self.PROGRESS, cid, pid, "done")
                logger.info(f"章节完成: {point['title']}")
                i += 1
                continue

            if result == ChapterResult.NOT_OPEN:
                # 让界面显示「未开放」而不是「待学」
                locked.add(pid)
                self._emit(self.PROGRESS, cid, pid, "not_open")
                opened = self._handle_not_open(course, points, i, cid, uid)
                if opened:
                    locked.discard(pid)
                    # 章节已开放，保持在 i 重新处理本章
                    continue
                # 软件关闭 / 停止
                logger.info("章节未开放且已停止轮询，结束当前课程")
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
                    locked.discard(pid)
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

    def _handle_not_open(self, course: dict, points: list[dict], i: int,
                         cid: str, uid: str) -> bool:
        """
        处理「章节未开放」：
          - 若存在上一章节且其未完成 -> 回过头重新完成上一章节（见 _redo_prev_until_open）。
            完成后检查当前章节是否已解锁：已解锁返回 True，否则转入轮询。
          - 否则（上一章节已完成，或本就是首章）-> 每 poll_interval 秒检查当前章节状态，
            直到开放（返回 True）或软件关闭/停止（返回 False）。
        """
        # 1) 回退检查上一章节（以服务端状态为准；本次运行中已重做成功会置为 True）
        if i > 0 and not points[i - 1].get("has_finished"):
            return self._redo_prev_until_open(course, points, i, cid, uid)

        # 2) 上一章无问题（或本就是首章）：只能等平台解锁
        return self._poll_until_open(course, points, i)

    def _redo_prev_until_open(self, course: dict, points: list[dict], i: int,
                              cid: str, uid: str) -> bool:
        """重做上一章节，直到当前章节解锁 / 只能轮询 / 用户停止。

        **答题不合格没有次数上限**：上一章被平台判「需重新作答」时，本章永远不会解锁，
        重答是唯一出路 —— 因此每 ``reanswer_interval`` 秒再来一轮。
        但**答案必须变化**才算有效重答：平台否掉这批答案后缓存已被作废、错题本会避开
        被判错的选项，所以每轮都会搜出新答案；若某一轮提交的答案与上一轮完全相同
        （题库/AI 都换不出来），那就再重答一万次也是同一个结果，此处会停止重答转轮询。
        其它原因（上一章也锁着、网络报错）不值得无限重试，退回轮询。
        """
        prev = points[i - 1]
        title = prev["title"]
        cur_title = points[i]["title"]
        error_tries = max(1, int(self.work_retry or 1))    # 非答题类报错的重试上限
        errors = 0
        attempt = 0
        # 上一轮提交的答案指纹：两轮答案完全一样说明重答没有产生任何新信息，
        # 再问平台一万次也还是同一个结果 —— 必须停下来，不能真的「无限重答」。
        last_digest = ""
        # 连续「提交的答案一模一样」的轮数：第 1 次先作废这批答案再重答，
        # 超过 MAX_SAME_ANSWER_ROUNDS 仍换不出新答案就停止重答转轮询。
        repeats = 0
        while not self._stop:
            attempt += 1
            if attempt == 1:
                logger.info(f"章节未开放，回退检查上一章节未完成，重新完成: {title}")
                self._emit(self.STATUS, "backtrack", f"回退重做: {title}")
            else:
                logger.warning(f"上一章节仍未通过平台校验，继续重答(第 {attempt} 轮): {title}")
            self._set_video_context(prev, i, len(points))

            r = self._process_chapter(course, prev)

            if r == ChapterResult.STOPPED:
                return False

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
                # 答题不合格：本章永远解不了锁，只能继续重答 —— 但只在「这一轮的答案
                # 跟上一轮不一样」时才有意义。答案没变就不可能换个结果。
                digest = str(getattr(self.chaoxing, "last_submitted_digest", "") or "")
                if digest and digest == last_digest:
                    repeats += 1
                    if repeats > MAX_SAME_ANSWER_ROUNDS:
                        logger.error(
                            f"上一章节连续 {repeats} 轮提交的答案完全一样（平台没给出可用的"
                            f"判分信息，题库/AI 也换不出新答案），再重答不会改变结果，"
                            f"停止重答并转为轮询: {title}"
                        )
                        self._emit(self.STATUS, "reanswer",
                                   f"重答答案无变化，停止重答: {title}")
                        break
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
