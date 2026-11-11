"""Real-time KRW exchange-rate client (Naver m.stock front-api) for travel briefing FX timing signals.

- Spot : productDetail  -> result.closePrice ("1,339.40"), fluctuationsRatio, fullName, localTradedAt
- 3M   : pricesByPeriod -> result.priceInfos[].closePrice (float) averaged
- Unit invariant: JPY/VND/IDR quote per 100 units (quote_unit=100), others per 1 unit; spot, avg_3m and
  diff_vs_3m_pct are always computed on the same quote unit.
- Fail-Open: a failed currency is simply omitted; callers must render the briefing without it.
The formatter never calls this module; the search worker pre-fetches and injects `fx_map`.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

SPOT_URL = "https://m.stock.naver.com/front-api/marketIndex/productDetail?category=exchange&reutersCode={code}"
CHART_URL = (
    "https://m.stock.naver.com/front-api/chart/pricesByPeriod?reutersCode={code}"
    "&category=exchange&chartInfoType=marketindex&scriptChartType=areaMonthThree"
)
FX_TTL_SECONDS = 3600.0
FX_FAIL_TTL_SECONDS = 60.0
FX_HTTP_TIMEOUT_SECONDS = 2.0
FX_MAX_PARALLEL_CURRENCIES = 3
GREEN_THRESHOLD_PCT = -1.5
ORANGE_THRESHOLD_PCT = 1.5

HUNDRED_UNIT_CURRENCIES = frozenset({"JPY", "VND", "IDR"})

CURRENCY_UNIT_NAMES: dict[str, str] = {
    "JPY": "엔", "VND": "동", "IDR": "루피아", "USD": "달러", "THB": "바트", "TWD": "대만달러",
    "SGD": "싱가포르달러", "PHP": "페소", "MYR": "링깃", "HKD": "홍콩달러", "MOP": "파타카",
    "CNY": "위안", "EUR": "유로", "GBP": "파운드", "AUD": "호주달러", "MNT": "투그릭",
}

SIGNAL_LABELS: dict[str, str] = {
    "GREEN": "🟢 환율 꿀구간",
    "YELLOW": "🟡 3개월 평균권",
    "ORANGE": "🟠 환율 강세",
}

_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def quote_unit_for(currency: str) -> int:
    return 100 if (currency or "").upper() in HUNDRED_UNIT_CURRENCIES else 1


def classify_signal(diff_vs_3m_pct: float) -> str:
    d = round(float(diff_vs_3m_pct), 2)
    if d <= GREEN_THRESHOLD_PCT:
        return "GREEN"
    if d >= ORANGE_THRESHOLD_PCT:
        return "ORANGE"
    return "YELLOW"


def _to_float(raw: Any) -> Optional[float]:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
    else:
        try:
            val = float(str(raw).replace(",", "").strip())
        except ValueError:
            return None
    return val if val > 0 else None


@dataclass(frozen=True)
class FXQuote:
    currency: str
    quote_unit: int
    spot: float                       # KRW per `quote_unit` units
    avg_3m: Optional[float] = None    # KRW per `quote_unit` units (same basis as spot)
    diff_vs_3m_pct: Optional[float] = None
    signal: Optional[str] = None      # GREEN | YELLOW | ORANGE (None when the 3M chart is unavailable)
    change_ratio_pct: Optional[float] = None
    full_name: str = ""
    traded_at: str = ""

    @property
    def signal_label(self) -> str:
        return SIGNAL_LABELS.get(self.signal or "", "")

    @property
    def unit_name(self) -> str:
        return CURRENCY_UNIT_NAMES.get(self.currency, self.currency)

    @property
    def per_10k_krw(self) -> Optional[float]:
        """KRW per 10,000 units (VND/IDR 보조표기)."""
        return self.spot * (10000 / self.quote_unit) if self.currency in ("VND", "IDR") else None


def parse_spot_payload(currency: str, payload: Any) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    spot = _to_float(result.get("closePrice"))
    if spot is None:
        return None
    return {
        "spot": spot,
        "change_ratio_pct": _to_float_signed(result.get("fluctuationsRatio")),
        "full_name": str(result.get("fullName") or ""),
        "traded_at": str(result.get("localTradedAt") or ""),
    }


def _to_float_signed(raw: Any) -> Optional[float]:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except ValueError:
        return None


def parse_chart_average(payload: Any) -> Optional[float]:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    infos = result.get("priceInfos") if isinstance(result, dict) else None
    if not isinstance(infos, list):
        return None
    values = [v for v in (_to_float(i.get("closePrice")) for i in infos if isinstance(i, dict)) if v is not None]
    if not values:
        return None
    return sum(values) / len(values)


def build_quote(currency: str, spot_info: dict, avg_3m: Optional[float]) -> FXQuote:
    cur = currency.upper()
    spot = float(spot_info["spot"])
    diff = signal = None
    if avg_3m and avg_3m > 0:
        diff = round((spot / avg_3m - 1.0) * 100.0, 2)
        signal = classify_signal(diff)
    return FXQuote(
        currency=cur,
        quote_unit=quote_unit_for(cur),
        spot=spot,
        avg_3m=avg_3m,
        diff_vs_3m_pct=diff,
        signal=signal,
        change_ratio_pct=spot_info.get("change_ratio_pct"),
        full_name=spot_info.get("full_name", ""),
        traded_at=spot_info.get("traded_at", ""),
    )


class FXClient:
    """Thread-safe, memory-TTL cached FX fetcher. Never raises to callers."""

    def __init__(
        self,
        opener: Optional[Callable[..., Any]] = None,
        ttl_seconds: float = FX_TTL_SECONDS,
        timeout: float = FX_HTTP_TIMEOUT_SECONDS,
        max_parallel: int = FX_MAX_PARALLEL_CURRENCIES,
    ):
        self._opener = opener or urllib.request.urlopen
        self.ttl_seconds = ttl_seconds
        self.timeout = timeout
        self.max_parallel = max(1, int(max_parallel))
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, FXQuote]] = {}
        self._fail: dict[str, float] = {}

    def _get_json(self, url: str) -> Any:
        req = urllib.request.Request(url, headers={"User-Agent": _MOBILE_UA, "Accept": "application/json"})
        with self._opener(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))

    def _fetch_uncached(self, currency: str) -> Optional[FXQuote]:
        code = urllib.parse.quote(f"FX_{currency}KRW")
        spot_info = parse_spot_payload(currency, self._get_json(SPOT_URL.format(code=code)))
        if spot_info is None:
            return None
        avg_3m = None
        try:
            avg_3m = parse_chart_average(self._get_json(CHART_URL.format(code=code)))
        except Exception as exc:
            logger.info("FX 3M chart unavailable for %s: %s", currency, exc)
        return build_quote(currency, spot_info, avg_3m)

    def _cached(self, currency: str) -> tuple[bool, Optional[FXQuote]]:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(currency)
            if hit and (now - hit[0]) <= self.ttl_seconds:
                return True, hit[1]
            failed_at = self._fail.get(currency)
            if failed_at is not None and (now - failed_at) <= FX_FAIL_TTL_SECONDS:
                return True, None
        return False, None

    def get(self, currency: str) -> Optional[FXQuote]:
        return self.get_many([currency]).get((currency or "").upper())

    def get_many(self, currencies: Iterable[str]) -> dict[str, FXQuote]:
        """Fetches up to `max_parallel` unique currencies in parallel; failed ones are omitted (Fail-Open)."""
        uniq: list[str] = []
        for c in currencies or []:
            cur = (c or "").strip().upper()
            if cur and cur != "KRW" and cur.isalpha() and len(cur) == 3 and cur not in uniq:
                uniq.append(cur)
        uniq = uniq[: self.max_parallel]

        out: dict[str, FXQuote] = {}
        pending: list[str] = []
        for cur in uniq:
            known, quote = self._cached(cur)
            if known:
                if quote is not None:
                    out[cur] = quote
            else:
                pending.append(cur)
        if not pending:
            return out

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(pending), thread_name_prefix="TravelFX")
        try:
            fut_map = {pool.submit(self._fetch_uncached, cur): cur for cur in pending}
            # Spot + chart run sequentially per currency, each bounded by `timeout`.
            done, _ = concurrent.futures.wait(fut_map, timeout=self.timeout * 2 + 0.5)
            for fut, cur in fut_map.items():
                quote = None
                if fut in done:
                    try:
                        quote = fut.result()
                    except Exception as exc:
                        logger.info("FX fetch failed for %s: %s", cur, exc)
                with self._lock:
                    if quote is not None:
                        self._cache[cur] = (time.monotonic(), quote)
                        self._fail.pop(cur, None)
                    else:
                        self._fail[cur] = time.monotonic()
                if quote is not None:
                    out[cur] = quote
        finally:
            pool.shutdown(wait=False)
        return out
