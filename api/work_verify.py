# -*- coding: utf-8 -*-
"""作业提交结果核验：从「已提交」页面解析每道题的对错与正确答案。

本地题库只收录**服务端确认过**的答案，所以提交后需要拿一次判分结果。
超星作业提交完成后，再打开同一份作业拿到的是作答/成绩页，页面里每道题的
容器 ``div.singleQuesId`` 内会带上判分痕迹（对/错图标、得分、正确答案）。

本模块只负责「拿页面 + 解析」，不决定要不要入库（由 ``api.local_bank`` 决定）：

    verdicts = verify_work(session, params)
    # {"<qid>": True}                                    平台判对
    # {"<qid>": False}                                   平台判错，且没给出正确答案
    # {"<qid>": {"ok": False, "answer": "A", ...}}       平台判错，但拿到了正确答案
    # {}                                                 拿不到判分信息（未批阅 / 结构不认识 / 请求失败）

「判错 + 拿到正确答案」是最有价值的一种：本地题库会把正确答案收进去，
下次同一道题直接答对（见 ``api/local_bank.py`` 的收录口径）。

解析刻意做得宽容：认不出就当作「未判定」，绝不猜。宁可少收录，也不能把错答案
写进本地题库，否则后面所有账号都会被它带错。
"""
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from api.logger import logger

# 作业页（作答页 / 成绩页是同一个地址，提交前后返回的内容不同）
WORK_URL = "https://mooc1.chaoxing.com/mooc-ans/api/work"

# 判分图标的 class 名（超星各版本用过好几种）
_WRONG_TOKENS = {"cuo", "wrong", "error", "cross", "cha", "icon-cuo", "icon-wrong"}
_RIGHT_TOKENS = {"dui", "right", "correct", "tick", "gou", "icon-dui", "icon-right"}

_SCORE_RE = re.compile(r"得\s*分\s*[:：]?\s*(\d+(?:\.\d+)?)")
_FULL_SCORE_RE = re.compile(r"(?:满分|本题分值|分值)\s*[:：]?\s*(\d+(?:\.\d+)?)")
_RIGHT_ANSWER_RE = re.compile(r"正确答案\s*[:：]?\s*([^\n\r]{1,200})")
_MY_ANSWER_RE = re.compile(r"我的答案\s*[:：]?\s*([^\n\r]{1,200})")
# 提交后页面上的状态提示（用于判断「有没有判分」这件事本身）
_SUBMITTED_HINTS = ("已提交", "已完成", "我的答案", "正确答案", "得分")
# 正确答案后面常跟的下一段内容，取到它就要截断
_ANSWER_STOP_RE = re.compile(r"(?:答案解析|我的答案|题型|难度|知识点|正确答案|得分|满分)")
_LETTER_PREFIX_RE = re.compile(r"^\s*([A-Za-z])\s*[.、．)）:：]?\s*")


def _tokens(element) -> set:
    """收集元素及其后代的 class token（小写）。"""
    tokens = set()
    for tag in [element] + element.find_all(True):
        for name in (tag.get("class") or []):
            tokens.add(str(name).strip().lower())
    return tokens


def _clean_answer(raw: str) -> str:
    """清理「正确答案」文本：截断后续段落、去掉两端多余符号。"""
    answer = _ANSWER_STOP_RE.split(str(raw or ""))[0]
    answer = answer.replace("\r", " ").replace("\n", " ").strip(" \t:：,，;；。")
    return answer.strip()


def _block_options(block) -> str:
    """从判分页的题目块里取选项（``A. xxx\\nB. yyy``）。

    判分页的「正确答案：A」指的是**这一页**的选项顺序，因此入库时优先用这一页的
    选项去还原答案文本；取不到（选项没有字母前缀/重复）就返回空串，让调用方
    退回提交页的选项。
    """
    try:
        ul = block.find("ul")
        if not ul:
            return ""
        lines = []
        letters = []
        for li in ul.find_all("li", recursive=False):
            text = re.sub(r"[\s\u3000]+", " ", li.get_text(" ", strip=True)).strip()
            if not text:
                return ""
            matched = _LETTER_PREFIX_RE.match(text)
            if not matched:
                return ""
            letters.append(matched.group(1).upper())
            lines.append(text)
        if len(set(letters)) != len(letters):
            return ""
        return "\n".join(lines)
    except Exception:  # noqa: BLE001  页面结构异常不影响判分
        return ""


def _block_mine(block) -> str:
    """页面上的「我的答案」文本（没有就是空串）。"""
    text = block.get_text(" ", strip=True)
    matched = _MY_ANSWER_RE.search(text)
    return _clean_answer(matched.group(1)) if matched else ""


def _block_verdict(block) -> Optional[Dict[str, Any]]:
    """单个题目的判定：``{"ok": bool|None, "answer": str, "options": str}`` 或 None。

    ``answer`` 只在页面真的给出「正确答案」时才有值——判错时这就是纠错依据。
    """
    tokens = _tokens(block)
    text = block.get_text(" ", strip=True)

    # 1) 先摘「正确答案」——它对判对/判错都有价值（判错时用来纠正入库答案）
    right = _RIGHT_ANSWER_RE.search(text)
    answer = _clean_answer(right.group(1)) if right else ""
    options = _block_options(block) if answer else ""

    # 2) 再判对错
    ok: Optional[bool] = None
    if tokens & _WRONG_TOKENS:
        ok = False
    elif tokens & _RIGHT_TOKENS:
        ok = True
    else:
        score = _SCORE_RE.search(text)
        if score:
            got = float(score.group(1))
            full = _FULL_SCORE_RE.search(text)
            ok = (got + 1e-6 >= float(full.group(1))) if full else (got > 0)
        elif len(text) <= 60:                       # 只有对勾/叉号的极简页面
            if "√" in text:
                ok = True
            elif "×" in text:
                ok = False

    if ok is False:
        # 判错：有正确答案就交给题库纠错；没有就只能标记「错」，不入库
        return {"ok": False, "answer": answer, "options": options}
    if ok is True:
        # 判对：直接存本次答案就好（提交页的选项顺序才是重映射的依据）
        return {"ok": True, "answer": "", "options": ""}
    if answer:
        # 看不出对错的页面但给了正确答案：正确答案本身就可以入库
        return {"ok": None, "answer": answer, "options": options}
    return None


def parse_verdicts(html_text: str) -> Dict[str, Any]:
    """从作业页 HTML 解析出 ``{qid: verdict}``；解析不出任何判定时返回空 dict。"""
    if not html_text:
        return {}
    try:
        soup = BeautifulSoup(html_text, "lxml")
    except Exception as exc:  # noqa: BLE001  页面异常不能影响刷课
        logger.debug(f"判分页面解析失败：{exc}")
        return {}
    verdicts: Dict[str, Any] = {}
    for block in soup.find_all("div", class_="singleQuesId"):
        qid = str(block.get("data") or "").strip()
        if not qid:
            continue
        verdict = _block_verdict(block)
        if verdict is None:
            continue
        # 无附加信息时退化成简单的 True/False，方便阅读与兼容旧逻辑
        if verdict["ok"] is True and not verdict["answer"]:
            verdicts[qid] = True
        elif verdict["ok"] is False and not verdict["answer"]:
            verdicts[qid] = False
        else:
            verdicts[qid] = {"ok": verdict["ok"],
                             "answer": verdict["answer"],
                             **({"options": verdict["options"]} if verdict["options"] else {})}
    return verdicts


def has_result_marks(html_text: str) -> bool:
    """页面是否已经出现判分痕迹（用来区分「刚提交还没判分」和「已判分」）。"""
    return any(hint in str(html_text or "") for hint in _SUBMITTED_HINTS)


def _iter_question_blocks(html_text: str):
    """遍历 ``div.singleQuesId`` 题目块，产出 ``(qid, block)``。"""
    if not html_text:
        return []
    try:
        soup = BeautifulSoup(html_text, "lxml")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"作业页面解析失败：{exc}")
        return []
    items = []
    for block in soup.find_all("div", class_="singleQuesId"):
        qid = str(block.get("data") or "").strip()
        if qid:
            items.append((qid, block))
    return items


def parse_questions(html_text: str) -> Dict[str, Dict[str, str]]:
    """从作业页解析题目本身（qid / 题干 / 题型 / 选项）。

    实现在 ``api.decode.decode_work_questions``（复用同一套题干/选项提取与字体解密），
    这样采集进来的指纹与作答时算出来的完全一致。
    """
    try:
        from api.decode import decode_work_questions
        return decode_work_questions(html_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"作业题目解析失败：{type(exc).__name__}: {exc}")
        return {}


def parse_my_answers(html_text: str) -> Dict[str, str]:
    """从作业页解析「我的答案」（``{qid: 答案文本}``）。

    采集别人的已完成作业时，教师可能只公布对错、不公布正确答案；这时
    「平台判对 + 我的答案」照样是可靠答案（对的那次就是平台认可的答案）。
    """
    mine: Dict[str, str] = {}
    for qid, block in _iter_question_blocks(html_text):
        answer = _block_mine(block)
        if answer:
            mine[qid] = answer
    return mine


def build_work_params(course: Dict[str, Any], job: Dict[str, Any],
                      job_info: Dict[str, Any]) -> Dict[str, Any]:
    """作业页请求参数（抓题、提交后核验、采集已完成作业共用同一组）。"""
    return {
        "api": "1",
        "workId": str(job["jobid"]).replace("work-", ""),
        "jobid": job["jobid"],
        "originJobId": job["jobid"],
        "needRedirect": "true",
        "skipHeader": "true",
        "knowledgeid": str(job_info["knowledgeid"]),
        "ktoken": job_info["ktoken"],
        "cpi": job_info["cpi"],
        "ut": "s",
        "clazzId": course["clazzId"],
        "type": "",
        "enc": job["enc"],
        "mooc2": "1",
        "courseid": course["courseId"],
    }


def _counts(verdicts: Dict[str, Any]) -> "tuple[int, int, int]":
    """统计（判对 / 判错 / 拿到正确答案）。"""
    right = wrong = fixed = 0
    for value in verdicts.values():
        if value is True:
            right += 1
        elif value is False:
            wrong += 1
        elif isinstance(value, dict):
            if value.get("answer"):
                fixed += 1
            elif value.get("ok") is False:
                wrong += 1
            elif value.get("ok") is True:
                right += 1
    return right, wrong, fixed


def verify_work(session, params: Dict[str, Any], url: Optional[str] = None,
                timeout: float = 10.0) -> Dict[str, Any]:
    """重新拉取作业页并解析判分结果。

    ``params`` 与提交前抓题时用的参数一致（见 ``Chaoxing.study_work``）。
    任何异常都只记日志并返回空 dict —— 核验失败不应影响刷课主流程。
    """
    target = url or WORK_URL
    try:
        resp = session.get(target, params=params, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"作业判分核验请求失败：{type(exc).__name__}: {exc}")
        return {}
    text = getattr(resp, "text", "") or ""
    if getattr(resp, "status_code", 0) != 200:
        logger.warning(f"作业判分核验返回异常状态码：{getattr(resp, 'status_code', '?')}")
        return {}
    verdicts = parse_verdicts(text)
    if not verdicts:
        logger.info("作业判分核验：未从页面解析到对错信息（可能尚未批阅），本次不收录")
        return {}
    right, wrong, fixed = _counts(verdicts)
    logger.info(f"作业判分核验：识别 {len(verdicts)} 题（对 {right} / 错 {wrong} / "
                f"拿到正确答案 {fixed}）")
    return verdicts
