# -*- coding: utf-8 -*-
"""

归档脚本（一次性诊断用，不参与自动测试）。
运行前请设置环境变量 CX_USER / CX_PWD，否则会以空账号登录失败。
临时诊断脚本：真实登录 -> 找到已完成作业页 -> 原样落盘，用于定位解析问题。用完即删。"""
import json
import os
import sys

# 归档到 test/archive/probes/ 后距项目根 4 层，import 需显式指到根
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from api import work_verify  # noqa: E402
from api.base import SessionManager  # noqa: E402
from engine import StudyEngine  # noqa: E402

USER = os.getenv("CX_USER", "")
PWD = os.getenv("CX_PWD", "")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
os.makedirs(OUT, exist_ok=True)

def main() -> int:
    eng = StudyEngine()
    print("登录中...", flush=True)
    eng.login(USER, PWD, use_cookies=False, remember=False)
    courses = eng.list_courses()
    print(f"课程 {len(courses)} 门", flush=True)
    for c in courses:
        print("  -", c.get("title"), c.get("courseId"), flush=True)

    session = SessionManager.get_session()
    found = 0
    for course in courses:
        if found >= 3:
            break
        try:
            points = eng.get_course_points(course)
        except Exception as exc:  # noqa: BLE001
            print("取章节失败", course.get("title"), exc, flush=True)
            continue
        for point in points:
            if found >= 3:
                break
            try:
                jobs, info = eng.chaoxing.get_job_list(course, point, stop_early=True)
            except Exception as exc:  # noqa: BLE001
                print("取任务点失败", point.get("title"), exc, flush=True)
                continue
            works = [j for j in (jobs or []) if j.get("type") == "workid"]
            if not works:
                continue
            job = works[0]
            print(f"命中作业：{course.get('title')} / {point.get('title')} / {job.get('jobid')}",
                  flush=True)
            params = work_verify.build_work_params(course, job, info or {})
            resp = session.get(work_verify.WORK_URL, params=params, timeout=20)
            print("  status", resp.status_code, "最终URL", resp.url, "长度", len(resp.text),
                  flush=True)
            name = f"work_{found}_{str(course.get('courseId'))}_{str(job.get('jobid')).replace('work-','')[:10]}"
            with open(os.path.join(OUT, name + ".html"), "w", encoding="utf-8") as fp:
                fp.write(resp.text)
            with open(os.path.join(OUT, name + ".json"), "w", encoding="utf-8") as fp:
                json.dump({"url": resp.url, "params": params, "point": point,
                           "course": course}, fp, ensure_ascii=False, indent=2)
            questions = work_verify.parse_questions(resp.text)
            verdicts = work_verify.parse_verdicts(resp.text)
            mine = work_verify.parse_my_answers(resp.text)
            print(f"  questions={len(questions)} verdicts={len(verdicts)} mine={len(mine)}",
                  flush=True)
            if questions:
                k = list(questions)[0]
                print("  样例题：", json.dumps(questions[k], ensure_ascii=False)[:300], flush=True)
            if verdicts:
                k = list(verdicts)[0]
                print("  样例判定：", k, json.dumps(verdicts[k], ensure_ascii=False)[:200],
                      flush=True)
            found += 1
    print("已落盘", found, "份 ->", OUT, flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
