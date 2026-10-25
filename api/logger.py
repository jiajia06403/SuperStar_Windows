from loguru import logger
from tqdm import tqdm
import sys

try:  # 发行包：日志写在程序目录下随包自带的 chaoxing.log
    from paths import log_path as _log_path
except Exception:  # noqa: BLE001  api/ 单独使用时退化为当前目录
    def _log_path() -> str:
        return "chaoxing.log"

tqdm_stream = sys.stderr

def tqdm_sink(msg):
    tqdm.write(msg.rstrip(), file=tqdm_stream)
    tqdm_stream.flush()

logger.remove()
logger.add(tqdm_sink, colorize=True, enqueue=True)

_LOG_FILE = _log_path()
try:
    logger.add(_LOG_FILE, rotation="10 MB", level="TRACE")
except Exception:  # noqa: BLE001  日志不可写不应影响主流程
    logger.add("chaoxing.log", rotation="10 MB", level="TRACE")
