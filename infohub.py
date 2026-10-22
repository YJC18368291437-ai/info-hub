"""Local QQ collection service. Email is gated; see docs/acceptance-report.md."""
import logging
import os
import socket

from nonebot import get_driver, init, load_plugin, run
from nonebot.adapters.onebot.v11 import Adapter


def _port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


from pathlib import Path
from info_hub.process_lock import ProcessLock, process_image
_ROOT = Path(__file__).resolve().parent
os.chdir(_ROOT)
if (_ROOT / 'data' / 'PAUSED').exists():
    raise SystemExit('info-hub paused; use explicit start script to resume')
_lock = ProcessLock(_ROOT / 'data' / 'run.lock').acquire()
import atexit
atexit.register(_lock.close)
from info_hub.settings import get_settings
_settings = get_settings()  # Validate BEFORE the server can collect/connect.
init(host='127.0.0.1',log_level='WARNING')  # do not log incoming private QQ bodies
_port = int(os.getenv("PORT", "8082"))
if _port_in_use(_port):
    print(f"端口 {_port} 已被占用：info-hub 可能已在运行，本进程退出。")
    raise SystemExit(2)

_driver = get_driver()
_driver.register_adapter(Adapter)

@_driver.on_bot_connect
async def _connected(bot):
    import json, time
    if str(bot.self_id) != _settings.self_qq:
        logging.getLogger('info_hub').error('Rejected unexpected OneBot identity')
        return
    # This evidence proves adapter self_id, not just an open TCP port.
    (_ROOT / 'data' / 'onebot-status.json').write_text(json.dumps({
        'self_id': str(bot.self_id), 'connected': True, 'port': 8082,
        'connected_at': int(time.time())}),encoding='utf-8')
    try:
        from info_hub.qq_collect import dump_group_list
        present = await dump_group_list(bot)
        logging.getLogger('info_hub').info('群列表诊断完成: %s', present)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger('info_hub').warning('群列表诊断失败: %s', type(exc).__name__)

@_driver.on_bot_disconnect
async def _disconnected(bot):
    import json
    if str(bot.self_id) == _settings.self_qq:
        (_ROOT / 'data' / 'onebot-status.json').write_text(json.dumps({
            'self_id': str(bot.self_id), 'connected': False, 'port': 8082}),encoding='utf-8')

# 把 info_hub.* 的标准库日志转发到 nonebot 的 loguru，便于统一落盘查看
from nonebot.log import logger as _nb_logger  # noqa: E402

try:
    import pathlib as _pl

    _pl.Path("data").mkdir(exist_ok=True)
    _nb_logger.add("data/info_hub.log", rotation="5 MB", retention=3, encoding="utf-8", level="INFO",
                   filter=lambda record: record['extra'].get('info_hub',False))
except Exception:  # noqa: BLE001
    pass


class _ForwardHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            from info_hub.redaction import redact
            message=redact(record.getMessage(),(_settings.mail_auth_code,_settings.deepseek_api_key,_settings.tg_token))
            _nb_logger.bind(info_hub=True).opt(depth=6).log(record.levelname, message)
        except Exception:  # noqa: BLE001
            pass


_hub_logger = logging.getLogger("info_hub")
_hub_logger.setLevel(logging.INFO)
_hub_logger.addHandler(_ForwardHandler())

# 也把 PTB 的 telegram 日志转发出来（便于看按钮回调报错）
_tg_logger = logging.getLogger("telegram")
_tg_logger.setLevel(logging.INFO)
_tg_logger.addHandler(_ForwardHandler())

from info_hub import runtime  # noqa: E402
import json
import time
(_ROOT / 'data' / 'run.pid.json').write_text(json.dumps({'pid': os.getpid(),
    'entry': str(_ROOT / 'infohub.py'), 'image': process_image(), 'started': time.time()}),encoding='utf-8')

_driver.on_startup(runtime.startup)
_driver.on_shutdown(runtime.shutdown)

load_plugin("info_hub.qq_collect")

if __name__ == "__main__":
    run()
