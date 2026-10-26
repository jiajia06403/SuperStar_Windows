# -*- coding: utf-8 -*-
"""

归档脚本（一次性诊断用，不参与自动测试）。
运行前请设置环境变量 CX_USER / CX_PWD，否则会以空账号登录失败。
临时诊断脚本 3：批量探测作业列表 / 成绩页 URL 形态。用完即删。"""
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
BASE = "https://mooc1.chaoxing.com"

PATHS = [
    "/mooc2/work/list",
    "/mooc-ans/work/list",
    "/mooc2/work/selectWorkList",
    "/mooc-ans/work/selectWorkList",
    "/mooc2/work/index",
    "/mooc-ans/work/index",
    "/work/list",
    "/mooc2/work/myWorkList",
]
PARAM_STYLES = [
    ("clazzId", True), ("classId", False), ("classId", True),
]

def summarize(text: str) -> str:
    marks = ("正确答案", "我的答案", "已完成", "待批阅", "未交", "bottomList", "mark_item", "作业")
    return " ".join(f"{m}={text.count(m)}" for m in marks)

def main() -> int:
    eng = StudyEngine()
    eng.login(USER, PWD, use_cookies=False, remember=False)
    course = eng.list_courses()[0]
    session = SessionManager.get_session()
    print("课程", course["title"], flush=True)
    seen = set()
    for path in PATHS:
        for key, _mooc2 in PARAM_STYLES:
            params = {"courseId": course["courseId"], key: course["clazzId"],
                      "cpi": course["cpi"], "ut": "s"}
            if _mooc2:
                params["newMooc"] = "1"
            try:
                resp = session.get(BASE + path, params=params, timeout=20, allow_redirects=True)
            except Exception as exc:  # noqa: BLE001
                print(f"{path} {key} ERR {exc}", flush=True)
                continue
            text = resp.text or ""
            tag = f"{resp.status_code} len={len(text)}"
            print(f"{path} {key} -> {tag} {summarize(text)}", flush=True)
            if resp.status_code == 200 and len(text) > 2000 and path not in seen:
                seen.add(path)
                with open(os.path.join(OUT, "list2_" + path.strip('/').replace('/', '_') + ".html"),
                          "w", encoding="utf-8") as fp:
                    fp.write(text)
                datas = re.findall(r'data="([^"]{10,300})"', text)
                print("   data 样例:", datas[:3], flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
