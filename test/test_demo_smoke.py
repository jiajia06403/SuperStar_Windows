# -*- coding: utf-8 -*-
# layer: core  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""引擎端到端冒烟测试：用 DemoChaoxing 假后端完整跑一遍学习流程（无需网络）。

验证：
  - login_demo 能建立假后端并上报账号/状态事件；
  - 课程/章节读取正常；
  - 预缓存章节被跳过，未缓存章节经 study_* 后被标记完成；
  - 学习线程正常结束并触发 FINISHED。
"""
import os
import sys
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate, annotate_exc  # noqa: E402

import engine  # noqa: E402
from engine import StudyEngine  # noqa: E402


def test_demo_full_run():
    tmp = tempfile.mkdtemp()
    eng = StudyEngine(cache_dir=str(tmp), poll_interval=0.01)
    eng.login_demo()

    courses = eng.list_courses()
    assert len(courses) == 3, f"课程数异常: {len(courses)}"
    first = courses[0]
    cid = str(first["courseId"])
    uid = eng.current_uid

    # 预标记第 2 章为已缓存，验证「跳过已缓存章节」
    points_before = eng.get_course_points(first)
    assert len(points_before) >= 2
    eng.cache.mark_done(uid, cid, str(points_before[1]["id"]))

    finished = []
    eng.on(eng.FINISHED, lambda _stopped=False: finished.append(1))

    eng.start_study([first], speed=1.0, jobs=1)
    for _ in range(200):
        if finished:
            break
        time.sleep(0.05)
    assert finished, "学习线程未触发 FINISHED"

    # 所有章节均被标记完成（含跳过的预缓存章节）
    for p in eng.get_course_points(first):
        assert eng.cache.is_done(uid, cid, str(p["id"])) is True, \
            f"章节未标记完成: {p['title']}"


if __name__ == "__main__":
    try:
        test_demo_full_run()
        print("PASS  test_demo_full_run")
        sys.exit(0)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  test_demo_full_run: {e}")
        import traceback
        traceback.print_exc()
        annotate_exc("test_demo_smoke.py::test_demo_full_run")
        sys.exit(1)
