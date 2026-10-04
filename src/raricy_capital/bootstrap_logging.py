"""Stdlib-only, bounded startup log for the fund service.

Imported **before** any third-party module (``config`` pulls in ``cryptography``
and ``yaml``; ``serve`` pulls in ``aiohttp``) so a fatal startup failure still
leaves a sanitized trace even when the console is hidden (Windows ``pythonw``
gives ``sys.stdout`` / ``sys.stderr`` as ``None``) or the interpreter cannot
import a runtime dependency.

The log is JSON Lines under ``<data-dir>/logs/bootstrap.jsonl``, rotated at
1 MiB with three backups, created ``0600`` where the OS supports it.  Records
never contain ``str(exception)``, source-line text, request payloads,
environment variables, configuration content or credentials: known service
errors contribute a stable code, everything else contributes only its exception
type, an ``errno`` when present, and a sanitized frame (basename / function /
line).  A logging failure never replaces the original error and never raises.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_FILE_NAME = 'bootstrap.jsonl'
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 3
ENV_LOG_DIR = 'FUNDS_BOOTSTRAP_LOG_DIR'
DEFAULT_DATA_SUFFIX = ('data', 'capital_funds')

#: Stable codes may be reused verbatim; object messages never are.
_CODE_RE = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
_SAFE_RE = re.compile(r'[^A-Za-z0-9_.-]')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _token(value: object, limit: int) -> str:
    """Collapse an untrusted token to a short, log-safe identifier."""
    return _SAFE_RE.sub('_', str(value if value is not None else ''))[:limit]


def _repo_root(explicit: Path | None = None) -> Path:
    """Repository root for the default data path; ``cwd`` when installed."""
    if explicit is not None:
        return Path(explicit)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / 'pyproject.toml').is_file():
            return parent
    return Path.cwd()


def resolve_log_dir(data_dir: Path | None = None, *, repo_root: Path | None = None) -> Path:
    """Resolve the bootstrap log directory.

    ``FUNDS_BOOTSTRAP_LOG_DIR`` always wins: it is the only target that can be
    set before the configuration (and therefore ``data_dir``) is readable, which
    matters under the Linux unit's hardening.  Otherwise a CLI ``--data-dir`` is
    honoured, falling back to the repository default ``data/capital_funds``.
    """
    override = os.environ.get(ENV_LOG_DIR)
    if override:
        return Path(override).expanduser()
    if data_dir is not None:
        base = Path(data_dir).expanduser()
    else:
        base = _repo_root(repo_root).joinpath(*DEFAULT_DATA_SUFFIX)
    return base / 'logs'


def _known_code(exc: BaseException) -> str | None:
    """Stable code for the service's own error types, if this is one of them."""
    name = type(exc).__name__
    if name == 'FundError':
        code = getattr(exc, 'code', None)
        if isinstance(code, str) and _CODE_RE.match(code):
            return code
    elif name == 'DataLockError':
        # ``DataLockError`` messages are stable category codes by contract.
        try:
            text = str(exc)
        except Exception:
            return None
        if _CODE_RE.match(text):
            return text
    return None


def _generic_code(exc: BaseException) -> str:
    if isinstance(exc, ImportError):
        return 'runtime_dependency_missing'
    if isinstance(exc, OSError):
        return 'os_error'
    if isinstance(exc, KeyboardInterrupt):
        return 'interrupted'
    return 'unexpected_error'


def safe_error_code(exc: BaseException) -> str:
    """Public, sanitized code for an exception; never touches the message text."""
    known = _known_code(exc)
    return known if known is not None else _generic_code(exc)


def _frame_info(exc: BaseException) -> dict:
    """Last frame as basename / function / line; never the source line itself."""
    tb = getattr(exc, '__traceback__', None)
    if tb is None:
        return {}
    try:
        while tb.tb_next is not None:
            tb = tb.tb_next
        code = tb.tb_frame.f_code
        return {
            'file': _token(os.path.basename(str(code.co_filename)), 160),
            'function': _token(code.co_name, 64),
            'line': int(tb.tb_lineno),
        }
    except Exception:
        return {}


class BootstrapLogger:
    """Append-only, rotating JSONL sink that never raises at its call sites."""

    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)

    @classmethod
    def for_startup(cls, *, data_dir: Path | None = None, repo_root: Path | None = None) -> 'BootstrapLogger':
        return cls(resolve_log_dir(data_dir, repo_root=repo_root))

    def retarget(self, data_dir: Path) -> 'BootstrapLogger':
        """Follow the validated configuration unless the env override is set."""
        if not os.environ.get(ENV_LOG_DIR):
            self.log_dir = Path(data_dir) / 'logs'
        return self

    @property
    def path(self) -> Path:
        return self.log_dir / LOG_FILE_NAME

    def fatal(self, phase: str, exc: BaseException, *, exit_code: int = 1) -> str:
        """Record a fatal startup failure and return the safe code for reporting."""
        code = safe_error_code(exc)
        self.report(phase, exc=exc, code=code, exit_code=exit_code, level='fatal')
        return code

    def report(self, phase: str, *, code: str | None = None, exc: BaseException | None = None,
               exit_code: int | None = None, level: str = 'fatal') -> str | None:
        record: dict[str, object] = {'ts': _now(), 'level': _token(level, 24), 'phase': _token(phase, 32)}
        resolved = code
        if exc is not None:
            known = _known_code(exc)
            record['error_type'] = _token(type(exc).__name__, 64)
            if known is not None:
                resolved = known
            else:
                resolved = resolved or _generic_code(exc)
                errno = getattr(exc, 'errno', None)
                if isinstance(errno, int) and not isinstance(errno, bool):
                    record['errno'] = errno
                record.update(_frame_info(exc))
        if resolved:
            record['code'] = _token(resolved, 64)
        if exit_code is not None:
            record['exit_code'] = int(exit_code)
        if not self._append(record):
            self._fallback(record)
        return record.get('code')

    # ------------------------------------------------------------------ io
    def _append(self, record: dict) -> bool:
        try:
            line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8', 'replace')
        except (TypeError, ValueError):
            return False
        try:
            self._rotate_if_needed(len(line))
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self._chmod_dir()
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
            self._chmod_file()
            return True
        except Exception:
            return False

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size + incoming <= MAX_BYTES:
            return
        try:
            self.path.with_name(f'{self.path.name}.{BACKUP_COUNT}').unlink()
        except OSError:
            pass
        for index in range(BACKUP_COUNT - 1, 0, -1):
            source = self.path.with_name(f'{self.path.name}.{index}')
            target = self.path.with_name(f'{self.path.name}.{index + 1}')
            try:
                os.replace(source, target)
            except OSError:
                pass
        try:
            os.replace(self.path, self.path.with_name(f'{self.path.name}.1'))
        except OSError:
            pass

    def _chmod_dir(self) -> None:
        if os.name != 'nt':
            try:
                os.chmod(self.log_dir, 0o700)
            except OSError:
                pass

    def _chmod_file(self) -> None:
        if os.name != 'nt':
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def _fallback(self, record: dict) -> None:
        """Report a logging failure on stderr without recursing into logging."""
        try:
            stream = sys.stderr
            if stream is None or not hasattr(stream, 'write'):
                return
            stream.write(f"bootstrap-log-failed phase={record.get('phase', 'startup')} "
                         f"code={record.get('code', 'startup_failed')}\n")
            flush = getattr(stream, 'flush', None)
            if callable(flush):
                flush()
        except Exception:
            pass
