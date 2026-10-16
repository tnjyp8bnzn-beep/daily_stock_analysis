"""Regression coverage for batching, provenance, report consistency and PushPlus."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from data_provider.base import DataFetcherManager
from data_provider.realtime_types import UnifiedRealtimeQuote
from data_provider.tickflow_fetcher import TickFlowFetcher
from src.analyzer import AnalysisResult, GeminiAnalyzer
from src.config import Config
from src.notification import NotificationService
from src.notification_sender.pushplus_sender import PushplusSender
from src.quote_provenance import quote_metadata, provenance_lines
from src.report_consistency import apply_report_consistency
from src.services.report_renderer import render
from src.schemas.decision_action import display_action_fields_for_result
from src.services.decision_signal_extractor import resolve_decision_signal_action_fields


@pytest.mark.parametrize('count,batch_size,expected', [(6, 100, [5, 1]), (11, 99, [5, 5, 1]), (6, 2, [2, 2, 2])])
def test_tickflow_batches_without_losing_symbols(count, batch_size, expected):
    fetcher = TickFlowFetcher('test-key', batch_size=batch_size)
    resource = Mock()

    def get(*, symbols):
        assert len(symbols) <= 5
        return [{'symbol': symbol, 'last_price': 10} for symbol in symbols]

    resource.get.side_effect = get
    fetcher._client = SimpleNamespace(quotes=resource, close=lambda: None)
    codes = [f'600{i:03d}' for i in range(count)]
    assert fetcher.prefetch_realtime_quotes(codes + [codes[0]]) == count
    assert [len(call.kwargs['symbols']) for call in resource.get.call_args_list] == expected
    assert [fetcher.get_realtime_quote(code).code for code in codes] == codes
    assert resource.get.call_count == len(expected)


def test_failed_batch_does_not_skip_later_batch_and_single_stock_fallback_recovers():
    fetcher = TickFlowFetcher('test-key')
    resource = Mock()

    def get(*, symbols):
        if len(symbols) == 5:
            raise TimeoutError('temporary batch failure')
        return [{'symbol': symbol, 'last_price': 10} for symbol in symbols]

    resource.get.side_effect = get
    fetcher._client = SimpleNamespace(quotes=resource, close=lambda: None)
    codes = [f'600{i:03d}' for i in range(6)]
    assert fetcher.prefetch_realtime_quotes(codes) == 1
    assert all(fetcher.get_realtime_quote(code) is not None for code in codes)


def test_cache_retains_acquisition_time_and_unknown_provider_time():
    fetcher = TickFlowFetcher('test-key')
    fetcher._store_quotes([{'symbol': '600519.SH', 'last_price': 10}])
    quote = fetcher.get_realtime_quote('600519')
    fetched_at = quote.fetched_at
    manager = DataFetcherManager.__new__(DataFetcherManager)
    manager._enrich_realtime_quote(quote)
    assert quote.fetched_at == fetched_at
    assert quote.provider_timestamp is None
    assert quote.stale_seconds is None
    assert fetcher.get_realtime_quote('600519').fetched_at == fetched_at


@pytest.mark.parametrize('timestamp', ['2026-10-16T10:00:00', '2026-10-16', 'bad', None])
def test_unconfirmed_timezone_is_never_invented(timestamp):
    manager = DataFetcherManager.__new__(DataFetcherManager)
    quote = UnifiedRealtimeQuote('600519', price=10, provider_timestamp=timestamp)
    manager._enrich_realtime_quote(quote)
    assert quote.provider_timestamp is None
    assert quote.stale_seconds is None


def _context():
    return {
        'code': '600519', 'date': '2026-10-16', 'yesterday': {'close': 100},
        'today': {'date': '2026-10-16', 'close': 105, 'open': 105, 'high': 106, 'low': 104,
                  'volume': 1000000, 'amount': 105000000, 'pct_chg': 5, 'ma5': 101, 'ma10': 99, 'ma20': 98},
        'daily_snapshot': {'date': '2026-10-15', 'close': 100, 'open': 100,
                           'high': 101, 'low': 99, 'volume': 1000000, 'amount': 100000000,
                           'pct_chg': 0, 'data_source': 'daily-provider'},
        'realtime': {'price': 105, 'open_price': 105, 'high': 106, 'low': 104,
                     'volume': 1000000, 'amount': 105000000, 'change_pct': 5, 'source': 'tickflow',
                     'provider_timestamp': '2026-10-16T10:00:00+08:00',
                     'fetched_at': '2026-10-16T02:00:07+00:00'},
        'market_phase_context': {'phase': 'intraday', 'session_date': '2026-10-16', 'is_partial_bar': True},
    }


def _result(**changes):
    values = dict(code='600519', name='测试股票', sentiment_score=50, trend_prediction='震荡',
                  operation_advice='观望', decision_type='hold', buy_reason='均线尚未确认',
                  dashboard={'core_conclusion': {'one_sentence': '观望'},
                             'battle_plan': {'position_strategy': {'entry_plan': '分批建仓'}}})
    values.update(changes)
    return AnalysisResult(**values)


def test_snapshot_keeps_daily_close_and_quote_separate_and_both_renderers_show_times():
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    snapshot = analyzer._build_market_snapshot(_context())
    assert snapshot['close'] == '100.00'
    assert snapshot['price'] == '105.00'
    assert snapshot['daily_is_final'] is True
    assert snapshot['stale_seconds'] == 7
    result = _result(market_snapshot=snapshot)
    with patch('src.notification.get_config', return_value=Config(stock_list=[])):
        service = NotificationService()
    lines = []
    service._append_market_snapshot(lines, result)
    outputs = ['\n'.join(lines), render('markdown', [result])]
    for output in outputs:
        assert 'daily-provider' in output
        assert '2026-10-16T02:00:00+00:00' in output
        assert '2026-10-16T02:00:07+00:00' in output
        assert '7s' in output
        assert '无法确认' in output  # daily quote timestamp was never supplied


def test_intraday_daily_bar_and_latest_quote_are_not_confirmed_close():
    context = _context()
    context.pop('daily_snapshot')
    assert quote_metadata(context)['daily_is_final'] is False
    context['market_phase_context']['phase'] = 'postmarket'
    assert quote_metadata(context)['daily_is_final'] is True
    context['today']['is_estimated'] = True
    assert quote_metadata(context)['daily_is_final'] is False
    assert quote_metadata(context)['price_type'] == 'latest_quote'


def test_unknown_and_future_quote_time_do_not_report_zero_delay():
    context = _context()
    context['realtime']['provider_timestamp'] = 'not-a-date'
    data = quote_metadata(context)
    assert data['quote_time_status'] == 'unknown'
    assert '无法确认' in '\n'.join(provenance_lines(data))
    context['realtime']['provider_timestamp'] = '2026-10-16T03:00:00+00:00'
    assert quote_metadata(context)['stale_seconds'] is None


def test_watch_conclusion_removes_entry_position_and_sniper_instructions():
    result = _result()
    result.dashboard['battle_plan']['sniper_points'] = {'ideal_buy': 100}
    assert 'action_conflict' in apply_report_consistency(result, _context())
    assert result.operation_advice == '观望'
    assert result.action == 'watch' and result.decision_type == 'hold'
    assert '建仓' not in str(result.dashboard['battle_plan'])
    points = result.dashboard['battle_plan']['sniper_points']
    assert points['ideal_buy'] is None and points['secondary_buy'] is None
    assert points['stop_loss'] is None
    assert result.dashboard['price_validation']['stop_loss']['reason']
    assert '观望' in result.dashboard['core_conclusion']['one_sentence']


def test_missing_basis_can_never_produce_buy_or_sell():
    for action, advice in [('buy', '买入'), ('sell', '卖出')]:
        result = _result(operation_advice=advice, decision_type=action, dashboard={})
        apply_report_consistency(result, {'data_missing': True})
        assert result.action == 'watch' and result.decision_type == 'hold'
        for reason in ('N/A', '数据缺失', '数据不足，无法判断'):
            result = _result(operation_advice=advice, decision_type=action, buy_reason=reason, dashboard={})
            apply_report_consistency(result, _context())
            assert result.action == 'watch'


def test_grounded_consistent_technical_buy_is_preserved():
    result = _result(operation_advice='买入', decision_type='buy', dashboard={
        'core_conclusion': {'one_sentence': '买入'},
        'battle_plan': {'position_strategy': {'entry_plan': '分批建仓'}},
    })
    apply_report_consistency(result, _context())
    assert result.action == 'buy' and result.decision_type == 'buy'
    assert result.dashboard['battle_plan']['position_strategy']['entry_plan'] == '分批建仓'


def test_placeholder_or_invented_citation_cannot_back_factual_claims():
    result = _result(news_summary='今日重大利好 https://invented.invalid 2026-10-16',
                     market_sentiment='市场情绪乐观', volume_analysis='主力净流入一亿元')
    result.dashboard['intelligence'] = {'latest_news': '机构一致上调评级'}
    apply_report_consistency(result, _context(), '【情报搜索结果】未找到相关信息')
    for text in (result.news_summary, result.market_sentiment, result.volume_analysis,
                 result.dashboard['intelligence']['latest_news']):
        assert '数据缺失' in text
    assert 'invented.invalid' not in str(result.to_dict())
    assert '资金流向 / 机构观点: 数据缺失' in result.risk_warning


def test_only_factual_extracts_from_actual_dated_linked_evidence_are_retained():
    evidence = '2026-10-16 公司公告：订单金额10亿元 https://example.com/notice'
    result = _result(news_summary=evidence)
    apply_report_consistency(result, _context(), evidence)
    assert result.news_summary == evidence
    result = _result(news_summary='2026-10-16 公司公告：订单金额100亿元 https://example.com/notice')
    apply_report_consistency(result, _context(), evidence)
    assert '数据缺失' in result.news_summary


def test_pushplus_real_name_failure_is_false_and_explicit_without_token(caplog):
    sender = PushplusSender(Config(stock_list=[], pushplus_token='secret-test-token'))
    response = Mock(status_code=200)
    response.json.return_value = {'code': 403, 'msg': '账户未实名认证 secret-test-token'}
    with patch('src.notification_sender.pushplus_sender.requests.post', return_value=response):
        assert sender.send_to_pushplus('report') is False
    assert '实名认证' in caplog.text and 'code=403' in caplog.text
    assert 'secret-test-token' not in caplog.text
    assert '发送成功' not in caplog.text


def test_pushplus_business_failure_propagates_to_dispatch_status():
    with patch('src.notification.get_config', return_value=Config(stock_list=[], pushplus_token='test-token')):
        service = NotificationService()
    response = Mock(status_code=200)
    response.json.return_value = {'code': 403, 'msg': '账户未实名认证'}
    with patch('src.notification_sender.pushplus_sender.requests.post', return_value=response):
        outcome = service.send_with_results('report')
    assert outcome.success is False
    assert outcome.status == 'all_failed'
    assert not any(item.success for item in outcome.channel_results)


def test_high_model_score_never_overrides_final_watch_in_report_or_signal():
    result = _result(sentiment_score=95, operation_advice='买入', decision_type='buy', dashboard={})
    apply_report_consistency(result, {'data_missing': True})
    assert display_action_fields_for_result(result)['action'] == 'watch'
    assert resolve_decision_signal_action_fields(result, report_type='simple')['action'] == 'watch'
    output = render('markdown', [result])
    assert '观望' in output


def test_final_pipeline_check_is_applied_before_persistence():
    from tests.test_pipeline_market_phase_context import _make_pipeline, _phase_payload
    from src.enums import ReportType

    pipeline = _make_pipeline()
    pipeline.analyzer.analyze.return_value = _result(sentiment_score=95)
    phase = SimpleNamespace(to_dict=lambda: _phase_payload())
    with patch('src.core.pipeline.build_market_phase_context', return_value=phase):
        result = pipeline.analyze_stock('600519', ReportType.SIMPLE, query_id='regression')
    assert result is not None and result.action == 'watch'
    saved = pipeline.db.save_analysis_history.call_args.kwargs['result']
    assert saved.action == 'watch' and saved.decision_type == 'hold'
    assert '建仓' not in str(saved.dashboard['battle_plan'])


def test_agent_final_check_precedes_history_save_and_uses_quote_provenance():
    from tests.test_pipeline_market_phase_context import _make_pipeline, _phase_payload
    from src.agent.executor import AgentResult
    from src.enums import ReportType

    pipeline = _make_pipeline(agent_mode=True)
    pipeline._ensure_agent_history = Mock()
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    pipeline.analyzer._build_market_snapshot.side_effect = analyzer._build_market_snapshot
    executor = Mock()
    executor.run.return_value = AgentResult(
        success=True, content='{}', provider='test',
        dashboard={'stock_name': '测试股票', 'sentiment_score': 95, 'operation_advice': '买入',
                   'decision_type': 'buy', 'analysis_summary': '观望',
                   'dashboard': {'core_conclusion': {'one_sentence': '观望'},
                                 'battle_plan': {'position_strategy': {'entry_plan': '立即建仓'}}}},
    )
    with patch('src.agent.factory.build_agent_executor', return_value=executor):
        result = pipeline._analyze_with_agent(
            code='600519', report_type=ReportType.SIMPLE, query_id='agent-regression', stock_name='测试股票',
            realtime_quote=UnifiedRealtimeQuote('600519', price=105), chip_data=None,
            fundamental_context={}, trend_result=None, market_phase_context=_phase_payload(),
            market_phase_summary=_phase_payload(),
        )
    assert result is not None and result.action == 'watch'
    assert result.market_snapshot['quote_time_status'] == 'unknown'
    assert pipeline.db.save_analysis_history.call_args.kwargs['result'].action == 'watch'


def test_pushplus_any_chunk_failure_fails_whole_message():
    sender = PushplusSender(Config(stock_list=[], pushplus_token='test-token'))
    ok, failed = Mock(status_code=200), Mock(status_code=200)
    ok.json.return_value = {'code': 200}
    failed.json.return_value = {'code': 403, 'msg': '未实名认证'}
    with patch('src.notification_sender.pushplus_sender.requests.post', side_effect=[ok, failed]), \
         patch('src.notification_sender.pushplus_sender.time.sleep'):
        assert sender.send_to_pushplus('A' * 25000) is False


def test_market_review_prose_cannot_bypass_stock_evidence_rules():
    from src.report_consistency import sanitize_factual_prose

    text = '### 指数结构\n指数站上均线。\n### 最新新闻\n传言公司将并购。\n### 资金与情绪\n资金持续流入。\n### 风险计划\n机构一致上调评级。'
    cleaned = sanitize_factual_prose(text, '')
    assert '指数站上均线' in cleaned
    for unsupported in ('传言公司将并购', '资金持续流入', '机构一致上调评级'):
        assert unsupported not in cleaned
    assert '数据缺失' in cleaned


def test_watch_preserves_protective_stop_and_dashboard_metadata():
    result = _result()
    result.dashboard['core_conclusion']['time_sensitivity'] = '当日有效'
    result.dashboard['intelligence'] = {'risk_alerts': []}
    result.dashboard['battle_plan'] = {
        'sniper_points': {'ideal_buy': 105, 'secondary_buy': 103, 'stop_loss': 95},
        'position_strategy': {'entry_plan': '分批买入', 'risk_control': '跌破95止损卖出'},
        'action_checklist': ['跌破95止损卖出', '放量买入'],
        'risk_budget': '1%',
    }
    apply_report_consistency(result, _context())
    assert result.action == 'watch' and result.decision_type == 'hold'
    assert result.dashboard['decision_type'] == result.decision_type
    assert result.dashboard['sentiment_score'] == result.sentiment_score
    assert result.dashboard['core_conclusion']['time_sensitivity'] == '当日有效'
    battle = result.dashboard['battle_plan']
    assert battle['sniper_points'] == {'stop_loss': 95, 'ideal_buy': None, 'secondary_buy': None, 'take_profit': None, 'warning_price': None}
    assert result.dashboard['price_validation']['stop_loss']['status'] == 'supported_plan'
    assert battle['position_strategy']['risk_control'] == '跌破95止损卖出'
    assert '跌破95止损卖出' in battle['action_checklist']
    assert '放量买入' not in battle['action_checklist']
    assert battle['risk_budget'] == '1%'
    from src.analyzer import check_content_integrity
    assert check_content_integrity(result) == (True, [])


def test_sparse_legacy_result_without_decision_type_fails_closed_without_crashing():
    result = SimpleNamespace(success=True, sentiment_score=90, operation_advice='持有', analysis_summary='技术待确认')
    apply_report_consistency(result, _context())
    assert result.action == 'watch' and result.decision_type == 'hold'
    assert result.dashboard['decision_type'] == 'hold'
    assert result.dashboard['battle_plan']['sniper_points']['stop_loss'] is None
    assert result.dashboard['price_validation']['stop_loss']['reason']


@pytest.mark.parametrize('has_history', [True, False])
def test_history_prefetch_uses_real_tuple_contract_and_preserves_empty_fallback(has_history, caplog):
    from datetime import date
    import pandas as pd
    from src.core.pipeline import StockAnalysisPipeline

    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.db = Mock()
    pipeline.db.get_data_range.return_value = []
    pipeline.fetcher_manager = Mock()
    frame = pd.DataFrame([{'date': '2026-10-15', 'close': 100}]) if has_history else pd.DataFrame()
    pipeline.fetcher_manager.get_daily_data.return_value = (frame, 'test-history')
    pipeline._resolve_resume_target_date = Mock(return_value=date(2026, 10, 15))
    with patch('src.services.history_loader.get_frozen_target_date', return_value=None):
        pipeline._ensure_agent_history('600519')
    pipeline.fetcher_manager.get_daily_data.assert_called_once_with('600519', days=240)
    if has_history:
        pipeline.db.save_daily_data.assert_called_once_with(frame, '600519', 'test-history')
    else:
        pipeline.db.save_daily_data.assert_not_called()
    assert 'not enough values to unpack' not in caplog.text
