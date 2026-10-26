from loguru import logger
from tqdm import tqdm
import sys

try:  # 发行包：日志写在程序目录下随包自带的 chaoxing.log
    from paths import log_path as _log_path
except Exception:  # noqa: BLE001  api/ 单独使用时退化为当前目录
    def _log_path() -> str:
        return "chaoxing.log"

try:  # GUI 打包环境（--windowed）下 sys.stderr 为 None
    from safeio import ensure_stdio, safe_stream
except Exception:  # noqa: BLE001  safeio 缺失时自身保证可用
    def ensure_stdio() -> None:
        pass

    def safe_stream(*streams):
        for stream in streams:
            if stream is not None:
                return stream
        return None

ensure_stdio()
tqdm_stream = safe_stream(sys.stderr, sys.stdout)


def tqdm_sink(msg):
    stream = tqdm_stream if tqdm_stream is not None else None
    try:
        tqdm.write(msg.rstrip(), file=stream)
    except Exception:  # noqa: BLE001  写失败不得影响业务线程
        pass

logger.remove()
logger.add(tqdm_sink, colorize=True, enqueue=True)

_LOG_FILE = _log_path()
try:
    logger.add(_LOG_FILE, rotation="10 MB", level="TRACE")
except Exception:  # noqa: BLE001  日志不可写不应影响主流程
    logger.add("chaoxing.log", rotation="10 MB", level="TRACE")
