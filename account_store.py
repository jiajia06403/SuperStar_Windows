# -*- coding: utf-8 -*-
"""账号持久化：把登录过的账号记在本机，省去每次启动重新输入。

存储位置：``<缓存目录>/accounts.json``（默认 ``progress_cache/accounts.json``，已被 .gitignore 排除）。

密码安全：
  - Windows 下用 **DPAPI**（``CryptProtectData``）以当前用户身份加密，密文只能被同一台机器的
    同一 Windows 用户解开；换机器或换用户都无法解密。
  - 其它平台 / DPAPI 不可用时，退化为 base64 混淆并打印警告（不假装它安全）。

文件结构::

    {
      "version": 1,
      "last_uid": "12345678",
      "accounts": {
        "12345678": {"username": "13800000000", "name": "小号",
                     "password": "dpapi:...", "use_cookies": false}
      }
    }

``password`` 为空字符串表示「只记住账号、不记住密码」。
``name`` 是用户自定义的本地名称（界面显示与重名判重用），空串表示未设置，
此时按 :func:`account_display_name` 退回手机号 / uid。
"""
import base64
import ctypes
import json
import sys
from pathlib import Path

from api.logger import logger

_VERSION = 1
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_PREFIX_DPAPI = "dpapi:"
_PREFIX_PLAIN = "plain:"


# --------------------------------------------------------------------------- #
# Windows DPAPI（不依赖 pywin32，纯 ctypes）
# --------------------------------------------------------------------------- #
class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


_dpapi = None          # (crypt32, kernel32) 缓存
_dpapi_failed = False  # 初始化失败后不再重试，避免反复抛异常


def _load_dpapi():
    """加载并配置 crypt32/kernel32 函数原型（64 位下必须设 argtypes，否则句柄会被截断）。"""
    global _dpapi, _dpapi_failed
    if _dpapi is not None:
        return _dpapi
    if _dpapi_failed or sys.platform != "win32":
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        blob_p = ctypes.POINTER(_DataBlob)
        crypt32.CryptProtectData.argtypes = [
            blob_p, ctypes.c_wchar_p, blob_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_uint32, blob_p,
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int
        crypt32.CryptUnprotectData.argtypes = [
            blob_p, ctypes.POINTER(ctypes.c_wchar_p), blob_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_uint32, blob_p,
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        _dpapi = (crypt32, kernel32)
        return _dpapi
    except Exception as e:  # noqa: BLE001
        _dpapi_failed = True
        logger.warning(f"DPAPI 初始化失败，账号密码将退化为混淆存储: {e}")
        return None


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi_protect(plain: bytes):
    libs = _load_dpapi()
    if libs is None:
        return None
    crypt32, kernel32 = libs
    src, _keep = _blob(plain)   # _keep 必须存活到调用结束
    out = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(src), "SuperStar", None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    ):
        logger.warning(f"CryptProtectData 失败: {ctypes.get_last_error()}")
        return None
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _dpapi_unprotect(cipher: bytes):
    libs = _load_dpapi()
    if libs is None:
        return None
    crypt32, kernel32 = libs
    src, _keep = _blob(cipher)
    out = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    ):
        logger.warning(f"CryptUnprotectData 失败: {ctypes.get_last_error()}")
        return None
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


# --------------------------------------------------------------------------- #
# 密码编解码
# --------------------------------------------------------------------------- #
def encode_password(password: str) -> str:
    """把明文密码编码为可落盘的字符串；空密码返回空串（只记住账号）。"""
    if not password:
        return ""
    raw = password.encode("utf8")
    cipher = _dpapi_protect(raw)
    if cipher is not None:
        return _PREFIX_DPAPI + base64.b64encode(cipher).decode("ascii")
    logger.warning("无法使用 DPAPI 加密密码，已退化为 base64 混淆存储")
    return _PREFIX_PLAIN + base64.b64encode(raw).decode("ascii")


def decode_password(token: str) -> str:
    """解析落盘的密码字段；无法解析时返回空串（当作未保存密码）。"""
    if not token:
        return ""
    try:
        if token.startswith(_PREFIX_DPAPI):
            cipher = base64.b64decode(token[len(_PREFIX_DPAPI):])
            plain = _dpapi_unprotect(cipher)
            if plain is None:
                return ""
            return plain.decode("utf8")
        if token.startswith(_PREFIX_PLAIN):
            return base64.b64decode(token[len(_PREFIX_PLAIN):]).decode("utf8")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"账号密码解析失败，已忽略: {e}")
        return ""
    logger.warning("账号密码字段格式未知，已忽略")
    return ""


# --------------------------------------------------------------------------- #
# 账号存储
# --------------------------------------------------------------------------- #
def account_display_name(uid, acc: dict) -> str:
    """账号显示名：优先用户自定义名称，其次手机号，最后 uid。"""
    acc = acc or {}
    name = str(acc.get("name") or "").strip()
    if name:
        return name
    username = str(acc.get("username") or "").strip()
    return username or str(uid or "")


class AccountStore:
    """账号文件读写。任何异常都不抛出，最多退化为空账号表。"""

    def __init__(self, path):
        self.path = Path(path)
        self.last_uid = ""

    # ---- 读 ----
    def load(self) -> dict[str, dict]:
        """返回 ``{uid: {username, name, password, use_cookies}}``（密码已解密）。"""
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf8"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"账号文件解析失败，已忽略: {e}")
            return {}
        if not isinstance(data, dict):
            return {}

        self.last_uid = str(data.get("last_uid") or "")
        raw_accounts = data.get("accounts")
        if not isinstance(raw_accounts, dict):
            return {}

        accounts: dict[str, dict] = {}
        for uid, item in raw_accounts.items():
            if not isinstance(item, dict):
                continue
            accounts[str(uid)] = {
                "username": str(item.get("username") or ""),
                "name": str(item.get("name") or ""),
                "password": decode_password(str(item.get("password") or "")),
                "use_cookies": bool(item.get("use_cookies", False)),
            }
        if self.last_uid not in accounts:
            self.last_uid = ""
        logger.info(f"已载入 {len(accounts)} 个已保存账号")
        return accounts

    # ---- 写 ----
    def save(self, accounts: dict[str, dict], last_uid: str = "") -> None:
        payload = {
            "version": _VERSION,
            "last_uid": str(last_uid or ""),
            "accounts": {
                str(uid): {
                    "username": acc.get("username", ""),
                    "name": str(acc.get("name") or "").strip(),
                    "password": encode_password(acc.get("password", "")),
                    "use_cookies": bool(acc.get("use_cookies", False)),
                }
                for uid, acc in accounts.items()
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf8")
            tmp.replace(self.path)
            self.last_uid = payload["last_uid"]
        except Exception as e:  # noqa: BLE001
            logger.error(f"保存账号失败: {e}")
