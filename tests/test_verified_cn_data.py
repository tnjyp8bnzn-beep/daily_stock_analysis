"""Public payload contracts and failure paths; no network or model calls.

Financial/announcement/flow fixtures are trimmed public responses captured on
2026-10-16. Synthetic boundaries below test rejection, never data completion.
"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

from data_provider.cn_public import PublicCNData, merge_verified_context
from data_provider.akshare_fetcher import AkshareFetcher
from data_provider.base import DataFetcherManager
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from src.analyzer import AnalysisResult
from src.config import Config
from src.market_analyzer import MarketAnalyzer, MarketIndex, MarketOverview
from src.notification import NotificationService
from src.report_consistency import apply_report_consistency
from src.services.report_renderer import render
from src.stock_analyzer import StockTrendAnalyzer, TrendAnalysisResult, VolumeStatus

NOW = datetime(2026, 10, 16, 19, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
FIXTURES = Path(__file__).parent / 'fixtures' / 'cn_public'


def payload(name):
    return json.loads((FIXTURES / name).read_text())


def response(data):
    return SimpleNamespace(json=lambda: deepcopy(data))


@pytest.mark.parametrize('fallback', [False, True])
def test_actual_dated_financial_facts_preserve_negative_profit(fallback):
    client = PublicCNData()
    replies = [requests.Timeout(), response(payload('financial_sina.json'))] if fallback else [response(payload('financial.json'))]
    with patch.object(client, '_request', side_effect=replies) as fetch:
        block = client.financials('002624', NOW)
    assert block['status'] == 'ok'
    assert block['kind'] == 'historical_fact'
    assert block['data']['report_date'] == '2026-06-30'
    assert block['data']['published_date'] == '2026-08-20'
    assert block['data']['net_profit_parent'] == pytest.approx(-118437769.22)
    assert block['data']['operating_cash_flow'] == pytest.approx(647301141.75)
    assert 'mirror' in block['source'] and block['fetched_at']
    if fallback:
        assert fetch.call_args.kwargs['params']['paperCode'] == 'sz002624'
        assert block['errors'] == ['eastmoney:Timeout']


@pytest.mark.parametrize('field,value', [('NOTICE_DATE', '2026-10-17'), ('SECURITY_CODE', '600487')])
def test_wrong_security_and_future_disclosures_cannot_fill_financials(field, value):
    data = payload('financial.json')
    data['result']['data'][0][field] = value
    client = PublicCNData()
    with patch.object(client, '_request', side_effect=[response(data), requests.Timeout()]):
        block = client.financials('002624', NOW)
    assert block['status'] == 'missing' and not block['data']
    assert 'eastmoney:no_dated_financial_record' in block['errors']


def test_official_announcements_first_then_explicit_mirror_excludes_future():
    client = PublicCNData()
    official = {'announcements': [{'secCode': '002624', 'announcementTitle': '正式披露',
        'announcementTime': int(NOW.timestamp()*1000), 'adjunctUrl': 'finalpage/2026-10-16/test.PDF'}]}
    metadata = {'stockList': [{'code': '002624', 'orgId': '9900021520'}]}
    with patch.object(client, '_request', side_effect=[response(metadata), response(official)]) as fetch:
        block = client.announcements('002624', NOW)
    assert fetch.call_count == 2 and block['source'] == 'cninfo_official'
    assert block['data'][0]['url'].startswith('https://static.cninfo.com.cn/')
    client = PublicCNData()
    with patch.object(client, '_request', side_effect=[requests.Timeout(), response(payload('announcements.json'))]):
        block = client.announcements('002624', NOW)
    assert block['source'] == 'issuer_disclosure_mirror'
    assert block['data'] and all(r['published_date'] <= '2026-10-16' for r in block['data'])
    assert not any('2026-10-17' == r['published_date'] for r in block['data'])
    assert block['errors'] == ['cninfo:Timeout']


def test_news_name_fallback_keeps_only_recent_relevant_dated_links():
    client = PublicCNData()
    rows = [{'title': '完美世界公告', 'date': '2026-10-16 17:00:00', 'url': 'https://example.com/a'},
            {'title': '完美世界未来报道', 'date': '2026-10-17', 'url': 'https://example.com/b'},
            {'title': '其他公司', 'date': '2026-10-16', 'url': 'https://example.com/c'},
            {'title': '完美世界旧闻', 'date': '2026-01-01', 'url': 'https://example.com/d'}]
    replies = [SimpleNamespace(text='callback({"result":{}})'),
               SimpleNamespace(text='callback('+json.dumps({'result': {'cmsArticleWebOld': rows}})+')')]
    with patch.object(client, '_request', side_effect=replies) as fetch:
        block = client.news('002624', '完美世界', as_of=NOW)
    assert [r['title'] for r in block['data']] == ['完美世界公告']
    assert json.loads(fetch.call_args.kwargs['params']['param'])['keyword'] == '完美世界'


def test_flow_preserves_actual_negative_value_date_and_identity():
    client = PublicCNData()
    with patch.object(client, '_request', return_value=response(payload('flow.json'))):
        block = client.capital_flow('002624', NOW)
        wrong = client.capital_flow('600111', NOW)
    flow = block['data']['stock_flow']
    assert flow['main_net_inflow'] == -32264248 and flow['trade_date'] == '2026-10-16'
    assert flow['unit'] == '元' and '非机构账户' in flow['definition']
    assert not wrong['data']
    with patch.object(client, '_request', side_effect=requests.Timeout()) as fetch:
        failed = client.capital_flow('002624', NOW)
    assert failed['missing_reason'] == 'fetch_failed' and failed['errors'] == ['Timeout']
    assert fetch.call_count == 1  # do not reinterpret unrelated quote field f62 as net flow


def test_legu_breadth_requires_matching_date_and_valid_counts():
    client = PublicCNData()
    html = '<div class="market-activity-meta">2026-10-16</div><table><tr><td>上涨</td><td>3061</td><td>下跌</td><td>2023</td></tr><tr><td>平盘</td><td>132</td><td>涨停</td><td>61</td></tr><tr><td>跌停</td><td>11</td><td>其他</td><td>0</td></tr></table>'
    with patch.object(client, '_request', return_value=SimpleNamespace(text=html)):
        data = client.market_stats(NOW)
    assert [data[k] for k in ['up_count', 'down_count', 'flat_count', 'limit_up_count', 'limit_down_count']] == [3061, 2023, 132, 61, 11]
    with patch.object(client, '_request', return_value=SimpleNamespace(text=html.replace('2026-10-16', '2026-10-15'))):
        assert client.market_stats(NOW) == {}


def test_partial_results_survive_sibling_timeout_and_failed_cache_is_retried():
    client = PublicCNData()
    ok = client._block([{'title': '公告'}], 'cninfo_official', 'https://www.cninfo.com.cn/')
    with patch.object(client, 'announcements', return_value=ok) as ann, \
            patch.object(client, 'news', side_effect=[requests.Timeout(), client._block([], 'public_news', '')]) as news, \
            patch.object(client, 'capital_flow', side_effect=requests.Timeout()):
        first = client.collect('159206', '卫星ETF', as_of=NOW, is_etf=True)
        second = client.collect('159206', '卫星ETF', as_of=NOW, is_etf=True)
    assert first['announcements'] == ok and second['announcements'] == ok
    assert ann.call_count == 1 and news.call_count == 2
    assert first['financials']['status'] == 'not_supported'
    assert first['news']['errors'] == ['Timeout']


def test_financial_periods_not_mixed_when_newest_verified_report_arrives():
    old = {'earnings': {'data': {'financial_report': {'report_date': '2026-03-31', 'revenue': 1}}, 'errors': ['timeout']}}
    fin = PublicCNData()._block({'report_date': '2026-06-30', 'net_profit_parent': -2}, 'mirror', 'https://example.com')
    merged = merge_verified_context(old, {'financials': fin})
    assert merged['earnings']['data']['financial_report'] == fin['data']
    assert merged['earnings']['data']['historical_financial_reports'][0]['revenue'] == 1
    assert merged['earnings']['errors'] == ['timeout'] and old['earnings']['data']['financial_report']['revenue'] == 1


@pytest.mark.parametrize('method,api', [('_fetch_stock_data_em', 'stock_zh_a_hist'), ('_fetch_etf_data', 'fund_etf_hist_em')])
def test_akshare_eastmoney_daily_units_convert_at_source_once(method, api):
    raw = pd.DataFrame([{'日期': '2026-10-16', '成交量': 123, '成交额': 123000, '收盘': 10}])
    fetcher = AkshareFetcher()
    with patch.object(fetcher, '_set_random_user_agent'), patch.object(fetcher, '_enforce_rate_limit'), \
            patch('akshare.'+api, return_value=raw):
        normalized = fetcher._normalize_data(getattr(fetcher, method)('002624', '2026-10-01', '2026-10-16'), '002624')
    assert normalized.iloc[0]['volume'] == 12300 and normalized.iloc[0]['amount'] == 123000
    assert raw.iloc[0]['成交量'] == 123
    # Common normalizer must leave Sina's already-share volume unchanged.
    assert fetcher._normalize_data(pd.DataFrame([{'date': '2026-10-16', 'volume': 12300}]), '002624').iloc[0]['volume'] == 12300


def test_etf_explicit_tencent_route_does_not_fall_back_to_fund_spot():
    fetcher = AkshareFetcher()
    quote = UnifiedRealtimeQuote('159206', source=RealtimeSource.TENCENT, price=1.182, volume_ratio=1.25)
    with patch.object(fetcher, '_get_stock_realtime_quote_tencent', return_value=quote) as tx, \
            patch.object(fetcher, '_get_etf_realtime_quote', side_effect=AssertionError('wrong route')):
        assert fetcher.get_realtime_quote('159206', source='tencent') is quote
    tx.assert_called_once_with('159206')


@pytest.mark.parametrize('price,stamp,status', [(10, '2026-10-16T15:00:00+08:00', 'same_day_agreement'),
                                               (11, '2026-10-16T15:00:00+08:00', 'price_conflict'),
                                               (10, '2026-10-15T15:00:00+08:00', 'different_date')])
def test_quote_cross_validation_blocks_conflicts_and_preserves_per_field_provenance(price, stamp, status):
    p = UnifiedRealtimeQuote('002624', source=RealtimeSource.TICKFLOW, price=10,
                            provider_timestamp='2026-10-16T07:00:00+00:00')
    s = UnifiedRealtimeQuote('002624', source=RealtimeSource.TENCENT, price=price, volume_ratio=1.07,
                            provider_timestamp=stamp, fetched_at=NOW.isoformat())
    filled = DataFetcherManager._merge_quote_fields(p, s)
    assert p.price == 10 and p.to_dict()['cross_validation']['status'] == status
    if status == 'same_day_agreement':
        assert 'volume_ratio' in filled and p.to_dict()['field_sources']['volume_ratio']['source'] == 'tencent'
    else:
        assert filled == [] and p.volume_ratio is None


@pytest.mark.parametrize('latest_volume', [10000., 0.])
def test_invalid_volume_gets_no_volume_score_and_ma60_is_not_ma20(latest_volume):
    analyzer = StockTrendAnalyzer()
    df = pd.DataFrame({'date': pd.date_range('2026-08-01', periods=39), 'close': [10.]*39,
                       'high': [11.]*39, 'low': [9.]*39, 'open': [10.]*39,
                       'volume': [100.]*38+[latest_volume], 'amount': [1000.]*39})
    r = analyzer.analyze(df, '002624')
    assert r.volume_status == VolumeStatus.UNKNOWN and r.volume_ratio_5d is None
    assert r.ma60 == 0 and r.ma20 == 10
    a = TrendAnalysisResult('002624', volume_status=VolumeStatus.UNKNOWN)
    b = deepcopy(a);b.volume_status = VolumeStatus.NORMAL
    analyzer._generate_signal(a);analyzer._generate_signal(b)
    assert b.signal_score-a.signal_score == 10


def ctx(code):
    context = {'code': code, 'date': '2026-10-16', 'yesterday': {'close': 10},
            'today': dict(close=10, open=10, high=10.2, low=9.7, volume=1000, amount=10000,
                          pct_chg=0, ma5=10, ma10=10, ma20=10),
            'fundamental_context': {'verified_data': {
                'financials': PublicCNData()._block({'report_date': '2026-06-30', 'revenue': 2751024838.4,
                    'net_profit_parent': -118437769.22, 'operating_cash_flow': 647301141.75,
                    'roe': -1.7, 'published_date': '2026-08-20'}, 'eastmoney_disclosure_mirror', 'https://data.eastmoney.com/'),
                'news': PublicCNData()._block([], 'public_news', '', reason='no_recent_relevant_dated_news') }},
            'chip_availability': {'status': 'disabled', 'reason': '现有配置关闭筹码分布取数'}}
    if code == '159206':
        block = PublicCNData()._block({}, 'asset_capability', '', reason='ETF不适用上市公司财报')
        block['status'] = 'not_supported'
        context['fundamental_context']['verified_data']['financials'] = block
    return context


@pytest.mark.parametrize('code', ['002273', '600111', '600487', '002624', '601398', '159206'])
def test_six_reports_keep_real_fields_when_ai_omits_them_and_prices_are_numeric_nullable(code):
    r = AnalysisResult(code=code, name='测试', sentiment_score=50, trend_prediction='震荡',
                       operation_advice='观望', decision_type='hold', buy_reason='均线尚未确认', dashboard={
        'core_conclusion': {'one_sentence': '观望', 'position_advice': {'no_position': '观望', 'has_position': '持仓者跌破9.45元止损'}},
        'battle_plan': {'sniper_points': {'stop_loss': '9.45元', 'take_profit': '20元（AI猜测）'},
                        'position_strategy': {'risk_control': '跌破9.45元止损'}}})
    apply_report_consistency(r, ctx(code))
    assert r.dashboard['core_conclusion']['position_advice']['has_position'] == '持仓者跌破9.45元止损'
    points = r.dashboard['battle_plan']['sniper_points']
    assert points['stop_loss'] == 9.45 and points['take_profit'] is None
    assert all(v is None or isinstance(v, float) for v in points.values())
    with patch('src.notification.get_config', return_value=Config(stock_list=[])):
        notification = NotificationService()
    for output in [render('markdown', [r]), notification.generate_dashboard_report([r])]:
        if code == '159206':
            assert 'ETF不适用上市公司财报' in output
            assert '2751024838.4' not in output
        else:
            assert '2751024838.4' in output and '-118437769.22' in output
        for label in ['最新公告', '新闻 / 行业动态', '分红记录', '板块资金排行', '筹码分布', '技术指标', '仅观察预警价', '目标价']:
            assert label in output
        assert 'target_basis_unverified' in output and '现有配置关闭' in output


@pytest.mark.parametrize('bad_price', [-9.45, {'price': 9.45}, [9.45]])
def test_held_position_cannot_override_watch_with_buy_and_invalid_price_is_rejected(bad_price):
    r = AnalysisResult(code='002624', name='测试', sentiment_score=50, trend_prediction='震荡',
                       operation_advice='观望', buy_reason='均线尚未确认', dashboard={
        'core_conclusion': {'position_advice': {'has_position': '立即加仓买入'}},
        'battle_plan': {'sniper_points': {'stop_loss': bad_price}}})
    apply_report_consistency(r, ctx('002624'))
    assert '买入' not in r.dashboard['core_conclusion']['position_advice']['has_position']
    assert r.dashboard['battle_plan']['sniper_points']['stop_loss'] is None
    assert r.dashboard['price_validation']['stop_loss']['reason']
    assert r.action == 'watch'


def test_market_does_not_sum_component_amounts_as_whole_market_turnover():
    analyzer = object.__new__(MarketAnalyzer)
    analyzer.region = 'cn';analyzer.market_label = 'A股'
    analyzer.data_manager = Mock()
    analyzer.data_manager.get_market_stats.return_value = {'up_count': 3061, 'down_count': 2023,
        'flat_count': 132, 'limit_up_count': 61, 'limit_down_count': 11, 'source': 'legulegu_public',
        'trade_date': '2026-10-16', 'fetched_at': NOW.isoformat()}
    overview = MarketOverview(date='2026-10-16', indices=[MarketIndex(code='000001', name='上证', amount=100),
                                                         MarketIndex(code='399001', name='深成指', amount=200)])
    analyzer._get_market_statistics(overview)
    assert overview.up_count == 3061 and overview.total_amount == 0
    assert overview.data_provenance['total_amount']['status'] == 'missing'


def test_market_render_injects_verified_news_even_when_model_omits_it():
    with patch('src.market_analyzer.DataFetcherManager'):
        analyzer = MarketAnalyzer(config=Config(stock_list=[]))
    news = [{'title': '可核查新闻标题', 'source': '真实数据源', 'published_date': '2026-10-16',
             'url': 'https://example.com/verified'}]
    review = '### 一、盘面总览\n数据不足\n### 二、指数结构\n观察\n### 五、消息催化\n模型遗漏'
    overview = MarketOverview(date='2026-10-16', up_count=3061, down_count=2023, flat_count=132,
                              data_provenance={'breadth': {'source': 'legulegu_public', 'trade_date': '2026-10-16'}})
    output = analyzer._inject_data_into_review(review, overview, news)
    assert '可核查新闻标题' in output and 'https://example.com/verified' in output
    assert '3061' in output and 'legulegu_public' in output
    assert '### 五、消息催化' in output


def test_numeric_missing_stop_with_reason_does_not_trigger_ai_price_fabrication_retry():
    from src.analyzer import check_content_integrity
    r = AnalysisResult(code='002624', name='测试', sentiment_score=50, trend_prediction='震荡',
                       operation_advice='观望', buy_reason='等待', dashboard={
        'battle_plan': {'sniper_points': {'stop_loss': None}}})
    assert 'dashboard.battle_plan.sniper_points.stop_loss' in check_content_integrity(r)[1]
    r.dashboard['price_validation'] = {'stop_loss': {'status': 'unconfirmed', 'reason': 'insufficient_market_data'}}
    assert 'dashboard.battle_plan.sniper_points.stop_loss' not in check_content_integrity(r)[1]


def test_unverified_historical_stop_is_also_removed_from_held_sell_instruction():
    r = AnalysisResult(code='002624', name='测试', sentiment_score=50, trend_prediction='震荡',
                       operation_advice='观望', buy_reason='等待均线', dashboard={
        'core_conclusion': {'position_advice': {'has_position': '跌破9.40元止损'}},
        'battle_plan': {'sniper_points': {'stop_loss': '9.40元（近期低点）'},
                        'position_strategy': {'risk_control': '跌破9.40元止损'}}})
    apply_report_consistency(r, ctx('002624'))
    assert r.dashboard['battle_plan']['sniper_points']['stop_loss'] is None
    assert '跌破9.40' not in r.dashboard['core_conclusion']['position_advice']['has_position']
    assert '跌破9.40' not in r.dashboard['battle_plan']['position_strategy']['risk_control']


@pytest.mark.parametrize('date', ['2026-10-17', '2026-09-01', '2026-99-99'])
def test_market_factual_injection_rejects_future_old_and_invalid_news_dates(date):
    with patch('src.market_analyzer.DataFetcherManager'):
        analyzer = MarketAnalyzer(config=Config(stock_list=[]))
    overview = MarketOverview(date='2026-10-16')
    output = analyzer._inject_data_into_review('### 五、消息催化\n数据不足', overview, [
        {'title': '不得发布的时间不符新闻', 'source': '测试', 'url': 'https://example.com/a', 'published_date': date}])
    assert '不得发布' not in output


def test_t_plus_one_is_not_a_one_yuan_stop_and_valid_bank_protection_survives():
    r = AnalysisResult(code='601398', name='工商银行', sentiment_score=50, trend_prediction='震荡',
        operation_advice='观望', buy_reason='等待均线确认', dashboard={
        'core_conclusion': {'position_advice': {'has_position': '跌破MA20再行减仓'}},
        'battle_plan': {'sniper_points': {'stop_loss': '8.23（跌破MA20止损）'},
            'position_strategy': {'risk_control': '严格执行T+1制度下的止损策略，若收盘价有效跌破MA20则全线撤退。'}}})
    context = ctx('601398');context['today']['ma20'] = 8.2355
    apply_report_consistency(r, context)
    assert 'stop_loss_validation' not in r.dashboard
    assert r.dashboard['battle_plan']['sniper_points']['stop_loss'] == 8.23
    assert 'T+1' in r.dashboard['battle_plan']['position_strategy']['risk_control']


def test_invalid_downside_stop_does_not_erase_independently_verified_rebound_target():
    r = AnalysisResult(code='600487', name='亨通光电', sentiment_score=50, trend_prediction='震荡',
        operation_advice='观望', buy_reason='等待均线确认', dashboard={
        'battle_plan': {'sniper_points': {'stop_loss': '8元（今日最低价）',
                                          'take_profit': '10.0元（MA5压力位，反弹减仓目标）'}}})
    apply_report_consistency(r, ctx('600487'))
    points = r.dashboard['battle_plan']['sniper_points']
    assert points['stop_loss'] is None and points['take_profit'] == 10.0
    assert r.dashboard['price_validation']['take_profit']['original'] == '10.0元（MA5压力位，反弹减仓目标）'


def test_secondary_volume_ratio_with_unknown_time_never_borrows_primary_quote_time():
    from src.report_consistency import _volume_evidence
    evidence = _volume_evidence({'realtime': {'volume_ratio': 1.25, 'source': 'tickflow',
        'provider_timestamp': '2026-10-16T15:00:00+08:00',
        'field_sources': {'volume_ratio': {'source': 'tencent', 'provider_timestamp': None}}}})
    assert evidence['ratio'] == 1.25 and evidence['source'] == 'tencent'
    assert evidence['quote_time'] is None
