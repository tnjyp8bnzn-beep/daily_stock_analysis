"""Evidence-only intraday rules, also reusable by the offline research adapter."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from src.market_data_quality import finite_number, validate_bar
from src.core.trading_calendar import MarketPhase, infer_market_phase

CN = ZoneInfo('Asia/Shanghai')


def quote_issues(quote, now, max_age=90):
    if not quote:
        return ['quote_unavailable']
    check = validate_bar(quote, quote=True)
    issues = check['issues'] + check['missing_fields']
    try:
        quoted = datetime.fromisoformat(quote['provider_timestamp'])
        fetched = datetime.fromisoformat(quote['fetched_at'])
        if quoted.utcoffset() is None or fetched.utcoffset() is None:
            raise ValueError('timezone_missing')
        if quoted.astimezone(CN).date() != now.astimezone(CN).date():
            issues.append('wrong_trading_date')
        if infer_market_phase('cn', current_time=quoted) not in (
            MarketPhase.INTRADAY, MarketPhase.CLOSING_AUCTION,
        ):
            issues.append('quote_outside_verified_session')
        for label, stamp in [('quote', quoted), ('fetch', fetched)]:
            age = (now - stamp).total_seconds()
            if age < -5 or age > max_age:
                issues.append(f'{label}_time_invalid')
    except (KeyError, TypeError, ValueError):
        issues.append('quote_time_unconfirmed')
    if quote.get('is_stale'):
        issues.append('provider_stale')
    for key in ('volume', 'amount'):
        if (finite_number(quote.get(key)) or 0) <= 0:
            issues.append(f'{key}_not_positive')
    issues.extend(quote.get('validation_issues') or [])
    return list(dict.fromkeys(issues))


def history_levels(history, trading_date):
    """Use only completed bars; duplicates, gaps and invalid prices fail closed."""
    frame = history.copy().reset_index(drop=True)
    if not {'date', 'close', 'high', 'volume'}.issubset(frame.columns):
        raise ValueError('history_columns_missing')
    dates = pd.to_datetime(frame['date'], errors='coerce').dt.date
    if dates.isna().any() or dates.duplicated().any():
        raise ValueError('history_dates_invalid')
    frame = frame.loc[dates < trading_date].copy()
    frame['date'] = dates.loc[frame.index]
    frame = frame.sort_values('date').tail(20)
    if len(frame) < 20 or (trading_date - frame['date'].iloc[-1]).days > 7:
        raise ValueError('completed_history_insufficient_or_old')
    for key in ('close', 'high', 'volume'):
        values = frame[key].map(finite_number)
        if values.isna().any() or (values <= 0).any():
            raise ValueError(f'history_{key}_invalid')
        frame[key] = values.astype(float)
    if (frame['close'] > frame['high'] + .011).any():
        raise ValueError('history_range_invalid')
    return {'ma5': float(frame['close'].tail(5).mean()),
            'ma20': float(frame['close'].mean()),
            'previous_close': float(frame['close'].iloc[-1]),
            'breakout': float(frame['high'].max()),
            'history_date': frame['date'].iloc[-1].isoformat()}


def evaluate_signal(quote, levels):
    """A technical exit reference is not an invented personalized stop price."""
    price = finite_number(quote.get('price'))
    if price is None or price <= 0:
        raise ValueError('invalid_price')
    result = {'signal': 'watch', 'warning_price': levels['ma20'],
              'stop_loss': None, 'target_price': None, 'reference_support': levels['ma20'],
              'reason': '未满足量价突破条件；仅监控', **levels}
    if price < levels['ma20']:
        result.update(signal='exit_risk', reason='现价跌破上一交易日已完成日线 MA20；持仓风险预警')
    else:
        ratio = finite_number(quote.get('volume_ratio'))
        change = finite_number(quote.get('change_pct'))
        # ETF/indices, ST and stocks near their standard main-board limit are not entry candidates.
        code = str(quote.get('code', ''))
        name = str(quote.get('name') or '').strip()
        eligible = code.startswith(('000', '001', '002', '003', '600', '601', '603', '605'))
        if (eligible and name and 'ST' not in name.upper()
                and ratio is not None and ratio >= 2
                and change is not None and 2 <= change < 9.5
                and (finite_number(quote.get('amount')) or 0) >= 100_000_000
                and price > levels['breakout'] and levels['ma5'] > levels['ma20']):
            result.update(signal='entry_candidate', reason='突破前20根完整日线高点、MA5高于MA20、真实量比≥2且成交额≥1亿元')
        elif ratio is None:
            result['reason'] = '量比数据不足，禁止生成买入信号或缩量/放量结论'
    return result
