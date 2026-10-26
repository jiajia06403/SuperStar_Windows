# -*- coding: utf-8 -*-
"""

归档脚本（一次性诊断用，不参与自动测试）。
运行前请设置环境变量 CX_USER / CX_PWD，否则会以空账号登录失败。
临时诊断脚本 8：带完整隐藏字段提交一次测验，抓已批改页。用完即删。"""
import json
import os
import sys

# 归档到 test/archive/probes/ 后距项目根 4 层，import 需显式指到根
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from urllib.parse import parse_qsl, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))

from bs4 import BeautifulSoup  # noqa: E402

from api import work_verify  # noqa: E402
from api.base import SessionManager  # noqa: E402
from engine import StudyEngine  # noqa: E402

OUT = os.path.join(HERE, "_out")
TOKENS = ("正确答案", "我的答案", "得分", "mark_item", "mark_name", "rightAnswerContent",
          "stuAnswerContent", "mark_fill", "已提交", "重新作答")

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
    point, job, info = target
    params = work_verify.build_work_params(course, job, info)
    html = session.get(work_verify.WORK_URL, params=params, timeout=20).text

    soup = BeautifulSoup(html, "lxml")
    form_tag = soup.find("form")
    data = {}
    for field in form_tag.find_all("input"):
        name = field.get("name")
        if not name or name.startswith(("answer", "answertype", "tiankongsize")):
            continue
        data[name] = field.get("value") or ""
    for key, value in parse_qsl(urlparse(form_tag.get("action") or "").query):
        data.setdefault(key, value)
    print("提交字段", list(data), flush=True)

    qids = []
    for block in soup.find_all("div", class_="singleQuesId"):
        qid = block.get("data")
        qids.append(qid)
        timu = block.find("div", class_="TiMu")
        code = (timu.get("data") if timu else "0") or "0"
        lis = block.find_all("li")
        label = (lis[0].get("aria-label") or "A") if lis else "A"
        if code in ("0", "1"):
            answer = label[:1]
        elif code == "3":
            answer = "true"
        else:
            answer = label[1:].strip() or label
        data[f"answertype{qid}"] = code
        data[f"answer{qid}"] = answer
        print("  答", qid, code, "->", answer, flush=True)
    data["pyFlag"] = ""
    data["answerwqbid"] = ",".join(qids) + ","

    resp = session.post("https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew",
                        data=data, timeout=20,
                        headers={"X-Requested-With": "XMLHttpRequest",
                                 "Referer": "https://mooc1.chaoxing.com/",
                                 "Origin": "https://mooc1.chaoxing.com"})
    print("提交", resp.status_code, (resp.text or "")[:300], flush=True)

    after = session.get(work_verify.WORK_URL, params=params, timeout=20)
    text = after.text or ""
    marks = {m: text.count(m) for m in TOKENS if text.count(m)}
    print("提交后", len(text), marks, after.url[:120], flush=True)
    print("verdicts", len(work_verify.parse_verdicts(text)),
          "mine", len(work_verify.parse_my_answers(text)), flush=True)
    with open(os.path.join(OUT, "graded_real2.html"), "w", encoding="utf-8") as fp:
        fp.write(text)
    with open(os.path.join(OUT, "graded_real2.json"), "w", encoding="utf-8") as fp:
        json.dump({"params": params, "point": point, "course": course}, fp,
                  ensure_ascii=False, indent=2)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
