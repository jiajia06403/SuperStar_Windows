# -*- coding: utf-8 -*-
"""从「已经做完」的账号采集作业答案，写进本地题库（只读，不提交任何东西）。

已提交的作业再打开就是成绩页：每题带着对错、教师允许公布时的「正确答案」，
以及「我的答案」。把整门课的作业读一遍，就能一次性攒出一份高质量题库，
比自己边做边学快得多，而且完全不碰作业本身。

题库按课程存放（同一门课的题目和答案对所有账号都一样），**只存正确答案**；
库里已经有正确答案的题会跳过，只补缺的：

    ==============================  ==============================
    页面情况                        收录什么
    ==============================  ==============================
    给了「正确答案」                正确答案（最权威，判错也照收）
    平台判对、但没公布答案          页面上的「我的答案」（已被判对）
    判错且没有正确答案              跳过（不知道对的是什么）
    未批阅 / 作业已关闭 / 结构不认识  跳过并计数
    题库里已有答案且与页面一致        跳过（不重复写）
    题库里已有答案但与页面不一致      覆写纠正（页面上的答案为准）
    ==============================  ==============================

只发 GET：``mooc-ans/api/work`` 的同一组参数（见 ``api.work_verify.build_work_params``），
不 POST、不保存草稿，随时可中断。
"""
import argparse
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from api import local_bank, work_verify


def _limit(chaoxing: Any) -> None:
    """复用刷课的限速器；没有就当作不限速（测试用桩对象）。"""
    limiter = getattr(chaoxing, "rate_limiter", None)
    if limiter is None:
        return
    try:
        limiter.limit_rate()
    except Exception:  # noqa: BLE001  限速器异常不该中断采集
        pass


def _emit(handler: Optional[Callable], **event: Any) -> None:
    if handler is None:
        return
    try:
        handler(event)
    except Exception:  # noqa: BLE001  回调异常不影响采集
        pass


def _fetch_points(chaoxing: Any, course: Dict[str, Any]) -> List[dict]:
    data = chaoxing.get_course_point(course["courseId"], course["clazzId"], course["cpi"])
    return list((data or {}).get("points") or [])


def _collect_work(session, chaoxing, course: Dict[str, Any], job: dict,
                  job_info: dict, bank, course_title: str, stats: dict,
                  emit: Callable) -> None:
    """读一份已完成作业的成绩页，把能确认的正确答案补进本地题库。"""
    params = work_verify.build_work_params(course, job, job_info)
    course_id = str(course.get("courseId") or "")
    try:
        _limit(chaoxing)
        resp = session.get(work_verify.WORK_URL, params=params, timeout=15)
    except Exception as exc:  # noqa: BLE001
        stats["errors"] += 1
        emit(kind="log", message=f"【采集】请求作业页失败：{type(exc).__name__}: {exc}")
        return

    if getattr(resp, "status_code", 0) != 200:
        stats["unreadable"] += 1
        emit(kind="log", message=f"【采集】作业页返回 {getattr(resp, 'status_code', '?')}，已跳过")
        return

    text = getattr(resp, "text", "") or ""
    questions = work_verify.parse_questions(text)
    verdicts = work_verify.parse_verdicts(text)
    if not questions or not verdicts:
        # 未批阅 / 作业已关闭 / 页面结构不认识 —— 一律跳过，不猜
        stats["unreadable"] += 1
        return

    mine = work_verify.parse_my_answers(text)
    items = []
    for qid, question in questions.items():
        if not str(question.get("title") or "").strip():
            stats["skipped"] += 1          # 题干都读不出来的题，入库存进去也匹配不上
            continue
        items.append({
            "qid": qid,
            "title": question["title"],
            "type": question.get("type", "unknown"),
            "options": question.get("options", ""),
            "answer": mine.get(qid, ""),   # 判对但没公布答案时，这就是答案
        })
    if not items:
        stats["unreadable"] += 1
        return

    # 全量交给 learn：与页面一致的不重写，不一致的覆写纠正（题库里答错的题会被修好）
    known = bank.known(items, course_title, course_id)
    learned = bank.learn(items, verdicts, course=course_title, course_id=course_id)
    stats["read"] += 1
    stats["questions"] += len(items)
    stats["learned"] += learned
    stats["skipped"] = int(stats.get("skipped") or 0) + max(0, len(items) - learned)
    detail = f"，写入 {learned} 题" if learned else ""
    detail += f"，{known} 题此前已有答案" if known else ""
    emit(kind="log",
         message=f"【采集】作业 {job.get('jobid', '')}：读 {len(items)} 题{detail}")
    emit(kind="progress", message="", done=stats["works"], total=stats.get("works_total") or 0)


def collect_course(chaoxing: Any, course: Dict[str, Any],
                   points: Optional[Iterable[dict]] = None,
                   session: Any = None,
                   on_event: Optional[Callable] = None,
                   should_stop: Optional[Callable[[], bool]] = None,
                   max_works: Optional[int] = None) -> dict:
    """采集单门课的所有作业答案。

    ``points``    章节列表；不传则自己按课程信息取
    ``session``   requests.Session；不传用全局会话
    ``on_event``  进度回调，收到 ``{"kind": "log"/"progress", ...}``
    ``should_stop`` 返回 True 时尽快收尾（已采集的部分保留）
    ``max_works`` 最多读几份作业（试跑用）

    Returns:
        ``{"course","course_id","points","works","read","questions","learned",
           "unreadable","skipped","errors","stopped"}``，本地题库未启用时额外带 ``error``。
    """
    stats: Dict[str, Any] = {
        "course": str(course.get("title") or ""),
        "course_id": str(course.get("courseId") or ""),
        "points": 0, "works": 0, "works_total": 0, "read": 0,
        "questions": 0, "learned": 0, "unreadable": 0, "skipped": 0,
        "errors": 0, "stopped": False,
    }
    bank = local_bank.get_bank()
    if not bank.usable:
        stats["error"] = "本地题库未启用（[tiku] local_bank=false）"
        return stats

    stop = should_stop or (lambda: False)

    def emit(**event: Any) -> None:
        _emit(on_event, **event)

    course_title = stats["course"]

    # 入库归属：这门课（题库按课程共享，不区分账号）
    local_bank.set_scope(course=course_title, course_id=stats["course_id"])

    if session is None:
        from api.base import SessionManager
        session = SessionManager.get_session()

    if points is None:
        points = _fetch_points(chaoxing, course)
    points = list(points)
    stats["points"] = len(points)
    emit(kind="log", message=f"【采集】{course_title}：共 {len(points)} 个章节")

    for index, point in enumerate(points, 1):
        if stop():
            stats["stopped"] = True
            break
        try:
            jobs, job_info = chaoxing.get_job_list(course, point, stop_early=True)
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            emit(kind="log",
                 message=f"【采集】读取章节任务点失败（{point.get('title', '')}）："
                         f"{type(exc).__name__}: {exc}")
            continue

        works = [job for job in (jobs or []) if job.get("type") == "workid"]
        if not works:
            emit(kind="progress", message=point.get("title", ""),
                 done=index, total=len(points))
            continue

        stats["works_total"] += len(works)
        emit(kind="log",
             message=f"【采集】{point.get('title', '')}：发现 {len(works)} 份作业")
        for job in works:
            if stop():
                stats["stopped"] = True
                break
            if max_works is not None and stats["works"] >= max_works:
                stats["stopped"] = True
                emit(kind="log", message=f"【采集】已达上限 {max_works} 份作业，提前收尾")
                break
            stats["works"] += 1
            _collect_work(session, chaoxing, course, job, job_info or {}, bank,
                          course_title, stats, emit)
        if stats["stopped"]:
            break
        emit(kind="progress", message=point.get("title", ""),
             done=index, total=len(points))

    total = bank.stats().get("total", 0)
    if stats["learned"]:
        emit(kind="log",
             message=f"【采集】{course_title} 完成：读 {stats['read']} 份作业，"
                     f"写入 {stats['learned']} 题（跳过 {stats['skipped']} 题，"
                     f"题库共 {total} 题）")
    else:
        emit(kind="log",
             message=f"【采集】{course_title} 完成：没有需要写入的答案"
                     f"（读 {stats['read']} 份 / 跳过 {stats['skipped']} 题 / "
                     f"无法识别 {stats['unreadable']} 份，题库共 {total} 题）")
    return stats


def collect_courses(chaoxing: Any, courses: Iterable[Dict[str, Any]], **kwargs: Any) -> dict:
    """批量采集多门课，返回 ``{"courses": [...], "learned": N, "stopped": bool}``。"""
    on_event = kwargs.pop("on_event", None)
    merged = {"courses": [], "learned": 0, "stopped": False}
    for course in courses:
        stats = collect_course(chaoxing, course, on_event=on_event, **kwargs)
        merged["courses"].append(stats)
        merged["learned"] += int(stats.get("learned") or 0)
        if stats.get("stopped"):
            merged["stopped"] = True
            break
    return merged


# --------------------------------------------------------------------------- #
# 命令行：python -m api.collect [--uid UID] [--course 关键词] [--all] [--max-works N]
# --------------------------------------------------------------------------- #
def _main(argv: "List[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="从已完成作业采集答案到本地题库（只读，不提交作业）")
    parser.add_argument("--uid", default=None, help="已保存的账号 uid（默认最近一次登录的）")
    parser.add_argument("--course", default=None,
                        help="课程关键词或 courseId，逗号分隔；不填等于全部")
    parser.add_argument("--max-works", type=int, default=None, help="最多读几份作业（试跑）")
    args = parser.parse_args(argv)

    from config_store import load_section
    from engine import StudyEngine

    tiku_conf = load_section("tiku")
    local_bank.configure(tiku_conf)
    if not local_bank.get_bank().usable:
        print("本地题库未启用：[tiku] local_bank=false，已退出")
        return 2

    eng = StudyEngine()
    acc = eng.saved_account(args.uid)
    if not acc:
        print("没有可用的已保存账号：请先打开界面登录一次（密码用 DPAPI 加密存在本机）")
        return 2
    print(f"登录中：{acc['username']} ({acc['uid']})")
    eng.login(acc["username"], acc["password"], use_cookies=acc["use_cookies"],
              tiku_config=tiku_conf, remember=False)

    courses = eng.list_courses()
    words = [w.strip() for w in (args.course or "").split(",") if w.strip()]
    if words:
        selected = [c for c in courses
                    if any(w in str(c.get("title", "")) or w == str(c.get("courseId"))
                           for w in words)]
    else:
        selected = list(courses)
    if not selected:
        print("没有匹配的课程")
        return 1

    def on_event(event: dict) -> None:
        if event.get("kind") == "log" and event.get("message"):
            print(event["message"])
        elif event.get("kind") == "progress" and event.get("total"):
            print(f"  章节 {event['done']}/{event['total']} {event.get('message', '')}")

    total_learned = 0
    for course in selected:
        points = eng.get_course_points(course)
        stats = collect_course(eng.chaoxing, course, points=points, on_event=on_event,
                               max_works=args.max_works)
        total_learned += int(stats.get("learned") or 0)
        time.sleep(0.5)
    print(f"全部完成：新增 {total_learned} 题，题库现有 {local_bank.get_bank().stats()['total']} 题")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
