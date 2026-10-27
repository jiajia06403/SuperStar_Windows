# -*- coding: utf-8 -*-
"""临时诊断：真实账号跑一遍「采集题库」链路，逐层打印到底卡在哪。

用法（Windows bash）：
    CX_USER=... CX_PWD=... python test/archive/probes/_probe_collect_diag.py [课程关键词]

不写盘、不改代码，只读。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config_store import load_section  # noqa: E402
from api import local_bank, work_verify  # noqa: E402
from engine import StudyEngine  # noqa: E402

conf = load_section("tiku")
local_bank.configure(conf)
bank = local_bank.get_bank()
print("bank usable:", bank.usable, "total:", bank.stats().get("total"), flush=True)

eng = StudyEngine()
uid = eng.login(os.getenv("CX_USER", ""), os.getenv("CX_PWD", ""),
                use_cookies=False, remember=False)
print("logged in uid:", uid, flush=True)

courses = eng.list_courses()
print(f"courses: {len(courses)}", flush=True)
for c in courses:
    print("  -", c.get("title"), c.get("courseId"), flush=True)

keyword = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
target = [c for c in courses if keyword and keyword in str(c.get("title"))
          or not keyword]
course = target[0]
print("target course:", course.get("title"), flush=True)

points = eng.get_course_points(course)
print(f"points: {len(points)}", flush=True)

from api.base import SessionManager  # noqa: E402
session = SessionManager.get_session()

for point in points:
    try:
        jobs, job_info = eng.chaoxing.get_job_list(course, point, stop_early=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[{point.get('id')}] {point.get('title')}: get_job_list 异常 {exc}", flush=True)
        continue
    flags = {k: v for k, v in (job_info or {}).items()
             if k in ("notOpen", "empty", "unreadable")}
    types = [j.get("type") for j in (jobs or [])]
    works = [j for j in (jobs or []) if j.get("type") == "workid"]
    print(f"[{point.get('id')}] {point.get('title')}: jobs={len(jobs or [])} "
          f"types={types} works={len(works)} flags={flags}", flush=True)

    for job in works:
        try:
            params = work_verify.build_work_params(course, job, job_info or {})
        except Exception as exc:  # noqa: BLE001
            print("   build_work_params 失败:", type(exc).__name__, exc, flush=True)
            continue
        resp = session.get(work_verify.WORK_URL, params=params, timeout=15)
        text = getattr(resp, "text", "") or ""
        questions = work_verify.parse_questions(text)
        verdicts = work_verify.parse_verdicts(text)
        mine = work_verify.parse_my_answers(text)
        state = work_verify.page_state(text, bool(questions), bool(verdicts))
        print(f"   work={job.get('jobid')} status={resp.status_code} len={len(text)} "
              f"q={len(questions)} v={len(verdicts)} mine={len(mine)} state={state}",
              flush=True)
        if not questions:
            print("      HEAD:", text[:300].replace("\n", " "), flush=True)
        else:
            for qid, q in list(questions.items())[:2]:
                print(f"      qid={qid} type={q.get('type')} title={str(q.get('title'))[:40]!r} "
                      f"verdict={verdicts.get(qid, '<none>')!r} mine={mine.get(qid, '')!r}",
                      flush=True)
