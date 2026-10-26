# -*- coding: utf-8 -*-
# layer: core  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""engine.py 的逻辑测试（无需网络 / 无需 GUI）。

覆盖：
  - ProgressCache：按 uid 隔离、标记/查询、读取服务端进度重新缓存、落盘。
  - 章节未开放处理逻辑 _handle_not_open：
      回退重做上一章节 / 轮询每 15 秒直到开放 / 停止时退出。
  - _study_course：跳过已缓存完成章节、正常完成、遇到未开放轮询后继续。
"""
import json
import threading
import time
import tempfile
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate, annotate_exc  # noqa: E402

import engine  # noqa: E402
from engine import StudyEngine, ProgressCache, ChapterResult  # noqa: E402
from api.base import StudyResult  # noqa: E402

# 保存原始 process_chapter：部分测试会 monkeypatch 模块级函数且不还原
_ORIG_PROCESS_CHAPTER = engine.process_chapter


def _always_ok(ch, course, point, speed, work_retry=1, **kw):
    return ChapterResult.SUCCESS


# --------------------------------------------------------------------------- #
# Fake Chaoxing
# --------------------------------------------------------------------------- #
class FakeChaoxing:
    def __init__(self, uid):
        self._uid = uid
        self.points = []
        self.job_info_seq = []   # 轮询时依次返回的 job_info

    def get_uid(self):
        return self._uid

    def get_course_point(self, *a, **k):
        return {"points": self.points}

    def get_job_list(self, course, point, **kwargs):
        if self.job_info_seq:
            return [], self.job_info_seq.pop(0)
        return [], {"notOpen": False}


# --------------------------------------------------------------------------- #
# ProgressCache
# --------------------------------------------------------------------------- #
def test_cache_isolation_and_persist():
    tmp_path = tempfile.mkdtemp()
    cache = ProgressCache(base_dir=str(tmp_path))
    cache.mark_done("1001", "c1", "p1")
    cache.mark_done("1001", "c1", "p2")
    cache.mark_done("2002", "c1", "p1")
    assert cache.is_done("1001", "c1", "p1") is True
    assert cache.is_done("1001", "c1", "p2") is True
    assert cache.is_done("1001", "c1", "p3") is False
    # 不同 uid 互不影响
    assert cache.is_done("2002", "c1", "p1") is True
    assert cache.is_done("2002", "c1", "p2") is False

    # 重新加载（模拟进程重启），磁盘文件应保持一致
    cache2 = ProgressCache(base_dir=str(tmp_path))
    assert cache2.is_done("1001", "c1", "p1") is True
    assert cache2.is_done("2002", "c1", "p1") is True


def test_read_progress_from_server():
    tmp_path = tempfile.mkdtemp()
    cache = ProgressCache(base_dir=str(tmp_path))
    fake = FakeChaoxing("555")
    fake.points = [
        {"id": "a", "title": "第1章", "has_finished": True},
        {"id": "b", "title": "第2章", "has_finished": False},
        {"id": "c", "title": "第3章", "has_finished": True},
    ]
    cache.read_progress_from_server(fake, {"courseId": "C9", "clazzId": "cz", "cpi": "cp", "title": "测试课"})
    assert cache.is_done("555", "C9", "a") is True
    assert cache.is_done("555", "C9", "b") is False
    assert cache.is_done("555", "C9", "c") is True


def test_read_progress_clears_stale_local_done():
    """服务端说未完成时必须清掉本地「已完成」标记（否则会误判为已完成而跳过）。"""
    tmp_path = tempfile.mkdtemp()
    cache = ProgressCache(base_dir=str(tmp_path))
    # 本地“以为”完成了 b、c
    cache.mark_done("666", "C9", "b")
    cache.mark_done("666", "C9", "c")
    fake = FakeChaoxing("666")
    fake.points = [
        {"id": "b", "title": "第2章", "has_finished": False},   # 平台要求重新作答
        {"id": "c", "title": "第3章", "has_finished": True},
    ]
    points = cache.read_progress_from_server(
        fake, {"courseId": "C9", "clazzId": "cz", "cpi": "cp", "title": "测试课"})
    assert cache.is_done("666", "C9", "b") is False, "服务端未完成应清除本地标记"
    assert cache.is_done("666", "C9", "c") is True
    # 返回的 points 与缓存一致（界面不会出现「刷新后状态跳变」）
    assert [p["cache_done"] for p in points] == [False, True]
    # 落盘后重新加载仍然一致
    cache2 = ProgressCache(base_dir=str(tmp_path))
    assert cache2.is_done("666", "C9", "b") is False


def test_get_course_points_syncs_cache():
    """界面读取章节时也以服务端为准对齐缓存。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01)
    fake = FakeChaoxing("667")
    eng.chaoxing = fake
    eng.current_uid = "667"
    eng.cache.mark_done("667", "C", "a")
    fake.points = [{"id": "a", "title": "第1章", "has_finished": False}]
    points = eng.get_course_points({"courseId": "C", "clazzId": "cz", "cpi": "cp", "title": "课"})
    assert points[0]["cache_done"] is False
    assert eng.cache.is_done("667", "C", "a") is False


# --------------------------------------------------------------------------- #
# _handle_not_open
# --------------------------------------------------------------------------- #
def test_backtrack_when_prev_not_done():
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01)
    eng.chaoxing = FakeChaoxing("777")
    eng.current_uid = "777"
    points = [
        {"id": "p0", "title": "上一章"},
        {"id": "p1", "title": "当前章"},
    ]
    # process_chapter 被替换为：重做上一章返回成功；当前章处理在调用方处理
    eng.__dict__  # noop
    # 模拟：重做上一章成功
    engine.process_chapter = _always_ok
    opened = eng._handle_not_open({"courseId": "C", "title": "x"}, points, i=1, cid="C", uid="777")
    # 上一章未完成 -> 重做成功 -> 重新检查当前章已解锁 -> 返回 True（外层会重新处理当前章）
    assert opened is True
    assert eng.cache.is_done("777", "C", "p0") is True


def test_reanswer_unlimited_when_quiz_failed():
    """上一章答题不合格导致本章未开放 -> 重答不设上限，直到平台判通过。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01, reanswer_interval=0.01)
    eng.current_uid = "778"
    eng.work_retry = 1
    fake = FakeChaoxing("778")
    fake.job_info_seq = [{"notOpen": False}]      # 重答成功后本章已解锁
    eng.chaoxing = fake
    points = [
        {"id": "p0", "title": "上一章"},
        {"id": "p1", "title": "当前章"},
    ]

    rounds = {"n": 0}

    def _fail_then_ok(ch, course, point, speed, work_retry=1, **kw):
        rounds["n"] += 1
        # 前 3 轮都判「不合格」，第 4 轮才通过（远超 work_retry=1 的次数）
        return ChapterResult.UNVERIFIED if rounds["n"] <= 3 else ChapterResult.SUCCESS

    engine.process_chapter = _fail_then_ok
    try:
        opened = eng._handle_not_open({"courseId": "C", "title": "x"}, points,
                                      i=1, cid="C", uid="778")
    finally:
        engine.process_chapter = _ORIG_PROCESS_CHAPTER

    assert opened is True, "重答通过且本章解锁后应返回 True"
    assert rounds["n"] == 4, f"不合格必须无限重答，实际只重答了 {rounds['n']} 轮"
    assert eng.cache.is_done("778", "C", "p0") is True


def test_poll_until_open():
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01)
    fake = FakeChaoxing("888")
    fake.job_info_seq = [{"notOpen": True}, {"notOpen": False}]  # 第一次未开放，第二次开放
    eng.chaoxing = fake
    eng.current_uid = "888"
    points = [{"id": "p0", "title": "上一章", "has_finished": True},
              {"id": "p1", "title": "当前章"}]
    eng.cache.mark_done("888", "C", "p0")  # 上一章已完成 -> 走轮询分支
    engine.process_chapter = _always_ok
    opened = eng._handle_not_open({"courseId": "C", "title": "x"}, points, i=1, cid="C", uid="888")
    assert opened is True


def test_poll_stop_when_closing():
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.05)
    fake = FakeChaoxing("999")
    fake.job_info_seq = [{"notOpen": True}] * 50
    eng.chaoxing = fake
    eng.current_uid = "999"
    points = [{"id": "p1", "title": "当前章"}]  # 首章即未开放
    eng.cache.mark_done("999", "C", "pX")        # 无上一章时直接轮询
    eng._stop = True                             # 软件关闭 / 停止
    engine.process_chapter = _always_ok
    opened = eng._handle_not_open({"courseId": "C", "title": "x"}, points, i=0, cid="C", uid="999")
    assert opened is False


# --------------------------------------------------------------------------- #
# _study_course 集成
# --------------------------------------------------------------------------- #
def test_study_course_flow():
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01)
    fake = FakeChaoxing("111")
    eng.chaoxing = fake
    eng.current_uid = "111"

    course = {"courseId": "C", "clazzId": "cz", "cpi": "cp", "title": "课"}
    fake.points = [
        {"id": "a", "title": "第1章", "has_finished": False},
        {"id": "b", "title": "第2章", "has_finished": False},
        {"id": "c", "title": "第3章(锁)", "has_finished": False},
    ]
    # 章节 c 先未开放，轮询一次后开放
    fake.job_info_seq = [{"notOpen": False}, {"notOpen": True}, {"notOpen": False}]

    # 模拟 process_chapter：a,b 成功；c 在未开放时返回 NOT_OPEN，开放后成功
    calls = {"c": 0}

    def fake_process(ch, c2, point, speed, work_retry=1, **kw):
        if point["id"] == "c":
            calls["c"] += 1
            # 第一次进入 c 时，get_job_list 仍在返回 notOpen（轮询逻辑里）
            if calls["c"] == 1:
                return ChapterResult.NOT_OPEN
            return ChapterResult.SUCCESS
        return ChapterResult.SUCCESS

    engine.process_chapter = fake_process

    # 不真正启动线程，直接调用内部方法
    eng._study_course(course)
    assert eng.cache.is_done("111", "C", "a")
    assert eng.cache.is_done("111", "C", "b")
    assert eng.cache.is_done("111", "C", "c")


def test_study_course_skip_server_finished():
    """服务端标记已完成的章节直接跳过（不再依赖本地缓存）。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01)
    fake = FakeChaoxing("223")
    eng.chaoxing = fake
    eng.current_uid = "223"
    course = {"courseId": "C", "clazzId": "cz", "cpi": "cp", "title": "课"}
    fake.points = [
        {"id": "a", "title": "已完成章节", "has_finished": True},
        {"id": "b", "title": "新章节", "has_finished": False},
    ]
    seen = []

    def fake_process(ch, c2, point, speed, work_retry=1, **kw):
        seen.append(point["id"])
        return ChapterResult.SUCCESS

    engine.process_chapter = fake_process
    eng._study_course(course)
    assert "a" not in seen          # 服务端已完成，跳过
    assert "b" in seen
    assert eng.cache.is_done("223", "C", "b")


def test_study_course_ignores_stale_cache():
    """本地缓存说完成、但服务端说未完成（判分不合格需重做）时必须重新处理。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01)
    fake = FakeChaoxing("224")
    eng.chaoxing = fake
    eng.current_uid = "224"
    course = {"courseId": "C", "clazzId": "cz", "cpi": "cp", "title": "课"}
    fake.points = [
        {"id": "a", "title": "上一轮误判完成", "has_finished": False},
        {"id": "b", "title": "新章节", "has_finished": False},
    ]
    eng.cache.mark_done("224", "C", "a")   # 本地认为已完成
    seen = []

    def fake_process(ch, c2, point, speed, work_retry=1, **kw):
        seen.append(point["id"])
        return ChapterResult.SUCCESS

    engine.process_chapter = fake_process
    eng._study_course(course)
    assert "a" in seen, "服务端未完成时必须重新处理，不能沿用本地缓存跳过"
    assert eng.cache.is_done("224", "C", "a")


# --------------------------------------------------------------------------- #
# 答题「不合格」不应被误判为已完成
# --------------------------------------------------------------------------- #
def test_study_work_low_coverage_requires_reanswer():
    """覆盖率不足时：第一次仅保存并返回 UNSUBMITTED（不能当作已完成）；
    重试（rollback_times>=1）时即便覆盖率仍不足也会提交并返回 SUCCESS。"""
    import api.base as base

    class _Resp:
        def __init__(self, status=200, text="ok", json_data=None):
            self.status_code = status
            self.text = text
            self._json = json_data or {}

        def json(self):
            return self._json

    class _Session:
        def __init__(self):
            self.posts = []

        def get(self, url, params=None, **kw):
            return _Resp(200, "ok")

        def post(self, url, data=None, **kw):
            self.posts.append(dict(data) if data else {})
            return _Resp(200, "ok", {"status": True, "msg": "ok"})

    class _Tiku:
        DISABLE = False
        COVER_RATE = 0.8

        def query(self, q):  # 一道都搜不到 -> 覆盖率 0%
            return None

        def get_submit_params(self):
            return ""

        def judgement_select(self, res):
            return True

    def make_questions():
        return {"questions": [
            {"id": 1, "type": "single", "title": "Q1", "options": "A. x\nB. y",
             "answerField": {"answer1": "", "answertype1": "0"}},
            {"id": 2, "type": "single", "title": "Q2", "options": "A. x\nB. y",
             "answerField": {"answer2": "", "answertype2": "0"}},
        ]}

    sess = _Session()
    old_get_session = base.SessionManager.get_session
    old_decode = base.decode_questions_info
    base.SessionManager.get_session = staticmethod(lambda: sess)
    base.decode_questions_info = lambda text: make_questions()
    try:
        c = base.Chaoxing(account=None, tiku=_Tiku())
        course = {"courseId": "C", "clazzId": "cz", "title": "课"}
        job = {"type": "workid", "jobid": "work-1", "enc": "e"}
        job_info = {"knowledgeid": "p1", "ktoken": "k", "cpi": "cp"}

        r1 = c.study_work(course, job, job_info)
        assert r1 == StudyResult.UNSUBMITTED, r1
        assert c.rollback_times == 1
        assert sess.posts[-1].get("pyFlag") == "1"   # 仅保存

        r2 = c.study_work(course, job, job_info)
        assert r2 == StudyResult.SUCCESS, r2
        assert c.rollback_times == 0
        assert sess.posts[-1].get("pyFlag") == ""    # 提交
    finally:
        base.SessionManager.get_session = old_get_session
        base.decode_questions_info = old_decode


def test_process_chapter_unsubmitted_is_not_done():
    """UNSUBMITTED（覆盖率不足未提交）应判为未完成(ERROR)，以便上层重新作答。"""
    class _Limiter:
        def limit_rate(self, *a, **k):
            return None

    class _Tiku:
        DISABLE = False

    class _C:
        def __init__(self):
            self.rate_limiter = _Limiter()
            self.tiku = _Tiku()

        def get_job_list(self, course, point, **kwargs):
            return ([{"type": "workid", "jobid": "work-1"}],
                    {"knowledgeid": point["id"]})

        def study_work(self, *a, **k):
            return StudyResult.UNSUBMITTED

    point = {"id": "p1", "title": "x", "has_finished": False}
    r = _ORIG_PROCESS_CHAPTER(_C(), {"courseId": "C", "title": "x"}, point, 1.0)
    assert r == ChapterResult.ERROR, r


def _work_backend(pending_rounds: int):
    """构造：章节检测一直提交成功，但平台前 N 轮仍判「需重新作答」。"""
    class _Limiter:
        def limit_rate(self, *a, **k):
            return None

    class _Tiku:
        DISABLE = False

    class _C:
        def __init__(self):
            self.rate_limiter = _Limiter()
            self.tiku = _Tiku()
            self.rounds = 0
            self.study_calls = 0

        def get_job_list(self, course, point, **kwargs):
            # 平台视角：前 pending_rounds 轮回读时作业仍未通过 -> 仍返回 workid 任务
            jobs = [] if self.rounds > pending_rounds else [{"type": "workid", "jobid": "w"}]
            return jobs, {"knowledgeid": "p1"}

        def study_work(self, *a, **k):
            self.study_calls += 1
            self.rounds += 1
            return StudyResult.SUCCESS

    return _C()


def test_process_chapter_reanswers_when_platform_rejects():
    """平台判不合格（回读仍有作业任务）时：重新作答，直到通过才 SUCCESS。"""
    c = _work_backend(pending_rounds=1)     # 第 1 次回读未通过，第 2 次通过
    point = {"id": "p1", "title": "章", "has_finished": False}
    r = _ORIG_PROCESS_CHAPTER(c, {"courseId": "C", "title": "x"}, point, 1.0, work_retry=1)
    assert r == ChapterResult.SUCCESS, r
    assert c.study_calls == 2, f"应重新作答一次，实际 {c.study_calls} 次"


def test_process_chapter_unverified_after_retry_limit():
    """重答次数用尽仍不合格 -> UNVERIFIED（绝不能返回 SUCCESS 被标记完成）。"""
    c = _work_backend(pending_rounds=99)
    point = {"id": "p1", "title": "章", "has_finished": False}
    r = _ORIG_PROCESS_CHAPTER(c, {"courseId": "C", "title": "x"}, point, 1.0, work_retry=1)
    assert r == ChapterResult.UNVERIFIED, r
    assert c.study_calls == 2      # 首次 + 1 次重答，不无限重试


def test_process_chapter_no_retry_when_tiku_disabled():
    """题库停用时不做回读重答（避免白耗平台答题次数），保持原有跳过行为。"""
    class _Limiter:
        def limit_rate(self, *a, **k):
            return None

    class _Tiku:
        DISABLE = True

    class _C:
        def __init__(self):
            self.rate_limiter = _Limiter()
            self.tiku = _Tiku()

        def get_job_list(self, course, point, **kwargs):
            return [{"type": "workid", "jobid": "w"}], {"knowledgeid": "p1"}

        def study_work(self, *a, **k):
            return StudyResult.SUCCESS

    point = {"id": "p1", "title": "章", "has_finished": False}
    r = _ORIG_PROCESS_CHAPTER(_C(), {"courseId": "C", "title": "x"}, point, 1.0, work_retry=3)
    assert r == ChapterResult.SUCCESS, r


def test_study_course_unverified_not_marked_done():
    """UNVERIFIED 章节不得写入进度缓存，且继续处理后续章节。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01)
    fake = FakeChaoxing("225")
    eng.chaoxing = fake
    eng.current_uid = "225"
    course = {"courseId": "C", "clazzId": "cz", "cpi": "cp", "title": "课"}
    fake.points = [
        {"id": "a", "title": "不合格章", "has_finished": False},
        {"id": "b", "title": "正常章", "has_finished": False},
    ]

    def fake_process(ch, c2, point, speed, work_retry=1, **kw):
        return ChapterResult.UNVERIFIED if point["id"] == "a" else ChapterResult.SUCCESS

    engine.process_chapter = fake_process
    eng._study_course(course)
    assert eng.cache.is_done("225", "C", "a") is False, "不合格章节不能记为已完成"
    assert eng.cache.is_done("225", "C", "b") is True


def test_study_course_reanswer_until_platform_passes():
    """端到端：平台首次判不合格 -> 自动重新作答 -> 回读通过后才标记完成。"""
    class _Limiter:
        def limit_rate(self, *a, **k):
            return None

    class _Tiku:
        DISABLE = False

    class _Backend:
        def __init__(self, reject_rounds):
            self.rate_limiter = _Limiter()
            self.tiku = _Tiku()
            self.reject = reject_rounds
            self.attempts = 0

        def get_uid(self):
            return "u1"

        def get_course_point(self, *a, **k):
            return {"points": [{"id": "w1", "title": "章节检测章",
                                "has_finished": self.attempts > self.reject}]}

        def get_job_list(self, course, point, **kwargs):
            if self.attempts > self.reject:
                return [], {"notOpen": False}
            return [{"type": "workid", "jobid": "w"}], {"knowledgeid": "w1"}

        def study_work(self, *a, **k):
            self.attempts += 1
            return StudyResult.SUCCESS

    engine.process_chapter = _ORIG_PROCESS_CHAPTER   # 还原真实实现（前面用例 monkeypatch 过）
    eng = StudyEngine(cache_dir=tempfile.mkdtemp(), poll_interval=0.01)
    be = _Backend(reject_rounds=1)
    eng.chaoxing = be
    eng.current_uid = "u1"
    eng.work_retry = 1
    course = {"courseId": "C", "clazzId": "cz", "cpi": "cp", "title": "课"}

    eng._study_course(course)
    assert be.attempts == 2, f"应重新作答一次（共 2 次），实际 {be.attempts} 次"
    assert eng.cache.is_done("u1", "C", "w1") is True, "回读通过后才应标记完成"


def test_study_course_rejected_keeps_pending_after_retry():
    """端到端：重答次数用尽仍不合格 -> 不算完成（下次刷课/读取进度仍为未完成）。"""
    class _Limiter:
        def limit_rate(self, *a, **k):
            return None

    class _Tiku:
        DISABLE = False

    class _Backend:
        def __init__(self):
            self.rate_limiter = _Limiter()
            self.tiku = _Tiku()
            self.attempts = 0

        def get_uid(self):
            return "u2"

        def get_course_point(self, *a, **k):
            return {"points": [{"id": "w1", "title": "章节检测章", "has_finished": False}]}

        def get_job_list(self, course, point, **kwargs):
            return [{"type": "workid", "jobid": "w"}], {"knowledgeid": "w1"}

        def study_work(self, *a, **k):
            self.attempts += 1
            return StudyResult.SUCCESS

    engine.process_chapter = _ORIG_PROCESS_CHAPTER   # 还原真实实现
    eng = StudyEngine(cache_dir=tempfile.mkdtemp(), poll_interval=0.01)
    be = _Backend()
    eng.chaoxing = be
    eng.current_uid = "u2"
    eng.work_retry = 1
    course = {"courseId": "C", "clazzId": "cz", "cpi": "cp", "title": "课"}

    eng._study_course(course)
    assert be.attempts == 2                    # 首次 + 1 次重答
    assert eng.cache.is_done("u2", "C", "w1") is False, "不合格章节不能记为已完成"


# --------------------------------------------------------------------------- #
# 并发数（界面「并发」设置）
# --------------------------------------------------------------------------- #
class _SpiedLimiter:
    def limit_rate(self, *a, **k):
        return None


class _SpiedTiku:
    DISABLE = False


class _MultiJobChaoxing:
    """一章多个任务点（非章节检测），用来观察并发行为。"""

    def __init__(self, count):
        self.rate_limiter = _SpiedLimiter()
        self.tiku = _SpiedTiku()
        self.jobs = [{"type": "video", "jobid": f"v{i}"} for i in range(count)]

    def get_job_list(self, course, point, **kwargs):
        return list(self.jobs), {"knowledgeid": point["id"]}


class _RecordingExecutor:
    """替身线程池：记录 max_workers，并在调用线程上顺序跑完（结果顺序不变）。"""

    def __init__(self, max_workers=None, **kwargs):
        _RecordingExecutor.seen.append(max_workers)
        self._max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def map(self, fn, iterable):
        return [fn(item) for item in iterable]


def test_jobs_is_forwarded_to_thread_pool():
    """界面并发数必须真的生效：StudyEngine.jobs=3 -> 线程池 max_workers=3。"""
    _RecordingExecutor.seen = []
    old_executor, old_process_job = engine.ThreadPoolExecutor, engine.process_job
    engine.process_chapter = _ORIG_PROCESS_CHAPTER
    try:
        engine.ThreadPoolExecutor = _RecordingExecutor
        done = []

        def _fake_job(ch, course, job, job_info, speed):
            done.append(job["jobid"])
            return StudyResult.SUCCESS

        engine.process_job = _fake_job

        eng = StudyEngine(cache_dir=tempfile.mkdtemp(), poll_interval=0.01)
        eng.chaoxing = _MultiJobChaoxing(6)
        eng.current_uid = "u9"
        eng.jobs = 3                       # 相当于界面上选了「并发 3」

        r = eng._process_chapter({"courseId": "C", "title": "课"},
                                 {"id": "p1", "title": "章", "has_finished": False})
        assert r == ChapterResult.SUCCESS, r
        assert done == [f"v{i}" for i in range(6)], done
        assert _RecordingExecutor.seen == [3], f"线程池上限应为 3，实际 {_RecordingExecutor.seen}"
    finally:
        engine.ThreadPoolExecutor = old_executor
        engine.process_job = old_process_job


def test_jobs_one_runs_serial():
    """并发=1 时任务点必须严格串行（不建线程池、不出现重叠执行）。"""
    state = {"active": 0, "max_active": 0, "threads": set(), "order": []}
    lock = threading.Lock()

    def _slow_job(ch, course, job, job_info, speed):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            state["threads"].add(threading.current_thread().name)
            state["order"].append(job["jobid"])
        time.sleep(0.01)
        with lock:
            state["active"] -= 1
        return StudyResult.SUCCESS

    class _NoExecutor:
        def __init__(self, *a, **k):
            raise AssertionError("并发=1 时不应创建线程池")

    old_executor, old_process_job = engine.ThreadPoolExecutor, engine.process_job
    engine.process_chapter = _ORIG_PROCESS_CHAPTER
    try:
        engine.ThreadPoolExecutor = _NoExecutor
        engine.process_job = _slow_job

        eng = StudyEngine(cache_dir=tempfile.mkdtemp(), poll_interval=0.01)
        eng.chaoxing = _MultiJobChaoxing(5)
        eng.current_uid = "u9"
        eng.jobs = 1

        r = eng._process_chapter({"courseId": "C", "title": "课"},
                                 {"id": "p1", "title": "章", "has_finished": False})
        assert r == ChapterResult.SUCCESS, r
        assert state["max_active"] == 1, f"出现并发执行，最大同时执行数 {state['max_active']}"
        assert state["order"] == [f"v{i}" for i in range(5)], state["order"]
        assert state["threads"] == {threading.main_thread().name}, state["threads"]
    finally:
        engine.ThreadPoolExecutor = old_executor
        engine.process_job = old_process_job


def test_normalize_jobs_clamps():
    """并发数收敛到 1~8：非法/越界都回到合法区间。"""
    assert engine.normalize_jobs(3) == 3
    assert engine.normalize_jobs("2") == 2
    assert engine.normalize_jobs(0) == 1
    assert engine.normalize_jobs(-5) == 1
    assert engine.normalize_jobs(99) == engine.MAX_JOBS
    assert engine.normalize_jobs("x") == engine.DEFAULT_JOBS
    assert engine.normalize_jobs(None) == engine.DEFAULT_JOBS
    assert StudyEngine(cache_dir=tempfile.mkdtemp()).jobs == engine.DEFAULT_JOBS


# --------------------------------------------------------------------------- #
# 账号持久化
# --------------------------------------------------------------------------- #
def test_account_store_roundtrip():
    """账号表落盘后能原样读回，密码不以明文出现在文件里。"""
    from account_store import AccountStore
    path = os.path.join(tempfile.mkdtemp(), "accounts.json")
    AccountStore(path).save({
        "u1": {"username": "13800000000", "password": "p@ss-1", "use_cookies": False},
        "u2": {"username": "13900000000", "password": "", "use_cookies": True},
    }, "u2")

    raw = open(path, encoding="utf8").read()
    assert "p@ss-1" not in raw, "密码不能明文落盘"

    store = AccountStore(path)
    acc = store.load()
    assert store.last_uid == "u2"
    assert acc["u1"]["password"] == "p@ss-1"
    assert acc["u2"]["password"] == ""
    assert acc["u2"]["use_cookies"] is True


def test_account_store_tolerates_bad_file():
    """账号文件损坏 / 版本异常时退回空表，不能影响启动。"""
    from account_store import AccountStore
    path = os.path.join(tempfile.mkdtemp(), "accounts.json")
    open(path, "w", encoding="utf8").write("{not-json")
    store = AccountStore(path)
    assert store.load() == {}
    assert store.last_uid == ""

    # last_uid 指向不存在的账号 -> 清空，避免自动登录登到不存在的账号
    open(path, "w", encoding="utf8").write(
        '{"version": 1, "last_uid": "ghost", "accounts": {"a": {"username": "x", "password": ""}}}'
    )
    store = AccountStore(path)
    store.load()
    assert store.last_uid == ""


def test_engine_remembers_account_across_restart():
    """登录过的账号重启后仍在，可自动登录；演示账号不入库。"""
    cache = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=cache)
    assert eng.accounts == {}
    assert eng.saved_account() is None

    TEST_USERNAME = "13800138000"

    eng.accounts["777"] = {"username": TEST_USERNAME, "password": "pw", "use_cookies": False}
    eng._persist_accounts("777")

    reboot = StudyEngine(cache_dir=cache)      # 模拟重启
    assert "777" in reboot.accounts
    assert reboot.last_uid == "777"
    acc = reboot.saved_account()
    assert acc and acc["username"] == TEST_USERNAME and acc["password"] == "pw"

    # remember=False：只记账号，不存密码 -> 视为不可自动登录
    reboot.accounts["888"] = {"username": "13900000000", "password": "", "use_cookies": False}
    reboot._persist_accounts("888")
    again = StudyEngine(cache_dir=cache)
    assert again.last_uid == "888"
    assert again.saved_account() is None, "没存密码不应尝试自动登录"

    # 演示账号不落盘
    demo_cache = tempfile.mkdtemp()
    demo = StudyEngine(cache_dir=demo_cache)
    demo.login_demo()
    assert not os.path.exists(os.path.join(demo_cache, "accounts.json"))
    assert demo.saved_account() is None


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
            annotate_exc(f"test_engine.py::{fn.__name__}")
    print(f"\n{'ALL PASS' if failed == 0 else str(failed) + ' FAILED'}")
    sys.exit(1 if failed else 0)
