# -*- coding: utf-8 -*-
"""

归档脚本（一次性诊断用，不参与自动测试）。
运行前请设置环境变量 CX_USER / CX_PWD，否则会以空账号登录失败。
临时诊断脚本 2：探测「作业列表」入口，找已批阅的成绩页。用完即删。"""
import json
import os
import sys
import re

# 归档到 test/archive/probes/ 后距项目根 4 层，import 需显式指到根
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from api.base import SessionManager  # noqa: E402
from engine import StudyEngine  # noqa: E402

USER = os.getenv("CX_USER", "")
PWD = os.getenv("CX_PWD", "")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
os.makedirs(OUT, exist_ok=True)

CANDIDATES = [
    ("/mooc-ans/work/list", {"ut": "s", "mooc2": "1", "isdisplaytable": "1"}),
    ("/mooc-ans/work/list", {"ut": "s", "mooc2": "1", "classId_key": "0"}),
    ("/mooc-ans/work/selectWorkList", {"ut": "s", "mooc2": "1"}),
    ("/mooc-ans/work/list", {"ut": "s", "mooc2": "1", "isdisplaytable": "0"}),
]
BASE = "https://mooc1.chaoxing.com"
MARKERS = ("正确答案", "我的答案", "已完成", "未交", "已交", "待批阅", "bottomList", "data=", "mark_item")

def summarize(text: str) -> str:
    return " ".join(f"{m}={text.count(m)}" for m in MARKERS)

def main() -> int:
    eng = StudyEngine()
    eng.login(USER, PWD, use_cookies=False, remember=False)
    courses = eng.list_courses()
    print(f"课程 {len(courses)} 门", flush=True)
    session = SessionManager.get_session()
    course = courses[0]
    print("课程:", course.get("title"), course, flush=True)

    for path, extra in CANDIDATES:
        params = {"courseId": course["courseId"], "clazzId": course["clazzId"],
                  "cpi": course["cpi"], "t": "", **extra}
        url = BASE + path
        try:
            resp = session.get(url, params=params, timeout=20)
        except Exception as exc:  # noqa: BLE001
            print(path, extra, "ERR", exc, flush=True)
            continue
        text = resp.text or ""
        print(f"{path} {extra} -> {resp.status_code} len={len(text)} {summarize(text)}",
              flush=True)
        name = "_".join([path.strip('/').replace('/', '_')] +
                        [f"{k}{v}" for k, v in extra.items()][:2])
        with open(os.path.join(OUT, f"list_{name}.html"), "w", encoding="utf-8") as fp:
            fp.write(text)
        hrefs = re.findall(r'href="(https?://[^"]*work[^"]*)"', text)
        datas = re.findall(r'data="([^"]*work[^"]*)"', text)
        print("   hrefs", len(hrefs), "datas", len(datas), flush=True)
        for sample in (hrefs[:2] + datas[:2]):
            print("   样例:", sample[:200], flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
