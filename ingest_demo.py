"""Offline end-to-end market-data ingestion example; no message sending."""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from market_brief.market_data import FixtureProvider, QuoteStore, validate_batch

root=Path(__file__).resolve().parent
symbols=['KOSPI','KOSDAQ','KR3Y','SP500','NASDAQ_COMPOSITE','US10Y','USDKRW']
quotes=FixtureProvider(str(root/'fixtures/market_snapshot.json')).fetch(symbols)
validate_batch(quotes,set(symbols),now=datetime(2026,10,13,15,40,tzinfo=ZoneInfo('Asia/Seoul')))
store=QuoteStore(str(root/'market_quotes_dev.sqlite3'))
store.upsert(quotes)
for q in quotes:
    movement=(f'{q.change_bp:+.1f} bp' if q.unit=='PERCENT_YIELD' else f'{q.change_pct:+.2f}%')
    print(f'{q.asset_class:13} {q.symbol:18} {q.value:10} {movement:>12}  {q.source}')
print('stored unique snapshots:',store.count())
