"""独立性与支撑模块的针对性测试。

拆分的核心验收点是「不依赖原仓库、不依赖 PYTHONPATH 之外的任何东西」，因此这里
只测两件事：

* 包内所有模块都能**只靠本包与声明的依赖**导入（没有任何 ``raricy_bot``）；
* 提取出来的支撑原语行为未变：站点时间换算、练手盘页面解析、小时信号与结算
  公式、消息 DTO、安全堆栈与数据档案锁。

财务逻辑本身由 ``tests/test_ledger.py`` / ``tests/test_trader.py`` 覆盖，这里不重复。
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone

import pytest

from raricy_capital.chat_models import ChatMessage
from raricy_capital.hourly_signals import (
    DAY,
    FEE,
    HOUR,
    HourSignal,
    closed_signal,
    payout_units,
)
from raricy_capital.safe_logging import safe_stack
from raricy_capital.site_protocol import parse_trade_page, site_time_ms

UTC = timezone.utc
BASE = 1800000000000  # 与 test_trader 同一把尺子：整点、且能被 HOUR 整除


# ------------------------------------------------------------------ 独立导入
def test_every_module_imports_without_the_old_repo():
    names = [
        'raricy_capital',
        'raricy_capital.__main__',
        'raricy_capital.contracts',
        'raricy_capital.store',
        'raricy_capital.ledger',
        'raricy_capital.config',
        'raricy_capital.client',
        'raricy_capital.adapters',
        'raricy_capital.commands',
        'raricy_capital.payments',
        'raricy_capital.strategy',
        'raricy_capital.trader',
        'raricy_capital.runtime',
        'raricy_capital.web',
        'raricy_capital.operations',
        'raricy_capital.data_lock',
        'raricy_capital.chat_models',
        'raricy_capital.hourly_signals',
        'raricy_capital.safe_logging',
        'raricy_capital.site_protocol',
    ]
    for name in names:
        module = importlib.import_module(name)
        assert module is not None  # 导入失败会在上面直接抛 ImportError


# ---------------------------------------------------------------- 站点协议
def test_site_time_ms_turns_fake_z_wall_clock_into_real_utc():
    text = '2026-09-14T11:52:03.000Z'
    literal = int(datetime(2026, 9, 14, 11, 52, 3, tzinfo=UTC).timestamp() * 1000)
    # 站点把 UTC+8 墙钟贴上 Z 标签，减 8 小时才是真实 UTC 毫秒。
    assert site_time_ms(text) == literal - 8 * 3600 * 1000
    # 显式关闭墙钟换算时保留标签字面值（只有 K 线 openTime 走这条路）。
    assert site_time_ms(text, wall_clock=False) == literal
    with pytest.raises(ValueError):
        site_time_ms('2026-09-14T11:52:03')  # 无时区：不能猜


def test_parse_trade_page_reads_props_and_rejects_shape_changes():
    panel = {
        'balance': 42.5,
        'positions': [
            {'id': 'p1', 'symbol': 'BTCUSDT', 'stake': 10, 'entryPrice': 68412.35,
             'leverage': 5, 'liquidationPrice': 54729.88, 'openedAt': '2026-10-03T14:22:03.000Z'},
        ],
        'feeRate': 0.0002,
        'minStake': 1,
        'leverageOptions': [1, 2, 3, 5, 10, 20, 100],
        'leverageEnabled': True,
    }
    line = '3:' + json.dumps(panel, separators=(',', ':'))
    html = (
        '<html><script>self.__next_f.push('
        + json.dumps([1, line])
        + ')</script></html>'
    )
    parsed = parse_trade_page(html)
    assert parsed['balance'] == 42.5
    assert parsed['positions'][0]['id'] == 'p1'
    with pytest.raises(ValueError):
        parse_trade_page('<html>没有 props</html>')


# --------------------------------------------------------------- 小时信号
def _history(count=300):
    t = BASE
    return [[t + i * HOUR, 100 + i * .1, 100.3 + i * .1, 99.8 + i * .1, 100.1 + i * .1, 1.]
            for i in range(count)]


def test_closed_signal_uses_only_closed_hours_and_frozen_indicators():
    now = BASE + 300 * HOUR + 1000
    candles = _history()
    signal = closed_signal(candles, now)
    assert isinstance(signal, HourSignal)
    assert signal.end_ms == (now // HOUR) * HOUR  # 只认刚过去的那个整点
    assert signal.enter is True and signal.exit is False  # 单调上行 -> 趋势做多
    assert signal.change > 0 and 0 < signal.distance < 1

    # 尚未走完的那根不能参与：少一根已收盘小时就必须拒绝。
    with pytest.raises(ValueError):
        closed_signal(_history(205), now)
    # K 线不落在整点（或出现断档）一律拒绝，绝不猜。
    broken = _history()
    broken[-1][0] += 1
    with pytest.raises(ValueError):
        closed_signal(broken, now)


def test_payout_units_matches_the_site_settlement_formula():
    # n + n*leverage*(ratio-1) - n*leverage*ratio*fee，向下取整且不为负。
    assert payout_units(100.0, 100.0, 101.0, 3) == 1_029_394
    assert payout_units(100.0, 100.0, 50.0, 3) == 0
    assert DAY == 86400000 and HOUR == 3600000 and FEE == .0002


# ------------------------------------------------------------------ DTO
def test_chat_message_parses_and_rejects_unlocatable_ids():
    message = ChatMessage.from_dict({
        'id': 10,
        'channel_id': 'd_1',
        'author': {'id': 'u1', 'username': 'alice'},
        'content': 'hi',
        'created_at': '2026-09-11T20:31:05.000Z',
    })
    assert message.id == 10 and message.author.id == 'u1'
    assert message.image is None and message.reply is None and message.pat is None
    with pytest.raises(ValueError):
        ChatMessage.from_dict({'channel_id': 'd_1'})  # 无法定位的消息不能静默放过


# -------------------------------------------------------------- 安全堆栈
def test_safe_stack_keeps_only_module_function_and_line():
    def boom():
        raise RuntimeError('上游正文 不应该出现')

    try:
        boom()
    except RuntimeError as exc:
        stack = safe_stack(exc)

    assert stack is not None
    assert 'test_support.boom:' in stack
    assert 'RuntimeError' not in stack and '不应该出现' not in stack
    assert '/' not in stack and '\\' not in stack
    assert safe_stack(None) is None


# -------------------------------------------------------------- 数据档案锁
def test_data_lock_path_release_is_idempotent_and_reusable(tmp_path):
    from raricy_capital.data_lock import DataLockError, acquire_data_lock

    lock = acquire_data_lock(tmp_path)
    try:
        # 锁标识由规范化后的数据目录派生，锁文件放在该目录内。
        assert lock.path.name == '.raricy-data.lock'
        assert lock.path.parent.name == tmp_path.name
        assert lock.path.exists()  # 锁由内核持有，文件只作占用诊断
    finally:
        lock.release()
        lock.release()  # 幂等：重复释放不抛

    # 释放后可以重新取得（进程崩溃时也由操作系统回收，没有陈旧锁需要人工清理）。
    lock = acquire_data_lock(tmp_path)
    lock.release()

    # 目录不可用时是稳定的类别码错误，而不是静默放过。
    blocked = tmp_path / 'a-file'
    blocked.write_text('x', encoding='utf-8')
    with pytest.raises(DataLockError):
        acquire_data_lock(blocked)
