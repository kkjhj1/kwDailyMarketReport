"""Fetch boundary: no messaging, LLM, or report side effects."""
import json
import urllib.parse
import urllib.request
from datetime import datetime, date, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from .models import Quote, from_record, parse_decimal, DataValidationError

class MarketDataProvider(Protocol):
    def fetch(self, symbols: list[str]) -> list[Quote]: ...

class FixtureProvider:
    def __init__(self, file_path: str):
        self.path = Path(file_path)

    def fetch(self, symbols: list[str]) -> list[Quote]:
        raw = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(raw, list):
            raise DataValidationError('fixture must contain a list')
        items = [from_record(item) for item in raw]
        requested = set(symbols)
        found = {q.symbol for q in items}
        missing = requested - found
        if missing:
            raise DataValidationError('missing symbols: '+','.join(sorted(missing)))
        return [q for q in items if q.symbol in requested]


class FredProvider:
    """FRED daily US Treasury reference observations. NOT real-time prices.

    Supports a small explicitly mapped set of FRED daily yield series;
    uses latest two *published* observations; never equates publication with live time.
    """
    SERIES = {'US2Y': 'DGS2', 'US10Y': 'DGS10'}
    def __init__(self, api_key: str, *, opener=None):
        if not api_key:
            raise ValueError('FRED_API_KEY required')
        self._api_key = api_key
        self._opener = opener or urllib.request.urlopen

    def fetch(self, symbols: list[str]) -> list[Quote]:
        output = []
        for symbol in symbols:
            if symbol not in self.SERIES:
                raise DataValidationError(f'unsupported FRED series {symbol}')
            params = urllib.parse.urlencode({'series_id':self.SERIES[symbol], 'api_key':self._api_key,
                                             'file_type':'json', 'sort_order':'desc', 'limit':12})
            url = 'https://api.stlouisfed.org/fred/series/observations?' + params
            try:
                with self._opener(urllib.request.Request(url, headers={'User-Agent':'market-brief/0.2'}),timeout=10) as response:
                    data = json.load(response)
            except (OSError, ValueError) as exc:
                raise DataValidationError(f'FRED unavailable for {symbol}') from exc
            if not isinstance(data,dict) or not isinstance(data.get('observations'),list):
                raise DataValidationError('unexpected FRED payload')
            valid = [o for o in data['observations'] if isinstance(o,dict) and o.get('value') not in (None, '.')]
            if len(valid) < 2:
                raise DataValidationError(f'insufficient FRED observations {symbol}')
            # Endpoint requests descending observations: guard against unexpected order.
            valid.sort(key=lambda x: x['date'], reverse=True)
            latest, previous = valid[:2]
            trading_day = date.fromisoformat(latest['date'])
            output.append(Quote(symbol=symbol, asset_class='US_BOND', market='US',
                                session='REFERENCE', trade_date=trading_day,
                                as_of=datetime.combine(trading_day,time(23,59),tzinfo=timezone.utc),
                                value=parse_decimal(latest['value']), previous_close=parse_decimal(previous['value']),
                                unit='PERCENT_YIELD', source='FRED:'+self.SERIES[symbol],
                                is_final=True))
        return output


class LicensedFeedProvider:
    """Adapter for vendor-normalized REST feed (requires signed enterprise contract).

    Contract: GET /quotes?symbols=... returns {'quotes':[normalized Quote records]}.
    This is an integration boundary, not a claimed KRX/Massive native endpoint.
    """
    def __init__(self, base_url: str, token: str, *, opener=None):
        if not base_url.startswith('https://'):
            raise ValueError('HTTPS feed URL required')
        if not token:
            raise ValueError('feed bearer token required')
        self.base_url = base_url.rstrip('/')
        self.token = token
        self._opener = opener or urllib.request.urlopen

    def fetch(self, symbols: list[str]) -> list[Quote]:
        if not symbols or any(not x.isascii() or not x.replace('_','').replace('.','').isalnum() for x in symbols):
            raise DataValidationError('invalid symbols')
        url = self.base_url+'/quotes?'+urllib.parse.urlencode({'symbols':','.join(symbols)})
        req = urllib.request.Request(url,headers={'Authorization':'Bearer '+self.token,
                                                  'Accept':'application/json'})
        try:
            with self._opener(req,timeout=10) as res:
                data=json.load(res)
        except (OSError,ValueError) as exc:
            raise DataValidationError('licensed feed unavailable') from exc
        if not isinstance(data,dict) or not isinstance(data.get('quotes'),list):
            raise DataValidationError('invalid licensed feed schema')
        observations=[from_record(x) for x in data['quotes']]
        symbols_found={x.symbol for x in observations}
        if not set(symbols).issubset(symbols_found):
            raise DataValidationError('licensed feed missing symbols')
        return [x for x in observations if x.symbol in symbols]
