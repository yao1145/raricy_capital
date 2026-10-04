"""Independent Linux/Windows entry point, guarded by an OS lifetime lock."""
from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path

from aiohttp import web

from .data_lock import acquire_data_lock, DataLockError
from .config import FundConfig
from .contracts import FundError


async def serve(config: FundConfig) -> None:
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
        print(f'基金控制台 http://{config.host}:{config.port}/；模式：' + ('live' if config.live else '只读预览'), flush=True)
        print(f'管理令牌保存在 {config.data_dir / "admin.token"}，不要发到聊天中。', flush=True)
        await stopped.wait()
    finally:
        await runner.cleanup()
        await service.close()


def main() -> int:
    parser = argparse.ArgumentParser(description='capital1 / capital2 独立基金服务')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--port', type=int)
    parser.add_argument('--live', action='store_true', help='允许真实站点私聊、资金转账和模拟交易')
    parser.add_argument('--backup', action='store_true', help='停机状态下备份后退出')
    args = parser.parse_args()
    try:
        config = FundConfig.load(args.config, data_dir=args.data_dir, live=args.live, port=args.port)
        with acquire_data_lock(config.data_dir):
            if args.backup:
                from .operations import ServiceOperations
                from .store import FundStore
                store = FundStore(config.data_dir / 'funds.sqlite3')
                try:
                    result = ServiceOperations(store, config.data_dir, config).backup()
                    print(result)
                finally:
                    store.close()
            else:
                asyncio.run(serve(config))
        return 0
    except (FundError, DataLockError) as exc:
        print(f'服务未启动：{getattr(exc, "code", str(exc))}')
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
