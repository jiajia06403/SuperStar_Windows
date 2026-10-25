# -*- coding: utf-8 -*-
"""
SuperStar Windows 引擎

在 SuperStar_R_Android 的 Python 核心(api/) 之上，提供：
  1. 按账号 uid 缓存完成进度；切换账号时同步切换缓存；提供手动「读取进度」接口
     以用服务端真实进度重新缓存。
     **章节是否完成一律以服务端为准**：本地缓存只是辅助记录，平台若仍要求重新作答
     （判分不合格、未提交等）会清除本地标记并重新处理，避免误判为已完成。
  2. 章节检测提交后回读任务点校验；平台判不合格时重新作答（次数由 work_retry 控制），
     重答后仍不通过则保留为未完成（ChapterResult.UNVERIFIED），绝不标记完成。
  3. 切换章节时若发现章节未开放：
       - 回过头检查上一章节是否已经完成，若未完成则重新完成；
       - 否则每 15 秒检查当前章节状态，直到开放或软件关闭。

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

from api.answer import Tiku
from api.base import Account, Chaoxing, StudyResult
from api.exceptions import LoginError
from api.logger import logger
from api.live import Live
from api.live_process import LiveProcessor
from account_store import AccountStore
from paths import data_dir as _data_dir

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


# --------------------------------------------------------------------------- #
# 单任务点处理（来自原 main.py，保持核心逻辑不变）
# --------------------------------------------------------------------------- #
def process_job(chaoxing: Chaoxing, course: dict, job: dict, job_info: dict, speed: float) -> StudyResult:
    if job["type"] == "video":
        logger.trace(f"识别到视频任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        video_result = chaoxing.study_video(course, job, job_info, _speed=speed, _type="Video")
        if video_result.is_failure():
            logger.warning("当前任务非视频任务, 正在尝试音频任务解码")
            video_result = chaoxing.study_video(course, job, job_info, _speed=speed, _type="Audio")
        if video_result.is_failure():
            logger.warning(
                f"出现异常任务 -> 任务章节: {course['title']} 任务ID: {job['jobid']}, 已跳过"
            )
        return video_result
    elif job["type"] == "document":
        logger.trace(f"识别到文档任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        return chaoxing.study_document(course, job)
    elif job["type"] == "workid":
        logger.trace(f"识别到章节检测任务, 任务章节: {course['title']}")
        return chaoxing.study_work(course, job, job_info)
    elif job["type"] == "read":
        logger.trace(f"识别到阅读任务, 任务章节: {course['title']}")
        return chaoxing.study_read(course, job, job_info)
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
            thread.join()
            return StudyResult.SUCCESS
        except Exception as e:  # noqa: BLE001
            logger.error(f"处理直播任务时出错: {str(e)}")
            return StudyResult.ERROR

    logger.error(f"未知任务类型: {job['type']}")
    return StudyResult.ERROR


def _tiku_disabled(chaoxing: Chaoxing) -> bool:
    """题库是否停用（未配置题库时无法答题，此时不做回读重答以免白耗平台次数）。"""
    tiku = getattr(chaoxing, "tiku", None)
    return tiku is None or bool(getattr(tiku, "DISABLE", False))


def _work_still_pending(chaoxing: Chaoxing, course: dict, point: dict) -> Optional[bool]:
    """提交答题后回读章节任务点，判断章节检测是否仍未通过。

    超星的任务点卡片只有在 ``isPassed`` 为真时才被解析器跳过，因此回读到的 job 列表里
    若仍存在 ``workid`` 任务，说明平台仍要求「重新作答」（判分不合格 / 未提交）。

    返回 True=仍需重做，False=已通过，None=无法判断（网络/解析异常，不阻断流程）。
    """
    try:
        jobs, job_info = chaoxing.get_job_list(course, point)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"回读章节任务点失败，跳过完成校验: {e}")
        return None
    if not isinstance(jobs, list):
        return None
    if isinstance(job_info, dict) and job_info.get("notOpen", False):
        # 章节突然变回未开放，属于异常情况，不参与完成校验
        return None
    return any(j.get("type") == "workid" for j in jobs)


def process_chapter(chaoxing: Chaoxing, course: dict, point: dict, speed: float,
                    work_retry: int = 1) -> ChapterResult:
    """处理单个章节，返回 ChapterResult。

    ``work_retry``：章节检测提交后回读发现「仍未通过」时允许的重新作答次数。
    平台判分不合格（需要重新作答）时不会再被当作已完成。
    """
    logger.info(f'当前章节: {point["title"]}')
    if point.get("has_finished"):
        logger.info(f'章节：{point["title"]} 已完成所有任务点')
        return ChapterResult.SUCCESS

    try:
        work_retry = max(0, int(work_retry))
    except (TypeError, ValueError):
        work_retry = 1

    reanswer = 0
    while True:
        chaoxing.rate_limiter.limit_rate(random_time=True, random_min=0, random_max=0.2)

        jobs, job_info = chaoxing.get_job_list(course, point)

        # 发现未开放章节
        if job_info.get("notOpen", False):
            return ChapterResult.NOT_OPEN

        job_results: list[StudyResult] = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            for result in executor.map(lambda j: process_job(chaoxing, course, j, job_info, speed), jobs):
                job_results.append(result)

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
        pending = _work_still_pending(chaoxing, course, point)
        if pending is None:
            logger.warning(f"未能回读章节检测状态，暂按已完成处理: {point['title']}")
            return ChapterResult.SUCCESS
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
        time.sleep(min(3.0, 1.0 * reanswer))


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
            f"已从服务端读取进度并重新对齐缓存（uid={uid}, 课程={course['title']}, "
            f"完成 {sum(1 for p in points if p.get('has_finished'))}/{len(points)} 章）"
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
    FINISHED = "finished"          # ()

    def __init__(self, cache_dir: "str | None" = None, poll_interval: int = 15):
        cache_dir = str(cache_dir or _data_dir())
        self.cache = ProgressCache(cache_dir)
        self.poll_interval = poll_interval
        self.chaoxing: Optional[Chaoxing] = None
        self.current_uid: Optional[str] = None
        self.accounts: dict[str, dict] = {}      # uid -> {username, password, use_cookies}
        self._store = AccountStore(Path(cache_dir) / "accounts.json")
        self.accounts = self._store.load()       # 启动即恢复已保存账号
        self._handlers: dict[str, list[Callable]] = {}
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        # 运行状态
        self.speed = 1.0
        self.jobs = 1
        self.query_delay = 0
        self.work_retry = 1     # 章节检测回读不合格时的重新作答次数

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
        return Chaoxing(account=account, tiku=tiku, query_delay=self.query_delay)

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
        logger.info(f"登录成功，uid={uid}，已切换进度缓存")
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

    def login_demo(self) -> str:
        """演示登录：加载本地假后端 DemoChaoxing，无需真实账号/网络。

        用于本地预览、GUI 联调与自检验证（仅测试包内置该后端）。行为与真实 login 一致：
        建立 chaoxing、设置 current_uid、登记账号、切换进度缓存并上报事件。
        """
        from demo_chaoxing import DemoChaoxing
        self.chaoxing = DemoChaoxing()
        self.current_uid = self.chaoxing.get_uid()
        with self._lock:
            self.accounts[self.current_uid] = {
                "username": "demo", "password": "", "use_cookies": False,
                "persist": False,   # 演示账号不写入账号文件
            }
            self.cache.load(self.current_uid)
        logger.info(f"演示登录成功，uid={self.current_uid}，已切换进度缓存")
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

    def read_progress(self, course: dict) -> list[dict]:
        """手动读取进度按钮：从服务端重新读取并对齐该课程进度。"""
        assert self.chaoxing is not None, "请先登录"
        points = self.cache.read_progress_from_server(
            self.chaoxing, course, uid=self.current_uid
        )
        # 上报进度变化，让界面刷新
        cid = str(course["courseId"])
        for p in points:
            self._emit(self.PROGRESS, cid, str(p["id"]),
                       "done" if self.cache.is_done(self.current_uid, cid, str(p["id"])) else "pending")
        self._emit(self.STATUS, "read_progress", f"课程={course['title']}")
        return points

    # ---- 启动 / 停止 ----
    def start_study(self, course_list: list[dict], speed: float = 1.0, jobs: int = 1) -> None:
        assert self.chaoxing is not None, "请先登录"
        self.speed = min(2.0, max(1.0, speed))
        self.jobs = max(1, jobs)
        self._stop = False
        self._thread = threading.Thread(
            target=self._run, args=(course_list,), daemon=True
        )
        self._thread.start()
        self._emit(self.STATUS, "running", "开始刷课")

    def stop(self) -> None:
        self._stop = True
        self._emit(self.STATUS, "stopping", "正在停止...")

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- 主循环 ----
    def _run(self, course_list: list[dict]) -> None:
        try:
            for course in course_list:
                if self._stop:
                    break
                self._study_course(course)
            if self.current_uid:
                self.cache.flush(self.current_uid)
            self._emit(self.FINISHED)
            self._emit(self.STATUS, "idle", "全部完成" if not self._stop else "已停止")
        except Exception as e:  # noqa: BLE001
            logger.error(f"学习线程异常: {type(e).__name__}: {e}")
            logger.error(traceback.format_exc())
            self._emit(self.STATUS, "error", f"{type(e).__name__}: {e}")
        finally:
            self._stop = False

    def _study_course(self, course: dict) -> None:
        uid = self.current_uid
        cid = str(course["courseId"])
        point_list = self.chaoxing.get_course_point(
            course["courseId"], course["clazzId"], course["cpi"]
        )
        points = point_list.get("points", [])
        total = len(points)
        logger.info(f"开始学习课程: {course['title']}（共 {total} 个章节）")

        i = 0
        max_tries = 3
        while i < total:
            if self._stop:
                logger.info("收到停止信号，结束当前课程")
                return

            point = points[i]
            self._emit(self.CHAPTER, cid, i, total, point["title"])

            pid = str(point["id"])

            # 以服务端状态为准：平台显示已完成才跳过。
            # 本地缓存只是「上次以为跑完了」的记录，平台若仍要求重新作答（判分不合格、
            # 未提交等），绝不能据此跳过，否则就会出现「明明要重做却判断已完成」。
            if point.get("has_finished"):
                if not self.cache.is_done(uid, cid, pid):
                    self.cache.mark_done(uid, cid, pid)
                logger.info(f"章节已完成(服务端): {point['title']}")
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

            result = process_chapter(self.chaoxing, course, point, self.speed,
                                     work_retry=self.work_retry)

            if result == ChapterResult.SUCCESS:
                point["has_finished"] = True
                self.cache.mark_done(uid, cid, pid)
                self._emit(self.PROGRESS, cid, pid, "done")
                logger.info(f"章节完成: {point['title']}")
                i += 1
                continue

            if result == ChapterResult.NOT_OPEN:
                opened = self._handle_not_open(course, points, i, cid, uid)
                if opened:
                    # 章节已开放，保持在 i 重新处理本章
                    continue
                # 软件关闭 / 停止
                logger.info("章节未开放且已停止轮询，结束当前课程")
                return

            if result == ChapterResult.UNVERIFIED:
                # 任务点已提交但平台仍判未通过（需重新作答）。
                # 不标记完成、不盲目无限重试（避免耗尽平台答题次数），继续下一章。
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
                time.sleep(2)
                r = process_chapter(self.chaoxing, course, point, self.speed,
                                    work_retry=self.work_retry)
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

    def _handle_not_open(self, course: dict, points: list[dict], i: int,
                         cid: str, uid: str) -> bool:
        """
        处理「章节未开放」：
          - 若存在上一章节且其未完成 -> 回过头重新完成上一章节，完成后检查当前章节
            是否已解锁：已解锁则继续处理当前章节(返回 True)，否则转入轮询。
          - 否则（上一章节已完成，或本就是首章）-> 每 poll_interval 秒检查当前章节状态，
            直到开放（返回 True）或软件关闭/停止（返回 False）。
        """
        # 1) 回退检查上一章节（以服务端状态为准；本次运行中已重做成功会置为 True）
        if i > 0:
            prev = points[i - 1]
            if not prev.get("has_finished"):
                logger.info(f"章节未开放，回退检查上一章节未完成，重新完成: {prev['title']}")
                self._emit(self.STATUS, "backtrack", f"回退重做: {prev['title']}")
                r = process_chapter(self.chaoxing, course, prev, self.speed,
                                    work_retry=self.work_retry)
                if r == ChapterResult.SUCCESS:
                    prev["has_finished"] = True
                    self.cache.mark_done(uid, cid, str(prev["id"]))
                    self._emit(self.PROGRESS, cid, str(prev["id"]), "done")
                    logger.info(f"上一章节重新完成: {prev['title']}，检查当前章节是否解锁")
                    # 重新检查当前章节是否已开放，再决定是否继续处理当前章节
                    try:
                        _, job_info = self.chaoxing.get_job_list(course, points[i])
                    except Exception:  # noqa: BLE001
                        logger.warning("检查章节开放状态失败，转为轮询当前章节")
                        job_info = {"notOpen": True}
                    if not job_info.get("notOpen", False):
                        logger.info(f"当前章节已解锁: {points[i]['title']}")
                        return True
                    # 仍被锁 -> 落到下面的轮询逻辑
                elif r == ChapterResult.NOT_OPEN:
                    logger.warning(f"上一章节也处于未开放状态，转为轮询当前章节: {prev['title']}")
                    # 落到下面的轮询逻辑
                else:
                    logger.warning(f"上一章节重做出错: {prev['title']}，转为轮询当前章节")
                    # 落到下面的轮询逻辑

        # 2) 轮询当前章节，每 poll_interval 秒检查一次，直到开放或软件关闭
        logger.info(f"章节未开放，每 {self.poll_interval} 秒检查一次开放状态: {points[i]['title']}")
        self._emit(self.STATUS, "polling", f"等待开放: {points[i]['title']}")
        while not self._stop:
            time.sleep(self.poll_interval)
            if self._stop:
                return False
            try:
                _, job_info = self.chaoxing.get_job_list(course, points[i])
            except Exception:  # noqa: BLE001
                logger.warning("检查章节开放状态失败，稍后重试")
                continue
            if not job_info.get("notOpen", False):
                logger.info(f"章节已开放: {points[i]['title']}")
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
