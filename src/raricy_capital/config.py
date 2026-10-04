"""Loopback-only settings and a portable, separately keyed credential vault."""
from __future__ import annotations

import json
import os
import math
import secrets
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from .contracts import FundError, POLICIES, validate_control_user_id


def private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class FundConfig:
    data_dir: Path
    host: str = '127.0.0.1'
    port: int = 8137
    site_url: str = 'https://raricy.com'
    live: bool = False
    tick_seconds: float = 4.0
    backup_interval_seconds: int = 3600
    backup_retention: int = 72
    outage_failure_threshold: int = 3
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 10
    control_token: str = field(default='', repr=False)
    control_user_id: str = ''

    def __post_init__(self) -> None:
        self.control_user_id = validate_control_user_id(self.control_user_id)
        if not self.control_user_id:
            self.control_user_id = validate_control_user_id(os.environ.get('FUNDS_CONTROL_USER_ID', ''))
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        if self.host != '127.0.0.1':
            raise FundError('loopback_required')
        if type(self.live) is not bool or type(self.port) is not int:
            raise FundError('invalid_runtime_settings')
        if isinstance(self.tick_seconds, bool) or not isinstance(self.tick_seconds, (int,float)) or not math.isfinite(self.tick_seconds):
            raise FundError('invalid_runtime_settings')
        if not isinstance(self.backup_interval_seconds, int) or self.backup_interval_seconds < 60 or not 1 <= self.backup_retention <= 10000:
            raise FundError('invalid_runtime_settings')
        if not 1024 <= self.port <= 65535 or not 1 <= self.tick_seconds <= 60:
            raise FundError('invalid_runtime_settings')
        parsed = urlsplit(self.site_url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise FundError('invalid_site_url')
        self.site_url = self.site_url.rstrip('/')
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt':
            self.data_dir.chmod(0o700)
        token_path = self.data_dir / 'admin.token'
        self.control_token = self.control_token or os.environ.get('FUNDS_CONTROL_TOKEN', '')
        if not self.control_token:
            if token_path.exists():
                self.control_token = token_path.read_text(encoding='ascii').strip()
            else:
                self.control_token = secrets.token_urlsafe(32)
                private_write(token_path, self.control_token.encode('ascii'))
        if len(self.control_token) < 24:
            raise FundError('weak_control_token')

    def public(self) -> dict:
        return {'host': self.host, 'port': self.port, 'site_url': self.site_url,
                'live': self.live, 'tick_seconds': self.tick_seconds,
                'control_user_id': self.control_user_id,
                'backup_interval_seconds': self.backup_interval_seconds,
                'backup_retention': self.backup_retention,
                'policies': {k: p.public() for k, p in POLICIES.items()}}

    @property
    def backup_keep(self) -> int:
        return self.backup_retention

    @property
    def log_keep(self) -> int:
        return self.log_backup_count

    @property
    def health_interval_seconds(self) -> float:
        return self.tick_seconds

    @property
    def health_failure_threshold(self) -> int:
        return self.outage_failure_threshold

    @classmethod
    def load(cls, path: Path | None, *, data_dir: Path | None = None, live: bool = False, port: int | None = None) -> 'FundConfig':
        try:
            values = yaml.safe_load(path.read_text(encoding='utf-8-sig')) if path else {}
        except yaml.YAMLError:
            raise FundError('invalid_configuration') from None
        if not isinstance(values, dict):
            raise FundError('invalid_configuration')
        aliases = {'backup_keep': 'backup_retention', 'log_keep': 'log_backup_count',
                   'health_interval_seconds': 'tick_seconds', 'health_failure_threshold': 'outage_failure_threshold'}
        for old, new in aliases.items():
            if old in values:
                values[new] = values.pop(old)
        if 'fund_ids' in values:
            if set(values.pop('fund_ids')) != set(POLICIES):
                raise FundError('invalid_fund_configuration')
        if 'policies' in values:
            if values.pop('policies') != {k: p.public() for k, p in POLICIES.items()}:
                raise FundError('policies_are_frozen')
        allowed = {'data_dir', 'host', 'port', 'site_url', 'live', 'tick_seconds', 'backup_interval_seconds',
                   'backup_retention', 'outage_failure_threshold', 'log_max_bytes', 'log_backup_count', 'control_user_id'}
        if not isinstance(values, dict) or set(values) - allowed:
            raise FundError('invalid_configuration')
        if data_dir is not None:
            values['data_dir'] = data_dir
        values.setdefault('data_dir', Path('data/capital_funds'))
        if live:
            values['live'] = True
        if port is not None:
            values['port'] = port
        return cls(**values)


class CredentialVault:
    """Fernet is portable. The key is deliberately outside backup/export contents."""
    def __init__(self, root: Path):
        self.path = root / 'credentials.enc'
        key_path = root / 'credential.key'
        key = os.environ.get('FUNDS_CREDENTIAL_KEY', '').encode('ascii')
        if not key:
            if key_path.exists():
                key = key_path.read_bytes().strip()
            else:
                key = Fernet.generate_key()
                private_write(key_path, key)
        try:
            self.cipher = Fernet(key)
        except (ValueError, TypeError):
            raise FundError('invalid_credential_key') from None

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            values = json.loads(self.cipher.decrypt(self.path.read_bytes()))
            if not isinstance(values, dict) or set(values) - set(POLICIES):
                raise ValueError
            return values
        except (InvalidToken, ValueError, TypeError):
            raise FundError('credential_vault_invalid') from None

    def save(self, fund_id: str, username: str, password: str) -> None:
        if fund_id not in POLICIES:
            raise FundError('unknown_fund')
        values = self.read()
        values[fund_id] = {'username': username, 'password': password}
        private_write(self.path, self.cipher.encrypt(json.dumps(values, ensure_ascii=False).encode('utf-8')))
