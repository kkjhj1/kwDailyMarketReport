"""Weather Hard Gate & KST Season/Week Engine for YOLO Bot.
Enforces [INV-02]:
- Never treat missing/failed weather data as CLEAR (fail-safe to UNKNOWN / severe).
- Evaluates precipitation, snow, extreme heat/cold, dust, fog, and weather advisories.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from yolo_brief.models import KST, Season, WeatherClass, WeatherSnapshot


def get_kst_today(ref_date: Optional[date] = None) -> date:
    if ref_date is not None:
        return ref_date
    return datetime.now(KST).date()


def get_kst_season(ref_date: Optional[date] = None) -> Season:
    d = get_kst_today(ref_date)
    return Season.from_month(d.month)


def get_iso_week_key(ref_date: Optional[date] = None) -> str:
    """Returns ISO week key in KST, e.g., '2026-W45'."""
    d = get_kst_today(ref_date)
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year:04d}-W{iso_week:02d}"


def evaluate_weather_gate(
    snapshot: Optional[WeatherSnapshot],
) -> tuple[bool, WeatherClass, str]:
    """Evaluates whether the current weather triggers the Severe Weather Hard Gate.

    Returns:
        (is_severe_weather, effective_weather_class, human_readable_reason_ko)
    """
    if snapshot is None or snapshot.weather_class == WeatherClass.UNKNOWN:
        return (
            True,
            WeatherClass.UNKNOWN,
            "기상 정보 미확인 (안전 실내 모드)",
        )

    if snapshot.has_advisory:
        eff = (
            snapshot.weather_class
            if snapshot.weather_class not in (WeatherClass.CLEAR, WeatherClass.ALL)
            else WeatherClass.RAIN
        )
        return True, eff, "기상특보 발령"

    if snapshot.snow_mm > 0 or snapshot.weather_class == WeatherClass.SNOW:
        snow_txt = f" ({snapshot.snow_mm:.1f}mm)" if snapshot.snow_mm > 0 else ""
        return True, WeatherClass.SNOW, f"강설 예보{snow_txt}"

    if (
        snapshot.pop_percent >= 60 and snapshot.precip_mm >= 1.0
    ) or snapshot.weather_class == WeatherClass.RAIN:
        pop_txt = f" (강수확률 {snapshot.pop_percent}%)" if snapshot.pop_percent > 0 else ""
        return True, WeatherClass.RAIN, f"우천 예보{pop_txt}"

    if snapshot.feels_like_c >= 33.0 or snapshot.weather_class == WeatherClass.HEAT:
        return True, WeatherClass.HEAT, f"폭염 주의 (체감 {snapshot.feels_like_c:.0f}°C)"

    if snapshot.feels_like_c <= -10.0 or snapshot.weather_class == WeatherClass.COLD:
        return True, WeatherClass.COLD, f"한파 주의 (체감 {snapshot.feels_like_c:.0f}°C)"

    if (
        str(snapshot.pm_grade).upper() in ("BAD", "VERY_BAD")
        or snapshot.weather_class == WeatherClass.DUST
    ):
        return True, WeatherClass.DUST, "미세먼지 나쁨"

    if snapshot.weather_class == WeatherClass.FOG:
        return True, WeatherClass.FOG, "짙은 안개 주의"

    return False, WeatherClass.CLEAR, "맑음·활동 양호"
