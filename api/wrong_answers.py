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

import json
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


def alternative_for(qtype: str, options: str, known: Set[str],
                    current: str = "") -> Optional[str]:
    """这道题刚提交的答案已被判错 -> 换一个（排除法）。

    * 单选：在**没被判错**的选项里随机挑一个；
    * 多选：换一组**同样长度**、且没被判错过的组合；
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
            return "false"
        if current_key == "F":
            return "true"
        return None
    if qtype == "single":
        allowed = []
        for line in local_bank.option_lines(options):
            letter = local_bank.option_letter(line)
            if not letter:
                continue
            if local_bank.answer_key(qtype, letter, options) in known:
                continue
            allowed.append(letter)
        if not allowed:
            return None
        return random.choice(allowed)
    if qtype == "multiple":
        letters = [local_bank.option_letter(line) for line in local_bank.option_lines(options)]
        letters = [x for x in letters if x]
        if not letters:
            return None
        size = len(local_bank.pure_letters(current) or "") or min(2, len(letters))
        size = min(size, len(letters))
        for _ in range(20):
            picked = "".join(sorted(random.sample(letters, size)))
            if local_bank.answer_key(qtype, picked, options) not in known:
                return picked
        return None
    return None
