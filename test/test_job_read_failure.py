# -*- coding: utf-8 -*-
# layer: core  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""任务点「读不到」必须是失败态，不能被当成章节已完成。

回归背景：非 200 / 掉登录跳转页 / 验证码页 / 403 页 / mArg 解析失败都会让
get_job_list 返回 ([], {})，而 process_chapter 曾把空任务点判成 SUCCESS 并
cache.mark_done() —— 一次网络抖动就整章被永久跳过。

覆盖：
  1. decode_course_card：无 mArg / JSON 非法 -> ([], {"unreadable": True})，不抛异常；
  2. get_job_list：HTTP 非 200 -> unreadable；真正空章节 -> empty=True；
  3. process_chapter：([], {}) -> ERROR；unreadable -> ERROR；empty -> SUCCESS；
  4. _work_still_pending：读不到任务点 -> None，调用方按未通过处理（不得 SUCCESS）。
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate_exc  # noqa: E402

import api.base as base  # noqa: E402
import api.decode as decode  # noqa: E402
import engine  # noqa: E402
from engine import ChapterResult, StudyEngine  # noqa: E402
from api.base import StudyResult  # noqa: E402

_PROCESS_CHAPTER = engine.process_chapter

COURSE = {"courseId": "C1", "clazzId": "cz", "cpi": "cp", "title": "课"}
POINT = {"id": "p1", "title": "第1章", "has_finished": False}


# --------------------------------------------------------------------------- #
# 1. decode_course_card
# --------------------------------------------------------------------------- #
def test_decode_no_marg_is_unreadable():
    """403 / 掉登录跳转页（无 mArg）：按读取失败返回，不抛异常。"""
    for html in ("<html><body>403 Forbidden</body></html>",
                 "<script>location.href='/passport/login'</script>",
                 "<html>验证码</html>"):
        jobs, info = decode.decode_course_card(html)
        assert jobs == [] and info.get("unreadable") is True, (html, jobs, info)


def test_decode_bad_json_is_unreadable():
    """mArg 残缺导致 JSON 非法：兜底为读取失败，不得抛 JSONDecodeError。"""
    html = '<script>mArg={"defaults":{"ktoken":"k",;</script>'
    jobs, info = decode.decode_course_card(html)
    assert jobs == [] and info.get("unreadable") is True, (jobs, info)


def test_decode_not_open_unchanged():
    jobs, info = decode.decode_course_card("<html>章节未开放</html>")
    assert jobs == [] and info.get("notOpen") is True and "unreadable" not in info


def test_decode_normal_card_readable():
    html = '<script>mArg={"defaults":{"ktoken":"k"},"attachments":[]};</script>'
    jobs, info = decode.decode_course_card(html)
    assert jobs == [] and not info.get("unreadable"), info
    assert info.get("ktoken") == "k"


# 真实卡片：已通过的作业**没有 job / jobid 字段**，作业 id 只剩在 property._jobid 里
# （实测：章节测验通过后超星就是这么给的）。未通过的那份带 job:true + jobid。
_PASSED_WORK_CARD = (
    '<script>mArg={"defaults":{"ktoken":"k","knowledgeid":"1207354597","cpi":"573913559"},'
    '"attachments":[{"type":"workid","enc":"e1","mid":"m1","aid":1,'
    '"otherInfo":"nodeId_1-cpi_2",'
    '"property":{"module":"work","worktype":"workA","title":"1.6.5.4",'
    '"workid":"a72c79e8","_jobid":"work-a72c79e8"}}]};</script>')
_OPEN_WORK_CARD = (
    '<script>mArg={"defaults":{"ktoken":"k"},"attachments":[{"type":"workid",'
    '"jobid":"work-bb08","job":true,"enc":"e2","mid":"m2","aid":2,'
    '"otherInfo":"nodeId_2-cpi_2","property":{"module":"work","workid":"bb08",'
    '"_jobid":"work-bb08"}}]};</script>')


def test_decode_passed_work_is_dropped_by_default():
    """刷课默认行为不变：已通过的作业不进任务点列表（不该被重做）。"""
    jobs, _info = decode.decode_course_card(_PASSED_WORK_CARD)
    assert jobs == [], jobs


def test_decode_passed_work_kept_when_requested():
    """采集要 include_passed：否则「已通过」的作业整份丢失，统计永远偏。

    真机：244 章里 119 章已通过，采集却只看得到未通过的 101 份，于是满屏「还没提交」。
    """
    jobs, _info = decode.decode_course_card(_PASSED_WORK_CARD, include_passed=True)
    assert len(jobs) == 1, jobs
    job = jobs[0]
    assert job["type"] == "workid"
    assert job["jobid"] == "work-a72c79e8", "jobid 要从 property._jobid 兜回来"
    assert job["passed"] is True
    assert job["enc"] == "e1"


def test_decode_open_work_not_marked_passed():
    jobs, _info = decode.decode_course_card(_OPEN_WORK_CARD, include_passed=True)
    assert len(jobs) == 1 and jobs[0]["jobid"] == "work-bb08", jobs
    assert not jobs[0].get("passed"), jobs



# 服务端「本章没有更多卡片」时返回的真实页面片段：模板照常渲染，只是脚本里的兜底
# 赋值生效（mArg = $mArg），页面里没有任何 mArg={...} 数据。
_EMPTY_CARD_PAGE = """<!DOCTYPE html><html><head><title></title></head><body>
<div class="ans-cc"></div>
<script>
(function(){
  mArg = "";
  try{
    mArg = $mArg;
  }catch(e){
  }
  uParse(".ans-cc",null,mArg);
  delete window.uParse;
  window.isVideoVisibleName = 'false';
})();
</script>
</body></html>"""


def test_decode_empty_card_page_is_not_unreadable():
    """空卡片页（模板正常、mArg=$mArg 兜底）：是「没有任务点」，不是读取失败。

    回归背景：rc5 把这种页面判成 unreadable，导致每章都刷「卡片 N 读取失败」，
    并且空卡片不再累计 empty_run —— 每章白打满 7 张卡片。
    """
    jobs, info = decode.decode_course_card(_EMPTY_CARD_PAGE)
    assert jobs == [] and not info.get("unreadable"), info
    assert not info.get("notOpen")


def test_decode_truly_empty_marg_is_not_unreadable():
    """mArg={};（渲染成功但无数据）同样按空卡片处理。"""
    jobs, info = decode.decode_course_card("<script>mArg={};</script>")
    assert jobs == [] and not info.get("unreadable"), info


# --------------------------------------------------------------------------- #
# 2. get_job_list
# --------------------------------------------------------------------------- #
def _chaoxing(pages):
    """pages: 按卡片序号 num 依次返回的 (status_code, text)。"""
    calls = {"emptypage": 0}

    class _Resp:
        def __init__(self, status, text):
            self.status_code = status
            self.text = text

    class _Session:
        def __init__(self):
            self.nums = []

        def get(self, url, params=None, **kw):
            self.nums.append(params.get("num"))
            idx = min(len(self.nums) - 1, len(pages) - 1)
            status, text = pages[idx]
            return _Resp(status, text)

    sess = _Session()
    old = base.SessionManager.get_session
    base.SessionManager.get_session = staticmethod(lambda: sess)
    c = base.Chaoxing(account=None, tiku=None)
    c.rate_limiter = types.SimpleNamespace(limit_rate=lambda *a, **k: None)
    c.study_emptypage = lambda course, point: calls.__setitem__(
        "emptypage", calls["emptypage"] + 1)
    return c, calls, sess, old


def test_get_job_list_http_error_is_unreadable():
    c, calls, _sess, old = _chaoxing([(403, "<html>403</html>")])
    try:
        jobs, info = c.get_job_list(COURSE, POINT)
    finally:
        base.SessionManager.get_session = old
    assert jobs == [] and info.get("unreadable") is True, (jobs, info)
    assert calls["emptypage"] == 0, "读取失败不能当空章节上报"


def test_get_job_list_unparsable_page_is_unreadable():
    c, calls, _sess, old = _chaoxing([(200, "<html>验证码</html>")])
    try:
        jobs, info = c.get_job_list(COURSE, POINT)
    finally:
        base.SessionManager.get_session = old
    assert jobs == [] and info.get("unreadable") is True, (jobs, info)
    assert calls["emptypage"] == 0


def test_get_job_list_empty_chapter_marks_empty():
    """卡片解析成功但没有任务点：是真空章节（走 study_emptypage），不是失败。"""
    html = '<script>mArg={"defaults":{"ktoken":"k"},"attachments":[]};</script>'
    c, calls, _sess, old = _chaoxing([(200, html)])
    try:
        jobs, info = c.get_job_list(COURSE, POINT)
    finally:
        base.SessionManager.get_session = old
    assert jobs == [] and info.get("empty") is True, (jobs, info)
    assert not info.get("unreadable")
    assert calls["emptypage"] == 1


def test_get_job_list_not_open_unchanged():
    c, _calls, _sess, old = _chaoxing([(200, "<html>章节未开放</html>")])
    try:
        jobs, info = c.get_job_list(COURSE, POINT)
    finally:
        base.SessionManager.get_session = old
    assert jobs == [] and info.get("notOpen") is True and not info.get("unreadable")


def test_get_job_list_stops_on_trailing_empty_card_pages():
    """真实场景：卡片 0 有任务点，后面几张是服务端的空卡片页。

    必须做到：任务点照常读出、不报 unreadable、连续空卡片后提前收手（不再打满 7 张）。
    """
    video_card = (
        '<script>mArg="";try{mArg={"defaults":{"ktoken":"k"},"attachments":[{"type":"video",'
        '"job":"1","jobid":"v1","mid":"m1","objectId":"o1","otherInfo":"x",'
        '"property":{"name":"1.1"}}]};}catch(e){}</script>'
    )
    c, calls, sess, old = _chaoxing([(200, video_card), (200, _EMPTY_CARD_PAGE)])
    try:
        jobs, info = c.get_job_list(COURSE, POINT)
    finally:
        base.SessionManager.get_session = old
    assert [j["jobid"] for j in jobs] == ["v1"], jobs
    assert not info.get("unreadable") and not info.get("empty"), info
    assert calls["emptypage"] == 0
    # 1 张有任务点 + CARD_MAX_EMPTY 张空卡片后收手
    assert len(sess.nums) == 1 + base.Chaoxing.CARD_MAX_EMPTY, sess.nums


# --------------------------------------------------------------------------- #
# 2b. 请求量：别再把 7 张卡片无条件全打一遍
# --------------------------------------------------------------------------- #
_EMPTY_CARD = '<script>mArg={"defaults":{"ktoken":"k"},"attachments":[]};</script>'
_VIDEO_CARD = (
    '<script>mArg={"defaults":{"ktoken":"k"},"attachments":[{"type":"video",'
    '"job":"1","jobid":"v1","mid":"m1","objectId":"o1","otherInfo":"x",'
    '"property":{"name":"1.1"}}]};</script>'
)


def test_full_read_stops_after_consecutive_empty_cards():
    """全卡片都为空时，最多只请求 CARD_MAX_EMPTY 张，而不是固定 7 次。"""
    c, calls, sess, old = _chaoxing([(200, _EMPTY_CARD)])
    try:
        jobs, info = c.get_job_list(COURSE, POINT)
    finally:
        base.SessionManager.get_session = old
    assert jobs == [] and info.get("empty") is True, (jobs, info)
    assert calls["emptypage"] == 1
    assert len(sess.nums) == base.Chaoxing.CARD_MAX_EMPTY, sess.nums


def test_partial_read_never_declares_empty_chapter():
    """只请求指定卡片时不得断言「本章无任务点」：不打空页、不写 empty。"""
    c, calls, sess, old = _chaoxing([(200, _EMPTY_CARD)])
    try:
        jobs, info = c.get_job_list(COURSE, POINT, nums="0")
    finally:
        base.SessionManager.get_session = old
    assert sess.nums == ["0"], sess.nums
    assert jobs == [] and not info.get("empty") and not info.get("unreadable"), info
    assert calls["emptypage"] == 0, "半份数据不能上报空章节"


def test_stop_when_short_circuits_card_requests():
    """命中 stop_when 立即收手，后续卡片不再请求。"""
    c, _calls, sess, old = _chaoxing([(200, _VIDEO_CARD)])
    try:
        jobs, info = c.get_job_list(COURSE, POINT, stop_when=lambda j, i: True)
    finally:
        base.SessionManager.get_session = old
    assert sess.nums == ["0"], sess.nums
    assert [j["jobid"] for j in jobs] == ["v1"], jobs


def test_track_cards_records_card_of_each_job():
    """track_cards 给出 jobid -> 卡片序号，供复检时只回读相关卡片。"""
    c, _calls, _sess, old = _chaoxing([(200, _VIDEO_CARD), (200, _EMPTY_CARD)])
    try:
        _jobs, info = c.get_job_list(COURSE, POINT, track_cards=True)
    finally:
        base.SessionManager.get_session = old
    assert info.get("card_of_job") == {"v1": "0"}, info


# --------------------------------------------------------------------------- #
# 3. process_chapter
# --------------------------------------------------------------------------- #
class _Backend:
    """最小后端：rate_limiter + tiku + 可编排的 get_job_list。"""

    def __init__(self, script):
        self.rate_limiter = types.SimpleNamespace(limit_rate=lambda *a, **k: None)
        self.tiku = types.SimpleNamespace(DISABLE=False)
        self.script = list(script)
        self.calls = 0
        self.study_calls = 0

    def get_job_list(self, course, point, **kwargs):
        self.calls += 1
        if len(self.script) > 1:
            return self.script.pop(0)
        return self.script[0]

    def study_work(self, *a, **k):
        self.study_calls += 1
        return StudyResult.SUCCESS


def test_process_chapter_empty_job_list_is_error():
    """([], {}) 必须判失败：绝不能当作「本章无任务点」而标记完成。"""
    be = _Backend([([], {})])
    r = _PROCESS_CHAPTER(be, COURSE, POINT, 1.0, work_retry=1)
    assert r == ChapterResult.ERROR, r


def test_process_chapter_unreadable_is_error():
    be = _Backend([([], {"unreadable": True})])
    r = _PROCESS_CHAPTER(be, COURSE, POINT, 1.0, work_retry=1)
    assert r == ChapterResult.ERROR, r


def test_process_chapter_empty_chapter_still_success():
    """真正没有任务点的章节（empty=True）保持原行为。"""
    be = _Backend([([], {"empty": True})])
    r = _PROCESS_CHAPTER(be, COURSE, POINT, 1.0, work_retry=1)
    assert r == ChapterResult.SUCCESS, r


def test_process_chapter_not_open_unchanged():
    be = _Backend([([], {"notOpen": True})])
    r = _PROCESS_CHAPTER(be, COURSE, POINT, 1.0, work_retry=1)
    assert r == ChapterResult.NOT_OPEN, r


def test_work_still_pending_none_on_unreadable():
    be = _Backend([([], {"unreadable": True})])
    assert engine._work_still_pending(be, COURSE, POINT) is None


def test_work_card_nums_and_narrow_reread():
    """回读章节检测只请求 workid 所在卡片；没有归属信息时退回全量(None)。"""
    jobs = [{"type": "video", "jobid": "v"}, {"type": "workid", "jobid": "w"}]
    assert engine._work_card_nums(jobs, {"card_of_job": {"v": "0", "w": "2"}}) == "2"
    # 老数据 / 未开启 track_cards：无从收敛，退回全量读取（None）
    assert engine._work_card_nums(jobs, {"knowledgeid": "p1"}) is None

    seen = []

    class _CountingBackend:
        def get_job_list(self, course, point, **kwargs):
            seen.append(kwargs.get("nums"))
            return [], {"knowledgeid": "p1"}

    nums = engine._work_card_nums(jobs, {"card_of_job": {"v": "0", "w": "2"}})
    assert engine._work_still_pending(_CountingBackend(), COURSE, POINT, nums=nums) is False
    assert seen == ["2"], seen


def test_process_chapter_unreadable_reread_is_not_success():
    """回读不到任务点 -> 按未通过处理，重答次数用尽后是 UNVERIFIED，不是 SUCCESS。"""
    be = _Backend([([{"type": "workid", "jobid": "w"}], {"knowledgeid": "p1"}),
                   ([], {"unreadable": True})])
    r = _PROCESS_CHAPTER(be, COURSE, POINT, 1.0, work_retry=0)
    assert r == ChapterResult.UNVERIFIED, r
    assert be.study_calls == 1


def test_study_course_does_not_mark_done_on_read_failure():
    """端到端：读不到任务点的章节不得写入完成缓存。"""
    tmp = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=tmp, poll_interval=0.01)
    eng.current_uid = "u9"
    eng.work_retry = 0
    be = _Backend([([], {})])
    be.get_uid = lambda: "u9"
    be.get_course_point = lambda *a, **k: {"points": [dict(POINT)]}
    eng.chaoxing = be
    engine.process_chapter = _PROCESS_CHAPTER
    try:
        eng._study_course(COURSE)
    finally:
        pass
    assert eng.cache.is_done("u9", "C1", "p1") is False, "读取失败不能记为已完成"


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
            annotate_exc(f"test_job_read_failure.py::{fn.__name__}")
    print(f"\n{'ALL PASS' if failed == 0 else str(failed) + ' FAILED'}")
    sys.exit(1 if failed else 0)
