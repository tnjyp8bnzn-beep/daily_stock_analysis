# SPDX-License-Identifier: GPL-3.0-or-later
# This optional standalone research program uses Backtrader. See COPYING.
"""Replay authentic sampled quotes with Backtrader, never fabricate volume ratios.

The live service does not import this program. It reads only local CSV files and
does not contact a broker, fetch data or send notifications. Execution at the
next observed price is a sampling model, not proof that an order could be filled.
"""

import argparse
from datetime import date
import json
from pathlib import Path
import sys

import backtrader as bt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.core.intraday_signals import CN, evaluate_signal, history_levels, quote_issues  # noqa: E402
from src.core.trading_calendar import MarketPhase, infer_market_phase  # noqa: E402


class QuoteFeed(bt.feeds.PandasData):
    lines = ('upper_limit', 'lower_limit', 'suspended')
    params = (('upper_limit', -1), ('lower_limit', -1), ('suspended', -1))


class ChinaCommission(bt.CommInfoBase):
    params = (('commission', .0003), ('stamp_duty', .0005), ('minimum', 5.),
              ('stocklike', True), ('commtype', bt.CommInfoBase.COMM_PERC), ('percabs', True))

    def _getcommission(self, size, price, pseudoexec):
        if not size:
            return 0.
        notional = abs(size) * price
        return max(self.p.minimum, notional * self.p.commission) + (
            notional * self.p.stamp_duty if size < 0 else 0)


class ChinaBroker(bt.brokers.BackBroker):
    """Conservative limits/suspension/T+1 on the mature execution engine."""
    def __init__(self):
        super().__init__()
        self.last_buy = {}

    def _try_exec(self, order):
        data = order.data
        today = data.datetime.date(0)
        price = data.open[0]
        if data.suspended[0]:
            return
        if order.isbuy():
            if price * (1 + self.p.slip_perc) >= data.upper_limit[0]:
                return
        elif (self.last_buy.get(data) == today
              or price * (1 - self.p.slip_perc) <= data.lower_limit[0]):
            return
        before = order.executed.size
        super()._try_exec(order)
        if order.isbuy() and order.executed.size > before:
            self.last_buy[data] = today


class RuleStrategy(bt.Strategy):
    params = (('quotes', None), ('daily', None), ('evaluate_from', None), ('allocation', .1))

    def __init__(self):
        self.order = None
        self.fills = []
        self.blocked = []
        self.equity = []

    def notify_order(self, order):
        if order.status == order.Completed:
            self.fills.append({'side': 'buy' if order.isbuy() else 'sell',
                               'time': bt.num2date(order.executed.dt).replace(tzinfo=CN).isoformat(),
                               'quantity': abs(order.executed.size), 'price': order.executed.price,
                               'fees': order.executed.comm})
        if order.status in (order.Completed, order.Canceled, order.Margin, order.Rejected):
            self.order = None

    def next(self):
        quote = self.p.quotes[len(self.data) - 1]
        now = pd.Timestamp(quote['timestamp']).to_pydatetime().astimezone(CN)
        if now.date() < self.p.evaluate_from:
            return
        self.equity.append(float(self.broker.getvalue()))
        if self.order:
            return
        issues = quote_issues(quote, now)
        phase = infer_market_phase('cn', current_time=now)
        if phase not in (MarketPhase.INTRADAY, MarketPhase.CLOSING_AUCTION):
            issues.append('outside_session')
        if issues:
            self.blocked.append({'timestamp': quote['timestamp'], 'issues': issues})
            return
        try:
            levels = history_levels(self.p.daily, now.date())
        except ValueError as exc:
            self.blocked.append({'timestamp': quote['timestamp'], 'issues': [str(exc)]})
            return
        if abs(quote['pre_close'] - levels['previous_close']) > max(.02, levels['previous_close'] * .002):
            self.blocked.append({'timestamp': quote['timestamp'], 'issues': ['history_quote_price_basis_mismatch']})
            return
        signal = evaluate_signal(quote, levels)['signal']
        if signal == 'exit_risk' and self.position.size > 0:
            self.order = self.close()
        elif (signal == 'entry_candidate' and not self.position.size
              and phase == MarketPhase.INTRADAY):
            size = int(self.broker.getcash() * self.p.allocation / quote['price'] / 100) * 100
            if size > 0:
                self.order = self.buy(size=size)


def run_backtest(quotes, daily, *, evaluate_from, cash=100_000, commission=.0003,
                 stamp_duty=.0005, minimum_fee=5., slippage=.001):
    """One-symbol sample; require genuine point-in-time limits and timestamps."""
    if not 0 <= slippage < .1 or min(cash, minimum_fee) <= 0 or min(commission, stamp_duty) < 0:
        raise ValueError('invalid_cost_parameters')
    frame = quotes.copy()
    required = {'timestamp', 'code', 'name', 'price', 'open_price', 'high', 'low', 'pre_close',
                'change_pct', 'volume', 'amount', 'volume_ratio', 'upper_limit', 'lower_limit', 'suspended'}
    if not required.issubset(frame.columns) or frame.empty:
        raise ValueError('authentic_quote_fields_and_price_limits_required')
    if frame['code'].astype(str).nunique() != 1:
        raise ValueError('one_symbol_per_backtest')
    stamps = [pd.Timestamp(value) for value in frame['timestamp']]
    if any(stamp.tzinfo is None for stamp in stamps):
        raise ValueError('timezone_required')
    index = pd.DatetimeIndex(stamps).tz_convert(CN).tz_localize(None)
    if index.has_duplicates or not index.is_monotonic_increasing:
        raise ValueError('strictly_increasing_quote_times_required')
    numeric = frame[['price', 'upper_limit', 'lower_limit', 'suspended']].apply(pd.to_numeric, errors='coerce')
    if (numeric.isna().any().any() or not numeric['suspended'].isin([0, 1]).all()
            or (numeric[['price', 'upper_limit', 'lower_limit']] <= 0).any().any()
            or (numeric['lower_limit'] > numeric['price']).any()
            or (numeric['price'] > numeric['upper_limit']).any()):
        raise ValueError('invalid_price_limits_or_suspension_flags')
    rows = frame.where(pd.notna(frame), None).to_dict('records')
    for row in rows:
        row['code'] = str(row['code'])
        row['provider_timestamp'] = row['timestamp']
        row['fetched_at'] = row['timestamp']
    # Do not use the day's cumulative high/low to simulate an earlier fill.
    feed = pd.DataFrame({key: numeric['price'].to_numpy() for key in ('open', 'high', 'low', 'close')}, index=index)
    feed['volume'] = 0  # Size/liquidity are NOT simulated by a one-point sample.
    for key in ('upper_limit', 'lower_limit', 'suspended'):
        feed[key] = numeric[key].to_numpy()
    cerebro = bt.Cerebro(stdstats=False)
    broker = ChinaBroker()
    broker.setcash(cash)
    broker.set_slippage_perc(slippage, slip_open=True, slip_match=True, slip_out=True)
    broker.addcommissioninfo(ChinaCommission(commission=commission, stamp_duty=stamp_duty, minimum=minimum_fee))
    cerebro.setbroker(broker)
    cerebro.adddata(QuoteFeed(dataname=feed))
    cerebro.addstrategy(RuleStrategy, quotes=rows, daily=daily,
                        evaluate_from=date.fromisoformat(str(evaluate_from)))
    result = cerebro.run()[0]
    equity = pd.Series(result.equity, dtype=float)
    peak = equity.cummax().clip(lower=cash)
    drawdown = float((1 - equity / peak).max()) if len(equity) else None
    return {'engine': 'Backtrader 1.9.78.123', 'evaluate_from': str(evaluate_from),
            'initial_cash': cash, 'final_equity': broker.getvalue(),
            'max_drawdown': drawdown, 'fills': result.fills, 'blocked': result.blocked,
            'pending_order': result.order is not None,
            'assumptions': {'commission': commission, 'minimum_fee': minimum_fee,
                            'sell_stamp_duty': stamp_duty, 'slippage': slippage, 'allocation': .1},
            'limitations': ['sampled_next_quote_price_not_guaranteed_execution',
                            'no_orderbook_or_liquidity_model', 'single_symbol_not_market_selection_performance',
                            'historical_volume_ratio_and_price_limits_must_be_authentic',
                            'point_in_time_daily_history_and_out_of_sample_selection_required']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quotes', required=True)
    parser.add_argument('--daily', required=True)
    parser.add_argument('--evaluate-from', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    report = run_backtest(pd.read_csv(args.quotes, dtype={'code': str}),
                          pd.read_csv(args.daily), evaluate_from=args.evaluate_from)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
