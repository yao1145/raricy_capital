"""数据档案锁：同一份数据同时只允许一个写者。

基金服务与离线运维都要在打开账簿**之前**调用
`acquire_data_lock()`：只由 Launcher 自己加锁阻止不了 CLI 并发写入，所以锁标识由
**规范化后的数据档案目录**派生（存储目录，即数据库文件所在目录），不是进程内约定。

实现是在该目录里对一个锁文件取排他锁：Windows 用 `msvcrt.locking` 的字节区间锁，
POSIX 用 `fcntl.flock`。两种锁都随进程退出（含崩溃）由操作系统释放，因此不存在
需要人工清理的陈旧锁；锁文件里只写 pid 与启动时间，供占用诊断，不是锁本身。
"""

from __future__ import annotations

import os
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# 锁文件名：与数据档案内的其他文件区分开，`bot.db -wal/-shm` 之外单列。
DATA_LOCK_FILE: str = ".raricy-data.lock"
ACCOUNT_LOCK_EXTENSION: str = ".lock"
# 彻底删除后的墓碑（§6.2、D-145）；名字与 `raricy_launcher.paths.REMOVED_FILE` 逐字
# 相同 —— 核心包不能依赖 Launcher 包，两边各有一份定义，共享入口按同一文件名判定。
REMOVED_MARKER_FILE: str = "removed.json"


class DataLockError(Exception):
    """数据目录不可用或已被占用；消息是稳定类别码。"""


def data_lock_dir(db_path: str | Path) -> Path:
    """由数据库路径得到数据档案标识：**解析链接之后**它所在的目录。

    必须先把整个数据库路径规范化再取父目录：只规范化父目录会丢掉数据库文件
    自身的链接信息，两条指向同一个数据库文件的路径就能各拿一把锁，绕过单写者
    约束（§9.5、审查 P2）。
    """
    return _normalize(Path(db_path)).parent


def account_lock_dir(site_url: str, account_id: str | int) -> Path:
    """按规范化站点与稳定账号 ID 生成跨版本公共锁文件路径。

    文件名只暴露不可逆摘要；站点路径与账号 ID 不写进锁文件或诊断日志。
    Light 与完整版在同一 OS 用户下使用相同用户状态根，因此即使 DB 不同也互斥。
    """
    site = _normalize_site_url(site_url)
    if isinstance(account_id, bool) or not isinstance(account_id, (str, int)):
        raise DataLockError("account_lock_invalid_identity")
    stable_id = str(account_id).strip()
    if not stable_id:
        raise DataLockError("account_lock_invalid_identity")
    digest = hashlib.sha256(
        json.dumps([site, stable_id], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return _normalize(_default_account_lock_root() / f"{digest}{ACCOUNT_LOCK_EXTENSION}")


def acquire_account_lock(site_url: str, account_id: str | int) -> DataLock:
    """立即取得本机站点账号锁；重复实例收到稳定的 `account_in_use` 错误。"""
    lock_path = account_lock_dir(site_url, account_id)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DataLockError("account_lock_unavailable") from exc
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError as exc:
        raise DataLockError("account_lock_unavailable") from exc
    try:
        _lock_file(fd)
    except OSError as exc:
        os.close(fd)
        raise DataLockError("account_in_use") from exc
    _write_owner(fd)
    return DataLock(fd, lock_path)


def _normalize_site_url(site_url: str) -> str:
    """规范化已校验的站点地址，统一 scheme/host 大小写和末尾斜杠。"""
    if not isinstance(site_url, str) or not site_url.strip():
        raise DataLockError("account_lock_invalid_identity")
    try:
        parsed = urlsplit(site_url.strip())
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise DataLockError("account_lock_invalid_identity") from exc
    if scheme not in ("http", "https") or not hostname or parsed.query or parsed.fragment:
        raise DataLockError("account_lock_invalid_identity")
    if parsed.username is not None or parsed.password is not None:
        raise DataLockError("account_lock_invalid_identity")
    try:
        host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise DataLockError("account_lock_invalid_identity") from exc
    if ":" in host:
        host = f"[{host}]"
    if port == (443 if scheme == "https" else 80):
        port = None
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def _default_account_lock_root() -> Path:
    """返回当前OS用户的独立基金共享锁目录。"""
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        base = Path(root) if root else Path.home() / "AppData" / "Local"
        return base / "RaricyCapital" / "account-locks"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "RaricyCapital" / "account-locks"
    root = os.environ.get("XDG_STATE_HOME")
    base = Path(root) if root else Path.home() / ".local" / "state"
    return base / "raricy_capital" / "account-locks"


def _normalize(path: Path) -> Path:
    """绝对化、解析链接/重解析点、统一大小写；目标可以尚不存在。"""
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except OSError:
        resolved = Path(os.path.abspath(Path(path).expanduser()))
    return Path(os.path.normcase(str(resolved)))


def _lock_file(fd: int) -> None:
    """取排他锁；已被占用时抛 `OSError`（非阻塞）。"""
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fd: int) -> None:
    """释放锁；关闭路径不抛出（句柄关闭同样会释放）。"""
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _write_owner(fd: int) -> None:
    """尽力写入诊断信息；锁本身由 OS 文件锁实现，与该元数据无关。"""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.truncate(fd, 0)
        stamp = datetime.now(timezone.utc).isoformat()
        os.write(fd, f"pid={os.getpid()}\nstarted={stamp}\n".encode("utf-8"))
    except OSError:
        # owner 内容只用于诊断；不能让磁盘写入失败破坏有效的互斥锁。
        pass


class DataLock:
    """已取得的数据档案锁；`release()` 幂等，也可直接作上下文管理器。"""

    def __init__(self, fd: int, path: Path) -> None:
        self._fd: int | None = fd
        self.path = path

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock_file(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> DataLock:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def acquire_data_lock(data_dir: str | Path) -> DataLock:
    """**立即**取得数据档案锁；被占用或目录不可用时抛 `DataLockError`。

    取得动作发生在这里而不是 `with` 进入时：调用方（入口）要在「打开归档与
    Store 之前」判定占用，把失败包在自己的 try 里比包住 `with` 更不容易漏。
    锁随进程退出由操作系统回收，不按陈旧 PID 判断归属：读锁文件里的 pid 只用于
    诊断，不能作为「可以夺锁」的依据（§9.3、§9.5）。
    """
    directory = _normalize(Path(data_dir))
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DataLockError("data_dir_unavailable") from exc
    lock_path = directory / DATA_LOCK_FILE
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError as exc:
        raise DataLockError("data_dir_unavailable") from exc
    try:
        _lock_file(fd)
    except OSError as exc:
        os.close(fd)
        raise DataLockError("data_in_use") from exc
    _write_owner(fd)
    return DataLock(fd, lock_path)


def refuse_removed_profile(
    profile_root: str | Path, *, marker: str = REMOVED_MARKER_FILE
) -> None:
    """档案已被彻底删除（存在墓碑）时拒绝继续：抛 `DataLockError("profile_removed")`。

    共享入口（完整版 CLI 与 Light Worker）在**取得数据锁之后、打开归档与 Store
    之前**调用一次：墓碑说明这份数据已经按用户要求删除，旧版本程序不认识它，只有
    本程序自己的入口能据此拒绝（§6.2、D-145 的已知边界）。

    判定只看墓碑文件本身，不看目录内容：空目录不是「已删除」。目录不存在时无操作
    （首次运行本来就还没有档案目录）；墓碑在那里但读不到（权限、占用）按
    `data_dir_unavailable` 拒绝 —— 读不到不等于没有，宁可拒绝也不放行（D-130 同口径）。
    """
    path = Path(profile_root) / marker
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise DataLockError("data_dir_unavailable") from exc
    raise DataLockError("profile_removed")
