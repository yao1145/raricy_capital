"""Independent Linux/Windows entry point, guarded by an OS lifetime lock.

Third-party imports (``config`` -> ``cryptography``/``yaml``, ``runtime``/``web``
-> ``aiohttp``) are deliberately deferred into the guarded ``main``/``serve``
path so a missing runtime dependency is reported through the bootstrap log
instead of surfacing as an unhandled import error on a hidden ``pythonw``
startup.  The module CLI contract (``--config`` / ``--data-dir`` / ``--port`` /
``--live`` / ``--backup``), the OS single-writer lock and the offline backup
operation are unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

from .bootstrap_logging import BootstrapLogger

_EXIT_FAILURE = 1
#: Kept referenced for the process lifetime so hidden-mode null streams are never
#: garbage-collected unclosed (that would emit a ResourceWarning).
_NULL_STREAMS: list = []


def _usable(stream) -> bool:
    return stream is not None and hasattr(stream, 'write')


def _emit(text: object, *, stream=None) -> None:
    """Best-effort console line; silent when no stream exists (``pythonw``)."""
    target = stream
    if target is None:
        target = sys.stdout if _usable(sys.stdout) else sys.stderr
    if not _usable(target):
        return
    try:
        target.write(str(text).rstrip('\n') + '\n')
        flush = getattr(target, 'flush', None)
        if callable(flush):
            flush()
    except Exception:
        pass


def _ensure_streams() -> None:
    """Replace ``None`` stdio (Windows ``pythonw``) so debug/argparse cannot crash."""
    for name in ('stdout', 'stderr'):
        if getattr(sys, name, None) is None:
            try:
                stream = open(os.devnull, 'w', encoding='utf-8')
            except OSError:
                continue
            _NULL_STREAMS.append(stream)
            setattr(sys, name, stream)


def _report(code: str) -> None:
    _emit(f'服务未启动：{code}')


def _fail(logger: BootstrapLogger, phase: str, exc: BaseException) -> int:
    code = logger.fatal(phase, exc, exit_code=_EXIT_FAILURE)
    _report(code)
    return _EXIT_FAILURE


def _run_backup(config) -> None:
    from .operations import ServiceOperations
    from .store import FundStore

    store = FundStore(config.data_dir / 'funds.sqlite3')
    try:
        result = ServiceOperations(store, config.data_dir, config).backup()
        _emit(result)
    finally:
        store.close()


async def serve(config) -> None:
    from aiohttp import web

    from .runtime import FundService
    from .web import create_app

    service = FundService(config)
    runner = web.AppRunner(create_app(service), access_log=None)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for kind in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(kind, stopped.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(kind, lambda *_: loop.call_soon_threadsafe(stopped.set))
    try:
        await runner.setup()
        await web.TCPSite(runner, config.host, config.port).start()
        await service.start()
        _emit(f'基金控制台 http://{config.host}:{config.port}/；模式：' + ('live' if config.live else '只读预览'))
        _emit(f'管理令牌保存在 {config.data_dir / "admin.token"}，不要发到聊天中。')
        await stopped.wait()
    finally:
        await runner.cleanup()
        await service.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='capital1 / capital2 独立基金服务')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--port', type=int)
    parser.add_argument('--live', action='store_true', help='允许真实站点私聊、资金转账和模拟交易')
    parser.add_argument('--backup', action='store_true', help='停机状态下备份后退出')
    return parser


def main() -> int:
    _ensure_streams()
    try:
        args = _build_parser().parse_args()
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return 0 if exc.code is None else _EXIT_FAILURE

    logger = BootstrapLogger.for_startup(data_dir=args.data_dir)

    try:
        from .config import FundConfig
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # runtime dependency missing / broken install
        return _fail(logger, 'import', exc)

    try:
        config = FundConfig.load(args.config, data_dir=args.data_dir, live=args.live, port=args.port)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        return _fail(logger, 'config', exc)

    logger.retarget(config.data_dir)

    phase = 'lock'
    try:
        from .data_lock import acquire_data_lock

        with acquire_data_lock(config.data_dir):
            if args.backup:
                phase = 'backup'
                _run_backup(config)
            else:
                phase = 'serve'
                logger.report('startup', code='startup_attempt', level='info')
                asyncio.run(serve(config))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        return _fail(logger, phase, exc)


if __name__ == '__main__':
    raise SystemExit(main())
