"""练手盘页面解析与站点时间换算（冻结的上游协议片段）。

只保留两个函数，行为必须逐字不变：

* :func:`site_time_ms` —— 站点把 ``created_at`` / ``opened_at`` 写成
  「UTC+8 墙上时间贴 Z 标签」的假 UTC（``nowForDb()``），``wall_clock=True``
  时减 8 小时换算成真实 UTC 毫秒；只有 K 线的 ``openTime`` 是真 UTC。
* :func:`parse_trade_page` —— 只读服务端渲染的 ``/fish/trade`` 页面 props，
  绝不从可见文本推断字段；形状变化时抛 ``ValueError`` 而不是猜。
"""

from __future__ import annotations

from datetime import datetime
import json, math, re


def site_time_ms(text: str, *, wall_clock: bool = True) -> int:
    dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('invalid_site_time')
    return int(dt.timestamp() * 1000) - (28800000 if wall_clock and text.endswith('Z') else 0)


def parse_trade_page(html: str) -> dict:
    """Extract the actual server-rendered TradePanel props, never infer from text."""
    streams = []
    for match in re.finditer(r'self\.__next_f\.push\((\[.*?\])\)\s*;?\s*</script>', html, re.S):
        try:
            packet = json.loads(match.group(1))
            if packet[0] == 1 and isinstance(packet[1], str):
                streams.append(packet[1])
        except (ValueError, IndexError, TypeError):
            continue
    panels = []

    def walk(x):
        if isinstance(x, dict):
            # 面板必备键故意不含 ``leverageOptions``：站点 2026-10-07 起把杠杆从
            # props 白名单改成「1–100 整数 + 服务端范围校验」，页面不再渲染该数组。
            # 新形状（缺 ``leverageOptions``）与旧形状（带该数组）都接受；客户端把缺失
            # 视为「整数范围内任意值」，见 client.LEVERAGE_MIN / LEVERAGE_MAX。
            if {'balance', 'positions', 'feeRate', 'minStake', 'leverageEnabled'} <= x.keys():
                panels.append(x)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    for line in ''.join(streams).splitlines():
        try:
            walk(json.loads(line.split(':', 1)[1]))
        except (ValueError, IndexError):
            continue
    if len(panels) != 1:
        raise ValueError('trade_page_shape_changed')
    p = panels[0]
    if not isinstance(p['positions'], list) or not isinstance(p['balance'], (int, float)) or not math.isfinite(p['balance']):
        raise ValueError('invalid_snapshot')
    for pos in p['positions']:
        if not isinstance(pos, dict) or not {'id', 'symbol', 'stake', 'entryPrice', 'leverage', 'openedAt'} <= pos.keys():
            raise ValueError('invalid_position_snapshot')
    return p


__all__ = ['parse_trade_page', 'site_time_ms']
