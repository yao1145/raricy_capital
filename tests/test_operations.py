"""ServiceOperations 的针对性测试：备份轮转、迁移包安全边界、健康探测与脱敏日志。

只覆盖运维面最关键的几条不变量，不做全仓测试：

* 备份走 SQLite online backup，旁车 sha256 校验通过，按份数轮转；
* 迁移包 export→verify→restore 闭环，逐成员 sha256 生效；
* 解包安全：路径穿越、符号链接、白名单外成员、篡改成员一律拒绝；
* 单写者：写者锁被持有时取得被拒；restore 探测的是目标数据目录的 OS 锁；
* 健康探测：连续失败判离线、恢复后补发通知、状态持久化；
* 滚动日志按份数轮转，密码/令牌按键名剔除，异常只留安全堆栈。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from raricy_capital.contracts import FundError
from raricy_capital.operations import ServiceOperations
from raricy_capital.store import FundStore


def make_ops(tmp_path: Path, **overrides) -> ServiceOperations:
    store = FundStore(tmp_path / "funds.sqlite3")
    config = SimpleNamespace(
        data_dir=tmp_path,
        fund_ids=("capital1", "capital2"),
        backup_keep=2,
        log_max_bytes=4096,
        log_keep=3,
        health_failure_threshold=3,
        health_recovery_threshold=1,
        **overrides,
    )
    return ServiceOperations(store, tmp_path, config)


# --- 备份 -------------------------------------------------------------------


def test_backup_rotates_and_checksums(tmp_path):
    ops = make_ops(tmp_path)
    ops.store.put("records", "k", {"v": 1})

    first = ops.backup(created_ms=1_700_000_000_000)
    ops.backup(created_ms=1_700_000_001_000)
    third = ops.backup(created_ms=1_700_000_002_000)

    remaining = ops.list_backups()
    assert len(remaining) == 2  # keep=2
    assert remaining[0]["name"] == third.name
    assert first.name not in {item["name"] for item in remaining}

    verdict = ops.verify_backup(third)
    assert verdict["ok"] is True
    assert verdict["sha256"] == remaining[0]["sha256"]

    # 备份是可直接打开的一致快照，而不是复制中的 WAL。
    snapshot = FundStore(third)
    try:
        assert snapshot.get("records", "k") == {"v": 1}
    finally:
        snapshot.close()
    ops.store.close()


def test_backup_rejects_foreign_path(tmp_path):
    ops = make_ops(tmp_path)
    ops.backup(created_ms=1_700_000_000_000)
    with pytest.raises(FundError):
        ops.verify_backup("../../etc/passwd")
    ops.store.close()


# --- 迁移包闭环与安全边界 ---------------------------------------------------


def test_export_restore_roundtrip_excludes_secrets(tmp_path):
    ops = make_ops(tmp_path)
    ops.store.put("records", "k", {"amount_units": 1234})
    # 公开字段之外的凭据不得进入迁移包。
    ops.config.control_token = "T-SECRET"  # type: ignore[attr-defined]
    ops.config.password = "P-SECRET"  # type: ignore[attr-defined]

    bundle = ops.export_bundle(created_ms=1_700_000_000_000)
    assert bundle.exists()
    assert bundle.with_name(bundle.name + ".sha256").exists()
    assert ops.verify_bundle(bundle)["ok"] is True

    restored = ops.restore_bundle(bundle, created_ms=1_700_000_000_000)
    target = Path(restored["target"])
    assert (target / "data" / "funds.sqlite3").exists()
    assert (target / "config" / "public.json").exists()
    assert restored["files"] == ["README.txt", "config/public.json", "data/funds.sqlite3"]

    public = json.loads((target / "config" / "public.json").read_text(encoding="utf-8"))
    assert "control_token" not in public and "password" not in public

    # 导出的数据库能把业务记录带过去。
    carried = FundStore(target / "data" / "funds.sqlite3")
    try:
        assert carried.get("records", "k") == {"amount_units": 1234}
    finally:
        carried.close()

    # 非空目标目录需要显式允许。
    with pytest.raises(FundError) as excinfo:
        ops.restore_bundle(bundle, created_ms=1_700_000_000_000)
    assert excinfo.value.code == "restore_target_exists"
    ops.store.close()


def test_verify_bundle_rejects_traversal_and_disallowed(tmp_path):
    ops = make_ops(tmp_path)
    bundle = tmp_path / "exports" / "evil.zip"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("../escape.txt", b"x")
        archive.writestr("manifest.json", b"{}")
    with pytest.raises(FundError):
        ops.verify_bundle(bundle)

    other = tmp_path / "exports" / "other.zip"
    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr("unexpected.txt", b"x")
    with pytest.raises(FundError) as excinfo:
        ops.verify_bundle(other)
    assert excinfo.value.code == "bundle_member_disallowed"
    ops.store.close()


def test_verify_bundle_detects_tampering(tmp_path):
    ops = make_ops(tmp_path)
    bundle = ops.export_bundle(created_ms=1_700_000_000_000)

    with zipfile.ZipFile(bundle) as archive:
        payload = {name: archive.read(name) for name in archive.namelist()}
    mutation = bytearray(payload["data/funds.sqlite3"])
    mutation[0] ^= 0xFF
    payload["data/funds.sqlite3"] = bytes(mutation)
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
    bundle.with_name(bundle.name + ".sha256").unlink()  # 绕过整包旁车，直测成员校验

    with pytest.raises(FundError) as excinfo:
        ops.verify_bundle(bundle)
    assert excinfo.value.code == "bundle_checksum"
    ops.store.close()


def test_restore_refuses_active_writer(tmp_path):
    ops = make_ops(tmp_path)
    bundle = ops.export_bundle(created_ms=1_700_000_000_000)
    ops.acquire_writer_lock("service")
    try:
        assert ops.writer_active() is True
        with pytest.raises(FundError) as excinfo:
            ops.restore_bundle(bundle, target=ops.data_dir, allow_existing=True)
        assert excinfo.value.code == "writer_active"
        with pytest.raises(FundError):
            ops.acquire_writer_lock("second")
    finally:
        ops.release_writer_lock()
    assert ops.writer_active() is False
    ops.store.close()


def test_acquire_writer_lock_is_exclusive_and_released(tmp_path):
    """写者锁被本实例持有时独占；释放后回到空闲，且没有协作锁文件残留。"""
    ops = make_ops(tmp_path)
    ops.acquire_writer_lock("service")
    assert ops.writer_active() is True
    with pytest.raises(FundError) as excinfo:
        ops.acquire_writer_lock("second")
    assert excinfo.value.code == "writer_active"
    ops.release_writer_lock()
    assert ops.writer_active() is False
    assert not (tmp_path / "writer.lock").exists()  # 不再是需要人工清理的协作锁
    ops.store.close()


def test_force_cannot_steal_os_lock(tmp_path):
    """OS 锁不能被 force 抢占，也不存在「陈旧锁需人工清理」的语义。"""
    ops = make_ops(tmp_path)
    with pytest.raises(FundError) as excinfo:
        ops.acquire_writer_lock("service", force=True)
    assert excinfo.value.code == "writer_lock_force_unsupported"
    assert ops.writer_active() is False
    ops.store.close()


def test_data_dir_locked_reports_free_and_missing(tmp_path):
    """空闲目录探测为假；不存在的目录不可能有写者，探测也不创建目录。"""
    from raricy_capital.operations import _data_dir_locked

    assert _data_dir_locked(tmp_path) is False
    missing = tmp_path / "does-not-exist"
    assert _data_dir_locked(missing) is False
    assert not missing.exists()


def test_restore_probes_target_directory_lock(tmp_path, monkeypatch):
    """restore 检查的是**目标**目录上的锁，而不是源目录或本实例状态。"""
    ops = make_ops(tmp_path)
    bundle = ops.export_bundle(created_ms=1_700_000_000_000)
    target = tmp_path / "target-data"
    target.mkdir()

    probed: list[Path] = []

    def fake(directory):
        probed.append(Path(directory))
        return True

    monkeypatch.setattr("raricy_capital.operations._data_dir_locked", fake)
    with pytest.raises(FundError) as excinfo:
        ops.restore_bundle(bundle, target=target, allow_existing=True)
    assert excinfo.value.code == "writer_active"
    assert probed and probed[-1] == target

    # 目标无写者时恢复照常进行。
    monkeypatch.setattr("raricy_capital.operations._data_dir_locked", lambda directory: False)
    summary = ops.restore_bundle(bundle, target=target, allow_existing=True)
    assert (Path(summary["target"]) / "data" / "funds.sqlite3").exists()
    ops.store.close()


# --- 健康探测 ---------------------------------------------------------------


def test_health_tick_outage_and_recovery_queue_notices(tmp_path):
    ops = make_ops(tmp_path)
    steps = [1000, 5000, 9000]
    ticks = [ops.health_tick(moment, False, "timeout") for moment in steps]

    assert [tick["transition"] for tick in ticks] == [None, None, "outage"]
    assert ticks[-1]["network"] == "down"
    assert ticks[-1]["consecutive_failures"] == 3
    assert ticks[-1]["interval_seconds"] == 4
    assert ticks[-1]["error"] == "timeout"

    outage_notices = ops.store.list("notices")
    assert len(outage_notices) == 2  # 两条基金各一条，user_id 为空表示按持有人展开
    assert {row["fund_id"] for row in outage_notices} == {"capital1", "capital2"}
    assert all(row["status"] == "queued" and row["user_id"] is None for row in outage_notices)

    health = ops.store.get("ops", "health")
    assert health["network"] == "down" and health["outage_started_ms"] == 9000

    recovered = ops.health_tick(13000, True)
    assert recovered["transition"] == "recovered"
    assert recovered["network"] == "up"
    assert recovered["outage_ms"] == 4000

    assert len(ops.store.list("notices")) == 4  # 断线 + 恢复，各两条基金
    assert ops.store.get("ops", "health")["network"] == "up"
    ops.store.close()


def test_health_tick_notification_is_not_pretended_online(tmp_path):
    ops = make_ops(tmp_path)
    sent: list[str] = []
    ops.config.alert_sender = sent.append  # type: ignore[attr-defined]

    for moment in (1000, 5000, 9000):
        ops.health_tick(moment, False, ConnectionError("reset"))

    # 独立告警出口拿到的是脱敏后的纯文本；站内通知只能排队等待恢复。
    assert len(sent) == 1
    assert ops.store.list("notices")[0]["status"] == "queued"
    ops.store.close()


# --- 滚动日志与脱敏 ---------------------------------------------------------


def test_rotating_log_redacts_secrets_and_keeps_stack(tmp_path):
    ops = make_ops(tmp_path)

    try:
        raise RuntimeError("boom /secret/path")
    except RuntimeError as exc:
        ops.record_event(
            "wallet.error",
            fund_id="capital1",
            level="error",
            error=exc,
            details={"password": "hunter2", "session_token": "abc", "amount_units": 1234, "note": "ok"},
        )

    record = ops.recent_logs(1)[0]
    assert record["event"] == "wallet.error"
    assert record["fund_id"] == "capital1"
    assert record["details"]["password"] == "[redacted]"
    assert record["details"]["session_token"] == "[redacted]"
    assert record["details"]["note"] == "ok"
    assert isinstance(record["details"]["amount_units"], int)
    assert "stack" in record["details"]  # 只留安全堆栈，不含源码行或异常正文

    for index in range(400):
        ops.record_event("wallet.tick", details={"note": "x" * 200, "index": index})

    assert (tmp_path / "logs" / "funds-ops.jsonl.1").exists()
    assert not (tmp_path / "logs" / "funds-ops.jsonl.4").exists()  # log_keep=3
    assert ops.recent_logs(5)
    ops.store.close()


def test_status_is_counters_only(tmp_path):
    ops = make_ops(tmp_path)
    ops.health_tick(1000, True)
    status = ops.status()
    assert status["network"] == "up" and status["healthy"] is True
    assert status["backup_count"] == 0 and status["writer_active"] is False
    assert set(status) >= {"network", "writer_active", "backup_count", "updated_ms"}
    ops.store.close()
