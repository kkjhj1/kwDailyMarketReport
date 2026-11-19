"""Weekend weather for Kids Compass via Open-Meteo (keyless, stdlib only).

Fact-safety invariants:
- A failed / unparseable fetch returns `is_failed=True` snapshots — NEVER a fabricated '맑음'.
- Open-Meteo forecast has no air-quality data: `is_bad_dust` is always False and the summary says
  미세먼지 정보 미제공 — it must never be rendered as '미세먼지 좋음'.
- Failed snapshots are not cached, so the next call retries.
"""
from __future__ import annotations
import json
import urllib.request
from datetime import date, datetime, timedelta
from typing import Callable, Optional
from urllib.parse import urlencode

from kids_compass.models import WeatherSnapshot

API_URL = "https://api.open-meteo.com/v1/forecast"
PROVIDER = "open-meteo"
TIMEOUT_SEC = 5.0
CACHE_TTL = timedelta(hours=3)
TS_FMT = "%Y-%m-%d %H:%M:%S"

# Representative points (city hall) per supported 시·도
REGION_COORDS: dict[str, tuple[float, float]] = {
    "서울": (37.5665, 126.9780),
    "경기": (37.2636, 127.0286),  # 수원
    "인천": (37.4563, 126.7052),
}

RAIN_PROB_THRESHOLD = 60   # %
RAIN_SUM_THRESHOLD = 1.0   # mm
# WMO weather codes: drizzle/rain/freezing 51-67, snow 71-77, showers 80-86, thunderstorm 95-99
WET_CODES = set(range(51, 68)) | set(range(71, 78)) | set(range(80, 87)) | set(range(95, 100))

DUST_NOTE = "미세먼지 정보 미제공"
FAILED_TEXT = "날씨 정보를 불러오지 못했어요 (기상청 예보를 확인해 주세요)"
WEEKDAY_KO = "월화수목금토일"

Opener = Callable[[str, float], bytes]


def _default_opener(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "kids-compass-weather/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https host)
        return resp.read()


def weekend_dates(today: date) -> list[date]:
    """Upcoming (or current) weekend: Mon~Sat -> [Sat, Sun]; Sunday -> [Sun]."""
    if today.weekday() == 6:
        return [today]
    sat = today + timedelta(days=(5 - today.weekday()) % 7)
    return [sat, sat + timedelta(days=1)]


def build_url(region_sido: str, start: date, end: date) -> str:
    lat, lon = REGION_COORDS[region_sido]
    query = urlencode({
        "latitude": lat, "longitude": lon,
        "daily": "precipitation_probability_max,precipitation_sum,weather_code",
        "timezone": "Asia/Seoul",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
    })
    return f"{API_URL}?{query}"


def _label(d: date) -> str:
    return f"{d.month}/{d.day}({WEEKDAY_KO[d.weekday()]})"


def _failed(region_sido: str, d: date, now: datetime) -> WeatherSnapshot:
    stamp = now.strftime(TS_FMT)
    return WeatherSnapshot(region_sido=region_sido, target_date=d.isoformat(), is_rain_or_snow=False,
                           is_bad_dust=False, summary_text=f"{_label(d)} {FAILED_TEXT}", provider=PROVIDER,
                           fetched_at=stamp, expires_at=stamp, is_failed=True)


def _num(values: list, i: int) -> Optional[float]:
    try:
        v = values[i]
        return None if v is None else float(v)
    except (IndexError, TypeError, ValueError):
        return None


def parse_daily(payload: dict, region_sido: str, targets: list[date], now: datetime) -> list[WeatherSnapshot]:
    """Map Open-Meteo `daily` arrays to snapshots. Any day missing all signals -> is_failed."""
    daily = payload.get("daily") if isinstance(payload, dict) else None
    if not isinstance(daily, dict):
        return [_failed(region_sido, d, now) for d in targets]
    times = [str(t) for t in daily.get("time") or []]
    probs = daily.get("precipitation_probability_max") or []
    sums = daily.get("precipitation_sum") or []
    codes = daily.get("weather_code") or []
    out = []
    for d in targets:
        iso = d.isoformat()
        if iso not in times:
            out.append(_failed(region_sido, d, now))
            continue
        i = times.index(iso)
        prob, total, code = _num(probs, i), _num(sums, i), _num(codes, i)
        if prob is None and total is None and code is None:
            out.append(_failed(region_sido, d, now))
            continue
        wet = ((prob is not None and prob >= RAIN_PROB_THRESHOLD)
               or (total is not None and total >= RAIN_SUM_THRESHOLD)
               or (code is not None and int(code) in WET_CODES))
        parts = [_label(d)]
        if prob is not None:
            parts.append(f"강수확률 {prob:.0f}%")
        parts.append("비/눈 예보 → 실내 위주" if wet else "비 소식 적음")
        out.append(WeatherSnapshot(
            region_sido=region_sido, target_date=iso, is_rain_or_snow=wet, is_bad_dust=False,
            summary_text=" · ".join(parts) + f" ({DUST_NOTE})", provider=PROVIDER,
            fetched_at=now.strftime(TS_FMT), expires_at=(now + CACHE_TTL).strftime(TS_FMT), is_failed=False,
        ))
    return out


def fetch_weekend_weather(region_sido: str, today: date, db=None, opener: Optional[Opener] = None,
                          now: Optional[datetime] = None) -> list[WeatherSnapshot]:
    """Weekend snapshots for `region_sido`. Uses the DB cache when every day is fresh; otherwise one HTTP call.
    Unsupported region / network / parse failure -> is_failed snapshots (never a fake sunny day)."""
    now = now or datetime.now()
    targets = weekend_dates(today)
    if db is not None:
        cached = [db.get_weather_snapshot(region_sido, d.isoformat(), now) for d in targets]
        if all(c is not None and not c.is_failed for c in cached):
            return cached  # type: ignore[return-value]
    if region_sido not in REGION_COORDS:
        return [_failed(region_sido, d, now) for d in targets]
    try:
        raw = (opener or _default_opener)(build_url(region_sido, targets[0], targets[-1]), TIMEOUT_SEC)
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
        snaps = parse_daily(payload, region_sido, targets, now)
    except Exception:
        return [_failed(region_sido, d, now) for d in targets]
    if db is not None:
        for s in snaps:
            if not s.is_failed:
                db.upsert_weather_snapshot(s)
    return snaps


def weekend_outlook(snaps: list[WeatherSnapshot]) -> tuple[bool, bool]:
    """(any_rain_or_snow, any_failed). Callers should treat a failed outlook as 'unknown' and, if they must
    pick, prefer indoor-safe suggestions — never claim the weather is clear."""
    return any(s.is_rain_or_snow for s in snaps), (not snaps) or any(s.is_failed for s in snaps)
