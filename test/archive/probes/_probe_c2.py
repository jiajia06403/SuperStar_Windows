# -*- coding: utf-8 -*-
"""

归档脚本（一次性诊断用，不参与自动测试）。
运行前请设置环境变量 CX_USER / CX_PWD，否则会以空账号登录失败。
临时诊断脚本 6：扫描第二门课的作业页，找一份「已批阅」的成绩页。用完即删。"""
import json
import os
import sys

# 归档到 test/archive/probes/ 后距项目根 4 层，import 需显式指到根
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

HERE = os.path.dirname(os.path.abspath(__file__))

from api import work_verify  # noqa: E402
from api.base import SessionManager  # noqa: E402
from engine import StudyEngine  # noqa: E402

OUT = os.path.join(HERE, "_out")
TOKENS = ("正确答案", "我的答案", "得分", "mark_item", "mark_name", "rightAnswerContent",
          "stuAnswerContent", "已提交", "重新作答", "提交作业", "未交")

def main() -> int:
    eng = StudyEngine()
    eng.login(os.getenv("CX_USER", ""), os.getenv("CX_PWD", ""), use_cookies=False, remember=False)
    session = SessionManager.get_session()
    for course in eng.list_courses()[1:]:
        points = eng.get_course_points(course)
        print(f"课程 {course.get('title')}：{len(points)} 章", flush=True)
        scanned = 0
        for point in points:
            if scanned >= 60:
                break
            try:
                jobs, info = eng.chaoxing.get_job_list(course, point, stop_early=True)
            except Exception as exc:  # noqa: BLE001
                continue
            for job in [j for j in (jobs or []) if j.get("type") == "workid"]:
                if scanned >= 60:
                    break
                params = work_verify.build_work_params(course, job, info or {})
                try:
                    resp = session.get(work_verify.WORK_URL, params=params, timeout=20)
                except Exception as exc:  # noqa: BLE001
                    print("  请求失败", exc, flush=True)
                    continue
                text = resp.text or ""
                scanned += 1
                marks = {m: text.count(m) for m in TOKENS if text.count(m)}
                q = len(work_verify.parse_questions(text))
                v = len(work_verify.parse_verdicts(text))
                print(f"[{scanned}] {point.get('title', '')[:22]} q={q} v={v} {marks}",
                      flush=True)
                if marks.get("正确答案") or marks.get("rightAnswerContent") or marks.get("我的答案"):
                    name = f"graded_c2_{scanned}"
                    with open(os.path.join(OUT, name + ".html"), "w", encoding="utf-8") as fp:
                        fp.write(text)
                    with open(os.path.join(OUT, name + ".json"), "w", encoding="utf-8") as fp:
                        json.dump({"params": params, "point": point, "course": course,
                                   "url": resp.url}, fp, ensure_ascii=False, indent=2)
                    print("  -> 落盘", name, flush=True)
        print(f"扫完 {scanned} 份", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
