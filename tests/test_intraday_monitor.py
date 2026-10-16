"""Offline safety tests; synthetic fixtures are not performance evidence."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import json

import pandas as pd
import pytest

from src.config import Config
from src.core.intraday_signals import CN, evaluate_signal, history_levels, quote_issues
from src.core.trading_calendar import MarketPhase
from src.repositories.intraday_repo import IntradayRepository
from src.services.intraday_monitor import IntradayMonitor, discover_candidates, render_alert
from src.services.intraday_portfolio import portfolio_context
from src.services.runtime_scheduler import RuntimeSchedulerService
from src.storage import DatabaseManager


NOW = datetime(2026, 10, 16, 10, 30, tzinfo=CN)


def quote(**changes):
    return {'code': '600111', 'name': '测试股票', 'price': 10.5, 'open_price': 10,
            'high': 10.6, 'low': 9.9, 'pre_close': 10, 'change_pct': 5,
            'volume': 20_000_000, 'amount': 205_000_000, 'volume_ratio': 2.2,
            'provider_timestamp': NOW.isoformat(), 'fetched_at': NOW.isoformat(),
            'source': 'test_only', **changes}


def history():
    return pd.DataFrame({'date': pd.bdate_range(end='2026-10-15', periods=30),
                         'close': [9.71 + i * .01 for i in range(30)],
                         'high': [10] * 30, 'volume': [100_000] * 30})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    env = tmp_path / 'test.env'
    env.write_text('', encoding='utf-8')
    monkeypatch.setenv('ENV_FILE', str(env))
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'test.db'))
    Config.reset_instance()
    DatabaseManager.reset_instance()
    result = IntradayRepository()
    yield result
    DatabaseManager.reset_instance()
    Config.reset_instance()


def worker(repo, codes=None, notify=False, portfolio=None, now=None):
    manager = MagicMock()
    manager.get_realtime_quote.side_effect = lambda code, **kw: SimpleNamespace(
        to_dict=lambda: quote(code=code))
    manager.get_daily_data.return_value = history(), 'test_history'
    service = portfolio or MagicMock()
    if portfolio is None:
        service.get_portfolio_snapshot.return_value = {'accounts': [], 'account_count': 0}
    config = SimpleNamespace(intraday_monitor_enabled=True, intraday_monitor_notify=notify,
                             stock_list=codes or ['600111'], intraday_portfolio_snapshot_path='')
    notifier = MagicMock()
    notifier.send_with_results.return_value = SimpleNamespace(
        channel_results=[SimpleNamespace(success=True, error_code=None, channel='test')])
    obj = IntradayMonitor(config_provider=lambda: config, manager=manager, repo=repo,
                          portfolio=service, notifier=notifier, now=now or (lambda: NOW),
                          phase=lambda *a, **kw: MarketPhase.INTRADAY, discover=lambda: [])
    # Discovery itself is tested separately; no threads/races in cycle fixtures.
    obj._scan_at = __import__('time').monotonic()
    return obj


@pytest.mark.parametrize('change,issue', [
    ({'provider_timestamp': None}, 'quote_time_unconfirmed'),
    ({'provider_timestamp': '2026-10-16T10:30:00'}, 'quote_time_unconfirmed'),
    ({'provider_timestamp': (NOW - timedelta(minutes=2)).isoformat()}, 'quote_time_invalid'),
    ({'provider_timestamp': (NOW + timedelta(minutes=1)).isoformat()}, 'quote_time_invalid'),
    ({'fetched_at': (NOW - timedelta(minutes=3)).isoformat()}, 'fetch_time_invalid'),
    ({'price': float('nan')}, 'close_invalid'),
    ({'amount': 2_050_000}, 'amount_volume_price_mismatch'),
    ({'volume': 0}, 'volume_not_positive'),
    ({'change_pct': 20}, 'change_pct_inconsistent'),
])
def test_invalid_quotes_fail_closed(change, issue):
    assert issue in quote_issues(quote(**change), NOW)


def test_completed_bars_only_and_no_invented_volume():
    frame = pd.concat([history(), pd.DataFrame([{'date': NOW.replace(tzinfo=None),
                                               'close': 1000, 'high': 1001, 'volume': 1}])])
    levels = history_levels(frame, NOW.date())
    assert levels['breakout'] == 10
    assert levels['history_date'] == '2026-10-15'
    assert evaluate_signal(quote(), levels)['signal'] == 'entry_candidate'
    missing = evaluate_signal(quote(volume_ratio=None), levels)
    assert missing['signal'] == 'watch'
    assert '数据不足' in missing['reason']
    risk = evaluate_signal(quote(price=9), levels)
    assert risk['signal'] == 'exit_risk'
    assert risk['stop_loss'] is None and risk['target_price'] is None
    assert evaluate_signal(quote(name=''), levels)['signal'] == 'watch'
    assert evaluate_signal(quote(name='*ST测试'), levels)['signal'] == 'watch'


@pytest.mark.parametrize('mutate', ['short', 'duplicate', 'nan', 'old'])
def test_invalid_history_blocks_signals(mutate):
    frame = history()
    if mutate == 'short':
        frame = frame.tail(19)
    elif mutate == 'duplicate':
        frame.loc[0, 'date'] = frame.loc[1, 'date']
    elif mutate == 'nan':
        frame.loc[29, 'close'] = float('nan')
    else:
        frame['date'] -= pd.Timedelta(days=30)
    with pytest.raises(ValueError):
        history_levels(frame, date(2026, 10, 16))


def test_six_stocks_no_loss_and_durable_dedup(repo):
    codes = ['002273', '600111', '600487', '002624', '601398', '159206']
    obj = worker(repo, codes, notify=True)
    assert obj.run_once()['checked'] == 6
    obj.manager.prefetch_realtime_quotes.assert_called_with(codes, force_refresh=True)
    obj.run_once()
    # ETF may be held/monitored but cannot trigger this main-board entry strategy.
    assert len(repo.recent()) == 5
    assert obj.notifier.send_with_results.call_count == 5
    restarted = worker(repo, codes, notify=True)
    restarted.run_once()
    restarted.notifier.send_with_results.assert_not_called()


def test_failed_push_not_successful_and_retry_is_bounded(repo):
    clock = [NOW]
    obj = worker(repo, notify=True, now=lambda: clock[0])
    obj.manager.get_realtime_quote.side_effect = lambda *a, **kw: SimpleNamespace(to_dict=lambda: quote(
        provider_timestamp=clock[0].isoformat(), fetched_at=clock[0].isoformat()))
    obj.notifier.send_with_results.return_value.channel_results[0].success = False
    obj.notifier.send_with_results.return_value.channel_results[0].error_code = 'pushplus_identity_required'
    obj.run_once()
    assert repo.recent()[0]['status'] == 'failed'
    for _ in range(4):
        clock[0] += timedelta(seconds=121)
        obj.run_once()
    assert obj.notifier.send_with_results.call_count == 3
    assert repo.recent()[0]['attempts'] == 3


def test_bad_quote_never_rearms_and_paper_not_replayed(repo):
    obj = worker(repo)
    obj.run_once()
    assert repo.recent()[0]['status'] == 'paper'
    obj.config_provider().intraday_monitor_notify = True
    obj.manager.get_realtime_quote.return_value = None
    obj.manager.get_realtime_quote.side_effect = None
    assert obj.run_once()['checked'] == 0
    obj.manager.get_realtime_quote.side_effect = lambda *a, **kw: SimpleNamespace(to_dict=quote)
    obj.run_once()
    obj.notifier.send_with_results.assert_not_called()
    assert len(repo.recent()) == 1


@pytest.mark.parametrize('phase', [MarketPhase.LUNCH_BREAK, MarketPhase.POSTMARKET,
                                  MarketPhase.NON_TRADING, MarketPhase.UNKNOWN])
def test_no_requests_outside_verified_session(repo, phase):
    obj = worker(repo)
    obj.phase = lambda *a, **kw: phase
    assert obj.run_once()['status'] == 'outside_session'
    obj.manager.prefetch_realtime_quotes.assert_not_called()


def test_quote_request_crossing_session_blocks(repo):
    obj = worker(repo)
    phases = iter([MarketPhase.INTRADAY, MarketPhase.LUNCH_BREAK])
    obj.phase = lambda *a, **kw: next(phases)
    assert 'session_changed_during_fetch' in obj.run_once()['blocked']['600111']
    assert not repo.recent()


def test_snapshot_not_replayed_or_committed_and_accounts_separate(tmp_path):
    path = tmp_path / 'private.json'
    path.write_text(json.dumps({'schema_version': 1, 'positions': [
        {'code': '600111', 'quantity': 200, 'account': 'A', 'avg_cost': 10},
        {'code': '600111', 'quantity': 100, 'account': 'B', 'avg_cost': 11},
        {'code': '015506', 'quantity': None, 'asset_type': 'off_exchange_fund'},
    ], 'executions': [{'side': 'buy', 'quantity': 200}],
        'cancelled_orders': [{'filled_quantity': 0}]}), encoding='utf-8')
    service = MagicMock()
    service.get_portfolio_snapshot.return_value = {'accounts': [], 'account_count': 0}
    context = portfolio_context(service, str(path))
    assert len(context['positions']) == 2
    assert sum(row['quantity'] for row in context['positions']) == 300
    assert context['status'] == 'snapshot_unreconciled'
    service.get_portfolio_snapshot.assert_called_with(include_realtime=False, read_only=True)
    service.record_trade.assert_not_called()


def test_real_ledger_is_not_added_to_screenshot(tmp_path):
    path = tmp_path / 'private.json'
    path.write_text(json.dumps({'schema_version': 1, 'positions': [
        {'code': '600111', 'quantity': 300}]}))
    service = MagicMock()
    service.get_portfolio_snapshot.return_value = {'account_count': 1, 'accounts': [
        {'account_name': 'A', 'positions': [{'symbol': '600111', 'market': 'cn', 'quantity': 300}]}]}
    context = portfolio_context(service, str(path))
    assert sum(row['quantity'] for row in context['positions']) == 300
    assert context['status'] == 'reconciliation_required'


def test_scanning_reuses_existing_engine_without_llm_or_stale_fallback():
    with patch('src.services.screening.pipeline.screen') as screen:
        screen.return_value = SimpleNamespace(picks=[SimpleNamespace(code='600111')], degradation=[])
        assert discover_candidates() == ['600111']
        kwargs = screen.call_args.kwargs
        assert kwargs['use_llm'] is False and kwargs['post_analyzers'] == []
        assert kwargs['config'].fallback_snapshot_path is None
        assert 'tushare' not in kwargs['config'].snapshot_source_priority


def test_monitor_only_runtime_never_enables_daily_llm():
    config = SimpleNamespace(intraday_monitor_enabled=True, schedule_enabled=False,
                             schedule_times=['18:00'], schedule_time='18:00')
    runtime = RuntimeSchedulerService(config_provider=lambda: config, owns_schedule=True,
                                      background_tasks_provider=lambda c: [])
    with patch('src.services.runtime_scheduler.Scheduler') as scheduler, \
            patch('src.services.runtime_scheduler.threading.Thread'):
        runtime.start(run_immediately=True)
        scheduler.return_value.set_daily_task.assert_not_called()
        runtime.stop()


def test_alert_has_one_reference_and_no_fabricated_stop(repo):
    obj = worker(repo)
    obj.run_once()
    content = render_alert(repo.recent()[0])
    assert '实际止损触发价：未配置' in content
    assert '目标价：数据不足' in content
    assert '交易所报价时间' in content and '抓取时间' in content


def test_stale_primary_falls_through_without_supplementing_stale_fields():
    from data_provider.base import DataFetcherManager
    from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote

    old = quote(provider_timestamp=(NOW - timedelta(minutes=10)).isoformat())
    old.pop('source')
    fresh = quote()
    fresh.pop('source')
    primary = UnifiedRealtimeQuote(**old, source=RealtimeSource.EFINANCE)
    fallback = UnifiedRealtimeQuote(**fresh, source=RealtimeSource.TENCENT)
    first = SimpleNamespace(name='EfinanceFetcher', priority=0, get_realtime_quote=lambda *a, **k: primary)
    second = SimpleNamespace(name='AkshareFetcher', priority=1, get_realtime_quote=lambda *a, **k: fallback)
    config = SimpleNamespace(enable_realtime_quote=True, realtime_source_priority='efinance,tencent')
    with patch('src.config.get_config', return_value=config):
        manager = DataFetcherManager(fetchers=[first, second])
        result = manager.get_realtime_quote('600111', quote_acceptor=lambda q: not quote_issues(q.to_dict(), NOW))
    assert result is fallback
    assert result.fallback_from == 'efinance'


def test_small_tickflow_batch_forced_refresh_preserves_default():
    from data_provider.base import DataFetcherManager

    fetcher = SimpleNamespace(name='TickFlowFetcher', priority=0, get_realtime_quote=MagicMock(),
                              prefetch_realtime_quotes=MagicMock(return_value=1))
    config = SimpleNamespace(enable_realtime_quote=True, realtime_source_priority='tickflow,tencent')
    with patch('src.config.get_config', return_value=config):
        manager = DataFetcherManager(fetchers=[fetcher])
        assert manager.prefetch_realtime_quotes(['600111']) == 0
        assert manager.prefetch_realtime_quotes(['600111'], force_refresh=True) == 1
    fetcher.prefetch_realtime_quotes.assert_called_once()


def test_history_delay_rechecks_quote_age(repo):
    times = iter([NOW, NOW, NOW + timedelta(minutes=3)])
    obj = worker(repo, now=lambda: next(times))
    # The mock manager does not invoke its acceptor; the service still gates twice.
    assert 'quote_time_invalid' in obj.run_once()['blocked']['600111']
    assert repo.recent() == []


def test_recovery_rearms_once_and_expires_failed_signal(repo):
    payload = {'signal': 'entry_candidate', 'code': '600111'}
    repo.observe('test', payload, notify=True)
    event_id = repo.claim('test', 'entry_candidate', NOW.timestamp())
    repo.finish(event_id, False, 'test_rejection')
    repo.observe('test', {**payload, 'signal': 'watch'}, notify=True)
    assert repo.recent()[0]['status'] == 'expired'
    repo.observe('test', payload, notify=True)
    assert len(repo.recent()) == 2
    assert repo.claim('test', 'entry_candidate', NOW.timestamp()) is not None


def test_monitor_status_distinguishes_configured_from_running():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.deps import get_runtime_scheduler_service
    from api.v1.endpoints.monitor import router

    app = FastAPI()
    app.include_router(router, prefix='/monitor')
    runtime = MagicMock()
    runtime.is_background_task_active.return_value = False
    app.dependency_overrides[get_runtime_scheduler_service] = lambda: runtime
    config = SimpleNamespace(intraday_monitor_enabled=True, intraday_monitor_notify=False,
                             intraday_monitor_interval_seconds=60)
    with patch('api.v1.endpoints.monitor.get_config', return_value=config), TestClient(app) as client:
        result = client.get('/monitor/status').json()
        assert result['enabled'] is True and result['active'] is False
        assert client.get('/monitor/observations?limit=5001').status_code == 422


def test_prefixed_actual_holdings_are_monitored_without_index_collision(repo):
    portfolio = MagicMock()
    portfolio.get_portfolio_snapshot.return_value = {'account_count': 1, 'accounts': [
        {'account_name': 'A', 'positions': [{'symbol': 'SH600111', 'market': 'cn', 'quantity': 100}]}]}
    obj = worker(repo, ['sh000001', '600111.SH'], portfolio=portfolio)
    result = obj.run_once()
    assert result['checked'] == 1
    obj.manager.prefetch_realtime_quotes.assert_called_once_with(['600111'], force_refresh=True)
    assert result['signals'][0]['position']['quantity'] == 100


def test_partial_acceptance_preserves_pushplus_failure(repo):
    obj = worker(repo, notify=True)
    obj.notifier.send_with_results.return_value.channel_results = [
        SimpleNamespace(channel='pushplus', success=False, error_code='identity_required'),
        SimpleNamespace(channel='email', success=True, error_code=None),
    ]
    obj.run_once()
    event = repo.recent()[0]
    assert event['status'] == 'accepted'
    assert event['error'] == 'pushplus:identity_required'
