"""Continuous session polling, free-source discovery and durable paper signals.

No LLM is called, no order is placed, and no portfolio trade is written here.
"""

import logging
import threading
import time
from datetime import datetime

from src.config import get_config
from src.core.intraday_signals import CN, evaluate_signal, history_levels, quote_issues
from src.core.trading_calendar import MarketPhase, infer_market_phase
from src.services.intraday_portfolio import cn_stock_code, portfolio_context, position_advice

logger = logging.getLogger(__name__)


def discover_candidates():
    """Reuse the vendored AlphaSift pipeline; no paid service or AI ranking."""
    from src.services.screening.config import Config
    from src.services.screening.pipeline import screen

    config = Config.from_env()
    config.snapshot_source_priority = ['sina', 'efinance', 'akshare_em', 'em_datacenter']
    config.snapshot_cache_ttl_seconds = 0
    config.fallback_snapshot_path = None
    result = screen('volume_breakout', config=config, max_output=10, use_llm=False,
                    collect_llm_candidate_context=False, post_analyzers=[],
                    daily_enrich=True, daily_enrich_max_candidates=20)
    if result.degradation:
        logger.warning('Intraday screening degradation: %s', result.degradation)
    # Discovery alone is not a buy: each candidate must pass a fresh quote gate.
    return [pick.code for pick in result.picks]


class IntradayMonitor:
    def __init__(self, *, config_provider=get_config, manager=None, repo=None,
                 portfolio=None, notifier=None, now=None, phase=infer_market_phase,
                 discover=discover_candidates):
        from data_provider.base import DataFetcherManager
        from src.repositories.intraday_repo import IntradayRepository
        from src.services.portfolio_service import PortfolioService

        self.config_provider = config_provider
        self.manager = manager or DataFetcherManager()
        self.repo = repo or IntradayRepository()
        self.portfolio = portfolio or PortfolioService()
        self.notifier = notifier
        self.now = now or (lambda: datetime.now(CN))
        self.phase = phase
        self.discover = discover
        self._lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._history = {}
        self._candidates = []
        self._scan_date = None
        self._scan_at = 0
        self._scan_error = None

    def _scan(self, trading_date):
        try:
            codes = self.discover()
            self._candidates = list(dict.fromkeys(str(code) for code in codes))[:10]
            self._scan_date = trading_date
            self._scan_error = None
        except Exception as exc:
            self._scan_error = type(exc).__name__
            logger.warning('Intraday candidate scan failed: %s', type(exc).__name__)
        finally:
            self._scan_at = time.monotonic()
            self._scan_lock.release()

    def run_once(self):
        if not self._lock.acquire(blocking=False):
            return {'status': 'cycle_already_running'}
        try:
            return self._run_once()
        finally:
            self._lock.release()

    def _run_once(self):
        config = self.config_provider()
        if not getattr(config, 'intraday_monitor_enabled', False):
            return {'status': 'disabled'}
        now = self.now()
        phase = self.phase('cn', current_time=now)
        if phase not in (MarketPhase.INTRADAY, MarketPhase.CLOSING_AUCTION):
            return {'status': 'outside_session', 'phase': phase.value}
        trading_date = now.astimezone(CN).date()
        if (not self._scan_at or time.monotonic() - self._scan_at >= 900) and self._scan_lock.acquire(False):
            threading.Thread(target=self._scan, args=(trading_date,), daemon=True,
                             name='intraday-discovery').start()
        try:
            context = portfolio_context(self.portfolio, getattr(config, 'intraday_portfolio_snapshot_path', ''))
        except Exception as exc:
            context = {'positions': [], 'status': 'unavailable', 'reason': type(exc).__name__}
        codes = list(dict.fromkeys([
            *(row['code'] for row in context['positions']), *config.stock_list,
            *(self._candidates if self._scan_date == trading_date else []),
        ]))
        # All owned/watch stocks survive batching; discovered universe is bounded.
        codes = list(dict.fromkeys(code for raw in codes if (code := cn_stock_code(raw))))
        self.manager.prefetch_realtime_quotes(codes, force_refresh=True)
        stats = {'status': 'ok', 'checked': 0, 'blocked': {}, 'signals': [],
                 'portfolio_status': context['status'], 'scan_error': self._scan_error}
        for code in codes:
            try:
                # Compare with wall clock *after* the request, not cycle start.
                acceptor = lambda quote: not quote_issues(quote.to_dict(), self.now())
                obj = self.manager.get_realtime_quote(code, quote_acceptor=acceptor)
                quote = obj.to_dict() if obj is not None else {}
                check_time = self.now()
                issues = quote_issues(quote, check_time)
                if self.phase('cn', current_time=check_time) != phase:
                    issues.append('session_changed_during_fetch')
                if quote.get('code') != code:
                    issues.append('symbol_mismatch')
                if not issues:
                    self.repo.record_quote(code, check_time.isoformat(), quote, [])
                if issues:
                    stats['blocked'][code] = issues
                    continue
                key = (code, trading_date)
                if key not in self._history:
                    daily, source = self.manager.get_daily_data(code, days=90)
                    self._history[key] = {**history_levels(daily, trading_date), 'history_source': source}
                # History retrieval may be slow. Re-check before an actionable edge.
                checked_now = self.now()
                late_issues = quote_issues(quote, checked_now)
                if self.phase('cn', current_time=checked_now) != phase:
                    late_issues.append('session_changed_during_history_fetch')
                previous = self._history[key]['previous_close']
                if abs(quote['pre_close'] - previous) > max(.02, previous * .002):
                    late_issues.append('history_quote_price_basis_mismatch')
                if late_issues:
                    stats['blocked'][code] = late_issues
                    continue
                decision = evaluate_signal(quote, self._history[key])
                if phase == MarketPhase.CLOSING_AUCTION and decision['signal'] == 'entry_candidate':
                    decision.update(signal='watch', reason='收盘集合竞价，禁止新建仓提醒')
                position = position_advice(context, code, decision['signal'])
                payload = {**decision, 'code': code, 'quote': quote, 'position': position,
                           'observed_at': checked_now.isoformat(), 'kind': 'paper_signal'}
                state_key = f'{trading_date}:{code}:breakout_ma20_v1'
                notify = bool(getattr(config, 'intraday_monitor_notify', False))
                self.repo.observe(state_key, payload, notify=notify)
                stats['checked'] += 1
                if decision['signal'] != 'watch':
                    stats['signals'].append({'code': code, **decision, 'position': position})
                    if notify:
                        self._notify(state_key, payload, checked_now.timestamp())
            except Exception as exc:
                safe_reasons = {'history_columns_missing', 'history_dates_invalid',
                                'completed_history_insufficient_or_old', 'history_close_invalid',
                                'history_high_invalid', 'history_volume_invalid', 'history_range_invalid'}
                reason = str(exc) if isinstance(exc, ValueError) and str(exc) in safe_reasons else None
                stats['blocked'][code] = [type(exc).__name__] + ([reason] if reason else [])
                logger.warning('Intraday observation failed for %s: %s', code, type(exc).__name__)
        self._history = {key: value for key, value in self._history.items() if key[1] == trading_date}
        logger.info('Intraday cycle: checked=%d blocked=%d signals=%d', stats['checked'],
                    len(stats['blocked']), len(stats['signals']))
        return stats

    def _notify(self, key, payload, now):
        current = self.config_provider()
        if not current.intraday_monitor_enabled or not current.intraday_monitor_notify:
            return
        event_id = self.repo.claim(key, payload['signal'], now)
        if event_id is None:
            return
        try:
            if self.notifier is None:
                from src.notification import NotificationService
                self.notifier = NotificationService()
            result = self.notifier.send_with_results(render_alert(payload), route_type='alert', dedup_key=event_id)
            accepted = any(row.success for row in result.channel_results)
            errors = ','.join(f'{row.channel}:{row.error_code or "rejected"}'
                              for row in result.channel_results if not row.success)
            # Another channel accepting the alert must not hide a PushPlus rejection.
            self.repo.finish(event_id, accepted, errors or (None if accepted else 'no_channel_accepted'))
        except Exception as exc:
            self.repo.finish(event_id, False, type(exc).__name__)


def render_alert(payload):
    quote, position = payload['quote'], payload['position']
    label = '买入候选' if payload['signal'] == 'entry_candidate' else '持仓/卖出风险预警'
    return '\n'.join([
        f"## {label} {payload['code']} {quote.get('name', '')}",
        f"现价 {quote['price']} 元；{payload['reason']}",
        f"参考支撑/预警价 {payload['warning_price']:.2f}；实际止损触发价：未配置；目标价：数据不足",
        position['advice'],
        f"持仓证据数量 {position['quantity']}；状态 {position['status']}；当前可卖数量待券商确认",
        f"来源 {quote.get('source')}；行情源标注报价时间 {quote['provider_timestamp']}",
        '交易所报价时间：未提供可独立核验的交易所原始时间，不能当作官方成交时间',
        f"抓取时间 {quote['fetched_at']}；规则检查时间 {payload['observed_at']}",
        '规则信号，未下单、未成交；推送服务接受消息不代表手机已读。',
    ])


def build_intraday_background_tasks(config, *, config_provider=get_config):
    if not getattr(config, 'intraday_monitor_enabled', False):
        return []
    worker = IntradayMonitor(config_provider=config_provider)
    return [{'task': worker.run_once, 'interval_seconds': max(30, config.intraday_monitor_interval_seconds),
             'name': 'intraday_monitor', 'run_immediately': True}]
