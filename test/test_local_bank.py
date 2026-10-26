# -*- coding: utf-8 -*-
# layer: core  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""本地题库测试（api/local_bank.py + api/work_verify.py）。

覆盖：
  1. 指纹稳定：题号 / 分值 / 题型标签 / 选项顺序不同，仍是同一道题；
  2. 收录口径：**只存正确答案**——判对存本次答案、判错但有正确答案存正确答案、
     判错无正确答案不入库、没核验一律不入库；
  3. 只补缺失：题库已有正确答案的题跳过（不重复写、不改写）；
  4. 选项乱序时按文本重映射字母；文本对不上时宁可不答；
  5. 判断题 true/false 按配置词表翻译；
  6. 文件结构：按课程一个文件、全账号共享（不再按账号分文件夹），旧结构自动迁移；
  7. 导出 / 导入合并 / 多文件合并的冲突取舍；
  8. 关闭开关后完全不读不写；文件损坏时备份并继续工作；
  9. 判分页面解析（对/错/得分/正确答案）。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate_exc  # noqa: E402

import api.local_bank as lb  # noqa: E402
import api.work_verify as wv  # noqa: E402

TMP = tempfile.mkdtemp(prefix="localbank_")
_seq = 0

OPTS = "A. 甲选项\nB. 乙选项\nC. 丙选项"


def _store(root=None, enabled=True, verify=True) -> lb.BankStore:
    """建一个独立题库目录（每次一个新根目录，互不干扰）。"""
    global _seq
    _seq += 1
    root = root or os.path.join(TMP, f"store{_seq}")
    return lb.BankStore(root=root, enabled=enabled, verify=verify,
                        true_word="正确", false_word="错误",
                        legacy=os.path.join(TMP, f"legacy{_seq}.json"))


def _bank(enabled=True, verify=True) -> lb.BankStore:
    return _store(enabled=enabled, verify=verify)


def _item(qid="1", title="1. 题目一（2.0分）", qtype="single", options=OPTS, answer="B"):
    return {"qid": qid, "title": title, "type": qtype, "options": options, "answer": answer}


# --------------------------------------------------------------------------- #
# 1. 指纹
# --------------------------------------------------------------------------- #
def test_fingerprint_is_stable_across_noise():
    variants = [
        "1. 下列说法正确的是（）",
        "下列说法正确的是（2.0分）",
        "【单选题】下列说法正确的是",
        ". 下列说法正确的是",
    ]
    keys = {lb.fingerprint(t, "single", OPTS) for t in variants}
    assert len(keys) == 1, keys


def test_fingerprint_ignores_option_order():
    plain = lb.fingerprint("题目", "single", OPTS)
    shuffled = lb.fingerprint("题目", "single", "C. 丙选项\nA. 甲选项\nB. 乙选项")
    assert plain == shuffled
    assert lb.fingerprint("题目", "multiple", OPTS) != plain, "不同题型必须是不同的题"


def test_fingerprint_uses_options():
    other = "A. 丁选项\nB. 戊选项\nC. 己选项"
    assert lb.fingerprint("题目", "single", OPTS) != lb.fingerprint("题目", "single", other)


# --------------------------------------------------------------------------- #
# 2. 入库口径
# --------------------------------------------------------------------------- #
def test_only_correct_answers_are_stored():
    bank = _bank()
    assert bank.learn([_item()], {"1": False}) == 0
    assert bank.lookup("题目一", "single", OPTS) is None
    assert bank.learn([_item()], {"1": True}) == 1
    assert bank.lookup("题目一", "single", OPTS) == "B"


def test_wrong_answer_with_server_correct_answer_is_stored():
    """提交的答案判错，但页面给了「正确答案」-> 存正确答案，下次直接答对。"""
    bank = _bank()
    assert bank.learn([_item(answer="C")], {"1": {"ok": False, "answer": "A"}}) == 1
    assert bank.lookup("题目一", "single", OPTS) == "A", "应存平台给出的正确答案"


def test_wrong_answer_without_correct_answer_is_dropped():
    bank = _bank()
    assert bank.learn([_item(answer="C")], {"1": {"ok": False}}) == 0
    assert bank.lookup("题目一", "single", OPTS) is None


def test_unverified_answers_are_never_stored():
    """没做核验 = 不知道对错：题库只存正确答案，一条都不收。"""
    bank = _bank()
    assert bank.learn([_item()], None) == 0
    assert bank.learn([_item()], {}, allow_unverified=True) == 0
    assert bank.stats()["total"] == 0


def test_verdicts_without_this_question_are_skipped():
    """核验只认出部分题时，没认出的题不能被顺带入库。"""
    bank = _bank()
    assert bank.learn([_item(), _item(qid="2", title="2. 题目二")], {"1": True}) == 1
    assert bank.lookup("题目二", "single", OPTS) is None


def test_existing_answer_is_not_rewritten_when_consistent():
    """平台结果和库里一致就不写；不一致（说明库里那条是坏的）就覆写。"""
    bank = _bank()
    assert bank.learn([_item(answer="B")], {"1": True}) == 1
    assert bank.learn([_item(answer="B")], {"1": True}) == 0, "答案一致不应重复入库"
    assert bank.learn([_item(answer="B")],
                      {"1": {"ok": False, "answer": "B"}}) == 0, "正确答案与库一致不应重写"
    assert bank.lookup("题目一", "single", OPTS) == "B"

    # 判对但和库里不一致：库里那条是坏的，用本次被判对的答案修好
    assert bank.learn([_item(answer="C")], {"1": True}) == 1
    assert bank.lookup("题目一", "single", OPTS) == "C"

    # only_missing=False 时无条件写
    assert bank.learn([_item(answer="B")], {"1": True}, only_missing=False) == 1
    assert bank.lookup("题目一", "single", OPTS) == "B"


def test_wrong_bank_answer_is_overwritten_by_server_answer():
    """用题库答案作答被判错 -> 库里的坏答案被页面给出的正确答案覆写。"""
    bank = _bank()
    bank.learn([_item(answer="B")], {"1": True})
    overwrite = {"1": {"ok": False, "answer": "A",
                       "options": "A. 甲选项\nB. 乙选项"}}
    assert bank.learn([_item(answer="B")], overwrite) == 1
    assert bank.stats()["total"] == 1, "覆写而不是新增"
    assert bank.lookup("题目一", "single", OPTS) == "A"

    # 再把同一结果学一遍：已一致，不再重复写
    assert bank.learn([_item(answer="B")], overwrite) == 0


def test_judgement_letter_and_word_are_equivalent():
    """判断题「正确答案：A」与库里的「正确」是同一个答案，不该产生覆写。"""
    jdg = "A. 正确\nB. 错误"
    bank = _bank()
    bank.learn([_item(qtype="judgement", options=jdg, answer="正确")], {"1": True})
    assert bank.learn([_item(qtype="judgement", options=jdg, answer="错误")],
                      {"1": {"ok": False, "answer": "A", "options": jdg}}) == 0
    assert bank.lookup("题目一", "judgement", jdg) == "正确"
    # 换成 B（错误）就真的不一致了
    assert bank.learn([_item(qtype="judgement", options=jdg, answer="正确")],
                      {"1": {"ok": False, "answer": "B", "options": jdg}}) == 1
    assert bank.lookup("题目一", "judgement", jdg) == "错误"


def test_judgement_letter_without_options_is_not_stored():
    """判断题只给字母又没有选项可对照 -> 不知道是「对」还是「错」，不写。"""
    bank = _bank()
    assert bank.learn([_item(qtype="judgement", options="", answer="正确")],
                      {"1": {"ok": False, "answer": "A"}}) == 0
    assert bank.stats()["total"] == 0


def test_answer_that_cannot_map_is_treated_as_missing():
    """库里答案映射不到当前选项（旧答案配错选项）-> 视为缺答案，允许被纠正。"""
    bank = _bank()
    # 判分页选项和提交页选项文字不同：存进来的答案文本对不上提交页选项
    bank.learn([_item(answer="B")],
               {"1": {"ok": False, "answer": "A", "options": "A. 全新一\nB. 全新二"}})
    assert bank.lookup("题目一", "single", OPTS) is None, "映射不上就不该答"
    assert [it["qid"] for it in bank.missing([_item()])] == ["1"], "应视为缺答案"
    # 这次拿到能映射的正确答案 -> 覆盖掉坏数据
    assert bank.learn([_item()], {"1": {"ok": False, "answer": "C"}}) == 1
    assert bank.lookup("题目一", "single", OPTS) == "C"


def test_missing_lists_only_questions_without_answer():
    bank = _bank()
    bank.learn([_item()], {"1": True})
    pending = bank.missing([_item(), _item(qid="2", title="2. 题目二")])
    assert [it["qid"] for it in pending] == ["2"], pending
    assert bank.known([_item(), _item(qid="2", title="2. 题目二")]) == 1


def test_server_side_correct_answer_wins():
    bank = _bank()
    assert bank.learn([_item(answer="C")], {"1": "A"}) == 1
    assert bank.lookup("题目一", "single", OPTS) == "A"


def test_empty_answer_is_never_stored():
    bank = _bank()
    assert bank.learn([_item(answer="")], {"1": True}) == 0


# --------------------------------------------------------------------------- #
# 3. 选项重映射
# --------------------------------------------------------------------------- #
def test_remap_when_options_shuffled():
    bank = _bank()
    bank.learn([_item(answer="B")], {"1": True})       # B = 乙选项
    shuffled = "A. 乙选项\nB. 甲选项\nC. 丙选项"
    assert bank.lookup("题目一", "single", shuffled) == "A"


def test_no_answer_when_option_text_changed():
    """选项文字都变了（题干被改写）：不能硬按旧字母提交，宁可放弃。"""
    bank = _bank()
    bank.learn([_item(answer="B")], {"1": True})
    rewritten = "A. 全新选项一\nB. 全新选项二\nC. 全新选项三"
    assert bank.lookup("题目一", "single", rewritten) is None


def test_multiple_choice_remap():
    bank = _bank()
    multi_opts = "A. 甲\nB. 乙\nC. 丙\nD. 丁"
    bank.learn([_item(qtype="multiple", options=multi_opts, answer="AC")], {"1": True})
    shuffled = "A. 丙\nB. 甲\nC. 丁\nD. 乙"
    assert bank.lookup("题目一", "multiple", shuffled) == "AB"


def test_judgement_uses_configured_words():
    bank = _bank()
    bank.learn([_item(qtype="judgement", options="", answer="true")], {"1": True})
    assert bank.lookup("题目一", "judgement", "") == "正确"
    # 同一道题已有答案，想改写要显式 only_missing=False（正常流程不会这样用）
    bank.learn([_item(qtype="judgement", options="", answer="false")], {"1": True},
               only_missing=False)
    assert bank.lookup("题目一", "judgement", "") == "错误"


def test_completion_keeps_raw_text():
    bank = _bank()
    bank.learn([_item(qtype="completion", options="", answer="牛顿第二定律")], {"1": True})
    assert bank.lookup("题目一", "completion", "") == "牛顿第二定律"


# --------------------------------------------------------------------------- #
# 4. 开关 / 容错
# --------------------------------------------------------------------------- #
def test_disabled_bank_reads_and_writes_nothing():
    bank = _bank(enabled=False)
    assert bank.learn([_item()], {"1": True}) == 0
    assert bank.lookup("题目一", "single", OPTS) is None
    assert bank.stats()["total"] == 0
    assert not bank.usable
    assert not os.path.isdir(bank.root), "关闭时不应创建题库目录"


def test_corrupt_file_is_backed_up():
    path = os.path.join(TMP, "broken.json")
    with open(path, "w", encoding="utf8") as fp:
        fp.write("{ 这不是 JSON")
    bank = lb.LocalBank(path=path)
    assert bank.stats()["total"] == 0
    backups = [n for n in os.listdir(TMP) if n.startswith("broken.json.bad.")]
    assert backups, "损坏文件应被备份而不是直接丢弃"


def test_lookup_survives_external_edit():
    """别的账号进程写过库后，本进程再查要能看到新内容（按 mtime 自动重载）。"""
    path = os.path.join(TMP, "shared.json")
    first = lb.LocalBank(path=path)
    first.learn([_item()], {"1": True})
    second = lb.LocalBank(path=path)          # 模拟另一个进程
    second.learn([_item(qid="2", title="2. 题目二")], {"2": True})
    os.utime(path, (0, 0))                    # 强制 mtime 变化，确保触发重载
    assert first.lookup("题目二", "single", OPTS) == "B"


# --------------------------------------------------------------------------- #
# 5. 文件结构：一门课一个文件，全账号共享
# --------------------------------------------------------------------------- #
def test_files_are_split_by_course_only():
    """同课程题目和答案相同 -> 只按课程分文件，不按账号分文件夹。"""
    store = _store()
    store.set_scope(course="高等数学", course_id="1001")
    store.learn([_item()], {"1": True})
    store.set_scope(course="大学英语", course_id="1002")
    store.learn([_item(qid="2", title="2. 题目二")], {"2": True})
    # 换账号做同一门课：写的是同一个文件（不新增账号目录）
    store.set_scope(account="13900000000", course="高等数学", course_id="1001")
    store.learn([_item(qid="3", title="3. 题目三")], {"3": True})

    stats = store.stats()
    rel = sorted(os.path.relpath(c["path"], store.root).replace("\\", "/")
                 for c in stats["courses"])
    assert rel == ["1001.json", "1002.json"], rel
    assert stats["total"] == 3 and stats["files"] == 2
    assert not any(os.path.isdir(os.path.join(store.root, n))
                   for n in os.listdir(store.root)), "不应再产生账号子目录"


def test_lookup_hits_across_accounts_and_courses():
    store = _store()
    store.set_scope(course="高等数学", course_id="1001")
    store.learn([_item()], {"1": True})
    # 换账号做同一门课：同一个文件，直接命中
    store.set_scope(account="13900000000", course="高等数学", course_id="1001")
    assert store.lookup("题目一", "single", OPTS) == "B"
    # 换课程也能命中（同一道题在另一门课里复用）
    store.set_scope(account="13900000000", course="线性代数", course_id="2002")
    assert store.lookup("题目一", "single", OPTS) == "B"


def test_current_course_file_has_priority():
    """同一道题在多个课程文件里都有时，优先当前课程那份。"""
    store = _store()
    store.set_scope(course="高等数学", course_id="1001")
    store.learn([_item(answer="B")], {"1": True})
    store.set_scope(course="大学英语", course_id="1002")
    store.learn([_item(answer="A")], {"1": {"ok": False, "answer": "A"}})
    assert store.lookup("题目一", "single", OPTS) == "A", "应优先命中当前课程文件"
    store.set_scope(course="高等数学", course_id="1001")
    assert store.lookup("题目一", "single", OPTS) == "B"


def test_legacy_single_file_is_migrated():
    legacy = os.path.join(TMP, "legacy_old.json")
    lb.LocalBank(path=legacy).learn([_item()], {"1": True})
    store = lb.BankStore(root=os.path.join(TMP, "mig_root"), legacy=legacy)
    result = store.migrate_legacy()
    assert result and result["added"] == 1, result
    assert not os.path.exists(legacy), "迁移后旧文件应改名，避免重复导入"
    assert os.path.exists(legacy + ".migrated")
    assert store.lookup("题目一", "single", OPTS) == "B"


def test_account_folders_are_migrated_into_course_files():
    """上一版「<根>/<账号>/<课程>.json」自动合并成「<根>/<课程ID>.json」。"""
    root = os.path.join(TMP, "mig_accounts")
    os.makedirs(os.path.join(root, "13800000000"))
    entry = {"q": "题目一", "y": "single", "a": "B", "x": ["乙选项"],
             "o": OPTS, "ok": 1, "ts": 1}
    key = lb.fingerprint("题目一", "single", OPTS)
    old_file = os.path.join(root, "13800000000", "高等数学_1001.json")
    json.dump({"v": 2, "course": "高等数学", "course_id": "1001",
               "entries": {key: dict(entry)}},
              open(old_file, "w", encoding="utf8"), ensure_ascii=False)

    store = lb.BankStore(root=root)
    result = store.migrate_legacy()
    assert result and result["migrated"] == 1, result
    assert store.lookup("题目一", "single", OPTS) == "B"
    assert os.path.isfile(os.path.join(root, "1001.json"))
    assert not os.path.isdir(os.path.join(root, "13800000000")), "旧账号目录应已改名"
    assert os.path.isdir(os.path.join(root, "13800000000" + lb.MIGRATED_SUFFIX)), \
        "旧目录应保留为 .migrated（不删数据）"
    # 重复调用不会再迁移一次
    assert store.migrate_legacy() is None


def test_course_filename_prefers_course_id():
    """文件名用课程 ID：改课名、同名课都不会互相覆盖。"""
    assert lb.course_filename("高等数学", "1001") == "1001.json"
    assert lb.course_filename("高等数学", "") == "高等数学.json"
    assert lb.course_filename("", "1001") == "1001.json"
    assert lb.course_filename("", "") == f"{lb.UNKNOWN_COURSE}.json"
    # 标题里带 Windows 非法字符也不影响文件名
    assert lb.course_filename("高等数学/上", "1001") == "1001.json"


def test_old_title_id_filenames_are_renamed():
    """上一版「<课程标题>_<课程ID>.json」启动时自动改名成「<课程ID>.json」。"""
    root = os.path.join(TMP, "mig_names")
    os.makedirs(root)
    entry = {"q": "题目一", "y": "single", "a": "B", "x": ["乙选项"],
             "o": OPTS, "ok": 1, "ts": 1}
    key = lb.fingerprint("题目一", "single", OPTS)
    old_file = os.path.join(root, "高等数学_1001.json")
    with open(old_file, "w", encoding="utf8") as fh:
        json.dump({"v": 2, "course": "高等数学", "course_id": "1001",
                   "entries": {key: dict(entry)}}, fh, ensure_ascii=False)

    store = lb.BankStore(root=root)
    result = store.migrate_legacy()
    assert result and result["migrated"] == 1, result
    assert not os.path.exists(old_file), "旧命名文件应已改名"
    assert os.path.isfile(os.path.join(root, "1001.json"))
    assert store.lookup("题目一", "single", OPTS) == "B"
    assert store.migrate_legacy() is None, "改名幂等，重复调用不再动文件"


def test_rename_merges_into_existing_course_id_file():
    """目标 ID 文件已存在时合并进去，两份数据都不丢。"""
    root = os.path.join(TMP, "mig_names_merge")
    os.makedirs(root)
    e1 = {"q": "题目一", "y": "single", "a": "B", "x": ["乙选项"],
          "o": OPTS, "ok": 1, "ts": 1}
    e2 = {"q": "题目二", "y": "single", "a": "A", "x": ["甲选项"],
          "o": OPTS, "ok": 1, "ts": 1}
    with open(os.path.join(root, "1001.json"), "w", encoding="utf8") as fh:
        json.dump({"v": 3, "course": "高等数学", "course_id": "1001",
                   "entries": {lb.fingerprint("题目一", "single", OPTS): e1}},
                  fh, ensure_ascii=False)
    with open(os.path.join(root, "高等数学（旧名）_1001.json"), "w",
              encoding="utf8") as fh:
        json.dump({"v": 2, "course": "高等数学（旧名）", "course_id": "1001",
                   "entries": {lb.fingerprint("题目二", "single", OPTS): e2}},
                  fh, ensure_ascii=False)

    store = lb.BankStore(root=root)
    store.migrate_legacy()
    store.set_scope(course="高等数学", course_id="1001")
    assert store.lookup("题目一", "single", OPTS) == "B"
    assert store.lookup("题目二", "single", OPTS) == "A"
    assert sorted(os.listdir(root)) == ["1001.json"], os.listdir(root)


def test_judgement_answer_letter_is_translated():
    """判分页给的是「正确答案：A」，判断题要换成文字（对/错）再入库。"""
    store = _store()
    store.set_scope(course="马原", course_id="3001")
    store.learn([_item(qtype="judgement", options="", answer="false")],
                {"1": {"ok": False, "answer": "A",
                       "options": "A. 对\nB. 错"}})
    assert store.lookup("题目一", "judgement", "") == "正确"


# --------------------------------------------------------------------------- #
# 6. 导出 / 导入 / 合并
# --------------------------------------------------------------------------- #
def test_export_import_keeps_course():
    src = _store()
    src.set_scope(course="高等数学", course_id="1001")
    src.learn([_item()], {"1": True})
    dump = os.path.join(TMP, "bundle.json")
    payload = src.export_to(dump)
    assert payload["files"] == 1 and payload["total"] == 1, payload
    assert "account" not in json.load(open(dump, encoding="utf8"))["files"][0]

    dst = _store()
    result = dst.import_from(dump)
    assert result["added"] == 1 and result["total"] == 1, result
    rel = [os.path.relpath(c["path"], dst.root).replace("\\", "/")
           for c in dst.stats()["courses"]]
    assert rel == ["1001.json"], rel


def test_import_old_account_bundle_merges_into_course_file():
    """旧的「按账号分文件」导出包：忽略账号，按课程合并进同一个文件。"""
    dump = os.path.join(TMP, "old_bundle.json")
    entry = {"q": "题目一", "y": "single", "a": "B", "x": ["乙选项"],
             "o": OPTS, "ok": 1, "ts": 1}
    key = lb.fingerprint("题目一", "single", OPTS)
    payload = {"v": 2, "kind": lb.BUNDLE_KIND, "files": [
        {"account": "138", "course": "高等数学", "course_id": "1001",
         "entries": {key: dict(entry)}},
        {"account": "139", "course": "高等数学", "course_id": "1001",
         "entries": {key: dict(entry)}},
    ]}
    json.dump(payload, open(dump, "w", encoding="utf8"), ensure_ascii=False)

    store = _store()
    result = store.import_from(dump)
    assert result["files"] == 2 and result["total"] == 1, result
    assert [c["file"] for c in store.stats()["courses"]] == ["1001.json"]
    assert store.lookup("题目一", "single", OPTS) == "B"


def test_import_old_style_single_file():
    legacy = os.path.join(TMP, "old_style.json")
    lb.LocalBank(path=legacy).learn([_item()], {"1": True})
    store = _store()
    store.set_scope(course="高等数学", course_id="1001")
    result = store.import_from(legacy)
    assert result["added"] == 1 and result["total"] == 1, result
    assert store.lookup("题目一", "single", OPTS) == "B"


def test_export_import_roundtrip():
    src = _bank()
    src.learn([_item()], {"1": True})
    dump = os.path.join(TMP, "export.json")
    assert src.export_to(dump)["total"] == 1

    dst = _bank()
    result = dst.import_from(dump)
    assert result["added"] == 1 and result["total"] == 1
    assert dst.lookup("题目一", "single", OPTS) == "B"


def test_import_merges_and_keeps_better_answer():
    """冲突时取确认次数更多的答案；相同答案则累加确认次数。"""
    a, b = _bank(), _bank()
    opts2 = "A. 甲选项\nB. 乙选项\nC. 丙选项"
    for _ in range(3):
        a.learn([_item(answer="B")], {"1": True}, only_missing=False)
    b.learn([_item(answer="A")], {"1": True})
    src = os.path.join(TMP, "from_b.json")
    b.export_to(src)

    a.import_from(src)
    assert a.lookup("题目一", "single", opts2) == "B", "确认次数多的答案应保留"
    assert a.stats()["total"] == 1

    c = _bank()
    c.learn([_item(answer="B")], {"1": True})
    src2 = os.path.join(TMP, "same.json")
    c.export_to(src2)
    a.import_from(src2)
    course_file = a.stats()["courses"][0]["path"]
    entries = json.load(open(course_file, encoding="utf8"))["entries"]
    assert list(entries.values())[0]["ok"] >= 4, "同答案应累加确认次数（3 + 1）"


def test_merge_files():
    one, two = _bank(), _bank()
    one.learn([_item()], {"1": True})
    two.learn([_item(qid="2", title="2. 题目二")], {"2": True})
    p1, p2 = os.path.join(TMP, "m1.json"), os.path.join(TMP, "m2.json")
    one.export_to(p1)
    two.export_to(p2)
    out = os.path.join(TMP, "merged.json")
    result = lb.merge_files([p1, p2], out)
    assert result["total"] == 2, result
    merged = json.load(open(out, encoding="utf8"))
    assert merged["files"], "导出包应包含课程文件"


def test_clear_empties_bank():
    bank = _bank()
    bank.learn([_item()], {"1": True})
    assert bank.clear() == 1
    assert bank.stats()["total"] == 0


# --------------------------------------------------------------------------- #
# 7. 配置
# --------------------------------------------------------------------------- #
def test_from_config_defaults_and_overrides():
    default = lb.BankStore.from_config({})
    assert default.enabled and default.verify
    custom = lb.BankStore.from_config({
        "local_bank": "false", "local_bank_verify": "false",
        "local_bank_dir": os.path.join(TMP, "customdir"),
        "true_list": "对,√", "false_list": "错,×",
    })
    assert not custom.enabled and not custom.verify
    assert custom.root.endswith("customdir")
    assert custom.true_word == "对" and custom.false_word == "错"
    # 旧键（单文件路径）仍可用：取其所在目录
    legacy = lb.BankStore.from_config({"local_bank_path": os.path.join(TMP, "old", "local_bank.json")})
    assert legacy.root == os.path.abspath(os.path.join(TMP, "old"))


def test_config_keys_are_validated():
    """配置迁移会校验这几项，非法值回落默认（见 config_store.TIKU_BOOL_KEYS）。"""
    import config_store
    for key in ("local_bank", "local_bank_verify"):
        assert key in config_store.TIKU_BOOL_KEYS, key
        assert config_store._invalid("tiku", key, "maybe") is True
        assert config_store._invalid("tiku", key, "true") is False
        assert config_store._invalid("tiku", key, "") is False
    assert "local_bank_trust" not in config_store.TIKU_BOOL_KEYS, "废弃键应已移除"
    for key in ():
        assert key in config_store.TIKU_BOOL_KEYS, key
        assert config_store._invalid("tiku", key, "maybe") is True
        assert config_store._invalid("tiku", key, "true") is False
        assert config_store._invalid("tiku", key, "") is False


# --------------------------------------------------------------------------- #
# 8. 判分页解析
# --------------------------------------------------------------------------- #
def test_parse_verdicts_from_markers():
    html = """
    <div class="singleQuesId" data="405717711"><div class="TiMu"><span class="dui"></span></div></div>
    <div class="singleQuesId" data="405717712"><div class="TiMu"><span class="cuo"></span></div></div>
    """
    assert wv.parse_verdicts(html) == {"405717711": True, "405717712": False}


def test_parse_verdicts_from_scores():
    html = """
    <div class="singleQuesId" data="405717711"><div>得分：2.0分 满分：2.0分</div></div>
    <div class="singleQuesId" data="405717712"><div>得分：0.0分 满分：2.0分</div></div>
    """
    assert wv.parse_verdicts(html) == {"405717711": True, "405717712": False}


def test_parse_verdicts_from_right_answer():
    html = '<div class="singleQuesId" data="405717711"><div>正确答案：B 我的答案：C</div></div>'
    verdict = wv.parse_verdicts(html)["405717711"]
    assert isinstance(verdict, dict), verdict
    assert verdict["answer"] == "B" and verdict["ok"] is None, verdict


def test_parse_verdicts_wrong_with_correct_answer():
    """判错 + 页面上有正确答案 -> 带上正确答案（本地题库据此纠错）。"""
    html = """
    <div class="singleQuesId" data="405717711">
      <div class="TiMu"><span class="cuo"></span></div>
      <ul><li>A. 甲选项</li><li>B. 乙选项</li></ul>
      <div>正确答案：A 我的答案：B</div>
    </div>
    """
    verdict = wv.parse_verdicts(html)["405717711"]
    assert verdict["ok"] is False and verdict["answer"] == "A", verdict
    assert verdict["options"].startswith("A. 甲选项"), verdict


def test_parse_verdicts_empty_when_no_marks():
    html = '<div class="singleQuesId" data="405717711"><div>教师未创建完成该测验</div></div>'
    assert wv.parse_verdicts(html) == {}
    assert wv.parse_verdicts("") == {}


# 实测的「已提交」页面：我的答案在 div.myAnswer > div.answerCon，对错是 span.marking_dui。
# 旧解析只认 ul>li 的字母前缀和「正确答案：」文本，遇到这个结构一题都读不出来。
PAGE_REAL_GRADED = """
<div class="singleQuesId" data="405804363" id="question405804363">
  <div class="TiMu newTiMu" data="3">
    <div class="Zy_TItle clearfix"><i class="fl">1</i>
      <div class="font-cxsecret"><span class="newZy_TItle">【判断题】</span>加强大学生安全教育是重要依托。( )</div>
    </div>
    <ul class="Zy_ulTop"><li qid="405804363" qtype="3"><span class="num_option" data="A">A</span><a class="fl after">对</a></li>
      <li qid="405804363" qtype="3"><span class="num_option" data="B">B</span><a class="fl after">错</a></li></ul>
  </div>
  <div class="newAnswerBx"><div class="myAnswerBx marBot16"><div class="myAnswer">
      <span class="answerFont fl">我的答案：</span><div class="fl answerCon"> 对 </div><p class="clear"></p></div>
      <div class="answerScore"><div class="CorrectOrNot fl"><span class="marking_dui"></span></div></div>
  </div></div>
</div>
<div class="singleQuesId" data="405804361" id="question405804361">
  <div class="TiMu newTiMu" data="1">
    <div class="Zy_TItle clearfix"><i class="fl">3</i>
      <div><span class="newZy_TItle">【多选题】</span>加强大学生安全教育的意义:( )(多选)</div></div>
    <ul class="Zy_ulTop"><li qid="405804361" qtype="1"><span class="num_option" data="A">A</span><a class="fl after">掌握安全知识</a></li>
      <li qid="405804361" qtype="1"><span class="num_option" data="B">B</span><a class="fl after">提高安全意识</a></li></ul>
  </div>
</div>
"""

PAGE_REAL_UNSUBMITTED = """
<html><body>
<div class="workTitle">章节测验（待完成）</div>
<div class="singleQuesId" data="405803973" id="question405803973">
  <div class="TiMu newTiMu" data="0">
    <div class="Zy_TItle clearfix"><i class="fl">1</i>
      <div><span class="newZy_TItle">【单选题】</span>【选择题】大学生安全教育的基本定义不包括( )</div></div>
    <ul class="Zy_ulTop"><li qid="405803973" qtype="0"><span class="num_option choice405803973" data="A">A</span>
      <a class="fl after">学习大学生安全教育是国家法律法规规定的</a></li></ul>
    <input id="answer405803973" name="answer405803973" type="hidden" value=""/>
  </div>
</div>
<a class="btnSubmit" href="javascript:;">提交</a>
</body></html>
"""


def test_parse_real_graded_page():
    """实测结构：marking_dui + div.answerCon（我的答案）。"""
    verdicts = wv.parse_verdicts(PAGE_REAL_GRADED)
    assert verdicts["405804363"] is True, verdicts
    assert "405804361" not in verdicts, "没有对错标记的多选题不应被判分"
    assert wv.parse_my_answers(PAGE_REAL_GRADED)["405804363"] == "对"
    questions = wv.parse_questions(PAGE_REAL_GRADED)
    assert questions["405804363"]["type"] == "judgement", questions["405804363"]
    assert questions["405804361"]["type"] == "multiple", questions["405804361"]
    assert len(questions) == 2


def test_real_page_state_is_classified():
    """未提交 / 已提交未批阅 / 结构不认识 要能区分（采集日志据此说明原因）。"""
    assert wv.page_state(PAGE_REAL_GRADED, True, True) == wv.STATE_OK
    assert wv.page_state(PAGE_REAL_GRADED, True, False) == wv.STATE_UNGRADED
    assert wv.page_state(PAGE_REAL_UNSUBMITTED, True, False) == wv.STATE_UNSUBMITTED
    assert wv.page_state("<html>参数验证不通过</html>", False, False) == wv.STATE_UNKNOWN


def test_submitted_but_no_answer_is_ungraded():
    """已交过但老师不公布答案的页，不能误报成未提交。

    特征：有「重新作答」入口、没有对错/正确答案标记（marking 为空）。
    """
    page = """
    <html><body>
    <div class="singleQuesId" data="405803973" id="question405803973">
      <div class="TiMu" data="0"><div class="Zy_TItle"><span class="newZy_TItle">【单选题】</span>题干</div></div>
      <ul class="Zy_ulTop"><li><span class="num_option" data="A">A</span><a class="fl after">选项</a></li></ul>
    </div>
    <a class="btnReEdit" href="javascript:;">重新作答</a>
    </body></html>
    """
    assert wv.is_unsubmitted_page(page) is False
    assert wv.page_state(page, True, False) == wv.STATE_UNGRADED


def test_closed_work_page_is_its_own_state():
    """老师关掉作业后接口只回一句「作业已结束」（实测 31 字节），要单独归类。"""
    closed = "<html><body>作业已结束</body></html>"
    assert wv.is_closed_page(closed) is True
    assert wv.page_state(closed, False, False) == wv.STATE_CLOSED
    # 正常作业页里偶然出现这几个字，不能被当成关闭页（靠长度与是否有题目兜住）
    long_page = PAGE_REAL_UNSUBMITTED + "<!-- 作业已结束 -->" * 300
    assert wv.is_closed_page(long_page) is False
    assert wv.page_state(long_page, True, False) == wv.STATE_UNSUBMITTED


def test_normalize_strips_repeated_type_tags():
    """题干「1【单选题】【选择题】x」与「1. x」必须归一成同一串（跨页面指纹一致）。"""
    assert lb.normalize_text("1【单选题】【选择题】大学生安全教育( )") == \
        lb.normalize_text("1. 大学生安全教育")
    assert lb.normalize_text("【判断题】2. 题干") == lb.normalize_text("题干")
    assert lb.normalize_text("2026年安全教育") == "2026年安全教育", "正文里的年份不能被当题号剥掉"


def test_verify_work_swallows_errors():
    class _Boom:
        def get(self, *a, **kw):
            raise RuntimeError("network down")

    assert wv.verify_work(_Boom(), {}) == {}


# --------------------------------------------------------------------------- #
# 9. 端到端：study_work 提交成功后按判分入库
# --------------------------------------------------------------------------- #
def _run_study_work(verdict_html: str, bank: lb.LocalBank, tiku=None):
    """跑一遍 study_work：第一次 GET 抓题、第二次 GET 核验判分。"""
    import api.base as base

    class _Resp:
        def __init__(self, status=200, text="ok", data=None):
            self.status_code = status
            self.text = text
            self._data = data or {}

        def json(self):
            return self._data

    class _Session:
        def __init__(self):
            self.calls = 0
            self.posts = []

        def get(self, url, params=None, **kw):
            self.calls += 1
            return _Resp(200, "<html/>" if self.calls == 1 else verdict_html)

        def post(self, url, data=None, **kw):
            self.posts.append(dict(data) if data else {})
            return _Resp(200, "ok", {"status": True, "msg": "ok"})

    class _Tiku:
        DISABLE = False
        COVER_RATE = 0.0

        def query(self, q):
            return "B"

        def get_submit_params(self):
            return ""

        def judgement_select(self, res):
            return True

        def ensure_available(self):
            return True

        def learn_work_answers(self, items, verdicts=None, course="", course_id="", **kw):
            return bank.learn(items, verdicts, course, course_id=course_id)

    def make_questions():
        # qid 用真实长度（9 位数字）：解析器靠 ≥4 位数字区分 qid 与「题型码 data="0"」，
        # 这里必须和 verdict_html 里 singleQuesId 的 data 一致才能关联判分结果。
        qid = "405717701"
        return {"questions": [{
            "id": qid, "type": "single", "title": "1. 题目一（2.0分）",
            "options": "A. 甲选项\nB. 乙选项",
            "answerField": {f"answer{qid}": "", f"answertype{qid}": "0"},
        }]}

    session = _Session()
    old_session = base.SessionManager.get_session
    old_decode = base.decode_questions_info
    base.SessionManager.get_session = staticmethod(lambda: session)
    base.decode_questions_info = lambda text: make_questions()
    try:
        c = base.Chaoxing(account=None, tiku=tiku or _Tiku())
        result = c.study_work(
            {"courseId": "C", "clazzId": "cz", "title": "高等数学"},
            {"type": "workid", "jobid": "work-1", "enc": "e"},
            {"knowledgeid": "p1", "ktoken": "k", "cpi": "cp"})
        return result, session
    finally:
        base.SessionManager.get_session = old_session
        base.decode_questions_info = old_decode


def test_study_work_stores_only_verified_answers():
    import api.base as base
    bank = _bank()
    old = lb._BANK
    lb._BANK = bank
    try:
        result, session = _run_study_work(
            '<div class="singleQuesId" data="405717701"><i class="dui"></i></div>', bank)
        assert result == base.StudyResult.SUCCESS, result
        assert session.calls >= 2, "提交后应再拉一次页面核验判分"
        assert bank.lookup("题目一", "single", "A. 甲选项\nB. 乙选项") == "B"
    finally:
        lb._BANK = old


def test_study_work_skips_learn_when_answer_wrong():
    bank = _bank()
    old = lb._BANK
    lb._BANK = bank
    try:
        _run_study_work(
            '<div class="singleQuesId" data="405717701"><i class="cuo"></i></div>', bank)
        assert bank.stats()["total"] == 0, "判错的题不能入库"
    finally:
        lb._BANK = old


def test_study_work_ignores_bank_when_disabled():
    bank = _bank(enabled=False)
    old = lb._BANK
    lb._BANK = bank
    try:
        _run_study_work(
            '<div class="singleQuesId" data="405717701"><i class="dui"></i></div>', bank)
        assert bank.stats()["total"] == 0
        assert not os.path.isdir(bank.root), "关闭时不应创建题库目录"
    finally:
        lb._BANK = old


def test_study_work_stores_correct_answer_when_wrong():
    """端到端：本次作答判错，但页面给了正确答案 -> 正确答案入库。"""
    bank = _bank()
    old = lb._BANK
    lb._BANK = bank
    try:
        html = """
        <div class="singleQuesId" data="405717701">
          <div class="TiMu"><span class="cuo"></span></div>
          <ul><li>A. 甲选项</li><li>B. 乙选项</li></ul>
          <div>正确答案：A 我的答案：B</div>
        </div>
        """
        _run_study_work(html, bank)
        assert bank.lookup("题目一", "single", "A. 甲选项\nB. 乙选项") == "A", \
            "判错时应收录平台给出的正确答案"
    finally:
        lb._BANK = old


def test_study_work_writes_into_course_file():
    """入库文件落在 <课程ID>.json（不再按账号建目录）。"""
    bank = _bank()
    old = lb._BANK
    lb._BANK = bank
    try:
        _run_study_work(
            '<div class="singleQuesId" data="405717701"><i class="dui"></i></div>', bank)
        courses = bank.stats()["courses"]
        assert len(courses) == 1, courses
        rel = os.path.relpath(courses[0]["path"], bank.root).replace("\\", "/")
        assert rel == "C.json", rel
    finally:
        lb._BANK = old


def test_study_work_verifies_even_when_bank_has_answer():
    """题库已有答案也要核验：否则「用题库答案作答却被判错」永远发现不了。"""
    bank = _bank()
    bank.set_scope(course="高等数学", course_id="C")
    bank.learn([_item(title="1. 题目一（2.0分）", options="A. 甲选项\nB. 乙选项")],
               {"1": True})
    old = lb._BANK
    lb._BANK = bank
    try:
        result, session = _run_study_work(
            '<div class="singleQuesId" data="405717701"><i class="dui"></i></div>', bank)
        import api.base as base
        assert result == base.StudyResult.SUCCESS, result
        assert session.calls == 2, "题库已有答案时仍应核验判分"
        assert bank.stats()["total"] == 1, "答案一致时不应重复写入"
        assert bank.lookup("题目一", "single", "A. 甲选项\nB. 乙选项") == "B"
    finally:
        lb._BANK = old


def test_study_work_overwrites_wrong_bank_answer():
    """用题库答案作答被判错：用页面给的正确答案覆写库里那条坏答案。"""
    bank = _bank()
    bank.set_scope(course="高等数学", course_id="C")
    bank.learn([_item(title="1. 题目一（2.0分）", options="A. 甲选项\nB. 乙选项")],
               {"1": True})                        # 库里存的是 B（错的）
    old = lb._BANK
    lb._BANK = bank
    try:
        html = """
        <div class="singleQuesId" data="405717701">
          <div class="TiMu"><span class="cuo"></span></div>
          <ul><li>A. 甲选项</li><li>B. 乙选项</li></ul>
          <div>正确答案：A 我的答案：B</div>
        </div>
        """
        _run_study_work(html, bank)
        assert bank.stats()["total"] == 1, "覆写而不是新增"
        assert bank.lookup("题目一", "single", "A. 甲选项\nB. 乙选项") == "A", \
            "用题库答案答错后应被正确答案覆写"
    finally:
        lb._BANK = old


# --------------------------------------------------------------------------- #
# 10. 并发：课程归属逐次显式传参（不再依赖进程级 set_scope）
# --------------------------------------------------------------------------- #
def test_learn_uses_explicit_course_without_scope():
    """不设作用域也能写对文件：course / course_id 由调用方给出。"""
    store = _store()
    assert store.learn([_item()], {"1": True},
                       course="高等数学", course_id="1001") == 1
    assert store.learn([_item(qid="2", title="2. 题目二")], {"2": True},
                       course="大学英语", course_id="1002") == 1
    rel = sorted(os.path.relpath(c["path"], store.root).replace("\\", "/")
                 for c in store.stats()["courses"])
    assert rel == ["1001.json", "1002.json"], rel


def test_lookup_priority_uses_explicit_course():
    """同一道题在两门课里答案不同：按显式传入的课程取对应那份。"""
    store = _store()
    store.learn([_item(answer="B")], {"1": True},
                course="高等数学", course_id="1001")
    store.learn([_item(answer="A")], {"1": {"ok": False, "answer": "A"}},
                course="大学英语", course_id="1002")
    assert store.lookup("题目一", "single", OPTS,
                        course="高等数学", course_id="1001") == "B"
    assert store.lookup("题目一", "single", OPTS,
                        course="大学英语", course_id="1002") == "A"


def test_concurrent_learn_writes_each_course_separately():
    """两门课并发入库：各写各的文件，互不串台（无进程级作用域参与）。"""
    store = _store()
    gate = threading.Barrier(2)
    errors: list = []

    def worker(course: str, course_id: str, tag: str) -> None:
        try:
            gate.wait(10)
            for i in range(8):
                qid = f"{tag}{i}"
                store.learn([_item(qid=qid, title=f"{i}. {tag}题目{i}")],
                            {qid: True}, course=course, course_id=course_id)
                time.sleep(0.001)          # 拉长重叠窗口，放大竞争概率
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{tag}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=args)
               for args in (("高等数学", "1001", "A"), ("大学英语", "1002", "B"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert not errors, errors
    assert not any(t.is_alive() for t in threads), "线程未结束"
    rel = sorted(os.path.relpath(c["path"], store.root).replace("\\", "/")
                 for c in store.stats()["courses"])
    assert rel == ["1001.json", "1002.json"], rel
    for cid, tag, other in (("1001", "A", "B"), ("1002", "B", "A")):
        with open(os.path.join(store.root, f"{cid}.json"), encoding="utf8") as fh:
            titles = [str(e.get("q") or "") for e in json.load(fh)["entries"].values()]
        assert len(titles) == 8, (cid, titles)
        assert all(tag in t for t in titles), (cid, titles)
        assert not any(other in t for t in titles), f"{cid} 里混进了别的课程的题：{titles}"


def test_study_work_passes_course_to_tiku():
    """study_work 把课程显式传给题库，且不写进程级作用域。"""
    bank = _bank()
    seen: list = []

    class _Recorder:
        DISABLE = False
        COVER_RATE = 0.0

        def query(self, q, course="", course_id=""):
            seen.append((course, course_id))
            return "B"

        def get_submit_params(self):
            return ""

        def ensure_available(self):
            return True

    old = lb._BANK
    lb._BANK = bank
    try:
        _run_study_work('<div class="singleQuesId" data="405717701">'
                        '<i class="dui"></i></div>', bank, tiku=_Recorder())
    finally:
        lb._BANK = old

    assert seen == [("高等数学", "C")], seen
    assert bank.scope == {"course": "", "course_id": ""}, "不应再写全局作用域"


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
                annotate_exc(f"test_local_bank.py::{fn.__name__}")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {fn.__name__}: unexpected")
                annotate_exc(f"test_local_bank.py::{fn.__name__}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("ALL PASS" if not failed else f"FAILED: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
