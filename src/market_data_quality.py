"""Deterministic checks on canonical shares/currency data, without guessing units."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def validate_bar(data: Mapping[str, Any], *, quote: bool = False,
                 pre_close: Any = None) -> dict:
    """Check one time-consistent bar; never infer a correction from a 100x error.

    VWAP is checked against the same bar's range, with 2% tolerance for rounded
    upstream fields. Do not apply this check to historical adjusted OHLC versus
    unadjusted turnover; callers validate the current analysis bar only.
    """
    aliases = {'close': 'price', 'open': 'open_price', 'pct_chg': 'change_pct'} if quote else {}
    keys = ('close', 'open', 'high', 'low', 'volume', 'amount', 'pct_chg')
    values = {key: finite_number(data.get(aliases.get(key, key))) for key in keys}
    missing, issues, invalid = [], [], []
    for key in keys:
        raw = data.get(aliases.get(key, key))
        value = values[key]
        if raw is None:
            missing.append(key)
        elif value is None or (key in ('close', 'open', 'high', 'low') and value <= 0) or (
            key in ('volume', 'amount') and value < 0
        ):
            issues.append(f'{key}_invalid')
            invalid.append(key)
    low, high, close, opening = (values[k] for k in ('low', 'high', 'close', 'open'))
    if low is not None and high is not None and low > 0 and high > 0:
        if low > high or any(v is not None and not low - .011 <= v <= high + .011
                             for v in (close, opening)):
            issues.append('ohlc_range_inconsistent')
            invalid.extend(('open', 'high', 'low', 'close'))
        volume, amount = values['volume'], values['amount']
        if volume is not None and amount is not None and volume > 0 and amount > 0:
            if not low * .98 <= amount / volume <= high * 1.02:
                issues.append('amount_volume_price_mismatch')
                invalid.extend(('volume', 'amount'))
        elif volume is not None and amount is not None and (volume > 0) != (amount > 0):
            issues.append('amount_volume_zero_mismatch')
            invalid.extend(('volume', 'amount'))
    raw_previous = data.get('pre_close')
    if raw_previous is None:
        raw_previous = pre_close
    previous = finite_number(raw_previous)
    if raw_previous is None:
        missing.append('pre_close')
    elif previous is None or previous <= 0:
        issues.append('pre_close_invalid')
        invalid.append('pre_close')
    pct = values['pct_chg']
    if close is not None and close > 0 and previous is not None and previous > 0 and pct is not None:
        # Allow one-cent price rounding plus 0.05 percentage points.
        if abs(pct - (close / previous - 1) * 100) > .05 + 1 / previous:
            issues.append('change_pct_inconsistent')
            invalid.append('pct_chg')
    return {'status': 'invalid' if issues else 'partial' if missing else 'ok',
            'missing_fields': missing, 'issues': sorted(set(issues)),
            'invalid_fields': sorted(set(invalid))}


def validate_technical(data: Mapping[str, Any]) -> dict:
    missing, issues = [], []
    for key in ('ma5', 'ma10', 'ma20'):
        value = finite_number(data.get(key))
        if value is None or value <= 0:
            missing.append(key)
    for key, raw in data.items():
        if key.startswith(('rsi_', 'macd_', 'bias_ma')) and not key.endswith(('status', 'signal')):
            value = finite_number(raw)
            if value is None or (key.startswith('rsi_') and not 0 <= value <= 100):
                issues.append(f'{key}_invalid')
    count = finite_number(data.get('sample_count'))
    if count is not None:
        for key, needed in (('ma5', 5), ('ma10', 10), ('ma20', 20),
                            ('rsi_6', 7), ('rsi_12', 13), ('rsi_24', 25), ('macd_warmup', 34)):
            if count < needed and key not in missing:
                missing.append(key)
    return {'status': 'invalid' if issues else 'partial' if missing else 'ok',
            'missing_fields': missing, 'issues': issues}


def context_quality(context: Mapping[str, Any]) -> dict:
    today = context.get('today') or {}
    daily = context.get('daily_snapshot', today) or {}
    realtime = context.get('realtime') or context.get('realtime_quote') or {}
    previous = (context.get('yesterday') or {}).get('close')
    blocks = {'daily_bars': validate_bar(daily, pre_close=previous),
              'technical': validate_technical({**today, **(context.get('trend_analysis') or {})})}
    if realtime:
        blocks['quote'] = validate_bar(realtime, quote=True, pre_close=previous)
    issues = [f'{block}:{issue}' for block, check in blocks.items() for issue in check['issues']]
    missing = [f'{block}:{field}' for block, check in blocks.items() for field in check['missing_fields']]
    return {'status': 'invalid' if issues else 'partial' if missing else 'ok',
            'issues': issues, 'missing_fields': missing, 'blocks': blocks}
