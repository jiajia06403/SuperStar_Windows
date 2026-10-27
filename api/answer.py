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

# AI 大模型答题属于可选能力：openai/httpx 改为「首次使用时」才导入（见 _get_openai）。
# 这样 provider 用 TikuYanxi/Like/DeepSeek/SiliconFlow 时，启动不再白付 ~1.1s 的导入开销；
# 缺失这两个依赖时其余题库与刷课功能照常工作。
_openai_import = None  # None=尚未尝试, False=不可用, (OpenAI, httpx)=可用


def _get_openai():
    """惰性加载 openai/httpx，返回 ``(OpenAI, httpx)``；缺失依赖时返回 ``(None, None)``。

    结果按模块级缓存（import 由 GIL + import 锁保护，并发首次调用幂等），
    因此只有第一次真正用到 AI 题库时才付导入代价。
    """
    global _openai_import
    if _openai_import is None:
        try:
            import httpx
            from openai import OpenAI
            _openai_import = (OpenAI, httpx)
        except ImportError:  # pragma: no cover - 精简发行版不打包该可选依赖
            _openai_import = False
    return _openai_import or (None, None)

from api import local_bank, wrong_answers
from api.answer_check import *
from api.logger import logger

# 关闭警告
disable_warnings(exceptions.InsecureRequestWarning)


def _openai_client(base_url: str, api_key: str, proxy: Optional[str] = None,
                   max_retries: Optional[int] = None):
    """构造 OpenAI 客户端（可选依赖，缺失时给出明确提示）。

    ``max_retries`` 仅供连通性探测用 0：默认重试 2 次会让「地址填错」的测试按钮等很久。
    """
    OpenAI, httpx = _get_openai()
    if OpenAI is None or httpx is None:
        raise RuntimeError("AI 题库需要 openai 与 httpx 依赖：pip install openai httpx")
    kwargs = {"base_url": base_url, "api_key": api_key}
    if max_retries is not None:
        kwargs["max_retries"] = max_retries
    if proxy:
        return OpenAI(http_client=httpx.Client(proxy=proxy), **kwargs)
    return OpenAI(**kwargs)


def _short_error(exc: BaseException, limit: int = 110) -> str:
    """把异常压成一行短提示，供界面直接显示（去掉换行、限制长度）。"""
    detail = " ".join(str(exc).split())
    if len(detail) > limit:
        detail = detail[:limit] + "…"
    name = type(exc).__name__
    return f"{name}: {detail}" if detail else name


__all__ = ["CacheDAO", "Tiku", "TikuYanxi", "TikuLike", "TikuAdapter", "AI", "DeepSeek",
           "SiliconFlow", "test_tiku_connection"]


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


# --------------------------------------------------------------------------- #
# 缓存文件锁：同一进程内按「缓存文件真实路径」共享一把 RLock
#
# 之前 CacheDAO.__init__ 里是 self._lock = threading.RLock()，实例级：
# Tiku.query 每次查询 new 一个 CacheDAO()，任务点线程池（最多 5 线程）各拿各的锁，
# 互斥根本不成立；add_cache 是 read-modify-write，原子 replace 只保证文件不损坏，
# 挡不住「两个线程读到同一份旧内容、后写的覆盖先写的」→ 丢更新。
# 改成模块级锁表后，指向同一个 cache.json 的所有实例共用一把锁；
# 不同路径（单测里的临时文件）仍各用各的锁，互不阻塞。
# 用 RLock：add_cache/remove_cache 已持锁再进 _read_cache/_write_cache 需要可重入。
# --------------------------------------------------------------------------- #
_CACHE_LOCKS: "dict[str, threading.RLock]" = {}
_CACHE_LOCKS_GUARD = threading.Lock()


def _cache_lock_for(file) -> "threading.RLock":
    """取缓存文件对应的共享锁（同文件同锁；路径按 abspath+normcase 归一化）。"""
    try:
        key = os.path.normcase(os.path.abspath(str(file)))
    except Exception:  # noqa: BLE001 - 路径异常时退化为字符串，不至于崩在拿锁上
        key = str(file)
    lock = _CACHE_LOCKS.get(key)
    if lock is None:
        with _CACHE_LOCKS_GUARD:
            lock = _CACHE_LOCKS.get(key)
            if lock is None:
                lock = threading.RLock()
                _CACHE_LOCKS[key] = lock
    return lock


class CacheDAO:
    """
    @Author: SocialSisterYi
    @Reference: https://github.com/SocialSisterYi/xuexiaoyi-to-xuexitong-tampermonkey-proxy
    """
    DEFAULT_CACHE_FILE = "cache.json"

    def __init__(self, file: Optional[str] = None):
        self.cache_file = Path(file or _default_cache_file())
        # 不是实例级锁：同一缓存文件的所有实例共享 _cache_lock_for() 返回的锁，
        # 否则多线程并发 add_cache 的 read-modify-write 会丢更新。
        self._lock = _cache_lock_for(self.cache_file)
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
        # 为「读取-修改-回写」整段加锁（锁按缓存文件共享）：
        # 原子 replace 只保证文件不损坏，不保证不丢更新——
        # 两个线程同时读到旧内容时，后写的一份会整块覆盖掉先写的那题。
        with self._lock:
            data = self._read_cache()
            data[question] = answer
            self._write_cache(data)

    def remove_cache(self, question: str) -> bool:
        """删掉某题的缓存答案（平台判错后作废，避免重答时复用同一个错答案）。"""
        with self._lock:
            data = self._read_cache()
            if question not in data:
                return False
            data.pop(question, None)
            self._write_cache(data)
            return True


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
        conf = self._conf or {}
        # 通用项一律读配置：口令是「题库停用 = 不答题」由调用方（study_work/query）判断，
        # 这里只负责把 submit / cover_rate / 判断题词表解析出来，缺项时用类默认值，
        # 避免老配置少了某个键就把整个题库初始化搞崩。
        try:
            submit = str(conf.get('submit', 'true') or 'true').strip().lower()
            self.SUBMIT = submit in ('true', '1', 'yes', 'on')
            cover = str(conf.get('cover_rate', '') or '').strip()
            if cover:
                self.COVER_RATE = float(cover)
            true_list = str(conf.get('true_list', '') or '正确,对,√,是')
            false_list = str(conf.get('false_list', '') or '错误,错,×,否')
            self.true_list = [w.strip() for w in true_list.split(',') if w.strip()]
            self.false_list = [w.strip() for w in false_list.split(',') if w.strip()]
        except (TypeError, ValueError):  # noqa: BLE001 - 配置异常时用类默认值，不中断
            pass
        # 本地题库按同一份配置初始化/刷新（关闭时既不读也不写）
        local_bank.configure(conf)
        if not self.DISABLE:
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
        
    def query(self, q_info: dict, course: str = "", course_id: str = "") -> Optional[str]:
        # 预处理, 去除【单选题】这样与标题无关的字段
        # （本地题库的指纹用同一套处理后标题，所以必须放在最前面）
        logger.debug(f"原始标题：{q_info['title']}")
        q_info['title'] = sub(r'^\d+', '', q_info['title'])
        q_info['title'] = sub(r'（\d+\.\d+分）$', '', q_info['title'])
        logger.debug(f"处理后标题：{q_info['title']}")

        # 题库停用（provider 留空 / 未配置）= 不答作业，本地题库也不参与：
        # 否则纯本地模式下遇到没收录的题会「随机作答并提交」，把作业做烂。
        if self.DISABLE:
            return None

        title = str(q_info.get('title') or '')
        qtype = str(q_info.get('type') or '')
        options = str(q_info.get('options') or '')
        # 这道题被平台判错过的答案（按选项正文记录：换账号 / 选项乱序都对得上）。
        # 判错过的答案绝不能再原样提交，否则平台必然再判一次不合格（死循环根因）。
        known = wrong_answers.known_keys(title, qtype, options)

        # 本地题库优先：只存平台判过对的答案，命中即用，不消耗任何远程额度
        # course / course_id 由调用方显式传入（并发安全，不读进程级作用域）
        local_answer = self._query_local_bank(q_info, course, course_id)
        if local_answer:
            if not self._is_rejected(known, qtype, options, local_answer):
                return local_answer
            # 题库这条答案已被平台判错 -> 删掉它（题库只留确认正确的），继续往下找
            logger.warning(f"本地题库答案已被平台判错，已删除该条目并重新搜题：{title}")
            self._drop_local_bank(title, qtype, options, local_answer)

        # 先过缓存（本地缓存不消耗任何远程额度，大模型不可用时也应继续命中）
        cache_dao = CacheDAO()
        answer = cache_dao.get_cache(title)
        if answer:
            if self._is_rejected(known, qtype, options, answer):
                # 缓存里存的就是被判错的答案：作废，改走远程重新请求。
                # 不删的话每次重答都命中同一批错答案，永远跳不出循环。
                logger.warning(f"缓存答案已被平台判错，已作废并重新搜题：{title}")
                cache_dao.remove_cache(title)
            else:
                logger.info(f"从缓存中获取答案：{title} -> {answer}")
                return answer.strip()

        # 大模型不可用（未配置 Key / 连接检查失败 / 连续失败）：不再发起任何请求，
        # 直接返回 None，由上层随机作答。
        if self._unavailable_reason:
            logger.debug(f"{self.name}不可用，跳过远程查询：{self._unavailable_reason}")
            return None

        # 把「已判错的选项」告诉大模型（搜题类题库会忽略这个键，靠下面的排除法兜底）
        hints = wrong_answers.hint_texts(qtype, options, known)
        if hints:
            q_info['_exclude'] = hints
        try:
            answer = self._query(q_info)
        except Exception as e:  # noqa: BLE001 - 任何异常都不能中断刷课，交给随机作答兜底
            logger.error(f"{self.name}查询异常：{type(e).__name__}: {e}")
            self._record_failure()
            return None
        finally:
            q_info.pop('_exclude', None)

        if answer:
            self._failure_count = 0
            answer = answer.strip()
            logger.info(f"从{self.name}获取答案：{title} -> {answer}")
            if self._is_rejected(known, qtype, options, answer):
                # 大模型无视排除提示、又给了同一个错答案 -> 直接用排除法换一个，
                # 保证这一轮提交的答案和上一轮不同，平台才有机会判对。
                alt = wrong_answers.alternative_for(qtype, options, known, answer)
                if not alt:
                    if str(qtype or "").strip().lower() in ("single", "multiple",
                                                            "judgement"):
                        logger.warning(f"该题所有可提交的答案都已被平台判错，"
                                       f"无法再换答案：{title}")
                    else:
                        logger.info(f"该题型没有排除法可用，放弃本题：{title}")
                    return None
                logger.warning(f"{self.name}仍返回被判错的答案，改用排除法："
                               f"{title} {answer} -> {alt}")
                answer = alt
            if check_answer(answer, qtype, self):
                cache_dao.add_cache(title, answer)
                return answer
            else:
                logger.info(f"从{self.name}获取到的答案类型与题目类型不符，已舍弃")
                return None

        logger.error(f"从{self.name}获取答案失败：{title}")
        self._record_failure()
        return None

    @staticmethod
    def _is_rejected(known, qtype: str, options: str, answer) -> bool:
        """这个答案是否已被平台判错过（``known`` 为空时恒为 False）。"""
        if not known or not answer:
            return False
        try:
            return local_bank.answer_key(qtype, answer, options) in known
        except Exception as exc:  # noqa: BLE001 - 比对失败按「没判错过」处理
            logger.error(f"错题本比对异常：{type(exc).__name__}: {exc}")
            return False

    @staticmethod
    def _drop_local_bank(title: str, qtype: str, options: str, answer: str) -> None:
        """删掉题库里「就是这个被判错的答案」的条目（题库只留确认正确的）。"""
        try:
            local_bank.get_bank().drop_answer(title, qtype, options, answer)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"本地题库条目清理异常：{type(exc).__name__}: {exc}")

    def _query_local_bank(self, q_info: dict, course: str = "",
                          course_id: str = "") -> Optional[str]:
        """查本地题库（平台判过对的答案）。关闭 / 未命中 / 异常都返回 None。

        ``course`` / ``course_id`` 决定「先查哪个课程文件」，由调用方显式给出；
        留空时题库退回兼容作用域（见 :meth:`BankStore.set_scope`）。
        """
        try:
            bank = local_bank.get_bank()
            if not bank.usable:
                return None
            return bank.lookup(q_info.get('title', ''), q_info.get('type', ''),
                               q_info.get('options', ''),
                               course=course, course_id=course_id)
        except Exception as exc:  # noqa: BLE001 - 本地题库异常不能影响正常搜题
            logger.error(f"本地题库查询异常：{type(exc).__name__}: {exc}")
            return None

    def learn_work_answers(self, items: list, verdicts=None, course: str = "",
                           course_id: str = "", **kwargs) -> int:
        """把平台确认过的正确答案写进本地题库（题库只存正确答案）。

        库里已有的答案与平台一致则不重写；不一致（用题库答案作答被判错）则**覆写纠正**。

        ``items``    : [{"qid", "title", "type", "options", "answer"}]
        ``verdicts`` : {qid: True/False/{ok, answer, options}}；None 表示没做核验，不入库
        返回写入条数（新增 + 纠正）；题库关闭时恒为 0。
        """
        try:
            bank = local_bank.get_bank()
            return bank.learn(items, verdicts, course=course, course_id=course_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"本地题库入库异常：{type(exc).__name__}: {exc}")
            return 0

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
        # 题库停用时调用方已提前返回，不会走到这里；保留词表兜底，
        # 避免配置少写 true_list/false_list 时把判断题全判成「错」。
        if not self.true_list:
            self.true_list = ['正确', '对', '√', '是']
        if not self.false_list:
            self.false_list = ['错误', '错', '×', '否']
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

    # ---------------- 连通性测试（界面的「测试连通性」按钮） ----------------
    TEST_TIMEOUT = 15                      # 单次探测超时（秒）
    TEST_QUESTION = "连通性测试：1+1 等于几？"   # 探测用题目，可能消耗 1 次额度

    def test_connection(self) -> "tuple[bool, str]":
        """探测当前配置能否正常使用，返回 ``(是否连通, 给用户看的一句话)``。

        只读 ``self._conf``，不依赖 ``init_tiku()``（于是不会改动全局题库状态），
        因此界面可以拿「尚未保存的界面值」直接测试。子类按各自接口实现。
        """
        return False, "该答案提供方式没有可测试的远程服务"


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

    def test_connection(self):
        """测试言溪：带 TOKEN 发一次真实查询（会消耗少量次数）。"""
        tokens = str((self._conf or {}).get('tokens', '') or '')
        token = tokens.split(',')[0].strip() if tokens else ''
        if not token:
            return False, "未填写 TOKEN"
        try:
            res = requests.get(
                self.api,
                params={'question': self.TEST_QUESTION, 'token': token},
                verify=False,
                timeout=self.TEST_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - 探测失败只需一句提示
            return False, "请求失败 · " + _short_error(exc)
        if res.status_code != 200:
            return False, f"HTTP {res.status_code} · {res.text[:60]}"
        try:
            data = res.json()
        except ValueError:
            return False, "响应不是合法 JSON（地址可能不对）"
        if data.get('code'):
            times = (data.get('data') or {}).get('times')
            tail = f"，剩余 {times} 次" if times not in (None, '') else ""
            return True, f"连接正常{tail}"
        msg = str(data.get('message') or data.get('msg') or '').strip()
        return False, msg or "TOKEN 无效或查询次数不足"

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

    def test_connection(self):
        """测试 LIKE：查第一个 TOKEN 的余额接口（只读，不消耗查询次数）。"""
        tokens = [t.strip() for t in
                  str((self._conf or {}).get('tokens', '') or '').split(',') if t.strip()]
        if not tokens:
            return False, "未填写 TOKEN"
        token = tokens[0]
        try:
            res = requests.get(
                self.balance_api,
                headers={**self._headers, 'Authorization': f'Bearer {token}'},
                verify=False,
                timeout=self.TEST_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            return False, "请求失败 · " + _short_error(exc)
        if res.status_code == 401:
            return False, "TOKEN 无效或已过期"
        if res.status_code != 200:
            return False, f"HTTP {res.status_code} · {res.text[:60]}"
        try:
            balance = int(res.json().get('balance', 0))
        except (ValueError, AttributeError, TypeError):
            return True, "连接正常（余额未知）"
        if balance > 0:
            return True, f"连接正常，剩余 {balance} 次"
        return True, "连接正常，但该 TOKEN 余额为 0"

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

    def test_connection(self):
        """测试 TikuAdapter：向服务地址发一次探测查询，只看能否正常响应。"""
        url = str((self._conf or {}).get('url', '') or '').strip()
        if not url:
            return False, "未填写服务地址"
        try:
            res = requests.post(
                url,
                json={'question': self.TEST_QUESTION, 'options': ['1', '2'], 'type': 0},
                verify=False,
                timeout=self.TEST_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            return False, "请求失败 · " + _short_error(exc)
        if res.status_code != 200:
            return False, f"HTTP {res.status_code} · {res.text[:60]}"
        try:
            res.json()
        except ValueError:
            return False, "响应不是合法 JSON（地址可能不对）"
        return True, "连接正常"

class AI(Tiku):
    # AI大模型答题实现（OpenAI 兼容端点，例如 DeepSeek 官方 / 自建 Ollama）
    LLM_PROVIDER = True
    ALLOW_EMPTY_KEY = True      # 自建端点不需要鉴权时，配置里 ignore_api_key=true 放行
    # 配置键与默认值；DeepSeek 子类只改这几个类属性即可复用整套请求/探测逻辑
    ENDPOINT_FIELD = "endpoint"
    KEY_FIELD = "key"
    MODEL_FIELD = "model"
    KEY_LABEL = "AI 端点"
    DEFAULT_ENDPOINT = ""
    DEFAULT_MODEL = ""
    LEGACY_SILICONFLOW_FALLBACK = True   # 老配置只填过 siliconflow_* 时切到 AI 仍可用
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
        # 已提交过并被平台判错的答案：明确让大模型避开，否则它很可能重复同一个错答案
        for hint in (q_info.get('_exclude') or []):
            user_content += f"\n注意：{hint}"

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

    def _conf_params(self) -> tuple:
        """从配置解析 ``(endpoint, key, model, proxy)``，供初始化与连通性测试共用。"""
        conf = self._conf or {}
        legacy = conf if self.LEGACY_SILICONFLOW_FALLBACK else {}
        endpoint = _normalize_base_url(
            conf.get(self.ENDPOINT_FIELD) or legacy.get('siliconflow_endpoint') or self.DEFAULT_ENDPOINT
        )
        key = conf.get(self.KEY_FIELD) or legacy.get('siliconflow_key', '')
        model = conf.get(self.MODEL_FIELD) or legacy.get('siliconflow_model') or self.DEFAULT_MODEL
        return endpoint, str(key or ''), str(model or ''), conf.get('http_proxy', '')

    def _init_tiku(self):
        self.endpoint, self.key, self.model, self.http_proxy = self._conf_params()
        try:
            self.min_interval_seconds = float(self._conf.get('min_interval_seconds', 3) or 3)
        except (TypeError, ValueError):
            self.min_interval_seconds = 3.0
        # 未配置 Key 时立即停用（不发起任何请求）；自建端点可显式勾选忽略 Key
        self._require_api_key(self.key, self.KEY_LABEL)

    def test_connection(self):
        """测试大模型端点：发一条最小请求，看是否真能拿到回复。"""
        endpoint, key, model, proxy = self._conf_params()
        if not endpoint:
            return False, "未填写接口地址"
        if not model:
            return False, "未填写模型"
        if not key and not (self.ALLOW_EMPTY_KEY and self._conf_truthy(self.EMPTY_KEY_OPTION)):
            return False, "未填写 API Key"
        try:
            client = _openai_client(endpoint, key, proxy, max_retries=0)
            completion = client.chat.completions.create(
                model=model,
                messages=[{'role': 'user', 'content': '你好，请回答：1+1 等于几？只回答数字。'}],
                max_tokens=10,
                timeout=self.TEST_TIMEOUT,
            )
            content = (completion.choices[0].message.content or '').strip()
        except Exception as exc:  # noqa: BLE001 - 探测失败只回一句提示
            return False, _short_error(exc)
        if not content:
            return False, "端点已响应，但内容为空（检查模型名是否存在）"
        return True, f"连接正常（{model} 回复 {content[:12]}）"

    def check_llm_connection(self) -> bool:
        """刷课开始前的可用性检查：复用界面的同一套探测逻辑。"""
        logger.info(f'正在检查 {self.name} 连接...')
        ok, msg = self.test_connection()
        if ok:
            logger.info(f'{self.name} 连接检查成功：{msg}')
        else:
            logger.error(f'{self.name} 连接检查失败：{msg}')
        return ok


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
        user_content = f"题目：{q_info['title']}\n选项：{q_info['options']}"
        # 已提交过并被平台判错的答案：明确让大模型避开，否则它很可能重复同一个错答案
        for hint in (q_info.get('_exclude') or []):
            user_content += f"\n注意：{hint}"
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": user_content
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
        """刷课开始前的可用性检查：复用界面的同一套探测逻辑。"""
        logger.info(f'正在检查 {self.name} 连接...')
        ok, msg = self.test_connection()
        if ok:
            logger.info(f'{self.name} 连接检查成功：{msg}')
        else:
            logger.error(f'{self.name} 连接检查失败：{msg}')
        return ok

    def test_connection(self):
        """测试硅基流动：POST 一条最小请求（地址需是 .../chat/completions 完整地址）。"""
        conf = self._conf or {}
        endpoint = str(conf.get('siliconflow_endpoint', '') or '').strip() \
            or 'https://api.siliconflow.cn/v1/chat/completions'
        key = str(conf.get('siliconflow_key', '') or '').strip()
        model = str(conf.get('siliconflow_model', '') or '').strip() or 'deepseek-ai/DeepSeek-V3'
        if not key:
            return False, "未填写 API Key"
        payload = {
            'model': model,
            'messages': [{'role': 'user', 'content': '你好，请回答：1+1 等于几？只回答数字。'}],
            'stream': False,
            'max_tokens': 10,
            'temperature': 0.7,
            'top_p': 0.7,
            'response_format': {'type': 'text'},
        }
        try:
            res = requests.post(
                endpoint,
                headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                json=payload,
                timeout=self.TEST_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            return False, "请求失败 · " + _short_error(exc)
        if res.status_code == 401:
            return False, "API Key 无效或已过期"
        if res.status_code != 200:
            return False, f"HTTP {res.status_code} · {res.text[:60]}"
        try:
            content = str(res.json()['choices'][0]['message']['content'] or '').strip()
        except (ValueError, KeyError, IndexError, TypeError):
            return False, "响应格式不符（地址需为 .../v1/chat/completions）"
        if not content:
            return False, "端点已响应，但内容为空（检查模型名是否存在）"
        return True, f"连接正常（{model} 回复 {content[:12]}）"


class DeepSeek(AI):
    """DeepSeek 官方端点答题实现。

    与 AI 走同一套 OpenAI 兼容请求逻辑与提示词，只是配置键独立
    （deepseek_endpoint / deepseek_key / deepseek_model），
    这样界面上 AI / DeepSeek / SiliconFlow 三种方式可以各存各的配置与默认值。
    """
    ALLOW_EMPTY_KEY = False     # DeepSeek 官方必须带 Key
    ENDPOINT_FIELD = "deepseek_endpoint"
    KEY_FIELD = "deepseek_key"
    MODEL_FIELD = "deepseek_model"
    KEY_LABEL = "DeepSeek 端点"
    DEFAULT_ENDPOINT = "https://api.deepseek.com/v1"
    DEFAULT_MODEL = "deepseek-chat"
    LEGACY_SILICONFLOW_FALLBACK = False   # 用官方专用键，不吃 siliconflow_* 回退

    def __init__(self) -> None:
        super().__init__()
        self.name = 'DeepSeek大模型'


# --------------------------------------------------------------------------- #
# 连通性测试入口（界面「测试连通性」按钮用）
# --------------------------------------------------------------------------- #
def test_tiku_connection(conf) -> "tuple[bool, str]":
    """按给定的 ``[tiku]`` 配置做一次连通性探测，返回 ``(是否连通, 一句话说明)``。

    只用传入的配置构造题库对象，**不落盘、不调用 init_tiku()**：
    界面因此可以测试「尚未保存的界面值」，也不会把正在运行的任务的题库状态改掉。
    """
    conf = dict(conf or {})
    name = str(conf.get('provider', '') or '').strip()
    if not name:
        return False, "未选择答案提供方式（题库已停用）"
    cls = globals().get(name)
    if not isinstance(cls, type) or not issubclass(cls, Tiku):
        return False, f"未知的答案提供方式：{name}"
    try:
        tiku = cls()
        tiku.config_set(conf)
        return tiku.test_connection()
    except Exception as exc:  # noqa: BLE001 - 探测异常也要变成可读提示，不能崩界面
        return False, _short_error(exc)

