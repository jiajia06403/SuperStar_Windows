# -*- coding: utf-8 -*-
# layer: core  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""题库答案缓存并发一致性测试（api/answer.py::CacheDAO）。

背景：任务点线程池最多 5 个线程并发答题，每个线程各自 new 一个 CacheDAO，
add_cache 是「读文件 -> 改内存 -> 整块回写」。锁如果是实例级，互斥不成立，
后写的那份会覆盖掉别的线程刚写进去的题目（丢更新）。

覆盖：
  1. 8 线程并发 add_cache 各写不同题目，最终文件里 8 条都在；
  2. 不同实例指向同一文件时共用一把锁（这是互斥成立的前提）；
  3. 不同文件互不阻塞（各用各的锁）；
  4. 锁可重入（add_cache 内再调 _read_cache/_write_cache 不死锁）；
  5. remove_cache 并发删各自的题，最终只丢该丢的；
  6. 缓存文件位置与格式不变（progress/cache.json 之外的路径亦为纯 JSON 字典）。
"""
import json
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate_exc  # noqa: E402

from api.answer import CacheDAO, _cache_lock_for  # noqa: E402

TMP = tempfile.mkdtemp(prefix="cacheconc_")
_seq = 0
WORKERS = 8


def _file() -> str:
    """每个用例一份独立缓存文件，互不干扰。"""
    global _seq
    _seq += 1
    return os.path.join(TMP, f"cache{_seq}.json")


def _dao(path: str = None) -> CacheDAO:
    return CacheDAO(path or _file())


# --------------------------------------------------------------------------- #
# 1. 并发写不丢更新
# --------------------------------------------------------------------------- #
def test_concurrent_add_cache_keeps_every_question():
    """8 个线程各写一道不同的题，全部都要留下（锁为实例级时会丢）。"""
    path = _file()
    dao = _dao(path)
    keys = [f"并发题目{i}（{i}.0分）" for i in range(WORKERS)]
    barrier = threading.Barrier(WORKERS)
    errors = []

    def worker(idx: int):
        try:
            d = _dao(path)                 # 每个线程自己的实例（与 Tiku.query 一致）
            barrier.wait()                 # 尽量同时开跑，放大竞态窗口
            d.add_cache(keys[idx], f"答案{idx}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{idx}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, f"并发写入抛异常: {errors}"
    assert not any(t.is_alive() for t in threads), "有线程超时未结束（可能死锁）"

    data = json.loads(open(path, encoding="utf8").read())
    missing = [k for k in keys if k not in data]
    assert not missing, f"并发写丢更新，缺失 {len(missing)}/{WORKERS} 条: {missing[:3]}"
    assert all(data[k] == f"答案{i}" for i, k in enumerate(keys)), "答案内容被写坏"


def test_concurrent_add_cache_repeated_rounds():
    """多轮放大：每轮 8 线程各写 1 题，任一轮丢一条即判失败。"""
    for _round in range(5):
        path = _file()
        keys = [f"轮{_round}题目{i}" for i in range(WORKERS)]
        barrier = threading.Barrier(WORKERS)

        def worker(idx: int, keys=keys, path=path, barrier=barrier):
            d = _dao(path)
            barrier.wait()
            d.add_cache(keys[idx], f"A{idx}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        data = json.loads(open(path, encoding="utf8").read())
        assert len(data) == WORKERS, (
            f"第 {_round} 轮并发写丢更新：期望 {WORKERS} 条，实际 {len(data)} 条"
        )


# --------------------------------------------------------------------------- #
# 2~4. 锁的作用域
# --------------------------------------------------------------------------- #
def test_instances_share_one_lock_per_file():
    """同一文件：不同实例（含不同写法路径）必须共用一把锁。"""
    path = _file()
    a, b = CacheDAO(path), CacheDAO(path)
    assert a._lock is b._lock, "同一缓存文件的两个实例没共用锁，互斥不成立"
    c = CacheDAO(os.path.join(path, "..", os.path.basename(path)))  # 未归一化的同一路径
    assert c._lock is a._lock, "等价路径（含 ..）应解析到同一把锁"


def test_different_files_have_different_locks():
    """不同文件各用各的锁：避免无关缓存互相串行。"""
    assert _cache_lock_for(_file()) is not _cache_lock_for(_file()), \
        "不同缓存文件不应共用一把锁"


def test_lock_is_reentrant():
    """RLock：add_cache / remove_cache 持锁后再进 _read_cache/_write_cache 不死锁。"""
    dao = _dao()
    done = threading.Event()

    def run():
        with dao._lock:                 # 外层已持锁，内部再取必须放行
            dao.add_cache("重入题目", "A")
            dao.remove_cache("重入题目")
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=10)
    assert done.is_set(), "锁不可重入，_read_cache/_write_cache 发生死锁"


# --------------------------------------------------------------------------- #
# 5. 并发删除
# --------------------------------------------------------------------------- #
def test_concurrent_remove_cache_only_drops_its_own():
    """并发 add + remove 各自的题：不丢别人的、不误删别人的。"""
    path = _file()
    keys = [f"删除题目{i}" for i in range(WORKERS)]
    for i, k in enumerate(keys):        # 预置 8 条
        _dao(path).add_cache(k, f"A{i}")
    barrier = threading.Barrier(WORKERS)
    errors = []

    def worker(idx: int):
        try:
            d = _dao(path)
            barrier.wait()
            if idx % 2 == 0:
                assert d.remove_cache(keys[idx]) is True, "预置的题目应能删掉"
            else:
                d.add_cache(f"新增题目{idx}", f"N{idx}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{idx}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, f"并发删写抛异常: {errors}"

    data = json.loads(open(path, encoding="utf8").read())
    survived = [keys[i] for i in range(WORKERS) if i % 2]
    assert all(k in data for k in survived), "奇数号题目被误删（丢更新）"
    assert not any(keys[i] in data for i in range(WORKERS) if i % 2 == 0), "偶数号未被删掉"
    assert all(f"新增题目{i}" in data for i in range(WORKERS) if i % 2), "新增题目丢失"


# --------------------------------------------------------------------------- #
# 6. 文件位置与格式不变
# --------------------------------------------------------------------------- #
def test_default_path_and_format_unchanged():
    """默认仍为 progress_cache/cache.json，落盘是「题目->答案」的纯 JSON 字典。"""
    from api.answer import _default_cache_file
    default = _default_cache_file()
    assert os.path.basename(default) == "cache.json", f"缓存文件名变了: {default}"
    assert os.path.basename(os.path.dirname(default)) == "progress_cache", \
        f"缓存目录变了: {default}"
    assert CacheDAO.DEFAULT_CACHE_FILE == "cache.json"

    path = _file()
    dao = _dao(path)
    dao.add_cache("格式题目", "格式答案")
    raw = open(path, encoding="utf8").read()
    assert json.loads(raw) == {"格式题目": "格式答案"}, f"缓存格式变了: {raw[:80]}"
    assert dao.get_cache("格式题目") == "格式答案"


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
                annotate_exc(f"test_cache_concurrency.py::{fn.__name__}")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {fn.__name__}: unexpected")
                annotate_exc(f"test_cache_concurrency.py::{fn.__name__}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("ALL PASS" if not failed else f"FAILED: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
