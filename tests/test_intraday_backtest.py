"""Synthetic rule/engine safety fixtures, never claimed as real market returns."""

from datetime import datetime

import pandas as pd
import pytest

from scripts.research.intraday_backtest import ChinaCommission, run_backtest
from src.core.intraday_signals import CN


def daily():
    return pd.DataFrame({'date': pd.bdate_range(end='2026-10-15', periods=30),
                         'close': [9.71 + i * .01 for i in range(30)],
                         'high': [10] * 30, 'volume': [100_000] * 30})


def quotes():
    result = []
    for day, minute, price in [(16, 30, 10.5), (16, 31, 10.55), (16, 32, 9.5),
                                (16, 33, 9.4), (19, 30, 9.4), (19, 31, 9.3)]:
        result.append({'timestamp': datetime(2026, 10, day, 10, minute, tzinfo=CN).isoformat(),
                       'code': '600111', 'name': '测试股票', 'price': price, 'open_price': 10.,
                       'high': 10.6, 'low': 9., 'pre_close': 10., 'change_pct': (price / 10 - 1) * 100,
                       'volume': 20_000_000., 'amount': price * 20_000_000, 'volume_ratio': 2.2,
                       'upper_limit': 11., 'lower_limit': 9., 'suspended': 0})
    return pd.DataFrame(result)


def test_next_quote_execution_costs_and_t_plus_one():
    result = run_backtest(quotes(), daily(), evaluate_from='2026-10-16')
    assert len(result['fills']) == 2
    buy, sell = result['fills']
    assert buy['side'] == 'buy' and sell['side'] == 'sell'
    assert buy['price'] == pytest.approx(10.55 * 1.001)
    assert '2026-10-16T10:31' in buy['time']
    assert '2026-10-19' in sell['time']  # Same-day risk cannot liquidate a T+1 position.
    assert buy['quantity'] % 100 == 0
    assert buy['fees'] >= 5 and sell['fees'] > 5
    assert result['final_equity'] < result['initial_cash']
    assert result['max_drawdown'] > 0


@pytest.mark.parametrize('block', ['upper_limit', 'suspended'])
def test_entry_blocked_at_limit_or_suspension(block):
    frame = quotes()
    if block == 'upper_limit':
        frame.loc[1:, 'upper_limit'] = frame.loc[1:, 'price']
    else:
        frame.loc[1:, 'suspended'] = 1
    result = run_backtest(frame, daily(), evaluate_from='2026-10-16')
    assert result['fills'] == []
    assert result['pending_order'] is True


def test_limit_down_prevents_exit():
    frame = quotes()
    frame.loc[4:, 'lower_limit'] = frame.loc[4:, 'price']
    result = run_backtest(frame, daily(), evaluate_from='2026-10-16')
    assert [fill['side'] for fill in result['fills']] == ['buy']
    assert result['pending_order'] is True


def test_missing_real_volume_ratio_does_not_create_buy():
    frame = quotes()
    frame['volume_ratio'] = None
    assert run_backtest(frame, daily(), evaluate_from='2026-10-16')['fills'] == []


@pytest.mark.parametrize('field', ['upper_limit', 'timestamp', 'volume_ratio'])
def test_missing_historical_evidence_is_an_error(field):
    with pytest.raises(ValueError, match='authentic_quote_fields'):
        run_backtest(quotes().drop(columns=field), daily(), evaluate_from='2026-10-16')


def test_out_of_sample_and_future_bars_never_enter_history():
    frame = pd.concat([daily(), pd.DataFrame([{'date': pd.Timestamp('2026-10-20'),
                                             'close': 999., 'high': 999., 'volume': 1}])])
    actual = run_backtest(quotes(), frame, evaluate_from='2026-10-16')
    baseline = run_backtest(quotes(), daily(), evaluate_from='2026-10-16')
    assert actual['fills'] == baseline['fills']
    assert run_backtest(quotes(), frame, evaluate_from='2026-10-19')['fills'] == []


def test_costs_explicit_and_sell_stamp_only():
    costs = ChinaCommission()
    assert costs.getcommission(100, 10) == 5
    assert costs.getcommission(-100, 10) == 5.5


def test_times_must_be_zoned_and_ordered():
    frame = quotes()
    frame.loc[0, 'timestamp'] = '2026-10-16T10:30:00'
    with pytest.raises(ValueError, match='timezone_required'):
        run_backtest(frame, daily(), evaluate_from='2026-10-16')
    with pytest.raises(ValueError, match='strictly_increasing'):
        run_backtest(quotes().iloc[::-1], daily(), evaluate_from='2026-10-16')
