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
    新的一次空白作答                跳过（见下）
    已通过（平台不再开放页面）        跳过并计数
    未批阅 / 作业已关闭 / 结构不认识  跳过并计数
    题库里已有答案且与页面一致        跳过（不重复写）
    题库里已有答案但与页面不一致      覆写纠正（页面上的答案为准）
    ==============================  ==============================

**「新的一次空白作答」不等于「没做过」**：超星作业允许重做时，每次打开都新开一条
作答记录（``workAnswerId`` 重新分配、状态写「待完成」），上次的答案不会带过来。
所以本模块只能说「这一页没有上次答案」，绝不能替用户断言「没提交」。

只发 GET：``mooc-ans/api/work`` 的同一组参数（见 ``api.work_verify.build_work_params``），
不 POST、不保存草稿，随时可中断。
"""
import argparse
import json
import os
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from api import local_bank, work_verify

# 每门课最多转储几份「认不出的页面」——够定位问题，又不会把磁盘塞满
_DUMP_LIMIT = 3
_DUMP_DIRNAME = "collect_debug"


def _dump_page(params: Dict[str, Any], text: str, stats: dict, reason: str) -> str:
    """把认不出的作业页原样存到 ``progress_cache/collect_debug``，返回路径。

    页面结构是超星那边改的，靠猜没用：留一份原始 HTML，用户反馈时能直接定位。
    写盘失败不影响采集。
    """
    if int(stats.get("dumps") or 0) >= _DUMP_LIMIT:
        return ""
    try:
        from paths import data_path
        folder = data_path(_DUMP_DIRNAME)
        os.makedirs(folder, exist_ok=True)
        jobid = str(params.get("jobid") or "work").replace("work-", "")[:16]
        safe_reason = re.sub(r"[^\w\u4e00-\u9fa5]+", "_", reason)[:20] or "unknown"
        stamp = time.strftime("%m%d-%H%M%S")
        base = os.path.join(folder, f"{stamp}_{jobid}_{safe_reason}")
        with open(base + ".html", "w", encoding="utf-8", errors="replace") as fp:
            fp.write(text or "")
        with open(base + ".json", "w", encoding="utf-8") as fp:
            json.dump({"params": params, "reason": reason,
                       "length": len(text or "")}, fp, ensure_ascii=False, indent=2)
        stats["dumps"] = int(stats.get("dumps") or 0) + 1
        return base + ".html"
    except Exception:  # noqa: BLE001  转储失败不该影响采集
        return ""


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
                  emit: Callable) -> str:
    """读一份已完成作业的成绩页，把能确认的正确答案补进本地题库。

    Returns:
        结果键：``"read"``（读到了）/ ``"fresh"``（平台给的是新的一次作答，页面上没有
        上次答案）/ ``"ungraded"`` / ``"passed"`` / ``"closed"`` / ``"no_questions"`` /
        ``"no_verdicts"`` / ``"no_title"`` / ``"request_error"`` / ``"bad_status"``。
        调用方据此做章节级汇总（「新的一次作答」最常见，逐份打日志会把界面日志刷满）。
    """
    params = work_verify.build_work_params(course, job, job_info)
    course_id = str(course.get("courseId") or "")
    try:
        _limit(chaoxing)
        resp = session.get(work_verify.WORK_URL, params=params, timeout=15)
    except Exception as exc:  # noqa: BLE001
        stats["errors"] += 1
        emit(kind="log", message=f"【采集】请求作业页失败：{type(exc).__name__}: {exc}")
        return "request_error"

    if getattr(resp, "status_code", 0) != 200:
        stats["unreadable"] += 1
        stats["bad_status"] = int(stats.get("bad_status") or 0) + 1
        emit(kind="log", message=f"【采集】作业页返回 {getattr(resp, 'status_code', '?')}，已跳过")
        return "bad_status"

    text = getattr(resp, "text", "") or ""
    questions = work_verify.parse_questions(text)
    verdicts = work_verify.parse_verdicts(text)
    if not questions or not verdicts:
        # 没答案可读 / 已关闭 / 结构不认识 —— 一律跳过，不猜
        stats["unreadable"] += 1
        state = work_verify.page_state(text, bool(questions), bool(verdicts))
        status = work_verify.page_status(text)
        if state == work_verify.STATE_CLOSED:
            key, reason = "closed", "作业已结束（老师已关闭或已过期），没有答案可采"
        elif not questions:
            key, reason = "no_questions", "没读到题目"
        elif state == work_verify.STATE_PASSED:
            key, reason = "passed", f"平台标记已完成（页面不给答案），跳过{f'（{status}）' if status else ''}"
        elif state == work_verify.STATE_UNSUBMITTED:
            # 注意：这只说明「这一页是新的空白作答」，**不代表没交过** —— 作业允许重做时
            # 超星每次打开都新开一条作答记录，上次答案不会带过来（见 work_verify 说明）。
            key, reason = "fresh", "平台给的是新的一次作答，页面上没有上次的答案"
        elif state == work_verify.STATE_UNGRADED:
            key, reason = "ungraded", "已提交但平台还没批阅/没公布答案"
        else:
            key, reason = "no_verdicts", "页面里没有对错或正确答案"
        stats[key] = int(stats.get(key) or 0) + 1
        # 「新的一次作答」太常见（一门课可能几十份）：逐份打日志会把界面日志窗口刷满，
        # 改成章节级汇总（见 collect_course），这里不再 emit。
        if key == "fresh":
            return key
        # 结构不认识才需要留原始页面；已通过/未批阅/已关闭都是正常情况，不刷日志不写盘
        dumped = "" if key in ("ungraded", "closed", "passed") \
            else _dump_page(params, text, stats, reason)
        detail = f"，原始页面已存到 {dumped}" if dumped else ""
        emit(kind="log",
             message=f"【采集】作业 {job.get('jobid', '')}：{reason}，已跳过{detail}")
        return key

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
        stats["no_title"] = int(stats.get("no_title") or 0) + 1
        dumped = _dump_page(params, text, stats, "题干读不出来")
        detail = f"，原始页面已存到 {dumped}" if dumped else ""
        emit(kind="log",
             message=f"【采集】作业 {job.get('jobid', '')}：读到 {len(questions)} 题但题干全为空，已跳过{detail}")
        return "no_title"

    # 全量交给 learn：与页面一致的不重写，不一致的覆写纠正（题库里答错的题会被修好）
    known = bank.known(items, course_title, course_id)
    learned = bank.learn(items, verdicts, course=course_title, course_id=course_id)
    stats["read"] += 1
    stats["questions"] += len(items)
    stats["learned"] += learned
    stats["known"] = int(stats.get("known") or 0) + known
    # 既没写进库、也不是「库里已有」的，就是平台没判对也没公布正确答案的题
    undecided = max(0, len(items) - learned - known)
    stats["undecided"] = int(stats.get("undecided") or 0) + undecided
    stats["skipped"] = int(stats.get("skipped") or 0) + max(0, len(items) - learned)
    detail = f"，写入 {learned} 题" if learned else ""
    detail += f"，{known} 题此前已有答案" if known else ""
    if undecided:
        detail += f"，{undecided} 题平台没判对也没给正确答案"
    emit(kind="log",
         message=f"【采集】作业 {job.get('jobid', '')}：读 {len(items)} 题{detail}")
    emit(kind="progress", message="", done=stats["works"], total=stats.get("works_total") or 0)
    return "read"


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
           "fresh","passed","unreadable","skipped","errors","stopped"}``，
        本地题库未启用时额外带 ``error``。

    Note:
        ``fresh`` 是「平台给了一次新的空白作答」，**不等于「用户没做过」**：
        作业允许重做时超星每次打开都新开作答记录，上次答案不带过来。
        ``passed`` 是「平台标已完成，但不再开放页面查看答案」。
    """
    stats: Dict[str, Any] = {
        "course": str(course.get("title") or ""),
        "course_id": str(course.get("courseId") or ""),
        "points": 0, "works": 0, "works_total": 0, "read": 0,
        "questions": 0, "learned": 0, "unreadable": 0, "skipped": 0,
        "known": 0, "undecided": 0, "fresh": 0, "passed": 0,
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
    # 入库归属：这门课（题库按课程共享，不区分账号）。
    # 显式传给 known/learn（见 _collect_work），不用进程级 set_scope —— 采集可能
    # 与刷课并发，全局作用域会被别的课程覆盖，答案会写进错误的课程文件。

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
            # mark_empty=False：采集是只读的，绝不把空章节上报成「已完成」
            # （那会改服务端进度，还会每章刷一条「空页面任务完成」把界面日志刷满）
            # include_passed=True：把已通过的作业也读出来（否则它们连统计都进不来）
            jobs, job_info = chaoxing.get_job_list(course, point, stop_early=True,
                                                   mark_empty=False,
                                                   include_passed=True)
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            emit(kind="log",
                 message=f"【采集】读取章节任务点失败（{point.get('title', '')}）："
                         f"{type(exc).__name__}: {exc}")
            continue

        if isinstance(job_info, dict) and job_info.get("unreadable"):
            # 读不到任务点（掉登录/风控/解析失败）：不是「本章没有作业」，计为错误
            stats["errors"] += 1
            emit(kind="log",
                 message=f"【采集】读取章节任务点失败（{point.get('title', '')}）：页面不可读")
            continue

        works = [job for job in (jobs or []) if job.get("type") == "workid"]
        if not works:
            emit(kind="progress", message=point.get("title", ""),
                 done=index, total=len(points))
            continue

        # 已通过（平台标「已完成」）的作业：答案在平台侧、页面不开放（实测直连 403，
        # 去掉 jobid 只会拿到全新空白作答页），所以不再发请求，只计数报出来。
        passed = [job for job in works if job.get("passed")]
        pending = [job for job in works if not job.get("passed")]
        stats["works_total"] += len(works)
        if passed:
            stats["passed"] = int(stats.get("passed") or 0) + len(passed)
        emit(kind="log",
             message=f"【采集】{point.get('title', '')}：发现 {len(works)} 份作业"
                     + (f"（其中 {len(passed)} 份已通过，平台不开放查看答案）"
                        if passed else ""))
        outcomes = []
        for job in pending:
            if stop():
                stats["stopped"] = True
                break
            if max_works is not None and stats["works"] >= max_works:
                stats["stopped"] = True
                emit(kind="log", message=f"【采集】已达上限 {max_works} 份作业，提前收尾")
                break
            stats["works"] += 1
            outcomes.append(_collect_work(session, chaoxing, course, job, job_info or {},
                                          bank, course_title, stats, emit))
        # 「新的一次作答」逐份打日志会把界面日志刷满（一门课几十份），这里按章节合并一条
        fresh = outcomes.count("fresh")
        if fresh:
            emit(kind="log",
                 message=f"【采集】{point.get('title', '')}：{fresh} 份作业没有上次的答案"
                         f"（平台给的是新的一次作答；做过但要重做过的才有答案可读）")
        if stats["stopped"]:
            break
        emit(kind="progress", message=point.get("title", ""),
             done=index, total=len(points))

    total = bank.stats().get("total", 0)
    parts = [f"读 {stats['read']} 份作业"]
    if stats["learned"]:
        parts.append(f"新增/纠正 {stats['learned']} 题")
    if int(stats.get("known") or 0):
        parts.append(f"题库已有 {stats['known']} 题")
    if int(stats.get("undecided") or 0):
        parts.append(f"平台未给答案 {stats['undecided']} 题")
    skips = []
    for key, label in (("fresh", "无上次答案"),
                       ("passed", "已通过(平台不开放)"),
                       ("ungraded", "未批阅/未公布答案"),
                       ("closed", "已结束"),
                       ("no_questions", "没读到题目"),
                       ("no_verdicts", "没有对错或正确答案"),
                       ("no_title", "题干为空"),
                       ("bad_status", "状态码异常")):
        count = int(stats.get(key) or 0)
        if count:
            skips.append(f"{label} {count} 份")
    if skips:
        parts.append("跳过：" + "、".join(skips))
    if not stats["read"] and (int(stats.get("fresh") or 0)
                              or int(stats.get("passed") or 0)):
        parts.append("没读到答案：平台对这些作业要么给的是新的空白作答，"
                     "要么已通过但不再开放查看（要在网页端「查看详情」才看得到历史答案）")
    parts.append(f"题库共 {total} 题")
    emit(kind="log", message=f"【采集】{course_title} 完成：" + "，".join(parts))
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
