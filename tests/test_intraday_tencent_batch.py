"""Tencent watchlist batching must preserve the repaired units and fallback."""

from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from data_provider.akshare_fetcher import AkshareFetcher
from data_provider.base import DataFetcherManager
from data_provider.realtime_types import RealtimeSource
from src.core.intraday_signals import CN, quote_issues


CODES = ['002273', '600111', '600487', '002624', '601398', '159206']
SYMBOLS = ['sz002273', 'sh600111', 'sh600487', 'sz002624', 'sh601398', 'sz159206']


def tencent_row(code, symbol, *, volume='20000000', amount='205000000', timestamp='20261016103000'):
    fields = [''] * 50
    fields[1:7] = ['测试', code, '10.50', '10.00', '10.00', volume]
    fields[30:35] = [timestamp, '0.50', '5.00', '10.60', '9.90']
    fields[35] = f'10.25/{volume}/{amount}'
    fields[38] = '2.00'
    fields[43:46] = ['7.00', '100.00', '200.00']
    fields[49] = '2.20'
    return f'v_{symbol}="' + '~'.join(fields) + '";'


def batch_fetch(monkeypatch, rows):
    fetcher = AkshareFetcher()
    monkeypatch.setattr(fetcher, '_enforce_rate_limit', lambda: None)
    response = SimpleNamespace(status_code=200, text=''.join(rows), raise_for_status=lambda: None)
    getter = MagicMock(return_value=response)
    monkeypatch.setattr('data_provider.akshare_fetcher.requests.get', getter)
    quotes = fetcher.get_tencent_quotes_batch(CODES)
    return fetcher, quotes, getter


def test_six_quotes_one_request_same_units_as_single_parser(monkeypatch):
    _, quotes, getter = batch_fetch(monkeypatch, [
        tencent_row(code, symbol) for code, symbol in zip(CODES, SYMBOLS)
    ])
    assert getter.call_count == 1
    assert getter.call_args.args[0].endswith(','.join(SYMBOLS))
    assert set(quotes) == set(CODES)
    for code, item in quotes.items():
        assert item.code == code
        assert item.volume == 20_000_000  # Tencent field 6 is already shares here.
        assert item.amount == 205_000_000  # The precise triple is yuan.
        assert item.provider_timestamp == '2026-10-16T10:30:00+08:00'
        item.fetched_at = '2026-10-16T10:30:00+08:00'
        assert not quote_issues(item.to_dict(), datetime(2026, 10, 16, 10, 30, tzinfo=CN))


def test_missing_and_wrong_symbol_rows_are_not_fabricated(monkeypatch):
    _, quotes, _ = batch_fetch(monkeypatch, [
        tencent_row('002273', 'sz002273'),
        tencent_row('000001', 'sh600111'),  # mismatched body for requested symbol
    ])
    assert list(quotes) == ['002273']


def test_manager_reuses_batch_and_falls_back_only_for_missing_symbol(monkeypatch):
    fetcher, one_quote, _ = batch_fetch(monkeypatch, [tencent_row('002273', 'sz002273')])
    fallback = MagicMock(return_value=None)
    monkeypatch.setattr(fetcher, 'get_realtime_quote', fallback)
    config = SimpleNamespace(enable_realtime_quote=True, prefetch_realtime_quotes=True,
                             realtime_source_priority='tencent', realtime_cache_ttl=90)
    with patch('src.config.get_config', return_value=config):
        manager = DataFetcherManager(fetchers=[fetcher])
        monkeypatch.setattr(fetcher, 'get_tencent_quotes_batch', MagicMock(return_value=one_quote))
        assert manager.prefetch_realtime_quotes(CODES, force_refresh=True) == 1
        assert manager.get_realtime_quote('002273', quote_acceptor=lambda quote: True) is one_quote['002273']
        fallback.assert_not_called()
        manager.get_realtime_quote('600111', quote_acceptor=lambda quote: True)
        fallback.assert_called_once_with('600111', source='tencent')


def test_default_daily_prefetch_does_not_use_tencent_batch(monkeypatch):
    fetcher = SimpleNamespace(name='AkshareFetcher', priority=1,
                              get_tencent_quotes_batch=MagicMock(return_value={}),
                              get_realtime_quote=MagicMock())
    config = SimpleNamespace(enable_realtime_quote=True, prefetch_realtime_quotes=True,
                             realtime_source_priority='tencent')
    with patch('src.config.get_config', return_value=config):
        manager = DataFetcherManager(fetchers=[fetcher])
        assert manager.prefetch_realtime_quotes(CODES) == 0
    fetcher.get_tencent_quotes_batch.assert_not_called()


def test_old_batch_row_falls_through_to_fresh_source(monkeypatch):
    fetcher, rows, _ = batch_fetch(monkeypatch, [tencent_row('002273', 'sz002273')])
    fresh = deepcopy(rows['002273'])
    fresh.provider_timestamp = '2026-10-16T10:31:00+08:00'
    fresh.source = RealtimeSource.EFINANCE
    backup = SimpleNamespace(name='EfinanceFetcher', priority=2,
                             get_realtime_quote=MagicMock(return_value=fresh))
    config = SimpleNamespace(enable_realtime_quote=True, prefetch_realtime_quotes=True,
                             realtime_source_priority='tencent,efinance', realtime_cache_ttl=90)
    with patch('src.config.get_config', return_value=config):
        manager = DataFetcherManager(fetchers=[fetcher, backup])
        monkeypatch.setattr(fetcher, 'get_tencent_quotes_batch', MagicMock(return_value=rows))
        manager.prefetch_realtime_quotes(CODES, force_refresh=True)
        result = manager.get_realtime_quote(
            '002273', quote_acceptor=lambda quote: quote.provider_timestamp == fresh.provider_timestamp
        )
    assert result is fresh
    assert result.fallback_from == 'tencent'
