# -*- coding: utf-8 -*-
"""配置双层管理：默认配置（随包分发） + 用户配置（本地生成）。

设计目的：升级软件解压覆盖时**不丢用户配置**，且升级后能用**新的默认配置**校验用户配置。

    config_win.ini    默认配置：随压缩包分发，升级时会被覆盖 —— 用户不要改它
    config_user.ini   用户配置：程序首次启动按默认配置生成，升级不覆盖

规则：
  1. 读取：默认配置打底，用户配置逐键覆盖（用户显式留空也算覆盖，可用于停用题库）；
  2. 迁移：每次启动用最新的默认配置比对用户配置
       - 新增键     -> 以默认值补进用户配置
       - 已删除的键 -> 从原段移除，值保留到 [obsolete] 段（不生效、不丢数据）
       - 取值非法   -> 回落为默认值（记入报告，见 sync_with_default 的 fixed）
  3. 写入：只写用户配置，且走**文本级最小改动**，保留用户写的注释；迁移幂等，
     反复启动不会重复追加。

本模块只做配置读写与迁移，不做弹窗；调用方（GUI）负责把报告写进日志。
"""
import configparser
import os
import re
import sys
import tempfile

import paths

DEFAULT_NAME = "config_win.ini"     # 随包分发，勿改（改了下次升级会被覆盖）
USER_NAME = "config_user.ini"       # 用户配置，自动生成
OBSOLETE_SECTION = "obsolete"       # 存放「默认配置已移除」的历史键

USER_HEADER = [
    "; SuperStar 用户配置（首次启动时由同目录的 config_win.ini 自动生成，请勿删除）。",
    "; 本文件的取值优先于 config_win.ini（随包默认配置）。",
    "; 升级软件时解压覆盖 config_win.ini 不会影响本文件；",
    "; 每次启动会用新的默认配置校验本文件：补齐新增项、把已废弃项移到 [obsolete]、修正非法取值。",
    "; 要改配置请改本文件；改 config_win.ini 是无效的（下次升级必被覆盖）。",
]

# 取值校验规则（只对 [tiku] 生效；空值一律视为合法）
TIKU_FLOAT_KEYS = ("cover_rate", "delay", "min_interval_seconds")
TIKU_INT_KEYS = ("work_retry", "likeapi_retry_times")
TIKU_BOOL_KEYS = ("submit", "ignore_api_key", "likeapi_search", "likeapi_vision", "likeapi_retry")
TIKU_PROVIDERS = ("", "TikuYanxi", "TikuLike", "TikuAdapter", "AI", "DeepSeek", "SiliconFlow")

_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
_KV_RE = re.compile(r"^\s*(?P<key>[^#;=\s][^=]*?)\s*=(?P<rest>.*)$")


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def default_path() -> str:
    """随包默认配置（程序目录）。"""
    return paths.config_path()


def user_path() -> str:
    """用户配置（程序目录，自动生成）。"""
    return paths.user_config_path()


def _default_candidates() -> "list[str]":
    """默认配置的查找顺序：程序目录 > 打包内嵌副本 > 源码根。"""
    cands = [paths.config_path()]
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            cands.append(os.path.join(meipass, DEFAULT_NAME))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), DEFAULT_NAME))
    return cands


# --------------------------------------------------------------------------- #
# 基础读写
# --------------------------------------------------------------------------- #
def _parser() -> configparser.ConfigParser:
    # interpolation=None：避免值里的 % 被当成插值语法；strict=False：容忍重复键
    return configparser.ConfigParser(interpolation=None, strict=False)


def _read_parser(path: str) -> configparser.ConfigParser:
    parser = _parser()
    try:
        parser.read(path, encoding="utf8")
    except Exception:  # noqa: BLE001 - 配置损坏时按「空配置」处理，由迁移补齐
        return _parser()
    return parser


def load_default() -> configparser.ConfigParser:
    """随包默认配置；找不到就返回空配置（题库等能力随之停用）。"""
    for path in _default_candidates():
        if os.path.exists(path):
            parser = _read_parser(path)
            if parser.sections():
                return parser
    return _parser()


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf8") as fp:
            return fp.read()
    except Exception:  # noqa: BLE001
        return ""


def _write_text(path: str, text: str) -> None:
    """原子写：先写临时文件再替换，避免写一半崩溃导致配置损坏。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".cfg_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf8", newline="\n") as fp:
            fp.write(text)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def create_user_config() -> bool:
    """用户配置不存在时，按默认配置生成一份（保留注释）。返回是否新建。"""
    path = user_path()
    if os.path.exists(path):
        return False
    src = next((p for p in _default_candidates() if os.path.exists(p)), "")
    body = _read_text(src).strip() if src else ""
    text = "\n".join(USER_HEADER) + "\n\n" + (body + "\n" if body else "")
    _write_text(path, text)
    return True


# --------------------------------------------------------------------------- #
# 文本级最小改动（保留注释）
# --------------------------------------------------------------------------- #
def _section_range(lines: "list[str]", section: str):
    """返回段的行范围 ``(段头下标, 段尾下标)``；不存在返回 None。"""
    start = None
    for i, line in enumerate(lines):
        matched = _SECTION_RE.match(line)
        if not matched:
            continue
        if start is not None:
            return start, i
        if matched.group("name").strip().lower() == section.lower():
            start = i
    return (start, len(lines)) if start is not None else None


def _find_key(lines: "list[str]", start: int, end: int, key: str):
    for i in range(start + 1, end):
        matched = _KV_RE.match(lines[i])
        if matched and matched.group("key").strip().lower() == key.lower():
            return i
    return None


def _kv_line(key: str, value: str) -> str:
    return f"{key} = {value}"


def _set_value(lines: "list[str]", section: str, key: str, value: str) -> None:
    """设置 ``section.key = value``：键存在则改值，否则插入段尾；段不存在则新建段。"""
    found = _section_range(lines, section)
    if found is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        lines.append(_kv_line(key, value))
        return
    start, end = found
    idx = _find_key(lines, start, end, key)
    if idx is not None:
        lines[idx] = _kv_line(key, value)
        return
    pos = end
    while pos - 1 > start and not lines[pos - 1].strip():
        pos -= 1
    lines.insert(pos, _kv_line(key, value))


def _delete_key(lines: "list[str]", section: str, key: str) -> bool:
    found = _section_range(lines, section)
    if found is None:
        return False
    idx = _find_key(lines, *found, key)
    if idx is None:
        return False
    del lines[idx]
    return True


def _has_key(lines: "list[str]", section: str, key: str) -> bool:
    found = _section_range(lines, section)
    return False if found is None else _find_key(lines, *found, key) is not None


# --------------------------------------------------------------------------- #
# 取值校验
# --------------------------------------------------------------------------- #
def _invalid(section: str, key: str, value: str) -> bool:
    """取值是否非法（空值一律合法，用于「留空即停用」）。"""
    if section != "tiku" or not value.strip():
        return False
    value = value.strip()
    if key in TIKU_FLOAT_KEYS:
        try:
            float(value)
        except ValueError:
            return True
        return False
    if key in TIKU_INT_KEYS:
        return not re.fullmatch(r"\d+", value)
    if key in TIKU_BOOL_KEYS:
        return value.lower() not in ("true", "false")
    if key == "provider":
        return value not in TIKU_PROVIDERS
    return False


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def sync_with_default() -> dict:
    """用最新的默认配置校验用户配置，并就地修补。

    返回迁移报告::

        {"created": bool, "added": {段: [键]}, "obsolete": {段: [键]},
         "fixed": {段: {键: (旧值, 新值)}}, "changed": bool}

    迁移幂等：已补过的键、已废弃的键不会重复处理。
    """
    report: dict = {"created": False, "added": {}, "obsolete": {}, "fixed": {}, "changed": False}
    report["created"] = create_user_config()

    default = load_default()
    path = user_path()
    lines = _read_text(path).splitlines()
    user = _read_parser(path)

    # 1) 默认配置新增的键 -> 用默认值补齐
    for section in default.sections():
        for key, value in default.items(section):
            if user.has_option(section, key):
                continue
            _set_value(lines, section, key, value)
            report["added"].setdefault(section, []).append(key)

    # 2) 默认配置已删除的键 -> 移入 [obsolete] 保留（键名记为 原段.键）
    for section in user.sections():
        if section == OBSOLETE_SECTION:
            continue
        for key, value in user.items(section):
            if default.has_option(section, key):
                continue
            legacy = f"{section}.{key}"
            if _has_key(lines, OBSOLETE_SECTION, legacy):
                continue
            _delete_key(lines, section, key)
            _set_value(lines, OBSOLETE_SECTION, legacy, value)
            report["obsolete"].setdefault(section, []).append(key)

    # 3) 取值非法 -> 回落默认值
    for section in user.sections():
        if section == OBSOLETE_SECTION:
            continue
        for key, value in user.items(section):
            if not default.has_option(section, key):
                continue
            if not _invalid(section, key, value):
                continue
            fallback = default.get(section, key)
            _set_value(lines, section, key, fallback)
            report["fixed"].setdefault(section, {})[key] = (value, fallback)

    report["changed"] = bool(report["created"] or report["added"]
                             or report["obsolete"] or report["fixed"])
    if report["changed"]:
        _write_text(path, "\n".join(lines).rstrip("\n") + "\n")
    return report


def load_section(section: str) -> dict:
    """生效配置：默认配置打底 + 用户配置覆盖。"""
    merged: dict = {}
    default = load_default()
    if default.has_section(section):
        merged.update(dict(default[section]))
    user = _read_parser(user_path())
    if user.has_section(section):
        merged.update(dict(user[section]))
    return merged


def save_section(section: str, values: dict) -> None:
    """把一段配置写进用户配置（保留注释；不写默认配置）。"""
    create_user_config()
    lines = _read_text(user_path()).splitlines()
    for key, value in values.items():
        _set_value(lines, section, key, str(value))
    _write_text(user_path(), "\n".join(lines).rstrip("\n") + "\n")
