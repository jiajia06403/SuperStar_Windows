# -*- coding: utf-8 -*-
# layer: core  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""停止按钮能否真正中断长耗时任务点（回归：点停止后界面卡在「停止中」）。

线上现象：视频任务点动辄几十分钟，停止只是在章节边界检查标记，
于是点了停止界面一直显示「停止中 · 正在停止...」，直到整段视频跑完。

覆盖：
  1. api.cancel 的语义（set/clear/check/wait）。
  2. 真实 study_video 循环：停止后应在一个轮询间隔内抛 Cancelled，而不是跑完时长。
  3. process_chapter：被中断时返回 ChapterResult.STOPPED（不当作失败/未通过）。
  4. StudyEngine.start_study/stop：停止后线程及时退出，事件为 idle「已停止」+ FINISHED(stopped=True)。
"""
import os
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate_exc  # noqa: E402

import api.base as base  # noqa: E402
from api import cancel  # noqa: E402
from api.cancel import Cancelled  # noqa: E402

import engine  # noqa: E402
from engine import ChapterResult, StudyEngine  # noqa: E402
from api.base import Chaoxing, StudyResult  # noqa: E402


# --------------------------------------------------------------------------- #
# cancel 基础语义
# --------------------------------------------------------------------------- #
def test_cancel_flag_semantics():
    cancel.clear()
    assert cancel.is_set() is False
    cancel.check()            # 未停止 -> 不抛
    cancel.set()
    assert cancel.is_set() is True
    try:
        cancel.check()
    except Cancelled:
        pass
    else:
        raise AssertionError("cancel.check() 在已停止时应抛 Cancelled")
    # wait 在停止后应立即返回 True（可中断的 sleep）
    started = time.time()
    assert cancel.wait(5) is True
    assert time.time() - started < 0.5, "停止后 wait 不该继续睡满"
    cancel.clear()
    assert cancel.wait(0.01) is False


# --------------------------------------------------------------------------- #
# 真实 study_video 循环：停止必须中断
# --------------------------------------------------------------------------- #
class _FakeResp:
    def json(self):
        # 视频信息接口：给一段 1 小时的视频，正常情况要跑满 1 小时
        return {"status": "success", "duration": 3600, "dtoken": "tok", "crc": "crc", "key": "k"}


class _FakeSession:
    def get(self, *a, **k):
        return _FakeResp()


class _VideoChaoxing(Chaoxing):
    """绕过 __init__（不联网），只留 study_video 需要的接口。"""

    def __init__(self):  # noqa: D401 - 测试桩
        pass

    def get_uid(self):
        return "9001"

    def get_fid(self):
        return "fid"

    def video_progress_log(self, *a, **k):
        # 平台始终不认这次进度 -> 循环会一直跑到时长结束
        return False, 200


def _patched_session():
    return types.SimpleNamespace(get_session=staticmethod(lambda: _FakeSession()))


def test_study_video_interrupted_by_stop():
    job = {"objectid": "o1", "playTime": "0", "name": "测试视频", "jobid": "j1"}
    course = {"courseId": "C", "clazzId": "cz", "cpi": "cp", "title": "课"}
    original = base.SessionManager
    base.SessionManager = _patched_session()
    cancel.clear()

    outcome = {}

    def run():
        try:
            _VideoChaoxing().study_video(course, job, {"notOpen": False}, _speed=1.0)
            outcome["result"] = "finished"
        except Cancelled:
            outcome["result"] = "cancelled"
        except Exception as exc:  # noqa: BLE001
            outcome["result"] = f"{type(exc).__name__}: {exc}"

    try:
        worker = threading.Thread(target=run, daemon=True)
        started = time.time()
        worker.start()
        time.sleep(0.5)
        cancel.set()                     # 相当于用户点了「停止」
        worker.join(timeout=10)
        elapsed = time.time() - started
    finally:
        base.SessionManager = original
        cancel.clear()

    assert not worker.is_alive(), "停止后 study_video 仍在跑（界面会卡在「停止中」）"
    assert outcome.get("result") == "cancelled", f"未按预期中断: {outcome}"
    assert elapsed < 8, f"中断太慢: {elapsed:.1f}s"


# --------------------------------------------------------------------------- #
# process_chapter：中断 != 失败
# --------------------------------------------------------------------------- #
class _FakeChaoxing:
    """最小后端：1 个视频任务点，process_job 会被替换成长循环。"""

    rate_limiter = types.SimpleNamespace(limit_rate=lambda **k: None)

    def __init__(self):
        self.calls = 0

    def get_job_list(self, course, point):
        self.calls += 1
        return [{"type": "video", "jobid": "j1"}], {"notOpen": False}

    def get_course_point(self, *a, **k):
        return {"points": [{"id": "p1", "title": "第1章", "has_finished": False}]}


def test_process_chapter_returns_stopped():
    fake = _FakeChaoxing()
    original = engine.process_job

    def slow_job(chaoxing, course, job, job_info, speed):
        while True:
            cancel.check()          # 与真实视频循环一致：每轮检查停止
            time.sleep(0.05)

    engine.process_job = slow_job
    cancel.clear()
    try:
        timer = threading.Timer(0.4, cancel.set)
        timer.start()
        started = time.time()
        result = engine.process_chapter(fake, {"title": "课"}, {"id": "p1", "title": "第1章"},
                                        1.0, work_retry=1)
        elapsed = time.time() - started
        timer.cancel()
    finally:
        engine.process_job = original
        cancel.clear()

    assert result == ChapterResult.STOPPED, f"应返回 STOPPED，实际 {result}"
    assert elapsed < 5, f"中断太慢: {elapsed:.1f}s"


# --------------------------------------------------------------------------- #
# 引擎层面：停止后线程及时退出、状态回到 idle
# --------------------------------------------------------------------------- #
def test_engine_stop_ends_thread_and_reports_idle():
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01)
    fake = _FakeChaoxing()
    eng.chaoxing = fake
    eng.current_uid = "9001"

    statuses, finished = [], []
    eng.on(eng.STATUS, lambda st, msg: statuses.append((st, msg)))
    eng.on(eng.FINISHED, lambda stopped=False: finished.append(stopped))

    original_point = fake.get_job_list
    fake.get_job_list = lambda course, point: (
        [{"type": "video", "jobid": "j1"}], {"notOpen": False})
    original_job = engine.process_job

    def slow_job(chaoxing, course, job, job_info, speed):
        while True:
            cancel.check()
            time.sleep(0.05)

    engine.process_job = slow_job
    try:
        eng.start_study([{"courseId": "C", "clazzId": "cz", "cpi": "cp", "title": "课"}])
        time.sleep(0.5)
        assert eng.is_running is True
        eng.stop()
        deadline = time.time() + 10
        while eng.is_running and time.time() < deadline:
            time.sleep(0.05)
        assert eng.is_running is False, "停止后学习线程没退出"
    finally:
        engine.process_job = original_job
        fake.get_job_list = original_point
        cancel.clear()

    # 等事件回调落地（emit 在工作线程内同步调用，这里只是稳妥）
    time.sleep(0.2)
    assert ("stopping", "正在停止...") in statuses
    assert ("idle", "已停止") in statuses, f"状态未回到 idle: {statuses}"
    assert finished == [True], f"FINISHED 应触发一次且带 stopped=True: {finished}"
    assert eng.last_stopped is True, "FINISHED 后应标记 last_stopped（界面据此不弹「完成」框）"


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
            annotate_exc(f"test_stop.py::{fn.__name__}")
    print(f"\n{'ALL PASS' if failed == 0 else str(failed) + ' FAILED'}")
    sys.exit(1 if failed else 0)
