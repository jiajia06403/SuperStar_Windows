# -*- coding: utf-8 -*-
"""演示用假后端（DemoChaoxing）。

无需真实超星账号 / 网络即可驱动整套引擎与 GUI 流程，用于：
  - 本地预览界面
  - GUI 事件接线 / 布局联调
  - 界面自检（gui/selftest.py，测试包 --selftest）
  - 引擎端到端冒烟测试（见 test/test_demo_smoke.py）

注意：本模块**只随源码与测试包分发**。发行包构建时用 --exclude-module 排除，
界面上不会出现「演示」入口。

所有 study_* 方法直接返回 SUCCESS，章节任务点由 get_job_list 提供，
从而让真实的学习编排（_study_course / process_chapter / 进度缓存）完整跑通。
"""
from api.base import StudyResult


class _DummyLimiter:
    """替代 Chaoxing.rate_limiter，演示模式下不真正限速。"""

    def limit_rate(self, *args, **kwargs):  # noqa: D401
        return None


class DemoChaoxing:
    def __init__(self, not_open_ids=()):
        self._uid = "demo_10086"
        self.rate_limiter = _DummyLimiter()
        # 演示「未开放」章节：默认没有，测试可传 point id 造出未开放章节
        self.not_open_ids = {str(x) for x in (not_open_ids or ())}
        self._courses = [
            {
                "courseId": "C1001", "clazzId": "B2001", "cpi": "P3001",
                "title": "高等数学（上）", "teacher": "张伟",
            },
            {
                "courseId": "C1002", "clazzId": "B2002", "cpi": "P3002",
                "title": "大学英语（IV）", "teacher": "李娜",
            },
            {
                "courseId": "C1003", "clazzId": "B2003", "cpi": "P3003",
                "title": "马克思主义基本原理", "teacher": "王强",
            },
        ]
        self._points = {
            "C1001": [
                {"id": "pt1", "title": "第一章 函数与极限", "has_finished": True},
                {"id": "pt2", "title": "第二章 导数与微分", "has_finished": False},
                {"id": "pt3", "title": "第三章 中值定理与导数的应用", "has_finished": False},
                {"id": "pt4", "title": "第四章 不定积分", "has_finished": False},
                {"id": "pt5", "title": "第五章 定积分", "has_finished": False},
            ],
            "C1002": [
                {"id": "en1", "title": "Unit 1 The Language of Music", "has_finished": False},
                {"id": "en2", "title": "Unit 2 The Pressure to Succeed", "has_finished": False},
                {"id": "en3", "title": "Unit 3 Friendship", "has_finished": True},
                {"id": "en4", "title": "Unit 4 The Joy of Travel", "has_finished": False},
            ],
            "C1003": [
                {"id": "ma1", "title": "导论 马克思主义的产生", "has_finished": False},
                {"id": "ma2", "title": "第一章 世界的物质性", "has_finished": False},
                {"id": "ma3", "title": "第二章 事物的普遍联系", "has_finished": False},
            ],
        }

    # ---- 与 Chaoxing 兼容的接口 ----
    def get_uid(self):
        return self._uid

    def login(self, login_with_cookies=False):
        return {"status": True, "msg": "登录成功(演示)"}

    def get_course_list(self):
        return [dict(c) for c in self._courses]

    def get_course_point(self, courseid, clazzid, cpi):
        cid = str(courseid)
        pts = self._points.get(cid, [])
        return {"points": [dict(p) for p in pts]}

    def get_job_list(self, course, point, **kwargs):
        if str(point.get("id")) in self.not_open_ids:
            return [], {"notOpen": True}
        # 已完成的章节不再返回任务点（与真实平台 isPassed 语义一致）
        if point.get("has_finished"):
            return [], {"notOpen": False}
        # 提供一个视频任务点，让 process_chapter 走完整 study 流程
        jobs = [
            {
                "type": "video",
                "jobid": f"v_{point['id']}",
                "objectid": "obj_demo",
                "otherinfo": "",
                "title": point.get("title", ""),
                "rt": "1",
                "videoFaceCaptureEnc": "",
                "attDuration": "",
                "attDurationEnc": "",
                "duration": "600",
                "dtoken": "dt_demo",
            }
        ]
        return jobs, {"notOpen": False}

    # ---- 学习动作：像真实平台一样，完成后把章节标记为已完成 ----
    def _finish(self, job) -> None:
        """把 job 所属章节点标记为已完成（模拟服务端进度落库）。"""
        jobid = (job or {}).get("jobid", "")
        if not jobid.startswith("v_"):
            return
        pid = jobid[2:]
        for points in self._points.values():
            for point in points:
                if str(point["id"]) == pid:
                    point["has_finished"] = True

    def study_video(self, course=None, job=None, *args, **kwargs):
        self._finish(job)
        return StudyResult.SUCCESS

    def study_document(self, course=None, job=None, *args, **kwargs):
        self._finish(job)
        return StudyResult.SUCCESS

    def study_work(self, course=None, job=None, *args, **kwargs):
        self._finish(job)
        return StudyResult.SUCCESS

    def study_read(self, course=None, job=None, *args, **kwargs):
        self._finish(job)
        return StudyResult.SUCCESS
