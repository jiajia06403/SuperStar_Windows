# -*- coding: utf-8 -*-
"""大模型答题的降级行为测试（无需网络 / 无需 GUI）。

覆盖本次改动的三条约定：
  1. 未配置 API Key：SiliconFlow（以及未勾选忽略 Key 的 AI）**一个请求都不发**，
     题库直接进入「不可用」，题目随机作答。
  2. provider=AI 且勾选 ignore_api_key=true：允许无鉴权访问自建端点。
  3. 任务开始时只做一次可用性检查；检查失败后整轮不再逐题请求大模型；
     连续失败达阈值同样停用（不会每道题都去打大模型）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ci_annotate import annotate, annotate_exc  # noqa: E402

import api.answer as answer  # noqa: E402
from api.answer import AI, DeepSeek, SiliconFlow, Tiku, TikuLike  # noqa: E402

BASE_CONF = {
    "provider": "AI",
    "submit": "true",
    "cover_rate": "0",
    "delay": "0",
    "work_retry": "1",
    "true_list": "正确,对,√,是",
    "false_list": "错误,错,×,否,不对,不正确",
    "siliconflow_endpoint": "https://api.siliconflow.cn/v1/chat/completions",
    "siliconflow_model": "deepseek-ai/DeepSeek-V3.2",
    "min_interval_seconds": "0",
}

_TMP_CACHE = os.path.join(tempfile.mkdtemp(prefix="superstar_cache_"), "cache.json")


def _patch_cache():
    """把题库答案缓存指向临时目录，避免污染真实数据目录。"""
    answer._default_cache_file = lambda: _TMP_CACHE


class _CountedTiku(Tiku):
    """记录 check_llm_connection / _query 调用次数的假题库。"""
    LLM_PROVIDER = True

    def __init__(self, conn_ok=True, query_raises=False):
        super().__init__()
        self.name = "假大模型"
        self.conn_ok = conn_ok
        self.query_raises = query_raises
        self.check_calls = 0
        self.query_calls = 0

    def check_llm_connection(self) -> bool:
        self.check_calls += 1
        return self.conn_ok

    def _query(self, q_info):
        self.query_calls += 1
        if self.query_raises:
            raise RuntimeError("模拟网络不可达")
        return "答案"


def _build(cls, **overrides):
    tiku = cls()
    conf = dict(BASE_CONF)
    conf.update(overrides)
    tiku.config_set(conf)
    tiku.init_tiku()
    return tiku


def _question(title="1+1=?"):
    return {"title": title, "type": "single", "options": "A. 1\nB. 2"}


# --------------------------------------------------------------------------- #
def test_siliconflow_without_key_sends_no_request():
    """未配置 Key：硅基流动不发起任何请求，query 直接返回 None。"""
    tiku = _build(SiliconFlow, siliconflow_key="")
    assert tiku.unavailable is True, "未配置 Key 时应判定为不可用"
    assert not tiku.DISABLE, "不可用不等于题库停用：章节仍要随机作答并提交"

    calls = []
    origin_post = answer.requests.post
    answer.requests.post = lambda *a, **k: calls.append(a) or None
    try:
        assert tiku.query(_question()) is None
        assert tiku.query(_question("ABC")) is None
    finally:
        answer.requests.post = origin_post
    assert not calls, f"未配置 Key 时不应发起任何请求，实际 {len(calls)} 次"


def test_ai_requires_key_unless_ignore_option_on():
    """AI 端点：默认必须有 Key；勾选「忽略 API Key」后才允许留空。"""
    strict = _build(AI, siliconflow_key="")
    assert strict.unavailable is True, "未勾选忽略 Key 时 AI 也不应放行"
    assert _build(AI, siliconflow_key="sk-x").unavailable is False

    ignored = _build(AI, siliconflow_key="", ignore_api_key="true")
    assert ignored.unavailable is False, "勾选忽略后应允许免鉴权访问自建端点"


def test_ensure_available_checks_only_once():
    """任务开始时只检查一次连通性；失败后整轮不再请求大模型。"""
    tiku = _CountedTiku(conn_ok=True)
    tiku.config_set(dict(BASE_CONF))
    tiku.init_tiku()
    assert tiku.ensure_available() is True
    assert tiku.ensure_available() is True
    assert tiku.check_calls == 1, f"连通性检查应只做一次，实际 {tiku.check_calls} 次"

    down = _CountedTiku(conn_ok=False)
    down.config_set(dict(BASE_CONF))
    down.init_tiku()
    assert down.ensure_available() is False
    assert down.ensure_available() is False
    assert down.check_calls == 1, f"失败后不应反复重试，实际 {down.check_calls} 次"
    assert down.unavailable is True

    # 停用后：既不再跑连通性检查，也不再调用 _query
    for i in range(3):
        assert down.query(_question(f"Q{i}")) is None
    assert down.check_calls == 1
    assert down.query_calls == 0, f"停用后不应再请求大模型，实际 {down.query_calls} 次"


def test_consecutive_failures_disable_llm():
    """连续失败达阈值后自动停用，避免每道题都去打不可用的端点。"""
    tiku = _CountedTiku(conn_ok=True, query_raises=True)
    tiku.config_set(dict(BASE_CONF))
    tiku.init_tiku()
    assert tiku.ensure_available() is True

    raised = 0
    for i in range(tiku.MAX_CONSECUTIVE_FAILURES):
        assert tiku.query(_question(f"Q{i}")) is None
        raised += 1
    assert tiku.unavailable is True, "连续失败达阈值后应停用"
    before = tiku.query_calls
    assert tiku.query(_question("Q-last")) is None
    assert tiku.query_calls == before, "停用后不应再请求大模型"
    assert raised == tiku.MAX_CONSECUTIVE_FAILURES


def test_ai_query_parses_json_answer():
    """AI._query 正常路径：解析 {"Answer": [...]}，且 Markdown 代码块包装也能剥离。"""

    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _Msg(content)

    class _Completions:
        content = '```json\n{"Answer": ["2"]}\n```'
        last_kwargs = None

        def create(self, **kwargs):
            _Completions.last_kwargs = kwargs
            return type("R", (), {"choices": [_Choice(_Completions.content)]})()

    class _Client:
        def __init__(self):
            self.chat = type("C", (), {"completions": _Completions()})()

    tiku = _build(AI, siliconflow_key="sk-test")
    origin = answer._openai_client
    answer._openai_client = lambda *a, **k: _Client()
    try:
        got = tiku._query(_question())
        assert got == "2", f"解析结果异常: {got!r}"
        assert getattr(_Completions, "last_kwargs", None) is not None, "请求参数未透传"

        _Completions.content = '{"Answer": ["2", "3"]}'
        multi = tiku._query({"title": "哪些是质数", "type": "multiple", "options": "A. 2\nB. 4"})
        assert multi == "2\n3", f"多选答案拼接异常: {multi!r}"
    finally:
        answer._openai_client = origin


def test_ai_query_handles_request_exception():
    """端点不可用时 _query 必须吞掉异常返回 None，不能把异常抛进刷课流程。"""
    tiku = _build(AI, siliconflow_key="sk-test")

    def _boom(*a, **k):
        raise RuntimeError("模拟连接失败")

    origin = answer._openai_client
    answer._openai_client = _boom
    try:
        assert tiku._query(_question()) is None
    finally:
        answer._openai_client = origin


def test_real_ai_none_failures_disable_llm():
    """真实 AI._query 会吞请求异常并返回 None；基类仍须累计失败并停用。"""
    _patch_cache()
    tiku = _build(AI, siliconflow_key="sk-test", min_interval_seconds="0")
    calls = []

    def _boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("模拟端点不可达")

    origin = answer._openai_client
    answer._openai_client = _boom
    try:
        for i in range(tiku.MAX_CONSECUTIVE_FAILURES):
            assert tiku.query(_question(f"真实AI失败-{i}")) is None
    finally:
        answer._openai_client = origin
    assert tiku.unavailable is True, "AI._query 返回 None 时也应累计连续失败"
    assert len(calls) == tiku.MAX_CONSECUTIVE_FAILURES, calls


def test_llm_interval_accepts_fraction():
    """配置校验允许浮点秒数，三种大模型都必须接受 0.5。"""
    ai = _build(AI, siliconflow_key="sk-ai", min_interval_seconds="0.5")
    ds = _build(DeepSeek, deepseek_key="sk-ds", min_interval_seconds="0.5")
    sf = _build(SiliconFlow, siliconflow_key="sk-sf", min_interval_seconds="0.5")
    assert ai.min_interval_seconds == 0.5
    assert ds.min_interval_seconds == 0.5
    assert sf.min_interval == 0.5


def test_non_llm_provider_unaffected():
    """非大模型题库（未覆写检查）保持原行为：ensure_available 恒为 True 且不发额外请求。"""
    tiku = Tiku()
    tiku.config_set(dict(BASE_CONF))
    assert tiku.ensure_available() is True
    assert tiku.unavailable is False
    assert tiku.check_llm_connection() is True


# --------------------------------------------------------------------------- #
# 多提供方：DeepSeek 独立配置键 / 地址归一化 / LIKE 开关解析
# --------------------------------------------------------------------------- #
def test_deepseek_uses_own_config_keys():
    """provider=DeepSeek 读取 deepseek_* 键，忽略 AI 的 endpoint/key/model。"""
    tiku = _build(DeepSeek, deepseek_key="sk-ds", deepseek_model="deepseek-chat",
                  deepseek_endpoint="https://api.deepseek.com/v1",
                  endpoint="https://api.openai.com/v1", key="sk-openai")
    assert tiku.unavailable is False, "填了 deepseek_key 就不该判不可用"
    assert tiku.key == "sk-ds", f"Key 应取 deepseek_key，实际 {tiku.key!r}"
    assert tiku.endpoint == "https://api.deepseek.com/v1", tiku.endpoint
    assert tiku.model == "deepseek-chat", tiku.model


def test_deepseek_requires_key():
    """DeepSeek 不支持「忽略 API Key」（官方必须鉴权）。"""
    tiku = _build(DeepSeek, deepseek_key="", ignore_api_key="true")
    assert tiku.unavailable is True, "未填 Key 时应停用（不能靠 ignore_api_key 放行）"


def test_llm_endpoint_accepts_full_url():
    """接口地址误填成完整 .../chat/completions 时自动去掉后缀，避免 404。"""
    tiku = _build(AI, siliconflow_key="sk-x",
                  endpoint="https://api.deepseek.com/v1/chat/completions")
    assert tiku.endpoint == "https://api.deepseek.com/v1", tiku.endpoint

    ds = _build(DeepSeek, deepseek_key="sk-ds",
                deepseek_endpoint="https://api.deepseek.com/chat/completions")
    assert ds.endpoint == "https://api.deepseek.com", ds.endpoint


def test_like_switches_parse_text_bool():
    """图形界面写回的是 true/false 文本：likeapi_* 必须按文本解析，不能 bool("false")=True。"""
    tiku = TikuLike()
    tiku.config_set(dict(BASE_CONF, provider="TikuLike", tokens="tok-a",
                         likeapi_search="false", likeapi_vision="true",
                         likeapi_retry="false", likeapi_retry_times="5"))
    tiku.load_config()
    assert tiku._search is False and tiku._vision is True and tiku._retry is False
    assert tiku._retry_times == 5, tiku._retry_times

    tiku.config_set(dict(BASE_CONF, provider="TikuLike", tokens="tok-a",
                         likeapi_search="true", likeapi_retry="true"))
    tiku.load_config()
    assert tiku._search is True and tiku._retry is True


# --------------------------------------------------------------------------- #
# 连通性测试（界面「测试连通性」按钮的后端）
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, status=200, payload=None, text='{"ok":1}'):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeChat:
    def __init__(self, content, capture):
        self._content = content
        self._capture = capture

    def create(self, **kwargs):
        self._capture.update(kwargs)
        msg = type("Msg", (), {"content": self._content})()
        choice = type("Choice", (), {"message": msg})()
        return type("Completion", (), {"choices": [choice]})()


def _fake_client_factory(capture, content="2"):
    def _factory(base_url, api_key, proxy=None, max_retries=None):
        capture.update(base_url=base_url, api_key=api_key, proxy=proxy, max_retries=max_retries)
        return type("Client", (), {
            "chat": type("ChatNS", (), {"completions": _FakeChat(content, capture)})()
        })()
    return _factory


def test_conn_test_disabled_provider():
    """provider 留空 → 直接说明题库停用，不发起任何请求。"""
    ok, msg = answer.test_tiku_connection({"provider": ""})
    assert ok is False and "停用" in msg, msg
    ok, msg = answer.test_tiku_connection({"provider": "NotAProvider"})
    assert ok is False and "未知" in msg, msg


def test_conn_test_ai_without_key():
    """未填 Key：不发请求，直接给出可读提示。"""
    calls = []
    origin = answer._openai_client
    answer._openai_client = lambda *a, **k: calls.append(a)
    try:
        ok, msg = answer.test_tiku_connection(
            {"provider": "AI", "endpoint": "https://api.openai.com/v1", "model": "gpt-3.5-turbo"})
    finally:
        answer._openai_client = origin
    assert ok is False and "API Key" in msg, msg
    assert not calls, "未填 Key 时不应构造客户端"


def test_conn_test_ai_ok():
    """填全后能拿到回复即判定连通，且探测不复用刷课时的重试（max_retries=0）。"""
    capture = {}
    origin = answer._openai_client
    answer._openai_client = _fake_client_factory(capture, content="2")
    try:
        ok, msg = answer.test_tiku_connection({
            "provider": "AI", "endpoint": "https://api.openai.com/v1/chat/completions",
            "key": "sk-x", "model": "gpt-3.5-turbo"})
    finally:
        answer._openai_client = origin
    assert ok is True, msg
    assert "2" in msg, msg
    assert capture["base_url"] == "https://api.openai.com/v1", capture
    assert capture["api_key"] == "sk-x", capture
    assert capture["max_retries"] == 0, "探测应关闭 SDK 自动重试，避免按钮长时间无响应"
    assert capture["model"] == "gpt-3.5-turbo", capture


def test_conn_test_ai_uses_ignore_key_option():
    """AI 勾选「忽略 API Key」后允许免鉴权端点；DeepSeek 不吃这个开关。"""
    origin = answer._openai_client
    answer._openai_client = _fake_client_factory({}, content="2")
    try:
        ok, _msg = answer.test_tiku_connection({
            "provider": "AI", "endpoint": "http://127.0.0.1:11434/v1",
            "model": "qwen", "ignore_api_key": "true"})
        assert ok is True, "勾选忽略 Key 后自建端点应可测试"
        ok, msg = answer.test_tiku_connection({
            "provider": "DeepSeek", "deepseek_key": "", "ignore_api_key": "true"})
        assert ok is False and "API Key" in msg, msg
    finally:
        answer._openai_client = origin


def test_conn_test_deepseek_uses_own_keys():
    """DeepSeek 探测读 deepseek_* 键，不受 AI 的 endpoint/model 影响。"""
    capture = {}
    origin = answer._openai_client
    answer._openai_client = _fake_client_factory(capture, content="2")
    try:
        ok, _msg = answer.test_tiku_connection({
            "provider": "DeepSeek", "deepseek_endpoint": "https://api.deepseek.com/v1",
            "deepseek_key": "sk-ds", "deepseek_model": "deepseek-chat",
            "endpoint": "https://api.openai.com/v1", "key": "sk-openai", "model": "gpt-4o"})
    finally:
        answer._openai_client = origin
    assert ok is True
    assert capture["base_url"] == "https://api.deepseek.com/v1", capture
    assert capture["api_key"] == "sk-ds", capture
    assert capture["model"] == "deepseek-chat", capture


def test_conn_test_yanxi_and_adapter():
    """言溪 / TikuAdapter 走 requests：成功判连通，异常给可读提示。"""
    origin_get, origin_post = answer.requests.get, answer.requests.post
    try:
        answer.requests.get = lambda *a, **k: _FakeResp(
            200, {"code": 1, "data": {"answer": "2", "times": 88}})
        ok, msg = answer.test_tiku_connection({"provider": "TikuYanxi", "tokens": "t1,t2"})
        assert ok is True and "88" in msg, msg

        answer.requests.get = lambda *a, **k: _FakeResp(
            200, {"code": 0, "message": "TOKEN无效"})
        ok, msg = answer.test_tiku_connection({"provider": "TikuYanxi", "tokens": "t1"})
        assert ok is False and "TOKEN无效" in msg, msg

        def _boom(*a, **k):
            raise ConnectionError("模拟网络不可达")

        answer.requests.get = _boom
        ok, msg = answer.test_tiku_connection({"provider": "TikuYanxi", "tokens": "t1"})
        assert ok is False and "请求失败" in msg, msg

        answer.requests.post = lambda *a, **k: _FakeResp(200, {"answer": {"bestAnswer": []}})
        ok, msg = answer.test_tiku_connection(
            {"provider": "TikuAdapter", "url": "http://127.0.0.1:5000/api/search"})
        assert ok is True, msg

        ok, msg = answer.test_tiku_connection({"provider": "TikuAdapter", "url": ""})
        assert ok is False and "服务地址" in msg, msg

        # LIKE：余额接口 200 且余额可解析 → 连通并报剩余次数；401 → TOKEN 失效
        answer.requests.get = lambda *a, **k: _FakeResp(200, {"balance": 12})
        ok, msg = answer.test_tiku_connection({"provider": "TikuLike", "tokens": "t1,t2"})
        assert ok is True and "12" in msg, msg

        answer.requests.get = lambda *a, **k: _FakeResp(401, {}, text="unauthorized")
        ok, msg = answer.test_tiku_connection({"provider": "TikuLike", "tokens": "t1"})
        assert ok is False and "TOKEN" in msg, msg

        ok, msg = answer.test_tiku_connection({"provider": "TikuLike", "tokens": ""})
        assert ok is False and "TOKEN" in msg, msg
    finally:
        answer.requests.get, answer.requests.post = origin_get, origin_post


def test_conn_test_does_not_touch_global_state():
    """探测不调用 init_tiku：不会重建本地题库单例，也不写配置。"""
    import api.local_bank as local_bank
    before = getattr(local_bank, "_BANK", None)
    origin = answer._openai_client
    answer._openai_client = _fake_client_factory({}, content="2")
    try:
        answer.test_tiku_connection({"provider": "AI", "endpoint": "https://x/v1",
                                     "key": "sk-x", "model": "m"})
    finally:
        answer._openai_client = origin
    assert getattr(local_bank, "_BANK", None) is before, \
        "连通性测试不应调用 init_tiku（会重建/改写本地题库单例）"


if __name__ == "__main__":
    _patch_cache()
    cases = [
        test_siliconflow_without_key_sends_no_request,
        test_ai_requires_key_unless_ignore_option_on,
        test_ensure_available_checks_only_once,
        test_consecutive_failures_disable_llm,
        test_ai_query_parses_json_answer,
        test_ai_query_handles_request_exception,
        test_real_ai_none_failures_disable_llm,
        test_llm_interval_accepts_fraction,
        test_non_llm_provider_unaffected,
        test_deepseek_uses_own_config_keys,
        test_deepseek_requires_key,
        test_llm_endpoint_accepts_full_url,
        test_like_switches_parse_text_bool,
        test_conn_test_disabled_provider,
        test_conn_test_ai_without_key,
        test_conn_test_ai_ok,
        test_conn_test_ai_uses_ignore_key_option,
        test_conn_test_deepseek_uses_own_keys,
        test_conn_test_yanxi_and_adapter,
        test_conn_test_does_not_touch_global_state,
    ]
    failed = []
    for case in cases:
        try:
            case()
            print(f"PASS  {case.__name__}")
        except AssertionError as exc:
            failed.append(case.__name__)
            print(f"FAIL  {case.__name__}: {exc}")
            annotate_exc(f"test_llm_fallback.py::{case.__name__}")
        except Exception:  # noqa: BLE001
            failed.append(case.__name__)
            annotate_exc(f"test_llm_fallback.py::{case.__name__}")
    if failed:
        annotate("test_llm_fallback.py", "失败用例: " + ", ".join(failed))
        sys.exit(1)
    sys.exit(0)
