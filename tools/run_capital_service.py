#!/usr/bin/env python3
"""资金服务运维 CLI：启动薄封装 + 备份/导出/恢复/校验/预检。

服务本身由包入口 ``python -m raricy_capital``（或控制台脚本 ``raricy-capital``）
提供（``--config`` / ``--data-dir`` / ``--port`` / ``--live`` / ``--backup``；
信号处理、OS 单写者锁、装配都由 runtime 负责）；本脚本**不改写**那套契约，只做
两件事：

1. ``serve`` 原样转交给 ``raricy_capital.__main__.main``，供 systemd /
   Task Scheduler 使用同一入口；
2. 备份、导出迁移包、校验、恢复与预检——这些动作经 ``ServiceOperations`` 执行。

**单写者**：凡是会打开 SQLite 的动作（``backup`` / ``export`` / ``restore``）都先
``acquire_data_lock``（与 root 服务同一把 OS 生命周期锁）再打开数据库；服务在线时
锁被占用，动作以 ``service_running`` 明确拒绝，**绝不**绕过锁并发写。只读且不打开
数据库的动作（``status`` / ``list-backups`` / ``verify`` / ``check``）不取锁。

脚本**永不**发起真实网络请求；备份走 SQLite online backup，不复制 WAL 运行库。
服务运行期间的备份走控制台已认证的 ``POST /api/backup`` 或服务内部每小时备份；
本脚本的 ``backup`` 是**停机**用的离线一次性备份。
"""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


def _ensure_src_on_path() -> None:
    """未安装包时把仓库 ``src`` 放到导入路径最前，便于直接运行本脚本。"""
    source = Path(__file__).resolve().parents[1] / "src"
    if source.is_dir() and str(source) not in sys.path:
        sys.path.insert(0, str(source))


_ensure_src_on_path()

from raricy_capital.data_lock import DataLockError, acquire_data_lock  # noqa: E402
from raricy_capital.contracts import FundError  # noqa: E402
from raricy_capital.operations import ServiceOperations  # noqa: E402
from raricy_capital.store import FundStore  # noqa: E402

DEFAULT_DATA_DIR = "data/capital_funds"

#: 没有 ``--config`` 时的公开默认值；与根 ``FundConfig`` 保持一致。
_FALLBACK_FIELDS: dict[str, object] = {
    "host": "127.0.0.1",
    "port": 8137,
    "live": False,
    "fund_ids": ("capital1", "capital2"),
    "backup_keep": 72,
    "backup_interval_seconds": 3600,
    "log_keep": 10,
    "log_max_bytes": 10 * 1024 * 1024,
    "health_interval_seconds": 4,
    "health_failure_threshold": 3,
    "health_recovery_threshold": 1,
}


class ServiceRunning(RuntimeError):
    """数据目录已被 OS 锁占用：服务在线，离线写操作必须拒绝而不是并发写。"""


def _fallback_config(data_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(data_dir=Path(data_dir), **_FALLBACK_FIELDS)


def _load_config(args: argparse.Namespace):
    """按 ``--config`` / ``--data-dir`` 解析服务配置；缺失时用公开默认值。"""
    try:
        from raricy_capital.config import FundConfig
    except ImportError:
        # config.py 依赖 cryptography（见 pyproject 的 dependencies）。
        raise FundError("funds_extra_required") from None

    path = Path(args.config) if args.config else None
    data_dir = Path(args.data_dir) if args.data_dir else None
    return FundConfig.load(path, data_dir=data_dir)


def _resolve(args: argparse.Namespace):
    """返回 ``(data_dir, config)``；``--config`` 优先，其次 ``--data-dir``。"""
    if args.config:
        config = _load_config(args)
        return Path(config.data_dir), config
    data_dir = Path(args.data_dir or DEFAULT_DATA_DIR)
    return data_dir, _fallback_config(data_dir)


@contextmanager
def _operations(args: argparse.Namespace, *, need_store: bool):
    """构造 ``ServiceOperations``；需要数据库时先取 OS 锁再打开 SQLite。"""
    data_dir, config = _resolve(args)
    lock = None
    store = None
    try:
        if need_store:
            data_dir.mkdir(parents=True, exist_ok=True)
            try:
                lock = acquire_data_lock(data_dir)
            except DataLockError as exc:
                raise ServiceRunning(str(exc)) from None
            store = FundStore(data_dir / "funds.sqlite3")
        yield ServiceOperations(store, data_dir, config)
    finally:
        if store is not None:
            store.close()
        if lock is not None:
            lock.release()


def _cmd_backup(args: argparse.Namespace) -> int:
    with _operations(args, need_store=True) as operations:
        if args.keep is not None:
            operations.backup_keep = args.keep
        path = operations.backup(label=args.label)
        print(f"备份完成：{path}")
        print(f"sha256：{operations.verify_backup(path)['sha256']}")
    return 0


def _cmd_list_backups(args: argparse.Namespace) -> int:
    with _operations(args, need_store=False) as operations:
        rows = operations.list_backups()
        if not rows:
            print("暂无备份。")
            return 0
        for row in rows:
            print(f"{row['name']}\t{row['size_bytes']} bytes\t{row['sha256'] or '（无旁车）'}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    with _operations(args, need_store=True) as operations:
        path = operations.export_bundle(filename=args.out)
        print(f"迁移包已导出：{path}")
        print("包内不含密钥；凭据请通过独立安全通道迁移。")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    with _operations(args, need_store=False) as operations:
        summary = operations.verify_bundle(args.bundle)
        print(f"校验通过：{summary['path']}")
        print(f"成员：{', '.join(summary['members'])}")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    with _operations(args, need_store=True) as operations:
        summary = operations.restore_bundle(
            args.bundle,
            target=args.target,
            allow_existing=args.force,
        )
        print(f"已恢复到：{summary['target']}")
        for name in summary["files"]:
            print(f"  - {name}")
        print("请在确认服务已停止后再把数据库放入新的数据目录。")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    with _operations(args, need_store=False) as operations:
        for key, value in operations.status().items():
            print(f"{key}: {value}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    """预检配置：只加载 ``FundConfig``（校验字段/取值），不打开数据库、不取锁。

    注意：根 CLI **目前没有** ``--check``；该预检由本脚本提供，缺失的校验能力已
    作为请求提给 root。加载会按需创建数据目录与 ``admin.token``。
    """
    if not args.config and not args.data_dir:
        raise FundError("config_required")
    config = _load_config(args)
    print("配置合法。")
    print(f"data_dir: {config.data_dir}")
    for key, value in config.public().items():
        if key == "policies":
            print(f"policies: {', '.join(sorted(value))}")
        else:
            print(f"{key}: {value}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """原样转交包入口；入口缺席时明确失败，绝不假装启动成功。"""
    try:
        from raricy_capital.__main__ import main as funds_main
    except ImportError as exc:  # pragma: no cover - 取决于装配进度
        print(
            "找不到 raricy_capital.__main__，无法启动服务。\n"
            "请改用 `python -m raricy_capital` 并确认 runtime/CLI 已装配。\n"
            f"（{exc}）",
            file=sys.stderr,
        )
        return 2
    forwarded: list[str] = []
    if args.config:
        forwarded += ["--config", str(args.config)]
    if args.data_dir:
        forwarded += ["--data-dir", str(args.data_dir)]
    forwarded += list(args.remainder)
    # 根 CLI 的 main() 读 sys.argv（无 argv 形参）；这里只临时替换后原样调用，
    # 不复制它的参数解析，也不新建入口。
    previous = sys.argv
    sys.argv = [previous[0], *forwarded]
    try:
        return int(funds_main())
    finally:
        sys.argv = previous


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_capital_service", description=__doc__)
    parser.add_argument("--config", default=None, help="服务配置 YAML/JSON（优先于 --data-dir）")
    parser.add_argument(
        "--data-dir",
        default=None,
        help=f"资金数据目录（--config 缺席时默认 {DEFAULT_DATA_DIR}）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="转交给根 CLI 启动服务")
    serve.add_argument("remainder", nargs=argparse.REMAINDER)
    serve.set_defaults(func=_cmd_serve)

    backup = sub.add_parser("backup", help="停机状态下生成一次一致性备份并轮转")
    backup.add_argument("--label", default=None, help="备份名后缀（仅 [A-Za-z0-9._-]）")
    backup.add_argument("--keep", type=int, default=None, help="保留份数（默认取配置 72）")
    backup.set_defaults(func=_cmd_backup)

    listing = sub.add_parser("list-backups", help="列出备份与校验和（不打开数据库）")
    listing.set_defaults(func=_cmd_list_backups)

    export = sub.add_parser("export", help="导出可移植迁移包（需先停止服务）")
    export.add_argument("--out", default=None, help="包文件名（默认按时间戳）")
    export.set_defaults(func=_cmd_export)

    verify = sub.add_parser("verify", help="校验迁移包（不打开数据库）")
    verify.add_argument("bundle")
    verify.set_defaults(func=_cmd_verify)

    restore = sub.add_parser("restore", help="恢复迁移包（需先停止写者）")
    restore.add_argument("bundle")
    restore.add_argument("--target", default=None)
    restore.add_argument("--force", action="store_true", help="允许写入非空目标目录")
    restore.set_defaults(func=_cmd_restore)

    status = sub.add_parser("status", help="打印运维状态快照（不打开数据库）")
    status.set_defaults(func=_cmd_status)

    check = sub.add_parser("check", help="预检服务配置（不打开数据库、不取锁）")
    check.set_defaults(func=_cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ServiceRunning as exc:
        print(
            "服务正在运行（数据目录被 OS 锁占用），拒绝离线写操作。\n"
            "运行中的备份请用控制台已认证的 POST /api/backup 或服务内部每小时备份；"
            "导出/恢复请先停服。\n"
            f"（{exc}）",
            file=sys.stderr,
        )
        return 3
    except FundError as exc:
        print(f"操作被拒绝：{exc.code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
