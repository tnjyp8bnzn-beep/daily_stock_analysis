"""Quote provenance shared by prompts and both report renderers."""

from datetime import datetime, timezone
from typing import Any

from src.report_language import get_report_labels


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
    return ['', f"| {labels['provenance_label']} | |", '|---|---|'] + [
        f'| {key} | {value} |' for key, value in rows
    ]
