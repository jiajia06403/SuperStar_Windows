# -*- coding: utf-8 -*-
"""

归档脚本（一次性诊断用，不参与自动测试）。
运行前请设置环境变量 CX_USER / CX_PWD，否则会以空账号登录失败。
临时诊断脚本 7：在测试账号上真正提交一份章节测验，抓「已批改」页面结构。用完即删。"""
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
from api.decode import decode_questions_info  # noqa: E402
from engine import StudyEngine  # noqa: E402

OUT = os.path.join(HERE, "_out")
TOKENS = ("正确答案", "我的答案", "得分", "mark_item", "mark_name", "rightAnswerContent",
          "stuAnswerContent", "已提交", "重新作答", "singleQuesId", "mark_fill")

def main() -> int:
    eng = StudyEngine()
    eng.login(os.getenv("CX_USER", ""), os.getenv("CX_PWD", ""), use_cookies=False, remember=False)
    session = SessionManager.get_session()
    course = eng.list_courses()[0]
    target = None
    for point in eng.get_course_points(course):
        jobs, info = eng.chaoxing.get_job_list(course, point, stop_early=True)
        works = [j for j in (jobs or []) if j.get("type") == "workid"]
        if works:
            target = (point, works[0], info)
            break
    if not target:
        print("没找到作业", flush=True)
        return 1
    point, job, info = target
    print("目标作业", point.get("title"), job.get("jobid"), flush=True)

    params = work_verify.build_work_params(course, job, info)
    html = session.get(work_verify.WORK_URL, params=params, timeout=20).text
    form = decode_questions_info(html)
    questions = form.pop("questions", [])
    print("题目数", len(questions), flush=True)
    letters = "ABCD"
    for q in questions:
        qid = q["id"]
        answers = [li[:1] for li in str(q["options"]).split("\n") if li]
        pick = answers[0] if answers else "A"
        form[f"answer{qid}"] = pick[0] if pick else letters[0]
        form[f"answertype{qid}"] = q["answerField"][f"answertype{qid}"]
        print("  答", qid, q["type"], "->", form[f"answer{qid}"], flush=True)
    form["pyFlag"] = ""
    resp = session.post(
        "https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew",
        data=form,
        headers={
            "Host": "mooc1.chaoxing.com",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://mooc1.chaoxing.com",
        },
        timeout=20,
    )
    print("提交状态", resp.status_code, (resp.text or "")[:300], flush=True)

    after = session.get(work_verify.WORK_URL, params=params, timeout=20)
    text = after.text or ""
    marks = {m: text.count(m) for m in TOKENS if text.count(m)}
    print("提交后页面", len(text), marks, after.url[:160], flush=True)
    q = work_verify.parse_questions(text)
    v = work_verify.parse_verdicts(text)
    mine = work_verify.parse_my_answers(text)
    print(f"questions={len(q)} verdicts={len(v)} mine={len(mine)}", flush=True)
    if v:
        print("verdicts 样例:", json.dumps(dict(list(v.items())[:3]), ensure_ascii=False)[:400],
              flush=True)
    with open(os.path.join(OUT, "graded_real.html"), "w", encoding="utf-8") as fp:
        fp.write(text)
    with open(os.path.join(OUT, "graded_real.json"), "w", encoding="utf-8") as fp:
        json.dump({"params": params, "point": point, "course": course}, fp,
                  ensure_ascii=False, indent=2)
    print("已落盘 graded_real.html", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
