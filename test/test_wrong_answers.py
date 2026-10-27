# -*- coding: utf-8 -*-
# layer: core   # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""错题本（api/wrong_answers.py）与「判错后作废答案」的接线测试。

覆盖的问题（用户报的现象）：

    接入了 AI，但某道题答错导致章节检测判「不合格」-> 程序不会重新请求 AI，
    而是把同一批错答案反复提交，形成无限循环。

所以这里盯的是三件事：
  1. 错题本按**选项正文**记录被判错的答案（换账号 / 选项乱序都对得上）；
  2. ``Tiku.query`` 发现题库或 cache.json 里就是被判错的答案时，**作废它并重新搜题**，
     大模型仍给同一个错答案时用排除法换一个；
  3. 提交后核验：判错 -> 记错题本 + 清缓存 + 删题库里的错条目；
     判对 -> 入库并清掉该题的错答记录。**只有平台确认正确的才进题库。**
"""
import contextlib
import itertools
import json
import os
import shutil
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate_exc  # noqa: E402

import api.answer as answer  # noqa: E402
import api.base as base  # noqa: E402
import api.local_bank as lb  # noqa: E402
import api.wrong_answers as wa  # noqa: E402

TMP = tempfile.mkdtemp(prefix="wrongbook_")
OPTS = "A. 甲选项\nB. 乙选项\nC. 丙选项"
# 同一组选项、换个顺序（模拟换账号 / 平台打乱选项）
OPTS_SHUFFLED = "A. 丙选项\nB. 甲选项\nC. 乙选项"

_seq = 0


def _path(name: str) -> str:
    global _seq
    _seq += 1
    return os.path.join(TMP, f"{name}{_seq}.json")


def _bank(name: str = "bank", enabled: bool = True) -> lb.BankStore:
    global _seq
    _seq += 1
    return lb.BankStore(root=os.path.join(TMP, f"{name}{_seq}"), enabled=enabled,
                        verify=True, true_word="正确", false_word="错误",
                        legacy=os.path.join(TMP, f"legacy{_seq}.json"))


class _FakeTiku(answer.Tiku):
    """假题库：``answers`` 依次返回，记录每次查询收到的 q_info。"""

    def __init__(self):
        super().__init__()
        self.name = "假题库"
        self.answers = []
        self.queries = []
        self.true_list = ["正确", "对", "√", "是"]
        self.false_list = ["错误", "错", "×", "否"]

    def _query(self, q_info: dict):
        self.queries.append(dict(q_info))
        return self.answers.pop(0) if self.answers else None


@contextlib.contextmanager
def _env(name: str, bank=None):
    """把错题本 / cache.json / 本地题库都指到临时文件（互不干扰）。"""
    book_path = _path(f"{name}_book")
    cache_path = _path(f"{name}_cache")
    real_cache = answer.CacheDAO
    bank = bank or _bank(f"{name}_null", enabled=False)
    with mock.patch.object(wa, "default_path", lambda: book_path), \
            mock.patch.object(answer, "CacheDAO", lambda *a, **k: real_cache(cache_path)), \
            mock.patch.object(base, "CacheDAO", lambda *a, **k: real_cache(cache_path)), \
            mock.patch.object(lb, "get_bank", lambda: bank):
        wa.reset_for_test()
        try:
            yield book_path, cache_path
        finally:
            wa.reset_for_test()


def _q(title="题目一", qtype="single", options=OPTS):
    return {"title": title, "type": qtype, "options": options}


# --------------------------------------------------------------------------- #
# 1. 答案口径：按选项正文比较，换账号 / 选项乱序都还是同一个答案
# --------------------------------------------------------------------------- #
def test_answer_key_uses_option_text_not_letter():
    assert lb.answer_key("single", "B", OPTS) == "乙选项"
    # 选项顺序变了、答案给的是正文，键仍然是同一个
    assert lb.answer_key("single", "乙选项", OPTS_SHUFFLED) == "乙选项"
    assert lb.answer_key("single", "C", OPTS) == lb.answer_key("single", "丙选项", OPTS)


def test_answer_key_judgement_and_multiple():
    assert lb.answer_key("judgement", "true") == "T"
    assert lb.answer_key("judgement", "正确") == "T"
    assert lb.answer_key("judgement", "错") == "F"
    # 多选：字母串 / 正文 / 多行正文 三种给法都要归到同一个键
    by_letter = lb.answer_key("multiple", "AC", OPTS)
    by_text = lb.answer_key("multiple", "丙选项|甲选项", OPTS)
    by_lines = lb.answer_key("multiple", "甲选项\n丙选项", OPTS)
    assert by_letter == by_text == by_lines, (by_letter, by_text, by_lines)


# --------------------------------------------------------------------------- #
# 2. 错题本：持久化 / 去重 / 忘记 / 清空 / 坏文件降级
# --------------------------------------------------------------------------- #
def test_wrong_book_persists_and_dedupes():
    path = _path("book")
    book = wa.WrongBook(path)
    assert book.record("fp1", "乙选项", title="题目一", qtype="single") is True
    assert book.record("fp1", "乙选项") is False        # 同一个错答案不重复写
    assert book.known("fp1") == {"乙选项"}
    # 重新打开文件仍在（刷课重启后照样记得）
    again = wa.WrongBook(path)
    assert again.known("fp1") == {"乙选项"}
    assert again.is_wrong("fp1", "乙选项") is True
    assert again.is_wrong("fp1", "丙选项") is False


def test_wrong_book_forget_and_clear():
    book = wa.WrongBook(_path("book"))
    book.record("fp", "甲选项")
    book.record("fp", "乙选项")
    assert book.forget_key("fp", "甲选项") is True
    assert book.known("fp") == {"乙选项"}
    assert book.clear("fp") is True
    assert book.known("fp") == set()


def test_wrong_book_corrupt_file_degrades_to_empty():
    path = _path("broken")
    with open(path, "w", encoding="utf8") as fh:
        fh.write("{ 这不是 json")
    book = wa.WrongBook(path)
    assert book.known("anything") == set()
    # 坏文件不能把刷课搞崩：还能继续记录（并覆盖回正常结构）
    assert book.record("fp", "甲选项") is True
    assert wa.WrongBook(path).known("fp") == {"甲选项"}


def test_known_keys_survives_option_shuffle():
    with _env("shuffle"):
        wa.record_wrong("题目一", "single", OPTS, "B")
        assert wa.known_keys("题目一", "single", OPTS) == {"乙选项"}
        # 选项乱序后（同一道题、同一批选项文本）依然认得出来
        assert wa.known_keys("题目一", "single", OPTS_SHUFFLED) == {"乙选项"}


# --------------------------------------------------------------------------- #
# 3. 排除信息：提示词 / 排除法
# --------------------------------------------------------------------------- #
def test_hint_texts_tell_model_which_option_is_wrong():
    hints = wa.hint_texts("single", OPTS, {"乙选项"})
    assert hints and "乙选项" in hints[0]
    assert wa.hint_texts("single", OPTS, set()) == []
    assert wa.hint_texts("judgement", "", {"T"})[0].find("正确") >= 0


def test_alternative_for_never_returns_rejected_option():
    for _ in range(40):
        alt = wa.alternative_for("single", OPTS, {"乙选项"})
        assert alt in ("甲选项", "丙选项"), alt


def test_alternative_for_judgement_flips():
    assert wa.alternative_for("judgement", "", {"T"}, "正确") == "错误"
    assert wa.alternative_for("judgement", "", {"F"}, "错误") == "正确"


def test_alternative_for_multiple_returns_text_lines():
    # 已知「甲+乙」这个组合被判错了 -> 必须换一组正文，而不是原样返回
    known = {lb.answer_key("multiple", "AB", OPTS)}
    alt = wa.alternative_for("multiple", OPTS, known, "AB")
    assert alt and "\n" in alt, alt
    assert lb.answer_key("multiple", alt, OPTS) not in known


def test_alternative_for_gives_up_when_everything_rejected():
    known = {lb.answer_key("single", c, OPTS) for c in "ABC"}
    assert wa.alternative_for("single", OPTS, known) is None


# --------------------------------------------------------------------------- #
# 3b. 提交形态（字母 / true-false）：study_work 填表单直接用，重做时排除错答
# --------------------------------------------------------------------------- #
def test_pick_submit_answer_avoids_rejected_single():
    known = {lb.answer_key("single", "B", OPTS)}
    for _ in range(30):
        got = wa.pick_submit_answer("single", OPTS, known)
        assert got in ("A", "C"), got


def test_pick_submit_answer_prefers_a_different_option():
    """给出了当前答案（上一轮被判错的那个）时，必须换成别的，不能原样返回。"""
    known = {lb.answer_key("single", "B", OPTS)}
    for _ in range(30):
        assert wa.pick_submit_answer("single", OPTS, known, "B") in ("A", "C")


def test_pick_submit_answer_flips_judgement():
    assert wa.pick_submit_answer("judgement", "", {"T"}) == "false"
    assert wa.pick_submit_answer("judgement", "", {"F"}) == "true"
    assert wa.pick_submit_answer("judgement", "", {"T"}, "true") == "false"
    # 两个判断都判错过 -> 换不出来（不能瞎猜）
    assert wa.pick_submit_answer("judgement", "", {"T", "F"}) is None


def test_pick_submit_answer_multiple_never_returns_rejected_combo():
    known = {lb.answer_key("multiple", "AC", OPTS)}
    for _ in range(30):
        got = wa.pick_submit_answer("multiple", OPTS, known, "AC")
        assert got and lb.answer_key("multiple", got, OPTS) not in known, got
        assert got != "AC", "应尽量换一组新的组合"


def test_pick_submit_answer_multiple_respects_size():
    """多选题「选几个」由调用方决定（随机作答按原加权策略抽样），排除法只换组合。"""
    known = {lb.answer_key("multiple", "AB", OPTS)}
    got = wa.pick_submit_answer("multiple", OPTS, known, "", size=3)
    assert got == "ABC", got
    # 不指定个数时保持原来的「跟当前答案一样多」的行为
    assert len(wa.pick_submit_answer("multiple", OPTS, known, "AB")) == 2


def test_pick_submit_answer_gives_up_when_every_option_rejected():
    known = {lb.answer_key("single", c, OPTS) for c in "ABC"}
    assert wa.pick_submit_answer("single", OPTS, known) is None
    assert wa.pick_submit_answer("completion", "", {"某答案"}) is None


def test_pick_submit_answer_multiple_explores_other_sizes():
    """多选题正确答案要选 3 个，但当前错答案只选了 2 个（且 2 个的所有组合都判错过）。

    排除法必须能换到「3 个选项」的组合，不能卡死在 2 个选项的组合上——
    否则永远交不出正确答案，陷入重答死循环（模拟 avoid_judged_wrong 不传 size 的场景）。
    """
    opts = "A. 甲\nB. 乙\nC. 丙\nD. 丁"
    known = {lb.answer_key("multiple", c, opts)
             for c in ("AB", "AC", "AD", "BC", "BD", "CD")}
    for _ in range(30):
        got = wa.pick_submit_answer("multiple", opts, known, "AB")
        assert got, "应当换出一组尚未被判错的组合"
        assert lb.answer_key("multiple", got, opts) not in known
        assert len(got) == 3, f"正确答案需要选 3 个，实际换出 {got}"


def test_pick_submit_answer_multiple_still_prefers_same_size():
    """当前错答案是 2 个、且还有没试过的 2 个组合时，优先换同尺寸（不改变选几个）。"""
    opts = "A. 甲\nB. 乙\nC. 丙\nD. 丁"
    known = {lb.answer_key("multiple", c, opts) for c in ("AB", "AC")}
    for _ in range(30):
        got = wa.pick_submit_answer("multiple", opts, known, "AB")
        assert got and len(got) == 2, got
        assert lb.answer_key("multiple", got, opts) not in known


def test_alternative_for_multiple_explores_other_sizes():
    """alternative_for 同样要在同尺寸换不出时尝试其它尺寸（题库/AI 层）。

    4 个选项、所有 2 个组合都判错过 -> 必须能换出 3/4 个选项的组合。
    """
    opts = "A. 甲\nB. 乙\nC. 丙\nD. 丁"
    known = {lb.answer_key("multiple", c, opts)
             for c in ("AB", "AC", "AD", "BC", "BD", "CD")}
    for _ in range(30):
        alt = wa.alternative_for("multiple", opts, known, "AB")
        assert alt and "\n" in alt, alt
        key = lb.answer_key("multiple", alt, opts)
        assert key not in known
        assert len(alt.split("\n")) == 3, f"应换出 3 个选项的组合，实际 {alt.splitlines()}"


def test_multiple_rejection_is_combo_level_not_option_level():
    """多选题的判错是**组合级**的：只选 1 个的组合判错，不代表那个选项本身错。

    用户报的现象：某题反复重答后，错题本里攒下了「甲」「乙」「丙」「丁」四条
    「只选 1 个」的错答案；旧实现把这 4 个选项整体剔除，候选空间被砍空，于是报
    「该题可选答案都已被平台判错」——可实际上 2/3/4 个选项的组合一个都没试过。

    正确行为：只排除被判错的那**个组合**，组合里出现的选项照旧可用。
    """
    opts = "A. 甲\nB. 乙\nC. 丙\nD. 丁"
    known = {lb.answer_key("multiple", c, opts) for c in "ABCD"}
    for _ in range(30):
        got = wa.pick_submit_answer("multiple", opts, known, "AB", size=2)
        assert got, "只判错「单个选项」不能得出「无解」的结论"
        assert len(got) == 2, got
        assert lb.answer_key("multiple", got, opts) not in known
        alt = wa.alternative_for("multiple", opts, known, "AB")
        assert alt, "题库层同样不能把选项整体剔除"
        assert lb.answer_key("multiple", alt, opts) not in known


def test_pick_submit_answer_multiple_is_exhaustive_not_random():
    """「都已被平台判错」必须是**穷举**结论，不能是随机抽样没抽到。

    8 个选项、1/2/3 个的组合全判错、4 个的组合只剩 ``ABCD`` 没试过 —— 旧实现
    每种尺寸只随机试 16 次，抽到 ABCD（1/70）几乎不可能，于是 100% 返回 None，
    上层把「换不出来」当成「都判错了」，正确答案再也交不出去。
    """
    opts = "\n".join(f"{c}. {c}项" for c in "ABCDEFGH")
    known = set()
    for size in (1, 2, 3):
        for combo in itertools.combinations("ABCDEFGH", size):
            known.add(lb.answer_key("multiple", "".join(combo), opts))
    for combo in itertools.combinations("ABCDEFGH", 4):
        if "".join(combo) != "ABCD":
            known.add(lb.answer_key("multiple", "".join(combo), opts))

    for _ in range(30):
        assert wa.pick_submit_answer("multiple", opts, known, "ABCD") == "ABCD"
        alt = wa.alternative_for("multiple", opts, known, "ABCD")
        assert alt and lb.answer_key("multiple", alt, opts) == \
            lb.answer_key("multiple", "ABCD", opts)


def test_pick_submit_answer_multiple_gives_up_only_when_all_combos_rejected():
    """所有尺寸、所有组合都判错过时才算无解（3 个选项共 7 种组合）。"""
    opts = "A. 甲\nB. 乙\nC. 丙"
    known = {lb.answer_key("multiple", "".join(c), opts)
             for n in (1, 2, 3) for c in itertools.combinations("ABC", n)}
    assert wa.pick_submit_answer("multiple", opts, known, "AB") is None
    assert wa.alternative_for("multiple", opts, known, "AB") is None


# --------------------------------------------------------------------------- #
# 3c. 提交前最后一道闸：任何来源的答案撞到已知错答都要换掉
# --------------------------------------------------------------------------- #
def test_avoid_judged_wrong_swaps_rejected_answer():
    with _env("gate"):
        wa.record_wrong("题目一", "single", OPTS, "甲选项")
        assert base.avoid_judged_wrong(_q(), "A") in ("B", "C")
        # 没被判错过的答案原样保留（不能顺手把正确答案也改掉）
        assert base.avoid_judged_wrong(_q(), "C") == "C"
        # 没记录的题不受影响
        assert base.avoid_judged_wrong(_q(title="题目二"), "A") == "A"


def test_avoid_judged_wrong_flips_judgement():
    with _env("gate_j"):
        wa.record_wrong("判断题一", "judgement", "", "true")
        q = {"title": "判断题一", "type": "judgement", "options": ""}
        assert base.avoid_judged_wrong(q, "true") == "false"
        assert base.avoid_judged_wrong(q, "false") == "false"


# --------------------------------------------------------------------------- #
# 4. Tiku.query：缓存/题库里的答案被判错 -> 作废并重新搜题
# --------------------------------------------------------------------------- #
def test_query_drops_cached_answer_that_was_judged_wrong():
    with _env("cache_reject") as (_book, cache_path):
        cache = answer.CacheDAO(cache_path)
        cache.add_cache("题目一", "乙选项")          # 上次 AI 给出的错答案被缓存住了
        wa.record_wrong("题目一", "single", OPTS, "乙选项")

        tiku = _FakeTiku()
        tiku.answers = ["丙选项"]
        got = tiku.query(_q())

        assert got == "丙选项", f"应重新搜题，实际 {got!r}"
        assert tiku.queries, "缓存作废后必须真的去请求题库/AI"
        # 提示词里带上了「这个选项判错过」
        excludes = "".join(tiku.queries[0].get("_exclude") or [])
        assert "乙选项" in excludes, tiku.queries[0]
        # 错答案已从缓存里清掉（不会下一轮又命中）
        assert cache.get_cache("题目一") != "乙选项"


def test_query_switches_answer_when_model_repeats_rejected_option():
    with _env("repeat"):
        wa.record_wrong("题目一", "single", OPTS, "乙选项")
        tiku = _FakeTiku()
        tiku.answers = ["乙选项"]                  # 大模型无视提示、又给了错答案
        got = tiku.query(_q())
        assert got in ("甲选项", "丙选项"), f"必须换成别的选项，实际 {got!r}"


def test_query_uses_cache_when_answer_was_not_rejected():
    with _env("cache_ok") as (_book, cache_path):
        cache = answer.CacheDAO(cache_path)
        cache.add_cache("题目一", "乙选项")
        tiku = _FakeTiku()
        tiku.answers = ["甲选项"]
        assert tiku.query(_q()) == "乙选项"
        assert tiku.queries == [], "没判错过的缓存答案应直接命中，不消耗远程额度"


def test_query_deletes_bank_entry_that_was_judged_wrong():
    bank = _bank("bank_reject")
    bank.learn([{"qid": 1, "title": "题目一", "type": "single",
                 "options": OPTS, "answer": "B"}],
               {1: True}, course="课", course_id="1")
    assert bank.lookup("题目一", "single", OPTS, course="课", course_id="1") == "B"

    with _env("bank_reject", bank=bank):
        wa.record_wrong("题目一", "single", OPTS, "B")     # 题库这条答案被判错了
        tiku = _FakeTiku()
        tiku.answers = ["丙选项"]
        got = tiku.query(_q(), course="课", course_id="1")
        assert got == "丙选项", f"应改走远程搜题，实际 {got!r}"
        assert bank.lookup("题目一", "single", OPTS,
                           course="课", course_id="1") is None, "错答案必须从题库里删掉"


# --------------------------------------------------------------------------- #
# 5. 提交后核验：判错作废 / 判对入库
# --------------------------------------------------------------------------- #
def _submitted(title, answer, qid="1", options=OPTS):
    return {"qid": qid, "title": title, "type": "single",
            "options": options, "answer": answer}


def test_record_rejected_tracks_purges_cache_and_bank():
    bank = _bank("purge")
    bank.learn([{"qid": 1, "title": "题目一", "type": "single",
                 "options": OPTS, "answer": "B"}],
               {1: True}, course="课", course_id="1")
    with _env("purge", bank=bank) as (_book, cache_path):
        cache = answer.CacheDAO(cache_path)
        cache.add_cache("题目一", "乙选项")

        ch = base.Chaoxing(account=None, tiku=_FakeTiku())
        ch._record_rejected([_submitted("题目一", "B")], {"1": False})

        assert wa.known_keys("题目一", "single", OPTS) == {"乙选项"}
        assert cache.get_cache("题目一") is None, "判错后缓存必须作废"
        assert bank.lookup("题目一", "single", OPTS,
                           course="课", course_id="1") is None, "错答案不能留在题库里"


def test_record_rejected_clears_wrong_record_when_judged_right():
    with _env("right"):
        wa.record_wrong("题目一", "single", OPTS, "甲选项")
        ch = base.Chaoxing(account=None, tiku=_FakeTiku())
        ch._record_rejected([_submitted("题目一", "B")], {"1": True})
        assert wa.known_keys("题目一", "single", OPTS) == set()


def test_sync_after_submit_only_learns_confirmed_answers():
    bank = _bank("learn")
    submitted = [
        _submitted("题目一", "A", qid="1"),                    # 平台判对
        _submitted("题目二", "B", qid="2"),                    # 判错，但公布了正确答案 A
        _submitted("题目三", "C", qid="3"),                    # 判错且没有正确答案
    ]
    verdicts = {"1": True, "2": {"ok": False, "answer": "A"}, "3": False}
    with _env("learn", bank=bank):
        ch = base.Chaoxing(account=None, tiku=_FakeTiku())
        with mock.patch.object(base.work_verify, "verify_work",
                               lambda *a, **k: dict(verdicts)):
            ch._sync_after_submit({"title": "课", "courseId": "1"}, submitted, {})

        # 判对 / 平台公布答案的题入库
        assert bank.lookup("题目一", "single", OPTS, course="课", course_id="1") == "A"
        assert bank.lookup("题目二", "single", OPTS, course="课", course_id="1") == "A"
        # 判错又没正确答案的题：库里不该有东西（绝不猜）
        assert bank.lookup("题目三", "single", OPTS,
                           course="课", course_id="1") is None
        # 判错的两个答案进了错题本，判对的没进
        assert wa.known_keys("题目二", "single", OPTS) == {"乙选项"}
        assert wa.known_keys("题目三", "single", OPTS) == {"丙选项"}
        assert wa.known_keys("题目一", "single", OPTS) == set()


def test_reanswer_keeps_right_answers_and_changes_the_wrong_one():
    """复现用户现场：5 题全部由题库作答 -> 平台判「4 对 1 错」-> 重答。

    期望：判对的 4 题仍然直接命中题库（不浪费额度、答案不变）；
    判错的那 1 题必须**换答案**（题库条目被删 + 走远程重新搜题），
    否则就是「重新作答却又提交同一批答案」的死循环。
    """
    bank = _bank("reanswer")
    titles = ["题目一", "题目二", "题目三", "题目四", "题目五"]
    submitted = []
    for idx, title in enumerate(titles, start=1):
        answer_letter = "B" if idx == 5 else "A"     # 第 5 题用的是错答案
        submitted.append(_submitted(title, answer_letter, qid=str(idx)))
    bank.learn([dict(it, qid=int(it["qid"])) for it in submitted],
               {i: True for i in range(1, 6)}, course="课", course_id="1")

    verdicts = {str(i): True for i in range(1, 5)}
    verdicts["5"] = False                            # 第 5 题判错，且平台没公布正确答案

    with _env("reanswer", bank=bank):
        ch = base.Chaoxing(account=None, tiku=_FakeTiku())
        with mock.patch.object(base.work_verify, "verify_work",
                               lambda *a, **k: dict(verdicts)):
            ch._sync_after_submit({"title": "课", "courseId": "1"}, submitted, {})

        tiku = _FakeTiku()
        tiku.answers = ["丙选项"]                     # 重新搜题给了一个不同的答案
        # 判对的题：题库直接命中，不请求远程
        for title in titles[:4]:
            assert tiku.query(_q(title), course="课", course_id="1") == "A", title
        assert tiku.queries == [], "判对的题不该重新搜题"
        # 判错的题：换答案
        got = tiku.query(_q("题目五"), course="课", course_id="1")
        assert got == "丙选项", f"判错的题必须换答案，实际 {got!r}"
        assert tiku.queries, "判错的题必须重新搜题"


def test_sync_after_submit_does_nothing_without_verdicts():
    bank = _bank("noverdict")
    with _env("noverdict", bank=bank):
        ch = base.Chaoxing(account=None, tiku=_FakeTiku())
        with mock.patch.object(base.work_verify, "verify_work", lambda *a, **k: {}):
            ch._sync_after_submit({"title": "课", "courseId": "1"},
                                  [_submitted("题目一", "B")], {})
        assert bank.lookup("题目一", "single", OPTS,
                           course="课", course_id="1") is None
        assert wa.known_keys("题目一", "single", OPTS) == set()


def test_quarantine_last_submission_purges_bank_and_cache():
    """「同一批答案原样交了两轮还是不过」-> 整批作废（含题库），下一轮才会换答案。"""
    bank = _bank("quarantine")
    bank.learn([{"qid": 1, "title": "题目一", "type": "single",
                 "options": OPTS, "answer": "B"}],
               {1: True}, course="课", course_id="1")
    with _env("quarantine", bank=bank) as (_book, cache_path):
        cache = answer.CacheDAO(cache_path)
        cache.add_cache("题目一", "乙选项")

        ch = base.Chaoxing(account=None, tiku=_FakeTiku())
        ch.last_submitted = [_submitted("题目一", "B")]

        assert ch.quarantine_last_submission() == 1
        assert wa.known_keys("题目一", "single", OPTS) == {"乙选项"}
        assert cache.get_cache("题目一") is None
        assert bank.lookup("题目一", "single", OPTS,
                           course="课", course_id="1") is None, "无效答案不能留在题库里"
        assert ch.last_submitted == [] and ch.last_submitted_digest == ""


def test_quarantine_last_submission_is_safe_when_nothing_submitted():
    ch = base.Chaoxing(account=None, tiku=_FakeTiku())
    assert ch.quarantine_last_submission() == 0
    assert ch.discard_last_submission() == 0


def test_discard_last_submission_clears_cache_so_ai_is_asked_again():
    with _env("discard") as (_book, cache_path):
        cache = answer.CacheDAO(cache_path)
        cache.add_cache("题目一", "乙选项")
        cache.add_cache("题目二", "甲选项")

        ch = base.Chaoxing(account=None, tiku=_FakeTiku())
        ch.last_submitted = [_submitted("题目一", "B"), _submitted("题目二", "A")]
        ch.last_submitted_digest = "abc"

        assert ch.discard_last_submission() == 2
        assert cache.get_cache("题目一") is None
        assert cache.get_cache("题目二") is None
        assert ch.last_submitted == [] and ch.last_submitted_digest == ""


def test_discard_keeps_judged_correct_answers_cached():
    """「4 对 1 错」判不通过时，重答只作废判错的那题。

    做对的题必须保留原答案：作废它们只会让下一轮白跑一遍题库/AI，
    甚至换成更差的答案，把本来能及格的那几道也做错。
    """
    with _env("discard_right") as (_book, cache_path):
        cache = answer.CacheDAO(cache_path)
        titles = ["题目一", "题目二", "题目三"]
        for title in titles:
            cache.add_cache(title, "乙选项")

        ch = base.Chaoxing(account=None, tiku=_FakeTiku())
        ch.last_submitted = [_submitted(t, "B", qid=str(i))
                             for i, t in enumerate(titles, start=1)]
        ch.last_verdicts = {"1": True, "2": True, "3": False}

        assert ch.discard_last_submission() == 1, "只该作废判错的那 1 题"
        assert cache.get_cache("题目一") == "乙选项", "判对的题不能重新搜题"
        assert cache.get_cache("题目二") == "乙选项"
        assert cache.get_cache("题目三") is None, "判错的题必须重新搜题"
        assert ch.last_verdicts == {} and ch.last_submitted == []


def test_quarantine_keeps_judged_correct_answers():
    """整批作废也不能动平台判对的题（题库 / 缓存都保留）。"""
    bank = _bank("quarantine_right")
    bank.learn([{"qid": "1", "title": "题目一", "type": "single",
                 "options": OPTS, "answer": "B"},
                {"qid": "2", "title": "题目二", "type": "single",
                 "options": OPTS, "answer": "A"}],
               {"1": True, "2": True}, course="课", course_id="1")
    with _env("quarantine_right", bank=bank) as (_book, cache_path):
        cache = answer.CacheDAO(cache_path)
        cache.add_cache("题目一", "乙选项")
        cache.add_cache("题目二", "甲选项")

        ch = base.Chaoxing(account=None, tiku=_FakeTiku())
        ch.last_submitted = [_submitted("题目一", "B", qid="1"),
                             _submitted("题目二", "A", qid="2")]
        ch.last_verdicts = {"1": True, "2": False}

        assert ch.quarantine_last_submission() == 1, "只有判错的那题进错题本"
        assert wa.known_keys("题目二", "single", OPTS) == {"甲选项"}
        assert wa.known_keys("题目一", "single", OPTS) == set(), "判对的题不该记错题"
        assert cache.get_cache("题目一") == "乙选项"
        assert cache.get_cache("题目二") is None
        assert bank.lookup("题目一", "single", OPTS,
                           course="课", course_id="1") is not None, \
            "判对的答案不能从题库里删掉"


def test_digest_changes_only_when_answers_change():
    one = [_submitted("题目一", "B"), _submitted("题目二", "A", qid="2")]
    same = [_submitted("题目一", "B"), _submitted("题目二", "A", qid="2")]
    other = [_submitted("题目一", "C"), _submitted("题目二", "A", qid="2")]
    assert base.Chaoxing._digest_of(one) == base.Chaoxing._digest_of(same)
    assert base.Chaoxing._digest_of(one) != base.Chaoxing._digest_of(other)


# --------------------------------------------------------------------------- #
# 6. 通知 URL 之类的日志脱敏不受影响（回归）——这里只确认模块没被写坏
# --------------------------------------------------------------------------- #
def test_wrong_book_stats():
    with _env("stats"):
        wa.record_wrong("题目一", "single", OPTS, "B")
        wa.record_wrong("题目一", "single", OPTS, "A")
        stats = wa.get_book().stats()
        assert stats["questions"] == 1 and stats["wrong"] == 2, stats
        assert stats["path"].endswith(".json")


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    try:
        for fn in tests:
            try:
                fn()
                print(f"PASS  {fn.__name__}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL  {fn.__name__}: {exc}")
                annotate_exc(f"test_wrong_answers.py::{fn.__name__}")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {fn.__name__}: unexpected")
                annotate_exc(f"test_wrong_answers.py::{fn.__name__}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("ALL PASS" if not failed else f"FAILED: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
