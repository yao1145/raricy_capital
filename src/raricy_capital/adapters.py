"""Bridge normalized site units to the trader's research DTOs, with a live gate."""
from __future__ import annotations

from datetime import datetime, timezone

from .contracts import FundError, money_text, money_units, now_ms


class TradingClientAdapter:
    def __init__(self, site_client, store, fund_id: str, *, live: bool):
        self.site = site_client
        self.store = store
        self.fund_id = fund_id
        self.live = live
        self.quote_ms = 0
        self.balance_ms = 0

    async def balance(self):
        result = await self.site.balance()
        self.balance_ms = now_ms()
        return result

    async def quote(self):
        result = await self.site.quote()
        self.quote_ms = now_ms()
        return result

    async def candles(self):
        return await self.site.candles()

    @staticmethod
    def _position(row):
        stamp = row.get('opened_ms')
        return {'id': row['position_id'], 'symbol': row.get('symbol'),
                'stake': money_text(row['stake_units']), 'leverage': row['leverage'],
                'entry_price': row['entry_price'], 'entryPrice': row['entry_price'],
                'liquidation_price': row.get('liquidation_price'),
                'opened_at': datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat() if stamp else None}

    async def snapshot(self):
        data = await self.site.snapshot()
        return {'balance': money_text(data['balance_units']), 'feeRate': data['fee_rate'],
                'minStake': money_text(data['min_stake_units']),
                'leverageOptions': data['leverage_options'], 'leverageEnabled': data['leverage_enabled'],
                'positions': [self._position(p) for p in data['positions']]}

    async def buy(self, amount, leverage, key):
        control = self.store.get('fund_controls', self.fund_id, {})
        hold = self.store.get('settlement_holds', self.fund_id, {}).get('hold', False)
        reconcile = self.store.get('reconciliation_holds', self.fund_id, {}).get('hold', False)
        valuation = self.store.get('valuation_holds', self.fund_id, {}).get('hold', False)
        if not self.live or not control.get('running', False) or hold or reconcile or valuation:
            raise FundError('new_entries_disabled')
        result = await self.site.buy(money_units(amount, positive=True), leverage, key)
        return {'position': self._position(result), 'replayed': result['replayed'],
                'balance': money_text(result['balance_units'])}

    async def sell(self, position_id):
        if not self.live:
            raise FundError('live_required')
        result = await self.site.sell(position_id)
        return {'position_id': result['position_id'], 'payout': money_text(result['payout_units']),
                'profit': money_text(result['profit_units']), 'exit_price': result['exit_price'],
                'liquidated': result['liquidated'], 'replayed': result['replayed'],
                'balance': money_text(result['balance_units'])}
