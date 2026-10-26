# -*- coding: utf-8 -*-
"""采集题库测试（api/collect.py + api/work_verify 的题目/我的答案解析）。

覆盖：
  1. 已完成作业页：判对存「我的答案」、判错存「正确答案」、判断题字母转文字；
  2. 只加 GET 不提交：请求参数正确，全程没有 POST；
  3. 未批阅 / 作业已关闭 / 状态码异常 -> 计为「无法识别」且不入库；
  4. 中断（should_stop）与 --max-works 上限；
  5. 关闭本地题库时直接返回错误、一个请求都不发；
  6. 采集结果落在 <课程>.json（按课程共享，不按账号分文件夹），能按指纹命中；
  7. 题库已有正确答案的题只跳过、不重复写。
"""
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate_exc  # noqa: E402

import api.collect as collect  # noqa: E402
import api.local_bank as lb  # noqa: E402
import api.work_verify as wv  # noqa: E402

TMP = tempfile.mkdtemp(prefix="collect_")
_seq = 0

OPTS = "A. 甲选项\nB. 乙选项"
OPTS_JUDGE = "A. 对\nB. 错"

COURSE = {"title": "高等数学", "courseId": "246831735", "clazzId": "107515845", "cpi": "338350298"}
POINT = {"id": "913820156", "title": "第一章"}

JOB = {"type": "workid", "jobid": "work-07647c38d8de4c648a9277c5bed7075a",
       "enc": "1d826aab06d44a1198fc983ed3d243b1", "otherinfo": "", "mid": "1", "aid": "2"}

# 一份「已批阅」的作业页：q11 判对、q12 判错但给了正确答案、q13 判断题判错、q14 无判分
PAGE_OK = """
<div class="singleQuesId" data="11">
  <div class="TiMu" data="0">
    <div class="Zy_TItle">1. 下列说法正确的是（2.0分）</div>
    <ul><li>A. 甲选项</li><li>B. 乙选项</li></ul>
  </div>
  <div><span class="dui"></span> 我的答案：B 正确答案：B 得分：2.0分 满分：2.0分</div>
</div>
<div class="singleQuesId" data="12">
  <div class="TiMu" data="0">
    <div class="Zy_TItle">2. 另一道题（2.0分）</div>
    <ul><li>A. 甲选项</li><li>B. 乙选项</li></ul>
  </div>
  <div><span class="cuo"></span> 我的答案：B 正确答案：A 得分：0.0分 满分：2.0分</div>
</div>
<div class="singleQuesId" data="13">
  <div class="TiMu" data="3">
    <div class="Zy_TItle">3. 判断题（1.0分）</div>
    <ul><li>A. 对</li><li>B. 错</li></ul>
  </div>
  <div><span class="cuo"></span> 我的答案：A 正确答案：B</div>
</div>
<div class="singleQuesId" data="14">
  <div class="TiMu" data="0">
    <div class="Zy_TItle">4. 还没批阅的题</div>
    <ul><li>A. 甲选项</li><li>B. 乙选项</li></ul>
  </div>
</div>
"""

# 只判对、不公布正确答案：应当用「我的答案」（选项乱序，靠文本重映射）
PAGE_NO_ANSWER = """
<div class="singleQuesId" data="21">
  <div class="TiMu" data="0">
    <div class="Zy_TItle">1. 下列说法正确的是（2.0分）</div>
    <ul><li>B. 乙选项</li><li>A. 甲选项</li></ul>
  </div>
  <div><span class="dui"></span> 我的答案：A</div>
</div>
"""


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class _Limiter:
    def limit_rate(self):
        pass


class _Resp:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


class _Session:
    """只实现 GET：采集若敢 POST 就直接 AttributeError，测试会立刻发现。"""

    def __init__(self, pages=None):
        self.pages = dict(pages or {})
        self.calls = []

    def get(self, url, params=None, timeout=None):
        assert "api/work" in url, url
        self.calls.append(dict(params or {}))
        return self.pages.get(params.get("jobid")) or _Resp(status_code=500)


class _Chaoxing:
    rate_limiter = _Limiter()
    account = SimpleNamespace(username="13800000000")

    def __init__(self, jobs=None, points=None):
        self.jobs = jobs if jobs is not None else {POINT["id"]: [dict(JOB)]}
        self.points = points if points is not None else [dict(POINT)]
        self.job_calls = []

    def get_course_point(self, courseid, clazzid, cpi):
        return {"points": [dict(p) for p in self.points]}

    def get_job_list(self, course, point, stop_early=False):
        self.job_calls.append((point["id"], stop_early))
        return [dict(j) for j in self.jobs.get(point["id"], [])], {
            "knowledgeid": "913820156", "ktoken": "ktoken-value", "cpi": "338350298"}


def _store(enabled=True):
    global _seq
    _seq += 1
    return lb.BankStore(root=os.path.join(TMP, f"store{_seq}"), enabled=enabled,
                        true_word="正确", false_word="错误",
                        legacy=os.path.join(TMP, f"legacy{_seq}.json"))


def _with_bank(store):
    """把单例换成测试题库（collect 内部走 get_bank()）。"""
    class _Ctx:
        def __enter__(self):
            self.old = lb._BANK
            lb._BANK = store
            return store

        def __exit__(self, *exc):
            lb._BANK = self.old
            return False
    return _Ctx()


def _collect(store, html=PAGE_OK, jobs=None, points=None, **kwargs):
    session = _Session({JOB["jobid"]: _Resp(html)})
    chaoxing = _Chaoxing(jobs=jobs, points=points)
    with _with_bank(store):
        stats = collect.collect_course(chaoxing, dict(COURSE), session=session, **kwargs)
    return stats, session, chaoxing


# --------------------------------------------------------------------------- #
# 1. 收录口径
# --------------------------------------------------------------------------- #
def test_collect_stores_my_answer_when_correct():
    store = _store()
    stats, _session, _cx = _collect(store)
    assert stats["read"] == 1 and stats["learned"] == 3, stats
    assert store.lookup("1. 下列说法正确的是（2.0分）", "single", OPTS) == "B"


def test_collect_stores_correct_answer_when_wrong():
    """判错但页面给了正确答案 -> 存正确答案（下次直接答对）。"""
    store = _store()
    _collect(store)
    assert store.lookup("2. 另一道题（2.0分）", "single", OPTS) == "A"


def test_collect_translates_judgement_letters():
    store = _store()
    _collect(store)
    answer = store.lookup("3. 判断题（1.0分）", "judgement", OPTS_JUDGE)
    assert answer == "错误", answer          # 正确答案 B 对应选项「错」


def test_collect_uses_my_answer_when_no_correct_answer_shown():
    store = _store()
    _collect(store, html=PAGE_NO_ANSWER)
    # 采集页选项是 B/A 乱序、答案是 A（=甲选项）；用 A/B 顺序查也应命中 A
    answer = store.lookup("1. 下列说法正确的是（2.0分）", "single", OPTS)
    assert answer == "A", answer


def test_unjudged_question_is_skipped():
    store = _store()
    stats, _session, _cx = _collect(store)
    assert store.lookup("4. 还没批阅的题", "single", OPTS) is None
    assert stats["questions"] == 4, "题目应全部计入统计，只是没判分的题不入库"


# --------------------------------------------------------------------------- #
# 2. 只读：请求参数正确，没有 POST
# --------------------------------------------------------------------------- #
def test_request_params_match_work_page():
    store = _store()
    _stats, session, chaos = _collect(store)
    assert len(session.calls) == 1, session.calls
    params = session.calls[0]
    assert params["workId"] == "07647c38d8de4c648a9277c5bed7075a"
    assert params["enc"] == JOB["enc"]
    assert params["cpi"] == "338350298"
    assert params["clazzId"] == COURSE["clazzId"]
    assert params["courseid"] == COURSE["courseId"]
    assert chaos.job_calls[0][1] is True, "采集应走 stop_early 省请求"


# --------------------------------------------------------------------------- #
# 3. 无法识别的作业 / 异常
# --------------------------------------------------------------------------- #
def test_page_without_verdicts_is_counted_not_stored():
    store = _store()
    stats, _session, _cx = _collect(store, html="<html><body>作业已结束</body></html>")
    assert stats["read"] == 0 and stats["learned"] == 0, stats
    assert stats["unreadable"] == 1, stats
    assert store.stats()["total"] == 0


def test_bad_status_counts_as_unreadable():
    store = _store()
    session = _Session({})                     # 没有页面 -> _Resp(status_code=500)
    chaos = _Chaoxing()
    with _with_bank(store):
        stats = collect.collect_course(chaos, dict(COURSE), session=session)
    assert stats["unreadable"] == 1 and stats["errors"] == 0, stats


def test_stop_before_start_sends_no_request():
    store = _store()
    stats, session, _cx = _collect(store, should_stop=lambda: True)
    assert stats["stopped"] is True and stats["works"] == 0
    assert session.calls == [], "已停止就不该再发请求"


def test_max_works_limits_requests():
    store = _store()
    jobs = {POINT["id"]: [dict(JOB, jobid="work-a"), dict(JOB, jobid="work-b")]}
    session = _Session({"work-a": _Resp(PAGE_OK), "work-b": _Resp(PAGE_OK)})
    chaos = _Chaoxing(jobs=jobs)
    with _with_bank(store):
        stats = collect.collect_course(chaos, dict(COURSE), session=session, max_works=1)
    assert stats["works"] == 1 and stats["stopped"] is True, stats
    assert len(session.calls) == 1, session.calls


# --------------------------------------------------------------------------- #
# 4. 开关与文件布局
# --------------------------------------------------------------------------- #
def test_disabled_bank_returns_error_without_requests():
    store = _store(enabled=False)
    stats, session, _cx = _collect(store)
    assert stats.get("error"), stats
    assert session.calls == [] and not os.path.isdir(store.root)


def test_answers_land_in_course_file():
    """按课程一个文件，不按账号分文件夹。"""
    store = _store()
    _collect(store)
    courses = store.stats()["courses"]
    assert len(courses) == 1, courses
    rel = os.path.relpath(courses[0]["path"], store.root).replace("\\", "/")
    assert rel == "高等数学_246831735.json", rel
    assert not [n for n in os.listdir(store.root)
                if os.path.isdir(os.path.join(store.root, n))], "不应按账号建目录"


def test_existing_answers_are_skipped_on_second_collect():
    """再采一次：题库里已有且一致的不重写，库内容不变。"""
    store = _store()
    first, _s1, _c1 = _collect(store)
    assert first["learned"] == 3, first
    total = store.stats()["total"]

    second, _s2, _c2 = _collect(store)
    assert second["learned"] == 0, second
    assert second["skipped"] == 4, second          # 4 题全部没写入
    assert store.stats()["total"] == total, "答案一致时不应重复写入"


def test_collect_overwrites_wrong_answer_with_page_answer():
    """采集时页面答案最权威：题库里那条与页面不一致就被覆写纠正。"""
    store = _store()
    with _with_bank(store):
        store.set_scope(course="高等数学", course_id="246831735")
        loaded = store.learn([{"qid": "1", "title": "1. 下列说法正确的是（2.0分）",
                               "type": "single", "options": OPTS, "answer": "A"}],
                             {"1": True})
        assert loaded == 1
        assert store.lookup("1. 下列说法正确的是（2.0分）", "single", OPTS) == "A"
        stats, _session, _cx = _collect(store)
    assert stats["learned"] >= 1, stats
    assert store.lookup("1. 下列说法正确的是（2.0分）", "single", OPTS) == "B", \
        "页面给出的答案应覆盖库里的错误答案"


def test_points_are_fetched_when_not_given():
    store = _store()
    session = _Session({JOB["jobid"]: _Resp(PAGE_OK)})
    chaos = _Chaoxing()
    with _with_bank(store):
        # 用空章节列表绕过「不传 points」的分支会取不到作业，这里直接验证取章节走的是课程信息
        stats = collect.collect_course(chaos, dict(COURSE), points=None, session=session)
    assert stats["points"] == 1 and stats["read"] == 1, stats


def test_collect_courses_merges_stats():
    store = _store()
    chaos = _Chaoxing()
    session = _Session({JOB["jobid"]: _Resp(PAGE_OK)})
    with _with_bank(store):
        merged = collect.collect_courses(chaos, [dict(COURSE), dict(COURSE, title="线性代数",
                                                                   courseId="2")],
                                         session=session)
    assert len(merged["courses"]) == 2, merged
    # 第二门课的题目与第一门完全相同 -> 题库答案已一致，不再写入
    assert merged["learned"] == 3, merged
    assert merged["courses"][1]["skipped"] == 4, merged["courses"][1]
    assert len(store.stats()["courses"]) == 1, "同样的题不会另建课程文件"
    # 第二门课没写自己的文件，但查询能通过兜底命中
    assert store.lookup("1. 下列说法正确的是（2.0分）", "single", OPTS) == "B"


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    try:
        for fn in tests:
            try:
                fn()
                print(f"PASS  {fn.__name__}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {fn.__name__}: {exc}")
                annotate_exc(f"test_collect.py::{fn.__name__}")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {fn.__name__}: unexpected")
                annotate_exc(f"test_collect.py::{fn.__name__}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("ALL PASS" if not failed else f"FAILED: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
