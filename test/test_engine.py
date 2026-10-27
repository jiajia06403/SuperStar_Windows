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
    """上一章答题不合格导致本章未开放 -> 在 reanswer_rounds 上限内一直重答，直到通过。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01, reanswer_interval=0.01)
    eng.current_uid = "778"
    eng.work_retry = 1
    eng.reanswer_rounds = 5
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
        # 前 3 轮都判「不合格」，第 4 轮才通过（work_retry=1 之外的额外重答）
        return ChapterResult.UNVERIFIED if rounds["n"] <= 3 else ChapterResult.SUCCESS

    engine.process_chapter = _fail_then_ok
    try:
        opened = eng._handle_not_open({"courseId": "C", "title": "x"}, points,
                                      i=1, cid="C", uid="778")
    finally:
        engine.process_chapter = _ORIG_PROCESS_CHAPTER

    assert opened is True, "重答通过且本章解锁后应返回 True"
    assert rounds["n"] == 4, f"上限内应继续重答，实际只重答了 {rounds['n']} 轮"
    assert eng.cache.is_done("778", "C", "p0") is True


def test_reanswer_stops_after_max_rounds():
    """重答轮数达到 reanswer_rounds 仍不合格 -> 停止刷课（不再轮询解锁）。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01, reanswer_interval=0.01)
    eng.current_uid = "779"
    eng.work_retry = 1
    eng.reanswer_rounds = 3
    eng.manual_wait = 0          # 本用例验证「停手」，不等手动窗口
    fake = FakeChaoxing("779")
    fake.job_info_seq = [{"notOpen": True}] * 100
    eng.chaoxing = fake
    points = [{"id": "p0", "title": "上一章"}, {"id": "p1", "title": "当前章"}]

    rounds = {"n": 0}

    def _always_unverified(ch, course, point, speed, work_retry=1, **kw):
        rounds["n"] += 1
        return ChapterResult.UNVERIFIED

    engine.process_chapter = _always_unverified
    try:
        opened = eng._handle_not_open({"courseId": "C", "title": "x"}, points,
                                      i=1, cid="C", uid="779")
    finally:
        engine.process_chapter = _ORIG_PROCESS_CHAPTER

    assert opened is None, "重答无果时必须判「无解」并结束课程"
    assert rounds["n"] == 3, f"应最多重答 {3} 轮，实际 {rounds['n']} 轮"
    assert len(fake.job_info_seq) == 100, "停止重答后不得再查开放状态"
    assert points[0]["work_refused"] is True


def test_reanswer_stops_when_no_progress():
    """判分没进展（判对题数不涨、拿不到正确答案）连续两轮 -> 停止重答。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01, reanswer_interval=0.01)
    eng.current_uid = "77a"
    eng.work_retry = 1
    eng.reanswer_rounds = 9          # 只靠「无进展」早停
    eng.manual_wait = 0
    fake = FakeChaoxing("77a")
    fake.last_verdict_stats = {}
    fake.job_info_seq = [{"notOpen": True}] * 100
    eng.chaoxing = fake
    points = [{"id": "p0", "title": "上一章"}, {"id": "p1", "title": "当前章"}]

    rounds = {"n": 0}

    def _no_progress(ch, course, point, speed, work_retry=1, **kw):
        rounds["n"] += 1
        # 每轮答案都不同（digest 会变），但判分始终「对 2 错 1、没拿到正确答案」
        fake.last_submitted_digest = f"digest-{rounds['n']}"
        fake.last_verdict_stats = {"right": 2, "wrong": 1, "fixed": 0}
        return ChapterResult.UNVERIFIED

    engine.process_chapter = _no_progress
    try:
        opened = eng._handle_not_open({"courseId": "C", "title": "x"}, points,
                                      i=1, cid="C", uid="77a")
    finally:
        engine.process_chapter = _ORIG_PROCESS_CHAPTER

    assert opened is None, opened
    # 第 1 轮 establish best_right=2；第 2、3 轮无进展 -> 停（不跑满 9 轮）
    assert rounds["n"] == 3, f"无进展应在 {3} 轮内停手，实际 {rounds['n']} 轮"


def test_reanswer_stops_when_no_answer_found():
    """整份作业一题答案都没搜到（全靠随机）-> 一轮后就停，不再重做提交。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01, reanswer_interval=0.01)
    eng.current_uid = "77b"
    eng.work_retry = 1
    eng.reanswer_rounds = 9
    eng.manual_wait = 0
    fake = FakeChaoxing("77b")
    fake.job_info_seq = [{"notOpen": True}] * 100
    eng.chaoxing = fake
    points = [{"id": "p0", "title": "上一章"}, {"id": "p1", "title": "当前章"}]

    rounds = {"n": 0}

    def _no_answer(ch, course, point, speed, work_retry=1, **kw):
        rounds["n"] += 1
        fake.last_answer_stats = {"total": 4, "found": 0}
        return ChapterResult.UNVERIFIED

    engine.process_chapter = _no_answer
    try:
        opened = eng._handle_not_open({"courseId": "C", "title": "x"}, points,
                                      i=1, cid="C", uid="77b")
    finally:
        engine.process_chapter = _ORIG_PROCESS_CHAPTER

    assert opened is None, opened
    assert rounds["n"] == 1, f"没搜到答案应立即停手，实际 {rounds['n']} 轮"


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


def test_course_events_and_no_not_open_status():
    """课程开始/结束事件（界面「进行中」的依据）；未开放章节不再上报 not_open。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01)
    fake = FakeChaoxing("225")
    eng.chaoxing = fake
    eng.current_uid = "225"
    course = {"courseId": "C", "clazzId": "cz", "cpi": "cp", "title": "课"}
    fake.points = [{"id": "a", "title": "锁章", "has_finished": False}]
    fake.job_info_seq = [{"notOpen": True}, {"notOpen": False}]   # 轮询一次后解锁

    seen_course, seen_status = [], []
    eng.on(eng.COURSE, lambda cid, phase: seen_course.append((cid, phase)))
    eng.on(eng.PROGRESS, lambda cid, pid, st: seen_status.append(st))

    calls = {"n": 0}

    def fake_process(ch, c2, point, speed, work_retry=1, **kw):
        calls["n"] += 1
        return ChapterResult.NOT_OPEN if calls["n"] == 1 else ChapterResult.SUCCESS

    engine.process_chapter = fake_process
    eng._run([course])

    assert seen_course == [("C", "start"), ("C", "end")], seen_course
    assert "not_open" not in seen_status, f"界面已不再展示「未开放」，不应上报: {seen_status}"
    assert "pending" in seen_status, seen_status      # 等待解锁期间保持「待学」
    assert eng.cache.is_done("225", "C", "a")


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
# 并发（界面「并发章节」= 章节级；设置里「任务点并发」= 章内级）
# --------------------------------------------------------------------------- #
class _SpiedLimiter:
    def limit_rate(self, *a, **k):
        return None


class _SpiedTiku:
    DISABLE = False


class _MultiJobChaoxing:
    """一章多个任务点（非章节检测），用来观察「任务点并发」。"""

    def __init__(self, count):
        self.rate_limiter = _SpiedLimiter()
        self.tiku = _SpiedTiku()
        self.jobs = [{"type": "video", "jobid": f"v{i}"} for i in range(count)]

    def get_job_list(self, course, point, **kwargs):
        return list(self.jobs), {"knowledgeid": point["id"]}


class _MultiChapterChaoxing:
    """一门课多个章节，用来观察「章节并发」/降级行为。"""

    def __init__(self, count):
        self.rate_limiter = _SpiedLimiter()
        self.tiku = _SpiedTiku()
        self.points = [{"id": f"p{i}", "title": f"第{i}章", "has_finished": False}
                       for i in range(count)]

    def get_course_point(self, *a, **k):
        return {"points": self.points}


def _course() -> dict:
    return {"courseId": "C", "clazzId": "cz", "cpi": "cp", "title": "课"}


def test_chapters_run_concurrently():
    """界面「并发章节」= 3 时，确实有 3 个章节同时在跑。"""
    state = {"active": 0, "max": 0}
    done: list[str] = []
    lock = threading.Lock()

    def _fake(ch, course, point, speed, work_retry=3, jobs=1):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
            done.append(point["id"])
        return ChapterResult.SUCCESS

    old = engine.process_chapter
    engine.process_chapter = _fake
    try:
        eng = StudyEngine(cache_dir=tempfile.mkdtemp(), poll_interval=0.01)
        eng.chaoxing = _MultiChapterChaoxing(6)
        eng.current_uid = "u"
        eng.jobs = 3
        eng._study_course(_course())
    finally:
        engine.process_chapter = old

    assert state["max"] == 3, f"章节并发上限应为 3，实际 {state['max']}"
    assert sorted(done) == [f"p{i}" for i in range(6)], done
    assert all(p["has_finished"] for p in eng.chaoxing.points)


def test_not_open_degrades_to_serial():
    """出现「未开放」章节后，剩余章节必须回到串行（不再用并发 worker）。"""
    lock = threading.Lock()
    seen: "list[tuple[str, str]]" = []      # (pid, 线程名)
    first_not_open = {"hit": False, "at": None}

    def _fake(ch, course, point, speed, work_retry=3, jobs=1):
        with lock:
            seen.append((point["id"], threading.current_thread().name))
            if point["id"] == "p2" and not first_not_open["hit"]:
                first_not_open["hit"] = True
                first_not_open["at"] = len(seen)
                return ChapterResult.NOT_OPEN
        time.sleep(0.01)
        return ChapterResult.SUCCESS

    old = engine.process_chapter
    engine.process_chapter = _fake
    try:
        eng = StudyEngine(cache_dir=tempfile.mkdtemp(), poll_interval=0.01)
        eng.chaoxing = _MultiChapterChaoxing(5)
        eng.current_uid = "u"
        eng.jobs = 3
        eng._study_course(_course())
    finally:
        engine.process_chapter = old

    assert first_not_open["hit"] is True, "假后端没有触发 NOT_OPEN"
    assert all(p["has_finished"] for p in eng.chaoxing.points), \
        [p["title"] for p in eng.chaoxing.points if not p["has_finished"]]
    tail = seen[first_not_open["at"]:]
    assert tail, "降级之后没有任何章节被续跑"
    assert all(not name.startswith("chapter-") for _pid, name in tail), \
        f"降级后仍在用并发 worker: {tail}"


def test_jobs_one_runs_serial():
    """并发章节=1 时不建 worker 线程，全部在刷课主线程上顺序执行。"""
    seen: "list[tuple[str, str]]" = []
    lock = threading.Lock()

    def _fake(ch, course, point, speed, work_retry=3, jobs=1):
        with lock:
            seen.append((point["id"], threading.current_thread().name))
        return ChapterResult.SUCCESS

    old = engine.process_chapter
    engine.process_chapter = _fake
    try:
        eng = StudyEngine(cache_dir=tempfile.mkdtemp(), poll_interval=0.01)
        eng.chaoxing = _MultiChapterChaoxing(5)
        eng.current_uid = "u"
        eng.jobs = 1
        eng._study_course(_course())
    finally:
        engine.process_chapter = old

    assert [pid for pid, _n in seen] == [f"p{i}" for i in range(5)], seen
    assert all(not name.startswith("chapter-") for _pid, name in seen), seen


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


def test_point_jobs_is_forwarded_to_thread_pool():
    """「任务点并发」必须真的生效：StudyEngine.point_jobs=3 -> 线程池 max_workers=3。"""
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
        eng.point_jobs = 3                 # 相当于设置里填了「任务点并发 3」

        r = eng._process_chapter({"courseId": "C", "title": "课"},
                                 {"id": "p1", "title": "章", "has_finished": False})
        assert r == ChapterResult.SUCCESS, r
        assert done == [f"v{i}" for i in range(6)], done
        assert _RecordingExecutor.seen == [3], f"线程池上限应为 3，实际 {_RecordingExecutor.seen}"
    finally:
        engine.ThreadPoolExecutor = old_executor
        engine.process_job = old_process_job


def test_point_jobs_one_runs_serial():
    """任务点并发=1 时章内必须严格串行（不建线程池、不出现重叠执行）。"""
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
            raise AssertionError("任务点并发=1 时不应创建线程池")

    old_executor, old_process_job = engine.ThreadPoolExecutor, engine.process_job
    engine.process_chapter = _ORIG_PROCESS_CHAPTER
    try:
        engine.ThreadPoolExecutor = _NoExecutor
        engine.process_job = _slow_job

        eng = StudyEngine(cache_dir=tempfile.mkdtemp(), poll_interval=0.01)
        eng.chaoxing = _MultiJobChaoxing(5)
        eng.current_uid = "u9"
        eng.point_jobs = 1

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
    """章节并发/任务点并发都收敛到合法区间；引擎默认章节并发为 1（最稳）。"""
    assert engine.normalize_jobs(3) == 3
    assert engine.normalize_jobs("2") == 2
    assert engine.normalize_jobs(0) == 1
    assert engine.normalize_jobs(-5) == 1
    assert engine.normalize_jobs(99) == engine.MAX_JOBS
    assert engine.normalize_jobs("x") == engine.DEFAULT_JOBS
    assert engine.normalize_jobs(None) == engine.DEFAULT_JOBS
    assert StudyEngine(cache_dir=tempfile.mkdtemp()).jobs == engine.DEFAULT_CHAPTER_JOBS

    assert engine.normalize_point_jobs(3) == 3
    assert engine.normalize_point_jobs(0) == 1
    assert engine.normalize_point_jobs(99) == engine.MAX_POINT_JOBS
    assert engine.normalize_point_jobs("x") == engine.DEFAULT_POINT_JOBS
    assert engine.normalize_point_jobs(None) == engine.DEFAULT_POINT_JOBS
    assert engine.normalize_point_jobs("") == engine.DEFAULT_POINT_JOBS


# --------------------------------------------------------------------------- #
# 平台拒绝重答（「提交失败，重做次数过多」）不能变成「每 15 秒查一次开放」的死循环
# --------------------------------------------------------------------------- #
def test_work_retry_refused_detector():
    """平台阻断提交：重做次数用尽 / 图形验证码都要能识别，普通失败不能误判。"""
    from api.base import CAPTCHA_HINT, work_block_reason, work_retry_refused
    # 实测「提交失败，重做次数过多！」的真实原因是弹了图形验证码，两种都算阻断
    assert work_retry_refused("提交失败，重做次数过多！")
    assert work_retry_refused("您的重做次数已用尽")
    assert work_retry_refused("不允许重做")
    assert work_retry_refused("请输入验证码")
    assert work_retry_refused("请完成安全验证后重试")
    assert not work_retry_refused("提交失败")
    assert not work_retry_refused("")
    assert not work_retry_refused(None)
    # 原因可读（提示用户去浏览器手动验证）
    assert "验证码" in work_block_reason("请输入验证码")
    assert "重做次数" in work_block_reason("提交失败，重做次数过多！")
    assert work_block_reason("") == ""
    assert "验证码" in CAPTCHA_HINT


class _RefuseBackend:
    """最小后端：章节检测提交被平台拒绝（StudyResult.REFUSED）。"""

    def __init__(self):
        import types
        self.rate_limiter = types.SimpleNamespace(limit_rate=lambda *a, **k: None)
        self.tiku = types.SimpleNamespace(DISABLE=False)
        self.work_refused_reason = ""
        self.study_calls = 0

    def get_job_list(self, course, point, **kwargs):
        return [{"type": "workid", "jobid": "w"}], {"knowledgeid": "p1"}

    def study_work(self, *a, **k):
        self.study_calls += 1
        self.work_refused_reason = "提交失败，重做次数过多！"
        return StudyResult.REFUSED


def test_process_chapter_refused_is_no_retry():
    """拒绝重答必须映射成 NO_RETRY，绝不能是 ERROR（ERROR 会触发回退重答/轮询）。"""
    be = _RefuseBackend()
    # 前面的用例会把 engine.process_chapter 换成假实现且不还原：这里显式用原始实现
    r = _ORIG_PROCESS_CHAPTER(be, {"courseId": "C", "title": "课"},
                              {"id": "p1", "title": "章节"}, 1.0, work_retry=1)
    assert r == ChapterResult.NO_RETRY, r
    assert be.study_calls == 1, "拒绝重答后不得再重答"


def test_refused_prev_stops_polling():
    """上一章被平台拒绝重答 -> _handle_not_open 返回 None，一次轮询都不做。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01, reanswer_interval=0.01)
    eng.current_uid = "781"
    eng.work_retry = 1
    eng.manual_wait = 0          # 不等手动窗口，验证「拒绝后不轮询」
    fake = FakeChaoxing("781")
    fake.job_info_seq = [{"notOpen": True}] * 100     # 怎么问都是未开放
    eng.chaoxing = fake
    points = [{"id": "p0", "title": "上一章"}, {"id": "p1", "title": "当前章"}]

    calls = {"n": 0}

    def _refused(ch, course, point, speed, work_retry=1, **kw):
        calls["n"] += 1
        return ChapterResult.NO_RETRY

    engine.process_chapter = _refused
    try:
        opened = eng._handle_not_open({"courseId": "C", "title": "x"}, points,
                                      i=1, cid="C", uid="781")
    finally:
        engine.process_chapter = _ORIG_PROCESS_CHAPTER

    assert opened is None, "拒绝重答后必须判「无解」，不能继续轮询"
    assert calls["n"] == 1, f"只允许尝试重做一次，实际 {calls['n']} 次"
    assert points[0]["work_refused"] is True
    assert len(fake.job_info_seq) == 100, "拒绝重答后不得再查开放状态"


def test_refused_flag_short_circuits_later_chapters():
    """前面任一章节被拒 -> 后续章节直接判无解，不重做也不轮询。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01)
    fake = FakeChaoxing("782")
    eng.chaoxing = fake
    eng.current_uid = "782"
    points = [
        {"id": "p0", "title": "被拒章", "work_refused": True},
        {"id": "p1", "title": "锁章"},
    ]

    def _boom(*a, **k):     # 不允许再处理任何章节
        raise AssertionError("被拒章节之后的章节不应再回退重做")

    engine.process_chapter = _boom
    try:
        opened = eng._handle_not_open({"courseId": "C", "title": "x"}, points,
                                      i=1, cid="C", uid="782")
    finally:
        engine.process_chapter = _ORIG_PROCESS_CHAPTER
    assert opened is None, opened
    assert fake.job_info_seq == [], "不得发起开放状态查询"


def test_study_course_refused_does_not_hang():
    """端到端：上一章被拒、下一章始终未开放 -> 必须结束本课程（不能空转）。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01, reanswer_interval=0.01)
    eng.current_uid = "783"
    eng.work_retry = 1
    eng.manual_wait = 0          # 不等手动窗口，验证端到端不挂死
    fake = FakeChaoxing("783")
    fake.job_info_seq = [{"notOpen": True}] * 1000
    eng.chaoxing = fake
    course = {"courseId": "C", "clazzId": "cz", "cpi": "cp", "title": "课"}
    fake.points = [
        {"id": "a", "title": "被拒章", "has_finished": False},
        {"id": "b", "title": "锁章", "has_finished": False},
    ]

    def _fake_process(ch, c2, point, speed, work_retry=1, **kw):
        # a 被平台拒绝重答；b 因 a 未通过而始终未开放
        return ChapterResult.NO_RETRY if point["id"] == "a" else ChapterResult.NOT_OPEN

    statuses = []
    eng.on(eng.STATUS, lambda kind, msg: statuses.append(kind))
    engine.process_chapter = _fake_process
    t = threading.Thread(target=eng._study_course, args=(course,), daemon=True)
    try:
        t.start()
        t.join(10)
    finally:
        engine.process_chapter = _ORIG_PROCESS_CHAPTER
    assert not t.is_alive(), "拒绝重答 + 后续章节未开放时不得无限轮询"
    assert "blocked" in statuses, statuses
    assert not eng.cache.is_done("783", "C", "a"), "被拒章节绝不能标记完成"
    assert not eng.cache.is_done("783", "C", "b")


def test_study_work_refused_returns_refused():
    """真实提交链路：平台返回「重做次数过多」-> StudyResult.REFUSED + 记录原因。"""
    import api.base as base

    class _Resp:
        def __init__(self, json_data):
            self.status_code = 200
            self.text = "ok"
            self._json = json_data

        def json(self):
            return self._json

    class _Session:
        def get(self, url, params=None, **kw):
            return _Resp({})

        def post(self, url, data=None, **kw):
            return _Resp({"status": False, "msg": "提交失败，重做次数过多！"})

    class _Tiku:
        DISABLE = False
        COVER_RATE = 0        # 覆盖率门槛 0 -> 直接提交

        def query(self, q):
            return None

        def get_submit_params(self):
            return ""

        def judgement_select(self, res):
            return True

    questions = {"questions": [
        {"id": 1, "type": "single", "title": "Q1", "options": "A. x\nB. y",
         "answerField": {"answer1": "", "answertype1": "0"}},
    ]}
    old_get_session = base.SessionManager.get_session
    old_decode = base.decode_questions_info
    base.SessionManager.get_session = staticmethod(lambda: _Session())
    base.decode_questions_info = lambda text: {"questions": [dict(q) for q in questions["questions"]]}
    try:
        c = base.Chaoxing(account=None, tiku=_Tiku())
        r = c.study_work({"courseId": "C", "clazzId": "cz", "title": "课"},
                         {"type": "workid", "jobid": "work-1", "enc": "e"},
                         {"knowledgeid": "p1", "ktoken": "k", "cpi": "cp"})
    finally:
        base.SessionManager.get_session = old_get_session
        base.decode_questions_info = old_decode

    assert r == StudyResult.REFUSED, r
    assert "重做次数" in c.work_refused_reason, c.work_refused_reason


class _ManualFake(FakeChaoxing):
    """可模拟「用户手动在浏览器里做完了上一章」：第 N 次回读进度起报告已完成。"""

    def __init__(self, uid, finish_after=None):
        super().__init__(uid)
        self.finish_after = finish_after   # None = 永远不做完
        self.reads = 0

    def get_course_point(self, *a, **k):
        self.reads += 1
        if self.finish_after is not None and self.reads >= self.finish_after:
            for p in self.points:
                p["has_finished"] = True
        return {"points": self.points}


def test_manual_wait_continues_after_user_finishes_chapter():
    """重答轮数到上限后不立刻结束：用户手动做完上一章 -> 自动继续刷课。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01, reanswer_interval=0.01)
    eng.current_uid = "78a"
    eng.work_retry = 1
    eng.reanswer_rounds = 1
    eng.manual_wait = 2                    # 2 秒窗口，回读 2 次后用户做完
    fake = _ManualFake("78a", finish_after=2)
    fake.points = [{"id": "p0", "title": "上一章", "has_finished": False},
                   {"id": "p1", "title": "当前章", "has_finished": False}]
    fake.job_info_seq = [{"notOpen": False}]   # 做完后本章已解锁
    eng.chaoxing = fake
    points = [{"id": "p0", "title": "上一章"}, {"id": "p1", "title": "当前章"}]

    engine.process_chapter = lambda ch, c, p, s, work_retry=1, **kw: ChapterResult.UNVERIFIED
    try:
        opened = eng._handle_not_open({"courseId": "C", "clazzId": "cz", "cpi": "cp",
                                       "title": "课"}, points, i=1, cid="C", uid="78a")
    finally:
        engine.process_chapter = _ORIG_PROCESS_CHAPTER

    assert opened is True, "用户手动完成后必须自动继续，而不是结束课程"
    assert not points[0].get("work_refused"), "手动做完了不能标成「被拒绝」"
    assert points[0].get("has_finished") is True
    assert eng.cache.is_done("78a", "C", "p0") is True


def test_manual_wait_timeout_stops_course():
    """重答到顶 + 手动窗口内毫无变化 -> 判无解结束课程，且不去查开放状态。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.02, reanswer_interval=0.01)
    eng.current_uid = "78b"
    eng.work_retry = 1
    eng.reanswer_rounds = 1
    eng.manual_wait = 1
    fake = _ManualFake("78b", finish_after=None)    # 用户始终没动
    fake.points = [{"id": "p0", "title": "上一章", "has_finished": False},
                   {"id": "p1", "title": "当前章", "has_finished": False}]
    eng.chaoxing = fake
    points = [{"id": "p0", "title": "上一章"}, {"id": "p1", "title": "当前章"}]
    prog = []
    eng.on(eng.PROGRESS, lambda c, p, st: prog.append((p, st)))

    engine.process_chapter = lambda ch, c, p, s, work_retry=1, **kw: ChapterResult.UNVERIFIED
    try:
        opened = eng._handle_not_open({"courseId": "C", "clazzId": "cz", "cpi": "cp",
                                       "title": "课"}, points, i=1, cid="C", uid="78b")
    finally:
        engine.process_chapter = _ORIG_PROCESS_CHAPTER

    assert opened is None, opened
    assert fake.reads >= 2, f"窗口期内应多次回读进度，实际 {fake.reads} 次"
    assert ("p0", "manual") in prog, f"等待手动处理时章节行应标「需手动」: {prog}"
    assert fake.job_info_seq == [], "上一章没做完时不应去查开放状态"
    assert points[0]["work_refused"] is True
    assert not eng.cache.is_done("78b", "C", "p0")


def test_manual_wait_after_captcha_block():
    """平台弹验证码（NO_RETRY）后同样走手动窗口：用户过完验证码做完本章 -> 继续。"""
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01, reanswer_interval=0.01)
    eng.current_uid = "78c"
    eng.work_retry = 1
    eng.manual_wait = 2
    fake = _ManualFake("78c", finish_after=1)
    fake.points = [{"id": "p0", "title": "上一章", "has_finished": False},
                   {"id": "p1", "title": "当前章", "has_finished": False}]
    fake.job_info_seq = [{"notOpen": False}]
    fake.work_refused_reason = "请输入验证码"
    eng.chaoxing = fake
    points = [{"id": "p0", "title": "上一章"}, {"id": "p1", "title": "当前章"}]

    engine.process_chapter = lambda ch, c, p, s, work_retry=1, **kw: ChapterResult.NO_RETRY
    try:
        opened = eng._handle_not_open({"courseId": "C", "clazzId": "cz", "cpi": "cp",
                                       "title": "课"}, points, i=1, cid="C", uid="78c")
    finally:
        engine.process_chapter = _ORIG_PROCESS_CHAPTER

    assert opened is True, "手动过完验证码做完本章后应继续刷课"
    assert not points[0].get("work_refused")


def test_manual_wait_waits_for_popup_ack_before_counting():
    """弹窗未关闭（未 ack_manual）前不计时、不回读进度；关闭后才开始计时窗口。

    这就是「需手动」弹窗的契约：引擎发 PROMPT 后阻塞，界面关掉弹窗再 ack，
    引擎才真的开始 5 分钟倒计时与轮询进度。
    """
    from api import cancel

    cancel.clear()
    tmp_path = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp_path), poll_interval=0.01, reanswer_interval=0.01)
    eng.current_uid = "78d"
    eng.work_retry = 1
    eng.reanswer_rounds = 1
    eng.manual_wait = 5
    fake = _ManualFake("78d", finish_after=1)
    fake.points = [{"id": "p0", "title": "上一章", "has_finished": False},
                   {"id": "p1", "title": "当前章", "has_finished": False}]
    fake.job_info_seq = [{"notOpen": False}]
    eng.chaoxing = fake
    points = [{"id": "p0", "title": "上一章"}, {"id": "p1", "title": "当前章"}]
    prompts = []
    eng.on(eng.PROMPT, lambda t, ct, sec: prompts.append((t, ct, sec)))

    box = {}
    engine.process_chapter = lambda ch, c, p, s, work_retry=1, **kw: ChapterResult.UNVERIFIED
    try:
        th = threading.Thread(target=lambda: box.setdefault(
            "ret", eng._handle_not_open({"courseId": "C", "clazzId": "cz", "cpi": "cp",
                                        "title": "课"}, points, i=1, cid="C", uid="78d")))
        th.start()
        th.join(0.5)
        assert prompts, "应发出「需手动」提示事件"
        assert prompts[0][0] == "上一章" and prompts[0][2] == 5, prompts
        assert th.is_alive(), "用户没关弹窗前引擎必须一直等，不能自己开始计时"
        assert fake.reads == 0, f"用户没关弹窗前不该回读进度，实际回读 {fake.reads} 次"

        eng.ack_manual()          # 模拟用户关掉弹窗
        th.join(5)
        assert not th.is_alive(), "确认之后应继续执行（计时窗口内做完则返回 True）"
        assert box.get("ret") is True, box
        assert fake.reads >= 1, "关掉弹窗后才开始轮询进度"
    finally:
        engine.process_chapter = _ORIG_PROCESS_CHAPTER
        cancel.clear()


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
