# -*- coding: utf-8 -*-
# layer: core  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""异常隔离 / 失败重试测试（无需网络 / 无需 GUI）。

背景：一次 JSONDecodeError / RequestException 曾从 study_video 经
``ThreadPoolExecutor.map`` 一路冒到 ``StudyEngine._run`` 的 except Exception，
直接结束整轮刷课 —— 后面选中的课程全都不跑。

覆盖三层隔离 + 请求重试：
  1. process_job：单任务点异常 -> StudyResult.ERROR，同章其它任务点照跑；
     Cancelled 仍要上抛（停止语义不变）。
  2. process_chapter：异常 -> ChapterResult.ERROR；Cancelled -> STOPPED（优先）。
  3. _study_course / _run：坏章、坏课都不影响后续，最终发 finished 而不是 error。
  4. api.base._request_json：状态码检查 + 指数退避重试 + 停止即中断。
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate_exc  # noqa: E402

from api import cancel  # noqa: E402
from api.cancel import Cancelled  # noqa: E402
from api.exceptions import StudyRequestError  # noqa: E402
import api.base as base  # noqa: E402
from api.base import StudyResult  # noqa: E402
import engine  # noqa: E402
from engine import StudyEngine, ChapterResult  # noqa: E402


# --------------------------------------------------------------------------- #
# 假后端：第 1 章的「视频任务点」必炸，其它任务点/章节正常
# --------------------------------------------------------------------------- #
class _Limiter:
    def limit_rate(self, *a, **k):
        return None


class _Tiku:
    """题库停用：不做回读重答，隔离用例只关心任务点级别的异常。"""
    DISABLE = True


class FaultyBackend:
    """第 1 章 = 坏视频 + 正常文档；第 2/3 章 = 正常文档。"""

    def __init__(self):
        self.rate_limiter = _Limiter()
        self.tiku = _Tiku()
        self.uid = "u-fault"
        self.calls = []          # 记录真正执行过的任务点 jobid
        self.points = [
            {"id": "bad", "title": "坏章（视频必炸）", "has_finished": False},
            {"id": "ok1", "title": "正常章1", "has_finished": False},
            {"id": "ok2", "title": "正常章2", "has_finished": False},
        ]

    # ---- 引擎接口 ----
    def get_uid(self):
        return self.uid

    def get_course_point(self, *a, **k):
        return {"points": [dict(p) for p in self.points]}

    def get_job_list(self, course, point, **kwargs):
        pid = str(point.get("id"))
        if pid == "bad":
            return ([{"type": "video", "jobid": "bad-video"},
                     {"type": "document", "jobid": "bad-doc"}], {"notOpen": False})
        return [{"type": "document", "jobid": f"{pid}-doc"}], {"notOpen": False}

    # ---- 学习动作 ----
    def study_video(self, course, job, job_info, _speed=1.0, _type="Video"):
        self.calls.append(job["jobid"])
        # 模拟真实故障：平台返回了非 JSON（验证码页 / 网关错误页）
        raise json.JSONDecodeError("Expecting value: line 1 column 1 (char 0)", "网关错误", 0)

    def study_document(self, course, job):
        self.calls.append(job["jobid"])
        return StudyResult.SUCCESS


# --------------------------------------------------------------------------- #
# 1. process_job：单任务点隔离
# --------------------------------------------------------------------------- #
def test_process_job_converts_exception_to_error():
    be = FaultyBackend()
    job = {"type": "video", "jobid": "bad-video"}
    # 视频失败后会 fallback 成音频再试一次，两次都炸 -> 仍返回 ERROR 而不是抛出
    r = engine.process_job(be, {"title": "课"}, job, {}, 1.0)
    assert r == StudyResult.ERROR, r
    assert be.calls == ["bad-video", "bad-video"], be.calls


def test_process_job_reraises_cancelled():
    """停止信号不能被当成任务失败吞掉（否则界面会卡在「停止中」）。"""

    class _StopBackend(FaultyBackend):
        def study_video(self, course, job, job_info, _speed=1.0, _type="Video"):
            raise Cancelled()

    cancel.clear()
    try:
        try:
            engine.process_job(_StopBackend(), {"title": "课"},
                               {"type": "video", "jobid": "v"}, {}, 1.0)
        except Cancelled:
            pass
        else:
            raise AssertionError("Cancelled 必须继续上抛")
    finally:
        cancel.clear()


def test_process_job_unknown_type_is_error():
    assert engine.process_job(FaultyBackend(), {"title": "课"},
                              {"type": "??", "jobid": "x"}, {}, 1.0) == StudyResult.ERROR


# --------------------------------------------------------------------------- #
# 2. process_chapter：章节级隔离
# --------------------------------------------------------------------------- #
def test_process_chapter_error_but_sibling_jobs_run():
    """同章的其它任务点必须照常执行（异常只废掉坏的那一个）。"""
    be = FaultyBackend()
    r = engine.process_chapter(be, {"courseId": "C", "title": "课"},
                               {"id": "bad", "title": "坏章"}, 1.0)
    assert r == ChapterResult.ERROR, r
    assert "bad-video" in be.calls, "坏任务点应被调用过"
    assert "bad-doc" in be.calls, "同章其它任务点必须继续执行"


def test_process_chapter_cancelled_still_wins():
    """Cancelled 优先于 Exception：必须返回 STOPPED，保持停止语义。"""
    cancel.clear()
    cancel.set()
    try:
        r = engine.process_chapter(FaultyBackend(), {"courseId": "C", "title": "课"},
                                   {"id": "bad", "title": "坏章"}, 1.0)
    finally:
        cancel.clear()
    assert r == ChapterResult.STOPPED, r


def test_process_chapter_error_when_get_job_list_raises():
    class _Boom(FaultyBackend):
        def get_job_list(self, course, point, **kwargs):
            raise ConnectionError("连接被重置")

    r = engine.process_chapter(_Boom(), {"courseId": "C", "title": "课"},
                               {"id": "bad", "title": "坏章"}, 1.0)
    assert r == ChapterResult.ERROR, r


# --------------------------------------------------------------------------- #
# 3. _study_course / _run：坏章、坏课都不影响后续
# --------------------------------------------------------------------------- #
def test_study_course_continues_after_chapter_error():
    eng = StudyEngine(cache_dir=tempfile.mkdtemp(), poll_interval=0.01)
    be = FaultyBackend()
    eng.chaoxing = be
    eng.current_uid = be.uid
    course = {"courseId": "C", "clazzId": "cz", "cpi": "cp", "title": "课"}

    eng._study_course(course)

    # 坏章失败后仍继续处理后续章节
    assert "ok1-doc" in be.calls and "ok2-doc" in be.calls, be.calls
    assert eng.cache.is_done(be.uid, "C", "ok1") is True
    assert eng.cache.is_done(be.uid, "C", "ok2") is True
    assert eng.cache.is_done(be.uid, "C", "bad") is False, "坏章不能被标记完成"


def test_run_emits_finished_not_error():
    """验收用例：study_video 抛异常 -> 同章其它任务点继续 + 后续章节继续 +
    最终发出 finished（而不是 error），整轮刷课不被单点异常中止。"""
    eng = StudyEngine(cache_dir=tempfile.mkdtemp(), poll_interval=0.01)
    be = FaultyBackend()
    eng.chaoxing = be
    eng.current_uid = be.uid

    statuses, finished = [], []
    eng.on(StudyEngine.STATUS, lambda state, msg: statuses.append(state))
    eng.on(StudyEngine.FINISHED, lambda stopped: finished.append(stopped))

    course = {"courseId": "C", "clazzId": "cz", "cpi": "cp", "title": "课"}
    eng._run([course])

    assert finished == [False], f"必须发出 finished（未被停止），实际 {finished}"
    assert "error" not in statuses, f"不应出现 error 状态，实际 {statuses}"
    assert statuses[-1] == "idle", statuses
    # 1) 同章其它任务点继续执行
    assert "bad-doc" in be.calls, "同章其它任务点应继续执行"
    # 2) 后续章节继续执行
    assert "ok1-doc" in be.calls and "ok2-doc" in be.calls, be.calls
    assert eng.cache.is_done(be.uid, "C", "ok1") is True
    assert eng.cache.is_done(be.uid, "C", "ok2") is True


def test_run_isolates_broken_course():
    """一门课在读章节时就炸 -> 跳过它，后面选中的课照跑。"""
    eng = StudyEngine(cache_dir=tempfile.mkdtemp(), poll_interval=0.01)
    be = FaultyBackend()
    eng.chaoxing = be
    eng.current_uid = be.uid
    finished = []
    eng.on(StudyEngine.FINISHED, lambda stopped: finished.append(stopped))

    boom = {"courseId": "X", "clazzId": "cz", "cpi": "cp", "title": "坏课"}

    def _get_point(courseid, clazzid, cpi):
        if str(courseid) == "X":
            raise RuntimeError("课程接口 500")
        return {"points": [dict(p) for p in be.points]}

    be.get_course_point = _get_point
    eng._run([boom, {"courseId": "C", "clazzId": "cz", "cpi": "cp", "title": "好课"}])

    assert finished == [False], finished
    assert "ok1-doc" in be.calls, "坏课之后的课程必须继续刷"


# --------------------------------------------------------------------------- #
# 4. _request_json：状态码检查 + 指数退避重试 + 停止即中断
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self.text = text
        self._payload = {"ok": True} if payload is None else payload

    def json(self):
        return self._payload


class _BadJsonResp(_Resp):
    """200 但响应体不是 JSON（网关/验证码页）—— 真实环境里最常见的脏响应。"""

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


class _FakeSession:
    """按脚本依次返回响应的假 session。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def _next(self):
        self.calls += 1
        item = self.script.pop(0) if self.script else _Resp(200)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kw):
        return self._next()

    def post(self, url, **kw):
        return self._next()


def test_request_json_retries_until_success():
    class _S:
        n = 0

        def get(self, url, **kw):
            _S.n += 1
            return _Resp(500) if _S.n < 3 else _Resp(200)

    s = _S()
    data = base._request_json(s, "get", "u", what="测试", base_delay=0.01)
    assert data == {"ok": True}
    assert s.n == 3, f"应在第 3 次成功，实际请求 {s.n} 次"


def test_request_json_raises_after_retries():
    class _S:
        n = 0

        def get(self, url, **kw):
            _S.n += 1
            return _Resp(500, text="server down")

    s = _S()
    try:
        base._request_json(s, "get", "u", what="测试", base_delay=0.01)
    except StudyRequestError as exc:
        assert "测试" in str(exc) and "500" in str(exc), str(exc)
    else:
        raise AssertionError("重试耗尽必须抛 StudyRequestError")
    assert s.n == 3, f"最多重试 3 次，实际 {s.n} 次"


def test_request_json_retries_on_network_and_bad_json():
    from requests import RequestException

    # 第 1 次网络异常、第 2 次 200 但不是 JSON、第 3 次正常
    s = _FakeSession([RequestException("连接被重置"), _BadJsonResp(200), _Resp(200)])
    data = base._request_json(s, "get", "u", what="测试", base_delay=0.01)
    assert data == {"ok": True}
    assert s.calls == 3, f"网络异常/脏 JSON 都要重试，实际请求 {s.calls} 次"


def test_request_json_aborts_immediately_on_stop():
    cancel.clear()

    class _S:
        def get(self, url, **kw):
            cancel.set()          # 模拟用户在等待期间点了「停止」
            return _Resp(500)

    try:
        try:
            base._request_json(_S(), "get", "u", what="测试", base_delay=0.01)
        except Cancelled:
            pass
        else:
            raise AssertionError("停止后应立刻中断，而不是继续重试")
    finally:
        cancel.clear()


# --------------------------------------------------------------------------- #
# 5. study_video / study_work 的护栏（真实方法 + 假 session）
# --------------------------------------------------------------------------- #
def _with_session(script, fn, *args, **kwargs):
    """把 SessionManager.get_session 换成假 session 后执行 fn。"""
    session = _FakeSession(script)
    with mock.patch.object(base.SessionManager, "get_session",
                           classmethod(lambda cls: session)), \
         mock.patch.object(base.Chaoxing, "get_fid", lambda self: "fid"):
        return fn(*args, **kwargs), session


def test_study_video_raises_study_request_error():
    """元数据请求 3 次都失败 -> 抛明确异常，由上层（process_job）按 ERROR 处理。"""
    ch = base.Chaoxing(account=None, tiku=None)
    old_backoff = base.REQUEST_BACKOFF_BASE
    base.REQUEST_BACKOFF_BASE = 0.01
    try:
        def call():
            return ch.study_video({"courseId": "c", "clazzId": "b"},
                                  {"objectid": "o", "name": "v", "playTime": "0"}, {})

        try:
            _with_session([_Resp(500, text="down")] * 3, call)
        except StudyRequestError as exc:
            assert "读取视频信息" in str(exc), str(exc)
        else:
            raise AssertionError("元数据请求失败必须抛 StudyRequestError")
    finally:
        base.REQUEST_BACKOFF_BASE = old_backoff


def test_study_video_bad_json_ends_as_error_job():
    """200 但响应不是 JSON -> 一路收敛成 StudyResult.ERROR（不抛给主循环）。"""
    ch = base.Chaoxing(account=None, tiku=None)
    old_backoff = base.REQUEST_BACKOFF_BASE
    base.REQUEST_BACKOFF_BASE = 0.01
    try:
        def call():
            return engine.process_job(ch, {"title": "课"},
                                      {"type": "video", "jobid": "v1", "objectid": "o"},
                                      {}, 1.0)

        result, session = _with_session([_BadJsonResp(200)] * 9, call)
    finally:
        base.REQUEST_BACKOFF_BASE = old_backoff
    assert result == StudyResult.ERROR, result
    assert session.calls == 6, f"视频 3 次 + 音频兜底 3 次，实际 {session.calls} 次"


def test_study_work_submit_retries_then_raises():
    """提交请求非 200 -> 重试 3 次后抛 StudyRequestError（上层按 ERROR 处理）。"""

    class _Tiku:
        DISABLE = False
        COVER_RATE = 0.0        # 命中 0 题也满足阈值，直接提交

        def query(self, q):
            return None

        def get_submit_params(self):
            return ""

        def judgement_select(self, res):
            return True

    class _S(_FakeSession):
        """抓题页正常，提交一律 500。"""

        def __init__(self):
            super().__init__([])
            self.posts = 0

        def get(self, url, **kw):
            return _Resp(200, text="题目页")

        def post(self, url, **kw):
            self.posts += 1
            return _Resp(500, text="down")

    def _questions():
        return {"questions": [
            {"id": 1, "type": "single", "title": "Q1", "options": "A. x\nB. y",
             "answerField": {"answer1": "", "answertype1": "0"}},
        ]}

    sess = _S()
    ch = base.Chaoxing(account=None, tiku=_Tiku())
    old_session = base.SessionManager.get_session
    old_decode = base.decode_questions_info
    old_backoff = base.REQUEST_BACKOFF_BASE
    base.SessionManager.get_session = staticmethod(lambda: sess)
    base.decode_questions_info = lambda text: _questions()
    base.REQUEST_BACKOFF_BASE = 0.01
    try:
        try:
            ch.study_work({"courseId": "C", "clazzId": "cz", "title": "课"},
                          {"type": "workid", "jobid": "work-1", "enc": "e"},
                          {"knowledgeid": "p1", "ktoken": "k", "cpi": "cp"})
        except StudyRequestError as exc:
            assert "章节检测提交" in str(exc), str(exc)
        else:
            raise AssertionError("提交失败必须抛 StudyRequestError")
        assert sess.posts == 3, f"提交最多重试 3 次，实际 {sess.posts} 次"
    finally:
        base.SessionManager.get_session = old_session
        base.decode_questions_info = old_decode
        base.REQUEST_BACKOFF_BASE = old_backoff


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: {sys.exc_info()[1]}")
            annotate_exc(f"test_error_isolation.py::{fn.__name__}")
    print(f"\n{'ALL PASS' if failed == 0 else str(failed) + ' FAILED'}")
    sys.exit(1 if failed else 0)
