# -*- coding: utf-8 -*-
try:  # 发行包：cookies 落在程序目录的 progress_cache/ 内，不污染 exe 目录
    from paths import cookies_path as _cookies_path
    _COOKIES_PATH = _cookies_path()
except Exception:  # noqa: BLE001  api/ 单独使用时退化为当前目录
    _COOKIES_PATH = "cookies.txt"


class GlobalConst:
    AESKey = "u2oh6Vu^HWe4_AES"
    COOKIES_PATH = _COOKIES_PATH

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Chromium";v="118", "Google Chrome";v="118", "Not=A?Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"'
    }
    VIDEO_HEADERS = {
        "Referer": "https://mooc1.chaoxing.com/ananas/modules/video/index.html?v=2025-0725-1842",
    }
    AUDIO_HEADERS = {
        "Referer": "https://mooc1.chaoxing.com/ananas/modules/audio/index_new.html?v=2025-0725-1842",
    }

    THRESHOLD = 1
