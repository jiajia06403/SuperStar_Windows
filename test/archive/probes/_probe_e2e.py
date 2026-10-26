# -*- coding: utf-8 -*-
"""

归档脚本（一次性诊断用，不参与自动测试）。
运行前请设置环境变量 CX_USER / CX_PWD，否则会以空账号登录失败。
临时端到端验证：真实账号跑一遍采集。用完即删。"""
import os
import sys

# 归档到 test/archive/probes/ 后距项目根 4 层，import 需显式指到根
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config_store import load_section  # noqa: E402
from api import collect, local_bank  # noqa: E402
from engine import StudyEngine  # noqa: E402

conf = load_section("tiku")
local_bank.configure(conf)
print("bank usable", local_bank.get_bank().usable, flush=True)
eng = StudyEngine()
eng.login(os.getenv("CX_USER", ""), os.getenv("CX_PWD", ""), use_cookies=False, remember=False)
course = eng.list_courses()[0]
points = eng.get_course_points(course)


def on_event(event):
    if event.get("message"):
        print("EVENT", event.get("kind"), event["message"], flush=True)


stats = collect.collect_course(eng.chaoxing, course, points=points,
                               on_event=on_event, max_works=3)
print("STATS", stats, flush=True)
print("BANK", local_bank.get_bank().stats(), flush=True)
