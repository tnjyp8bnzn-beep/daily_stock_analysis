"""Run #2 evidence and deterministic market-data regression checks (no API calls)."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from data_provider.base import DataFetcherManager
from data_provider.efinance_fetcher import EfinanceFetcher
from data_provider.akshare_fetcher import AkshareFetcher, _normalize_tencent_volume
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote, safe_float, safe_int
from data_provider.tickflow_fetcher import TickFlowFetcher
from src.analyzer import AnalysisResult, GeminiAnalyzer
from src.core.pipeline import StockAnalysisPipeline
from src.market_data_quality import context_quality, validate_bar, validate_technical
from src.notification import NotificationService
from src.report_consistency import apply_report_consistency
from src.services.report_renderer import render


def _bar():
    return dict(date='2026-10-16', open=46.50, high=47.00, low=45.20, close=45.68,
                volume=102307400, amount=4717003000, pct_chg=.40,
                ma5=47.81, ma10=51.731, ma20=59.681, data_source='EfinanceFetcher')


def _quote(**overrides):
    values = dict(code='600487', source=RealtimeSource.TICKFLOW, price=45.68,
                  open_price=46.50, high=47.00, low=45.20, pre_close=45.50,
                  change_pct=.40, volume=102307400, amount=4717003000,
                  provider_timestamp='2026-10-16T15:00:00+08:00',
                  fetched_at='2026-10-16T15:12:36+08:00')
    values.update(overrides)
    return UnifiedRealtimeQuote(**values)


def _context():
    return {'code': '600487', 'date': '2026-10-16', 'today': _bar(), 'daily_snapshot': _bar(),
            'yesterday': {'close': 45.50, 'volume': 112131400}, 'realtime': _quote().to_dict(),
            'market_phase_context': {'phase': 'postmarket', 'session_date': '2026-10-16'}}


def _result(stop):
    return AnalysisResult(code='600487', name='亨通光电', sentiment_score=50,
                          trend_prediction='看空', operation_advice='观望', decision_type='hold',
                          buy_reason='等待均线转强', dashboard={'battle_plan': {
                              'sniper_points': {'stop_loss': stop},
                              'position_strategy': {'risk_control': '严格执行止损，防止损失扩大。'}}})


@pytest.mark.parametrize('code', ['600487', '159206', '688111', '920748'])
def test_efinance_daily_converts_lots_once_and_preserves_yuan(code):
    raw = pd.DataFrame([{'日期': '2026-10-16', '开盘': 46.50, '收盘': 45.68,
                         '最高': 47, '最低': 45.20, '成交量': 1023074,
                         '成交额': 4717003000, '涨跌幅': .40}])
    normalized = EfinanceFetcher()._normalize_data(raw, code)
    row = normalized.iloc[0]
    assert row.volume == 102307400
    assert row.amount == 4717003000
    assert 45.20 < row.amount / row.volume < 47
    assert raw.iloc[0]['成交量'] == 1023074


def test_efinance_missing_values_are_not_fabricated_as_zero_or_flat_ohlc():
    row = EfinanceFetcher()._normalize_data(pd.DataFrame([{'日期': '2026-10-16', '收盘': 45.68}]), '600487')
    check = validate_bar(row.iloc[0].to_dict())
    assert {'open', 'high', 'low', 'volume', 'amount'}.issubset(check['missing_fields'])


@pytest.mark.parametrize('code', ['600487', '159206'])
def test_efinance_spot_stock_and_etf_use_shares(code):
    frame = pd.DataFrame([{'股票代码': code, '股票名称': '测试', '最新价': 45.68,
                           '涨跌幅': .40, '成交量': 1023074, '成交额': 4717003000}])
    fetcher = EfinanceFetcher()
    ef = SimpleNamespace(stock=SimpleNamespace(get_realtime_quotes=Mock(return_value=frame)))
    empty_cache = {'data': None, 'timestamp': 0, 'ttl': 600}
    with patch.dict('sys.modules', {'efinance': ef}), patch.object(fetcher, '_set_random_user_agent'), \
            patch.object(fetcher, '_enforce_rate_limit'), \
            patch('data_provider.efinance_fetcher._realtime_cache', empty_cache.copy()), \
            patch('data_provider.efinance_fetcher._etf_realtime_cache', empty_cache.copy()), \
            patch('data_provider.efinance_fetcher.get_realtime_circuit_breaker'):
        quote = fetcher.get_realtime_quote(code)
    assert quote.volume == 102307400 and quote.amount == 4717003000


def test_tickflow_observed_cn_lots_contract_matches_efinance_after_normalization():
    fetcher = TickFlowFetcher('test-key')
    quote = fetcher._quote_to_unified_quote('600487', {
        'last_price': 45.68, 'prev_close': 45.50, 'open': 46.50, 'high': 47, 'low': 45.20,
        'volume': 1023074, 'amount': 4717003000})
    assert quote.volume == 102307400 and quote.amount == 4717003000
    assert validate_bar(quote.to_dict(), quote=True)['status'] == 'ok'


@pytest.mark.parametrize('raw_volume', [1023074, 102307400])
def test_tencent_resolves_observed_lots_or_shares_using_same_bar(raw_volume):
    fields = [''] * 50
    fields[3], fields[6], fields[33], fields[34] = '45.68', str(raw_volume), '47.00', '45.20'
    fields[35] = '45.68/1023074/4717003000'
    assert _normalize_tencent_volume(fields) == 102307400


def test_tencent_provider_timestamp_is_parsed_without_using_fetch_time():
    fields = [''] * 50
    for index, value in {1: '亨通光电', 2: '600487', 3: '45.68', 4: '45.50', 5: '46.50',
                         6: '1023074', 30: '20261016150000', 31: '.18', 32: '.40',
                         33: '47', 34: '45.20', 35: '45.68/1023074/4717003000'}.items():
        fields[index] = value
    response = Mock(status_code=200, text='v_sh600487="' + '~'.join(fields) + '";')
    fetcher = AkshareFetcher()
    with patch('data_provider.akshare_fetcher.requests.get', return_value=response), \
            patch('data_provider.akshare_fetcher.get_realtime_circuit_breaker'):
        quote = fetcher._get_stock_realtime_quote_tencent('600487')
    assert quote.provider_timestamp == '2026-10-16T15:00:00+08:00'
    assert quote.volume == 102307400


@pytest.mark.parametrize('timestamp,fetched,stale,status', [
    ('2026-10-16T15:00:00+08:00', '2026-10-16T15:12:36+08:00', False, 'session_close'),
    ('2026-10-16T15:00:00+08:00', '2026-10-17T12:00:00+08:00', False, 'session_close'),
    ('2026-10-16T15:00:00+08:00', '2026-10-19T09:20:00+08:00', False, 'session_close'),
    ('2026-10-16T15:00:00+08:00', '2026-10-19T09:31:00+08:00', True, 'stale'),
    ('2026-10-16T14:40:00+08:00', '2026-10-16T15:12:36+08:00', True, 'stale'),
    ('2026-10-15T15:00:00+08:00', '2026-10-16T15:12:36+08:00', True, 'stale'),
    ('2026-10-16T11:30:00+08:00', '2026-10-16T12:20:00+08:00', False, 'session_break'),
    ('2026-10-16T11:00:00+08:00', '2026-10-16T12:20:00+08:00', True, 'stale'),
    ('2026-10-16T10:00:00+08:00', '2026-10-16T10:02:00+08:00', False, 'fresh'),
    ('2026-10-16T10:00:00+08:00', '2026-10-16T10:12:00+08:00', True, 'stale'),
    ('2026-10-16T15:00:01+08:00', '2026-10-16T15:00:00+08:00', None, 'unknown'),
])
def test_session_freshness_keeps_actual_age(timestamp, fetched, stale, status):
    quote = _quote(provider_timestamp=timestamp, fetched_at=fetched)
    DataFetcherManager.__new__(DataFetcherManager)._enrich_realtime_quote(quote)
    assert quote.is_stale is stale
    assert quote.freshness_status == status
    assert quote.fetched_at == fetched
    if status == 'session_close' and fetched.endswith('15:12:36+08:00'):
        assert quote.stale_seconds == 756


@pytest.mark.parametrize('changes', [{'amount': None}, {'volume': 1023074}, {'high': None}, {'pre_close': None}])
def test_incomplete_or_inconsistent_close_is_not_certified_fresh(changes):
    quote = _quote(**changes)
    DataFetcherManager.__new__(DataFetcherManager)._enrich_realtime_quote(quote)
    assert quote.is_stale is None
    assert quote.freshness_status == 'incomplete_close'
    assert quote.data_quality == 'partial'


@pytest.mark.parametrize('field,value,issue', [
    ('close', float('nan'), 'close_invalid'), ('close', float('inf'), 'close_invalid'),
    ('volume', -1, 'volume_invalid'), ('amount', -1, 'amount_invalid'),
    ('volume', 1023074, 'amount_volume_price_mismatch'),
    ('pct_chg', 40, 'change_pct_inconsistent'), ('low', 48, 'ohlc_range_inconsistent'),
])
def test_bar_validation_rejects_bad_values_without_guessing_corrections(field, value, issue):
    bar = _bar(); bar[field] = value
    check = validate_bar(bar, pre_close=45.50)
    assert issue in check['issues'] and check['status'] == 'invalid'
    assert check['invalid_fields']


@pytest.mark.parametrize('value', [float('inf'), float('-inf'), float('nan'), 'inf', 'NaN'])
def test_nonfinite_provider_numbers_are_missing(value):
    assert safe_float(value) is None and safe_int(value) is None
    assert UnifiedRealtimeQuote('600487', price=value).has_basic_data() is False


def test_short_history_does_not_manufacture_full_window_mas():
    df = pd.DataFrame({'close': [45.68] * 20, 'volume': [102307400] * 20})
    out = EfinanceFetcher()._calculate_indicators(df)
    assert pd.isna(out.iloc[3].ma5) and out.iloc[4].ma5 == 45.68
    assert pd.isna(out.iloc[8].ma10) and out.iloc[9].ma10 == 45.68
    assert pd.isna(out.iloc[18].ma20) and out.iloc[19].ma20 == 45.68
    assert validate_technical(out.iloc[3].to_dict())['status'] == 'partial'
    assert validate_technical({**_bar(), 'rsi_6': 101})['issues'] == ['rsi_6_invalid']
    assert validate_technical({**_bar(), 'macd_dif': float('inf')})['status'] == 'invalid'


def test_raw_daily_storage_and_realtime_overlay_have_same_share_unit(tmp_path):
    from src.storage import DatabaseManager
    from src.stock_analyzer import StockTrendAnalyzer

    raw = pd.DataFrame([dict(日期=day, 开盘=46.5, 收盘=45.68, 最高=47.0,
                             最低=45.2, 成交量=1023074, 成交额=4717003000, 涨跌幅=0)
                        for day in pd.bdate_range('2026-09-01', '2026-10-16')])
    raw.loc[len(raw) - 2, ['开盘', '收盘', '最高', '最低', '成交量', '成交额']] = [
        46.00, 45.50, 46.81, 45.10, 1121314, 5156201000]
    fetcher = EfinanceFetcher()
    normalized = fetcher._calculate_indicators(fetcher._clean_data(fetcher._normalize_data(raw, '600487')))
    db = object.__new__(DatabaseManager)
    db.__init__('sqlite:///' + str(tmp_path / 'isolated.sqlite'))
    try:
        legacy = normalized.copy()
        legacy['volume'] /= 100
        db.save_daily_data(legacy, '600487', 'EfinanceFetcher')
        db.save_daily_data(normalized, '600487', 'EfinanceFetcher')
        context = db.get_analysis_context('600487', target_date=date(2026, 10, 16))
        assert context['today']['volume'] == 102307400
        pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
        pipeline.config = SimpleNamespace(enable_realtime_technical_indicators=True, report_language='zh')
        pipeline.search_service = None
        pipeline.fetcher_manager = Mock()
        trend = StockTrendAnalyzer().analyze(normalized, '600487')
        with patch('src.core.pipeline.get_market_now', return_value=pd.Timestamp('2026-10-16T15:12:36+08:00')):
            enhanced = pipeline._enhance_context(context, _quote(), None, trend, fundamental_context={})
        assert enhanced['volume_change_ratio'] == .91
        assert enhanced['today']['volume'] == enhanced['daily_snapshot']['volume'] == 102307400
    finally:
        db._engine.dispose()


def test_hengtong_conflicting_stop_is_not_replaced_by_an_invented_price():
    original = '43.00（若跌破今日低点并放量，需果断离场）'
    result = _result(original)
    adjustments = apply_report_consistency(result, _context())
    assert 'stop_loss_anchor_conflict' in adjustments
    assert result.action == 'watch' and result.decision_type == 'hold'
    validation = result.dashboard['stop_loss_validation']
    assert validation['original_value'] == original and validation['anchors'] == {'low': 45.2}
    assert '数据缺失' in result.dashboard['battle_plan']['sniper_points']['stop_loss']
    assert '不可执行' in result.risk_warning
    assert '严格执行止损' in result.dashboard['battle_plan']['position_strategy']['risk_control']
    apply_report_consistency(result, _context())
    assert result.dashboard['stop_loss_validation'] == validation


@pytest.mark.parametrize('stop', ['45.20（若跌破今日低点并放量）', '47.81（跌破MA5）', 43.0])
def test_consistent_stop_or_independent_protective_level_is_preserved(stop):
    result = _result(stop)
    apply_report_consistency(result, _context())
    assert result.dashboard['battle_plan']['sniper_points']['stop_loss'] == stop
    assert 'stop_loss_validation' not in result.dashboard


def test_stop_trigger_in_risk_control_is_checked_against_separate_numeric_level():
    result = _result(43.0)
    result.dashboard['battle_plan']['position_strategy']['risk_control'] = '跌破今日最低价需止损'
    apply_report_consistency(result, _context())
    assert result.dashboard['stop_loss_validation']['anchors'] == {'low': 45.2}
    assert result.dashboard['battle_plan']['sniper_points']['stop_loss'] != 43.0
    assert '核实前不可执行' in result.dashboard['battle_plan']['position_strategy']['risk_control']


def test_dependent_invalid_stop_trigger_is_removed_but_generic_protection_is_kept():
    result = _result('43.00（跌破今日低点止损）')
    battle = result.dashboard['battle_plan']
    battle['position_strategy']['risk_control'] = '跌破43.00止损'
    battle['action_checklist'] = ['跌破43.00止损卖出', '严格控制仓位风险']
    apply_report_consistency(result, _context())
    battle = result.dashboard['battle_plan']
    assert '跌破43.00' not in str(battle)
    assert '严格控制仓位风险' in battle['action_checklist']
    assert '跌破43.00止损' in result.dashboard['stop_loss_validation']['trigger_text']


@pytest.mark.parametrize('code,timestamp,fetched', [
    ('600487', '2026-09-30T15:00:00+08:00', '2026-10-01T15:12:00+08:00'),
    ('AAPL', '2026-11-27T13:00:00-05:00', '2026-11-27T13:12:00-05:00'),
])
def test_holiday_and_shortened_us_session_use_actual_exchange_close(code, timestamp, fetched):
    quote = _quote(code=code, provider_timestamp=timestamp, fetched_at=fetched)
    DataFetcherManager.__new__(DataFetcherManager)._enrich_realtime_quote(quote)
    assert quote.is_stale is False and quote.freshness_status == 'session_close'


def test_missing_calendar_cannot_certify_a_session_close():
    quote = _quote()
    with patch.dict('sys.modules', {'exchange_calendars': None}):
        DataFetcherManager.__new__(DataFetcherManager)._enrich_realtime_quote(quote)
    assert quote.is_stale is True and quote.freshness_status == 'stale'


def test_technical_warmup_and_invalid_input_are_explicit():
    from src.stock_analyzer import StockTrendAnalyzer
    assert 'macd_warmup' in validate_technical({**_bar(), 'sample_count': 25})['missing_fields']
    df = pd.DataFrame([_bar()] * 35)
    df.loc[34, 'close'] = float('inf')
    trend = StockTrendAnalyzer().analyze(df, '600487')
    assert trend.ma5 == 0 and trend.sample_count == 35
    assert any('close' in warning for warning in trend.risk_factors)


@pytest.mark.parametrize('stop', [float('nan'), float('inf'), 'NaN', '-43.00元', True])
def test_nonfinite_or_nonpositive_stop_never_becomes_executable(stop):
    result = _result(stop)
    apply_report_consistency(result, _context())
    assert result.dashboard['stop_loss_validation']['status'] == 'invalid'
    assert '数据缺失' in result.dashboard['battle_plan']['sniper_points']['stop_loss']


def test_ma_anchor_next_to_chinese_text_is_verified():
    result = _result('43.00（跌破MA20支撑止损）')
    apply_report_consistency(result, _context())
    assert result.dashboard['stop_loss_validation']['anchors'] == {'ma20': 59.681}


def test_price_only_history_remains_usable_without_fabricated_turnover():
    fetcher = EfinanceFetcher()
    raw = pd.DataFrame({'日期': pd.bdate_range('2026-09-01', periods=40), '收盘': [45.68] * 40})
    frame = fetcher._calculate_indicators(fetcher._clean_data(fetcher._normalize_data(raw, '159206')))
    assert len(frame) == 40 and frame.iloc[-1]['close'] == 45.68
    assert frame['volume'].isna().all() and frame['amount'].isna().all()
    assert frame['volume_ratio'].isna().all()
    assert validate_bar(frame.iloc[-1].to_dict())['status'] != 'ok'


def test_missing_previous_close_does_not_claim_complete_change_validation():
    check = validate_bar(_bar())
    assert check['status'] == 'partial' and 'pre_close' in check['missing_fields']


def test_bad_data_blocks_actions_and_is_visible_in_both_renderers():
    context = _context(); context['daily_snapshot']['volume'] = 1023074
    result = _result(43.0)
    result.operation_advice, result.decision_type, result.action = '买入', 'buy', 'buy'
    result.dashboard = {}
    apply_report_consistency(result, context)
    assert result.action == 'watch'
    assert context_quality(context)['issues'] == ['daily_bars:amount_volume_price_mismatch']
    result.market_snapshot = GeminiAnalyzer.__new__(GeminiAnalyzer)._build_market_snapshot(context)
    service = NotificationService.__new__(NotificationService)
    service.config = SimpleNamespace(report_language='zh')
    lines = []; service._append_market_snapshot(lines, result)
    for output in ('\n'.join(lines), render('markdown', [result])):
        assert 'amount_volume_price_mismatch' in output


def test_fixed_run2_snapshot_renders_correct_quantity_and_amount():
    context = _context()
    quote = _quote(); DataFetcherManager.__new__(DataFetcherManager)._enrich_realtime_quote(quote)
    context['realtime'] = quote.to_dict()
    snapshot = GeminiAnalyzer.__new__(GeminiAnalyzer)._build_market_snapshot(context)
    assert snapshot['volume'] == '1.02 亿股' and snapshot['amount'] == '47.17 亿元'
    assert snapshot['market_data_validation']['status'] == 'ok'
    assert snapshot['freshness_status'] == 'session_close' and snapshot['stale_seconds'] == 756
