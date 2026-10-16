"""Final, deterministic evidence and action checks before reports are saved."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from src.report_language import localize_confidence_level, normalize_report_language
from src.schemas.decision_action import localize_action_label, normalize_decision_action
from src.schemas.decision_scale import action_for_score
from src.market_data_quality import context_quality, finite_number


_FACT_MARKERS = re.compile(
    r'新闻|消息|公告|机构|研报|评级|目标价|主力|资金|大单|外资|净流入|净流出|北向|南向|抢筹|吸筹|出货|'
    r'市场情绪|投资者情绪|舆情|热点|利好|利空|'
    r'기관|자금|뉴스|시장 심리|투자자 심리|호재|악재|공시|'
    r'\b(news|institutional|analysts?|capital flows?|fund flows?|net inflows?|net outflows?|'
    r'market sentiment|investors?|northbound|southbound|ratings?)\b', re.I,
)
_URL = re.compile(r'https?://[^\s<>\]\)]+')
_DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
_BUCKET = {'buy': 'buy', 'add': 'buy', 'sell': 'sell', 'reduce': 'sell',
           'hold': 'hold', 'watch': 'hold', 'avoid': 'hold', 'alert': 'hold'}
_MISSING = {
    'zh': '数据缺失：未获得可核查依据。',
    'en': 'Data missing: no verifiable evidence was obtained.',
    'ko': '데이터 부족: 확인 가능한 근거가 없습니다.',
}
_NARRATIVES = (
    'dashboard', 'analysis_summary', 'key_points', 'risk_warning', 'buy_reason',
    'trend_analysis', 'short_term_outlook', 'medium_term_outlook', 'technical_analysis',
    'ma_analysis', 'volume_analysis', 'pattern_analysis', 'fundamental_analysis',
    'sector_position', 'company_highlights', 'news_summary', 'market_sentiment', 'hot_topics', 'data_sources',
)
_AUDIT_KEYS = {'stop_loss_validation', 'report_consistency', 'market_data_validation',
               'decision_stability', 'decision_score_calibration', 'risk_levels'}
_VOLUME_CLAIM = re.compile(r'缩量|放量|平量|量能萎缩|成交萎缩|量能平稳|量能正常|'
                           r'\b(?:shrinking|low|high|normal) volume\b', re.I)
_INTENT_CLAIM = re.compile(r'洗盘|抛压衰竭|抛压耗尽|多头抵抗|承接力|'
                           r'\bshakeout|selling pressure (?:exhausted|exhaustion)\b', re.I)
_STOP_TRIGGER = re.compile(r'止损|跌破|离场|减仓|清仓|stop.?loss|break below|\bexit\b', re.I)
_PRICE = re.compile(r'(?<![A-Za-z\d])\d+(?:\.\d+)?(?![\d.])(?:元)?(?!\s*[%％成])')


def _map_prose(value: Any, transform, path: str = '') -> Any:
    """Visit every displayed narrative, keeping audit originals out of execution."""
    if isinstance(value, dict):
        return {key: item if key in _AUDIT_KEYS else _map_prose(item, transform, f'{path}.{key}')
                for key, item in value.items()}
    if isinstance(value, list):
        return [_map_prose(item, transform, path) for item in value]
    return transform(value, path) if isinstance(value, str) else value


def _prices(text: str) -> list[float]:
    text = re.sub(r'检查项\d+', '', text)
    return [float(match.group().removesuffix('元')) for match in _PRICE.finditer(text)]


def _stop_clauses(text: str) -> list[str]:
    clauses = []
    for clause in re.split(r'[；;。，,\n]', text):
        # Rebound reductions / profit targets are separate from downside stops.
        if not re.search(r'止损|跌破|stop.?loss|break below', clause, re.I):
            continue
        # A warning must explicitly exclude execution to differ from the stop.
        observation = re.search(r'预警|warning', clause, re.I)
        no_execution = re.search(r'仅观察|不(?:自动)?(?:卖出|止损)|observation.only|no (?:sale|execution)', text, re.I)
        if observation and no_execution:
            continue
        clauses.append(clause)
    return clauses


def _volume_evidence(context: dict) -> dict:
    rt = context.get('realtime') or context.get('realtime_quote') or {}
    ratio = finite_number(rt.get('volume_ratio'))
    return {'ratio': ratio if ratio is not None and ratio > 0 else None,
            'source': rt.get('source'), 'quote_time': rt.get('provider_timestamp')}


def sanitize_factual_prose(text: str, evidence: str, language: str = 'zh') -> str:
    """Apply the same extract-only factual rule to free-form market reviews."""
    missing = _MISSING[normalize_report_language(language)]
    lines = []
    factual_section = False
    for line in text.splitlines():
        if line.lstrip().startswith('#'):
            factual_section = bool(_FACT_MARKERS.search(line))
            lines.append(line)
            continue
        if line.strip() and (factual_section or _FACT_MARKERS.search(line)):
            grounded = bool(_URL.search(line) and _DATE.search(line) and line.strip() in evidence)
            if not grounded:
                if not lines or lines[-1] != missing:
                    lines.append(missing)
                continue
        lines.append(line)
    return '\n'.join(lines)


def _positive(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (ValueError, TypeError):
        return False


def _validate_stop_anchor(dashboard: dict, context: dict, narratives: list | None = None) -> bool:
    """Invalidate a contradictory executable stop; retain original audit evidence."""
    battle = dashboard.get('battle_plan') or {}
    points = (battle.get('sniper_points') or {}) if isinstance(battle, dict) else {}
    if not isinstance(points, dict):
        return False
    original = points.get('stop_loss')
    previous = dashboard.get('stop_loss_validation') or {}
    if isinstance(previous, dict) and previous.get('status') == 'invalid':
        return True
    if not isinstance(original, (str, int, float)):
        return False
    text = str(original)
    numeric_literal = re.fullmatch(r'[-+]?(?:nan|inf(?:inity)?|\d+(?:\.\d+)?)(?:元)?', text.strip(), re.I)
    numeric_value = finite_number(text.strip().removesuffix('元'))
    invalid_level = isinstance(original, bool) or (
        bool(numeric_literal) and (numeric_value is None or numeric_value <= 0)
    )
    strategy = battle.get('position_strategy') or {}
    references = [strategy.get('risk_control')] if isinstance(strategy, dict) else []
    checklist = battle.get('action_checklist')
    if isinstance(checklist, list):
        references.extend(checklist)
    initial_references = references
    references = []
    def collect(value, path):
        if path.endswith(('.stop_loss', '.take_profit')) or not _STOP_TRIGGER.search(value):
            return value
        # Explicit non-executable warnings/support observations may differ from a stop.
        if _stop_clauses(value) and value not in references:
            references.append(value)
        return value

    for narrative in [*initial_references, *(narratives or [])]:
        _map_prose(narrative, collect)
    trigger_clauses = [clause for reference in references for clause in _stop_clauses(reference)]
    anchor_text = '\n'.join([text, *trigger_clauses])
    today = context.get('today') or context.get('daily_snapshot') or {}
    anchors = {}
    if re.search(r'(?:今日|当日|今天|本日).{0,3}(?:最低价?|低点)|today.{0,12}low', anchor_text, re.I):
        anchors['low'] = finite_number(today.get('low'))
    for period in re.findall(r'(?<![A-Za-z0-9])MA(5|10|20|60)(?!\d)', anchor_text, re.I):
        anchors[f'ma{period}'] = finite_number(today.get(f'ma{period}'))
    numbers = _prices(text)
    level = finite_number(numbers[0]) if numbers else None
    trigger_levels = sorted({number for clause in trigger_clauses for number in _prices(clause)})
    conflicting_levels = level is not None and any(abs(level - number) > .011 for number in trigger_levels)
    invalid = invalid_level or any(
        value is None or value <= 0 or (level is not None and abs(level - value) > .011)
        for value in anchors.values()) or conflicting_levels
    perspective = dashboard.get('data_perspective') or {}
    position = (perspective.get('price_position') or {}) if isinstance(perspective, dict) else {}
    position = position if isinstance(position, dict) else {}
    dashboard['risk_levels'] = {
        'stop_loss': {'price': level if not invalid else None, 'role': 'executable_stop' if not invalid else 'unconfirmed'},
        'support': {'price': finite_number(position.get('support_level')), 'role': 'reference_only'},
        'warning': {'role': 'observation_only', 'requires_explicit_label': True},
    }
    if invalid:
        dashboard['stop_loss_validation'] = {
            'status': 'invalid', 'reason': ('stop_loss_trigger_conflict' if conflicting_levels else
                                          'stop_loss_anchor_mismatch_or_missing' if anchors else 'stop_loss_value_invalid'),
            'original_value': text if finite_number(original) is None else original,
            'claimed_level': level, 'anchors': anchors,
            'trigger_text': references,
            'trigger_levels': trigger_levels,
        }
    return invalid


def apply_report_consistency(result: Any, context: dict, news_context: str | None = None) -> list[str]:
    """Fail closed on unsupported facts, missing technical basis, or action conflicts.

    Factual intelligence must be an extract from supplied, dated, linked evidence.
    A model-generated URL or a nonempty 'no results' placeholder is not evidence.
    This is intentionally conservative; technical interpretations remain available.
    """
    language = normalize_report_language(getattr(result, 'report_language', 'zh'))
    missing = _MISSING[language]
    wait = {
        'zh': '观望：等待可核查数据与一致的交易依据，暂不执行买卖。',
        'en': 'Watch: await verifiable data and consistent evidence before trading.',
        'ko': '관망: 확인 가능한 데이터와 일관된 근거를 기다립니다.',
    }[language]
    evidence = str(news_context or '')
    linked_evidence = bool(_URL.search(evidence) and _DATE.search(evidence))
    adjustments: list[str] = []
    volume_evidence = _volume_evidence(context)
    ratio = volume_evidence['ratio']
    volume_missing = {
        'zh': '数据不足：行情量比缺失，无法判定放量或缩量；日间成交量比较不等同行情量比。',
        'en': 'Insufficient data: quote volume ratio missing; day-to-day volume comparisons are a separate measure.',
        'ko': '데이터 부족: 시세 거래량 비율이 없습니다. 일간 거래량 비교는 별도 지표입니다.',
    }[language]
    uncertain = {
        'zh': '不确定：现有量价数据不能证实洗盘或抛压衰竭，需更多可核查证据。',
        'en': 'Uncertain: price and volume alone cannot establish a shakeout or exhausted selling pressure.',
        'ko': '불확실: 가격과 거래량만으로 매도 압력 소진이나 의도를 확인할 수 없습니다.',
    }[language]

    def grounded(text: str) -> bool:
        # Require the factual text itself, not just a citation the model invented.
        return bool(linked_evidence and _URL.search(text) and _DATE.search(text) and text.strip() in evidence)

    def clean(value: Any, path: str = '') -> Any:
        if isinstance(value, dict):
            return {key: clean(item, f'{path}.{key}') for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item, path) for item in value]
        if not isinstance(value, str) or not value.strip():
            return value
        factual_field = path.rsplit('.', 1)[-1] in {
            'news_summary', 'latest_news', 'hot_topics', 'market_sentiment',
            'sentiment_summary', 'earnings_outlook', 'risk_alerts', 'positive_catalysts',
            'institutional_views', 'capital_flow',
        }
        if (factual_field or _FACT_MARKERS.search(value)) and not grounded(value):
            adjustments.append('unsupported_fact_removed')
            return missing
        if _INTENT_CLAIM.search(value) and not grounded(value) and value != uncertain:
            adjustments.append('unsupported_interpretation_qualified')
            return uncertain
        # Future observation conditions do not assert that volume has already changed.
        actual_volume_claim = False
        for clause in re.split(r'[，,。；;\n]', value):
            claim = _VOLUME_CLAIM.search(clause)
            if claim and not re.search(r'等待|若|如果|需观察|待|\bif\b|\bwait\b', clause[:claim.start()], re.I):
                actual_volume_claim = True
        if ratio is None and actual_volume_claim and value != volume_missing:
            adjustments.append('unsupported_volume_claim_removed')
            return volume_missing
        return value

    # Traverse all displayed narratives, including nested checklist/strategy text.
    for field in _NARRATIVES:
        setattr(result, field, _map_prose(getattr(result, field, None), clean, field))

    # Mandatory disclosure even if the model left these fields empty.
    for field in ('news_summary', 'market_sentiment', 'hot_topics'):
        if not getattr(result, field, None):
            setattr(result, field, missing)
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    perspective = dashboard.get('data_perspective')
    if isinstance(perspective, dict) and isinstance(perspective.get('volume_analysis'), dict):
        volume = perspective['volume_analysis']
        volume['volume_ratio'] = ratio if ratio is not None else 'N/A'
        volume['volume_evidence'] = volume_evidence
        if ratio is None:
            volume.update(volume_status=volume_missing, volume_meaning=volume_missing)
        else:
            volume['volume_status'] = ('放量' if ratio >= 1.2 else '缩量' if ratio < .8 else '平量') if language == 'zh' else (
                'High volume' if ratio >= 1.2 else 'Low volume' if ratio < .8 else 'Normal volume')
            source = volume_evidence['source'] or 'unknown'
            quote_time = volume_evidence['quote_time'] or 'unknown'
            volume['volume_meaning'] = (
                f'行情量比 {ratio:g}，来源 {source}，报价时间 {quote_time}；仅反映相对成交活跃度，不能证实交易意图。'
                if language == 'zh' else f'Quote volume ratio {ratio:g}; source {source}; quote time {quote_time}. '
                'This measures relative volume, not trading intent.'
            )
    intel = dashboard.setdefault('intelligence', {})
    if not isinstance(intel, dict):
        intel = dashboard['intelligence'] = {}
    for key in ('latest_news', 'sentiment_summary', 'capital_flow', 'institutional_views'):
        if not intel.get(key):
            intel[key] = missing

    flow = (context.get('fundamental_context') or {}).get('capital_flow') or {}
    if (isinstance(flow, dict) and flow.get('status') == 'ok'
            and flow.get('source') and flow.get('data')):
        # Show fetched structured values with their provenance, never model guesses.
        intel['capital_flow'] = json.dumps(flow, ensure_ascii=False, default=str)

    daily = context.get('today') or {}
    realtime = context.get('realtime') or context.get('realtime_quote') or {}
    quality = context_quality(context)
    dashboard['market_data_validation'] = quality
    stop_invalid = _validate_stop_anchor(dashboard, context, [getattr(result, field, None) for field in _NARRATIVES])
    if stop_invalid:
        adjustments.append('stop_loss_anchor_conflict')
    if quality['status'] != 'ok':
        adjustments.append('market_data_incomplete_or_invalid')
    reason = str(getattr(result, 'buy_reason', '') or '').strip()
    has_reason = bool(reason and reason.lower() not in {'n/a', 'none', 'null', '-', '--', '无', '未知'}
                      and not re.search(r'数据缺失|数据不足|无法判断|待补充|暂无|insufficient|unavailable|no data|unknown',
                                        reason, flags=re.I))
    has_basis = bool(
        not context.get('data_missing') and getattr(result, 'success', True)
        and (_positive(realtime.get('price')) or _positive(daily.get('close')))
        and all(_positive(daily.get(key)) for key in ('ma5', 'ma10', 'ma20'))
        and quality['status'] == 'ok' and not stop_invalid
        and realtime.get('is_stale') is not True
        and has_reason
    )
    advice_action = normalize_decision_action(getattr(result, 'operation_advice', None))
    direction = _BUCKET.get(advice_action)
    conflict = advice_action is None
    explicit = normalize_decision_action(getattr(result, 'action', None))
    if explicit and _BUCKET[explicit] != direction:
        conflict = True
    decision_type = getattr(result, 'decision_type', None)
    if decision_type in ('buy', 'sell') and decision_type != direction:
        conflict = True

    # A conditional entry plan must never silently override a Watch conclusion.
    core = dashboard.get('core_conclusion') or {}
    battle = dashboard.get('battle_plan') or {}
    phase = dashboard.get('phase_decision') or {}
    strategy = battle.get('position_strategy', {}) if isinstance(battle, dict) else {}
    plans = [getattr(result, field, None) for field in _NARRATIVES]

    def conflicting(value: Any, *, entry_only: bool = False) -> bool:
        if isinstance(value, dict):
            return any(conflicting(item, entry_only=entry_only) for key, item in value.items()
                       if key not in _AUDIT_KEYS and key not in {'stop_loss', 'take_profit'})
        if isinstance(value, list):
            return any(conflicting(item, entry_only=entry_only) for item in value)
        if not isinstance(value, str) or value == missing:
            return False
        for sentence in re.split(r'[。；;\n]', value):
            if (not entry_only and not stop_invalid and _STOP_TRIGGER.search(sentence)
                    and re.search(r'若|如果|跌破|\bif\b|below', sentence, re.I)
                    and not re.search(r'买入|加仓|建仓|\bbuy\b|\badd\b', sentence, re.I)):
                continue
            for clause in re.split(r'[，,：:]|同时|但|而|并且?|以及|\band\b|\bbut\b', sentence, flags=re.I):
                action = normalize_decision_action(clause)
                actions = [action] if action else [normalize_decision_action(token) for token in re.findall(
                    r'买入|加仓|建仓|减仓|卖出|清仓|持有|观望|\b(?:buy|add|sell|reduce|hold|watch)\b', clause, re.I)]
                if any(action and (_BUCKET[action] == 'buy' if entry_only else _BUCKET[action] != direction)
                       for action in actions):
                    return True
        return False

    conflict = conflict or any(conflicting(plan) for plan in plans)
    allocation = str(strategy.get('suggested_position', '')) if isinstance(strategy, dict) else ''
    if direction == 'hold' and re.search(r'[1-9]\d*(?:\.\d+)?\s*(?:成|[%％])', allocation):
        conflict = True
    if conflict or not has_basis:
        adjustments.append('action_conflict' if conflict else 'insufficient_basis')
        advice_action = 'watch'
        direction = 'hold'
        result.confidence_level = localize_confidence_level('低', language)
        if not result.analysis_summary or conflicting(result.analysis_summary):
            result.analysis_summary = wait
        result.buy_reason = wait
        if not result.key_points or conflicting(result.key_points):
            result.key_points = wait
        core = dict(core) if isinstance(core, dict) else {}
        core.update({
            'one_sentence': wait, 'signal_type': localize_action_label('watch', language),
            'position_advice': {'no_position': wait, 'has_position': wait},
        })
        dashboard['core_conclusion'] = core
        battle = dict(battle) if isinstance(battle, dict) else {}
        points = battle.get('sniper_points')
        points = dict(points) if isinstance(points, dict) else {}
        points.pop('ideal_buy', None)
        points.pop('secondary_buy', None)
        stop_loss = points.get('stop_loss')
        if (stop_invalid or stop_loss is None or isinstance(stop_loss, (list, tuple, dict))
                or (isinstance(stop_loss, str) and not stop_loss.strip())
                or (isinstance(stop_loss, (int, float)) and not _positive(stop_loss))):
            points['stop_loss'] = missing
        strategy = dict(strategy) if isinstance(strategy, dict) else {}
        strategy.update({'suggested_position': wait, 'entry_plan': wait})

        def protective(value: str) -> bool:
            return bool(re.search(r'止损|跌破|stop[ _-]?loss|below', value, flags=re.I))

        # Preserve protective stops; only suppress aggressive entry instructions.
        if (not strategy.get('risk_control')
                or conflicting(strategy.get('risk_control'), entry_only=True)
                or (conflicting(strategy.get('risk_control')) and not protective(str(strategy['risk_control'])))):
            strategy['risk_control'] = wait
        checklist = battle.get('action_checklist')
        checklist = [item for item in checklist if isinstance(item, str)
                     and not conflicting(item, entry_only=True)
                     and (not conflicting(item) or protective(item))] if isinstance(checklist, list) else []
        if wait not in checklist:
            checklist.append(wait)
        battle.update({'sniper_points': points, 'position_strategy': strategy, 'action_checklist': checklist})
        dashboard['battle_plan'] = battle
        # Eliminate stray contradictory instructions from other narrative fields.
        for field in ('short_term_outlook', 'medium_term_outlook', 'risk_warning', 'trend_analysis',
                      'technical_analysis', 'ma_analysis', 'volume_analysis', 'pattern_analysis',
                      'fundamental_analysis', 'sector_position', 'company_highlights'):
            if conflicting(getattr(result, field, '')):
                setattr(result, field, wait)
        phase = phase if isinstance(phase, dict) else {}
        phase['immediate_action'] = wait
        phase['watch_conditions'] = [wait]
        dashboard['phase_decision'] = phase

        def align(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: item if key in {
                        'decision_stability', 'decision_score_calibration',
                        'market_data_validation', 'stop_loss_validation',
                        'stop_loss', 'take_profit', 'risk_control', 'action_checklist',
                    } else align(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [align(item) for item in value]
            return wait if conflicting(value) else value

        dashboard = align(dashboard)
        result.dashboard = dashboard

    result.action = advice_action
    result.action_label = localize_action_label(advice_action, language)
    result.operation_advice = result.action_label
    if _BUCKET.get(advice_action) == 'hold':
        battle = dashboard.get('battle_plan')
        if isinstance(battle, dict):
            strategy = battle.get('position_strategy')
            if isinstance(strategy, dict):
                strategy['entry_plan'] = wait
                strategy['suggested_position'] = wait
            points = battle.get('sniper_points')
            if isinstance(points, dict):
                points.pop('ideal_buy', None)
                points.pop('secondary_buy', None)
    if advice_action != action_for_score(getattr(result, 'sentiment_score', None)):
        calibration = dashboard.get('decision_score_calibration')
        if not isinstance(calibration, dict):
            calibration = dashboard['decision_score_calibration'] = {}
        calibration['guardrail_reason'] = wait
        calibration['final_action'] = advice_action
    result.decision_type = _BUCKET.get(advice_action, 'hold')
    dashboard['action'] = result.action
    dashboard['operation_advice'] = result.operation_advice
    dashboard['decision_type'] = result.decision_type
    dashboard['sentiment_score'] = getattr(result, 'sentiment_score', None)
    dashboard['confidence_level'] = getattr(result, 'confidence_level', None)
    dashboard['analysis_summary'] = result.analysis_summary
    if stop_invalid:
        stop_warning = {
            'zh': '数据不一致：止损价与触发条件引用的价格不符或依据缺失；原始值已保留供核查，核实前不可执行。',
            'en': 'Data conflict: stop-loss and its price anchor disagree or lack evidence; original retained for review, do not execute.',
            'ko': '데이터 불일치: 손절가와 기준 가격이 다르거나 근거가 없습니다. 원문을 보존했으며 확인 전 실행하지 마세요.',
        }[language]
        # Suppress dependent executable triggers as well as the point itself.
        # Keep generic protective principles and preserve originals in validation.
        validation = dashboard['stop_loss_validation']
        claimed = validation.get('claimed_level')

        def invalid_trigger(value: Any) -> bool:
            if not isinstance(value, str) or not _STOP_TRIGGER.search(value):
                return False
            named = re.search(r'(?:今日|当日|今天).{0,3}(?:最低价?|低点)|today.{0,12}low|MA(?:5|10|20|60)(?!\d)', value, re.I)
            numbers = [finite_number(number) for number in re.findall(r'\d+(?:\.\d+)?', value)]
            return bool(named or (numbers and value in validation.get('trigger_text', [])) or (claimed is not None and any(
                number is not None and abs(number - claimed) <= .011 for number in numbers)))

        battle = dashboard.get('battle_plan') or {}
        strategy = battle.get('position_strategy') or {}
        if isinstance(strategy, dict) and invalid_trigger(strategy.get('risk_control')):
            strategy['risk_control'] = stop_warning
        checklist = battle.get('action_checklist')
        if isinstance(checklist, list):
            battle['action_checklist'] = [stop_warning if invalid_trigger(item) else item for item in checklist]
        for field in _NARRATIVES:
            setattr(result, field, _map_prose(getattr(result, field, None),
                                            lambda value, path: stop_warning if invalid_trigger(value) else value, field))
        dashboard = result.dashboard
        if stop_warning not in str(getattr(result, 'risk_warning', '') or ''):
            result.risk_warning = f'{getattr(result, "risk_warning", "") or ""}\n{stop_warning}'.strip()
    missing_dimensions = [key for key in ('capital_flow', 'institutional_views') if intel[key] == missing]
    if missing_dimensions:
        dimension_labels = {
            'zh': {'capital_flow': '资金流向', 'institutional_views': '机构观点'},
            'en': {'capital_flow': 'Capital flows', 'institutional_views': 'institutional views'},
            'ko': {'capital_flow': '자금 흐름', 'institutional_views': '기관 의견'},
        }[language]
        disclosure = ' / '.join(dimension_labels[key] for key in missing_dimensions) + ': ' + missing
        if disclosure not in str(result.risk_warning or ''):
            result.risk_warning = f'{result.risk_warning or ""}\n{disclosure}'.strip()
    previous_adjustments = (dashboard.get('report_consistency') or {}).get('adjustments', [])
    adjustments = sorted(set(adjustments).union(previous_adjustments))
    dashboard['report_consistency'] = {'adjustments': adjustments, 'checked': ['action', 'stop_loss', 'allocation', 'body'],
                                       'volume_evidence': volume_evidence}
    return sorted(set(adjustments))
