# -*- coding: utf-8 -*-
# layer: slow  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""视频状态日志测试（无需账号/网络）。

覆盖：
  - 状态行内容：章节号 / 章节名 / 进度百分比 / 已播时长 / 倍速 / 预计剩余；
  - study_video 按间隔输出（真实时间计时，倍速下仍按秒数间隔）；
  - 观看进度要求（video_finish_percent）：达标即判定完成、100% 保留旧的「必须看完」；
  - 引擎把 [common] 的间隔/进度要求与当前章节上下文推给后端；
  - [common] video_log_interval / video_finish_percent 的取值校验。
"""
import io
import os
import re
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate_exc  # noqa: E402
from loguru import logger  # noqa: E402

import api.base as base  # noqa: E402
import config_store  # noqa: E402
from api.base import Chaoxing, StudyResult  # noqa: E402
from engine import StudyEngine  # noqa: E402


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    """假 session：只要 status 接口返回视频元信息即可。"""

    cookies = {}

    def __init__(self, duration=2):
        self.duration = duration

    def get(self, url, **kwargs):
        return _Resp({
            "status": "success", "dtoken": "dt", "crc": "crc", "key": "key",
            "duration": self.duration,
        })


class _FakeBackend:
    """只有属性的空壳，用于验证引擎推送。"""


# --------------------------------------------------------------------------- #
# 状态行格式
# --------------------------------------------------------------------------- #
def test_status_line_contents():
    ch = Chaoxing()
    ch.video_context = {"chapter_index": 3, "chapter_total": 12,
                        "chapter_title": "第二章 导数与微分"}
    line = ch._video_status_line({"name": "1.1 导数的定义"}, 300.0, 1000, 2.0)

    assert line.startswith("视频进度 | "), line
    assert "章节 3/12" in line, line
    assert "第二章" in line, line
    assert "30.0%" in line, line
    assert "(300/1000s)" in line, line
    assert "2x" in line, line
    # 剩余真实时间 = (1000-300)/2 = 350s
    assert "剩余约5分50秒" in line, line

    # 没有章节上下文时（CLI 直接调用）退化为不带章节号的一行
    ch.video_context = {}
    line2 = ch._video_status_line({"name": "x"}, 50, 100, 1.0)
    assert "章节" not in line2 and "50.0%" in line2, line2


# --------------------------------------------------------------------------- #
# study_video 按间隔输出
# --------------------------------------------------------------------------- #
def test_study_video_emits_status_by_interval():
    ch = Chaoxing()
    ch.video_context = {"chapter_index": 3, "chapter_total": 12,
                        "chapter_title": "第二章 导数与微分"}
    ch.video_log_interval = 0.2       # 每次循环（1s）都会命中，便于断言

    calls = {"n": 0}

    def fake_log(_self, session, course, job, job_info, dtoken, duration,
                 play_time, _type, headers=None):
        calls["n"] += 1
        return calls["n"] >= 4, 200      # 前几次「未通过」，之后通过

    lines: "list[str]" = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        with mock.patch.object(base.SessionManager, "get_session",
                               classmethod(lambda cls: _Session(duration=2))), \
             mock.patch.object(base, "VIDEO_FINISH_RECHECK_SECONDS", 0.2), \
             mock.patch.object(Chaoxing, "video_progress_log", fake_log), \
             mock.patch.object(base, "_progress_stream", lambda: io.StringIO()):
            result = ch.study_video(
                {"courseId": "c", "clazzId": "b"},
                {"objectid": "obj", "name": "1.1 导数的定义", "playTime": "0"},
                {}, _speed=2.0,
            )
    finally:
        logger.remove(sink)

    assert result == StudyResult.SUCCESS, result
    status = [line for line in lines if line.startswith("视频进度")]
    assert len(status) >= 2, f"应按间隔输出多条状态日志，实际 {status}"
    assert all("章节 3/12" in line for line in status), status
    assert all("%" in line and "剩余约" in line for line in status), status
    # 进度要真的在动：至少有一行百分比大于 0（不是每条都 0%）
    percents = [float(re.search(r"(\d+\.\d)%", line).group(1)) for line in status]
    assert max(percents) > 0, status


def test_study_video_reports_progress_once():
    """回归：开播不能连发两条进度上报 —— 第一条的结果会被第二条覆盖，白打一次请求。"""
    ch = Chaoxing()
    ch.video_log_interval = 0

    seen = []

    def fake_log(_self, session, course, job, job_info, dtoken, duration,
                 play_time, _type, headers=None):
        seen.append((duration, play_time))
        return True, 200                  # 第一次上报就通过 -> 不应再有第二条

    with mock.patch.object(base.SessionManager, "get_session",
                           classmethod(lambda cls: _Session(duration=600))), \
         mock.patch.object(Chaoxing, "video_progress_log", fake_log), \
         mock.patch.object(base, "_progress_stream", lambda: io.StringIO()):
        ch.study_video({"courseId": "c"},
                       {"objectid": "o", "name": "v", "playTime": "123"}, {})

    assert len(seen) == 1, f"开播应只上报一次，实际 {seen}"
    assert seen[0] == (600, 600), seen[0]


def test_resume_playtime_not_zeroed():
    """回归：续播进度 playTime 单位是秒，不能 //1000 归零（否则「当前观看」始终显示 0 秒）。"""
    ch = Chaoxing()
    ch.video_context = {"chapter_index": 3, "chapter_total": 12, "chapter_title": "第二章"}
    ch.video_log_interval = 0.2

    calls = {"n": 0}

    def fake_log(_self, *args, **kwargs):
        calls["n"] += 1
        return calls["n"] >= 2, 200      # 开播一次上报即通过

    lines: "list[str]" = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        with mock.patch.object(base.SessionManager, "get_session",
                               classmethod(lambda cls: _Session(duration=600))), \
             mock.patch.object(Chaoxing, "video_progress_log", fake_log), \
             mock.patch.object(base, "_progress_stream", lambda: io.StringIO()):
            # 已观看 123 秒的视频，续播进度必须如实反映
            ch.study_video({"courseId": "c"},
                           {"objectid": "o", "name": "v", "playTime": "123"}, {})
    finally:
        logger.remove(sink)

    assert any("已进行: 123s" in line for line in lines), lines
    assert any("(123/600s)" in line for line in lines), lines
    # 绝不能出现归零后的续播起点
    assert not any("(0/600s)" in line for line in lines if "已进行" not in line), lines


def test_study_video_silent_when_disabled():
    """间隔为 0 时不输出任何状态行（默认行为，避免刷屏）。"""
    ch = Chaoxing()
    ch.video_context = {"chapter_index": 1, "chapter_total": 2, "chapter_title": "第一章"}
    ch.video_log_interval = 0

    calls = {"n": 0}

    def fake_log(_self, *args, **kwargs):
        calls["n"] += 1
        return calls["n"] >= 3, 200

    lines: "list[str]" = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        with mock.patch.object(base.SessionManager, "get_session",
                               classmethod(lambda cls: _Session(duration=1))), \
             mock.patch.object(base, "VIDEO_FINISH_RECHECK_SECONDS", 0.2), \
             mock.patch.object(Chaoxing, "video_progress_log", fake_log), \
             mock.patch.object(base, "_progress_stream", lambda: io.StringIO()):
            ch.study_video({"courseId": "c"}, {"objectid": "o", "name": "v", "playTime": "0"}, {})
    finally:
        logger.remove(sink)
    assert not [line for line in lines if line.startswith("视频进度")], lines


# --------------------------------------------------------------------------- #
# playTime 单位归一（部分卡片返回毫秒）
# --------------------------------------------------------------------------- #
def test_normalize_play_time_units():
    """秒/毫秒两种单位都要认，脏数据归零而不是当成「已看完」。"""
    assert base._normalize_play_time("123", 600) == 123       # 秒
    assert base._normalize_play_time(123, 600) == 123
    # 实测：1252s 的视频卡片报 playTime=75000（= 75s 的毫秒值）
    assert base._normalize_play_time("75000", 1252) == 75
    assert base._normalize_play_time(75000, 1252) == 75
    # 毫秒单位下已看完
    assert base._normalize_play_time("1252000", 1252) == 1252
    # 服务端取整略超时长，按看完处理
    assert base._normalize_play_time("1254", 1252) == 1252
    # 脏数据绝不误判完成
    assert base._normalize_play_time("99999999", 100) == 0
    assert base._normalize_play_time(None, 100) == 0
    assert base._normalize_play_time("abc", 100) == 0


def test_study_video_normalizes_millis_playtime():
    """回归：毫秒 playTime 曾导致「已进行 75000s / 总时长 1252s」的 100% 假进度。"""
    ch = Chaoxing()
    ch.video_context = {"chapter_index": 57, "chapter_total": 156}
    ch.video_log_interval = 0.2

    calls = {"n": 0}

    def fake_log(_self, *args, **kwargs):
        calls["n"] += 1
        return calls["n"] >= 3, 200

    lines: "list[str]" = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        with mock.patch.object(base.SessionManager, "get_session",
                               classmethod(lambda cls: _Session(duration=1252))), \
             mock.patch.object(Chaoxing, "video_progress_log", fake_log), \
             mock.patch.object(base, "_progress_stream", lambda: io.StringIO()):
            ch.study_video({"courseId": "c"},
                           {"objectid": "o", "name": "2.5.1.9Word-绘制复杂表格.mp4",
                            "playTime": "75000"}, {}, _speed=50.0)
    finally:
        logger.remove(sink)

    assert any("已进行: 75s" in line for line in lines), lines
    status = [line for line in lines if line.startswith("视频进度")]
    assert status, lines
    # 首条状态行必须是真实的 6.0%，不能是 100%
    assert "6.0% (75/1252s)" in status[0], status


# --------------------------------------------------------------------------- #
# 观看进度要求（video_finish_percent）
# --------------------------------------------------------------------------- #
def test_finish_percent_normalize_and_target():
    """百分比收敛到 90~100 + 达标秒数换算：越界收敛到边界，脏数据回默认。"""
    assert base.normalize_finish_percent("90") == 90
    assert base.normalize_finish_percent(90) == 90
    assert base.normalize_finish_percent("95") == 95
    assert base.normalize_finish_percent(100) == 100
    assert base.normalize_finish_percent("") == base.DEFAULT_VIDEO_FINISH_PERCENT
    assert base.normalize_finish_percent(None) == base.DEFAULT_VIDEO_FINISH_PERCENT
    assert base.normalize_finish_percent("abc") == base.DEFAULT_VIDEO_FINISH_PERCENT
    assert base.normalize_finish_percent("0") == 90             # 下界收敛（不允许「不看也过」）
    assert base.normalize_finish_percent("-10") == 90
    assert base.normalize_finish_percent("180") == 100          # 上界收敛（= 必须看完）

    assert base._video_finish_target(600, 90) == 540
    assert base._video_finish_target(600, 100) == 600           # 100% = 旧行为
    assert base._video_finish_target(600, 95) == 570
    assert base._video_finish_target(600, 0) == 540             # 越界按 90%
    assert base._video_finish_target(3, 90) == 3                # 短时长四舍五入
    assert base._video_finish_target(0, 90) == 0                # 时长未知
    assert base._video_finish_target(600, 300) == 600           # 越界按 100%


def test_study_video_keeps_watching_when_platform_not_convinced():
    """达标（90%）后平台仍判未通过时**不得中断观看**：继续看，5 秒复查一次，直到看完或通过。"""
    ch = Chaoxing()
    ch.video_log_interval = 0
    ch.video_finish_percent = 90

    seen = []
    calls = {"n": 0}

    def fake_log(_self, session, course, job, job_info, dtoken, duration,
                 play_time, _type, headers=None):
        calls["n"] += 1
        seen.append(play_time)
        # 开播那条「满时长」上报不算数；要看到结尾后再报一次才判通过
        return calls["n"] >= 3 and play_time >= duration, 200

    lines: "list[str]" = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        with mock.patch.object(base.SessionManager, "get_session",
                               classmethod(lambda cls: _Session(duration=1000))), \
             mock.patch.object(base, "VIDEO_FINISH_RECHECK_SECONDS", 0.2), \
             mock.patch.object(Chaoxing, "video_progress_log", fake_log), \
             mock.patch.object(base, "_progress_stream", lambda: io.StringIO()):
            result = ch.study_video({"courseId": "c"},
                                    {"objectid": "o", "name": "v", "playTime": "0"},
                                    {}, _speed=910.0)
    finally:
        logger.remove(sink)

    assert result == StudyResult.SUCCESS, result
    assert any("观看进度要求: 90%" in line for line in lines), lines
    # 达标点（≥900s）上报过一次，但**没有**就此收工
    assert any(900 <= play_time < 1000 for play_time in seen), seen
    assert seen[-1] >= 1000, f"达标后应继续看到结尾，实际上报位置 {seen}"
    assert len(seen) >= 3, f"达标后应继续复查，实际只上报了 {seen}"
    assert any("观看比例已达 90%" in line and "继续观看" in line for line in lines), lines


def test_recheck_interval_is_five_seconds():
    """复查间隔就是需求里的「再多看 5 秒」（真实秒），改这个常量即改变节奏。"""
    assert base.VIDEO_FINISH_RECHECK_SECONDS == 5.0


def test_study_video_percent_100_keeps_old_behavior():
    """percent=100 时保持旧行为：播到结尾后要继续上报，直到平台返回已通过。"""
    ch = Chaoxing()
    ch.video_log_interval = 0
    ch.video_finish_percent = 100

    seen = []

    def fake_log(_self, session, course, job, job_info, dtoken, duration,
                 play_time, _type, headers=None):
        seen.append(play_time)
        return len(seen) >= 3, 200        # 结尾要报 3 次才通过

    with mock.patch.object(base.SessionManager, "get_session",
                           classmethod(lambda cls: _Session(duration=10))), \
         mock.patch.object(base, "VIDEO_FINISH_RECHECK_SECONDS", 0.2), \
         mock.patch.object(Chaoxing, "video_progress_log", fake_log), \
         mock.patch.object(base, "_progress_stream", lambda: io.StringIO()):
        result = ch.study_video({"courseId": "c"},
                                {"objectid": "o", "name": "v", "playTime": "0"},
                                {}, _speed=50.0)

    assert result == StudyResult.SUCCESS, result
    assert len(seen) == 3, f"100% 时应在结尾重复上报，实际 {seen}"
    assert all(play_time == 10 for play_time in seen), seen


def test_default_percent_matches_packaged_config():
    """代码里的默认百分比必须与随包 config_win.ini 一致（防止两处漂移）。"""
    packaged = config_store.load_default().get("common", "video_finish_percent", fallback="")
    assert packaged.strip() == str(int(base.DEFAULT_VIDEO_FINISH_PERCENT)), packaged
    assert int(packaged) == 90, "产品默认值应为 90%"


# --------------------------------------------------------------------------- #
# 引擎：配置推送 + 章节上下文
# --------------------------------------------------------------------------- #
def test_engine_pushes_interval_and_context():
    eng = StudyEngine(cache_dir=tempfile.mkdtemp())
    eng.chaoxing = _FakeBackend()

    eng.video_log_interval = 12
    eng.video_finish_percent = 95
    eng._push_video_config()
    assert eng.chaoxing.video_log_interval == 12
    assert eng.chaoxing.video_finish_percent == 95

    # 越界值推给后端前先收敛到 90~100
    eng.video_finish_percent = 50
    eng._push_video_config()
    assert eng.chaoxing.video_finish_percent == 90

    eng._set_video_context({"id": "p3", "title": "第三章 中值定理"}, 3, 9)
    assert eng.chaoxing.video_context == {
        "chapter_index": 3, "chapter_total": 9, "chapter_title": "第三章 中值定理",
    }


def test_engine_reads_interval_from_config():
    assert config_store._invalid("common", "video_log_interval", "abc") is True
    assert config_store._invalid("common", "video_log_interval", "-5") is True
    assert config_store._invalid("common", "video_log_interval", "0") is False
    assert config_store._invalid("common", "video_log_interval", "60") is False
    assert config_store._invalid("common", "video_log_interval", "") is False

    # 观看进度百分比：90~100 的整数合法，越界/非整数/非数字非法（迁移时回落默认 90）
    assert config_store._invalid("common", "video_finish_percent", "90") is False
    assert config_store._invalid("common", "video_finish_percent", "95") is False
    assert config_store._invalid("common", "video_finish_percent", "100") is False
    assert config_store._invalid("common", "video_finish_percent", "") is False
    assert config_store._invalid("common", "video_finish_percent", "89") is True
    assert config_store._invalid("common", "video_finish_percent", "101") is True
    assert config_store._invalid("common", "video_finish_percent", "-1") is True
    assert config_store._invalid("common", "video_finish_percent", "abc") is True
    assert config_store._invalid("common", "video_finish_percent", "99.5") is True

    # 引擎读配置：结果恒在 90~100 之间（脏配置也不会低于下限）
    from engine import video_finish_percent_from_config
    assert 90 <= video_finish_percent_from_config() <= 100


if __name__ == "__main__":
    cases = [
        test_status_line_contents,
        test_study_video_emits_status_by_interval,
        test_resume_playtime_not_zeroed,
        test_normalize_play_time_units,
        test_study_video_normalizes_millis_playtime,
        test_study_video_silent_when_disabled,
        test_study_video_reports_progress_once,
        test_finish_percent_normalize_and_target,
        test_study_video_keeps_watching_when_platform_not_convinced,
        test_recheck_interval_is_five_seconds,
        test_study_video_percent_100_keeps_old_behavior,
        test_default_percent_matches_packaged_config,
        test_engine_pushes_interval_and_context,
        test_engine_reads_interval_from_config,
    ]
    failed = []
    for case in cases:
        try:
            case()
            print(f"PASS  {case.__name__}")
        except AssertionError as exc:
            failed.append(case.__name__)
            print(f"FAIL  {case.__name__}: {exc}")
            annotate_exc(f"test_video_log.py::{case.__name__}")
        except Exception:  # noqa: BLE001
            failed.append(case.__name__)
            annotate_exc(f"test_video_log.py::{case.__name__}")
    if failed:
        sys.exit(1)
    sys.exit(0)
