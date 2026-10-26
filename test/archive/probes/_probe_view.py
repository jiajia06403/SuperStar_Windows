# -*- coding: utf-8 -*-
"""

归档脚本（一次性诊断用，不参与自动测试）。
运行前请设置环境变量 CX_USER / CX_PWD，否则会以空账号登录失败。
临时诊断脚本 5：探测「成绩页」URL 形态。用完即删。"""
import json
import os
import sys

# 归档到 test/archive/probes/ 后距项目根 4 层，import 需显式指到根
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

HERE = os.path.dirname(os.path.abspath(__file__))

from api.base import SessionManager  # noqa: E402
from engine import StudyEngine  # noqa: E402

OUT = os.path.join(HERE, "_out")
os.makedirs(OUT, exist_ok=True)

def main() -> int:
    eng = StudyEngine()
    eng.login(os.getenv("CX_USER", ""), os.getenv("CX_PWD", ""), use_cookies=False, remember=False)
    s = SessionManager.get_session()
    course = eng.list_courses()[0]
    job = None
    info = {}
    for point in eng.get_course_points(course):
        jobs, info2 = eng.chaoxing.get_job_list(course, point, stop_early=True)
        works = [j for j in jobs if j.get("type") == "workid"]
        if works:
            job, info = works[0], info2
            break
    if job is None:
        print("没有作业任务点", flush=True)
        return 1
    print("job", json.dumps(job, ensure_ascii=False)[:300], flush=True)
    base = {"courseId": course["courseId"], "classId": course["clazzId"],
            "cpi": course["cpi"], "workId": str(job["jobid"]).replace("work-", ""),
            "jobid": job["jobid"], "knowledgeid": info.get("knowledgeid"),
            "enc": job.get("enc"), "ut": "s", "mooc2": "1"}
    for path in ("/mooc2/work/view", "/mooc-ans/work/view",
                 "/mooc2/work/task", "/mooc-ans/work/task"):
        for extra in ({}, {"answerId": "1"}):
            params = dict(base, **extra)
            try:
                resp = s.get("https://mooc1.chaoxing.com" + path, params=params, timeout=20)
            except Exception as exc:  # noqa: BLE001
                print(path, extra, "ERR", exc, flush=True)
                continue
            text = resp.text or ""
            tokens = ("正确答案", "我的答案", "得分", "mark_item", "mark_name",
                      "rightAnswerContent", "singleQuesId", "未交", "已完成", "作业题目")
            marks = {m: text.count(m) for m in tokens if text.count(m)}
            print(path, extra, resp.status_code, len(text), resp.url[:110], marks, flush=True)
            name = "view_" + path.strip("/").replace("/", "_") + ("_ans" if extra else "")
            with open(os.path.join(OUT, name + ".html"), "w", encoding="utf-8") as fp:
                fp.write(text)
    print("done", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
