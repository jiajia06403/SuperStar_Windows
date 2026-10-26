import configparser
import json
import os
import random
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from re import sub
from typing import Optional

import requests
from urllib3 import disable_warnings, exceptions

try:
    # AI 大模型答题属于可选能力：缺失 openai/httpx 时，其余题库与刷课功能照常工作。
    import httpx
    from openai import OpenAI
except ImportError:  # pragma: no cover - 精简发行版不打包该可选依赖
    httpx = None
    OpenAI = None

from api.answer_check import *
from api.logger import logger

# 关闭警告
disable_warnings(exceptions.InsecureRequestWarning)


def _openai_client(base_url: str, api_key: str, proxy: Optional[str] = None):
    """构造 OpenAI 客户端（可选依赖，缺失时给出明确提示）。"""
    if OpenAI is None or httpx is None:
        raise RuntimeError("AI 题库需要 openai 与 httpx 依赖：pip install openai httpx")
    if proxy:
        return OpenAI(http_client=httpx.Client(proxy=proxy), base_url=base_url, api_key=api_key)
    return OpenAI(base_url=base_url, api_key=api_key)

__all__ = ["CacheDAO", "Tiku", "TikuYanxi", "TikuLike", "TikuAdapter", "AI", "DeepSeek",
           "SiliconFlow"]


def _normalize_base_url(raw: str) -> str:
    """OpenAI 客户端要的是 base_url（如 https://api.deepseek.com/v1）。

    用户常把完整地址 .../chat/completions 粘进来，若原样当 base_url 会变成
    .../chat/completions/chat/completions 而 404，这里统一去掉该后缀。
    """
    url = str(raw or "").strip().rstrip("/")
    suffix = "/chat/completions"
    if url.lower().endswith(suffix):
        url = url[: -len(suffix)].rstrip("/")
    return url


def _strip_md_json_wrapper(md_str: str) -> str:
    """去掉大模型输出里可能存在的 Markdown/JSON 代码块包装。"""
    pattern = r'^\s*```(?:json)?\s*(.*?)\s*```\s*$'
    match = re.search(pattern, str(md_str or ""), re.DOTALL)
    return match.group(1).strip() if match else str(md_str or "").strip()


def _default_cache_file() -> str:
    """题库答案缓存：发行包落在 progress_cache/cache.json，不写到 exe 目录顶层。"""
    try:
        from paths import data_path
        return data_path("cache.json")
    except Exception:  # noqa: BLE001  api/ 单独使用时退化为当前目录
        return CacheDAO.DEFAULT_CACHE_FILE

class CacheDAO:
    """
    @Author: SocialSisterYi
    @Reference: https://github.com/SocialSisterYi/xuexiaoyi-to-xuexitong-tampermonkey-proxy
    """
    DEFAULT_CACHE_FILE = "cache.json"

    def __init__(self, file: Optional[str] = None):
        self.cache_file = Path(file or _default_cache_file())
        self._lock = threading.RLock()
        if not self.cache_file.is_file():
            self._write_cache({})

    def _read_cache(self) -> dict:
        # 新增缓存文件读取的异常处理
        try:
            with self._lock:
                if not self.cache_file.is_file():
                    return {}
                try:
                    with self.cache_file.open("r", encoding="utf8") as fp:
                        return json.load(fp)
                except json.JSONDecodeError as e:
                    logger.error(f"缓存文件 JSON 解析失败: {e}, 尝试恢复...")
                    # 尝试从原始二进制中以 utf-8 忽略错误地恢复有效 JSON 段
                    try:
                        raw = self.cache_file.read_bytes()
                        text = raw.decode("utf-8", errors="ignore")
                        start = text.find('{')
                        end = text.rfind('}')
                        if start != -1 and end != -1 and start < end:
                            try:
                                return json.loads(text[start:end+1])
                            except Exception:
                                pass
                    except Exception:
                        pass
                    # 若无法恢复，备份损坏文件并返回空缓存
                    try:
                        bak_name = f"{self.cache_file.name}.bak.{int(time.time())}"
                        bak_path = self.cache_file.with_name(bak_name)
                        shutil.copy2(self.cache_file, bak_path)
                        logger.error(f"缓存文件已损坏，已备份为: {bak_path}，将使用空缓存继续运行")
                    except Exception as ex:
                        logger.error(f"备份损坏缓存失败: {ex}")
                    return {}
                except UnicodeDecodeError as e:
                    logger.error(f"缓存文件编码读取失败: {e}, 采用恢复策略...")
                    try:
                        raw = self.cache_file.read_bytes()
                        text = raw.decode("utf-8", errors="ignore")
                        start = text.find('{')
                        end = text.rfind('}')
                        if start != -1 and end != -1 and start < end:
                            try:
                                return json.loads(text[start:end+1])
                            except Exception:
                                pass
                    except Exception:
                        pass
                    try:
                        bak_name = f"{self.cache_file.name}.bak.{int(time.time())}"
                        bak_path = self.cache_file.with_name(bak_name)
                        shutil.copy2(self.cache_file, bak_path)
                        logger.error(f"缓存文件编码错误，已备份为: {bak_path}，将使用空缓存继续运行")
                    except Exception as ex:
                        logger.error(f"备份损坏缓存失败: {ex}")
                    return {}
        except Exception as e:
            logger.error(f"读取缓存异常: {e}")
            return {}

    def _write_cache(self, data: dict) -> None:
        # 为缓存写入加锁，防止并发写入损坏文件
        try:
            with self._lock:
                parent = self.cache_file.parent
                if not parent.exists():
                    parent.mkdir(parents=True, exist_ok=True)
                # 写入临时文件后原子替换，减少并发写入时的损坏风险
                fd, tmp_path = tempfile.mkstemp(prefix=self.cache_file.name, dir=str(parent))
                try:
                    with os.fdopen(fd, "w", encoding="utf8") as fp:
                        json.dump(data, fp, ensure_ascii=False, indent=4)
                        fp.flush()
                        os.fsync(fp.fileno())
                    os.replace(tmp_path, str(self.cache_file))
                except Exception as e:
                    # 清理临时文件
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass
                    logger.error(f"Failed to write cache atomically: {e}")
        except IOError as e:
            logger.error(f"Failed to write cache: {e}")

    def get_cache(self, question: str) -> Optional[str]:
        data = self._read_cache()
        return data.get(question)

    def add_cache(self, question: str, answer: str) -> None:
        # 为缓存写入加锁，防止并发写入损坏文件
        with self._lock:
            data = self._read_cache()
            data[question] = answer
            self._write_cache(data)


# TODO: 重构此部分代码，将此类改为抽象类，加载题库方法改为静态方法，禁止直接初始化此类
class Tiku:
    # 兜底读取路径：GUI/引擎不传配置时用用户配置 config_user.ini（默认配置 config_win.ini 只读）
    CONFIG_PATH = ""  # 运行时由 config_store.user_path() 解析
    DISABLE = False     # 停用标志
    SUBMIT = False      # 提交标志
    COVER_RATE = 0.8    # 覆盖率
    true_list = []
    false_list = []
    # 大模型类题库标记（AI / DeepSeek / SiliconFlow 置 True）：异常失败会计入「连续失败」，
    # 超阈值后本轮停用。其余题库「搜不到答案」属正常，不受影响。
    LLM_PROVIDER = False
    MAX_CONSECUTIVE_FAILURES = 3
    # 未配置 API Key 时能否放行：默认不行；仅 AI（自建 OpenAI 兼容端点）可由配置显式打开
    ALLOW_EMPTY_KEY = False
    EMPTY_KEY_OPTION = "ignore_api_key"
    def __init__(self) -> None:
        self._name = None
        self._api = None
        self._conf = None
        # ---- 大模型可用性 ----
        # 只在首次答题前连通测试一次；不可用则本轮所有题目直接随机作答，
        # 不再逐题访问大模型（此前是每道题都去请求，端点不通时既慢又刷满错误日志）。
        self._availability_lock = threading.RLock()
        self._availability_checked = False
        self._unavailable_reason = ""
        self._failure_count = 0

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    @property
    def api(self):
        return self._api

    @api.setter
    def api(self, value):
        self._api = value

    @property
    def token(self):
        return self._token

    @token.setter
    def token(self, value):
        self._token = value

    def init_tiku(self):
        # 仅用于题库初始化, 应该在题库载入后作初始化调用, 随后才可以使用题库
        # 尝试根据配置文件设置提交模式
        if not self._conf:
            self.config_set(self._get_conf())
        if not self.DISABLE:
            # 设置提交模式
            self.SUBMIT = True if self._conf['submit'] == 'true' else False
            self.COVER_RATE = float(self._conf['cover_rate'])
            self.true_list = self._conf['true_list'].split(',')
            self.false_list = self._conf['false_list'].split(',')
            # 调用自定义题库初始化
            self._init_tiku()

    def _init_tiku(self):
        # 仅用于题库初始化, 例如配置token, 交由自定义题库完成
        pass

    def config_set(self,config):
        self._conf = config

    def _get_conf(self):
        """
        从用户配置文件查询配置, 如果未能查到, 停用题库
        """
        try:
            from config_store import user_path
            path = self.CONFIG_PATH or user_path()
            config = configparser.ConfigParser()
            config.read(path, encoding="utf8")
            return config['tiku']
        except (KeyError, FileNotFoundError):
            logger.info("未找到tiku配置, 已忽略题库功能")
            self.DISABLE = True
            return None
        
    def query(self,q_info:dict) -> Optional[str]:
        if self.DISABLE:
            return None

        # 预处理, 去除【单选题】这样与标题无关的字段
        logger.debug(f"原始标题：{q_info['title']}")
        q_info['title'] = sub(r'^\d+', '', q_info['title'])
        q_info['title'] = sub(r'（\d+\.\d+分）$', '', q_info['title'])
        logger.debug(f"处理后标题：{q_info['title']}")

        # 先过缓存（本地缓存不消耗任何远程额度，大模型不可用时也应继续命中）
        cache_dao = CacheDAO()
        answer = cache_dao.get_cache(q_info['title'])
        if answer:
            logger.info(f"从缓存中获取答案：{q_info['title']} -> {answer}")
            return answer.strip()

        # 大模型不可用（未配置 Key / 连接检查失败 / 连续失败）：不再发起任何请求，
        # 直接返回 None，由上层随机作答。
        if self._unavailable_reason:
            logger.debug(f"{self.name}不可用，跳过远程查询：{self._unavailable_reason}")
            return None

        try:
            answer = self._query(q_info)
        except Exception as e:  # noqa: BLE001 - 任何异常都不能中断刷课，交给随机作答兜底
            logger.error(f"{self.name}查询异常：{type(e).__name__}: {e}")
            self._record_failure()
            return None

        if answer:
            self._failure_count = 0
            answer = answer.strip()
            logger.info(f"从{self.name}获取答案：{q_info['title']} -> {answer}")
            if check_answer(answer, q_info['type'], self):
                cache_dao.add_cache(q_info['title'], answer)
                return answer
            else:
                logger.info(f"从{self.name}获取到的答案类型与题目类型不符，已舍弃")
                return None

        logger.error(f"从{self.name}获取答案失败：{q_info['title']}")
        self._record_failure()
        return None



    def _query(self, q_info:dict) -> Optional[str]:
        """
        查询接口, 交由自定义题库实现
        """
        pass


    def get_tiku_from_config(self):
        """
        从配置文件加载题库, 这个配置可以是用户提供, 可以是默认配置文件
        """
        if not self._conf:
            # 尝试从默认配置文件加载
            self.config_set(self._get_conf())
        if self.DISABLE:
            return self
        try:
            cls_name = self._conf['provider']
            if not cls_name:
                raise KeyError
        except KeyError:
            self.DISABLE = True
            logger.error("未找到题库配置, 已忽略题库功能")
            return self
        # FIXME: Implement using StrEnum instead. This is not only buggy but also not safe
        new_cls = globals()[cls_name]()
        new_cls.config_set(self._conf)
        return new_cls

    def judgement_select(self, answer: str) -> bool:
        """
        这是一个专用的方法, 要求配置维护两个选项列表, 一份用于正确选项, 一份用于错误选项, 以应对题库对判断题答案响应的各种可能的情况
        它的作用是将获取到的答案answer与可能的选项列对比并返回对应的布尔值
        """
        if self.DISABLE:
            return False
        # 对响应的答案作处理
        answer = answer.strip()
        if answer in self.true_list:
            return True
        elif answer in self.false_list:
            return False
        else:
            # 无法判断, 随机选择
            logger.error(f'无法判断答案 -> {answer} 对应的是正确还是错误, 请自行判断并加入配置文件重启脚本, 本次将会随机选择选项')
            return random.choice([True,False])

    def get_submit_params(self):
        """
        这是一个专用方法, 用于根据当前设置的提交模式, 响应对应的答题提交API中的pyFlag值
        """
        # 留空直接提交, 1保存但不提交
        if self.SUBMIT:
            return ""
        else:
            return "1"

    @property
    def unavailable(self) -> bool:
        """是否因 Key 缺失 / 连接失败而停用（停用后 query() 不再发起任何远程请求）。"""
        return bool(self._unavailable_reason)

    def _mark_unavailable(self, reason: str) -> None:
        """停用大模型题库；本轮剩余题目全部走随机作答。"""
        if self._unavailable_reason:
            return
        self._unavailable_reason = reason
        logger.error(
            f"{self.name}已停用：{reason}。本轮剩余题目将直接随机作答，不再访问大模型"
            "（修正配置后在「设置」保存一次即可恢复）"
        )

    def _record_failure(self) -> None:
        """记录一次查询异常；连续失败达阈值后停用本轮的远程查询（仅大模型类题库）。"""
        if not self.LLM_PROVIDER:
            return
        self._failure_count += 1
        logger.warning(
            f"{self.name}连续第 {self._failure_count} 次请求失败"
            f"（阈值 {self.MAX_CONSECUTIVE_FAILURES}）"
        )
        if self._failure_count >= self.MAX_CONSECUTIVE_FAILURES:
            self._mark_unavailable(f"连续 {self._failure_count} 次请求失败")

    def _require_api_key(self, key: str, label: str) -> bool:
        """API Key 校验：缺失即刻停用，一个网络请求都不发。

        SiliconFlow 必须带 Key；AI 指向自建 OpenAI 兼容端点时，
        可在 [tiku] 里设 ``ignore_api_key=true`` 显式放行。
        """
        if str(key or "").strip():
            return True
        if self.ALLOW_EMPTY_KEY and self._conf_truthy(self.EMPTY_KEY_OPTION):
            logger.warning(
                f"已勾选「忽略 API Key」：{label}将以无鉴权方式访问（请确保该端点确实不需要鉴权）"
            )
            return True
        tail = "" if self.ALLOW_EMPTY_KEY else "；如需免鉴权请改用 provider=AI 并勾选「忽略 API Key」"
        self._mark_unavailable(f"未配置{label}的 API Key{tail}")
        return False

    def _conf_truthy(self, key: str) -> bool:
        """配置里开关是否打开（true/1/yes/on 均视为开）。"""
        return str((self._conf or {}).get(key, "") or "").strip().lower() in ("true", "1", "yes", "on")

    def _conf_bool(self, key: str, default: bool = False) -> bool:
        """配置开关，缺项/留空用 default；其余按 true/1/yes/on 解析。

        与 ``_conf_truthy`` 的区别：字符串 "false" 必须解析为 False。
        图形界面写回配置时是 "true"/"false" 文本，直接 bool("false") 会误判成打开。
        """
        raw = (self._conf or {}).get(key)
        if raw is None or str(raw).strip() == "":
            return default
        return str(raw).strip().lower() in ("true", "1", "yes", "on")

    def ensure_available(self) -> bool:
        """任务开始时调用：整个任务只对大模型做一次连通性检查，之后不再重复。

        非大模型题库（未覆写 check_llm_connection）恒为 True，不产生额外请求。
        """
        if self.DISABLE or self._unavailable_reason:
            return False
        if self._availability_checked:
            return True
        with self._availability_lock:
            if not self._availability_checked:
                self._availability_checked = True
                if not self.check_llm_connection():
                    self._mark_unavailable("连接检查失败（地址/Key/模型/网络不可达）")
        return not self._unavailable_reason

    def check_llm_connection(self) -> bool:
        """
        检查大模型连接是否可用
        默认返回 True（非大模型题库不需要检查）
        """
        return True


# 按照以下模板实现更多题库

class TikuYanxi(Tiku):
    # 言溪题库实现
    def __init__(self) -> None:
        super().__init__()
        self.name = '言溪题库'
        self.api = 'https://tk.enncy.cn/query'
        self._token = None
        self._token_index = 0   # token队列计数器
        self._times = 100   # 查询次数剩余, 初始化为100, 查询后校对修正

    def _query(self,q_info:dict):
        res = requests.get(
            self.api,
            params={
                'question':q_info['title'],
                'token': self._token,
                # 'type':q_info['type'], #修复478题目类型与答案类型不符（不想写后处理了）
                # 没用，就算有type和options，言溪题库还是可能返回类型不符，问了客服，type仅用于收集
            },
            verify=False
        )
        if res.status_code == 200:
            res_json = res.json()
            if not res_json['code']:
                # 如果是因为TOKEN次数到期, 则更换token
                if self._times == 0 or '次数不足' in res_json['data']['answer']:
                    logger.info(f'TOKEN查询次数不足, 将会更换并重新搜题')
                    self._token_index += 1
                    self.load_token()
                    # 重新查询
                    return self._query(q_info)
                logger.error(f'{self.name}查询失败:\n\t剩余查询数{res_json["data"].get("times",f"{self._times}(仅参考)")}:\n\t消息:{res_json["message"]}')
                return None
            self._times = res_json["data"].get("times",self._times)
            return res_json['data']['answer'].strip()
        else:
            logger.error(f'{self.name}查询失败:\n{res.text}')
        return None

    def load_token(self):
        token_list = self._conf['tokens'].split(',')
        if self._token_index == len(token_list):
            # TOKEN 用完
            logger.error('TOKEN用完, 请自行更换再重启脚本')
            raise PermissionError(f'{self.name} TOKEN 已用完, 请更换')
        self._token = token_list[self._token_index]

    def _init_tiku(self):
        self.load_token()

class TikuLike(Tiku):
    # LIKE知识库实现 参考 https://www.datam.site/
    def __init__(self) -> None:
        super().__init__()
        self.name = 'LIKE知识库'
        self.ver = '2.0.0' #对应官网API版本
        self.query_api = 'https://app.datam.site/api/v1/query'
        self.models_api = 'https://app.datam.site/api/v1/query/models'
        self.balance_api = 'https://app.datam.site/api/v1/balance'
        self.homepage = 'https://www.datam.site'
        self._model = None
        self._timeout = 300
        self._retry = True
        self._retry_times = 3
        self._tokens = []
        self._balance = {}
        self._search = False
        self._vision = True
        self._count = 0
        self._headers = {"Content-Type": "application/json"}

    def _query(self, q_info:dict = None):
        if not q_info:
            logger.error("当前无题目信息，请检查")
            return ""
        
        q_info_map = {"single": "【单选题】", "multiple": "【多选题】", "completion": "【填空题】", "judgement": "【判断题】"}
        q_info_prefix = q_info_map.get(q_info['type'], "【其他类型题目】")
        options = ', '.join(q_info['options']) if isinstance(q_info['options'], list) else q_info['options']
        question = f"{q_info_prefix}{q_info['title']}\n"

        if q_info['type'] in ['single', 'multiple']:
            question += f"选项为: {options}\n"

        # 随机选择一个token进行查询
        token = random.choice(self._tokens)
        
        # 检查该token是否有余额
        if self._balance.get(token, 0) <= 0:
            logger.error(f'{self.name}当前Token查询次数不足: ...{token[-5:]}')
            # 尝试选择其他有余额的token
            available_tokens = [t for t in self._tokens if self._balance.get(t, 0) > 0]
            if available_tokens:
                token = random.choice(available_tokens)
            else:
                logger.error(f'{self.name}所有Token查询次数都不足')
                return None

        ans = None
        try_times = 0
        
        # 尝试查询，直到成功或达到重试次数
        while not ans and self._retry and try_times < self._retry_times:
            ans = self._query_single(token, question)
            try_times += 1
            if ans:  # 如果查询成功，减少余额
                self._balance[token] -= 1
                logger.info(f'使用Token ...{token[-5:]} 查询成功，剩余次数: {self._balance[token]}')
                break
            elif try_times < self._retry_times:
                logger.warning(f'使用Token ...{token[-5:]} 查询失败，进行第 {try_times + 1} 次重试...')
        
        # 10次查询后更新余额
        self._count = (self._count + 1) % 10
        if self._count == 0:
            self.update_times()

        return ans
    
    def _query_single(self, token: str = "", query: str = "") -> str:
        """
        查询单个问题的答案
        
        Args:
            token: API访问令牌
            query: 查询的问题内容
            
        Returns:
            查询到的答案，如果失败则返回None
        """
        # 验证输入参数
        if not token:
            logger.error(f'{self.name}查询失败: 未提供有效的token')
            return None
        
        if not query:
            logger.error(f'{self.name}查询失败: 查询内容为空')
            return None
            
        # 设置请求头
        temp_headers = self._headers.copy()
        temp_headers['Authorization'] = f'Bearer {token}'
        
        # 准备请求数据
        request_data = {
            'query': query,
            'model': self._model if self._model else '',
            'search': self._search,
            'vision': self._vision
        }
        
        # 发送API请求
        try:
            res = requests.post(
                self.query_api,
                json=request_data,
                headers=temp_headers,
                verify=False,
                timeout=self._timeout  # 添加超时设置
            )
        except requests.exceptions.Timeout:
            logger.error(f'{self.name}查询超时: 请求超过300秒')
            return None
        except requests.exceptions.ConnectionError:
            logger.error(f'{self.name}网络连接错误: 无法连接到API服务器')
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f'{self.name}查询异常: \n{e}')
            return None
        except Exception as e:
            logger.error(f'{self.name}查询发生未知错误: \n{e}')
            return None

        # 处理HTTP响应
        if res.status_code == 200:
            return self._parse_response(res)
        elif res.status_code == 401:
            logger.error(f'{self.name}认证失败: 请检查Token是否正确或已过期')
        elif res.status_code == 429:
            logger.error(f'{self.name}请求过于频繁: 已达到API速率限制')
        elif res.status_code == 500:
            logger.error(f'{self.name}服务器内部错误: API服务暂时不可用')
        elif res.status_code == 400:
            logger.error(f'{self.name}请求参数错误: 请检查查询内容格式')
        elif res.status_code == 403:
            logger.error(f'{self.name}访问被拒绝: 可能是Token权限不足')
        else:
            logger.error(f'{self.name}查询失败: 状态码 {res.status_code}, 响应内容: \n{res.text}')
        
        return None
    
    def _parse_response(self, response):
        """
        解析API响应
        
        Args:
            response: HTTP响应对象
            
        Returns:
            解析后的答案，如果解析失败则返回None
        """
        try:
            res_json = response.json()
        except json.JSONDecodeError:
            logger.error(f'{self.name}响应解析失败: 响应不是有效的JSON格式')
            return None
        except Exception as e:
            logger.error(f'{self.name}响应解析异常: {e}')
            return None
        
        # 记录响应消息
        msg = res_json.get('message', '')
        if msg:
            logger.info(f'{self.name}响应消息: {msg}')

        results = res_json.get('results', {})
        if not results or not isinstance(results, dict):
            logger.error(f'{self.name}查询结果格式错误: API返回结果中results字段格式不正确')
            return None
            
        output = results.get('output', None)
        if output is None or not isinstance(output, dict):
            logger.error(f'{self.name}查询结果中output字段格式错误或不存在')
            return None
            
        q_type = output.get('questionType', None)
        if q_type is None:
            logger.error(f'{self.name}查询结果中questionType字段不存在')
            return None
            
        answer = output.get('answer', None)
        if answer is None:
            logger.error(f'{self.name}查询结果中answer字段不存在')
            return None
            
        # 根据题目类型提取答案
        return self._extract_answer_by_type(q_type, answer)
    
    def _extract_answer_by_type(self, q_type: str, answer: dict) -> str:
        """
        根据题目类型提取答案
        
        Args:
            q_type: 题目类型
            answer: 答案字典
            
        Returns:
            提取的答案文本
        """
        if not isinstance(answer, dict):
            logger.error(f'{self.name}答案格式错误: 不是有效的字典格式')
            return None
            
        if q_type == "CHOICE":
            selected_options = answer.get('selectedOptions', None)
            if selected_options is not None:
                if isinstance(selected_options, list) and selected_options:
                    # 过滤掉None和空字符串
                    valid_options = [opt for opt in selected_options if opt is not None and str(opt).strip()]
                    if valid_options:
                        return '\n'.join(str(opt) for opt in valid_options)
                    else:
                        logger.error(f'{self.name}CHOICE类型题目没有有效的选项内容')
                else:
                    logger.error(f'{self.name}CHOICE类型题目没有有效的选项内容')
            else:
                logger.error(f'{self.name}CHOICE类型题目缺少selectedOptions字段')
        elif q_type == "FILL_IN_BLANK":
            blanks = answer.get('blanks', None)
            if blanks is not None:
                if isinstance(blanks, list) and blanks:
                    # 过滤掉None和空字符串
                    valid_blanks = [blank for blank in blanks if blank is not None and str(blank).strip()]
                    if valid_blanks:
                        return "\n".join(str(blank) for blank in valid_blanks)
                    else:
                        logger.error(f'{self.name}FILL_IN_BLANK类型题目没有有效的填空内容')
                else:
                    logger.error(f'{self.name}FILL_IN_BLANK类型题目没有有效的填空内容')
            else:
                logger.error(f'{self.name}FILL_IN_BLANK类型题目缺少blanks字段')
        elif q_type == "JUDGMENT":
            is_correct = answer.get('isCorrect', None)
            if is_correct is not None:
                return "正确" if is_correct else "错误"
            else:
                logger.error(f'{self.name}JUDGMENT类型题目缺少isCorrect字段')
        else:
            otherText = answer.get('otherText', None)
            if otherText is not None:
                return str(otherText)
            else:
                logger.error(f'{self.name}未知题目类型{q_type}且缺少otherText字段')
        
        return None
    
    def get_api_balance(self, token:str = ""):
        if not token:
            logger.error(f'{self.name}获取余额失败: 未提供有效的token')
            return 0
            
        temp_headers = self._headers.copy()
        temp_headers['Authorization'] = f'Bearer {token}'
        try:
            res = requests.get(
                self.balance_api,
                headers=temp_headers,
                verify=False,
                timeout=self._timeout
            )
            if res.status_code == 200:
                res_json = res.json()
                return int(res_json.get("balance", 0))
            else:
                logger.error(f'{self.name}请求余额接口失败，状态码: {res.status_code}')
                return 0
        except requests.exceptions.Timeout:
            logger.error(f'{self.name}获取余额超时: 请求超过30秒')
            return 0
        except requests.exceptions.ConnectionError:
            logger.error(f'{self.name}网络连接错误: 无法连接到余额查询API服务器')
            return 0
        except ValueError:  # json解析错误或int转换错误
            logger.error(f'{self.name}余额响应解析失败: 响应格式不正确')
            return 0
        except Exception as e:
            logger.error(f'{self.name}Token余额查询过程中出现错误: {e}')
            return 0

    def update_times(self) -> None:
        if not self._tokens:
            logger.warning(f'{self.name}未加载任何Token, 无法更新余额')
            return
        for token in self._tokens:
            balance = self.get_api_balance(token)
            self._balance[token] = balance
            logger.info(f"当前LIKE知识库Token: ...{token[-5:]} 的剩余查询次数为: {balance} (仅供参考, 实际次数以查询结果为准)")

    def load_tokens(self) -> None:
        tokens_str = self._conf.get('tokens')
        if not tokens_str:
            logger.error(f'{self.name}配置中未找到tokens')
            self._tokens = []
            return
        if ',' in tokens_str:
            tokens = [token.strip() for token in tokens_str.split(',') if token.strip()]
        else:
            tokens = [tokens_str.strip()] if tokens_str.strip() else []
        self._tokens = tokens
        if not self._tokens:
            logger.warning(f'{self.name}未加载任何有效的Token')

    def load_config(self) -> None:
        # 从配置中获取参数，提供默认值（图形界面写的是 "true"/"false" 文本，统一按开关解析）
        self._search = self._conf_bool('likeapi_search', False)
        self._model = self._conf.get('likeapi_model', None) or None
        self._vision = self._conf_bool('likeapi_vision', True)
        self._retry = self._conf_bool('likeapi_retry', True)
        try:
            self._retry_times = int(str(self._conf.get("likeapi_retry_times", 3)).strip() or 3)
        except (TypeError, ValueError):
            self._retry_times = 3

    def _init_tiku(self) -> None:
        self.load_config()
        self.load_tokens()
        if self._tokens:
            self.update_times()
        else:
            logger.error(f'{self.name}初始化失败: 未加载任何有效的Token')
            self.DISABLE = True

class TikuAdapter(Tiku):
    # TikuAdapter题库实现 https://github.com/DokiDoki1103/tikuAdapter
    def __init__(self) -> None:
        super().__init__()
        self.name = 'TikuAdapter题库'
        self.api = ''

    def _query(self, q_info: dict):
        # 判断题目类型
        if q_info['type'] == "single":
            type = 0
        elif q_info['type'] == 'multiple':
            type = 1
        elif q_info['type'] == 'completion':
            type = 2
        elif q_info['type'] == 'judgement':
            type = 3
        else:
            type = 4

        options = q_info['options']
        res = requests.post(
            self.api,
            json={
                'question': q_info['title'],
                'options': [sub(r'^[A-Za-z]\.?、?\s?', '', option) for option in options.split('\n')],
                'type': type
            },
            verify=False
        )
        if res.status_code == 200:
            res_json = res.json()
            # if bool(res_json['plat']):
            # plat无论搜没搜到答案都返回0
            # 这个参数是tikuadapter用来设定自定义的平台类型
            if not len(res_json['answer']['bestAnswer']):
                logger.error("查询失败, 返回：" + res.text)
                return None
            sep = "\n"
            return sep.join(res_json['answer']['bestAnswer']).strip()
        # else:
        #   logger.error(f'{self.name}查询失败:\n{res.text}')
        return None

    def _init_tiku(self):
        # self.load_token()
        self.api = self._conf['url']

class AI(Tiku):
    # AI大模型答题实现（OpenAI 兼容端点，例如 DeepSeek 官方 / 自建 Ollama）
    LLM_PROVIDER = True
    ALLOW_EMPTY_KEY = True      # 自建端点不需要鉴权时，配置里 ignore_api_key=true 放行
    # 各题型的系统提示词：要求输出 {"Answer": [...]} 的纯 JSON
    SYSTEM_PROMPTS = {
        "single": "本题为单选题，你只能选择一个选项，请根据题目和选项回答问题，以json格式输出正确的选项内容，示例回答：{\"Answer\": [\"答案\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料",
        "multiple": "本题为多选题，你必须选择两个或以上选项，请根据题目和选项回答问题，以json格式输出正确的选项内容，示例回答：{\"Answer\": [\"答案1\",\n\"答案2\",\n\"答案3\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料",
        "completion": "本题为填空题，你必须根据语境和相关知识填入合适的内容，请根据题目回答问题，以json格式输出正确的答案，示例回答：{\"Answer\": [\"答案\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料",
        "judgement": "本题为判断题，你只能回答正确或者错误，请根据题目回答问题，以json格式输出正确的答案，示例回答：{\"Answer\": [\"正确\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料",
    }
    # 未知题型（目前仅知有简答题）走这个兜底提示词
    FALLBACK_PROMPT = "本题为简答题，你必须根据语境和相关知识填入合适的内容，请根据题目回答问题，以json格式输出正确的答案，示例回答：{\"Answer\": [\"这是我的答案\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"

    def __init__(self) -> None:
        super().__init__()
        self.name = 'AI大模型答题'
        self.last_request_time = None

    def _wait_interval(self) -> None:
        """按最小请求间隔限速，避免打爆端点。"""
        if not self.last_request_time:
            return
        interval_time = time.time() - self.last_request_time
        if interval_time < self.min_interval_seconds:
            sleep_time = self.min_interval_seconds - interval_time
            logger.debug(f"API请求间隔过短, 等待 {sleep_time} 秒")
            time.sleep(sleep_time)

    def _query(self, q_info: dict):
        user_content = f"题目：{q_info['title']}"
        if q_info['type'] in ("single", "multiple"):
            # 去除选项字母，防止大模型直接输出字母而非内容
            options_list = q_info['options'].split('\n')
            cleaned_options = [re.sub(r"^[A-Z]\s*", "", option) for option in options_list]
            user_content += f"\n选项：{'\n'.join(cleaned_options)}"

        self._wait_interval()
        try:
            client = _openai_client(self.endpoint, str(self.key or ''), self.http_proxy)
            completion = client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": self.SYSTEM_PROMPTS.get(q_info['type'], self.FALLBACK_PROMPT),
                    },
                    {"role": "user", "content": user_content},
                ]
            )
        except Exception as e:  # noqa: BLE001 - 端点不通/鉴权失败都要落到随机作答，不能中断刷课
            logger.error(f"{self.name}请求失败：{type(e).__name__}: {e}")
            return None
        self.last_request_time = time.time()

        content = getattr(completion.choices[0].message, "content", "") or ""
        try:
            response = json.loads(_strip_md_json_wrapper(content))
            return "\n".join(response['Answer']).strip()
        except Exception as e:  # noqa: BLE001
            logger.error(f"无法解析大模型输出内容：{e}\n{content}")
            return None

    def _init_tiku(self):
        # 与 SiliconFlow 共用回退键：优先读 AI 专用键，缺失时回落到 siliconflow_* 键，
        # 避免老用户只填过 siliconflow_* 时切到 AI 直接不可用。
        self.endpoint = _normalize_base_url(
            self._conf.get('endpoint') or self._conf.get('siliconflow_endpoint', '')
        )
        self.key = self._conf.get('key') or self._conf.get('siliconflow_key', '')
        self.model = self._conf.get('model') or self._conf.get('siliconflow_model', '')
        self.http_proxy = self._conf.get('http_proxy', '')
        try:
            self.min_interval_seconds = float(self._conf.get('min_interval_seconds', 3) or 3)
        except (TypeError, ValueError):
            self.min_interval_seconds = 3.0
        # 未配置 Key 时立即停用（不发起任何请求）；自建端点可显式勾选忽略 Key
        self._require_api_key(self.key, "AI 端点")

    def check_llm_connection(self) -> bool:
        """
        检查大模型连接是否可用
        发送一个简单的测试请求来验证 API 配置
        """
        logger.info(f'正在检查 {self.name} 连接...')
        try:
            client = _openai_client(self.endpoint, self.key, self.http_proxy)
            
            # 发送一个简单的测试请求
            completion = client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        'role': 'user',
                        'content': '你好，请回答：1+1 等于几？只回答数字。'
                    }
                ],
                max_tokens=10
            )
            
            if completion.choices and completion.choices[0].message.content:
                logger.info(f'{self.name} 连接检查成功')
                return True
            else:
                logger.error(f'{self.name} 连接检查失败：未收到响应')
                return False
                
        except Exception as e:
            logger.error(f'{self.name} 连接检查失败：{e}')
            return False


class SiliconFlow(Tiku):
    """硅基流动大模型答题实现"""
    LLM_PROVIDER = True
    ALLOW_EMPTY_KEY = False     # 硅基流动必须带 Key，留空一律不发请求
    def __init__(self):
        super().__init__()
        self.name = '硅基流动大模型'
        self.last_request_time = None

    def _query(self, q_info: dict):
        def remove_md_json_wrapper(md_str):
            # 解析可能存在的JSON包装
            return _strip_md_json_wrapper(md_str)

        # 构造请求头
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        # 构造系统提示词
        system_prompt = ""
        if q_info['type'] == "single":
            system_prompt = "本题为单选题，请根据题目和选项选择唯一正确答案，输出的是选项的具体内容，而不是内容前的ABCD，并以JSON格式输出：示例回答：{\"Answer\": [\"正确选项内容\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
        elif q_info['type'] == 'multiple':
            system_prompt = "本题为多选题，请选择所有正确选项，输出的是选项的具体内容，而不是内容前的ABCD，以JSON格式输出：示例回答：{\"Answer\": [\"选项1\",\"选项2\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
        elif q_info['type'] == 'completion':
            system_prompt = "本题为填空题，请直接给出填空内容，以JSON格式输出：示例回答：{\"Answer\": [\"答案文本\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"
        elif q_info['type'] == 'judgement':
            system_prompt = "本题为判断题，请回答'正确'或'错误'，以JSON格式输出：示例回答：{\"Answer\": [\"正确\"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料"

        # 构造请求体
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": f"题目：{q_info['title']}\n选项：{q_info['options']}"
                }
            ],
            "stream": False,

            "max_tokens": 4096,

            "temperature": 0.7,
            "top_p": 0.7,
            "response_format": {"type": "text"}
        }

        # 处理请求间隔
        if self.last_request_time:
            interval = time.time() - self.last_request_time
            if interval < self.min_interval:
                time.sleep(self.min_interval - interval)

        try:
            response = requests.post(
                self.api_endpoint,
                headers=headers,
                json=payload,
                timeout=30
            )
            self.last_request_time = time.time()

            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                parsed = json.loads(remove_md_json_wrapper(content))
                return "\n".join(parsed['Answer']).strip()
            else:
                logger.error(f"API请求失败：{response.status_code} {response.text}")
                return None

        except Exception as e:
            logger.error(f"硅基流动API异常：{e}")
            return None

    def _init_tiku(self):
        # 从配置文件读取参数
        self.api_endpoint = self._conf.get('siliconflow_endpoint', 'https://api.siliconflow.cn/v1/chat/completions')
        self.api_key = self._conf.get('siliconflow_key', '')

        self.model_name = self._conf.get('siliconflow_model', 'deepseek-ai/DeepSeek-V3')


        try:
            self.min_interval = float(self._conf.get('min_interval_seconds', 3) or 3)
        except (TypeError, ValueError):
            self.min_interval = 3.0
        # 未配置 Key 时立即停用：不会向硅基流动发起任何请求，题目直接随机作答
        self._require_api_key(self.api_key, "硅基流动")

    def check_llm_connection(self) -> bool:
        """
        检查硅基流动大模型连接是否可用
        发送一个简单的测试请求来验证 API 配置
        """
        logger.info(f'正在检查 {self.name} 连接...')
        try:
            headers = {
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json'
            }
            
            payload = {
                'model': self.model_name,
                'messages': [
                    {
                        'role': 'user',
                        'content': '你好，请回答：1+1 等于几？只回答数字。'
                    }
                ],
                'stream': False,
                'max_tokens': 10,
                'temperature': 0.7,
                'top_p': 0.7,
                'response_format': {'type': 'text'}
            }
            
            response = requests.post(
                self.api_endpoint,
                headers=headers,
                json=payload,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('choices') and result['choices'][0]['message']['content']:
                    logger.info(f'{self.name} 连接检查成功')
                    return True
                else:
                    logger.error(f'{self.name} 连接检查失败：未收到有效响应')
                    return False
            else:
                logger.error(f'{self.name} 连接检查失败：{response.status_code} {response.text}')
                return False
                
        except Exception as e:
            logger.error(f'{self.name} 连接检查失败：{e}')
            return False


class DeepSeek(AI):
    """DeepSeek 官方端点答题实现。

    与 AI 走同一套 OpenAI 兼容请求逻辑与提示词，只是配置键独立
    （deepseek_endpoint / deepseek_key / deepseek_model），
    这样界面上 AI / DeepSeek / SiliconFlow 三种方式可以各存各的配置与默认值。
    """
    ALLOW_EMPTY_KEY = False     # DeepSeek 官方必须带 Key
    DEFAULT_ENDPOINT = "https://api.deepseek.com/v1"
    DEFAULT_MODEL = "deepseek-chat"

    def __init__(self) -> None:
        super().__init__()
        self.name = 'DeepSeek大模型'

    def _init_tiku(self):
        self.endpoint = _normalize_base_url(
            self._conf.get('deepseek_endpoint') or self.DEFAULT_ENDPOINT
        )
        self.key = self._conf.get('deepseek_key', '')
        self.model = self._conf.get('deepseek_model') or self.DEFAULT_MODEL
        self.http_proxy = self._conf.get('http_proxy', '')
        try:
            self.min_interval_seconds = float(self._conf.get('min_interval_seconds', 3) or 3)
        except (TypeError, ValueError):
            self.min_interval_seconds = 3.0
        self._require_api_key(self.key, "DeepSeek 端点")
