"""Transactional signal edges and bounded notification retries on the existing DB."""

import json

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.storage import (
    DatabaseManager, IntradayMonitorEvent, IntradayMonitorState, IntradayObservation, utc_naive_now,
)


class IntradayRepository:
    def __init__(self, db=None):
        self.db = db or DatabaseManager.get_instance()

    def observe(self, key, payload, notify=False):
        """Invalid observations must never call this method or rearm an edge."""
        signal = payload['signal']
        with self.db.get_session() as session:
            state = session.get(IntradayMonitorState, key)
            if state is None:
                state = IntradayMonitorState(key=key, signal='watch', revision=0)
                session.add(state)
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                state = session.get(IntradayMonitorState, key)
            if state.signal == signal:
                return
            revision = state.revision
            changed = session.execute(update(IntradayMonitorState).where(
                IntradayMonitorState.key == key,
                IntradayMonitorState.revision == revision,
            ).values(signal=signal, revision=revision + 1, updated_at=utc_naive_now())).rowcount
            if not changed:
                session.rollback()
                return
            # Old conditions must not be delivered after recovery or a new edge.
            session.execute(update(IntradayMonitorEvent).where(
                IntradayMonitorEvent.state_key == key,
                IntradayMonitorEvent.status.in_(['pending', 'failed']),
            ).values(status='expired'))
            if signal != 'watch':
                session.add(IntradayMonitorEvent(
                    id=f'{key}:{revision + 1}', state_key=key,
                    payload=json.dumps(payload, ensure_ascii=False, allow_nan=False),
                    status='pending' if notify else 'paper',
                ))
            session.commit()

    def claim(self, key, signal, now):
        """Lease one current edge; bounded retry, including after process restart."""
        with self.db.get_session() as session:
            state = session.get(IntradayMonitorState, key)
            if state is None or state.signal != signal:
                return None
            event_id = f'{key}:{state.revision}'
            changed = session.execute(update(IntradayMonitorEvent).where(
                IntradayMonitorEvent.id == event_id,
                IntradayMonitorEvent.status.in_(['pending', 'failed']),
                IntradayMonitorEvent.attempts < 3,
                IntradayMonitorEvent.retry_after <= now,
            ).values(attempts=IntradayMonitorEvent.attempts + 1, retry_after=now + 120)).rowcount
            session.commit()
            return event_id if changed else None

    def finish(self, event_id, accepted, error=None):
        with self.db.get_session() as session:
            session.execute(update(IntradayMonitorEvent).where(
                IntradayMonitorEvent.id == event_id,
                IntradayMonitorEvent.status.in_(['pending', 'failed']),
            ).values(status='accepted' if accepted else 'failed', error=error))
            session.commit()

    def recent(self, limit=50):
        with self.db.get_session() as session:
            rows = session.scalars(select(IntradayMonitorEvent).order_by(
                IntradayMonitorEvent.created_at.desc()).limit(limit)).all()
            return [{'id': row.id, 'status': row.status, 'attempts': row.attempts,
                     'error': row.error, **json.loads(row.payload)} for row in rows]

    def record_quote(self, code, observed_at, quote, issues):
        # Timestamp is the provider time: a stale cache is not a new market sample.
        key = f"{code}:{quote.get('provider_timestamp') or observed_at}"
        payload = {'code': code, 'observed_at': observed_at, 'quote': quote, 'issues': issues}
        with self.db.get_session() as session:
            if session.get(IntradayObservation, key) is not None:
                return
            session.add(IntradayObservation(key=key, payload=json.dumps(
                payload, ensure_ascii=False, allow_nan=False)))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()

    def observations(self, limit=200):
        with self.db.get_session() as session:
            rows = session.scalars(select(IntradayObservation).order_by(
                IntradayObservation.observed_at.desc()).limit(limit)).all()
            return [json.loads(row.payload) for row in rows]
