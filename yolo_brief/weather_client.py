"""Real-time Open-Meteo weather client for YOLO Bot (욜로봇).

[INV-02] Fail-Safe contract:
- Any failure (network / timeout / JSON error / non-integer or NaN values / missing fields /
  `error: true` / unknown `area_code`) returns `WeatherSnapshot(weather_class=UNKNOWN)` immediately.
- Expired cache entries are NEVER reused as a stale fallback; only a short negative cache (20s)
  is kept for failures so a flapping upstream is not hammered.
- Only fully successful responses are cached per area for `ttl_seconds` (900s).
- Air-quality failure degrades `pm_grade` to `"UNKNOWN"` (never `"GOOD"`).
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlencode
import urllib.request

from yolo_brief.models import AREA_NAMES, WeatherClass, WeatherSnapshot

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

AREA_COORDS: dict[str, tuple[float, float]] = {
    "1100000000": (37.5665, 126.9780),  # 서울·수도권
    "2600000000": (35.1796, 129.0756),  # 부산·경남
    "2700000000": (35.8714, 128.6014),  # 대구·경북
    "2900000000": (35.1595, 126.8526),  # 광주·호남
    "3000000000": (36.3504, 127.3845),  # 대전·충청
    "5000000000": (33.4996, 126.5312),  # 제주
    "5100000000": (37.8813, 127.7298),  # 강원
}
assert set(AREA_COORDS) == set(AREA_NAMES), "AREA_COORDS must cover every AREA_NAMES region"

_WMO_CLEAR = frozenset({0, 1, 2, 3})
_WMO_FOG = frozenset({45, 48})
_WMO_RAIN = frozenset({51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 97, 99})
_WMO_SNOW = frozenset({71, 73, 75, 77, 85, 86})

HEAT_FEELS_LIKE_C = 33.0
COLD_FEELS_LIKE_C = -10.0
POP_WINDOW_HOURS = 6

# Opener contract: `opener(url, timeout) -> bytes | str` (raises on failure).
Opener = Callable[[str, float], Any]


class WeatherFetchError(Exception):
    """Raised internally for any upstream/parse failure (never escapes `get_weather`)."""


def map_wmo_code(code: int) -> WeatherClass:
    if code in _WMO_CLEAR:
        return WeatherClass.CLEAR
    if code in _WMO_FOG:
        return WeatherClass.FOG
    if code in _WMO_RAIN:
        return WeatherClass.RAIN
    if code in _WMO_SNOW:
        return WeatherClass.SNOW
    return WeatherClass.UNKNOWN


def pm25_to_grade(pm25: float) -> str:
    """Korean PM2.5 grade (µg/m³): <=15 GOOD, <=35 NORMAL, <=75 BAD, else VERY_BAD."""
    if pm25 <= 15:
        return "GOOD"
    if pm25 <= 35:
        return "NORMAL"
    if pm25 <= 75:
        return "BAD"
    return "VERY_BAD"


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WeatherFetchError(f"{name} is not numeric: {value!r}")
    f = float(value)
    if not math.isfinite(f):
        raise WeatherFetchError(f"{name} is not finite: {value!r}")
    return f


def _integer(value: Any, name: str) -> int:
    f = _finite_number(value, name)
    if not f.is_integer():
        raise WeatherFetchError(f"{name} is not an integer: {value!r}")
    return int(f)


def _default_opener(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "yolo-bot-weather/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https URLs)
        return resp.read()


def unknown_snapshot(area_code: str) -> WeatherSnapshot:
    return WeatherSnapshot(area_code=str(area_code), weather_class=WeatherClass.UNKNOWN, pm_grade="UNKNOWN")


class OpenMeteoWeatherClient:
    """Per-area cached Open-Meteo forecast + air-quality client (no API key required)."""

    def __init__(
        self,
        opener: Optional[Opener] = None,
        ttl_seconds: float = 900.0,
        neg_ttl_seconds: float = 20.0,
        timeout: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._opener: Opener = opener or _default_opener
        self.ttl_seconds = float(ttl_seconds)
        self.neg_ttl_seconds = float(neg_ttl_seconds)
        self.timeout = float(timeout)
        self._clock = clock
        self._lock = threading.Lock()
        # area_code -> (expires_at, snapshot)
        self._cache: dict[str, tuple[float, WeatherSnapshot]] = {}

    # ------------------------------------------------------------------
    def get_weather(self, area_code: str) -> WeatherSnapshot:
        code = str(area_code or "").strip()
        if code not in AREA_COORDS:
            return unknown_snapshot(code)

        now = self._clock()
        with self._lock:
            cached = self._cache.get(code)
            if cached is not None:
                expires_at, snap = cached
                if now < expires_at:
                    return snap
                # [INV-02] expired -> drop it; never served as a stale fallback
                del self._cache[code]

        snap, ttl = self._fetch(code)
        with self._lock:
            self._cache[code] = (self._clock() + ttl, snap)
        return snap

    # ------------------------------------------------------------------
    def _get_json(self, base_url: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{base_url}?{urlencode(params)}"
        try:
            raw = self._opener(url, self.timeout)
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
        except WeatherFetchError:
            raise
        except Exception as exc:  # network, timeout, decode, JSON
            raise WeatherFetchError(f"request failed: {exc}") from exc
        if not isinstance(data, dict):
            raise WeatherFetchError("response is not a JSON object")
        if data.get("error") is True:
            raise WeatherFetchError(f"upstream error: {data.get('reason')!r}")
        return data

    def _fetch(self, code: str) -> tuple[WeatherSnapshot, float]:
        lat, lon = AREA_COORDS[code]
        try:
            forecast = self._get_json(
                FORECAST_URL,
                {
                    "latitude": lat,
                    "longitude": lon,
                    "current": "weather_code,apparent_temperature,precipitation,snowfall",
                    "hourly": "precipitation_probability",
                    "timezone": "Asia/Seoul",
                    "forecast_days": 2,
                },
            )
            fields = self._parse_forecast(forecast)
        except WeatherFetchError as exc:
            logger.warning("[YOLO weather] forecast failed for %s: %s", code, exc)
            return unknown_snapshot(code), self.neg_ttl_seconds

        pm_grade = "UNKNOWN"
        aq_ok = False
        try:
            aq = self._get_json(
                AIR_QUALITY_URL,
                {"latitude": lat, "longitude": lon, "current": "pm2_5", "timezone": "Asia/Seoul"},
            )
            current_aq = aq.get("current")
            if not isinstance(current_aq, dict):
                raise WeatherFetchError("air-quality `current` missing")
            pm_grade = pm25_to_grade(_finite_number(current_aq.get("pm2_5"), "pm2_5"))
            aq_ok = True
        except WeatherFetchError as exc:
            logger.warning("[YOLO weather] air-quality failed for %s: %s", code, exc)

        snap = WeatherSnapshot(area_code=code, pm_grade=pm_grade, **fields)
        # Partial success (no AQ) is only short-cached so the PM grade is retried soon.
        return snap, (self.ttl_seconds if aq_ok else self.neg_ttl_seconds)

    @staticmethod
    def _parse_forecast(data: dict[str, Any]) -> dict[str, Any]:
        current = data.get("current")
        if not isinstance(current, dict):
            raise WeatherFetchError("forecast `current` missing")
        wmo = _integer(current.get("weather_code"), "weather_code")
        feels = _finite_number(current.get("apparent_temperature"), "apparent_temperature")
        precip = _finite_number(current.get("precipitation"), "precipitation")
        snowfall_cm = _finite_number(current.get("snowfall"), "snowfall")
        if precip < 0 or snowfall_cm < 0:
            raise WeatherFetchError("negative precipitation/snowfall")

        hourly = data.get("hourly")
        if not isinstance(hourly, dict):
            raise WeatherFetchError("forecast `hourly` missing")
        times = hourly.get("time")
        probs = hourly.get("precipitation_probability")
        if not isinstance(times, list) or not isinstance(probs, list) or len(times) != len(probs):
            raise WeatherFetchError("hourly precipitation_probability malformed")
        cur_hour = str(current.get("time") or "")[:13]  # "YYYY-MM-DDTHH"
        start = next((i for i, t in enumerate(times) if str(t)[:13] == cur_hour), None)
        if start is None:
            raise WeatherFetchError("current hour not found in hourly series")
        window = [p for p in probs[start : start + POP_WINDOW_HOURS] if p is not None]
        if not window:
            raise WeatherFetchError("no precipitation_probability in window")
        pop = max(_integer(p, "precipitation_probability") for p in window)
        if not 0 <= pop <= 100:
            raise WeatherFetchError(f"precipitation_probability out of range: {pop}")

        weather_class = map_wmo_code(wmo)
        if weather_class == WeatherClass.CLEAR:
            if feels >= HEAT_FEELS_LIKE_C:
                weather_class = WeatherClass.HEAT
            elif feels <= COLD_FEELS_LIKE_C:
                weather_class = WeatherClass.COLD

        return {
            "weather_class": weather_class,
            "pop_percent": pop,
            "precip_mm": precip,
            "snow_mm": snowfall_cm * 10.0,
            "feels_like_c": feels,
        }


class StaticWeatherClient:
    """Fixed-snapshot client for `--demo` / tests (no network)."""

    def __init__(self, snapshot: WeatherSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[str] = []

    def get_weather(self, area_code: str) -> WeatherSnapshot:
        self.calls.append(str(area_code))
        from dataclasses import replace

        return replace(self.snapshot, area_code=str(area_code))


_DEFAULT_CLIENT: Optional[OpenMeteoWeatherClient] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_weather_client() -> OpenMeteoWeatherClient:
    """Process-wide shared client so per-update bot instances still share the 900s cache."""
    global _DEFAULT_CLIENT
    with _DEFAULT_LOCK:
        if _DEFAULT_CLIENT is None:
            _DEFAULT_CLIENT = OpenMeteoWeatherClient()
        return _DEFAULT_CLIENT
