# -*- coding: utf-8 -*-
"""

归档脚本（一次性诊断用，不参与自动测试）。
运行前请设置环境变量 CX_USER / CX_PWD，否则会以空账号登录失败。
临时诊断脚本 4：全量扫描作业页，区分「已批阅」和「未提交」。用完即删。"""
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

MARKS = {"正确答案": "right", "我的答案": "mine", "得分": "score",
         "mark_item": "mark_item", "mark_name": "mark_name",
         "rightAnswerContent": "rightContent", "stuAnswerContent": "stuContent",
         "提交作业": "submit_btn", "重新作答": "reanswer", "已提交": "submitted"}

def main() -> int:
    eng = StudyEngine()
    eng.login(USER, PWD, use_cookies=False, remember=False)
    courses = eng.list_courses()
    session = SessionManager.get_session()

    body = session.get("https://mooc1.chaoxing.com/mooc2/work/list",
                       params={"courseId": courses[0]["courseId"],
                               "clazzId": courses[0]["clazzId"],
                               "cpi": courses[0]["cpi"], "ut": "s"}, timeout=20).text
    print("=== mooc2/work/list 正文 ===", flush=True)
    print(body[:1200], flush=True)

    scanned = 0
    graded = 0
    for course in courses:
        try:
            points = eng.get_course_points(course)
        except Exception as exc:  # noqa: BLE001
            print("章节失败", exc, flush=True)
            continue
        print(f"课程 {course.get('title')}：{len(points)} 章", flush=True)
        for point in points:
            if scanned >= 40:
                break
            try:
                jobs, info = eng.chaoxing.get_job_list(course, point, stop_early=True)
            except Exception as exc:  # noqa: BLE001
                continue
            works = [j for j in (jobs or []) if j.get("type") == "workid"]
            for job in works:
                if scanned >= 40:
                    break
                if scanned == 0:
                    print("job 字段:", json.dumps(job, ensure_ascii=False)[:400], flush=True)
                params = work_verify.build_work_params(course, job, info or {})
                try:
                    resp = session.get(work_verify.WORK_URL, params=params, timeout=20)
                except Exception as exc:  # noqa: BLE001
                    print("  请求失败", exc, flush=True)
                    continue
                text = resp.text or ""
                scanned += 1
                hits = {label: text.count(token) for token, label in MARKS.items()
                        if text.count(token)}
                q = len(work_verify.parse_questions(text))
                v = len(work_verify.parse_verdicts(text))
                print(f"[{scanned}] {point.get('title', '')[:24]} q={q} v={v} {hits}", flush=True)
                if "right" in hits or "rightContent" in hits or "mine" in hits:
                    graded += 1
                    name = f"graded_{graded}_{str(job.get('jobid')).replace('work-', '')[:10]}"
                    with open(os.path.join(OUT, name + ".html"), "w", encoding="utf-8") as fp:
                        fp.write(text)
                    with open(os.path.join(OUT, name + ".json"), "w", encoding="utf-8") as fp:
                        json.dump({"params": params, "point": point, "course": course,
                                   "url": resp.url}, fp, ensure_ascii=False, indent=2)
                    print("  -> 已批阅页已落盘", name, flush=True)
    print(f"共扫描 {scanned} 份，已批阅 {graded} 份", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
