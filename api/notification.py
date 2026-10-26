"""
通知服务模块，用于向外部服务发送通知消息。
支持多种通知服务，如ServerChan、Qmsg和Bark。
"""

import configparser
import re
from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

import requests

from api.logger import logger


# --------------------------------------------------------------------------- #
# URL 脱敏
#
# 通知 URL 常常「本身就是凭据」：Server酱 的 sendkey 在路径里，Telegram 的 bot token
# 也在路径里。日志既落盘到程序目录的 chaoxing.log，又回显到 GUI 日志面板，用户截图
# 分享即泄露，所以写日志前必须先脱敏 —— 只保留 scheme+host 与凭据末 4 位。
# --------------------------------------------------------------------------- #
_MASK = "***"
_KEEP = 4
# 凭据样的连续串；长度门槛取 12 是为了放过 sendMessage 这类普通端点名
_CREDENTIAL_RE = re.compile(r"[A-Za-z0-9_:\-]{12,}")


def _mask_text(text: str, keep: int = _KEEP) -> str:
    """短串整体打码，长串只留末 keep 位。"""
    text = text or ""
    if len(text) <= keep:
        return "*" * len(text)
    return _MASK + text[-keep:]


def _split_url(url: str) -> Tuple[str, str]:
    """拆成 (scheme://host, host 之后的剩余部分)；非标准 URL 时 base 为空串。"""
    raw = (url or "").strip()
    if not raw:
        return "", ""
    parts = urlsplit(raw)
    if not (parts.scheme and parts.netloc):
        return "", raw
    prefix = f"{parts.scheme}://{parts.netloc}"
    netloc = parts.netloc
    if "@" in netloc:  # user:pass@host 形式的凭据同样要挡掉
        netloc = f"{_MASK}@{netloc.rpartition('@')[2]}"
    return f"{parts.scheme}://{netloc}", raw[len(prefix):]


def mask_url(url: str, keep: int = _KEEP) -> str:
    """把可能含凭据的通知 URL 脱敏，只留 scheme+host 与凭据末几位。

    https://sctapi.ftqq.com/SCT1234567890abcdef.send
        -> https://sctapi.ftqq.com/***cdef.send
    https://api.telegram.org/bot123456789:AAH-random-token-xyz/sendMessage
        -> https://api.telegram.org/***-xyz/sendMessage
    """
    base, tail = _split_url(url)
    if not tail:
        return base

    hits = list(_CREDENTIAL_RE.finditer(tail))
    if not hits:  # 没有长串可认，整段按文本打码
        return base + _mask_text(tail, keep)

    chunks, cursor = [], 0
    for hit in hits:
        chunks.append(tail[cursor:hit.start()])
        chunks.append(_MASK + hit.group()[-keep:])
        cursor = hit.end()
    chunks.append(tail[cursor:])
    return base + "".join(chunks)


def mask_text(text, *urls: str) -> str:
    """把文本（典型是 requests 异常串，里面会回显完整 URL）里的凭据换成脱敏形式。

    Args:
        text: 待清洗的文本，一般是异常对象或错误消息
        urls: 可能夹带进文本的完整 URL（通常是 self.url）
    """
    out = "" if text is None else str(text)
    for url in urls:
        raw = (url or "").strip()
        if not raw:
            continue
        _base, tail = _split_url(raw)
        for needle in (raw, tail):
            if len(needle) <= _KEEP:
                continue
            if needle in out:
                out = out.replace(needle, mask_url(raw) if needle == raw else _mask_text(needle))
        for hit in _CREDENTIAL_RE.finditer(tail):
            secret = hit.group()
            if secret in out:
                out = out.replace(secret, _MASK + secret[-_KEEP:])
    return out


class NotificationService(ABC):
    """
    通知服务基类，定义通知服务的公共接口和实现。
    所有具体的通知服务类应继承此类并实现必要的方法。
    """

    CONFIG_PATH = ""  # 运行时由 config_store.user_path() 解析（用户配置，默认配置只读）

    def __init__(self):
        """初始化通知服务"""
        self.name = self.__class__.__name__
        self.url = ""
        self.tg_chat_id = ""
        self._conf = None
        self.disabled = False

    def config_set(self, config: Dict[str, str]) -> None:
        """
        设置通知服务的配置
        
        Args:
            config: 包含配置参数的字典
        """
        self._conf = config

    def _load_config_from_file(self) -> Optional[Dict[str, str]]:
        """
        从配置文件中加载通知服务的配置
        
        Returns:
            成功返回配置字典，失败返回None
        """
        try:
            from config_store import user_path
            path = self.CONFIG_PATH or user_path()
            config = configparser.ConfigParser()
            config.read(path, encoding="utf8")
            return config['notification']
        except (KeyError, FileNotFoundError):
            logger.info("未找到notification配置，已忽略外部通知功能")
            self.disabled = True
            return None

    def init_notification(self) -> None:
        """初始化通知服务，加载配置并进行必要的设置"""
        if not self._conf:
            self._conf = self._load_config_from_file()

        if not self.disabled and self._conf:
            self._init_service()

    @abstractmethod
    def _init_service(self) -> None:
        """
        初始化特定的通知服务，由子类实现
        """
        pass

    @abstractmethod
    def _send(self, message: str) -> None:
        """
        发送通知消息，由子类实现
        
        Args:
            message: 要发送的消息内容
        """
        pass

    def send(self, message: str) -> None:
        """
        发送通知消息的公共接口
        
        Args:
            message: 要发送的消息内容
        """
        if not self.disabled:
            self._send(message)


class NotificationFactory:
    """
    通知服务工厂类，用于创建和获取通知服务实例
    """

    @staticmethod
    def create_service(config: Optional[Dict[str, str]] = None) -> NotificationService:
        """
        根据配置创建通知服务实例
        
        Args:
            config: 通知服务的配置，如果为None则从配置文件加载
            
        Returns:
            通知服务实例
        """
        service = DefaultNotification()

        if config:
            service.config_set(config)

        # 尝试获取具体的通知服务
        service = service.get_notification_from_config()
        service.init_notification()

        return service


class DefaultNotification(NotificationService):
    """
    默认通知服务，当未配置任何通知服务时使用
    """

    def _init_service(self) -> None:
        pass

    def _send(self, message: str) -> None:
        pass

    def get_notification_from_config(self) -> NotificationService:
        """
        根据配置创建具体的通知服务实例
        
        Returns:
            通知服务实例
        """
        if not self._conf:
            self._conf = self._load_config_from_file()

        if self.disabled:
            return self

        try:
            provider_name = self._conf['provider']
            if not provider_name:
                raise KeyError("未指定通知服务提供商")

            # 获取对应的通知服务类
            provider_class = globals().get(provider_name)
            if not provider_class:
                logger.error(f"未找到名为 {provider_name} 的通知服务提供商")
                self.disabled = True
                return self

            # 创建通知服务实例
            service = provider_class()
            service.config_set(self._conf)
            return service

        except KeyError:
            self.disabled = True
            logger.info("未找到外部通知配置，已忽略外部通知功能")
            return self


class ServerChan(NotificationService):
    """
    Server酱通知服务
    """

    def _init_service(self) -> None:
        """初始化Server酱服务"""
        if not self._conf or not self._conf.get('url'):
            self.disabled = True
            logger.info("未找到Server酱url配置，已忽略该通知服务")
            return

        self.url = self._conf['url']
        logger.info(f"已初始化Server酱通知服务，URL: {mask_url(self.url)}")

    def _send(self, message: str) -> None:
        """
        通过Server酱发送通知
        
        Args:
            message: 要发送的消息内容
        """
        params = {
            'text': message,  # 兼容两个版本的Server酱
            'desp': message,
        }
        headers = {
            'Content-Type': 'application/json;charset=utf-8'
        }

        try:
            response = requests.post(self.url, json=params, headers=headers)
            response.raise_for_status()
            result = response.json()
            logger.info(f"Server酱通知发送成功: {result}")
        except requests.RequestException as e:  # 异常串里会回显完整 URL，先脱敏
            logger.error(f"Server酱通知发送失败: {mask_text(e, self.url)}")
        except ValueError as e:
            logger.error(f"Server酱返回数据解析失败: {e}")


class Qmsg(NotificationService):
    """
    Qmsg酱通知服务
    """

    def _init_service(self) -> None:
        """初始化Qmsg酱服务"""
        if not self._conf or not self._conf.get('url'):
            self.disabled = True
            logger.info("未找到Qmsg酱url配置，已忽略该通知服务")
            return

        self.url = self._conf['url']
        logger.info(f"已初始化Qmsg酱通知服务，URL: {mask_url(self.url)}")

    def _send(self, message: str) -> None:
        """
        通过Qmsg酱发送通知
        
        Args:
            message: 要发送的消息内容
        """
        params = {'msg': message}
        headers = {'Content-Type': 'application/json;charset=utf-8'}

        try:
            response = requests.post(self.url, params=params, headers=headers)
            response.raise_for_status()
            result = response.json()
            logger.info(f"Qmsg酱通知发送成功: {result}")
        except requests.RequestException as e:  # 同上：URL 即密钥，不得直出
            logger.error(f"Qmsg酱通知发送失败: {mask_text(e, self.url)}")
        except ValueError as e:
            logger.error(f"Qmsg酱返回数据解析失败: {e}")


class Bark(NotificationService):
    """
    Bark通知服务
    """

    def _init_service(self) -> None:
        """初始化Bark服务"""
        if not self._conf or not self._conf.get('url'):
            self.disabled = True
            logger.info("未找到Bark的url配置，已忽略该通知服务")
            return

        self.url = self._conf['url']
        logger.info(f"已初始化Bark通知服务，URL: {mask_url(self.url)}")

    def _send(self, message: str) -> None:
        """
        通过Bark发送通知
        
        Args:
            message: 要发送的消息内容
        """
        params = {'body': message}

        try:
            response = requests.post(self.url, params=params)
            response.raise_for_status()
            result = response.json()
            logger.info(f"Bark通知发送成功: {result}")
        except requests.RequestException as e:
            logger.error(f"Bark通知发送失败: {mask_text(e, self.url)}")
        except ValueError as e:
            logger.error(f"Bark返回数据解析失败: {e}")

class Telegram(NotificationService):
    """
    通过Telegram发送通知
    """

    def _init_service(self) -> None:
        """初始化Telegram服务"""
        if not self._conf or not self._conf.get('url') or not self._conf.get('tg_chat_id'):
            self.disabled = True
            logger.info("未找到Telegram的url或tg_chat_id配置，已忽略该通知服务")
            return
        self.tg_chat_id = self._conf['tg_chat_id']
        self.url = self._conf['url']
        logger.info(f"已初始化Telegram通知服务，Chat_id: {self.tg_chat_id} "
                    f"URL: {mask_url(self.url)}")

    def _send(self, message: str) -> None:
        """
        通过Telegram发送通知
        
        Args:
            message: 要发送的消息内容
        """
        params = {
            'chat_id': self.tg_chat_id,
            'text': message,
            'parse_mode': 'HTML'
        }

        try:
            response = requests.post(self.url, data=params)
            response.raise_for_status()
            result = response.json()
            if result.get('ok'):
                logger.info(f"Telegram通知发送成功: {result}")
            else:
                logger.error(f"Telegram通知发送失败: {result}")
        except requests.RequestException as e:
            logger.error(f"Telegram通知发送失败: {mask_text(e, self.url)}")
        except ValueError as e:
            logger.error(f"Telegram返回数据解析失败: {e}")

# 为了向后兼容，保留原来的Notification类
Notification = DefaultNotification