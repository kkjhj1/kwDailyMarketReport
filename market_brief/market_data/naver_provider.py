"""Naver Finance public crawler & polling API adapter with fallback support."""
from dataclasses import dataclass
from datetime import datetime, date, timezone
from decimal import Decimal
import json
import re
import urllib.request
from zoneinfo import ZoneInfo
from .models import Quote, parse_decimal, DataValidationError

KST = ZoneInfo("Asia/Seoul")


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
        "SP500": "https://api.stock.naver.com/index/.INX/basic",
        "NASDAQ_COMPOSITE": "https://api.stock.naver.com/index/.IXIC/basic",
        "USDKRW": "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=exchange&reutersCode=FX_USDKRW",
        "KR3Y": "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=bond&reutersCode=KR3YT=RR",
        "KR10Y": "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=bond&reutersCode=KR10YT=RR",
        "US10Y": "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=bond&reutersCode=US10YT=X",
        "US2Y": "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=bond&reutersCode=US2YT=X",
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
        raw_prev = (
            body.get("compareToPreviousClosePrice")
            or body.get("prevClose")
            or body.get("previousClose")
        )
        value = self._clean_num(raw_price)

        # If `previousClose` is explicitly given, use it; if `compareToPreviousClosePrice` (diff) is given, compute previous_close
        if "previousClose" in body or "prevClose" in body:
            prev_val = self._clean_num(body.get("previousClose") or body.get("prevClose"))
        elif "compareToPreviousClosePrice" in body and body.get("compareToPreviousClosePrice") is not None:
            diff = self._clean_num(body["compareToPreviousClosePrice"])
            # Check direction sign if provided
            direction_info = body.get("compareToPreviousPrice") or {}
            code = str(direction_info.get("code", ""))
            # Naver code '5' or '4' means down
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
    """Tries primary provider (e.g. NaverFinanceProvider), falls back cleanly on error."""

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback
        self.last_used_source = "NONE"

    def fetch(self, symbols: list[str]) -> list[Quote]:
        try:
            result = self.primary.fetch(symbols)
            self.last_used_source = "PRIMARY"
            return result
        except Exception:
            result = self.fallback.fetch(symbols)
            self.last_used_source = "FALLBACK"
            return result
