"""Final, deterministic evidence and action checks before reports are saved."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from src.report_language import localize_confidence_level, normalize_report_language
from src.schemas.decision_action import localize_action_label, normalize_decision_action
from src.schemas.decision_scale import action_for_score


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
        return value

    # Traverse all displayed narratives, including nested checklist/strategy text.
    for field in (
        'dashboard', 'analysis_summary', 'key_points', 'risk_warning', 'buy_reason',
        'trend_analysis', 'short_term_outlook', 'medium_term_outlook', 'technical_analysis',
        'ma_analysis', 'volume_analysis', 'pattern_analysis', 'fundamental_analysis',
        'sector_position', 'company_highlights', 'news_summary', 'market_sentiment', 'hot_topics', 'data_sources',
    ):
        setattr(result, field, clean(getattr(result, field, None), field))

    # Mandatory disclosure even if the model left these fields empty.
    for field in ('news_summary', 'market_sentiment', 'hot_topics'):
        if not getattr(result, field, None):
            setattr(result, field, missing)
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
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
    reason = str(getattr(result, 'buy_reason', '') or '').strip()
    has_reason = bool(reason and reason.lower() not in {'n/a', 'none', 'null', '-', '--', '无', '未知'}
                      and not re.search(r'数据缺失|数据不足|无法判断|待补充|暂无|insufficient|unavailable|no data|unknown',
                                        reason, flags=re.I))
    has_basis = bool(
        not context.get('data_missing') and getattr(result, 'success', True)
        and (_positive(realtime.get('price')) or _positive(daily.get('close')))
        and all(_positive(daily.get(key)) for key in ('ma5', 'ma10', 'ma20'))
        and has_reason
    )
    advice_action = normalize_decision_action(result.operation_advice)
    direction = _BUCKET.get(advice_action)
    conflict = advice_action is None
    explicit = normalize_decision_action(getattr(result, 'action', None))
    if explicit and _BUCKET[explicit] != direction:
        conflict = True
    if result.decision_type in ('buy', 'sell') and result.decision_type != direction:
        conflict = True

    # A conditional entry plan must never silently override a Watch conclusion.
    core = dashboard.get('core_conclusion') or {}
    battle = dashboard.get('battle_plan') or {}
    phase = dashboard.get('phase_decision') or {}
    plans = [core, battle.get('position_strategy', {}) if isinstance(battle, dict) else {}, phase,
             result.analysis_summary, result.buy_reason, result.key_points]

    def conflicting(value: Any) -> bool:
        if isinstance(value, dict):
            return any(conflicting(item) for item in value.values())
        if isinstance(value, list):
            return any(conflicting(item) for item in value)
        if not isinstance(value, str) or value == missing:
            return False
        for clause in re.split(r'[，,。；;：:\n]|同时|但|而|\band\b|\bbut\b', value, flags=re.I):
            action = normalize_decision_action(clause)
            if action and _BUCKET[action] != direction:
                return True
        return False

    conflict = conflict or any(conflicting(plan) for plan in plans)
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
        dashboard['core_conclusion'] = {
            'one_sentence': wait, 'signal_type': localize_action_label('watch', language),
            'position_advice': {'no_position': wait, 'has_position': wait},
        }
        dashboard['battle_plan'] = {
            'sniper_points': {},
            'position_strategy': {'suggested_position': wait, 'entry_plan': wait, 'risk_control': wait},
            'action_checklist': [wait],
        }
        # Eliminate stray contradictory instructions from other narrative fields.
        for field in ('short_term_outlook', 'medium_term_outlook', 'risk_warning', 'trend_analysis',
                      'technical_analysis', 'ma_analysis', 'volume_analysis', 'pattern_analysis',
                      'fundamental_analysis', 'sector_position', 'company_highlights'):
            if conflicting(getattr(result, field, '')):
                setattr(result, field, wait)
        for key in ('decision_stability', 'decision_score_calibration', 'strategy_synthesis'):
            dashboard.pop(key, None)
        phase = phase if isinstance(phase, dict) else {}
        phase['immediate_action'] = wait
        phase['watch_conditions'] = [wait]
        dashboard['phase_decision'] = phase

        def align(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: align(item) for key, item in value.items()}
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
    if advice_action != action_for_score(result.sentiment_score):
        calibration = dashboard.get('decision_score_calibration')
        if not isinstance(calibration, dict):
            calibration = dashboard['decision_score_calibration'] = {}
        calibration['guardrail_reason'] = wait
        calibration['final_action'] = advice_action
    result.decision_type = _BUCKET.get(advice_action, 'hold')
    dashboard['action'] = result.action
    dashboard['operation_advice'] = result.operation_advice
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
    dashboard['report_consistency'] = {'adjustments': sorted(set(adjustments))}
    return sorted(set(adjustments))
