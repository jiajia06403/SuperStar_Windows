# -*- coding: utf-8 -*-
# layer: core  # core=默认必跑 / gui=真窗口 / slow=仅 --all
"""通知 URL 脱敏测试（凭据泄露回归）。

通知 URL 往往「本身就是凭据」：Server酱 的 sendkey、Telegram 的 bot token 都在路径里。
日志会落盘 chaoxing.log 并回显 GUI 面板，因此日志里只能出现脱敏形式。

覆盖：
  - mask_url 对四种 provider 的脱敏结果（保留 scheme+host，凭据只留末 4 位）；
  - 四个 provider 初始化日志不含完整 key/token；
  - requests 异常串里回显的 URL 也被清洗；
  - 落盘到日志文件（模拟 chaoxing.log）后同样不含 sendkey。
"""
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402
from loguru import logger  # noqa: E402

from api.notification import (  # noqa: E402
    Bark, Qmsg, ServerChan, Telegram, mask_text, mask_url,
)

# 四个 provider 各自的「URL + 里面的凭据」
CASES = {
    "ServerChan": ("https://sctapi.ftqq.com/SCT1234567890abcdef.send",
                   "SCT1234567890abcdef"),
    "Qmsg": ("https://qmsg.zendee.cn/send/1234abcd-1234-abcd-1234-abcdef123456",
             "1234abcd-1234-abcd-1234-abcdef123456"),
    "Bark": ("https://api.day.app/XXXXXXXXXXXXXXXXXXXXXX",
             "XXXXXXXXXXXXXXXXXXXXXX"),
    "Telegram": ("https://api.telegram.org/bot987654321:AAH-random-token-xyz/sendMessage",
                 "bot987654321:AAH-random-token-xyz"),
}

# provider -> 初始化所需的额外配置
EXTRA_CONF = {"Telegram": {"tg_chat_id": "-1001234567890"}}


def _capture(fn):
    """收集 fn 执行期间产生的日志文本。"""
    lines = []
    sink = logger.add(lines.append, level="TRACE", format="{message}")
    try:
        fn()
    finally:
        logger.remove(sink)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# mask_url 本身
# --------------------------------------------------------------------------- #
def test_mask_url_keeps_host_and_tail_only():
    for name, (url, secret) in CASES.items():
        masked = mask_url(url)
        assert secret not in masked, f"{name} 泄露了完整凭据: {masked}"
        assert url not in masked, f"{name} 泄露了完整 URL: {masked}"
        # 保留 scheme+host，便于确认打到哪个服务商
        host = url.split("//", 1)[1].split("/", 1)[0]
        assert host in masked, f"{name} 丢了 host: {masked}"
        # 只留末 4 位，够用户辨认是哪个 key，又不足以还原
        assert secret[-4:] in masked, f"{name} 没保留末 4 位: {masked}"
        assert "***" in masked, f"{name} 没打码: {masked}"


def test_mask_url_edge_cases():
    assert mask_url("") == ""
    assert mask_url(None) == ""
    # 短 URL / 非 URL 也不得原样吐出
    assert mask_url("abc") == "***"
    assert mask_url("not-a-url") == "***a-url" or "not-a-url" not in mask_url("not-a-url")
    # 带 userinfo 的 host 同样脱敏
    masked = mask_url("https://user:pass@example.com/hook")
    assert "pass" not in masked, masked
    assert "example.com" in masked, masked


def test_mask_text_scrubs_exception():
    """requests 异常串会回显完整 URL（含 sendkey），必须被清洗。"""
    url, secret = CASES["ServerChan"]
    err = (f"HTTPSConnectionPool(host='sctapi.ftqq.com', port=443): "
           f"Max retries exceeded with url: /{secret}.send (Caused by ...)")
    cleaned = mask_text(err, url)
    assert secret not in cleaned, cleaned
    assert "sctapi.ftqq.com" in cleaned, cleaned


# --------------------------------------------------------------------------- #
# 初始化日志
# --------------------------------------------------------------------------- #
def test_init_logs_leak_nothing():
    for name, (url, secret) in CASES.items():
        cls = {"ServerChan": ServerChan, "Qmsg": Qmsg,
               "Bark": Bark, "Telegram": Telegram}[name]
        conf = {"provider": name, "url": url}
        conf.update(EXTRA_CONF.get(name, {}))

        service = cls()
        service.config_set(conf)
        text = _capture(service.init_notification)

        assert not service.disabled, f"{name} 未初始化成功"
        assert secret not in text, f"{name} 初始化日志泄露凭据: {text}"
        assert url not in text, f"{name} 初始化日志泄露 URL: {text}"
        # 信息量要保住：看得出启用了哪个 provider
        assert name in text or cls.__name__ in text or "通知服务" in text, text


# --------------------------------------------------------------------------- #
# 发送失败日志（异常串回显 URL）
# --------------------------------------------------------------------------- #
def test_send_failure_log_scrubbed():
    for name, (url, secret) in CASES.items():
        cls = {"ServerChan": ServerChan, "Qmsg": Qmsg,
               "Bark": Bark, "Telegram": Telegram}[name]
        service = cls()
        service.config_set({"provider": name, "url": url, **EXTRA_CONF.get(name, {})})
        service.init_notification()

        def boom(*_args, **_kwargs):
            raise requests.RequestException(
                f"HTTPSConnectionPool: Max retries exceeded with url: {url}")

        with mock.patch.object(requests, "post", boom):
            text = _capture(lambda: service.send("测试消息"))

        assert "失败" in text, text
        assert secret not in text, f"{name} 失败日志泄露凭据: {text}"
        assert url not in text, f"{name} 失败日志泄露 URL: {text}"


# --------------------------------------------------------------------------- #
# 落盘：模拟 chaoxing.log
# --------------------------------------------------------------------------- #
def test_log_file_has_no_sendkey():
    url, secret = CASES["ServerChan"]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "chaoxing.log")
        sink = logger.add(path, level="TRACE", format="{message}")
        try:
            service = ServerChan()
            service.config_set({"provider": "ServerChan", "url": url})
            service.init_notification()
        finally:
            logger.remove(sink)

        content = open(path, encoding="utf-8").read()
        assert "Server酱" in content, content       # 仍看得出启用了谁
        assert secret not in content, f"日志文件泄露 sendkey: {content}"
        assert "sctapi.ftqq.com" in content, content  # host 保留，便于排查


if __name__ == "__main__":
    cases = [
        test_mask_url_keeps_host_and_tail_only,
        test_mask_url_edge_cases,
        test_mask_text_scrubs_exception,
        test_init_logs_leak_nothing,
        test_send_failure_log_scrubbed,
        test_log_file_has_no_sendkey,
    ]
    failed = []
    for case in cases:
        try:
            case()
            print(f"PASS  {case.__name__}")
        except AssertionError as exc:
            failed.append(case.__name__)
            print(f"FAIL  {case.__name__}: {exc}")
        except Exception:  # noqa: BLE001
            failed.append(case.__name__)
            print(f"FAIL  {case.__name__}: 见上方 traceback")
    sys.exit(1 if failed else 0)
