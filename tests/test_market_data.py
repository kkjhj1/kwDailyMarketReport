import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from market_brief.market_data import (FixtureProvider, FredProvider, LicensedFeedProvider,
    QuoteStore, validate_batch, validate_urgent_quote, DataValidationError)
from market_brief.market_data.models import from_record

ROOT=Path(__file__).resolve().parents[1]

class FakeResponse(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self,*_): self.close()

class MarketDataTests(unittest.TestCase):
    def setUp(self):
        self.quotes=FixtureProvider(str(ROOT/'fixtures/market_snapshot.json')).fetch(['KOSPI','KR3Y','SP500'])

    def test_normalization_yield_bp_and_index_pct(self):
        by={q.symbol:q for q in self.quotes}
        self.assertEqual(by['KR3Y'].change_bp,Decimal('-3.000'))
        self.assertIsNone(by['KR3Y'].change_pct)
        self.assertTrue(by['KOSPI'].change_pct < 0)

    def test_missing_required_fails_closed(self):
        with self.assertRaises(DataValidationError):
            FixtureProvider(str(ROOT/'fixtures/market_snapshot.json')).fetch(['NO_SUCH_SYMBOL'])

    def test_duplicate_and_future_and_stale(self):
        q=self.quotes[0]
        now=datetime(2026,10,13,6,35,tzinfo=timezone.utc)
        with self.assertRaisesRegex(DataValidationError,'duplicate'):
            validate_batch([q,q],{'KOSPI'},now=now)
        with self.assertRaisesRegex(DataValidationError,'stale'):
            validate_batch([q],{'KOSPI'},now=now+timedelta(days=1),max_age=timedelta(hours=1))
        with self.assertRaisesRegex(DataValidationError,'future'):
            validate_batch([q],{'KOSPI'},now=now-timedelta(hours=2))

    def test_corrupt_prices_and_naive_timestamp_rejected(self):
        r=self.quotes[0].to_record()
        r['value']='NaN'
        with self.assertRaises(DataValidationError): from_record(r)
        r['value']='10'
        r['as_of']='2026-10-13T15:30:00'
        with self.assertRaises(DataValidationError): from_record(r)

    def test_urgent_freshness_and_market_open_gate(self):
        q=self.quotes[0]
        now=datetime(2026,10,13,6,31,tzinfo=timezone.utc)
        with self.assertRaisesRegex(DataValidationError,'outside'):
            validate_urgent_quote(q,now=now,market_open=False)
        change=validate_urgent_quote(q,now=now,market_open=True)
        self.assertTrue(change < 0)
        with self.assertRaisesRegex(DataValidationError,'stale'):
            validate_urgent_quote(q,now=now+timedelta(minutes=5),market_open=True)
        with self.assertRaisesRegex(DataValidationError,'symbol not configured'):
            validate_urgent_quote(self.quotes[1],now=now,market_open=True)

    def test_sqlite_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=QuoteStore(str(Path(tmp)/'q.db'))
            self.assertEqual(store.upsert(self.quotes),3)
            store.upsert(self.quotes)
            self.assertEqual(store.count(),3)

    def test_fred_mocked_api_no_live_claim(self):
        captured=[]
        def opener(req,timeout):
            captured.append(req.full_url)
            return FakeResponse(json.dumps({'observations':[
                {'date':'2026-10-09','value':'4.07'},
                {'date':'2026-10-12','value':'4.12'},
                {'date':'2026-10-13','value':'.'}]}).encode())
        q=FredProvider('test-key',opener=opener).fetch(['US10Y'])[0]
        self.assertEqual(q.value,Decimal('4.12'))
        self.assertEqual(q.change_bp,Decimal('5.00'))
        self.assertEqual(q.session,'REFERENCE')
        self.assertEqual(q.source,'FRED:DGS10')
        self.assertIn('DGS10',captured[0])
        self.assertTrue(q.is_final)

    def test_fred_missing_observations_rejected(self):
        def opener(req,timeout): return FakeResponse(b'{"observations":[{"date":"2026-10-12","value":"."}]}')
        with self.assertRaises(DataValidationError):
            FredProvider('test-key',opener=opener).fetch(['US2Y'])

    def test_licensed_feed_mocked(self):
        record=self.quotes[0].to_record()
        def opener(req,timeout):
            self.assertEqual(req.get_header('Authorization'),'Bearer opaque-test-token')
            return FakeResponse(json.dumps({'quotes':[record]}).encode())
        q=LicensedFeedProvider('https://feed.example.test','opaque-test-token',opener=opener).fetch(['KOSPI'])
        self.assertEqual(q[0].symbol,'KOSPI')
        with self.assertRaises(ValueError): LicensedFeedProvider('http://invalid','test')

    def test_licensed_feed_missing_symbol_fails(self):
        def opener(req,timeout): return FakeResponse(b'{"quotes":[]}')
        with self.assertRaises(DataValidationError):
            LicensedFeedProvider('https://feed.example.test','test',opener=opener).fetch(['SP500'])

if __name__=='__main__': unittest.main()
