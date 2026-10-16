"""Read existing ledgers and optional broker snapshots without inventing fills."""

import json
from pathlib import Path

from src.market_data_quality import finite_number
from data_provider.base import normalize_stock_code
from src.services.stock_list_parser import ParseStatus, parse_analysis_target


def cn_stock_code(value):
    """Normalize ledger identities without converting exchange indices to stocks."""
    raw = str(value or '')
    if parse_analysis_target(raw).asset_type != ParseStatus.STOCK:
        return None
    code = normalize_stock_code(raw)
    return code if len(code) == 6 and code.isdigit() else None


def portfolio_context(service, snapshot_path=''):
    """Snapshots are evidence, not opening buys; unresolved reconciliation is explicit."""
    ledger = service.get_portfolio_snapshot(include_realtime=False, read_only=True)
    positions = []
    for account in ledger['accounts']:
        for position in account.get('positions', []):
            if position.get('market') == 'cn' and position.get('quantity', 0) > 0:
                code = cn_stock_code(position['symbol'])
                if code is None:
                    continue
                positions.append({**position, 'code': code,
                                  'account': account['account_name'],
                                  'source': 'trade_ledger', 'available_quantity': None})
    evidence = {}
    if snapshot_path:
        evidence = json.loads(Path(snapshot_path).read_text(encoding='utf-8'))
        if evidence.get('schema_version') != 1 or not isinstance(evidence.get('positions'), list):
            raise ValueError('invalid_broker_snapshot')
    # A full ledger and a screenshot cannot simply be added together.
    if positions and evidence.get('positions'):
        return {'positions': positions, 'status': 'reconciliation_required',
                'reason': '台账与截图需对账，未叠加；可卖数量待券商确认',
                'broker_evidence': evidence, 'trades': service.list_trade_events(page_size=20)}
    if not positions and evidence.get('positions'):
        for row in evidence['positions']:
            if row.get('asset_type', 'stock') not in ('stock', 'etf'):
                continue
            if not isinstance(row.get('code'), str) or len(row['code']) != 6:
                raise ValueError('invalid_broker_symbol')
            quantity = finite_number(row.get('quantity'))
            if quantity is None or quantity < 0:
                raise ValueError('invalid_broker_quantity')
        positions = [{**row, 'source': 'broker_snapshot'} for row in evidence['positions']
                     if row.get('asset_type', 'stock') in ('stock', 'etf')]
        return {'positions': positions, 'status': 'snapshot_unreconciled',
                'reason': '截图时点/成交对账未确认，仅显示持仓证据，不代表当前可卖数量',
                'broker_evidence': evidence, 'trades': evidence.get('executions', [])}
    return {'positions': positions, 'status': 'ledger' if ledger['account_count'] else 'unknown',
            'reason': '券商可卖数量未同步；提醒不代表成交',
            'trades': service.list_trade_events(page_size=20)}


def position_advice(context, code, signal):
    rows = [row for row in context['positions'] if row['code'] == code]
    quantity = sum(row['quantity'] for row in rows)
    if signal == 'exit_risk':
        advice = '持仓风险预警，核对券商可卖数量后决定是否减仓' if quantity else '无已核实持仓，勿把风险预警当成卖出指令'
    else:
        advice = '已有持仓，勿重复建仓；仓位与风险预算待确认' if quantity else '买入候选，资金与风险预算未确认，不生成下单数量'
    return {'quantity': quantity, 'accounts': rows, 'status': context['status'],
            'available_quantity': None, 'advice': advice}
