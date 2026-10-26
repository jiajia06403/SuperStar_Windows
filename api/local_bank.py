# -*- coding: utf-8 -*-
"""本地题库：只存**平台确认过的正确答案**，重做 / 换账号时直接命中。

与 ``progress_cache/cache.json`` 的区别
--------------------------------------
``CacheDAO`` 缓存的是题库接口返回的答案，不校验对错，任何一次搜题结果都会落盘。
本地题库只记录**提交后由平台判分确认**的正确答案，因此可以放心在
「重做同一份作业」「换个账号做同一门课」时直接读取，不再消耗题库/大模型额度。

收录口径（只存正确答案）
------------------------
1. 平台判对 -> 收录本次提交的答案；
2. 平台判错、但页面给出了「正确答案」-> 收录正确答案（下次直接答对）；
3. 判错且拿不到正确答案 -> 不收录（不知道对的是什么，收进去只会带错别人）；
4. 拿不到判分信息 / 没做核验 -> 一律不收录。题库里不存在「猜测的答案」。

存放结构（同一门课题目和答案相同 -> 只按课程分文件，全账号共享）
--------------------------------------------------------------
    progress_cache/local_bank/
        高等数学_246831735.json        <- 一门课一个文件
        大学英语_247100002.json

不按账号分文件夹：换账号做同一门课直接命中同一份文件，也不需要重复采集。
同一道题若在多门课里出现，查询会先看当前课程文件，再兜底查其它课程文件。

只补缺失的题 + 答错就纠错
--------------------------
作业提交后核验整份作业，写库时按「值不值得写」判断（``learn(only_missing=True)``）：

* 题库里**没有**该题的正确答案 -> 写入；
* 题库里已有答案且与平台给出的正确答案**等价** -> 跳过（不重复写）；
* 题库里已有答案但与平台给出的正确答案**不等价** -> **覆写纠正**
  （典型场景：上次用题库答案作答被判错，说明库里那条答案是坏的）；
* 判错且页面不公布正确答案 -> 一律不写（不知道对的是什么）。

因此重复做同一份作业、多人共用同一份题库都不会产生无谓写入，
但题库里答错的题会被平台的正确答案修好。

核心设计
--------
1. **指纹**：``sha1(归一化标题 + 题型 + 归一化选项)``。标题去掉题号与分值，
   选项逐行归一化后排序，保证同一道题在不同账号、不同章节里得到同一个键。
2. **按选项文本重映射**：入库时额外保存答案对应的选项文本。平台若打乱选项顺序，
   查询时按文本找回字母；文本对不上就返回 None，宁可不答也不答错。
3. **原子写 + 损坏备份**：写一半崩溃不会把题库写坏；文件损坏只备份并忽略，
   绝不因此中断刷课。

配置（``[tiku]`` 段，见 config_win.ini）
--------------------------------------
    local_bank         true/false  总开关（false = 完全不读不写本地题库）
    local_bank_verify  true/false  提交后是否核验并补录缺失的正确答案（false = 题库只读）
    local_bank_dir                 题库根目录（留空用 progress_cache/local_bank）
"""
import json
import os
import re
import shutil
import tempfile
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from api.logger import logger

SCHEMA_VERSION = 3
DIR_NAME = "local_bank"            # 默认根目录（progress_cache 下）
LEGACY_NAME = "local_bank.json"    # 旧版单文件，启动时自动迁移
BUNDLE_KIND = "superstar-local-bank"
UNKNOWN_COURSE = "未命名课程"
LEGACY_COURSE = "旧版题库"
MIGRATED_SUFFIX = ".migrated"      # 旧「账号文件夹」迁移后改名，保留数据不删

_LOCK = threading.RLock()
_BANK: "Optional[BankStore]" = None
_MIGRATED = False                  # 旧版单文件迁移每个进程只尝试一次

_WS_RE = re.compile(r"[\s\u3000]+")
_LEAD_NO_RE = re.compile(r"^\s*\d+\s*[.、．)）]\s*")
_LEAD_PUNCT_RE = re.compile(r"^[.、．)）:：\-—\s]+")
_LEAD_TAG_RE = re.compile(r"^[\[【(（]\s*(?:单选|多选|判断|填空|简答|论述|名词解释)题?\s*[\]】)）]\s*")
_TAIL_SCORE_RE = re.compile(r"[（(]\s*\d+(?:\.\d+)?\s*分\s*[)）]\s*$")
_TAIL_EMPTY_RE = re.compile(r"[（(]\s*[)）]\s*$")
_OPTION_LETTER_RE = re.compile(r"^\s*([A-Za-z])\s*[.、．)）:：]?\s*")
_BAD_FS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


# --------------------------------------------------------------------------- #
# 归一化 / 指纹
# --------------------------------------------------------------------------- #
def normalize_text(text: str) -> str:
    """标题/选项归一化：去题型标签、题号、末尾分值与空括号，压缩空白。

    目的是让「同一道题」在不同账号、不同章节、不同页面上得到一致的串：
    页面标题可能带 ``1.`` 题号、``（2.0分）`` 分值、``【单选题】`` 标签。
    """
    value = str(text or "")
    value = value.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    value = _TAIL_SCORE_RE.sub("", value)
    value = _TAIL_EMPTY_RE.sub("", value)
    value = _LEAD_TAG_RE.sub("", value)
    value = _LEAD_NO_RE.sub("", value)
    value = _LEAD_PUNCT_RE.sub("", value)
    return _WS_RE.sub("", value).strip()


def option_lines(options: str) -> "List[str]":
    """把 ``"A. x\\nB. y"`` 拆成行列表（保持原顺序）。"""
    return [line for line in str(options or "").replace("\r", "").split("\n") if line.strip()]


def option_text(line: str) -> str:
    """取选项正文（去掉 ``A.`` / ``A、`` 这类前缀）。"""
    return normalize_text(_OPTION_LETTER_RE.sub("", str(line or ""), count=1))


def option_letter(line: str) -> str:
    """取选项字母（取不到返回空串）。"""
    matched = _OPTION_LETTER_RE.match(str(line or ""))
    return matched.group(1).upper() if matched else ""


def fingerprint(title: str, qtype: str, options: str = "") -> str:
    """题目指纹：同一道题在不同账号/章节/选项顺序下应得到同一个值。"""
    import hashlib

    parts = [normalize_text(title), str(qtype or "").strip().lower()]
    parts.extend(sorted(option_text(line) for line in option_lines(options) if option_text(line)))
    raw = "\x1f".join(parts)
    return hashlib.sha1(raw.encode("utf8")).hexdigest()[:20]


def answer_letters(answer: str) -> str:
    """从答案里抽出选项字母（``"A,C"`` / ``"AC"`` / ``"A"`` 都归为 ``"AC"``）。"""
    letters = re.findall(r"[A-Za-z]", str(answer or ""))
    return "".join(sorted({c.upper() for c in letters}))


def _match_letter(text: str, lines: "List[str]") -> str:
    """按选项文本在当前选项里找字母：先精确，再互相包含（容忍标点差异）。"""
    target = normalize_text(text)
    if not target:
        return ""
    for line in lines:                       # 精确匹配
        if option_text(line) == target:
            return option_letter(line) or line.strip()[:1].upper()
    for line in lines:                       # 包含匹配（题干里带了多余标点/空格）
        body = option_text(line)
        if body and (target in body or body in target):
            return option_letter(line) or line.strip()[:1].upper()
    return ""


def remap_answer(entry: dict, options: str) -> Optional[str]:
    """把历史答案映射到「当前这版选项」的字母串；映射不上返回 None。

    映射策略（从严到宽）：
      1. 答案文本逐项按文本匹配当前选项 -> 得到新字母；
      2. 库里没有答案文本（入库时选项解析不出字母）时，若原字母在当前选项里
         都存在，直接沿用原字母；
      3. 其余情况 -> None（宁可随机作答，也不提交一个必然错的答案）。

    注意第 2 条只在「没有文本证据」时启用。有文本却对不上，说明选项改写了，
    此时沿用旧字母极可能答错，所以直接放弃。
    """
    lines = option_lines(options)
    letters = answer_letters(entry.get("a", ""))
    texts = [str(t or "") for t in (entry.get("x") or [])]

    if lines and letters:
        if texts:
            mapped = []
            for text in texts:
                letter = _match_letter(text, lines)
                if not letter:
                    return None
                mapped.append(letter)
            return "".join(sorted(set(mapped)))
        current = {option_letter(line) for line in lines if option_letter(line)}
        if current and set(letters).issubset(current):
            return letters
        return None
    if letters and not lines:
        return letters
    return None


# --------------------------------------------------------------------------- #
# 路径 / 命名
# --------------------------------------------------------------------------- #
def default_root() -> str:
    """默认题库根目录：progress_cache/local_bank（与进度缓存同目录）。"""
    try:
        from paths import data_path
        return data_path(DIR_NAME)
    except Exception:  # noqa: BLE001  api/ 被单独引用时退化为当前目录
        return DIR_NAME


def legacy_path() -> str:
    """旧版单文件路径：progress_cache/local_bank.json。"""
    try:
        from paths import data_path
        return data_path(LEGACY_NAME)
    except Exception:  # noqa: BLE001
        return LEGACY_NAME


def safe_name(value: str, default: str = "", limit: int = 48) -> str:
    """把课程名变成合法文件名：去掉 Windows 非法字符，限制长度。"""
    text = str(value or "").strip()
    text = _BAD_FS_RE.sub("_", text)
    text = _WS_RE.sub(" ", text).strip(" .")
    if len(text) > limit:
        text = text[:limit].rstrip(" .")
    return text or default


def course_filename(course: str = "", course_id: str = "") -> str:
    """课程文件名：``标题_课程ID.json``（有哪个用哪个，都没有则「未命名课程」）。"""
    title = safe_name(course, "")
    cid = safe_name(course_id, "", limit=20)
    if title and cid:
        return f"{title}_{cid}.json"
    if title:
        return f"{title}.json"
    if cid:
        return f"{cid}.json"
    return f"{UNKNOWN_COURSE}.json"


def _truthy(value, default: bool = True) -> bool:
    raw = None if value is None else str(value).strip()
    if not raw:
        return default
    return raw.lower() in ("true", "1", "yes", "on")


def _verdict_of(verdicts: dict, qid: Any) -> Any:
    """取某题的核验结果：页面 qid 是字符串、提交表单里可能是数字，两种都试。"""
    if not verdicts:
        return None
    try:
        value = verdicts.get(qid)
        return value if value is not None else verdicts.get(str(qid))
    except Exception:  # noqa: BLE001
        return None


def _atomic_write_json(path: str, payload: dict) -> None:
    """原子写：临时文件 + replace，避免写一半崩溃把文件写坏。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".bank_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=1, sort_keys=True)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, os.path.abspath(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str) -> dict:
    with open(path, encoding="utf8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise ValueError("题库文件格式不正确（应为 JSON 对象）")
    return data


# --------------------------------------------------------------------------- #
# 单文件题库
# --------------------------------------------------------------------------- #
class LocalBank:
    """单个题库文件的读写与合并。实例是线程安全的；查询命中不产生任何网络请求。

    通常通过 :class:`BankStore` 使用（按课程分文件）；直接构造本类适合
    做单文件读写、导入导出与测试。
    """

    def __init__(self, path: Optional[str] = None, enabled: bool = True,
                 verify: bool = True, trust: bool = False,
                 true_word: str = "正确", false_word: str = "错误") -> None:
        self.path = os.path.abspath(path or default_root())
        self.enabled = bool(enabled)
        self.verify = bool(verify)
        self.trust = bool(trust)
        self.true_word = true_word or "正确"
        self.false_word = false_word or "错误"
        self._data: dict = {"v": SCHEMA_VERSION, "updated": 0, "entries": {}}
        self._mtime = None
        self.reload()

    # ---------------- 配置 / 状态 ----------------
    @classmethod
    def from_config(cls, conf: "dict | None") -> "LocalBank":
        """用 ``[tiku]`` 配置段构造实例（缺项按默认值）。"""
        conf = conf or {}

        def _get(key: str, default: str = "") -> str:
            try:
                value = conf.get(key, default)
            except Exception:  # noqa: BLE001  ConfigParser 段对象行为不一致时兜底
                return default
            return default if value is None else str(value)

        true_list = [w.strip() for w in _get("true_list", "正确,对").split(",") if w.strip()]
        false_list = [w.strip() for w in _get("false_list", "错误,错").split(",") if w.strip()]
        return cls(
            path=_get("local_bank_dir").strip() or _get("local_bank_path").strip() or None,
            enabled=_truthy(_get("local_bank", "true"), True),
            verify=_truthy(_get("local_bank_verify", "true"), True),
            trust=_truthy(_get("local_bank_trust", "false"), False),
            true_word=true_list[0] if true_list else "正确",
            false_word=false_list[0] if false_list else "错误",
        )

    @property
    def usable(self) -> bool:
        """是否可参与答题（关闭时既不读也不写）。"""
        return bool(self.enabled)

    def stats(self) -> dict:
        with _LOCK:
            self._refresh_if_changed()
            entries = self._data.get("entries", {})
            by_type: dict = {}
            for entry in entries.values():
                key = str(entry.get("y") or "unknown")
                by_type[key] = by_type.get(key, 0) + 1
            size = 0
            try:
                size = os.path.getsize(self.path)
            except OSError:
                pass
            return {
                "path": self.path,
                "enabled": self.enabled,
                "verify": self.verify,
                "trust": self.trust,
                "total": len(entries),
                "by_type": by_type,
                "updated": self._data.get("updated", 0),
                "size": size,
            }

    # ---------------- 磁盘读写 ----------------
    def reload(self) -> None:
        """从磁盘重载（文件不存在视为空库，不创建，避免无谓落盘）。"""
        with _LOCK:
            self._data = self._read_file(self.path) or {
                "v": SCHEMA_VERSION, "updated": 0, "entries": {}}
            try:
                self._mtime = os.path.getmtime(self.path)
            except OSError:
                self._mtime = None

    def _read_file(self, path: str) -> "Optional[dict]":
        """读取题库文件；格式非法时备份坏文件并返回 None（绝不因题库损坏中断刷课）。"""
        if not os.path.isfile(path):
            return None
        try:
            data = _read_json(path)
            if not isinstance(data.get("entries"), dict):
                raise ValueError("缺少 entries")
            return data
        except Exception as exc:  # noqa: BLE001
            logger.error(f"本地题库读取失败，已忽略：{exc}")
            try:
                backup = f"{path}.bad.{int(time.time())}"
                os.replace(path, backup)
                logger.error(f"损坏的本地题库已备份为：{backup}")
            except OSError:
                pass
            return None

    def set_meta(self, **meta: Any) -> None:
        """记录课程归属信息（随文件一起写入，导出时用于还原课程文件）。"""
        with _LOCK:
            for key, value in meta.items():
                if value:
                    self._data[key] = value

    def _write(self) -> None:
        """原子写入（临时文件 + replace），避免写一半崩溃把题库写坏。"""
        with _LOCK:
            data = dict(self._data)
            data["v"] = SCHEMA_VERSION
            data["updated"] = int(time.time())
            _atomic_write_json(self.path, data)
            self._data = data
            try:
                self._mtime = os.path.getmtime(self.path)
            except OSError:
                self._mtime = None

    def _refresh_if_changed(self) -> None:
        """外部（别的账号进程/手工编辑/导入）改过文件时自动重载。"""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if self._mtime is not None and mtime == self._mtime:
            return
        with _LOCK:
            self._data = self._read_file(self.path) or {
                "v": SCHEMA_VERSION, "updated": 0, "entries": {}}
            self._mtime = mtime

    # ---------------- 查询 ----------------
    def entry(self, key: str) -> Optional[dict]:
        """按指纹取条目（会先检查磁盘有没有被别的进程改过）。"""
        try:
            self._refresh_if_changed()
        except Exception:  # noqa: BLE001
            pass
        with _LOCK:
            return self._data.get("entries", {}).get(key)

    def snapshot(self) -> dict:
        """返回条目副本（统计 / 导出用）。"""
        with _LOCK:
            self._refresh_if_changed()
            return dict(self._data.get("entries", {}))

    def lookup(self, title: str, qtype: str, options: str = "") -> Optional[str]:
        """查题：命中返回可直接提交的答案；未命中/不可用返回 None。"""
        if not self.enabled:
            return None
        try:
            key = fingerprint(title, qtype, options)
            entry = self.entry(key)
            if not entry:
                return None
            answer = self.answer_for(entry, qtype, options)
            if not answer:
                logger.debug(f"本地题库命中但无法映射到当前选项，已跳过：{title}")
                return None
            logger.info(f"本地题库命中：{normalize_text(title)} -> {answer}")
            return answer
        except Exception as exc:  # noqa: BLE001
            logger.error(f"本地题库查询异常：{type(exc).__name__}: {exc}")
            return None

    def answer_for(self, entry: dict, qtype: str, options: str = "") -> Optional[str]:
        """把库里的答案转成本次可直接使用的答案字符串。"""
        stored = str(entry.get("a") or "").strip()
        if not stored:
            return None
        if qtype == "judgement":
            low = stored.strip().lower()
            if low in ("true", "1", "yes", "对", "正确", "√", "t"):
                return self.true_word
            if low in ("false", "0", "no", "错", "错误", "×", "f"):
                return self.false_word
            return None
        if qtype in ("single", "multiple"):
            return remap_answer(entry, options)
        return stored                      # 填空 / 简答：原文照用

    # ---------------- 入库 ----------------
    def learn(self, items: Iterable[dict], verdicts: "dict | None" = None,
              course: str = "", allow_unverified: bool = False,
              only_missing: bool = True) -> int:
        """批量入库，**只收录平台确认过的正确答案**。

        ``items``：每题一项 ``{"qid", "title", "type", "options", "answer"}``
        ``verdicts``：``{qid: verdict}``，缺项视为「没核验」
            - ``{"ok": True}``                 判对 -> 收录本次答案
            - ``{"ok": False, "answer": "A"}`` 判错但有正确答案 -> **收录正确答案**
            - ``{"answer": "A"}``              只有正确答案 -> 收录正确答案
            - ``{"ok": False}`` / ``False``    判错且无正确答案 -> 不收录
            - ``"A"``                          旧式写法：直接当正确答案
        ``verdicts`` 为 None（没做核验）-> 一条都不收：题库只存正确答案。
        ``only_missing=True`` -> 库里已有正确答案的题不再重复写；但平台给出的
        正确答案与库里**不等价**时覆盖纠正（题库答错就改过来）。

        ``allow_unverified`` 已废弃，保留仅为兼容旧调用方，**不再生效**。

        返回实际写入的条目数（新增 + 纠正）。
        """
        if not self.enabled or not verdicts:
            return 0
        items = [it for it in (items or []) if str(it.get("answer") or "").strip()
                 or isinstance(_verdict_of(verdicts, it.get("qid")), (dict, str))]
        if not items:
            return 0

        learned = right = fixed = corrected = skipped = 0
        with _LOCK:
            self._refresh_if_changed()
            entries = self._data.setdefault("entries", {})
            for item in items:
                qid = item.get("qid")
                verdict = _verdict_of(verdicts, qid)   # 页面 qid 是字符串，提交时可能是数字
                if verdict is None:
                    continue

                answer, options, kind = self._decide(item, verdict)
                if not answer:
                    continue
                qtype = str(item.get("type") or "")
                item_options = str(item.get("options") or "")
                # 判断题字母得先换成「对/错」文字，换不出来说明无从判断对错 -> 跳过
                if not self._normalize_answer(answer, qtype, options or item_options):
                    continue
                key = fingerprint(item.get("title", ""), qtype, item_options)
                existed = False
                if only_missing:
                    old = entries.get(key) or {}
                    # 「已有正确答案」= 有答案且能用到当前这版选项上；映射不上的旧答案
                    # 视为缺答案，允许用平台给的正确结果覆盖（修坏数据）
                    if str(old.get("a") or "").strip() and self.answer_for(
                            old, qtype, item_options):
                        if self._same_answer(old, qtype, item_options, answer):
                            skipped += 1           # 库里答案与平台一致，不必重写
                            continue
                        existed = True             # 库里答案和平台不一致 -> 覆写纠正
                if self._upsert(entries, key, item, answer, course, options):
                    learned += 1
                    right += 1 if kind == "right" else 0
                    fixed += 1 if kind == "fixed" else 0
                    corrected += 1 if existed else 0
            if learned:
                try:
                    self._write()
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"本地题库写入失败：{exc}")
                    return 0

        if learned:
            detail = []
            if right:
                detail.append(f"{right} 题平台判对")
            if fixed:
                detail.append(f"{fixed} 题按平台正确答案写入")
            if corrected:
                detail.append(f"{corrected} 题纠正了库里的错误答案")
            if skipped:
                detail.append(f"跳过 {skipped} 题（题库答案与平台一致）")
            logger.info(f"本地题库已写入 {learned} 题（{'，'.join(detail) or '正确答案'}）")
        return learned

    @staticmethod
    def _decide(item: dict, verdict: Any):
        """只认「平台确认过的正确答案」。返回 ``(答案, 映射用选项, "right"/"fixed")``。"""
        submitted = str(item.get("answer") or "").strip()
        if isinstance(verdict, dict):
            override = str(verdict.get("answer") or "").strip()
            if override:                  # 平台标准答案：判对/判错都以它为准
                return override, str(verdict.get("options") or "").strip(), \
                    ("fixed" if verdict.get("ok") is False else "right")
            if verdict.get("ok") is True:  # 判对：本次提交的答案即正确答案
                return submitted, "", "right"
            return "", "", ""              # 判错又没正确答案 -> 不知道对的是什么
        if isinstance(verdict, str) and verdict.strip():   # 兼容旧格式：直接给正确答案
            return verdict.strip(), "", "fixed"
        if verdict is True:
            return submitted, "", "right"
        return "", "", ""

    @staticmethod
    def _normalize_answer(answer: Any, qtype: str, options: str = "") -> str:
        """入库口径归一化：判断题的 ``A`` / ``B`` 换成选项文字（对/错）。

        判断题只给了字母又没有选项可对照时返回空串（无法判定对错，宁可不写）。
        """
        value = str(answer or "").strip()
        if qtype == "judgement":
            if re.fullmatch(r"[A-Ha-h]", value):
                for line in option_lines(options):
                    if option_letter(line) == value.upper():
                        return option_text(line) or ""
                return ""
            return value
        return value

    def _same_answer(self, entry: dict, qtype: str, options: str, answer: Any) -> bool:
        """库里答案与「本次平台给出的正确答案」是否等价（选项顺序/写法不同也算相同）。"""
        try:
            left = self.answer_for(entry, qtype, options)
            if not left:
                return False
            normalized = self._normalize_answer(answer, qtype, options)
            if not normalized:
                return False
            right = self.answer_for({"a": normalized}, qtype, options)
            return bool(right) and left == right
        except Exception:  # noqa: BLE001 - 比较失败按「不同」处理，宁可多写一次
            return False

    def _upsert(self, entries: dict, key: str, item: dict, answer: str, course: str,
                options: str = "") -> bool:
        qtype = str(item.get("type") or "")
        item_options = str(item.get("options") or "")
        # 答案字母来自哪个页面，就用哪个页面的选项去取文本（判分页的字母别配提交页的选项）
        text_source = options or item_options
        # 判断题的「正确答案：A」指的是选项 A（对/错）：换成文字再入库，
        # 这样下次直接用配置里的 true_list/false_list 作答。
        answer = self._normalize_answer(answer, qtype, text_source)
        letters = answer_letters(answer) if qtype in ("single", "multiple") else ""
        texts: "List[str]" = []
        if letters:
            by_letter = {}
            for line in option_lines(text_source):
                letter = option_letter(line)
                if letter:
                    by_letter[letter] = option_text(line)
            texts = [by_letter[c] for c in letters if by_letter.get(c)]
            if len(texts) != len(letters):     # 选项解析不出字母 -> 无法按文本重映射
                texts = []
        old = entries.get(key) or {}
        same = str(old.get("a") or "") == answer
        entries[key] = {
            "q": normalize_text(item.get("title", ""))[:300],
            "y": qtype,
            "a": answer,
            "x": texts,
            "o": item_options[:1000],
            "ok": int(old.get("ok") or 0) + 1 if same else 1,
            "ts": int(time.time()),
            **({"c": course[:80]} if course else ({"c": old["c"]} if old.get("c") else {})),
        }
        return True

    def replace_entries(self, entries: dict, create: bool = True) -> None:
        """整体替换条目并落盘（导入 / 清空用）。``create=False`` 时不写入空库。"""
        with _LOCK:
            if not entries and not create:
                return
            self._data["entries"] = dict(entries)
            self._write()

    # ---------------- 导出 / 导入 / 合并 ----------------
    def export_to(self, path: str) -> dict:
        """导出为独立题库文件（可直接发给别人导入）。"""
        with _LOCK:
            self._refresh_if_changed()
            payload = {
                "v": SCHEMA_VERSION,
                "updated": int(time.time()),
                "entries": json.loads(json.dumps(self._data.get("entries", {}))),
            }
        _atomic_write_json(path, payload)
        logger.info(f"本地题库已导出：{path}（{len(payload['entries'])} 条）")
        return {"total": len(payload["entries"]), "path": path}

    def import_from(self, path: str, replace: bool = False) -> dict:
        """导入另一个题库文件并合并进本库。

        ``replace=True`` 时用导入内容整体替换（仍会以本库为输出文件）。
        返回 ``{"added", "merged", "total"}``。
        """
        data = self._read_file(os.path.abspath(path))
        if data is None:
            raise ValueError(f"无法读取题库文件：{path}")
        incoming = data.get("entries", {})
        added = merged = 0
        with _LOCK:
            self._refresh_if_changed()
            entries = {} if replace else self._data.setdefault("entries", {})
            for key, entry in incoming.items():
                if not isinstance(entry, dict) or not str(entry.get("a") or "").strip():
                    continue
                old = entries.get(key)
                if old is None:
                    entries[key] = dict(entry)
                    added += 1
                else:
                    entries[key] = self._merge_entry(old, entry)
                    merged += 1
            self._data["entries"] = entries
            self._write()
        logger.info(f"本地题库导入完成：新增 {added} 条，合并 {merged} 条，共 {len(entries)} 条")
        return {"added": added, "merged": merged, "total": len(entries)}

    @staticmethod
    def _merge_entry(old: dict, new: dict) -> dict:
        """冲突合并：答案相同时累加确认次数；不同则取确认次数多的那条。"""
        same = str(old.get("a") or "") == str(new.get("a") or "")
        if same:
            winner = dict(old)
            winner["ok"] = int(old.get("ok") or 0) + int(new.get("ok") or 0)
            winner["ts"] = max(int(old.get("ts") or 0), int(new.get("ts") or 0))
            return winner
        old_ok, new_ok = int(old.get("ok") or 0), int(new.get("ok") or 0)
        if new_ok > old_ok or (new_ok == old_ok and int(new.get("ts") or 0) > int(old.get("ts") or 0)):
            winner = dict(new)
            winner["ok"] = new_ok
            return winner
        return dict(old)

    def clear(self) -> int:
        """清空题库（仅清本库，不删文件）。返回清掉的条数。"""
        with _LOCK:
            count = len(self._data.get("entries", {}))
            self._data["entries"] = {}
            self._write()
        logger.warning(f"本地题库已清空，共移除 {count} 条")
        return count


# --------------------------------------------------------------------------- #
# 多账号 / 多课程题库目录
# --------------------------------------------------------------------------- #
class BankStore:
    """``<根目录>/<课程>.json`` 结构的**共享**题库（不按账号分文件夹）。

    同一门课的题目和答案对所有账号都一样，所以一门课一个文件、全账号共用：
    换账号做同一门课直接命中，也不需要重复采集。入库只写当前课程文件，
    查询先查当前课程文件，再兜底查其它课程文件（同一道题可能在多门课里出现）。
    """

    def __init__(self, root: Optional[str] = None, enabled: bool = True,
                 verify: bool = True, trust: bool = False,
                 true_word: str = "正确", false_word: str = "错误",
                 legacy: Optional[str] = None) -> None:
        self.root = os.path.abspath(root or default_root())
        self.enabled = bool(enabled)
        self.verify = bool(verify)
        self.trust = bool(trust)           # 已废弃：仅为兼容旧配置保留，不影响入库
        self.true_word = true_word or "正确"
        self.false_word = false_word or "错误"
        self.legacy = legacy or os.path.join(os.path.dirname(self.root), LEGACY_NAME)
        self._banks: Dict[str, LocalBank] = {}
        self._files: List[dict] = []
        self._scan_ts = 0.0
        self._scope = {"course": "", "course_id": ""}

    # ---------------- 配置 ----------------
    @classmethod
    def from_config(cls, conf: "dict | None") -> "BankStore":
        """用 ``[tiku]`` 配置段构造实例（缺项按默认值）。"""
        conf = conf or {}

        def _get(key: str, default: str = "") -> str:
            try:
                value = conf.get(key, default)
            except Exception:  # noqa: BLE001
                return default
            return default if value is None else str(value)

        true_list = [w.strip() for w in _get("true_list", "正确,对").split(",") if w.strip()]
        false_list = [w.strip() for w in _get("false_list", "错误,错").split(",") if w.strip()]
        # local_bank_dir 是目录；local_bank_path 是旧键（可能是 .json，取其所在目录）
        root = _get("local_bank_dir").strip() or _get("local_bank_path").strip()
        if root.lower().endswith(".json"):
            root = os.path.dirname(os.path.abspath(root))
        return cls(
            root=root or None,
            enabled=_truthy(_get("local_bank", "true"), True),
            verify=_truthy(_get("local_bank_verify", "true"), True),
            trust=_truthy(_get("local_bank_trust", "false"), False),
            true_word=true_list[0] if true_list else "正确",
            false_word=false_list[0] if false_list else "错误",
        )

    def reconfigure(self, conf: "dict | None") -> "BankStore":
        """运行期刷新配置（GUI 保存设置后调用），保留已载入的条目。"""
        fresh = BankStore.from_config(conf)
        with _LOCK:
            changed_root = os.path.abspath(fresh.root) != os.path.abspath(self.root)
            self.enabled = fresh.enabled
            self.verify = fresh.verify
            self.trust = fresh.trust
            self.true_word = fresh.true_word
            self.false_word = fresh.false_word
            if changed_root:
                self.root = fresh.root
                self._banks.clear()
                self._files = []
                self._scan_ts = 0.0
            for bank in self._banks.values():
                bank.verify = self.verify
                bank.trust = self.trust
                bank.true_word = self.true_word
                bank.false_word = self.false_word
        return self

    @property
    def usable(self) -> bool:
        return bool(self.enabled)

    def set_scope(self, account: "str | None" = None, course: "str | None" = None,
                  course_id: "str | None" = None) -> None:
        """设定当前课程（决定入库文件与查询优先级）。

        ``account`` 已废弃（题库全账号共享），保留形参只为兼容旧调用方。
        """
        with _LOCK:
            self._scope = {
                "course": str(course or ""),
                "course_id": str(course_id or ""),
            }

    @property
    def scope(self) -> dict:
        return dict(self._scope)

    # ---------------- 路径 ----------------
    def path_for(self, course: str = "", course_id: str = "") -> str:
        """``<根目录>/<课程>.json``（一门课一个文件，全账号共享）。"""
        return os.path.join(self.root, course_filename(course, course_id))

    def _bank(self, path: str) -> LocalBank:
        key = os.path.abspath(path)
        bank = self._banks.get(key)
        if bank is None:
            bank = LocalBank(path=key, enabled=True, verify=self.verify,
                             trust=self.trust, true_word=self.true_word,
                             false_word=self.false_word)
            self._banks[key] = bank
        return bank

    def _invalidate(self) -> None:
        self._scan_ts = 0.0

    def _scan(self, force: bool = False) -> List[dict]:
        """列出根目录下的课程文件（1.5 秒内复用上次结果，避免每题都走目录）。"""
        now = time.time()
        if self._files and not force and now - self._scan_ts < 1.5:
            return self._files
        files: List[dict] = []
        try:
            for name in sorted(os.listdir(self.root)):
                if name.startswith(".") or not name.lower().endswith(".json"):
                    continue
                path = os.path.join(self.root, name)
                if os.path.isfile(path):
                    files.append({"path": path, "file": name})
        except OSError:
            files = []
        self._files = files
        self._scan_ts = now
        return files

    def _current_path(self, course: str = "", course_id: str = "") -> str:
        course = course or self._scope["course"]
        if course == self._scope["course"]:
            course_id = course_id or self._scope["course_id"]
        return os.path.abspath(self.path_for(course, course_id))

    def _priority(self, course: str = "", course_id: str = "") -> List[dict]:
        """查询顺序：当前课程文件 -> 其它课程文件（同一道题可能被多门课复用）。"""
        current = self._current_path(course, course_id)
        files = self._scan()
        ordered: List[dict] = [{"path": current, "file": os.path.basename(current)}]
        seen = {current}
        for info in files:
            key = os.path.abspath(info["path"])
            if key in seen:
                continue
            seen.add(key)
            ordered.append(info)
        return ordered

    # ---------------- 查询 ----------------
    def lookup(self, title: str, qtype: str, options: str = "",
               account: str = "", course: str = "") -> Optional[str]:
        """跨文件查题：命中返回可直接提交的答案；未命中/停用返回 None。"""
        if not self.enabled:
            return None
        try:
            key = fingerprint(title, qtype, options)
            for info in self._priority(course):
                bank = self._bank(info["path"])
                entry = bank.entry(key)
                if not entry:
                    continue
                answer = bank.answer_for(entry, qtype, options)
                if not answer:
                    logger.debug(f"本地题库命中但无法映射到当前选项，已跳过：{title}")
                    continue
                logger.info(f"本地题库命中（{info['file']}）："
                            f"{normalize_text(title)} -> {answer}")
                return answer
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error(f"本地题库查询异常：{type(exc).__name__}: {exc}")
            return None

    def known(self, items: Iterable[dict], course: str = "",
              course_id: str = "") -> int:
        """统计这批题里「题库已有正确答案」的数量（用于日志与是否需要核验）。"""
        return len(list(items or [])) - len(self.missing(items, course, course_id))

    def missing(self, items: Iterable[dict], course: str = "",
                course_id: str = "") -> List[dict]:
        """返回这批题里**题库还没有正确答案**的那些题。

        注意：**不能**用它来决定「哪些题送去核验」。题库里答错的题只有在核验后
        才能发现（平台判错 + 给出正确答案），所以提交后应当核验整份作业，
        由 :meth:`learn` 按「等价跳过 / 不等价覆写」判断是否真正写库。
        这里只用于统计与展示（如采集时的「已有 N 题」。
        """
        items = list(items or [])
        if not self.enabled:
            return items
        try:
            paths = self._priority(course, course_id)
            pending: List[dict] = []
            for item in items:
                key = fingerprint(item.get("title", ""), str(item.get("type") or ""),
                                  str(item.get("options") or ""))
                found = False
                for info in paths:
                    bank = self._bank(info["path"])
                    entry = bank.entry(key)
                    if entry and bank.answer_for(
                            entry, str(item.get("type") or ""),
                            str(item.get("options") or "")):
                        found = True
                        break
                if not found:
                    pending.append(item)
            return pending
        except Exception as exc:  # noqa: BLE001 - 判断失败就当全都缺，交给 learn 去筛
            logger.error(f"本地题库比对异常：{type(exc).__name__}: {exc}")
            return items

    # ---------------- 入库 ----------------
    def _drop_known_elsewhere(self, items: List[dict], verdicts: dict,
                              current_path: str) -> List[dict]:
        """别的课程文件里已有**一致**答案的题不再重复存一份（同一门课只写一个文件）。

        只是「别处已有」不算数：答案和平台给的不一致时照旧写进当前课程文件，
        下次那份作业自己会把旧答案纠正过来。
        """
        try:
            others = [info for info in self._scan()
                      if os.path.abspath(info["path"]) != os.path.abspath(current_path)]
        except Exception:  # noqa: BLE001
            others = []
        if not items or not others:
            return items
        keep: List[dict] = []
        for item in items:
            verdict = _verdict_of(verdicts, item.get("qid"))
            answer, ans_options, _kind = LocalBank._decide(item, verdict)
            if not answer:
                keep.append(item)            # 没答案的题交给 learn 统一丢弃
                continue
            qtype = str(item.get("type") or "")
            options = str(item.get("options") or "")
            key = fingerprint(item.get("title", ""), qtype, options)
            dup = False
            for info in others:
                other = self._bank(info["path"])
                entry = other.entry(key)
                if entry and other._same_answer(entry, qtype,
                                                ans_options or options, answer):
                    dup = True
                    break
            if not dup:
                keep.append(item)
        return keep

    def learn(self, items: Iterable[dict], verdicts: "dict | None" = None,
              course: str = "", allow_unverified: bool = False,
              account: str = "", course_id: str = "", only_missing: bool = True) -> int:
        """把平台确认过的正确答案写进「当前课程」对应的文件。

        参数含义与 :meth:`LocalBank.learn` 相同（库里答案与平台不一致时会被覆写纠正）；
        ``course`` / ``course_id`` 留空时用 :meth:`set_scope` 设定的作用域。
        ``account`` 已废弃（题库共享），保留仅为兼容。
        """
        if not self.enabled:
            return 0
        scope = self._scope
        crs = course or scope["course"]
        cid = course_id or (scope["course_id"] if crs == scope["course"] else "")
        path = self.path_for(crs, cid)
        bank = self._bank(path)
        bank.set_meta(course=crs or UNKNOWN_COURSE, course_id=cid)
        batch = list(items or [])
        if only_missing and verdicts and batch:
            # 同一道题在别的课程文件里已有一致答案 -> 不重复存（避免一份作业写多份文件）
            batch = self._drop_known_elsewhere(batch, verdicts, path)
        learned = bank.learn(batch, verdicts, course=crs,
                             allow_unverified=allow_unverified,
                             only_missing=only_missing)
        if learned:
            self._invalidate()
        return learned

    # ---------------- 统计 / 导出 / 导入 ----------------
    def stats(self) -> dict:
        """汇总统计（题目数 / 课程文件 / 题型 / 大小 / 最近更新）。"""
        with _LOCK:
            files = self._scan(force=True)
            total = 0
            by_type: Dict[str, int] = {}
            courses: List[dict] = []
            updated = 0
            size = 0
            for info in files:
                bank = self._bank(info["path"])
                entries = bank.snapshot()
                count = len(entries)
                total += count
                for entry in entries.values():
                    key = str(entry.get("y") or "unknown")
                    by_type[key] = by_type.get(key, 0) + 1
                stamp = int(bank._data.get("updated") or 0)
                updated = max(updated, stamp)
                try:
                    size += os.path.getsize(info["path"])
                except OSError:
                    pass
                courses.append({
                    "course": bank._data.get("course") or os.path.splitext(info["file"])[0],
                    "file": info["file"],
                    "path": info["path"],
                    "total": count,
                    "updated": stamp,
                })
            return {
                "root": self.root,
                "path": self.root,          # 兼容旧字段
                "enabled": self.enabled,
                "verify": self.verify,
                "trust": self.trust,
                "total": total,
                "by_type": by_type,
                "courses": courses,
                "files": len(files),
                "updated": updated,
                "size": size,
            }

    def export_to(self, path: str) -> dict:
        """导出**全部课程**到一个 JSON 包（便于备份与分享）。"""
        with _LOCK:
            files = []
            total = 0
            for info in self._scan(force=True):
                bank = self._bank(info["path"])
                entries = bank.snapshot()
                if not entries:
                    continue
                total += len(entries)
                files.append({
                    "course": bank._data.get("course") or os.path.splitext(info["file"])[0],
                    "course_id": bank._data.get("course_id") or "",
                    "entries": json.loads(json.dumps(entries)),
                })
            payload = {
                "v": SCHEMA_VERSION,
                "kind": BUNDLE_KIND,
                "updated": int(time.time()),
                "total": total,
                "files": files,
            }
        _atomic_write_json(path, payload)
        logger.info(f"本地题库已导出：{path}（{len(files)} 个课程文件，{total} 条）")
        return {"total": total, "files": len(files), "path": path}

    def import_from(self, path: str, replace: bool = False,
                    account: str = "", course: str = "",
                    course_id: str = "") -> dict:
        """导入题库文件并合并。

        支持三种格式：
          * 导出包（``files`` 数组）：按课程还原成一个课程文件一个文件；
          * 旧版单文件（``entries``）：合并进当前课程（或文件名对应课程）；
          * 更旧的「按账号分文件」包：忽略账号，按课程合并进同一个文件。
        """
        target = os.path.abspath(path)
        if not os.path.isfile(target):
            raise ValueError(f"无法读取题库文件：{path}")
        data = _read_json(target)
        result = {"added": 0, "merged": 0, "total": 0, "files": 0}

        bundles: List[dict] = []
        if isinstance(data.get("files"), list):
            bundles = [item for item in data["files"] if isinstance(item, dict)]
        elif isinstance(data.get("entries"), dict):
            fallback_course = course or self._scope["course"] or os.path.splitext(os.path.basename(target))[0]
            bundles = [{
                "course": fallback_course,
                "course_id": course_id or (self._scope["course_id"] if fallback_course == self._scope["course"] else ""),
                "entries": data["entries"],
            }]
        else:
            raise ValueError(f"题库文件格式不正确：{path}")

        for item in bundles:
            entries = item.get("entries")
            if not isinstance(entries, dict) or not entries:
                continue
            crs = str(item.get("course") or course or self._scope["course"] or "")
            cid = str(item.get("course_id") or "")
            file_path = self.path_for(crs, cid)
            bank = self._bank(file_path)
            bank.set_meta(course=crs or UNKNOWN_COURSE, course_id=cid)
            part = self._merge_into(bank, entries, replace)
            result["added"] += part["added"]
            result["merged"] += part["merged"]
            result["files"] += 1
            self._invalidate()

        result["total"] = self.stats()["total"]
        logger.info(f"本地题库导入完成：新增 {result['added']} 条，合并 {result['merged']} 条，"
                    f"共 {result['total']} 条")
        return result

    @staticmethod
    def _merge_into(bank: LocalBank, incoming: dict, replace: bool = False) -> dict:
        added = merged = 0
        entries = {} if replace else dict(bank.snapshot())
        for key, entry in incoming.items():
            if not isinstance(entry, dict) or not str(entry.get("a") or "").strip():
                continue
            old = entries.get(key)
            if old is None:
                entries[key] = dict(entry)
                added += 1
            else:
                entries[key] = LocalBank._merge_entry(old, entry)
                merged += 1
        bank.replace_entries(entries, create=False)
        return {"added": added, "merged": merged, "total": len(entries)}

    def clear(self, account: "str | None" = None, course: "str | None" = None) -> int:
        """清空题库文件（默认清全部；可按课程限定）。返回移除的条数。"""
        removed = 0
        with _LOCK:
            files = self._scan(force=True)
            for info in files:
                if course:
                    bank = self._bank(info["path"])
                    name = str(bank._data.get("course") or "")
                    stem = os.path.splitext(info["file"])[0]
                    if course not in name and course not in stem:
                        continue
                bank = self._bank(info["path"])
                removed += len(bank.snapshot())
                try:
                    os.remove(info["path"])
                except OSError:
                    bank.replace_entries({}, create=False)
                self._banks.pop(os.path.abspath(info["path"]), None)
            self._invalidate()
            self._cleanup_empty_dirs()
        logger.warning(f"本地题库已清空，共移除 {removed} 条")
        return removed

    def _cleanup_empty_dirs(self) -> None:
        """删掉迁移后留下的空目录（失败无所谓）。"""
        try:
            for name in os.listdir(self.root):
                folder = os.path.join(self.root, name)
                if os.path.isdir(folder) and not os.listdir(folder):
                    os.rmdir(folder)
        except OSError:
            pass

    def migrate_legacy(self) -> Optional[dict]:
        """把旧结构与旧版单文件迁到 ``<根目录>/<课程>.json``。

        1. ``progress_cache/local_bank.json``（初版单文件）-> ``旧版题库.json``；
        2. ``<根目录>/<账号>/<课程>.json``（上一版按账号分文件夹）-> 按课程合并到
           根目录下的同名文件，旧账号文件夹改名为 ``<账号>.migrated``（保留数据不删）。
        """
        total = {"added": 0, "merged": 0, "migrated": 0}
        changed = False

        if os.path.isfile(self.legacy):
            try:
                data = _read_json(self.legacy)
                if not isinstance(data.get("entries"), dict):
                    raise ValueError("缺少 entries")
                target = self.path_for(LEGACY_COURSE)
                bank = self._bank(target)
                bank.set_meta(course=LEGACY_COURSE)
                part = self._merge_into(bank, data["entries"])
                os.replace(self.legacy, f"{self.legacy}{MIGRATED_SUFFIX}")
                total["added"] += part["added"]
                total["migrated"] += 1
                changed = True
                logger.info(f"旧版本地题库已迁移到 {target}（{part['added']} 条）")
            except Exception as exc:  # noqa: BLE001 - 迁移失败不能影响启动
                logger.error(f"旧版本地题库迁移失败（已保留原文件）：{exc}")

        try:
            folders = [name for name in os.listdir(self.root)
                       if os.path.isdir(os.path.join(self.root, name))
                       and not name.lower().endswith(MIGRATED_SUFFIX)]
        except OSError:
            folders = []
        for folder in folders:
            source_dir = os.path.join(self.root, folder)
            moved = False
            try:
                for name in sorted(os.listdir(source_dir)):
                    if not name.lower().endswith(".json"):
                        continue
                    data = _read_json(os.path.join(source_dir, name))
                    entries = data.get("entries")
                    if not isinstance(entries, dict) or not entries:
                        continue
                    course = str(data.get("course") or os.path.splitext(name)[0])
                    course_id = str(data.get("course_id") or "")
                    target = self.path_for(course, course_id)
                    bank = self._bank(target)
                    bank.set_meta(course=course or UNKNOWN_COURSE, course_id=course_id)
                    part = self._merge_into(bank, entries)
                    total["added"] += part["added"]
                    total["merged"] += part["merged"]
                    moved = True
                if moved:
                    os.replace(source_dir, source_dir + MIGRATED_SUFFIX)
                    total["migrated"] += 1
                    changed = True
                    logger.info(f"旧「按账号分文件夹」题库已合并进课程文件：{folder}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"迁移旧账号题库目录失败（已保留原目录 {folder}）：{exc}")

        if changed:
            self._invalidate()
            self._files = []
        return total if changed else None

    def list_courses(self) -> List[dict]:
        """课程文件清单（界面展示 / 调试用）。"""
        return self.stats()["courses"]


# --------------------------------------------------------------------------- #
# 单例与配置
# --------------------------------------------------------------------------- #
def _migrate_once(store: "BankStore") -> None:
    """旧版单文件 -> 新目录结构，每个进程只尝试一次。"""
    global _MIGRATED
    with _LOCK:
        if _MIGRATED:
            return
        _MIGRATED = True
    try:
        store.migrate_legacy()
    except Exception as exc:  # noqa: BLE001 - 迁移失败不能影响刷课
        logger.error(f"旧版本地题库迁移异常：{exc}")


def configure(conf: "dict | None") -> BankStore:
    """按最新 ``[tiku]`` 配置刷新单例（题库初始化 / 设置保存后调用）。"""
    global _BANK
    with _LOCK:
        if _BANK is None:
            _BANK = BankStore.from_config(conf)
        else:
            _BANK.reconfigure(conf)
        store = _BANK
    _migrate_once(store)
    return store


def get_bank() -> BankStore:
    """取本地题库单例；首次访问时按当前配置创建（并迁移旧版单文件）。"""
    global _BANK
    with _LOCK:
        if _BANK is None:
            _BANK = BankStore.from_config(_load_conf())
        store = _BANK
    _migrate_once(store)
    return store


def set_scope(account: "str | None" = None, course: "str | None" = None,
              course_id: "str | None" = None) -> None:
    """设定当前作业归属（账号 / 课程），决定入库文件与查询优先级。"""
    get_bank().set_scope(account=account, course=course, course_id=course_id)


def _load_conf() -> dict:
    try:
        from config_store import load_section
        return load_section("tiku")
    except Exception:  # noqa: BLE001  配置不可读时按默认值（启用、需核验）
        return {}


def reset_for_test() -> None:
    """仅供测试：丢弃单例。"""
    global _BANK, _MIGRATED
    with _LOCK:
        _BANK = None
        _MIGRATED = False


def merge_files(paths: Iterable[str], out: str) -> dict:
    """把多个题库文件合并到 ``out``。

    ``out`` 是目录 -> 合并成目录结构；``out`` 以 ``.json`` 结尾 -> 合并后导出成包。
    """
    is_bundle = str(out).lower().endswith(".json")
    temp_dir = None
    root = out
    if is_bundle:
        temp_dir = tempfile.mkdtemp(prefix="bankmerge_")
        root = temp_dir
    target = BankStore(root=root, enabled=True)
    result = {"added": 0, "merged": 0, "total": 0, "files": 0}
    try:
        for path in paths:
            if os.path.abspath(path) == os.path.abspath(out):
                continue
            part = target.import_from(path)
            result["added"] += part["added"]
            result["merged"] += part["merged"]
            result["files"] += part["files"]
        result["total"] = target.stats()["total"]
        if is_bundle:
            target.export_to(out)
            result["path"] = os.path.abspath(out)
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
    return result


# --------------------------------------------------------------------------- #
# 命令行：查看 / 导出 / 导入 / 合并
# --------------------------------------------------------------------------- #
def _main(argv: "List[str]") -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="local_bank", description="SuperStar 本地题库工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats", help="查看当前题库统计")
    sub.add_parser("list", help="列出课程文件")
    p_export = sub.add_parser("export", help="导出全部题库为一个 JSON 包")
    p_export.add_argument("out")
    p_import = sub.add_parser("import", help="导入题库文件（自动合并）")
    p_import.add_argument("src")
    p_import.add_argument("--replace", action="store_true", help="整体替换目标课程文件")
    p_merge = sub.add_parser("merge", help="合并多个题库文件到目录或 JSON 包")
    p_merge.add_argument("files", nargs="+")
    p_merge.add_argument("-o", "--out", required=True)
    args = parser.parse_args(argv)

    if args.cmd == "stats":
        print(json.dumps(get_bank().stats(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "list":
        for item in get_bank().list_courses():
            print(f"{item['file']}\t{item['total']} 题\t{item['course']}")
        return 0
    if args.cmd == "export":
        print(json.dumps(get_bank().export_to(args.out), ensure_ascii=False))
        return 0
    if args.cmd == "import":
        print(json.dumps(get_bank().import_from(args.src, replace=args.replace),
                         ensure_ascii=False))
        return 0
    if args.cmd == "merge":
        print(json.dumps(merge_files(args.files, args.out), ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    import sys
    raise SystemExit(_main(sys.argv[1:]))
