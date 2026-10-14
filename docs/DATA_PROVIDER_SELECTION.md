# Step 2: Data-provider decision record (2026-10-13)

Status: **shortlist only**. No contract, credentials, entitlement, or live access has been established.

| Dataset | Preferred commercial/data option | Alternative | Mandatory gate |
|---|---|---|---|
| KOSPI/KOSDAQ live ±3% | KRX licensed real-time distributor/feed | Approved domestic vendor | License for internal redistribution, latency, index exact codes, 15:40 availability |
| KR index closing 15:40 | Approved fast official close / vendor | KRX contracted real-time close | KRX EOD feed may first distribute at **16:00**, too late for 15:40 |
| US S&P500 / NASDAQ Composite live ±3% | Massive business-licensed indices plan (verify symbol + business license) | Other enterprise indices feed | Real-time entitlement, exact *composite* vs NASDAQ-100, exchange index redistribution |
| UST 2Y/10Y daily | FRED DGS2/DGS10 | Treasury / commercial rates feed | Reference rates only, not instant or live; as-of freshness |
| KR gov yields 3Y/10Y | Approved bond information provider | Bank of Korea statistical feed for reference | Quote vs evaluated yield, timestamp, 15:40 eligibility |
| FX, gold, oil, REIT | Contracted multi-asset feed | Authorized regional feeds | Each asset instrument / timeliness / redistribution |
| News and catalysts | Licensed finance news API | Press releases / primary releases | Reuse and summary rights, attribution, cross-check |

## Implementation now

- `Quote` is the provider-neutral schema; both `FixtureProvider` and `FredProvider` implement `fetch(symbols)`.
- `LicensedFeedProvider` requires a **customer-built normalized adapter** for the chosen vendor, exposed at a company-owned HTTPS endpoint. It is NOT a native KRX, Massive, or Twelve Data endpoint and should not be presented as such.
- For urgent events, accept only `REGULAR` index quotes with `delayed_minutes=0`, a confirmed market-open flag, a prior close, and a source timestamp at most 2 minutes old. Live session calendars + streaming are **not implemented** in v0.2.
- Morning/closing reporting still needs an exchange-aware snapshot policy: daily FRED series must not be treated as live or guaranteed current.
- Store all source timestamps, market trade dates, and quote provenance.

## Vendor due diligence before purchase

1. Does the plan cover corporate use and **internal staff redistribution** in a report/chat link?
2. Index naming: SP500 vs Nasdaq Composite (not Nasdaq 100). Exact tickers and endpoints?
3. Sample timestamps / outlier handling / corporate market holidays / DST?
4. Intraday real-time latency, rate caps, reliability and 15:30 to 15:40 data SLA?
5. KR gov yields and FX: official reference vs executable quote? Can these be shown together?
6. API key custody, outbound traffic restrictions, logging restrictions, regional hosting, audit rights?
7. Can cached data and derived summaries be persisted and viewed by employees?

## Sources checked
- https://openapi.krx.co.kr/contents/OPP/DATA/OPPDATA002.jsp
- https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO003.jsp
- https://massive.com/indices
- https://fred.stlouisfed.org/docs/api/fred/
