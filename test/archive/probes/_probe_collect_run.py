# -*- coding: utf-8 -*-
"""临时诊断 4：真实账号跑一遍 collect.collect_course，看真实统计。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config_store import load_section  # noqa: E402
from api import collect, local_bank  # noqa: E402
from api.base import Account, Chaoxing  # noqa: E402

conf = load_section("tiku")
local_bank.configure(conf)
bank = local_bank.get_bank()
print("bank usable:", bank.usable, "total:", bank.stats().get("total"), flush=True)

chaoxing = Chaoxing(account=Account(os.getenv("CX_USER", ""), os.getenv("CX_PWD", "")))
state = chaoxing.login(login_with_cookies=False)
print("login:", state.get("status"), "uid:", chaoxing.get_uid(), flush=True)

courses = chaoxing.get_course_list() or []
keyword = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
course = [c for c in courses if keyword and keyword in str(c.get("title"))][0] \
    if keyword else courses[0]
print("course:", course.get("title"), flush=True)
points = chaoxing.get_course_point(course["courseId"], course["clazzId"],
                                  course["cpi"]).get("points", [])


def on_event(event):
    msg = event.get("message")
    if msg:
        print("EVENT", event.get("kind"), msg, flush=True)


stats = collect.collect_course(chaoxing, course, points=points, on_event=on_event)
print("STATS", stats, flush=True)
print("BANK", bank.stats(), flush=True)
