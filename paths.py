# -*- coding: utf-8 -*-
"""运行时路径解析：所有运行期文件都落在「程序目录」下的固定位置。

    程序目录 /
    ├── SuperStar.exe     主程序（文件名不带版本号）
    ├── runtime\\          程序运行时文件（Python 与依赖库，exe 启动必需）
    ├── config_win.ini           默认配置（随包自带，升级时被覆盖，用户不要改）
    ├── config_user.ini          用户配置（首次启动生成，升级不覆盖）
    ├── chaoxing.log            运行日志（随包自带的空文件，追加写入）
    └── progress_cache/         进度缓存 / 账号 / 题库答案缓存 / cookies（随包自带的空目录）

这样做的目的：首次运行不会在 exe 旁边「突然多出」新文件——凡是要写的东西，
压缩包里一开始就有对应位置。使用注意（不能只拷 exe）写在随包的使用说明.txt。

注意：打包态为 onedir（exe + runtime/ 平铺同级），``sys.executable`` 仍指向
真实的 exe 路径，因此本模块的「程序目录」= exe 所在目录，与 onefile 时代一致。

本模块只解析路径，不做任何校验/告警。

开发态（源码目录）行为不变：程序目录 = 项目根目录。
"""
import os
import sys

CONFIG_NAME = "config_win.ini"
USER_CONFIG_NAME = "config_user.ini"
LOG_NAME = "chaoxing.log"
DATA_DIRNAME = "progress_cache"


def app_dir() -> str:
    """程序目录：打包态为 exe 所在目录，开发态为项目根目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def config_path() -> str:
    """默认配置文件（随包自带，升级时会被覆盖，用户不要改）。"""
    return os.path.join(app_dir(), CONFIG_NAME)


def user_config_path() -> str:
    """用户配置文件（首次启动由默认配置生成，升级不覆盖）。"""
    return os.path.join(app_dir(), USER_CONFIG_NAME)


def log_path() -> str:
    """运行日志文件（随包自带的空文件，loguru 追加写入）。"""
    return os.path.join(app_dir(), LOG_NAME)


def data_dir() -> str:
    """运行期数据目录（进度缓存 / 账号 / cookies 等）。"""
    return os.path.join(app_dir(), DATA_DIRNAME)


def data_path(*parts: str) -> str:
    """数据目录下的文件路径（不创建目录，避免 import 阶段产生副作用）。"""
    return os.path.join(data_dir(), *parts)


def cookies_path() -> str:
    return data_path("cookies.txt")


def ensure_data_dir() -> str:
    """确保数据目录存在（用户误删时兜底重建，不报错）。"""
    path = data_dir()
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return path
