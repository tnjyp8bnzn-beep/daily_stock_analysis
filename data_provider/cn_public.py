"""Bounded, key-free CN disclosures and data fallbacks; never infer missing values.

Disclosure dates are independent of report periods and acquisition times. Public
aggregators are labelled mirrors, never presented as exchange-certified data.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import StringIO
import json
import re
from threading import RLock
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from src.market_data_quality import finite_number

_CN = ZoneInfo('Asia/Shanghai')


class PublicCNData:
    def __init__(self, timeout: float = 3.0):
        self.timeout = timeout
        self._cache: dict = {}
        self._lock = RLock()

    def _request(self, url, *, params=None, data=None):
        kwargs = {'timeout': (self.timeout, self.timeout), 'headers': {
            'User-Agent': 'Mozilla/5.0', 'Referer': url.split('/api/')[0] + '/'}}
        if 'feed.mix.sina.com.cn' in url:
            kwargs['headers']['Referer'] = 'https://finance.sina.com.cn/'
        response = requests.post(url, data=data, **kwargs) if data is not None else requests.get(url, params=params, **kwargs)
        response.raise_for_status()
        return response

    def _cached(self, key, fetch, ttl=300):
        with self._lock:
            item = self._cache.get(key)
            if item and monotonic() - item[0] < ttl:
                return item[1]
        value = fetch()
        if not isinstance(value, dict) or value.get('status') not in ('missing', 'failed'):
            with self._lock:
                self._cache[key] = (monotonic(), value)
        return value

    @staticmethod
    def _block(data, source, url, *, errors=None, reason=None, kind='historical_fact'):
        return {'status': 'ok' if data else 'missing', 'data': data, 'source': source, 'url': url,
                'fetched_at': datetime.now(_CN).isoformat(), 'kind': kind,
                'errors': errors or [], 'missing_reason': reason if not data else None}

    @staticmethod
    def _day(value):
        try:
            stamp = pd.Timestamp(value)
            return None if pd.isna(stamp) else stamp.date()
        except (ValueError, TypeError):
            return None

    def financials(self, code, as_of=None):
        now = as_of or datetime.now(_CN)
        url = 'https://datacenter.eastmoney.com/securities/api/data/v1/get'
        suffix = 'SH' if code.startswith(('6', '9')) else 'BJ' if code.startswith(('4', '8')) else 'SZ'
        params = {'reportName': 'RPT_F10_FINANCE_MAINFINADATA', 'columns': 'ALL',
                  'filter': f'(SECUCODE="{code}.{suffix}")', 'sortColumns': 'REPORT_DATE',
                  'sortTypes': '-1', 'pageSize': 8, 'source': 'HSF10', 'client': 'PC'}
        try:
            rows = self._request(url, params=params).json().get('result') or {}
            rows = rows.get('data') or []
        except (requests.RequestException, ValueError, AttributeError) as exc:
            return self._sina_financials(code, now, ['eastmoney:' + type(exc).__name__])
        eligible = [r for r in rows if isinstance(r, dict) and r.get('SECURITY_CODE') == code
                    and self._day(r.get('REPORT_DATE')) and self._day(r.get('NOTICE_DATE'))
                    and (not self._day(r.get('UPDATE_DATE')) or self._day(r['UPDATE_DATE']) <= now.date())
                    and self._day(r['REPORT_DATE']) <= now.date() and self._day(r['NOTICE_DATE']) <= now.date()]
        if not eligible:
            return self._sina_financials(code, now, ['eastmoney:no_dated_financial_record'])
        row = max(eligible, key=lambda r: r['REPORT_DATE'])
        aliases = {'revenue': 'TOTALOPERATEREVE', 'net_profit_parent': 'PARENTNETPROFIT',
                   'operating_cash_flow': 'NETCASH_OPERATE_PK', 'roe': 'ROEJQ',
                   'revenue_yoy': 'TOTALOPERATEREVETZ', 'net_profit_yoy': 'PARENTNETPROFITTZ'}
        values = {key: finite_number(row.get(raw)) for key, raw in aliases.items()}
        if not any(value is not None for value in values.values()):
            return self._sina_financials(code, now, ['eastmoney:financial_fields_missing'])
        values.update(report_date=self._day(row['REPORT_DATE']).isoformat(),
                      published_date=self._day(row['NOTICE_DATE']).isoformat(),
                      updated_date=str(row.get('UPDATE_DATE') or ''), currency='CNY', amount_unit='元',
                      source='eastmoney_disclosure_mirror', url='https://data.eastmoney.com/bbsj/' + code + '.html')
        return self._block(values, 'eastmoney_disclosure_mirror', url)

    def _sina_financials(self, code, now, errors):
        from .fundamental_adapter import _financial_abstract_row
        from .akshare_fetcher import _to_sina_tx_symbol
        url = 'https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022'
        try:
            payload = self._request(url, params={'paperCode': _to_sina_tx_symbol(code), 'source': 'gjzb',
                                   'type': 0, 'page': 1, 'num': 4}).json()
            rows = payload['result']['data']['report_list']
            eligible = [(period, row) for period, row in rows.items() if self._day(period)
                        and self._day(row.get('publish_date')) and self._day(period) <= now.date()
                        and self._day(row['publish_date']) <= now.date()]
            if not eligible:
                return self._block({}, 'sina_disclosure_mirror', url, errors=errors, reason='no_dated_financial_record')
            period, metadata = max(eligible, key=lambda pair: pair[0])
            frame = pd.DataFrame({'指标': [r.get('item_title') for r in metadata['data']],
                                  period: [r.get('item_value') for r in metadata['data']]})
            row = _financial_abstract_row(frame)
            aliases = {'revenue': '营业总收入', 'net_profit_parent': '归母净利润',
                       'operating_cash_flow': '经营活动产生的现金流量净额', 'roe': '净资产收益率'}
            data = {key: finite_number(row.get(raw)) for key, raw in aliases.items()}
            if not any(value is not None for value in data.values()):
                return self._block({}, 'sina_disclosure_mirror', url, errors=errors, reason='financial_fields_missing')
            data.update(report_date=self._day(period).isoformat(), published_date=self._day(metadata['publish_date']).isoformat(),
                        currency=metadata.get('rCurrency'), amount_unit='元', source='sina_disclosure_mirror', url=url)
            return self._block(data, 'sina_disclosure_mirror', url, errors=errors)
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
            return self._block({}, 'sina_disclosure_mirror', url, errors=errors+['sina:' + type(exc).__name__], reason='fetch_failed')

    def announcements(self, code, as_of=None):
        now = as_of or datetime.now(_CN)
        official = 'https://www.cninfo.com.cn/new/hisAnnouncement/query'
        errors, records = [], []
        try:
            metadata = self._cached('cninfo_codes', lambda: self._request(
                'https://www.cninfo.com.cn/new/data/szse_stock.json').json(), ttl=86400)
            org = next((r['orgId'] for r in metadata.get('stockList', []) if r.get('code') == code), None)
            if org:
                payload = self._request(official, data={'stock': f'{code},{org}', 'tabName': 'fulltext',
                    'column': 'sse' if code.startswith('6') else 'szse', 'pageSize': 20, 'pageNum': 1,
                    'seDate': f'{(now-timedelta(days=365)).date()}~{now.date()}'}).json()
                for row in payload.get('announcements') or []:
                    stamp = pd.to_datetime(row.get('announcementTime'), unit='ms', utc=True, errors='coerce')
                    path = str(row.get('adjunctUrl') or '')
                    if (str(row.get('secCode')) == code and not pd.isna(stamp)
                            and stamp.to_pydatetime() <= now and path.startswith('finalpage/')):
                        records.append({'title': re.sub('<[^>]*>', '', row.get('announcementTitle') or ''),
                            'published_date': stamp.tz_convert(_CN).date().isoformat(),
                            'url': 'https://static.cninfo.com.cn/' + path, 'source': 'cninfo_official', 'code': code})
            else:
                errors.append('cninfo_security_identity_unavailable')
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
            errors.append('cninfo:' + type(exc).__name__)
        if records:
            return self._block(records, 'cninfo_official', official, errors=errors, kind='issuer_disclosure')
        mirror = 'https://np-anotice-stock.eastmoney.com/api/security/ann'
        try:
            rows = (self._request(mirror, params={'sr': -1, 'page_size': 20, 'page_index': 1,
                     'ann_type': 'A', 'stock_list': code}).json().get('data') or {}).get('list') or []
            for row in rows:
                day = self._day(row.get('notice_date'))
                if day and day <= now.date() and any(str(r.get('stock_code')) == code for r in row.get('codes', [])):
                    article = str(row.get('art_code') or '')
                    if re.fullmatch(r'AN\d+', article):
                        records.append({'title': row.get('title'), 'published_date': day.isoformat(),
                            'url': f'https://data.eastmoney.com/notices/detail/{code}/{article}.html',
                            'source': 'issuer_disclosure_mirror', 'code': code})
        except (requests.RequestException, ValueError, TypeError) as exc:
            errors.append('disclosure_mirror:' + type(exc).__name__)
        return self._block(records, 'issuer_disclosure_mirror', mirror, errors=errors,
                           reason='no_verified_disclosure_in_returned_window', kind='issuer_disclosure')

    def news(self, code, name='', *, as_of=None, days=3, _keyword=None):
        now = as_of or datetime.now(_CN)
        url = 'https://search-api-web.eastmoney.com/search/jsonp'
        if code == 'market' and _keyword is None:
            url = 'https://feed.mix.sina.com.cn/api/roll/get'
            payload = self._request(url, params={'pageid': 153, 'lid': 2516, 'num': 30, 'page': 1}).json()
            rows = (payload.get('result') or {}).get('data') or []
        else:
            param = {'uid': '', 'keyword': _keyword or code, 'type': ['cmsArticleWebOld'], 'client': 'web',
                     'clientType': 'web', 'clientVersion': 'curr', 'param': {'cmsArticleWebOld': {
                     'searchScope': 'default', 'sort': 'time', 'pageIndex': 1, 'pageSize': 30,
                     'preTag': '', 'postTag': ''}}}
            raw = self._request(url, params={'cb': 'callback', 'param': json.dumps(param)}).text
            payload = json.loads(raw[raw.find('(')+1:raw.rfind(')')])
            rows = (payload.get('result') or {}).get('cmsArticleWebOld') or []
        records = []
        for row in rows:
            title = re.sub('<[^>]*>', '', str(row.get('title') or ''))
            published = row.get('date') or row.get('ctime')
            try:
                stamp = pd.to_datetime(int(published), unit='s', utc=True).tz_convert(_CN) if str(published).isdigit() else pd.Timestamp(published)
                if stamp.tzinfo is None:
                    stamp = stamp.tz_localize(_CN)
            except (ValueError, TypeError, OverflowError):
                continue
            link = str(row.get('url') or '')
            if (pd.isna(stamp) or not now-timedelta(days=days) <= stamp.to_pydatetime() <= now
                    or not link.startswith(('https://', 'http://'))):
                continue
            if code != 'market' and code not in title and (not name or name not in title):
                continue
            records.append({'title': title, 'published_date': stamp.isoformat(), 'url': link,
                            'source': row.get('mediaName') or row.get('media_name') or (
                                'sina_public_news' if 'sina.com.cn' in url else 'eastmoney_public_news')})
        if not records and code != 'market' and name and _keyword is None:
            return self.news(code, name, as_of=now, days=days, _keyword=name)
        return self._block(records[:8], 'public_news', url, reason='no_recent_relevant_dated_news', kind='reported_news')

    def capital_flow(self, code, as_of=None):
        now = as_of or datetime.now(_CN)
        url = 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'
        try:
            payload = self._request(url, params={'lmt': 10, 'klt': 101,
                'secid': ('1.' if code.startswith('6') else '0.') + code,
                'fields1': 'f1,f2,f3,f7', 'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63'}).json()
        except requests.RequestException as exc:
            return self._block({}, 'eastmoney_fund_flow', url,
                               errors=[type(exc).__name__], reason='fetch_failed')
        data = payload.get('data') or {}
        rows = [str(row).split(',') for row in data.get('klines') or []]
        rows = [r for r in rows if len(r) >= 2 and self._day(r[0]) and self._day(r[0]) <= now.date()]
        if str(data.get('code')) != code or not rows:
            return self._block({}, 'eastmoney_fund_flow', url, reason='no_dated_capital_flow')
        row = max(rows, key=lambda r: r[0])
        net = finite_number(row[1])
        if net is None:
            return self._block({}, 'eastmoney_fund_flow', url, reason='invalid_capital_flow')
        return self._block({'stock_flow': {'main_net_inflow': net, 'trade_date': row[0], 'unit': '元',
                           'definition': '供应商按订单大小分类的主力净额，非机构账户真实交易记录'}}, 'eastmoney_fund_flow', url)

    def market_stats(self, as_of=None):
        from bs4 import BeautifulSoup
        now = as_of or datetime.now(_CN)
        url = 'https://legulegu.com/stockdata/market-activity'
        text = self._request(url).text
        soup = BeautifulSoup(text, 'lxml')
        stamp = soup.select_one('.market-activity-meta')
        day = self._day(stamp.get_text(strip=True)) if stamp else None
        # Never use undated or previous-session breadth as today's sentiment.
        if day != now.date():
            return {}
        frame = pd.read_html(StringIO(text))[0]
        values = {}
        for _, row in frame.iterrows():
            cells = list(row)
            for i in range(0, len(cells)-1, 2):
                values[str(cells[i]).strip()] = finite_number(cells[i+1])
        aliases = {'up_count': '上涨', 'down_count': '下跌', 'flat_count': '平盘',
                   'limit_up_count': '涨停', 'limit_down_count': '跌停'}
        result = {key: int(values[label]) if values.get(label) is not None and values[label] >= 0
                  and float(values[label]).is_integer() else None for key, label in aliases.items()}
        if any(result[k] is None for k in aliases):
            return {}
        result.update(source='legulegu_public', trade_date=day.isoformat(),
                      provider_timestamp=stamp.get_text(strip=True), fetched_at=now.isoformat(), url=url)
        return result

    def collect(self, code, name='', *, as_of=None, is_etf=False):
        now = as_of or datetime.now(_CN)
        operations = {'announcements': lambda: self.announcements(code, now),
                      'news': lambda: self.news(code, name, as_of=now),
                      'capital_flow': lambda: self.capital_flow(code, now)}
        if not is_etf:
            operations.update(financials=lambda: self.financials(code, now))
        def run(item):
            key, operation = item
            try:
                # No previous-session reuse. Preserve each block's original fetch time.
                return key, self._cached((key, code, name, now.date().isoformat(),
                                          now.hour, now.minute // 5), operation)
            except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
                return key, self._block({}, 'public_cn_fallback', '', errors=[type(exc).__name__], reason='fetch_failed')
        with ThreadPoolExecutor(max_workers=4) as pool:
            result = dict(pool.map(run, operations.items()))
        if is_etf:
            for key in ('financials',):
                result[key] = self._block({}, 'asset_capability', '', reason='ETF不适用上市公司财报')
                result[key]['status'] = 'not_supported'
        return result


_client = PublicCNData()


def get_public_cn_data():
    return _client


def merge_verified_context(context: dict, verified: dict) -> dict:
    """Fill field gaps only; keep original blocks/errors and the fetched evidence."""
    from copy import deepcopy
    result = deepcopy(context)
    result['verified_data'] = verified
    announcements = (verified.get('announcements') or {}).get('data') or []
    for key, pattern in (('forecast_disclosures', r'业绩预[告增减]'),
                         ('quick_report_disclosures', r'业绩快报'),
                         ('dividend_disclosures', r'分红|分派|派息')):
        records = [row for row in announcements if re.search(pattern, row.get('title') or '')]
        if records:
            result.setdefault('earnings', {}).setdefault('data', {})[key] = records
    fin = verified.get('financials') or {}
    if fin.get('status') == 'ok' and isinstance(fin.get('data'), dict):
        block = result.setdefault('earnings', {})
        data = block.setdefault('data', {})
        report = data.setdefault('financial_report', {})
        incoming = fin['data']
        # Keep old facts for audit; never combine amounts across report periods.
        if report.get('report_date') and incoming.get('report_date', '') > report['report_date']:
            data.setdefault('historical_financial_reports', []).append(deepcopy(report))
            report = data['financial_report'] = {}
        if not report.get('report_date') or report.get('report_date') == incoming.get('report_date'):
            for key, value in incoming.items():
                if report.get(key) is None and value is not None:
                    report[key] = value
            block.update(status='partial', source=fin['source'])
            block.setdefault('source_chain', []).append({'provider': fin['source'], 'result': 'ok', 'url': fin['url']})
    flow = verified.get('capital_flow') or {}
    if flow.get('status') == 'ok' and isinstance(flow.get('data'), dict):
        block = result.setdefault('capital_flow', {})
        data = block.setdefault('data', {})
        current = data.get('stock_flow') or {}
        if not current or not current.get('trade_date'):
            data['stock_flow'] = flow['data']['stock_flow']
            block.update(status='partial', source=flow['source'], fetched_at=flow['fetched_at'], url=flow['url'])
            block.setdefault('source_chain', []).append({'provider': flow['source'], 'result': 'ok', 'url': flow['url']})
    result.setdefault('coverage', {}).update({key: (result.get(key) or {}).get('status', 'missing')
                                             for key in ('earnings', 'capital_flow')})
    return result


def evidence_text(verified: dict) -> str:
    lines = []
    for key in ('news', 'announcements'):
        block = verified.get(key) or {}
        for row in block.get('data') or []:
            lines.append(f"{row['published_date']} [{row['source']}] {row['title']} {row['url']}")
    return '\n'.join(lines)
