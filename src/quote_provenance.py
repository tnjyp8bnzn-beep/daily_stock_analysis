"""Quote provenance shared by prompts and both report renderers."""

from datetime import datetime, timedelta, timezone
from typing import Any

from src.report_language import get_report_labels
from src.market_data_quality import validate_bar


def quote_freshness(quote: dict, provider_time: datetime, fetched_at: datetime,
                    ttl: int) -> tuple[bool | None, str]:
    """Use exchange sessions, never a long TTL, to validate a resting close.

    Only complete quotes at the actual session boundary can rest overnight or
    through holidays. An old intraday quote cannot become a valid closing quote.
    This does not promote a vendor quote to an exchange-certified daily bar.
    """
    age = (fetched_at - provider_time).total_seconds()
    if age < 0:
        return None, 'unknown'
    try:
        import exchange_calendars as xcals
        from zoneinfo import ZoneInfo
        from src.core.trading_calendar import MARKET_EXCHANGE, MARKET_TIMEZONE, get_market_for_stock

        market = quote.get('market') or get_market_for_stock(str(quote.get('code') or ''))
        calendar = xcals.get_calendar(MARKET_EXCHANGE[market])
        local = fetched_at.astimezone(ZoneInfo(MARKET_TIMEZONE[market]))
        session = calendar.date_to_session(local.date(), direction='previous')
        opening, closing = calendar.session_open(session), calendar.session_close(session)
        if fetched_at < opening:
            session = calendar.previous_session(session)
            closing = calendar.session_close(session)
        complete = validate_bar(quote, quote=True)['status'] == 'ok'
        # A close may rest only until the next session opens; dates are calendar
        # sessions, so weekends, holidays and half-day closes are handled too.
        if fetched_at >= closing and closing <= provider_time <= closing + timedelta(minutes=1):
            return (False, 'session_close') if complete else (None, 'incomplete_close')
        if calendar.is_session(local.date()) and calendar.session_has_break(session):
            start, end = calendar.session_break_start(session), calendar.session_break_end(session)
            if start <= fetched_at < end and start <= provider_time <= start + timedelta(minutes=1):
                return (False, 'session_break') if complete else (None, 'incomplete_break')
        # A recent vendor heartbeat outside a real session is not proof of a
        # regular-session trade. Keep the age, but do not certify it as fresh.
        provider_date = provider_time.astimezone(ZoneInfo(MARKET_TIMEZONE[market])).date()
        if not calendar.is_session(provider_date):
            return (True, 'stale') if age > ttl else (None, 'unknown_session')
        provider_session = calendar.date_to_session(provider_date)
        provider_open = calendar.session_open(provider_session)
        provider_close = calendar.session_close(provider_session)
        in_session = provider_open <= provider_time <= provider_close
        if calendar.session_has_break(provider_session):
            in_session = in_session and not (
                calendar.session_break_start(provider_session) < provider_time
                < calendar.session_break_end(provider_session)
            )
        if not in_session:
            return (True, 'stale') if age > ttl else (None, 'unknown_session')
        if not complete and age <= ttl:
            return None, 'incomplete_quote'
    except (ImportError, KeyError, ValueError, OverflowError, TypeError):
        return (True, 'stale') if age > ttl else (None, 'unknown')
    return (True, 'stale') if age > ttl else (False, 'fresh')


def confirmed_timestamp(value: Any) -> str | None:
    """Only accept a complete timestamp with an explicit timezone."""
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def quote_metadata(context: dict) -> dict:
    daily = context.get('daily_snapshot', context.get('today')) or {}
    realtime = context.get('realtime') or {}
    phase = context.get('market_phase_context') or {}
    daily_date = str(daily.get('date') or context.get('date') or '')[:10]
    session_date = str(phase.get('session_date') or phase.get('market_local_time') or '')[:10]
    # A bar can be a close only when its date/session supports that claim.
    complete = bool(
        not daily.get('is_estimated') and not daily.get('is_partial_bar')
        and validate_bar(daily, pre_close=(context.get('yesterday') or {}).get('close'))['status'] == 'ok'
        and not str(daily.get('data_source', '')).startswith('realtime:')
        and daily_date and session_date
        and (daily_date < session_date or (
            daily_date == session_date and phase.get('phase') == 'postmarket'
        ))
    )
    provider_time = confirmed_timestamp(realtime.get('provider_timestamp'))
    fetched_at = confirmed_timestamp(realtime.get('fetched_at'))
    delay = None
    if provider_time and fetched_at:
        delta = (datetime.fromisoformat(fetched_at) - datetime.fromisoformat(provider_time)).total_seconds()
        if delta >= 0:
            delay = int(delta)
    return {
        'daily_is_final': complete,
        'daily_date': daily_date or None,
        'daily_source': daily.get('data_source') or daily.get('source'),
        'daily_provider_timestamp': confirmed_timestamp(daily.get('provider_timestamp')),
        'daily_fetched_at': confirmed_timestamp(daily.get('fetched_at')),
        'provider_timestamp': provider_time,
        'fetched_at': fetched_at,
        'stale_seconds': delay,
        'is_stale': realtime.get('is_stale'),
        'freshness_status': realtime.get('freshness_status') or 'unknown',
        'quote_time_status': 'confirmed' if provider_time else 'unknown',
        # Latest quote is not promoted to an exchange-confirmed closing price.
        'price_type': 'latest_quote',
        'source': getattr(realtime.get('source'), 'value', realtime.get('source')),
    }


def provenance_lines(snapshot: dict, language: str = 'zh') -> list[str]:
    """Render unknown times explicitly, including for legacy snapshots."""
    labels = get_report_labels(language)
    unknown = labels['quote_time_unknown']
    delay = snapshot.get('stale_seconds')
    rows = [
        (labels['source_label'], snapshot.get('source') or unknown),
        (labels['daily_bar_label'], snapshot.get('daily_date') or unknown),
        (labels['daily_source_label'], snapshot.get('daily_source') or unknown),
        (labels['daily_quote_time_label'], snapshot.get('daily_provider_timestamp') or unknown),
        (labels['daily_fetch_time_label'], snapshot.get('daily_fetched_at') or unknown),
        (labels['quote_time_label'], snapshot.get('provider_timestamp') or unknown),
        (labels['fetch_time_label'], snapshot.get('fetched_at') or unknown),
        (labels['quote_delay_label'], f'{delay}s' if delay is not None else unknown),
    ]
    status = snapshot.get('freshness_status')
    if status:
        rows.append(('报价有效性' if language == 'zh' else 'Quote validity', str(status)))
    quality = snapshot.get('market_data_validation') or {}
    if quality.get('issues') or quality.get('missing_fields'):
        detail = ', '.join(quality.get('issues', []) + quality.get('missing_fields', []))
        rows.append(('数据校验（异常或缺失）' if language == 'zh' else 'Data validation (invalid or missing)', detail))
    return ['', f"| {labels['provenance_label']} | |", '|---|---|'] + [
        f'| {key} | {value} |' for key, value in rows
    ]
