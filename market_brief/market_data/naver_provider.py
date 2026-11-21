"""Naver Finance public crawler & polling API adapter with fallback support."""
from dataclasses import dataclass
from datetime import datetime, date, timezone
from decimal import Decimal
import json
import logging
import re
import urllib.request
from zoneinfo import ZoneInfo
from .models import Quote, parse_decimal, DataValidationError

KST = ZoneInfo("Asia/Seoul")
logger = logging.getLogger("naver_provider")


@dataclass(frozen=True)
class NewsItem:
    title: str
    summary: str
    source: str
    url: str
    published_at: str
    asset_class: str = "KR_EQUITY"


class NaverFinanceProvider:
    """Fetches 5-asset-class indicators & news from Naver Finance APIs/HTML with safe parsing.

    Supports dependency injection (`opener`) so unit tests and offline environments
    can verify parsing without hitting live network endpoints.
    """

    # Symbol metadata mapping for 5 asset classes
    SYMBOL_META = {
        "KOSPI": ("KR_EQUITY", "KR", "REGULAR", "INDEX", "NAVER:KOSPI"),
        "KOSDAQ": ("KR_EQUITY", "KR", "REGULAR", "INDEX", "NAVER:KOSDAQ"),
        "KOSPI200": ("KR_EQUITY", "KR", "REGULAR", "INDEX", "NAVER:KOSPI200"),
        "KR3Y": ("KR_BOND", "KR", "REFERENCE", "PERCENT_YIELD", "NAVER:KR3Y"),
        "KR10Y": ("KR_BOND", "KR", "REFERENCE", "PERCENT_YIELD", "NAVER:KR10Y"),
        "SP500": ("US_EQUITY", "US", "REGULAR", "INDEX", "NAVER:SP500"),
        "NASDAQ_COMPOSITE": ("US_EQUITY", "US", "REGULAR", "INDEX", "NAVER:NASDAQ"),
        "US10Y": ("US_BOND", "US", "REFERENCE", "PERCENT_YIELD", "NAVER:US10Y"),
        "US2Y": ("US_BOND", "US", "REFERENCE", "PERCENT_YIELD", "NAVER:US2Y"),
        "USDKRW": ("ALTERNATIVE", "GLOBAL", "REFERENCE", "FX", "NAVER:USDKRW"),
        "WTI": ("ALTERNATIVE", "GLOBAL", "REFERENCE", "PRICE", "NAVER:WTI"),
        "GOLD": ("ALTERNATIVE", "GLOBAL", "REFERENCE", "PRICE", "NAVER:GOLD"),
    }

    API_ENDPOINTS = {
        "KOSPI": "https://m.stock.naver.com/api/index/KOSPI/basic",
        "KOSDAQ": "https://m.stock.naver.com/api/index/KOSDAQ/basic",
        "KOSPI200": "https://m.stock.naver.com/api/index/KPI200/basic",
        "SP500": "https://api.stock.naver.com/index/.INX/basic",
        "NASDAQ_COMPOSITE": "https://api.stock.naver.com/index/.IXIC/basic",
        "USDKRW": "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=exchange&reutersCode=FX_USDKRW",
        "KR3Y": "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=bond&reutersCode=KR3YT=RR",
        "KR10Y": "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=bond&reutersCode=KR10YT=RR",
        "US10Y": "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=bond&reutersCode=US10YT=RR",
        "US2Y": "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=bond&reutersCode=US2YT=RR",
        "WTI": "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=energy&reutersCode=CLcv1",
        "GOLD": "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=metals&reutersCode=GCcv1",
    }

    def __init__(self, *, opener=None, timeout: int = 8):
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout

    @staticmethod
    def _clean_num(val) -> Decimal:
        if val is None:
            raise DataValidationError("missing numeric field in Naver payload")
        cleaned = str(val).replace(",", "").strip()
        return parse_decimal(cleaned)

    def _parse_payload(self, symbol: str, data: dict, now: datetime) -> Quote:
        if symbol not in self.SYMBOL_META:
            raise DataValidationError(f"Unsupported Naver symbol: {symbol}")
        asset_class, market, session, unit, source = self.SYMBOL_META[symbol]

        # Support both direct mobile index payload and wrapped `result` marketIndex payload
        body = data.get("result") if isinstance(data.get("result"), dict) else data
        if not isinstance(body, dict):
            raise DataValidationError(f"Malformed Naver payload for {symbol}")

        raw_price = (
            body.get("closePrice")
            or body.get("nowVal")
            or body.get("calcPrice")
            or body.get("value")
        )
        value = self._clean_num(raw_price)

        # 1. Fluctuations field with sign (+ / -) in mobile marketIndex productDetail
        if "fluctuations" in body and body.get("fluctuations") is not None:
            diff = self._clean_num(body["fluctuations"])
            prev_val = value - diff
        # 2. Explicit previousClose
        elif "previousClose" in body or "prevClose" in body:
            prev_val = self._clean_num(body.get("previousClose") or body.get("prevClose"))
        # 3. compareToPreviousClosePrice with direction code
        elif "compareToPreviousClosePrice" in body and body.get("compareToPreviousClosePrice") is not None:
            diff = self._clean_num(body["compareToPreviousClosePrice"])
            direction_info = body.get("compareToPreviousPrice") or {}
            code = str(direction_info.get("code", ""))
            if code in {"4", "5"} and diff > 0:
                diff = -diff
            prev_val = value - diff
        else:
            raise DataValidationError(f"Missing previous close reference for {symbol}")

        as_of = now.astimezone(KST)
        if raw_ts := body.get("localTradedAt") or body.get("tradedAt"):
            try:
                parsed_ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                if parsed_ts.tzinfo is not None:
                    as_of = parsed_ts
            except ValueError:
                pass

        return Quote(
            symbol=symbol,
            asset_class=asset_class,
            market=market,
            session=session,
            trade_date=as_of.date(),
            as_of=as_of,
            value=value,
            previous_close=prev_val,
            unit=unit,
            source=source,
            delayed_minutes=0,
            is_final=bool(body.get("isFinal", False)),
        )

    def fetch(self, symbols: list[str], *, now: datetime | None = None) -> list[Quote]:
        ref_now = now or datetime.now(KST)
        quotes: list[Quote] = []
        for sym in symbols:
            if sym not in self.API_ENDPOINTS:
                raise DataValidationError(f"Unsupported symbol for NaverFinanceProvider: {sym}")
            url = self.API_ENDPOINTS[sym]
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; MarketBriefBot/0.3)",
                    "Accept": "application/json",
                },
            )
            try:
                with self._opener(req, timeout=self._timeout) as resp:
                    payload = json.load(resp)
            except (OSError, ValueError) as exc:
                raise DataValidationError(f"Naver endpoint failed for {sym}: {exc}") from exc
            quotes.append(self._parse_payload(sym, payload, ref_now))
        return quotes


class CompositeMarketDataProvider:
    """Tries primary provider per symbol, falls back cleanly on error for isolated resilience."""

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback
        self.last_used_source = "NONE"

    def fetch(self, symbols: list[str]) -> list[Quote]:
        quotes_by_sym: dict[str, Quote] = {}
        # Fetch each symbol from primary with per-symbol error isolation
        for s in symbols:
            try:
                q_list = self.primary.fetch([s])
                if q_list:
                    quotes_by_sym[s] = q_list[0]
            except Exception:
                pass

        # Fill any missing symbols from fallback
        missing = [s for s in symbols if s not in quotes_by_sym]
        if missing:
            try:
                fb_list = self.fallback.fetch(missing)
                for q in fb_list:
                    quotes_by_sym[q.symbol] = q
            except Exception:
                pass

        self.last_used_source = "FALLBACK" if len(quotes_by_sym) == len(missing) else ("COMPOSITE" if missing else "PRIMARY")
        return [quotes_by_sym[s] for s in symbols if s in quotes_by_sym]


_NAVER_JSON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)",
    "Accept": "application/json",
}
_TREND_VALUE_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^[+-]?\d+(?:\.\d+)?$")
_MAX_SECTOR_NAME_LEN = 20


def _get_json(url: str, opener, timeout: int):
    _opener = opener or urllib.request.urlopen
    req = urllib.request.Request(url, headers=_NAVER_JSON_HEADERS)
    with _opener(req, timeout=timeout) as resp:
        return json.load(resp)


def _format_trend_value(raw) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if s.endswith("억"):
        s = s[:-1].strip()
    if not s or not _TREND_VALUE_RE.match(s):
        return None
    if s[0] not in "+-":
        s = "+" + s if s.replace(",", "").strip("0.") else s
    return f"{s}억"


def fetch_naver_investor_trend(
    timeout: int = 5, *, opener=None, trade_date: date | None = None
) -> "InvestorTrend | None":
    """Fetches KOSPI investor (개인/외국인/기관) net-buy trend. Fail-safe: returns None on any error."""
    from market_brief.core import InvestorTrend

    try:
        data = _get_json("https://m.stock.naver.com/api/index/KOSPI/trend", opener, timeout)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0]
        if not isinstance(data, dict):
            return None
        required = ("bizdate", "personalValue", "foreignValue", "institutionalValue")
        if any(k not in data for k in required):
            return None
        bizdate = re.sub(r"\D", "", str(data["bizdate"]))
        if len(bizdate) != 8:
            return None
        if trade_date is not None and bizdate != trade_date.strftime("%Y%m%d"):
            logger.debug("Naver investor trend bizdate %s != target %s", bizdate, trade_date)
            return None
        personal = _format_trend_value(data["personalValue"])
        foreign = _format_trend_value(data["foreignValue"])
        institutional = _format_trend_value(data["institutionalValue"])
        if personal is None or foreign is None or institutional is None:
            return None
        return InvestorTrend(personal=personal, foreign=foreign, institutional=institutional, bizdate=bizdate)
    except Exception as exc:
        logger.debug("Naver investor trend fetch failed: %s", exc)
        return None


def fetch_naver_leading_sectors(
    limit: int = 3, timeout: int = 5, *, opener=None
) -> "list[LeadingSector]":
    """Fetches top rising industry groups (changeRate > 0, max 3). Fail-safe: returns [] on any error."""
    from market_brief.core import LeadingSector

    limit = max(0, min(int(limit), 3))
    if limit == 0:
        return []
    try:
        data = _get_json("https://m.stock.naver.com/api/stocks/industry", opener, timeout)
        groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(groups, list):
            return []
        ranked: list[tuple[Decimal, str]] = []
        for g in groups:
            if not isinstance(g, dict):
                continue
            name = str(g.get("name") or "").strip()
            raw_rate = g.get("changeRate")
            if not name or raw_rate is None:
                continue
            try:
                rate = Decimal(str(raw_rate).replace(",", "").replace("%", "").strip())
            except Exception:
                continue
            if not rate.is_finite() or rate <= 0:
                continue
            if len(name) > _MAX_SECTOR_NAME_LEN:
                name = name[: _MAX_SECTOR_NAME_LEN - 1] + "…"
            ranked.append((rate, name))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [LeadingSector(name=n, change_rate=f"+{float(r):.2f}%") for r, n in ranked[:limit]]
    except Exception as exc:
        logger.debug("Naver leading sectors fetch failed: %s", exc)
        return []


def fetch_naver_market_news(limit: int = 5, opener=None, timeout: int = 6) -> list[NewsItem]:
    """Fetches live market headline news from Naver mobile stock API with direct article links."""
    _opener = opener or urllib.request.urlopen
    url = f"https://m.stock.naver.com/api/news/list?pageSize={limit}&page=1&category=mainnews"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)",
            "Accept": "application/json",
        },
    )
    items: list[NewsItem] = []
    try:
        with _opener(req, timeout=timeout) as resp:
            data = json.load(resp)
        if isinstance(data, list):
            for raw in data:
                oid = raw.get("oid", "")
                aid = raw.get("aid", "")
                tit = raw.get("tit", "").strip()
                sub = raw.get("subcontent", "").strip()
                src = raw.get("ohnm", "네이버금융").strip()
                dt_str = raw.get("dt", "")
                if not tit or not oid or not aid:
                    continue
                direct_url = f"https://n.news.naver.com/mnews/article/{oid}/{aid}"
                items.append(
                    NewsItem(
                        title=tit,
                        summary=sub,
                        source=src,
                        url=direct_url,
                        published_at=dt_str,
                        asset_class="KR_EQUITY",
                    )
                )
    except Exception:
        pass
    return items
