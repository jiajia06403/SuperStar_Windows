# -*- coding: utf-8 -*-
"""临时诊断 2：不 stop_early 读全部卡片，统计每章 job；抓作业页关键标志并存样。

绕过 engine（另一个改动正在改 engine.py），直接构造 api.base.Chaoxing。
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config_store import load_section  # noqa: E402
from api import local_bank, work_verify  # noqa: E402
from api.base import Account, Chaoxing  # noqa: E402
from api.answer import Tiku  # noqa: E402

conf = load_section("tiku")
local_bank.configure(conf)

tiku = Tiku()
tiku.config_set(conf)
tiku = tiku.get_tiku_from_config()
tiku.init_tiku()
chaoxing = Chaoxing(account=Account(os.getenv("CX_USER", ""), os.getenv("CX_PWD", "")))
state = chaoxing.login(login_with_cookies=False)
print("login:", state.get("status"), state.get("msg"), flush=True)
print("uid:", chaoxing.get_uid(), flush=True)

courses = chaoxing.get_course_list() or []
print("raw courses:", [(c.get("title"), c.get("courseId")) for c in courses], flush=True)

keyword = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
course = [c for c in courses if keyword and keyword in str(c.get("title"))][0] \
    if keyword else courses[0]
print("course:", course.get("title"), course, flush=True)

point_data = chaoxing.get_course_point(course["courseId"], course["clazzId"], course["cpi"])
points = point_data.get("points", [])
print("points:", len(points), flush=True)

all_jobs = {}
for point in points:
    jobs, job_info = chaoxing.get_job_list(course, point)     # 全集卡片
    works = [j for j in (jobs or []) if j.get("type") == "workid"]
    if works:
        cards = (job_info.get("card_of_job") or {})
        print(f"[{point.get('id')}] {point.get('title')}: jobs={len(jobs)} "
              f"types={[j.get('type') for j in jobs]} "
              f"works={[(j.get('jobid'), cards.get(str(j.get('jobid')) or str(j.get('id')))) for j in works]}",
              flush=True)
    for job in works:
        all_jobs[str(job.get("jobid"))] = (job, job_info, point)

print("total works:", len(all_jobs), flush=True)

MARKERS = ["重新作答", "重新提交", "已提交", "我的答案", "正确答案", "得分", "提交作业",
           "btnSubmit", "btnSave", "btnReEdit", "myAnswerBx", "newAnswerBx", "待完成",
           "交卷", "已批阅", "查看成绩", "作业已结束"]

from api.base import SessionManager  # noqa: E402
session = SessionManager.get_session()
os.makedirs("test/archive/probes/_out", exist_ok=True)

for i, (jobid, (job, job_info, point)) in enumerate(all_jobs.items()):
    params = work_verify.build_work_params(course, job, job_info)
    resp = session.get(work_verify.WORK_URL, params=params, timeout=15)
    text = getattr(resp, "text", "") or ""
    q = work_verify.parse_questions(text)
    v = work_verify.parse_verdicts(text)
    mine = work_verify.parse_my_answers(text)
    pstate = work_verify.page_state(text, bool(q), bool(v))
    flags = [m for m in MARKERS if m in text]
    title = re.search(r"<title>(.*?)</title>", text, re.S)
    print(f"{jobid} point={point.get('title')} status={resp.status_code} len={len(text)} "
          f"q={len(q)} v={len(v)} mine={len(mine)} state={pstate} final_url={resp.url}", flush=True)
    print(f"   title={(title.group(1).strip() if title else '')!r} flags={flags}", flush=True)
    if i < 4:
        path = f"test/archive/probes/_out/work_{i}.html"
        with open(path, "w", encoding="utf-8", errors="replace") as fp:
            fp.write(text)
        print("   saved", path, flush=True)
