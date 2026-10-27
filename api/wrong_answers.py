# -*- coding: utf-8 -*-
"""错题本：记住**被平台判错过**的答案，重答时避开，不再重复提交同一个错答案。

为什么需要它
------------
``progress_cache/cache.json``（``CacheDAO``）存的是「搜题接口返回的答案」，
**不校验对错**，答错的题也会被缓存住；``progress_cache/local_bank/`` 只存平台
确认过的正确答案，但**已经被平台判错**的旧条目（比如别的账号采集来的）并不会
被清掉。

于是会形成死循环：

    章节检测提交 -> 平台判「未通过」-> 重答 -> 命中缓存里的同一个错答案
                 -> 再次被判「未通过」-> 无限重答

本模块专门记「哪些答案被平台判过错」，按题目指纹归档（与本地题库同一套指纹：
归一化题干 + 题型 + 归一化选项集合），所以换账号、选项乱序都能对上同一道题。

记录的是**答案的文本形式**（选项正文 / 判断的对错），不是字母：选项顺序变了
也不会指错题。

对外接口
--------
    book = wrong_answers.get_book()
    book.record(fp, key)          # 记下一个被判错的答案
    book.known(fp)                # 这道题被判错过哪些答案
    book.forget_key(fp, key)      # 某个答案后来被判对了 -> 移除它
    book.clear(fp)                # 这道题答对了 -> 清掉它的错答记录
"""

import itertools
import json
import math
import os
import random
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Set

from api import local_bank
from api.logger import logger

FILE_NAME = "wrong_answers.json"
SCHEMA_VERSION = 1

_LOCK = threading.RLock()
_BOOK: Optional["WrongBook"] = None


# --------------------------------------------------------------------------- #
# 路径与指纹
# --------------------------------------------------------------------------- #
def default_path() -> str:
    """错题本文件路径（默认 ``progress_cache/wrong_answers.json``）。"""
    try:
        from paths import data_path
        return data_path(FILE_NAME)
    except Exception:  # noqa: BLE001 - api/ 被单独引用时退化为当前目录
        return FILE_NAME


def fingerprint_for(title: str, qtype: str, options: str = "") -> str:
    """题目指纹（与本地题库同一套，换账号 / 选项乱序也能对上）。"""
    return local_bank.fingerprint(title, qtype, options)


def answer_key(qtype: str, answer: Any, options: str = "") -> str:
    """答案 -> 可比较的键（见 :func:`api.local_bank.answer_key`）。"""
    return local_bank.answer_key(qtype, answer, options)


# --------------------------------------------------------------------------- #
# 错题本
# --------------------------------------------------------------------------- #
class WrongBook:
    """记录「被平台判错的答案」的持久化存储。

    文件结构::

        {"v": 1, "items": {"<题目指纹>": {"q": "题干", "y": "single",
                                         "w": ["甲选项"], "ts": 1700000000}}}
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = os.path.abspath(path or default_path())
        self._data: Dict[str, Any] = {"v": SCHEMA_VERSION, "items": {}}
        self._load()

    # ---------------- 读写 ----------------
    def _load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf8") as fp:
                data = json.load(fp)
            if not isinstance(data.get("items"), dict):
                raise ValueError("缺少 items")
            self._data = {"v": SCHEMA_VERSION, "items": data["items"]}
        except Exception as exc:  # noqa: BLE001 - 文件坏了就当空库，绝不中断刷课
            logger.error(f"错题本读取失败，已忽略：{exc}")
            self._data = {"v": SCHEMA_VERSION, "items": {}}

    def _save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".wrong_", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf8") as fp:
                json.dump(self._data, fp, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"错题本写入失败：{exc}")

    # ---------------- 记录 ----------------
    def known(self, fingerprint: str) -> Set[str]:
        """这道题被判错过哪些答案（键的集合）。"""
        with _LOCK:
            entry = self._data.get("items", {}).get(fingerprint) or {}
            return set(entry.get("w") or [])

    def is_wrong(self, fingerprint: str, key: str) -> bool:
        """这个答案是否已被平台判错过。"""
        return bool(key) and key in self.known(fingerprint)

    def record(self, fingerprint: str, key: str, title: str = "",
               qtype: str = "") -> bool:
        """记下一个被判错的答案（已记过则不重复写）。"""
        if not fingerprint or not key:
            return False
        with _LOCK:
            items = self._data.setdefault("items", {})
            entry = items.get(fingerprint) or {"w": [], "ts": 0}
            wrong = entry.setdefault("w", [])
            changed = False
            if key not in wrong:
                wrong.append(key)
                changed = True
            if title:
                entry["q"] = str(title)[:200]
            if qtype:
                entry["y"] = str(qtype)
            entry["ts"] = int(time.time())
            if changed:
                items[fingerprint] = entry
                self._save()
            return changed

    def forget_key(self, fingerprint: str, key: str) -> bool:
        """某个答案后来被判对了 -> 把它从错答记录里去掉。"""
        if not fingerprint or not key:
            return False
        with _LOCK:
            items = self._data.get("items", {})
            entry = items.get(fingerprint)
            if not entry:
                return False
            wrong = entry.get("w") or []
            if key not in wrong:
                return False
            wrong.remove(key)
            entry["ts"] = int(time.time())
            if not wrong:
                items.pop(fingerprint, None)
            self._save()
            return True

    def clear(self, fingerprint: str) -> bool:
        """这道题的所有错答记录都不要了（这题已经答对了）。"""
        with _LOCK:
            items = self._data.get("items", {})
            if fingerprint not in items:
                return False
            items.pop(fingerprint, None)
            self._save()
            return True

    def stats(self) -> dict:
        with _LOCK:
            items = self._data.get("items", {})
            return {
                "path": self.path,
                "questions": len(items),
                "wrong": sum(len((e or {}).get("w") or []) for e in items.values()),
            }


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #
def get_book() -> WrongBook:
    """取错题本单例（首次访问时创建）。"""
    global _BOOK
    with _LOCK:
        if _BOOK is None:
            _BOOK = WrongBook()
        return _BOOK


def reset_for_test() -> None:
    """仅供测试：丢弃单例。"""
    global _BOOK
    with _LOCK:
        _BOOK = None


# --------------------------------------------------------------------------- #
# 查询辅助：这道题被判错过哪些答案
# --------------------------------------------------------------------------- #
def known_keys(title: str, qtype: str, options: str = "") -> Set[str]:
    """这道题（按指纹）被判错过哪些答案键。"""
    if not title:
        return set()
    try:
        return get_book().known(fingerprint_for(title, qtype, options))
    except Exception as exc:  # noqa: BLE001 - 错题本异常不能影响正常答题
        logger.error(f"错题本查询异常：{type(exc).__name__}: {exc}")
        return set()


def is_wrong_answer(title: str, qtype: str, options: str, answer: Any) -> bool:
    """这个答案是否已被平台判错过。"""
    key = local_bank.answer_key(qtype, answer, options)
    if not key:
        return False
    return key in known_keys(title, qtype, options)


def record_wrong(title: str, qtype: str, options: str, answer: Any) -> bool:
    """记下一个被判错的答案（按题目指纹归档）。"""
    if not title:
        return False
    key = local_bank.answer_key(qtype, answer, options)
    if not key:
        return False
    try:
        return get_book().record(fingerprint_for(title, qtype, options), key,
                                 title=title, qtype=qtype)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"错题本写入异常：{type(exc).__name__}: {exc}")
        return False


# --------------------------------------------------------------------------- #
# 排除法：把「已知错答」变成可用信息
# --------------------------------------------------------------------------- #
def excluded_texts(qtype: str, options: str, known: Set[str]) -> List[str]:
    """已知错答 -> 可以直接甩给大模型的「别选这些」文本。

    * 单选：被判错的那个选项正文；
    * 判断：被判错的那个判断（正确 / 错误）；
    * 多选 / 填空 / 简答：给不出逐项排除（多选错在「组合」上），返回空列表。
    """
    qtype = str(qtype or "").strip().lower()
    texts: List[str] = []
    if not known:
        return texts
    if qtype == "single":
        for line in local_bank.option_lines(options):
            letter = local_bank.option_letter(line)
            if not letter:
                continue
            if local_bank.answer_key(qtype, letter, options) in known:
                text = local_bank.option_text(line)
                if text and text not in texts:
                    texts.append(text)
    elif qtype == "judgement":
        if "T" in known:
            texts.append("正确")
        if "F" in known:
            texts.append("错误")
    return texts


def hint_texts(qtype: str, options: str, known: Set[str]) -> List[str]:
    """给大模型看的「别再选这些」提示句。"""
    qtype = str(qtype or "").strip().lower()
    if not known:
        return []
    if qtype == "single":
        texts = excluded_texts(qtype, options, known)
        if texts:
            return ["以下选项已被平台判为错误，请不要选择：" + "、".join(texts)]
        return []
    if qtype == "judgement":
        texts = excluded_texts(qtype, options, known)
        if texts:
            return ["以下判断已被平台判为错误，请给出相反的判断：" + "、".join(texts)]
        return []
    if qtype == "multiple":
        combos = []
        for key in known:
            parts = [p for p in str(key).split("|") if p]
            if parts:
                combos.append("、".join(parts))
        if combos:
            return ["以下答案组合曾被平台判为错误，请给出与之不同的组合："
                    + "；".join(combos)]
    return []


# --------------------------------------------------------------------------- #
# 多选题：被判错的是「组合」，不是「某个选项」
# --------------------------------------------------------------------------- #
# 穷举上限：超过就退化为随机抽样，避免 20 个选项的组合数把 CPU 打满。
_ENUM_LIMIT = 20000


def option_letters(options: str) -> List[str]:
    """选项字母（按出现顺序去重）。"""
    out: List[str] = []
    for line in local_bank.option_lines(options):
        letter = local_bank.option_letter(line)
        if letter and letter not in out:
            out.append(letter)
    return out


def size_order(want: int, total: int) -> List[int]:
    """多选题的「先试几个选项」顺序：先原尺寸，再按常见度 2/3/4…，1 个放最后。"""
    if total < 1:
        return []
    want = max(1, min(int(want or 1), total))
    order = [want] + [s for s in range(2, total + 1) if s != want]
    if want != 1:
        order.append(1)
    return order


def multiple_pool(letters: List[str], options: str, known: Set[str],
                  size: int) -> List[str]:
    """穷举「选 ``size`` 个」里**没被判错过**的字母串（``"AB"`` 形态，字母已排序）。

    多选的判错是**组合级**的：``"甲|乙"`` 判错只说明「只选甲+乙」错，甲、乙本身
    完全可能是正确答案的一部分。所以这里只按组合键排除，**绝不**因为「某个选项
    单独提交过被判错」就把这个选项从所有组合里剔掉——那会把候选空间砍空，最后
    误报「该题可选答案都已被平台判错」。
    """
    if size < 1 or size > len(letters):
        return []
    if math.comb(len(letters), size) <= _ENUM_LIMIT:
        pool: List[str] = []
        for combo in itertools.combinations(letters, size):
            picked = "".join(combo)
            if local_bank.answer_key("multiple", picked, options) not in known:
                pool.append(picked)
        return pool
    pool = []
    seen: Set[str] = set()
    for _ in range(_ENUM_LIMIT):
        picked = "".join(sorted(random.sample(letters, size)))
        if picked in seen:
            continue
        if local_bank.answer_key("multiple", picked, options) in known:
            continue
        seen.add(picked)
        pool.append(picked)
    return pool


def alternative_for(qtype: str, options: str, known: Set[str],
                    current: str = "") -> Optional[str]:
    """这道题刚提交的答案已被判错 -> 换一个（排除法）。

    返回的是**可以直接交给 ``study_work`` 的答案形态**（与题库/大模型返回的一致）：
    单选给选项正文、多选给多行正文、判断给 ``正确`` / ``错误``。

    * 单选：在**没被判错**的选项里随机挑一个；
    * 多选：换一组**没被判错**的组合——先保持「选几个」不变，同尺寸的组合都判错过
      了再放宽到其它尺寸；尺寸内**穷举**，所以「换不出来」是真的无解，不是没抽到；
    * 判断：取反；
    * 填空 / 简答：换不了（返回 None）。

    返回 ``None`` 表示「没有别的可选答案了」。
    """
    qtype = str(qtype or "").strip().lower()
    if not known:
        return current or None
    if qtype == "judgement":
        current_key = local_bank.judgement_key(current)
        if current_key == "T":
            return "错误"
        if current_key == "F":
            return "正确"
        # 认不出当前答案：给出没被判错的那个
        if "T" in known and "F" not in known:
            return "错误"
        if "F" in known and "T" not in known:
            return "正确"
        return None
    if qtype == "single":
        allowed: List[str] = []
        for line in local_bank.option_lines(options):
            letter = local_bank.option_letter(line)
            if not letter:
                continue
            if local_bank.answer_key(qtype, letter, options) in known:
                continue
            text = local_bank.option_text(line) or letter
            allowed.append(text)
        if not allowed:
            return None
        return random.choice(allowed)
    if qtype == "multiple":
        texts: Dict[str, str] = {}
        for line in local_bank.option_lines(options):
            letter = local_bank.option_letter(line)
            if letter:
                texts[letter] = local_bank.option_text(line) or letter
        letters = list(texts)
        if not letters:
            return None
        cur = sorted(local_bank.pure_letters(current) or "")
        want = len(cur) or min(2, len(letters))
        for n in size_order(want, len(letters)):
            pool = multiple_pool(letters, options, known, n)
            if not pool:
                continue        # 这个尺寸的组合全被判错过 -> 换下一个尺寸
            others = [c for c in pool if list(c) != cur] or pool
            return "\n".join(texts[c] for c in random.choice(others))
        return None
    return None


# --------------------------------------------------------------------------- #
# 提交形态：study_work 直接填进表单的那种答案（字母 / true|false）
# --------------------------------------------------------------------------- #
def safe_letters(qtype: str, options: str, known: Set[str]) -> List[str]:
    """没被判错过的选项字母。

    **只对单选有意义**：多选的判错是组合级的（``"甲|乙"`` 判错不代表甲、乙本身
    错），按单个字母筛会把正确答案的组成部分一起剔掉（多选请走
    :func:`multiple_pool`）。
    """
    out: List[str] = []
    for line in local_bank.option_lines(options):
        letter = local_bank.option_letter(line)
        if not letter:
            continue
        if known and local_bank.answer_key(qtype, letter, options) in known:
            continue
        out.append(letter)
    return out


def pick_submit_answer(qtype: str, options: str, known: Set[str],
                       current: str = "", size: Optional[int] = None) -> Optional[str]:
    """**提交形态**下避开已知错答。

    与 :func:`alternative_for` 的分工：``alternative_for`` 服务题库层（返回选项正文，
    供 ``study_work`` 做「正文 -> 字母」映射）；本函数直接在提交形态上干活 ——
    单选/多选返回字母串（``"B"`` / ``"AC"``），判断返回 ``true`` / ``false``，
    ``study_work`` 填表单前直接可用。随机作答也走这里，否则「2 选 1」的判断题
    有 50% 概率再抽中同一个错答案，重答只是把错答案再交一遍。

    ``current`` 给出当前答案时**优先换成别的**；``size`` 指定多选题要选几个
    （随机作答按原加权策略挑好个数再交进来，排除法优先保持「选几个」不变）。
    **多选题同一尺寸的组合都试过、或正确答案本就不是这个尺寸时，会自动放宽到
    其它尺寸**——否则「正确答案要选 3 个、当前错答案只选了 2 个」会卡死在 2 个
    选项的组合里，永远交不出正确答案。

    返回 ``None`` 表示**真的**换不出来：单选/判断是全部选项都判错过，多选是
    「穷举后所有尺寸、所有组合都判错过」（不是随机抽样没抽到），填空/简答是这类
    题型没有排除法可用。
    """
    qtype = str(qtype or "").strip().lower()
    if not known:
        return None
    if qtype == "judgement":
        key = local_bank.judgement_key(current)
        if key == "T":
            return "false" if "F" not in known else None
        if key == "F":
            return "true" if "T" not in known else None
        if "T" in known and "F" not in known:
            return "false"
        if "F" in known and "T" not in known:
            return "true"
        return None
    if qtype == "single":
        allowed = safe_letters("single", options, known)
        if not allowed:
            return None
        cur = local_bank.pure_letters(current) or ""
        others = [c for c in allowed if c != cur] or allowed
        return random.choice(others)
    if qtype == "multiple":
        letters = option_letters(options)
        if not letters:
            return None
        cur = "".join(sorted(local_bank.pure_letters(current) or ""))
        want = int(size or 0) or len(cur) or min(2, len(letters))
        for n in size_order(want, len(letters)):
            pool = multiple_pool(letters, options, known, n)
            if not pool:
                continue        # 这个尺寸的组合全被判错过 -> 换下一个尺寸
            others = [c for c in pool if c != cur] or pool
            return random.choice(others)
        return None
    return None
