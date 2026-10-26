# -*- coding: utf-8 -*-
import functools
import os
import random
import re
import sys
import threading
import time
from enum import Enum
from hashlib import md5
from typing import Self, Optional, Literal

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
from api.exceptions import MaxRetryExceeded
from api import cancel, local_bank, work_verify


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


class SessionManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        self._session = requests.Session()
        self._session.mount("https://", HTTPAdapter(max_retries=10))
        self._session.mount("http://", HTTPAdapter(max_retries=10))
        self._session.request = functools.partial(self._session.request, timeout=5)
        # For debug purposes
        # self._session.verify=False
        self._session.headers.clear()
        self._session.headers.update(gc.HEADERS)
        self._session.cookies.update(use_cookies())

    @classmethod
    def get_instance(cls) -> Self:
        return cls()

    @classmethod
    def get_session(cls) -> requests.Session:
        instance = cls.get_instance()
        return instance._session

    @classmethod
    def update_cookies(cls):
        cls.get_instance()._session.cookies.update(use_cookies())


class Account:
    username = None
    password = None
    last_login = None
    isSuccess = None

    def __init__(self, _username, _password):
        self.username = _username
        self.password = _password


class RateLimiter:
    def __init__(self, call_interval):
        self.last_call = time.time()
        self.lock = threading.Lock()
        self.call_interval = call_interval

    def limit_rate(self, random_time=False, random_min=0.0, random_max=1.0):
        with self.lock:
            if random_time:
                wait_time = random.uniform(random_min, random_max)
                time.sleep(wait_time)
            now = time.time()
            time_elapsed = now - self.last_call
            if time_elapsed <= self.call_interval:
                time.sleep(self.call_interval - time_elapsed)
                self.last_call = time.time()
                return

            self.last_call = now
            return


class StudyResult(Enum):
    SUCCESS = 0
    FORBIDDEN = 1  # 403
    ERROR = 2
    TIMEOUT = 3
    # 已保存但未提交（题库覆盖率不足），平台侧仍需「重新作答」，
    # 因此不能视为本章已完成，交由上层重试后再提交。
    UNSUBMITTED = 4

    def is_success(self):
        return self == StudyResult.SUCCESS
    def is_failure(self):
        return self != StudyResult.SUCCESS

class Chaoxing:
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
        # 当前章节上下文（引擎在进入章节前写入）：章节序号/总数/名称，供视频日志显示。
        self.video_context: dict = {}

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
                     stop_early: bool = False) -> tuple[list[dict], dict]:
        """读取章节任务点。

        ``stop_early``：一遇到空卡片就停（采集题库时用；整章刷课仍走原来的
        「凑满 7 张卡片」逻辑，避免漏掉多卡片章节的任务点）。
        """
        _session = SessionManager.get_session()
        self.rate_limiter.limit_rate()
        job_list = []
        job_info = {}
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
        for _possible_num in "0123456":

            logger.trace("开始读取章节所有任务点...")

            cards_params.update({"num": _possible_num})
            _resp = _session.get("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards", params=cards_params)
            if _resp.status_code != 200:
                logger.error(f"未知错误: {_resp.status_code} 正在跳过")
                logger.error(_resp.text)
                return [], {}

            _job_list, _job_info = decode_course_card(_resp.text)
            if _job_info.get("notOpen", False):
                # 直接返回, 节省一次请求
                logger.info("该章节未开放")
                return [], _job_info

            if stop_early and not _job_list and _possible_num != "0":
                # 卡片序号从 0 连续排列：第 1 张之后还空就说明没有更多卡片了
                break

            job_list += _job_list
            job_info.update(_job_info)

        if not job_list:
            self.study_emptypage(course, point)
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
        _session = SessionManager.get_session()

        headers = gc.VIDEO_HEADERS if _type == "Video" else gc.AUDIO_HEADERS
        _info_url = f"https://mooc1.chaoxing.com/ananas/status/{_job['objectid']}?k={self.get_fid()}&flag=normal"
        _video_info = _session.get(_info_url, headers=headers).json()

        if _video_info["status"] != "success":
            logger.error(f"Unknown status: {_video_info['status']}")
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
            passed, state = self.video_progress_log(_session, _course, _job, _job_info, _dtoken, duration, play_time, _type,headers=headers)
            passed, state = self.video_progress_log(_session, _course, _job, _job_info, _dtoken, duration, duration, _type, headers=headers)

            if passed:
                logger.info("任务瞬间完成: {}", _job['name'])
                return StudyResult.SUCCESS

            if status_interval > 0:      # 开播先报一条，立刻能看到章节归属与续播进度
                self._log_video_status(_job, play_time, duration, _speed)

            while not passed:
                # 停止按钮必须能中断视频循环：一个视频动辄几十分钟，
                # 只在章节边界检查会让界面长时间卡在「停止中」。
                cancel.check()
                # Sometimes the last request needs to be sent several times to complete the task
                if play_time - last_log_time >= wait_time or play_time == duration:

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
        # 查询时优先查这门课的文件，入库也写进这门课的文件
        local_bank.set_scope(
            course=str(_course.get("title") or "") if isinstance(_course, dict) else "",
            course_id=str(_course.get("courseId") or "") if isinstance(_course, dict) else "",
        )
        _ORIGIN_HTML_CONTENT = ""  # 用于配合输出网页源码, 帮助修复#391错误

        def random_answer(options: str) -> str:
            answer = ""
            if not options:
                return answer

            if q["type"] == "multiple":
                logger.debug(f"当前选项列表[cut前] -> {options}")
                _op_list = multi_cut(options)
                logger.debug(f"当前选项列表[cut后] -> {_op_list}")

                if not _op_list:
                    logger.error(
                        "选项为空, 未能正确提取题目选项信息! 请反馈并提供以上信息"
                    )
                    return answer

                available_options = len(_op_list)
                select_count = 0

                # 根据可用选项数量调整可能选择的选项数
                if available_options <= 1:
                    select_count = available_options
                else:
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

                    select_count = random.choices(possible_counts, weights=weights, k=1)[0]

                selected_options = random.sample(_op_list, select_count) if select_count > 0 else []

                for option in selected_options:
                    answer += option[:1]  # 取首字为答案，例如A或B

                answer = "".join(sorted(answer))
            elif q["type"] == "single":
                answer = random.choice(options.split("\n"))[:1]  # 取首字为答案, 例如A或B
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
            res = self.tiku.query(q)
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

        res = _session.post(
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
        )
        action = "提交" if questions["pyFlag"] == "" else "保存"
        if res.status_code == 200:
            res_json = res.json()
            if res_json["status"]:
                # 面板只看这一条汇总；逐题的「题目 -> 答案」明细在 debug 级别
                logger.info(f'章节检测已{action}（共 {total_questions} 题，'
                            f'题库命中 {found_answers} 题）')
            else:
                logger.error(f'{action}答题失败 -> {res_json["msg"]}')
                return StudyResult.ERROR
        else:
            logger.error(f'{action}答题失败 -> {res.text}')
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
            # 提交成功后只补「题库里还没有正确答案」的题：判对存本次答案、
            # 判错但拿得到正确答案的存正确答案（写进 <课程ID>.json，重做/换账号直接用）
            self._learn_work_answers(_course, submitted, work_params)
        return StudyResult.SUCCESS

    def _learn_work_answers(self, course, submitted: list, work_params: dict) -> None:
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
            verdicts = work_verify.verify_work(SessionManager.get_session(), work_params) or None
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
            if learned:
                logger.info(f"本地题库写入 {learned} 题正确答案"
                            f"（题库原有 {known} 题命中，共 {bank.stats().get('total', 0)} 题）")
            else:
                logger.info("本次没有可写入的正确答案（判错且页面未公布答案）")
        except Exception as exc:  # noqa: BLE001 - 入库失败绝不能中断刷课
            logger.error(f"本地题库入库流程异常：{type(exc).__name__}: {exc}")

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
