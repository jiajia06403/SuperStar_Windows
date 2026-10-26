# -*- coding: utf-8 -*-
"""一次性诊断：对指定账号的「安全教育」课程，逐份作业核验两件事：
  1) 章节任务点是否已完成（has_finished，即用户说的「拿到了任务点」）
  2) 作业页实际是「已提交/未提交/已批阅/已关闭」（page_state）
  并统计「能否拿到答案」（ verdicts 非空）。
凭据走环境变量 CX_USER / CX_PWD（与仓库约定一致，避免写死账号）。
"""
import json
import os
import sys
import time

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

# 用户在对话里提到的「1.x.x.x」是课程 id 占位；这里自动按关键词匹配课程
COURSE_KEY = os.getenv("CX_COURSE", "安全")

# 直接打印的「提交/作答」信号词，避免只依赖启发式判定
_MARKS = ["待完成", "提交作业", "btnSubmit", "btnSave", "暂时保存", "尚未提交",
         "提交答案", "提交试卷", "重新作答", "重新提交", "已提交", "btnReEdit",
         "myAnswerBx", "newAnswerBx", "我的答案", "正确答案", "得分",
         "作业已结束", "作业已过期"]


def scan_marks(text):
    return [m for m in _MARKS if m in text]


def main() -> int:
    eng = StudyEngine()
    print("登录中...", flush=True)
    eng.login(USER, PWD, use_cookies=False, remember=False)
    uid = eng.current_uid
    courses = eng.list_courses()
    print(f"课程 {len(courses)} 门，当前 uid={uid}", flush=True)

    course = None
    for c in courses:
        title = str(c.get("title", ""))
        if COURSE_KEY in title:
            course = c
            break
    if not course:
        print("未匹配到含关键词的课程：", COURSE_KEY, flush=True)
        for c in courses:
            print("  -", c.get("title"), c.get("courseId"), flush=True)
        return 1

    print(f"命中课程：{course.get('title')}  courseId={course.get('courseId')}  "
          f"clazzId={course.get('clazzId')}  cpi={course.get('cpi')}", flush=True)

    session = SessionManager.get_session()
    points = eng.get_course_points(course)
    print(f"章节 {len(points)} 个", flush=True)

    rows = []
    for pi, point in enumerate(points, 1):
        try:
            jobs, info = eng.chaoxing.get_job_list(course, point, stop_early=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  [章节取任务点失败] {point.get('title')}: {exc}", flush=True)
            continue
        works = [j for j in (jobs or []) if j.get("type") == "workid"]
        if not works:
            continue
        hf = bool(point.get("has_finished"))
        for job in works:
            params = work_verify.build_work_params(course, job, info or {})
            try:
                resp = session.get(work_verify.WORK_URL, params=params, timeout=20)
            except Exception as exc:  # noqa: BLE001
                print(f"  [请求失败] {point.get('title')}/{job.get('jobid')}: {exc}",
                      flush=True)
                rows.append({"chapter": point.get("title"), "has_finished": hf,
                             "jobid": job.get("jobid"), "state": "request_error"})
                continue
            text = getattr(resp, "text", "") or ""
            questions = work_verify.parse_questions(text)
            verdicts = work_verify.parse_verdicts(text)
            mine = work_verify.parse_my_answers(text)
            state = work_verify.page_state(text, bool(questions), bool(verdicts))
            marks = scan_marks(text)
            row = {
                "chapter": point.get("title"), "has_finished": hf,
                "jobid": str(job.get("jobid")).replace("work-", ""),
                "len": len(text), "state": state,
                "n_q": len(questions), "n_v": len(verdicts), "n_mine": len(mine),
                "marks": marks,
            }
            rows.append(row)
            tag = "✓答案" if verdicts else "—无答案"
            print(f"  [{pi:>3}] {point.get('title')[:28]:<28} 任务点完成={hf!s:>5} "
                  f"state={state:<11} 题={len(questions):>3} 判定={len(verdicts):>2} "
                  f"{tag}  len={len(text)}", flush=True)
            time.sleep(0.15)

    # ---- 汇总 ----
    total = len(rows)
    by_state = {}
    by_hf = {"True": 0, "False": 0, "unknown": 0}
    hf_x_state = {}
    ans_ok = 0
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        hfk = str(r.get("has_finished"))
        if hfk not in ("True", "False"):
            hfk = "unknown"
        by_hf[hfk] = by_hf.get(hfk, 0) + 1
        key = (hfk, r["state"])
        hf_x_state[key] = hf_x_state.get(key, 0) + 1
        if r["state"] in (work_verify.STATE_OK,):
            ans_ok += 1

    print("\n================ 汇总 ================", flush=True)
    print(f"课程：{course.get('title')}  (courseId={course.get('courseId')})", flush=True)
    print(f"作业份数（workid 任务点总数）：{total}", flush=True)
    print(f"按 page_state：{by_state}", flush=True)
    print(f"按任务点 has_finished：{by_hf}", flush=True)
    print("交叉表 (任务点完成, 页面状态) -> 份数：", flush=True)
    for k in sorted(hf_x_state):
        print(f"    {k} -> {hf_x_state[k]}", flush=True)
    print(f"能拿到答案（state=ok）的份数：{ans_ok}", flush=True)
    print(f"拿不到答案的份数：{total - ans_ok}", flush=True)

    with open(os.path.join(OUT, "verify_state.json"), "w", encoding="utf-8") as fp:
        json.dump({"course": course.get("title"), "courseId": course.get("courseId"),
                   "total": total, "by_state": by_state, "by_hf": by_hf,
                   "hf_x_state": {f"{k[0]}|{k[1]}": v for k, v in hf_x_state.items()},
                   "ans_ok": ans_ok, "rows": rows},
                  fp, ensure_ascii=False, indent=2)
    print(f"\n明细已存：{os.path.join(OUT, 'verify_state.json')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
