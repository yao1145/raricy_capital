"""Fatal-startup logging and packaging invariants.

These tests cover the *bootstrap* path only: what the entry point records when it
fails before (or while) third-party dependencies, the console or the network are
available.  They assert the log stays sanitized and bounded, that a hidden
``pythonw`` startup (``sys.stdout``/``sys.stderr`` are ``None``) neither raises
nor loses the failure, and that the packaging units carry the corrected
directives.  Financial behaviour is out of scope here.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from raricy_capital import bootstrap_logging as bl
from raricy_capital.__main__ import main
from raricy_capital.bootstrap_logging import (
    BACKUP_COUNT,
    ENV_LOG_DIR,
    LOG_FILE_NAME,
    MAX_BYTES,
    BootstrapLogger,
)
from raricy_capital.contracts import FundError

PACKAGING = Path(__file__).resolve().parents[1] / 'packaging' / 'funds'


def _records(log_dir: Path) -> list[dict]:
    text = (log_dir / LOG_FILE_NAME).read_text(encoding='utf-8')
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ------------------------------------------------------------------- sanitizing
def test_known_fund_error_logs_stable_code(tmp_path):
    logger = BootstrapLogger(tmp_path / 'logs')

    code = logger.fatal('config', FundError('weak_control_token'))

    assert code == 'weak_control_token'
    records = _records(tmp_path / 'logs')
    assert records[-1]['code'] == 'weak_control_token'
    assert records[-1]['error_type'] == 'FundError'
    assert records[-1]['phase'] == 'config'
    assert records[-1]['exit_code'] == 1


def test_generic_error_logs_type_and_errno_without_message_or_source(tmp_path):
    secret = 'SUPER-SECRET-abcdef123456'
    logger = BootstrapLogger(tmp_path / 'logs')

    try:
        raise OSError(13, f'open failed while reading {secret}')
    except OSError as exc:
        code = logger.fatal('serve', exc)

    assert code == 'os_error'
    text = (tmp_path / 'logs' / LOG_FILE_NAME).read_text(encoding='utf-8')
    # The raw message, the credential it names and any source-line text must not
    # reach disk; only the stable type/errno and a sanitized frame are allowed.
    assert secret not in text
    assert 'open failed' not in text
    record = _records(tmp_path / 'logs')[-1]
    assert record['error_type'] == 'PermissionError'
    assert record['errno'] == 13
    assert record['phase'] == 'serve'
    assert record['file'] == 'test_bootstrap.py'
    assert record['function'] == 'test_generic_error_logs_type_and_errno_without_message_or_source'
    assert isinstance(record['line'], int)


def test_timestamp_is_present(tmp_path):
    BootstrapLogger(tmp_path / 'logs').fatal('config', FundError('invalid_configuration'))
    assert _records(tmp_path / 'logs')[-1]['ts']


# -------------------------------------------------------------- rotation bounds
def test_rotation_stays_within_bounded_files(tmp_path, monkeypatch):
    log_dir = tmp_path / 'logs'
    monkeypatch.setattr(bl, 'MAX_BYTES', 2000)
    logger = BootstrapLogger(log_dir)

    for _ in range(60):
        logger.report('serve', code='unexpected_error' * 3, exit_code=1, level='fatal')

    files = sorted(log_dir.glob(LOG_FILE_NAME + '*'))
    assert 1 < len(files) <= BACKUP_COUNT + 1
    assert all(path.stat().st_size <= 2000 for path in files)
    assert (log_dir / LOG_FILE_NAME).is_file()

    # The shipped defaults themselves are small and bounded.
    assert MAX_BYTES == 1024 * 1024
    assert BACKUP_COUNT == 3


# ------------------------------------------------------------------ entry point
def test_main_weak_config_without_streams_returns_nonzero_and_logs(tmp_path, monkeypatch):
    log_dir = tmp_path / 'boot'
    monkeypatch.setenv(ENV_LOG_DIR, str(log_dir))
    monkeypatch.setenv('FUNDS_CONTROL_TOKEN', 'ZZSECRETZZ')  # < 24 chars
    monkeypatch.setattr(sys, 'argv', ['raricy_capital', '--data-dir', str(tmp_path / 'data')])
    monkeypatch.setattr(sys, 'stdout', None)
    monkeypatch.setattr(sys, 'stderr', None)

    rc = main()

    assert rc != 0
    text = (log_dir / LOG_FILE_NAME).read_text(encoding='utf-8')
    assert 'ZZSECRETZZ' not in text
    record = _records(log_dir)[-1]
    assert record['phase'] == 'config'
    assert record['code'] == 'weak_control_token'
    assert record['exit_code'] == rc


def test_logging_failure_falls_back_to_stderr_without_masking(tmp_path, monkeypatch):
    blocker = tmp_path / 'blocked'
    blocker.write_text('not a directory', encoding='utf-8')  # log dir cannot be created
    monkeypatch.setenv(ENV_LOG_DIR, str(blocker / 'logs'))
    monkeypatch.setenv('FUNDS_CONTROL_TOKEN', 'ZZSECRETZZ')
    monkeypatch.setattr(sys, 'argv', ['raricy_capital', '--data-dir', str(tmp_path / 'data')])
    errors = io.StringIO()
    monkeypatch.setattr(sys, 'stderr', errors)

    rc = main()

    assert rc != 0
    assert 'bootstrap-log-failed' in errors.getvalue()
    assert 'ZZSECRETZZ' not in errors.getvalue()


# ------------------------------------------------------------ packaging statics
def test_tunnel_unit_is_a_user_unit_without_instance_specifier():
    text = (PACKAGING / 'raricy-funds-tunnel.service').read_text(encoding='utf-8')
    # A non-template unit must not use the %i instance specifier nor the %i-based
    # /home/%i key path; the per-user home is %h, and a user unit needs no User=.
    assert '%i' not in text
    assert 'User=' not in text
    assert '/home/' not in text
    assert '%h' in text


def test_fund_service_start_limit_directives_are_placed_correctly():
    text = (PACKAGING / 'raricy-funds.service').read_text(encoding='utf-8')
    directives = [line.strip() for line in text.splitlines()
                  if line.strip() and not line.lstrip().startswith('#')]
    # StartLimitBurst is a [Unit] key; used at all it could permanently stop the
    # service before connectivity returns, so no uncommented directive may carry it.
    assert not any(line.startswith('StartLimitBurst') for line in directives)
    assert 'Restart=always' in directives
    unit_section = text.partition('[Service]')[0]
    assert any(line.strip() == 'StartLimitIntervalSec=0' for line in unit_section.splitlines()
               if line.strip() and not line.lstrip().startswith('#'))
