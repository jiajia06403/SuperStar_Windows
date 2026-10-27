# -*- coding: utf-8 -*-
"""作业提交结果核验：从「已提交」页面解析每道题的对错与正确答案。

本地题库只收录**服务端确认过**的答案，所以提交后需要拿一次判分结果。
超星作业提交完成后，再打开同一份作业拿到的是作答/成绩页，页面里每道题的
容器内会带上判分痕迹（对/错图标、得分、正确答案）。

超星同一份作业在不同入口下会给出**好几套完全不同的 DOM**（实测）：

    ====================================  ==========================================
    页面 / 入口                            题目容器
    ====================================  ==========================================
    mooc-ans/work/doHomeWorkNew（作答态）    div.singleQuesId[data=qid]，题干 .Zy_TItle
    同上，新版（.TiMu 带题型码）             div.TiMu[data=题型码]，qid 在隐藏域 name 里
    mooc2/work/view（已批阅/成绩态）         div.mark_item[data=question<qid>]，
                                           题干 .mark_name，正确答案 .rightAnswerContent
    ====================================  ==========================================

所以解析一律「先找题块、再各自取 qid / 题干 / 答案」，任一套结构认得出就够用；
全都认不出才算「无法识别」（见 ``api.collect`` 的计数器）。

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
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from api import local_bank
from api.logger import logger

# 作业页（作答页 / 成绩页是同一个地址，提交前后返回的内容不同）
WORK_URL = "https://mooc1.chaoxing.com/mooc-ans/api/work"

# 判分图标的 class 名（超星各版本用过好几种）。按 ``-`` / ``_`` 切词后比对单词，
# 这样 ``marking_dui``、``icon-dui``、``icon_ok`` 都能命中，而 ``incorrect`` 不会误命中 ``correct``。
_WRONG_WORDS = {"cuo", "wrong", "error", "cross", "cha", "no", "err"}
_RIGHT_WORDS = {"dui", "right", "correct", "tick", "gou", "ok", "yes"}
_WORD_SPLIT_RE = re.compile(r"[-_\s]+")

# 题目容器的 class（按「从外到内」的识别顺序排列，同级重复的靠 qid 去重）
_BLOCK_CLASSES = ("singleQuesId", "mark_item", "questionLi", "TiMu")
# 题干容器的候选 class（新版/旧版/成绩态各一个）
_TITLE_CLASSES = ("Zy_TItle", "newZy_TItle", "mark_name", "q_title", "questionTitle")
# 我的答案：「我的答案：A」区块。``answerCon`` 本身在作答区也有（那里是选项容器），
# 所以只在 ``myAnswer`` / ``myAnswerBx`` 容器里才认它。
_MY_ANSWER_CLASSES = ("stuAnswerContent", "myAnswerCon", "myAnswerContent",
                      "my_answer", "mark_fill")
_MY_ANSWER_HOLDERS = ("myAnswerBx", "myAnswer")
_RIGHT_ANSWER_HOLDERS = ("rightAnswerBx", "rightAnswer")
_RIGHT_ANSWER_CLASSES = ("rightAnswerContent", "rightAnswerCon", "rightAnswer",
                         "correctAnswerContent")
# 选项字母单独成元素时的 class（成绩态常见：<i class="num_option">A</i>正文）
_LETTER_CLASSES = ("num_option", "num_option_a", "option_letter", "choice_letter")

_SCORE_RE = re.compile(r"得\s*分\s*[:：]?\s*(\d+(?:\.\d+)?)")
_FULL_SCORE_RE = re.compile(r"(?:满分|本题分值|分值)\s*[:：]?\s*(\d+(?:\.\d+)?)")
_RIGHT_ANSWER_RE = re.compile(r"正确答案\s*[:：]\s*([^\n\r]{1,200})")
_MY_ANSWER_RE = re.compile(r"我的答案\s*[:：]\s*([^\n\r]{1,200})")
# 提交后页面上的状态提示（用于判断「有没有判分」这件事本身）
_SUBMITTED_HINTS = ("已提交", "已完成", "我的答案", "正确答案", "得分")
# 正确答案后面常跟的下一段内容，取到它就要截断
_ANSWER_STOP_RE = re.compile(r"(?:答案解析|我的答案|题型|难度|知识点|正确答案|得分|满分)")
_LETTER_PREFIX_RE = re.compile(r"^\s*([A-Za-z])\s*[.、．)）:：]?\s*")
# 题目 id：``data="404723008"`` / ``data="question404723008"`` / ``id="question404723008"``
_QID_VALUE_RE = re.compile(r"^(?:question|q)?[_\-]?(\d{4,})$", re.I)
# 作答态把 qid 藏在隐藏域 name 里：answer405717732 / answertype405717732 / tiankongsize... / answerEditor4057177601
_HIDDEN_QID_RE = re.compile(r"^(?:answerEditor|answertype|tiankongsize|answer)(\d{4,})", re.I)
# 题干里带的题型标签，``1.（单选题）xxx`` / ``【判断题】xxx``
_TYPE_WORD_MAP = {
    "单选": "single", "多选": "multiple", "判断": "judgement",
    "填空": "completion", "简答": "shortanswer", "论述": "shortanswer",
    "名词解释": "shortanswer",
}
_TYPE_WORD_RE = re.compile(r"[\[【(（]\s*(单选|多选|判断|填空|简答|论述|名词解释)题?\s*[\]】)）]")
_TYPE_CODE_MAP = {"0": "single", "1": "multiple", "2": "completion",
                  "3": "judgement", "4": "shortanswer"}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _classes(element) -> "List[str]":
    return [str(name).strip().lower() for name in (element.get("class") or [])]


def _tokens(element) -> set:
    """收集元素及其后代的 class token（小写）。"""
    tokens = set()
    for tag in [element] + element.find_all(True):
        for name in (tag.get("class") or []):
            tokens.add(str(name).strip().lower())
    return tokens


def _has_word(tokens, words: set) -> bool:
    """class token 切词后是否命中词表（``marking_dui`` -> ``dui``）。"""
    for token in tokens:
        for word in _WORD_SPLIT_RE.split(token):
            if word in words:
                return True
    return False


def _text(element) -> str:
    return re.sub(r"[\s\u3000]+", " ", element.get_text(" ", strip=True)).strip() if element else ""


def _qid_of_block_attr(element) -> str:
    """只从块级属性（``data`` / ``id``）取 qid，不看选项上的 ``qid`` 属性。"""
    for attr in ("data", "data-qid", "questionid"):
        matched = _QID_VALUE_RE.match(str(element.get(attr) or "").strip())
        if matched:
            return matched.group(1)
    matched = _QID_VALUE_RE.match(str(element.get("id") or "").strip())
    return matched.group(1) if matched else ""


def _qid_of(element) -> str:
    """从元素自身属性里取 qid（取不到返回空串）。"""
    qid = _qid_of_block_attr(element)
    if qid:
        return qid
    matched = _QID_VALUE_RE.match(str(element.get("qid") or "").strip())
    return matched.group(1) if matched else ""


def _qid_of_hidden(element) -> str:
    """作答态：qid 在隐藏域 name 里（``answer405717732`` / ``answertype...``）。"""
    for field in element.find_all("input", attrs={"name": True}):
        matched = _HIDDEN_QID_RE.match(str(field.get("name") or "").strip())
        if matched:
            return matched.group(1)
    for tag in element.find_all(["li", "div"], attrs={"qid": True}):
        qid = _qid_of(tag)
        if qid:
            return qid
    return ""


def _is_group(element) -> bool:
    """这个元素是不是「题型分组」而不是「单道题」。

    成绩态页面里 ``.mark_item`` 既可能是单题容器，也可能是「一. 单选题(共8题)」的
    分组容器。分组容器的判据：内部还有 2 个及以上**各自带不同 qid** 的题目块
    （只认 ``data`` / ``id`` 上的 qid；选项 ``li[qid]`` 与所属题目同值，不算）。
    """
    seen = set()
    for child in element.find_all(["div", "li"]):
        qid = _qid_of_block_attr(child)
        if not qid or qid == _qid_of_block_attr(element):
            continue
        seen.add(qid)
        if len(seen) >= 2:
            return True
    return False


def iter_question_blocks(html_text: str) -> "List[Tuple[str, Any]]":
    """遍历页面上的题目块，产出 ``(qid, block)``（按文档顺序、同一 qid 只出一次）。"""
    if not html_text:
        return []
    try:
        soup = BeautifulSoup(html_text, "lxml")
    except Exception as exc:  # noqa: BLE001  页面异常不能影响刷课
        logger.debug(f"作业页面解析失败：{exc}")
        return []

    wanted = {name.lower() for name in _BLOCK_CLASSES}
    blocks: "List[Tuple[str, Any]]" = []
    seen = set()

    def _add(qid: str, block) -> None:
        if qid and qid not in seen:
            seen.add(qid)
            blocks.append((qid, block))

    for element in soup.find_all(["div", "li"]):
        classes = set(_classes(element))
        if not (classes & wanted):
            continue
        qid = _qid_of(element) or _qid_of_hidden(element)
        if qid and not _is_group(element):
            _add(qid, element)

    # 兜底：题块 class 全变了，但隐藏域还在（作答态最常见的退化情形）
    for field in soup.find_all("input", attrs={"name": True}):
        matched = _HIDDEN_QID_RE.match(str(field.get("name") or "").strip())
        if not matched:
            continue
        qid = matched.group(1)
        if qid in seen:
            continue
        block = None
        for ancestor in field.parents:
            if ancestor.name in ("div", "li") and set(_classes(ancestor)) & wanted:
                block = ancestor
                break
        _add(qid, block or field.parent or field)
    return blocks


def block_title_element(block):
    """题块里的题干元素（找不到返回 None）。"""
    if block is None:
        return None
    for name in _TITLE_CLASSES:
        found = block.find(class_=name)
        if found is not None:
            return found
    for text in ("div", "h3", "dt"):
        found = block.find(text)
        if found is not None and _text(found):
            return found
    return None


def block_qtype(block, title_text: str = "") -> str:
    """题型：优先 ``div.TiMu[data=题型码]``，其次题干里的 ``（单选题）`` 标签。"""
    if block is not None:
        timu = block if "timu" in set(_classes(block)) else block.find(class_="TiMu")
        code = str((timu.get("data") if timu is not None else "") or "").strip()
        if code in _TYPE_CODE_MAP:
            return _TYPE_CODE_MAP[code]
    matched = _TYPE_WORD_RE.search(str(title_text or ""))
    if matched:
        return _TYPE_WORD_MAP.get(matched.group(1), "unknown")
    return "unknown"


# --------------------------------------------------------------------------- #
# 答案解析
# --------------------------------------------------------------------------- #
def _clean_answer(raw: str) -> str:
    """清理「正确答案」文本：截断后续段落、去掉两端多余符号。"""
    answer = _ANSWER_STOP_RE.split(str(raw or ""))[0]
    answer = answer.replace("\r", " ").replace("\n", " ").strip(" \t:：,，;；。")
    return answer.strip()


def _strip_label(text: str, label: str) -> str:
    """去掉 ``正确答案：`` / ``我的答案：`` 这类前缀。"""
    value = str(text or "").strip()
    return re.sub(rf"^{label}\s*[:：]?\s*", "", value).strip()


def _letter_of(li) -> "Tuple[str, str]":
    """选项的「字母 + 整行文本」。

    两种真实写法都要认：
      1. 字母在文本里：``<li>A. 党的十六大</li>``；
      2. 字母单独成元素：``<li><i class="num_option">A</i>党的十六大</li>``
         （``num_option`` 的 ``data`` 是平台内部题序，**文本**才是用户看到的字母）。
    """
    text = _text(li)
    if not text:
        return "", ""
    matched = _LETTER_PREFIX_RE.match(text)
    if matched:
        return matched.group(1).upper(), text
    for element in li.find_all(True):
        if not (set(_classes(element)) & set(_LETTER_CLASSES)):
            continue
        letter = _LETTER_PREFIX_RE.match(_text(element))
        if not letter:
            continue
        body = _text(li)
        body = body.replace(_text(element), "", 1).strip()
        if body:
            return letter.group(1).upper(), f"{letter.group(1).upper()}. {body}"
    return "", text


def _block_options(block) -> str:
    """从题目块里取选项（``A. xxx\\nB. yyy``，顺序按页面/字母）。

    字母取不出来（没有前缀、重复字母）就返回空串——宁可不还原答案文本，也不猜。
    """
    try:
        ul = block.find("ul") if block is not None else None
        if not ul:
            return ""
        lines: "List[str]" = []
        letters: "List[str]" = []
        for li in ul.find_all("li"):
            letter, text = _letter_of(li)
            if not letter or not text:
                return ""
            letters.append(letter)
            lines.append(text)
        if len(set(letters)) != len(letters):
            return ""
        return "\n".join(lines)
    except Exception:  # noqa: BLE001  页面结构异常不影响判分
        return ""


def _class_text(block, names) -> str:
    """取第一个命中 class 的元素文本（找不到返回空串）。"""
    if block is None:
        return ""
    for name in names:
        found = block.find(class_=name)
        if found is not None:
            text = _text(found)
            if text:
                return text
    return ""


def _holder_text(block, holders, names) -> str:
    """在指定容器里取文本（实测成绩态：``div.myAnswer > div.answerCon``）。"""
    if block is None:
        return ""
    for holder_name in holders:
        holder = block.find(class_=holder_name)
        if holder is None:
            continue
        text = _class_text(holder, names)
        if text:
            return text
    return ""


def _block_right_answer(block) -> str:
    """页面给出的「正确答案」（没有就是空串）。"""
    text = _holder_text(block, _RIGHT_ANSWER_HOLDERS, _RIGHT_ANSWER_CLASSES)
    if not text:
        text = _class_text(block, _RIGHT_ANSWER_CLASSES)
    if text:
        return _clean_answer(_strip_label(text, "正确答案"))
    matched = _RIGHT_ANSWER_RE.search(_text(block))
    return _clean_answer(matched.group(1)) if matched else ""


def _block_mine(block) -> str:
    """页面上的「我的答案」文本（没有就是空串）。"""
    text = _holder_text(block, _MY_ANSWER_HOLDERS, ("answerCon", "answerFont"))
    if not text:
        text = _class_text(block, _MY_ANSWER_CLASSES)
    if text:
        return _clean_answer(_strip_label(text, "我的答案"))
    matched = _MY_ANSWER_RE.search(_text(block))
    return _clean_answer(matched.group(1)) if matched else ""


def _block_verdict(block) -> Optional[Dict[str, Any]]:
    """单个题目的判定：``{"ok": bool|None, "answer": str, "options": str}`` 或 None。

    ``answer`` 只在页面真的给出「正确答案」时才有值——判错时这就是纠错依据。
    """
    tokens = _tokens(block)
    text = _text(block)
    answer = _block_right_answer(block)
    options = _block_options(block) if answer else ""

    # 对错判定：图标 class 优先（最可靠），其次得分，最后才是极简页面的 √ / ×
    wrong = _has_word(tokens, _WRONG_WORDS)
    right = _has_word(tokens, _RIGHT_WORDS)
    ok: Optional[bool] = None
    if wrong:
        ok = False                      # 同时对错标记都有时按「错」处理（保守：不入库我的答案）
    elif right:
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
    verdicts: Dict[str, Any] = {}
    for qid, block in iter_question_blocks(html_text):
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


# 页面状态：用来把「没答案可采」的原因说清楚，而不是笼统报「无法识别」
STATE_OK = "ok"                    # 有判分/正确答案，能采
STATE_UNGRADED = "ungraded"        # 已提交，但平台还没批阅 / 没公布答案
STATE_UNSUBMITTED = "unsubmitted"  # 平台给的是新的一次作答（页面上没有上次答案）
STATE_PASSED = "passed"            # 平台标成已完成/已通过，但页面不再给答案
STATE_CLOSED = "closed"            # 作业已结束 / 已关闭，平台只回一句提示
STATE_UNKNOWN = "unknown"          # 结构不认识（需要抓页面看）

# 作业被老师关闭 / 已过期时，接口只返回一整个光秃秃的页面（实测 31 字节）：
# ``<html><body>作业已结束</body></html>``。这不是「解析失败」，也不是「没提交」。
_CLOSED_HINTS = ("作业已结束", "作业已过期", "作业已关闭", "该作业已结束",
                 "作业尚未开始", "作业未开始")

# 已提交页面的特征（作答区后面多出「我的答案 / 正确答案 / 得分」区块）。
# 不能用 ``answerCon`` / ``marking_``：未提交的作答区里也有这些 class。
_ANSWER_BLOCK_HINTS = ("newAnswerBx", "myAnswerBx", "我的答案", "正确答案",
                       "得分：", "得分:")

# 平台自己写在题目页头部的状态（实测 ``<div class="fr testTit_status"><i></i>待完成</div>``）。
# 这是**唯一权威**的判据：只有它知道这份作业以前交没交过。
_STATUS_BLOCK_RE = re.compile(r'class="[^"]*testTit_status[^"]*"[^>]*>(.*?)</div>', re.S)
_STATUS_TAG_RE = re.compile(r"<[^>]+>")
# 平台说法 → 语义。「待完成」不等于「从来没交过」：作业允许重做时，超星每次打开都
# 新开一条作答记录（workAnswerId 是新分配的，状态就写「待完成」），上次答案不会带过来。
_STATUS_TODO_WORDS = ("待完成", "未完成", "未提交", "尚未完成")
_STATUS_DONE_WORDS = ("已完成", "已通过", "已提交", "已批阅", "已阅")
_STATUS_REDO_WORDS = ("重做", "未达到", "未通过", "不合格", "重新作答", "重新提交")


def page_status(html_text: str) -> str:
    """平台写在作业页头部的状态文本（``待完成`` / ``未达到及格线，请重做`` / ``已完成``）。

    拿不到就是空串（老版页面、作业类页面没有这个区块）。
    """
    text = str(html_text or "")
    if not text:
        return ""
    matched = _STATUS_BLOCK_RE.search(text)
    if not matched:
        return ""
    return re.sub(r"[\s\u3000]+", " ", _STATUS_TAG_RE.sub(" ", matched.group(1))).strip()


def is_closed_page(html_text: str) -> bool:
    """是不是「作业已结束」这类只回一句提示的页面（不含题目）。

    这类响应往往只有几十字节（实测 ``<html><body>作业已结束</body></html>``，31 字节），
    加长度上限是为了避免正常作业页里恰好出现「作业已结束」几个字时被误判。
    """
    text = str(html_text or "")
    if not text.strip() or len(text) > 2000:
        return False
    return any(hint in text for hint in _CLOSED_HINTS)


def is_unsubmitted_page(html_text: str) -> bool:
    """平台给的是不是「一次新的作答」（页面上没有上次的答案）。

    **不能理解成「从来没交过」**：作业允许重做时，超星每次打开都新开一条作答记录，
    状态照样写「待完成」，上次的答案不会带过来（实测：已及格章节点进去就是全新作答页，
    ``workAnswerId`` 是新分配的）。所以只能确认「这一页没有上次答案」，
    不能断言「用户没做过」。日志文案必须按前者写。

    判据优先级：
    1. 页头 ``.testTit_status``（平台自己写的，最权威）；
    2. 没有状态区（老版 / 作业类页面）才退回正反向线索——**不再用 ``btnSubmit``**，
       因为「已提交可重做」的页面同样有提交按钮（实测同一个 ``<a class="btnSubmit">``）。
    """
    text = str(html_text or "")
    if not text.strip():
        return False
    status = page_status(text)
    if status:
        if any(word in status for word in _STATUS_DONE_WORDS + _STATUS_REDO_WORDS):
            return False
        return any(word in status for word in _STATUS_TODO_WORDS)
    # 已交过的反向标志：出现任何一个就说明这一页带着上次的痕迹
    if any(hint in text for hint in _ANSWER_BLOCK_HINTS) \
       or ("重新作答" in text) or ("重新提交" in text) or ("已提交" in text) \
       or ("btnReEdit" in text):
        return False
    # 未提交的正向标志
    return any(m in text for m in
               ("待完成", "提交作业", "btnSave", "暂时保存", "尚未提交",
                "提交答案", "提交试卷"))


def page_state(html_text: str, has_questions: bool = False,
               has_verdicts: bool = False) -> str:
    """判断这个作业页处在什么状态（供「采集」给出准确原因）。

    判定顺序刻意用**正向标志**优先，少靠「没有答案就当未提交」这种反推，
    否则「已提交但平台不显示答案」会被误报成未提交。
    """
    if has_verdicts:
        return STATE_OK
    text = str(html_text or "")
    if not text.strip():
        return STATE_UNKNOWN
    if is_closed_page(text) and not has_questions:
        return STATE_CLOSED
    status = page_status(text)
    if status:
        # 页头状态是平台自己写的，优先按它归类
        if any(word in status for word in _STATUS_REDO_WORDS):
            # 交过、没过、允许重做 —— 但这页没留下判分痕迹
            return STATE_UNGRADED
        if any(word in status for word in _STATUS_DONE_WORDS):
            # 平台说已完成/已通过，但页面没给答案：答案在平台侧，采不到
            return STATE_PASSED
        if any(word in status for word in _STATUS_TODO_WORDS):
            return STATE_UNSUBMITTED
    if is_unsubmitted_page(text):
        return STATE_UNSUBMITTED
    # 已交过但平台没公布答案 / 没判分：靠答案区块或「重新作答」识别
    if any(hint in text for hint in _ANSWER_BLOCK_HINTS) \
       or ("重新作答" in text) or ("重新提交" in text) or ("已提交" in text) \
       or ("btnReEdit" in text):
        return STATE_UNGRADED
    if has_questions:
        return STATE_UNKNOWN          # 有题目但没有任何提交/批阅线索：结构不认识，需留样
    return STATE_UNKNOWN


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
    for qid, block in iter_question_blocks(html_text):
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


def _counts(verdicts: Dict[str, Any]) -> "Tuple[int, int, int]":
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
                timeout: float = 10.0,
                page_out: "Optional[Dict[str, str]]" = None) -> Dict[str, Any]:
    """重新拉取作业页并解析判分结果。

    ``params`` 与提交前抓题时用的参数一致（见 ``Chaoxing.study_work``）。
    ``page_out`` 传一个 dict 进来，会把拿到的页面 HTML 写进 ``page_out["html"]``
    （供 :func:`align_verdicts` 按题干把判分对齐到提交明细）。传 None 时不保留页面。

    任何异常都只记日志并返回空 dict —— 核验失败不应影响刷课主流程。
    """
    target = url or WORK_URL
    try:
        resp = session.get(target, params=params, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"作业判分核验请求失败：{type(exc).__name__}: {exc}")
        return {}
    text = getattr(resp, "text", "") or ""
    if page_out is not None:
        page_out["html"] = text
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


def align_verdicts(verdicts: Dict[str, Any], items: "List[dict]",
                   page_html: str = "") -> Dict[str, Any]:
    """把「按页面 qid 索引」的判分结果对齐到**提交明细**的 qid 上。

    为什么需要这一步：作答页的题目 id 与成绩页的 qid 未必是同一个值
    （新版页面把 qid 藏在隐藏域、旧版放在 ``data``，两套 DOM 混用时会对不上），
    一旦对不上，``LocalBank.learn`` 就一条都匹配不到 —— 现象是日志明明写着
    「对 4 / 错 1」，本地题库却「写入 0 题」，下次重做又把已经答对的题全部重搜一遍。

    对齐口径：先看提交明细的 qid 在判分里有没有直接命中，全部命中就原样返回；
    否则用成绩页的题干（``decode_work_questions`` 归一化后）做一次**唯一匹配**，
    匹配不唯一（同一份作业里题干重复）就不猜，留给日志报「对不上」。
    """
    if not verdicts or not items:
        return verdicts or {}
    direct = 0
    missing: "List[dict]" = []
    for item in items:
        if local_bank.verdict_of(verdicts, item.get("qid")) is None:
            missing.append(item)
        else:
            direct += 1
    if not missing:
        return verdicts                     # qid 本来就能对上，什么都不用做
    if not page_html:
        logger.warning(f"判分结果与提交明细的题号对不上（{len(missing)} 题），"
                       f"且没有页面可用来按题干对齐")
        return verdicts

    page_questions = parse_questions(page_html)
    if not page_questions:
        return verdicts
    by_title: Dict[str, "List[Tuple[str, Any]]"] = {}
    for qid, info in page_questions.items():
        verdict = local_bank.verdict_of(verdicts, qid)
        if verdict is None:
            continue
        key = local_bank.normalize_text(info.get("title"))
        if key:
            by_title.setdefault(key, []).append((qid, verdict))

    aligned = dict(verdicts)
    fixed = 0
    for item in missing:
        key = local_bank.normalize_text(item.get("title"))
        candidates = by_title.get(key) or []
        if len(candidates) != 1:            # 找不到 / 有歧义 -> 不猜
            continue
        item_qid = item.get("qid")
        if item_qid in (None, ""):
            continue
        aligned[str(item_qid)] = candidates[0][1]
        fixed += 1
    if fixed:
        logger.warning(f"判分结果与提交明细的题号对不上，已按题干对齐 {fixed} 题"
                       f"（另有 {len(missing) - fixed} 题未能对齐）")
    else:
        logger.warning(f"判分结果与提交明细的题号对不上，按题干也没对齐任何一题"
                       f"（{len(missing)} 题，本题库不收，等下轮判分）")
    return aligned
