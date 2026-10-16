"""Third-run regressions (Actions 38878294377), plus role/evidence boundaries."""
from copy import deepcopy
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.analyzer import AnalysisResult, GeminiAnalyzer
from src.config import Config
from src.notification import NotificationService
from src.report_consistency import apply_report_consistency
from src.services.report_renderer import render
from src.stock_analyzer import StockTrendAnalyzer, TrendAnalysisResult, VolumeStatus


def context():
    # Complete, internally consistent synthetic bars isolate report semantics.
    return {'code': '002624', 'date': '2026-10-16', 'yesterday': {'close': 10},
            'today': {'close': 10, 'open': 10, 'high': 10.2, 'low': 9.7, 'volume': 1000,
                      'amount': 10000, 'pct_chg': 0, 'ma5': 10, 'ma10': 10, 'ma20': 10},
            'realtime': {'price': 10, 'open_price': 10, 'high': 10.2, 'low': 9.7,
                         'volume': 1000, 'amount': 10000, 'pre_close': 10, 'change_pct': 0,
                         'volume_ratio': 1.07, 'source': 'tickflow',
                         'provider_timestamp': '2026-10-16T07:00:00+00:00'}}


def result(stop='9.45元', risk='若跌破9.45元止损卖出'):
    return AnalysisResult(code='002624', name='完美世界', sentiment_score=50,
                          trend_prediction='震荡', operation_advice='观望', decision_type='hold',
                          buy_reason='均线尚未确认', dashboard={
                              'core_conclusion': {'one_sentence': '观望'},
                              'data_perspective': {'price_position': {'support_level': 9.70},
                                                   'volume_analysis': {'volume_ratio': 1.07}},
                              'battle_plan': {'sniper_points': {'stop_loss': stop},
                                              'position_strategy': {'risk_control': risk}}})


def outputs(report):
    with patch('src.notification.get_config', return_value=Config(stock_list=[])):
        service = NotificationService()
    return [render('markdown', [report]), service.generate_dashboard_report([report])]


def test_perfect_world_separate_support_does_not_authorize_two_sell_triggers():
    r = result('9.45元（跌破近期低点）', '严格执行止损，若跌破9.70元短期支撑，防范进一步下探风险。')
    r.dashboard['core_conclusion']['position_advice'] = {'has_position': '若有效跌破9.70元需考虑减仓避险'}
    apply_report_consistency(r, context())
    validation = r.dashboard['stop_loss_validation']
    assert validation['original_value'] == '9.45元（跌破近期低点）'
    assert validation['trigger_levels'] == [9.7]
    assert r.dashboard['risk_levels']['support'] == {'price': 9.7, 'role': 'reference_only'}
    assert r.dashboard['risk_levels']['stop_loss']['price'] is None
    assert r.action == 'watch'
    for output in outputs(r):
        assert '数据缺失' in output and '核实前不可执行' in output
        assert '跌破9.70' not in output and '9.45元' not in output
    saved = deepcopy(r.to_dict())
    apply_report_consistency(r, context())
    assert r.dashboard['stop_loss_validation'] == saved['dashboard']['stop_loss_validation']


@pytest.mark.parametrize('warning', ['9.70元为预警价，仅观察不卖出', '若跌破9.70元触发预警，仅观察不自动卖出'])
def test_distinct_explicit_warning_stop_and_reference_support_are_valid(warning):
    r = result(risk=warning + '；若跌破9.45元止损卖出')
    apply_report_consistency(r, context())
    assert 'stop_loss_validation' not in r.dashboard
    assert r.dashboard['battle_plan']['sniper_points']['stop_loss'] == '9.45元'
    assert warning in r.dashboard['battle_plan']['position_strategy']['risk_control']
    assert r.dashboard['risk_levels']['stop_loss']['price'] == 9.45


def test_rebound_reduction_and_profit_target_do_not_invalidate_a_downside_stop():
    r = result('45.20元（跌破今日最低价需果断离场）', '严格控制风险，不摊平亏损')
    r.dashboard['core_conclusion']['position_advice'] = {
        'has_position': '持仓者建议：逢反抽MA5（约47.81元）减仓，跌破45.20元止损。'}
    r.dashboard['battle_plan']['sniper_points']['take_profit'] = '47.81元（MA5压力位，反抽至此减仓）'
    ctx = context()
    ctx['today'].update(close=45.68, open=46.5, high=47, low=45.2,
                        ma5=47.81, ma10=48, ma20=49, amount=45680)
    ctx['yesterday']['close'] = 45.68
    ctx['realtime'] = {}
    apply_report_consistency(r, ctx)
    assert 'stop_loss_validation' not in r.dashboard
    assert r.dashboard['battle_plan']['sniper_points']['stop_loss'] == '45.20元（跌破今日最低价需果断离场）'
    assert r.dashboard['battle_plan']['sniper_points']['take_profit'] == '47.81元（MA5压力位，反抽至此减仓）'


@pytest.mark.parametrize('field', ['analysis_summary', 'risk_warning', 'technical_analysis', 'volume_analysis',
                                   'short_term_outlook', 'medium_term_outlook'])
def test_all_body_fields_are_checked_for_independent_conflicting_stops(field):
    r = result()
    setattr(r, field, '若跌破9.70元，应立即止损卖出')
    apply_report_consistency(r, context())
    assert r.dashboard['stop_loss_validation']['reason'] == 'stop_loss_trigger_conflict'
    assert '跌破9.70' not in str(getattr(r, field))


@pytest.mark.parametrize('field', ['technical_analysis', 'pattern_analysis', 'trend_analysis', 'risk_warning'])
def test_watch_cannot_hide_immediate_action_in_body(field):
    r = result()
    setattr(r, field, '立即买入并加仓')
    assert 'action_conflict' in apply_report_consistency(r, context())
    assert '立即买入' not in str(getattr(r, field))
    assert r.action == 'watch'


@pytest.mark.parametrize('allocation', ['3成', '10%', '0-1成'])
def test_numeric_allocation_cannot_override_watch(allocation):
    r = result()
    r.dashboard['battle_plan']['position_strategy']['suggested_position'] = allocation
    assert 'action_conflict' in apply_report_consistency(r, context())
    assert allocation not in r.dashboard['battle_plan']['position_strategy']['suggested_position']


@pytest.mark.parametrize('ratio', [None, 0, -1, np.nan, np.inf])
def test_missing_or_invalid_etf_quote_ratio_cannot_become_shrinking_volume(ratio):
    ctx = context()
    ctx['code'] = '159206'
    ctx['realtime']['volume_ratio'] = ratio
    ctx['volume_change_ratio'] = .82  # Independent day/day input from real run.
    r = result()
    r.code, r.name = '159206', '卫星ETF永赢'
    r.dashboard['data_perspective']['volume_analysis'] = {
        'volume_ratio': None, 'volume_status': '缩量', 'volume_meaning': '缩量反弹显示向上动能不足'}
    r.analysis_summary = '今日虽上涨，但量能萎缩，属于弱势修复'
    r.dashboard['battle_plan']['action_checklist'] = ['量能配合（缩量反弹，力度存疑）']
    apply_report_consistency(r, ctx)
    volume = r.dashboard['data_perspective']['volume_analysis']
    assert volume['volume_ratio'] == 'N/A'
    assert '数据不足' in volume['volume_status']
    for output in outputs(r):
        assert 'None (' not in output and '缩量反弹' not in output and '量能萎缩' not in output
        assert '数据不足' in output
    assert ctx['volume_change_ratio'] == .82


@pytest.mark.parametrize('claim', ['量能平稳，缩量回调显示上方抛压不重，洗盘特征明显。',
                                  '✅ 检查项3：量能配合（缩量回踩，抛压衰竭）'])
def test_icbc_intent_claims_need_more_than_price_and_volume(claim):
    r = result()
    r.code, r.name = '601398', '工商银行'
    r.technical_analysis = claim
    r.dashboard['battle_plan']['action_checklist'] = [claim]
    ctx = context()
    ctx['realtime']['volume_ratio'] = .99
    apply_report_consistency(r, ctx)
    assert '不确定' in r.technical_analysis
    assert '不确定' in str(r.dashboard['battle_plan']['action_checklist'])
    for output in outputs(r):
        assert '洗盘特征明显' not in output and '缩量回踩，抛压衰竭' not in output
        assert 'tickflow' in output and '0.99' in output


# Actual raw stop/risk/ratio extracts from analysis-reports-3, without logs/configuration.
@pytest.mark.parametrize('code,stop,risk,ratio', [
    ('601398', '8.15元（略低于MA20支撑位）', '若收盘价有效跌破MA20（8.24元附近），需果断止损。', .99),
    ('600111', '34.62（今日最低价或跌破MA10）', '严格执行止损，若收盘价有效跌破35.20元应果断离场。', .79),
    ('002624', '9.45元（跌破近期低点）', '严格执行止损，若跌破9.70元短期支撑，防范进一步下探风险。', 1.07),
    ('159206', '1.150（跌破前期低点或支撑位）', '严格遵守多头排列原则，空头排列下以现金管理为主。', None),
    ('002273', '18.80元（若跌破今日低点并持续放量则止损）', '严格执行止损，不加仓摊平亏损。', .75),
    ('600487', '45.20元（跌破今日最低价需果断离场）', '严格遵守空头排列坚决不碰原则，不摊平亏损。', .95),
])
def test_six_stock_report_contract_includes_body_stop_allocation_and_action(code, stop, risk, ratio):
    r = result(stop, risk)
    r.code = code
    r.technical_analysis = '建议立即买入'
    r.dashboard['battle_plan']['position_strategy']['suggested_position'] = '3成'
    ctx = context()
    ctx['realtime']['volume_ratio'] = ratio
    apply_report_consistency(r, ctx)
    assert r.action == 'watch' and r.decision_type == 'hold'
    assert r.dashboard['action'] == r.action
    assert r.dashboard['report_consistency']['checked'] == ['action', 'stop_loss', 'allocation', 'body']
    for output in outputs(r):
        assert '建议立即买入' not in output and '**💰 仓位建议**: 3成' not in output


@pytest.mark.parametrize('volumes', [[100] * 5, [100] * 4 + [np.nan, 50], [0] * 6, [100] * 5 + [np.inf]])
def test_technical_volume_requires_six_complete_finite_bars(volumes):
    df = pd.DataFrame({'volume': volumes, 'close': [10] * len(volumes)})
    r = TrendAnalysisResult(code='159206')
    StockTrendAnalyzer()._analyze_volume(df, r)
    assert r.volume_status == VolumeStatus.UNKNOWN and r.volume_ratio_5d is None
    assert '数据不足' in r.volume_trend
    assert '数据不足' in StockTrendAnalyzer().format_analysis(r)


def test_real_five_day_volume_comparison_remains_available_without_intent_claim():
    df = pd.DataFrame({'volume': [100] * 5 + [50], 'close': [10] * 5 + [9.8]})
    r = TrendAnalysisResult(code='601398')
    StockTrendAnalyzer()._analyze_volume(df, r)
    assert r.volume_ratio_5d == .5 and r.volume_status == VolumeStatus.SHRINK_VOLUME_DOWN
    assert '前5日均量' in r.volume_trend and '不能据此确认洗盘' in r.volume_trend


def test_future_watch_condition_cannot_mask_a_current_unsupported_volume_claim():
    ctx = context()
    ctx['realtime'].pop('volume_ratio')
    r = result()
    r.technical_analysis = '今日缩量反弹，等待放量突破确认趋势'
    r.dashboard['phase_decision'] = {'watch_conditions': ['等待放量突破']}
    apply_report_consistency(r, ctx)
    assert '数据不足' in r.technical_analysis and '今日缩量反弹' not in r.technical_analysis
    assert r.dashboard['phase_decision']['watch_conditions'] == ['等待放量突破']


def test_prompt_requires_separate_price_roles_and_volume_denominators():
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    with patch('src.analyzer.get_config', return_value=Config(stock_list=[])):
        ctx = context()
        ctx['trend_analysis'] = {'volume_ratio_5d': .5}
        prompt = analyzer._format_prompt(ctx, '完美世界', None)
    assert '仅观察的预警价' in prompt and '唯一可执行止损触发价' in prompt
    assert '当日量/前5日均量: 0.5 (volume_ratio_5d)' in prompt
    assert '成交量较昨日变化' in prompt and '前5日均量' in prompt and '可核查数据依据' in prompt
