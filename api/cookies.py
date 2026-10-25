# -*- coding: utf-8 -*-
import os.path

import requests

from api.config import GlobalConst as gc


def save_cookies(session: requests.Session):
    buffer=""
    with open(gc.COOKIES_PATH, "w") as f:
        for k, v in session.cookies.items():
            buffer += f"{k}={v};"
        buffer = buffer.removesuffix(";")
        f.write(buffer)


def use_cookies() -> dict:
    """读取 cookies 文件；文件缺失、为空或格式不合法时返回空字典（不抛异常）。"""
    if not os.path.exists(gc.COOKIES_PATH):
        return {}

    cookies = {}
    with open(gc.COOKIES_PATH, "r") as f:
        buffer = f.read().strip()
        for item in buffer.split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            k, v = item.split("=", 1)
            if k.strip():
                cookies[k.strip()] = v.strip()

    return cookies
