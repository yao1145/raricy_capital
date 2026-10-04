"""F：运维、备份轮转、健康探测与可移植迁移包（``ServiceOperations``）。

本模块只做运维面的事情，**不碰钱、不碰份额**：金额与份额一律留在
``ledger.py`` / ``store.py`` 里，这里只读写文件、SQLite online backup 与
持久化事件。金额若出现在事件详情里，也只是原样透传的整数（``*_units``），
本模块不做任何浮点累加。

设计要点（对应计划 F 段与 v0.3）：

* **单写者**：运行中的进程由入口在打开 SQLite **之前**取得的 OS 生命周期锁标记
  （``raricy_capital.data_lock.acquire_data_lock``，锁文件 ``.raricy-data.lock``）：锁随
  进程退出/崩溃由内核释放，不存在需要人工清理的陈旧锁。迁移（``restore_bundle``）
  只允许在**目标**数据目录无人持锁时执行；``writer_active`` 用同一把 OS 锁做只读
  探测，不夺锁、不改写持锁者的锁文件内容。
* **备份**：调用 ``FundStore.backup``（SQLite online backup API）生成一致快照，
  **绝不**直接复制正在使用的 WAL 运行库；备份按数量轮转，每份带 sha256 旁车文件。
* **迁移包**：zip 内只允许固定成员名（清单、数据库、脱敏公开配置、说明）。解包
  不调用 ``extract``/``extractall``，逐个成员做路径穿越、符号链接、压缩炸弹与
  sha256 校验后才落盘；包内**不含任何密钥**，凭据通过独立安全通道迁移。
* **离线通知**：站点不可达时无法经同一条链路即时发出站内消息，因此断线/恢复
  通知先写入 ``notices`` outbox（``user_id`` 为空表示按持有人展开），恢复后由
  消息 worker 补发。代码另有可选的 ``alert_sender`` 钩子，但**没有任何部署会配置
  它**，也**不假装**离线时能送达站内。
* **日志**：滚动脱敏的结构化 JSONL（事件名 / 事件 ID / 基金 ID / 安全堆栈），
  密码、Cookie、令牌、会话等按键名整体剔除。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import uuid
import zipfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .data_lock import DATA_LOCK_FILE, DataLock, DataLockError, acquire_data_lock
from .safe_logging import safe_stack
from .contracts import POLICIES, FundError, now_ms as _now_ms

# --- 迁移包常量 -------------------------------------------------------------

BUNDLE_FORMAT = "raricy.funds.bundle"
BUNDLE_SCHEMA = 1
MANIFEST_NAME = "manifest.json"
DB_MEMBER = "data/funds.sqlite3"
CONFIG_MEMBER = "config/public.json"
README_MEMBER = "README.txt"

#: 迁移包允许出现的**全部**成员；白名单之外一律拒绝（不解包）。
BUNDLE_MEMBERS: frozenset[str] = frozenset({MANIFEST_NAME, DB_MEMBER, CONFIG_MEMBER, README_MEMBER})

BACKUP_SUFFIX = ".sqlite3"
CHECKSUM_SUFFIX = ".sha256"

#: 健康探测的公开默认值：4 秒一次、连续 3 次失败判定为断线、一次成功即恢复。
DEFAULT_HEALTH_INTERVAL_SECONDS = 4
DEFAULT_HEALTH_FAILURE_THRESHOLD = 3
DEFAULT_HEALTH_RECOVERY_THRESHOLD = 1

#: 迁移包的硬上限，防止压缩炸弹与超大包。
DEFAULT_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_COMPRESSION_RATIO = 200.0
DEFAULT_MAX_MEMBERS = 16

#: ``ServiceOperations(config)`` 读取的公开字段。config 由 runtime/根 CLI 装配时
#: 加入这些字段；缺失时全部有默认值，运维模块不因缺字段而崩。默认值与根
#: ``FundConfig`` 一致（``backup_keep`` / ``backup_interval_seconds`` 对应其
#: ``backup_retention`` / ``backup_interval_seconds`` 属性）。
CONFIG_FIELDS: dict[str, object] = {
    "host": "127.0.0.1",
    "port": 8137,
    "live": False,
    "fund_ids": tuple(POLICIES),
    "backup_keep": 72,
    "backup_interval_seconds": 3600,
    "log_keep": 10,
    "log_max_bytes": 10 * 1024 * 1024,
    "health_interval_seconds": DEFAULT_HEALTH_INTERVAL_SECONDS,
    "health_failure_threshold": DEFAULT_HEALTH_FAILURE_THRESHOLD,
    "health_recovery_threshold": DEFAULT_HEALTH_RECOVERY_THRESHOLD,
    "max_uncompressed_bytes": DEFAULT_MAX_UNCOMPRESSED_BYTES,
}

#: 按键名整体剔除的字段：绝不因「字段名合法」就把它写进日志或事件详情。
_BLOCKED_KEY = re.compile(
    r"pass|passwd|secret|token|cookie|auth|credential|session|api[_-]?key|private", re.I
)

_EVENT_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,64}\Z")
_LABEL_RE = re.compile(r"\A[A-Za-z0-9._-]{1,32}\Z")
_MAX_STRING = 512
_LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})


def _pick(config: object, name: str, default: object = None) -> Any:
    """从 mapping 或对象式 config 读取字段；两者都支持，缺省时回退到默认值。"""
    if config is None:
        return default
    if isinstance(config, Mapping):
        return config.get(name, default)
    return getattr(config, name, default)


def sha256_file(path: Path) -> str:
    """流式计算文件 sha256；不把整份文件读进内存。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """对文本（UTF-8）求 sha256；测试与校验用。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_token(value: object, default: str = "unknown") -> str:
    """把任意值收敛成受控标识；不合法就退回默认值。"""
    if isinstance(value, str) and _EVENT_RE.match(value):
        return value
    return default


def _safe_label(value: object) -> str | None:
    if isinstance(value, str) and _LABEL_RE.match(value):
        return value
    return None


def _safe_member_name(name: object) -> str:
    """校验迁移包成员名：拒绝绝对路径、盘符、反斜杠、``..`` 与控制字符。"""
    if not isinstance(name, str) or not name:
        raise FundError("bundle_member_invalid")
    if name.startswith("/") or name.startswith("\\") or "\\" in name:
        raise FundError("bundle_member_invalid")
    if ":" in name:
        raise FundError("bundle_member_invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in name):
        raise FundError("bundle_member_invalid")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise FundError("bundle_member_invalid")
    return name


def _timestamp_name(created_ms: int) -> str:
    moment = datetime.fromtimestamp(created_ms / 1000, timezone.utc)
    return moment.strftime("%Y%m%d-%H%M%S")


def utc_text(ms: int) -> str:
    """毫秒时间戳 → 带 ``Z`` 的 UTC 文本；事件与清单用。"""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class ServiceOperations:
    """资金服务的运维面：健康探测、事件日志、备份轮转与可移植迁移。"""

    def __init__(self, store: object, root: Path | str, config: object = None) -> None:
        self.store = store
        self.config = config
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

        self.data_dir = Path(_pick(config, "data_dir", self.root) or self.root)
        self.backup_dir = Path(_pick(config, "backup_dir", self.root / "backups") or (self.root / "backups"))
        self.export_dir = Path(_pick(config, "export_dir", self.root / "exports") or (self.root / "exports"))
        self.restore_dir = Path(_pick(config, "restore_dir", self.root / "restore") or (self.root / "restore"))
        self.log_dir = Path(_pick(config, "log_dir", self.root / "logs") or (self.root / "logs"))

        self.backup_keep = max(1, int(_pick(config, "backup_keep", 72)))
        self.backup_interval_seconds = int(_pick(config, "backup_interval_seconds", 3600))
        self.log_keep = max(1, int(_pick(config, "log_keep", 10)))
        self.log_max_bytes = max(4096, int(_pick(config, "log_max_bytes", 10 * 1024 * 1024)))
        self.health_interval_seconds = int(
            _pick(config, "health_interval_seconds", DEFAULT_HEALTH_INTERVAL_SECONDS)
        )
        self.health_failure_threshold = max(
            1, int(_pick(config, "health_failure_threshold", DEFAULT_HEALTH_FAILURE_THRESHOLD))
        )
        self.health_recovery_threshold = max(
            1, int(_pick(config, "health_recovery_threshold", DEFAULT_HEALTH_RECOVERY_THRESHOLD))
        )
        self.max_uncompressed_bytes = int(
            _pick(config, "max_uncompressed_bytes", DEFAULT_MAX_UNCOMPRESSED_BYTES)
        )
        fund_ids = _pick(config, "fund_ids", tuple(POLICIES)) or ()
        self.fund_ids = tuple(str(item) for item in fund_ids)

        self._log_lock = threading.RLock()
        # 本实例持有的 OS 数据档案锁（``acquire_writer_lock`` 取得、``release`` 释放）。
        # root 服务在打开 SQLite 之前已持锁时本字段为 None，但同一把 OS 锁仍会
        # 让 ``writer_active()`` 探测为真。
        self._data_lock: DataLock | None = None
        # 进程内最近一次健康状态；持久副本在 store 的 ``ops`` namespace。
        self._health: dict[str, Any] | None = None

    # ------------------------------------------------------------------ 配置
    @staticmethod
    def config_fields() -> dict[str, object]:
        """公开的配置字段清单（字段名 → 默认值），供根 CLI 装配时对齐。"""
        return dict(CONFIG_FIELDS)

    def public_config(self) -> dict[str, Any]:
        """可写进迁移包的脱敏公开配置；**永不含密钥、令牌或会话**。

        优先使用 config 自己提供的 ``public()``/``public_dict()``（若存在），
        否则从已知公开字段拼装。任何结果都会再过一遍脱敏。
        """
        source: object = self.config
        for attribute in ("public", "public_dict"):
            provider = getattr(self.config, attribute, None)
            if callable(provider):
                source = provider()
                break
        if isinstance(source, Mapping):
            raw = {str(key): source[key] for key in source}
        else:
            raw = {name: _pick(self.config, name, default) for name, default in CONFIG_FIELDS.items()}
        return _scrub_mapping(raw)

    # -------------------------------------------------------------- 写者锁
    def writer_lock_path(self) -> Path:
        """数据档案 OS 锁文件路径；锁由内核按进程生命周期持有，文件只作诊断。"""
        return self.data_dir / DATA_LOCK_FILE

    def writer_active(self) -> bool:
        """是否有写者持有数据目录的 OS 锁；只读探测，不破坏持锁者。

        本实例已持锁时直接为真；否则尝试以非阻塞方式取得**同一把** OS 锁 ——
        取得即说明当前无人写入，立刻释放并返回假；失败（含目录不可用、读不到）
        返回真，宁可把「判定不了」当成有写者，也不把运行中的服务误判成已停止。
        探测只新开一个句柄，既不改写锁文件内容，也不释放持锁者的锁。
        """
        if self._data_lock is not None:
            return True
        return _data_dir_locked(self.data_dir)

    def acquire_writer_lock(self, holder: str = "service", *, force: bool = False) -> None:
        """取得数据目录的 OS 生命周期锁，持有到本实例 ``release_writer_lock``。

        锁由 ``raricy_capital.data_lock.acquire_data_lock`` 提供：崩溃后由内核释放，
        不存在需要 ``force`` 的陈旧锁。``force=True`` 因此被明确拒绝，而不是假装
        能夺走别人的锁或忽略它；已被占用或本实例已持锁时抛 ``writer_active``。
        ``holder`` 只作调用方语义标注，OS 锁的属主诊断由 data_lock 自行写入。
        """
        del holder  # OS 锁的属主信息由 data_lock 写入，这里不接受任意属主名
        if self._data_lock is not None:
            raise FundError("writer_active")
        if force:
            raise FundError("writer_lock_force_unsupported")
        try:
            lock = acquire_data_lock(self.data_dir)
        except DataLockError:
            raise FundError("writer_active") from None
        self._data_lock = lock

    def release_writer_lock(self) -> None:
        """释放本实例持有的 OS 锁并幂等清空；不影响其他进程持有的锁。"""
        lock, self._data_lock = self._data_lock, None
        if lock is not None:
            lock.release()

    # ------------------------------------------------------------ 健康探测
    def health_tick(self, now_ms: int, success: bool, error: object = None) -> dict[str, Any]:
        """记录一次站点探测结果，返回当前网络健康快照。

        判定是**敏感**的：连续 ``health_failure_threshold``（默认 3）次失败即判为
        断线，一次成功即恢复。发生状态跃迁时：

        * 断线：先写事件，再把断线通知写入 ``notices`` outbox（``user_id`` 为空，
          表示按持有人展开）。**离线时无法经同一条链路即时发出站内消息**，所以
          这条通知只能在恢复后补发；可选的独立告警出口(``alert_sender``)是唯一
          可能在离线期间送达的通道，但它不是站内消息。
        * 恢复：写恢复事件，并把「断线 + 恢复」摘要一并入队，由消息 worker 补发。

        返回的字典只含状态量与计数，不含异常正文。
        """
        state = self._load_health()
        transition: str | None = None
        event_id: str | None = None
        # 恢复跃迁会把 outage_started_ms 清零，因此时长在清零前先记进局部变量。
        outage_ms_value: int | None = None

        if success:
            state["consecutive_successes"] = int(state.get("consecutive_successes", 0)) + 1
            state["consecutive_failures"] = 0
            state["last_ok_ms"] = now_ms
            state["last_error"] = None
            previous = state.get("network")
            if previous == "down":
                if state["consecutive_successes"] >= self.health_recovery_threshold:
                    transition = "recovered"
                    state["network"] = "up"
                    outage_started = state.get("outage_started_ms")
                    outage_ms_value = max(0, now_ms - int(outage_started)) if outage_started else 0
                    event_id = self._record(
                        "ops.network_recovered",
                        level="info",
                        details={
                            "outage_ms": outage_ms_value,
                            "failures": int(state.get("outage_failures", 0)),
                        },
                        event_key=f"ops.network_recovered:{now_ms}",
                        created_ms=now_ms,
                    )
                    self._enqueue_notice(
                        None,
                        self._recovery_text(now_ms, outage_started),
                        now_ms,
                        event_id,
                        kind="network_recovered",
                    )
                    state["outage_started_ms"] = None
                    state["outage_failures"] = 0
            else:
                state["network"] = "up"
        else:
            state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
            state["consecutive_successes"] = 0
            state["last_error"] = _classify_error(error)
            if state.get("network") != "down" and state["consecutive_failures"] >= self.health_failure_threshold:
                transition = "outage"
                state["network"] = "down"
                state["outage_started_ms"] = now_ms
                state["outage_failures"] = state["consecutive_failures"]
                event_id = self._record(
                    "ops.network_down",
                    level="error",
                    details={
                        "failures": int(state["consecutive_failures"]),
                        "error": state["last_error"],
                        "interval_seconds": self.health_interval_seconds,
                    },
                    event_key=f"ops.network_down:{now_ms}",
                    created_ms=now_ms,
                )
                self._alert(self._outage_text(now_ms))
                self._enqueue_notice(
                    None, self._outage_text(now_ms), now_ms, event_id, kind="network_down"
                )

        state["updated_ms"] = now_ms
        if state.get("network") not in ("up", "down"):
            state["network"] = "unknown"
        self._save_health(state, now_ms)

        outage_started = state.get("outage_started_ms")
        return {
            "healthy": state.get("network") == "up",
            "network": state.get("network", "unknown"),
            "transition": transition,
            "consecutive_failures": int(state.get("consecutive_failures", 0)),
            "consecutive_successes": int(state.get("consecutive_successes", 0)),
            "error": state.get("last_error"),
            "outage_started_ms": outage_started,
            "outage_ms": outage_ms_value
            if outage_ms_value is not None
            else ((now_ms - int(outage_started)) if outage_started else None),
            "checked_ms": now_ms,
            "event_id": event_id,
            "threshold": self.health_failure_threshold,
            "interval_seconds": self.health_interval_seconds,
        }

    def _outage_text(self, now_ms: int) -> str:
        return (
            "【资金服务】检测到站点连接中断"
            f"（连续 {self.health_failure_threshold} 次探测失败）。"
            "服务已进入离线状态，期间不发起外部写操作。"
            "本通知在恢复后补发；离线期间无法通过站内消息即时送达。"
        )

    def _recovery_text(self, now_ms: int, outage_started: object) -> str:
        seconds = int(max(0, now_ms - int(outage_started)) / 1000) if outage_started else 0
        return (
            "【资金服务】站点连接已恢复，离线约 "
            f"{seconds} 秒。服务将先对账再继续；请用 /check 核对本人份额。"
        )

    # ------------------------------------------------------------- 事件记录
    def record_event(
        self,
        event: str,
        *,
        fund_id: str | None = None,
        level: str = "info",
        details: Mapping[str, Any] | None = None,
        error: object = None,
        event_key: str | None = None,
        created_ms: int | None = None,
    ) -> None:
        """写一条脱敏事件：持久化到 store 并追加到滚动 JSONL 日志。

        保持冻结签名返回 ``None``；需要事件 ID 的内部调用请用 ``_record``。
        """
        self._record(
            event,
            fund_id=fund_id,
            level=level,
            details=details,
            error=error,
            event_key=event_key,
            created_ms=created_ms,
        )

    def _record(
        self,
        event: str,
        *,
        fund_id: str | None = None,
        level: str = "info",
        details: Mapping[str, Any] | None = None,
        error: object = None,
        event_key: str | None = None,
        created_ms: int | None = None,
    ) -> str:
        moment = int(created_ms if created_ms is not None else _now_ms())
        if event_key:
            identifier = _safe_token(event_key, default="")
            if not identifier:
                # 过长的幂等键哈希成稳定短标识，绝不静默退化成 "unknown" 造成碰撞。
                identifier = "evt-" + hashlib.sha256(str(event_key).encode("utf-8")).hexdigest()[:16]
        else:
            identifier = f"evt-{uuid.uuid4().hex[:16]}"
        name = _safe_token(event, default="invalid")
        safe_level = level if level in _LOG_LEVELS else "info"
        payload: dict[str, Any] = dict(_scrub_mapping(details or {}))
        stack = safe_stack(error) if isinstance(error, BaseException) else None
        if stack:
            payload["stack"] = stack
        if isinstance(error, BaseException):
            payload.setdefault("error", type(error).__name__)
        elif isinstance(error, str):
            payload.setdefault("error", _classify_error(error))

        try:
            self.store.append_event(
                name,
                fund_id=fund_id,
                level=safe_level,
                details=payload,
                event_key=identifier,
                created_ms=moment,
            )
        except Exception:
            # 事件落库失败不应阻断运维动作；日志文件仍保留可诊断线索。
            self._append_log(
                {
                    "ts": utc_text(moment),
                    "event": name,
                    "event_id": identifier,
                    "fund_id": fund_id,
                    "level": safe_level,
                    "details": payload,
                    "store_error": True,
                }
            )
            raise
        self._append_log(
            {
                "ts": utc_text(moment),
                "event": name,
                "event_id": identifier,
                "fund_id": fund_id,
                "level": safe_level,
                "details": payload,
            }
        )
        return identifier

    def recent_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        """读取最近若干条已脱敏日志行；损坏行跳过，绝不因为一行坏掉而中断。"""
        path = self._log_path()
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        records: list[dict[str, Any]] = []
        for line in lines[-max(1, int(limit)) :]:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records

    # -------------------------------------------------------------- 滚动日志
    def _log_path(self) -> Path:
        return self.log_dir / "funds-ops.jsonl"

    def _rotated_path(self, index: int) -> Path:
        return Path(f"{self._log_path()}.{index}")

    def _append_log(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        encoded = line.encode("utf-8")
        with self._log_lock:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            path = self._log_path()
            if path.exists() and path.stat().st_size + len(encoded) > self.log_max_bytes:
                self._rotate_logs()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def _rotate_logs(self) -> None:
        """按份数轮转：``funds-ops.jsonl`` → ``.1`` → ``.2`` …，超出的删除。"""
        with _suppress_oserror():
            oldest = self._rotated_path(self.log_keep)
            if oldest.exists():
                oldest.unlink()
        for index in range(self.log_keep - 1, 0, -1):
            source = self._rotated_path(index)
            if source.exists():
                with _suppress_oserror():
                    os.replace(source, self._rotated_path(index + 1))
        base = self._log_path()
        if base.exists():
            with _suppress_oserror():
                os.replace(base, self._rotated_path(1))

    # ------------------------------------------------------------ 备份与轮转
    def backup(self, *, label: str | None = None, created_ms: int | None = None) -> Path:
        """生成一致性 SQLite 快照并轮转旧备份，返回新备份路径。

        使用 ``FundStore.backup``（online backup API），**不复制正在使用的 WAL**。
        先写 ``ops.backup.started`` 意图事件，再落盘，最后写完成事件。
        """
        moment = int(created_ms if created_ms is not None else _now_ms())
        name = f"funds-{_timestamp_name(moment)}"
        safe_label = _safe_label(label)
        if safe_label:
            name += f"-{safe_label}"
        name += BACKUP_SUFFIX

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        destination = self._unique_path(self.backup_dir, name)
        self._record(
            "ops.backup.started",
            details={"file": destination.name, "intent": True},
            event_key=f"ops.backup.started:{destination.name}",
            created_ms=moment,
        )
        self.store.backup(destination)
        digest = sha256_file(destination)
        sidecar = destination.with_name(destination.name + CHECKSUM_SUFFIX)
        sidecar.write_text(f"{digest}  {destination.name}\n", encoding="utf-8")
        self._rotate_backups()
        self._record(
            "ops.backup.completed",
            details={
                "file": destination.name,
                "size_bytes": destination.stat().st_size,
                "sha256": digest,
            },
            event_key=f"ops.backup.completed:{destination.name}",
            created_ms=moment,
        )
        return destination

    def list_backups(self) -> list[dict[str, Any]]:
        """按新→旧列出备份；每条含名称、路径、字节数与旁车 sha256。"""
        if not self.backup_dir.exists():
            return []
        results: list[dict[str, Any]] = []
        for path in sorted(self._backup_files(), key=lambda item: (item.stat().st_mtime, item.name), reverse=True):
            sidecar = path.with_name(path.name + CHECKSUM_SUFFIX)
            digest = None
            if sidecar.exists():
                digest = sidecar.read_text(encoding="utf-8").split()[0] if sidecar.stat().st_size else None
            stat_result = path.stat()
            results.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "size_bytes": stat_result.st_size,
                    "created_ms": int(stat_result.st_mtime * 1000),
                    "sha256": digest,
                }
            )
        return results

    def latest_backup(self) -> Path | None:
        backups = self.list_backups()
        return Path(backups[0]["path"]) if backups else None

    def verify_backup(self, backup: Path | str) -> dict[str, Any]:
        """核对单份备份与其旁车 sha256；缺失旁车视为不通过。"""
        path = self._resolve_existing(self.backup_dir, backup)
        if not path.exists():
            raise FundError("backup_missing")
        sidecar = path.with_name(path.name + CHECKSUM_SUFFIX)
        expected = None
        if sidecar.exists() and sidecar.stat().st_size:
            expected = sidecar.read_text(encoding="utf-8").split()[0]
        actual = sha256_file(path)
        return {
            "ok": expected is not None and expected == actual,
            "path": str(path),
            "sha256": actual,
            "expected": expected,
        }

    def _backup_files(self) -> list[Path]:
        if not self.backup_dir.exists():
            return []
        return list(self.backup_dir.glob(f"funds-*{BACKUP_SUFFIX}"))

    def _rotate_backups(self) -> None:
        """只保留最近 ``backup_keep`` 份，连同旁车一起删除；不越出备份目录。"""
        backups = sorted(
            self._backup_files(), key=lambda item: (item.stat().st_mtime, item.name), reverse=True
        )
        for stale in backups[self.backup_keep :]:
            with _suppress_oserror():
                stale.unlink()
            with _suppress_oserror():
                stale.with_name(stale.name + CHECKSUM_SUFFIX).unlink()
        for sidecar in self.backup_dir.glob(f"funds-*{BACKUP_SUFFIX}{CHECKSUM_SUFFIX}"):
            database = sidecar.with_name(sidecar.name[: -len(CHECKSUM_SUFFIX)])
            if not database.exists():
                with _suppress_oserror():
                    sidecar.unlink()

    # ---------------------------------------------------------------- 迁移包
    def export_bundle(
        self,
        filename: str | None = None,
        *,
        created_ms: int | None = None,
    ) -> Path:
        """导出可移植迁移包（zip）：数据库快照 + 脱敏公开配置 + 清单。

        包内**不含密钥、Cookie、会话或环境变量值**（配置公开字段之外的凭据需另行
        通过安全通道迁移）。每个成员带 sha256，整包另带 ``.sha256`` 旁车。写入
        采用先落临时文件再改名，中途失败不会留下半包。
        """
        moment = int(created_ms if created_ms is not None else _now_ms())
        name = filename or f"funds-bundle-{_timestamp_name(moment)}.zip"
        if Path(name).name != name or not name.endswith(".zip"):
            raise FundError("invalid_name")
        self.export_dir.mkdir(parents=True, exist_ok=True)
        destination = self._unique_path(self.export_dir, name)

        self._record(
            "ops.export.started",
            details={"file": destination.name, "intent": True},
            event_key=f"ops.export.started:{destination.name}",
            created_ms=moment,
        )

        staging = self.export_dir / f".staging-{uuid.uuid4().hex}"
        staging.mkdir(parents=True, exist_ok=False)
        try:
            database = staging / "funds.sqlite3"
            self.store.backup(database)
            config_bytes = json.dumps(
                self.public_config(), ensure_ascii=False, sort_keys=True, indent=2
            ).encode("utf-8")
            readme_bytes = (
                "raricy 资金服务迁移包。\n"
                "不含密钥、Cookie 或会话；凭据请通过独立安全通道迁移。\n"
                f"格式 {BUNDLE_FORMAT} schema {BUNDLE_SCHEMA}，创建于 {utc_text(moment)}。\n"
            ).encode("utf-8")

            members = [
                {"name": DB_MEMBER, "size": database.stat().st_size, "sha256": sha256_file(database)},
                {"name": CONFIG_MEMBER, "size": len(config_bytes), "sha256": _sha256_bytes(config_bytes)},
                {"name": README_MEMBER, "size": len(readme_bytes), "sha256": _sha256_bytes(readme_bytes)},
            ]
            manifest = {
                "format": BUNDLE_FORMAT,
                "schema_version": BUNDLE_SCHEMA,
                "created_ms": moment,
                "created_utc": utc_text(moment),
                "fund_ids": list(self.fund_ids),
                "members": members,
                "notes": "no credentials; migrate environment file separately",
            }
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")

            temporary = destination.with_name(destination.name + ".part")
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                _write_member(archive, MANIFEST_NAME, manifest_bytes)
                _write_member(archive, DB_MEMBER, database.read_bytes())
                _write_member(archive, CONFIG_MEMBER, config_bytes)
                _write_member(archive, README_MEMBER, readme_bytes)
            os.replace(temporary, destination)
        finally:
            _remove_tree(staging)

        digest = sha256_file(destination)
        destination.with_name(destination.name + CHECKSUM_SUFFIX).write_text(
            f"{digest}  {destination.name}\n", encoding="utf-8"
        )
        self._record(
            "ops.export.completed",
            details={
                "file": destination.name,
                "size_bytes": destination.stat().st_size,
                "sha256": digest,
                "members": len(BUNDLE_MEMBERS),
            },
            event_key=f"ops.export.completed:{destination.name}",
            created_ms=moment,
        )
        return destination

    def verify_bundle(self, bundle: Path | str) -> dict[str, Any]:
        """校验迁移包：成员白名单、路径安全、大小上限与逐成员 sha256。

        不解包、不写盘；只读 zip 内容后返回摘要。任何一条不通过都抛 ``FundError``。
        """
        path = self._resolve_existing(self.export_dir, bundle)
        if not path.exists():
            raise FundError("bundle_missing")
        self._verify_sidecar(path)

        try:
            with zipfile.ZipFile(path, "r") as archive:
                infos = archive.infolist()
                if len(infos) > DEFAULT_MAX_MEMBERS:
                    raise FundError("bundle_too_many_members")
                seen: dict[str, zipfile.ZipInfo] = {}
                total = 0
                for info in infos:
                    member = _safe_member_name(info.filename)
                    if member not in BUNDLE_MEMBERS:
                        raise FundError("bundle_member_disallowed")
                    if info.is_dir():
                        raise FundError("bundle_member_disallowed")
                    mode = info.external_attr >> 16
                    if mode and stat.S_ISLNK(mode):
                        raise FundError("bundle_symlink")
                    if member in seen:
                        raise FundError("bundle_duplicate_member")
                    seen[member] = info
                    total += info.file_size
                if MANIFEST_NAME not in seen:
                    raise FundError("bundle_manifest_missing")
                if DB_MEMBER not in seen or CONFIG_MEMBER not in seen:
                    raise FundError("bundle_missing_member")
                if total > self.max_uncompressed_bytes:
                    raise FundError("bundle_too_large")
                bundle_size = path.stat().st_size
                if bundle_size and total / bundle_size > DEFAULT_MAX_COMPRESSION_RATIO:
                    raise FundError("bundle_ratio")

                manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
                if manifest.get("format") != BUNDLE_FORMAT:
                    raise FundError("bundle_format")
                if int(manifest.get("schema_version", 0)) > BUNDLE_SCHEMA:
                    raise FundError("bundle_schema_newer")

                declared = {entry.get("name"): entry for entry in manifest.get("members", [])}
                for member, info in seen.items():
                    if member == MANIFEST_NAME:
                        continue
                    entry = declared.get(member)
                    if entry is None:
                        raise FundError("bundle_member_undeclared")
                    data = archive.read(info)
                    if len(data) != int(entry.get("size", -1)):
                        raise FundError("bundle_size_mismatch")
                    if _sha256_bytes(data) != entry.get("sha256"):
                        raise FundError("bundle_checksum")
        except FundError:
            raise
        except (zipfile.BadZipFile, ValueError, KeyError, TypeError):
            raise FundError("bundle_invalid") from None

        return {
            "ok": True,
            "path": str(path),
            "sha256": sha256_file(path),
            "created_ms": int(manifest.get("created_ms", 0)),
            "schema_version": int(manifest.get("schema_version", 0)),
            "fund_ids": list(manifest.get("fund_ids", [])),
            "members": sorted(name for name in seen if name != MANIFEST_NAME),
        }

    def restore_bundle(
        self,
        bundle: Path | str,
        target: Path | str | None = None,
        *,
        allow_existing: bool = False,
        created_ms: int | None = None,
    ) -> dict[str, Any]:
        """把迁移包恢复到 ``target`` 目录（默认 ``root/restore/<时间戳>``）。

        **单写者**：只看**目标**数据目录是否被 OS 锁占用（不是源目录是否存在锁文件，
        也不是本实例的锁状态）；目标正被写入时拒绝。解包逐个成员校验 sha256 后写入，
        绝不调用 ``extractall``，因此路径穿越成员在解包前就被挡下。已存在的目录需要
        显式 ``allow_existing=True``。

        产物按包内结构落盘：``data/funds.sqlite3``、``config/public.json``、
        ``README.txt``；迁移完成后由运维把数据库放进新的数据目录。
        """
        moment = int(created_ms if created_ms is not None else _now_ms())
        path = self._resolve_existing(self.export_dir, bundle)
        summary = self.verify_bundle(path)

        if target is None:
            destination = self.restore_dir / f"restore-{_timestamp_name(moment)}"
        else:
            destination = Path(target)
        if destination.exists() and any(destination.iterdir()) and not allow_existing:
            raise FundError("restore_target_exists")
        # 单写者：探测**目标**目录上的实际 OS 锁。目标不存在（全新迁移目录）时
        # 不可能有写者；目标正在被服务持锁（含本实例持锁）时拒绝。本实例持锁且
        # 目标就是本实例数据目录时直接拒绝，不依赖同进程重复探测的实现细节。
        if self._data_lock is not None and _same_dir(destination, self.data_dir):
            raise FundError("writer_active")
        if _data_dir_locked(destination):
            raise FundError("writer_active")

        self._record(
            "ops.restore.started",
            details={"bundle": path.name, "target": destination.name, "intent": True},
            event_key=f"ops.restore.started:{path.name}:{moment}",
            created_ms=moment,
        )

        staging = destination.parent / f".restore-{uuid.uuid4().hex}"
        staging.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        try:
            with zipfile.ZipFile(path, "r") as archive:
                for member in summary["members"]:
                    out = staging / member
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(archive.read(archive.getinfo(member)))
            destination.mkdir(parents=True, exist_ok=True)
            for source in sorted(staging.rglob("*")):
                if source.is_file():
                    relative = source.relative_to(staging)
                    out = destination / relative
                    out.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, out)
                    written.append(str(relative).replace("\\", "/"))
        finally:
            _remove_tree(staging)

        self._record(
            "ops.restore.completed",
            details={"bundle": path.name, "target": destination.name, "files": len(written)},
            event_key=f"ops.restore.completed:{path.name}:{moment}",
            created_ms=moment,
        )
        return {"ok": True, "target": str(destination), "files": sorted(written), "bundle": summary}

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict[str, Any]:
        """只含状态量与计数的运维快照；给 web ``public_status`` 复用。"""
        state = self._load_health()
        backups = self.list_backups()
        return {
            "network": state.get("network", "unknown"),
            "healthy": state.get("network") == "up",
            "consecutive_failures": int(state.get("consecutive_failures", 0)),
            "outage_ms": (
                max(0, _now_ms() - int(state["outage_started_ms"])) if state.get("outage_started_ms") else None
            ),
            "writer_active": self.writer_active(),
            "backup_count": len(backups),
            "latest_backup": backups[0]["name"] if backups else None,
            "health_interval_seconds": self.health_interval_seconds,
            "updated_ms": int(state.get("updated_ms", 0)),
        }

    # -------------------------------------------------------------- 内部工具
    def _load_health(self) -> dict[str, Any]:
        if self._health is None:
            stored = self.store.get("ops", "health", None) if self.store is not None else None
            self._health = dict(stored) if isinstance(stored, Mapping) else {}
            self._health.setdefault("network", "unknown")
        return dict(self._health)

    def _save_health(self, state: Mapping[str, Any], now_ms: int) -> None:
        snapshot = dict(state)
        snapshot["updated_ms"] = now_ms
        self._health = snapshot
        if self.store is not None:
            self.store.put("ops", "health", snapshot)

    def _enqueue_notice(
        self,
        fund_id: str | None,
        content: str,
        now_ms: int,
        event_id: str | None,
        *,
        kind: str,
    ) -> int:
        """把通知写入 ``notices`` outbox；``user_id`` 为空表示按持有人展开。

        断线期间无法即时送达站内消息，因此这些行只会停留在 ``queued``，由
        ``PaymentsWorker``/``CommandHandler`` 在恢复后按事件 ID 补发。
        """
        if self.store is None:
            return 0
        queued = 0
        for target_fund in (self.fund_ids if fund_id is None else (fund_id,)):
            if target_fund is None:
                continue
            identifier = _safe_token(event_id or f"evt-{uuid.uuid4().hex[:16]}")
            key = f"{identifier}:{target_fund}:{kind}"
            record = {
                "id": key,
                "event_id": identifier,
                "fund_id": target_fund,
                "user_id": None,
                "kind": kind,
                "content": _scrub_text(content),
                "created_ms": now_ms,
                "status": "queued",
            }
            if self.store.claim("notices", key, record):
                queued += 1
        return queued

    def _alert(self, text: str) -> None:
        """可选的独立告警出口；默认不配置，且**永不**发起真实网络请求。

        这是唯一可能在离线期间送达的通道，但它不是站内消息。
        """
        sender = getattr(self.config, "alert_sender", None)
        if not callable(sender):
            return
        try:
            sender(_scrub_text(text))
        except Exception:  # 告警失败绝不影响主流程
            self._append_log(
                {
                    "ts": utc_text(_now_ms()),
                    "event": "ops.alert_failed",
                    "event_id": f"evt-{uuid.uuid4().hex[:16]}",
                    "fund_id": None,
                    "level": "warning",
                    "details": {},
                }
            )

    def _unique_path(self, directory: Path, name: str) -> Path:
        candidate = directory / name
        if not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        for index in range(1, 1000):
            attempt = directory / f"{stem}-{index}{suffix}"
            if not attempt.exists():
                return attempt
        raise FundError("name_conflict")

    def _resolve_in(self, base: Path, candidate: Path | str) -> Path:
        """把候选名解析到 base 之内；拒绝绝对路径、``..`` 与盘符。"""
        text = str(candidate)
        if Path(text).is_absolute() or ":" in text or ".." in Path(text).parts:
            raise FundError("invalid_name")
        resolved = (base / text).resolve()
        root = base.resolve()
        if resolved != root and root not in resolved.parents:
            raise FundError("invalid_name")
        return resolved

    def _resolve_existing(self, base: Path, candidate: Path | str) -> Path:
        """已存在的路径按原样使用（调用方已显式给出）；否则当作 base 内的相对名解析。"""
        path = Path(candidate)
        if path.exists():
            return path
        return self._resolve_in(base, candidate)

    def _verify_sidecar(self, path: Path) -> None:
        sidecar = path.with_name(path.name + CHECKSUM_SUFFIX)
        if not sidecar.exists() or not sidecar.stat().st_size:
            return
        expected = sidecar.read_text(encoding="utf-8").split()[0]
        if sha256_file(path) != expected:
            raise FundError("bundle_checksum")


# --- 模块级辅助 -------------------------------------------------------------


class _suppress_oserror:  # noqa: N801 - 上下文管理器，命名保持可读
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, OSError)


def _same_dir(left: Path | str, right: Path | str) -> bool:
    """两个路径在解析链接/大小写后是否指向同一目录。"""
    try:
        return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))
    except OSError:
        return False


def _data_dir_locked(directory: Path | str) -> bool:
    """探测 ``directory`` 上的实际 OS 数据锁，不破坏持锁者。

    尝试以非阻塞方式取得**同一把** OS 锁：成功（说明无人写入）立即释放并返回假；
    被占用时取得失败、直接返回真 —— 取得失败不会写锁文件，也不会释放持锁者的锁。
    目录不可用或读不到时同样返回真：判定不了就按「有写者」处理，绝不把运行中的
    服务误判成已停止。目录不存在时返回假：服务启动前会先建目录，不存在的目录
    不可能有写者，也避免探测本身产生创建目录的副作用。
    """
    directory = Path(directory)
    if not directory.exists():
        return False
    try:
        lock = acquire_data_lock(directory)
    except (DataLockError, OSError):
        return True
    lock.release()
    return False


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        with _suppress_oserror():
            if child.is_dir():
                child.rmdir()
            else:
                child.unlink()
    with _suppress_oserror():
        path.rmdir()


def _write_member(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    """写一个成员，固定时间戳避免同一输入产生不同 zip。"""
    info = zipfile.ZipInfo(_safe_member_name(name), date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, data)


def _classify_error(error: object) -> str | None:
    """把失败原因收敛成稳定分类；绝不透传上游正文或异常消息。"""
    if error is None:
        return None
    if isinstance(error, BaseException):
        return _safe_token(type(error).__name__)
    if isinstance(error, str):
        return _safe_token(error)
    return _safe_token(type(error).__name__)


def _scrub_text(value: str) -> str:
    if not isinstance(value, str):
        value = str(value)
    if len(value) > _MAX_STRING:
        value = value[:_MAX_STRING] + "…"
    return value


def _scrub(value: object, *, depth: int = 0) -> Any:
    """递归脱敏：按键名剔除敏感字段，字符串截断，非 JSON 类型转文本。"""
    if depth > 6:
        return "[depth]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return "[nonfinite]"
        return value
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): ("[redacted]" if _BLOCKED_KEY.search(str(key)) else _scrub(item, depth=depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_scrub(item, depth=depth + 1) for item in value]
    return _scrub_text(repr(value))


def _scrub_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    scrubbed = _scrub(data)
    return scrubbed if isinstance(scrubbed, dict) else {}


__all__ = [
    "BUNDLE_FORMAT",
    "BUNDLE_MEMBERS",
    "BUNDLE_SCHEMA",
    "CONFIG_FIELDS",
    "ServiceOperations",
    "sha256_file",
    "sha256_text",
]
