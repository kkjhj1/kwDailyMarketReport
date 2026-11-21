"""Pediatric antipyretic dose engine (소아 해열제 안전 계산) — medical P0.

Invariants (checked in this order, fail-closed, never raises on bad input):
  1. weight None / NaN / inf / <= 0 / > 100 → INVALID_WEIGHT
  2. age_months < 4 (생후 3개월 미만 구간)    → BLOCKED_INFANT_UNDER_3M for every product
  3. age_months < 6 and NSAID              → BLOCKED_NSAID_UNDER_6M
  4. age_months < product.min_age_months   → BLOCKED_PRODUCT_MIN_AGE
Blocked / invalid results always carry recommended_ml == 0.0.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional

from kids_compass.models import FeverCalculationResult, IngredientType, MedicineProduct

STATUS_OK = "OK"
STATUS_INVALID_WEIGHT = "INVALID_WEIGHT"
STATUS_BLOCKED_INFANT = "BLOCKED_INFANT_UNDER_3M"
STATUS_BLOCKED_NSAID = "BLOCKED_NSAID_UNDER_6M"
STATUS_BLOCKED_MIN_AGE = "BLOCKED_PRODUCT_MIN_AGE"

CROSS_TIMER_NOTICE = (
    "직전 복용 기록을 확인하세요. 지금 다른 성분을 복용해도 된다는 의미가 아닙니다. "
    "제품 설명서·의료진 지시를 확인하고, 탈수·구토·천식·신장/간 질환·수두 등이 있으면 "
    "이부프로펜 계열을 임의로 사용하지 마세요."
)

CROSS_SAME_INGREDIENT = "SAME_INGREDIENT"
CROSS_BLOCKED_SAME_NSAID = "BLOCKED_SAME_NSAID_CLASS"
CROSS_ALLOWED_WITH_CAUTION = "ALLOWED_WITH_CAUTION"

MIN_WEIGHT_KG = 0.0
MAX_WEIGHT_KG = 100.0
INFANT_BLOCK_MONTHS = 4
NSAID_MIN_MONTHS = 6

WARN_INFANT_UNDER_3M = (
    "🚨 생후 3개월(100일) 미만 영아의 38.0℃ 이상 발열은 중증 감염 위험이 있으므로 "
    "임의로 해열제를 먹이지 마시고 즉시 응급실 또는 소아청소년과 진료를 받으세요!"
)
WARN_NSAID_UNDER_6M = (
    "⚠️ 생후 6개월 미만 영아는 신장 부담 위험으로 이부프로펜/덱시부프로펜 복용이 금지됩니다. "
    "아세트아미노펜(타이레놀/챔프빨강) 단일제를 확인하거나 의사와 상담하세요."
)
WARN_INVALID_WEIGHT = "⚠️ 체중을 다시 확인해 주세요 (0kg 초과 ~ 100kg 이하)."
WARN_PRODUCT_MIN_AGE = "⚠️ 이 제품의 사용 가능 최소 연령 미만입니다. 제품 설명서를 확인하고 의사·약사와 상담하세요."

DISCLAIMER = (
    "본 계산은 제품 설명서 기준 참고용이며 진단·처방이 아닙니다. "
    "복용 전 반드시 제품 라벨을 확인하고 의사·약사와 상담하세요."
)

DEFAULT_MEDICINES: dict[str, MedicineProduct] = {
    "TYLENOL_32": MedicineProduct(
        product_id="TYLENOL_32",
        product_name="어린이 타이레놀 현탁액 / 챔프시럽(빨강)",
        ingredient=IngredientType.ACETAMINOPHEN,
        concentration_mg_ml=32.0,
        min_age_months=4,
        recommended_mg_kg=12.5,
        max_single_mg=640.0,
        interval_hours=4,
        max_times_per_day=5,
        max_daily_mg_kg=75.0,
        disclaimer=DISCLAIMER,
    ),
    "BRUFEN_20": MedicineProduct(
        product_id="BRUFEN_20",
        product_name="어린이 부루펜 시럽 / 챔프 이부펜(파랑)",
        ingredient=IngredientType.IBUPROFEN,
        concentration_mg_ml=20.0,
        min_age_months=6,
        recommended_mg_kg=7.5,
        max_single_mg=400.0,
        interval_hours=6,
        max_times_per_day=4,
        max_daily_mg_kg=40.0,
        disclaimer=DISCLAIMER,
    ),
    "MAXIBUFEN_12": MedicineProduct(
        product_id="MAXIBUFEN_12",
        product_name="맥시부펜 시럽",
        ingredient=IngredientType.DEXIBUPROFEN,
        concentration_mg_ml=12.0,
        min_age_months=6,
        recommended_mg_kg=6.0,
        max_single_mg=300.0,
        interval_hours=4,
        max_times_per_day=4,
        max_daily_mg_kg=28.0,
        disclaimer=DISCLAIMER,
    ),
}

CROSS_DOSING_GUIDE: dict[str, str] = {
    CROSS_SAME_INGREDIENT: "같은 성분은 정해진 간격(최소 4~6시간)을 지켜 1일 최대 횟수 안에서만 복용하세요.",
    CROSS_BLOCKED_SAME_NSAID: "⛔ 이부프로펜 ↔ 덱시부프로펜은 같은 NSAID 계열이므로 교차복용 절대 금지!",
    CROSS_ALLOWED_WITH_CAUTION: (
        "단일 해열제 복용 후에도 고열·보챔이 지속될 때만 의사·약사 지도 하에 "
        "최소 2~3시간 간격으로 제한적으로 고려하세요."
    ),
}


def _valid_weight(weight_kg) -> Optional[float]:
    """Finite float in (0, 100] or None. Never raises (None / NaN / inf / str garbage / bool -> None)."""
    if weight_kg is None or isinstance(weight_kg, bool):
        return None
    try:
        w = float(weight_kg)
    except (TypeError, ValueError):
        return None
    if math.isnan(w) or math.isinf(w) or not (MIN_WEIGHT_KG < w <= MAX_WEIGHT_KG):
        return None
    return w


def calculate_dose(weight_kg: Optional[float], age_months: int, product: MedicineProduct) -> FeverCalculationResult:
    weight = _valid_weight(weight_kg)
    try:
        months = int(age_months)
    except (TypeError, ValueError):
        months = -1  # unknown age -> fail-closed below (treated as < 3 months)
    base = dict(age_months=months, weight_kg=weight if weight is not None else 0.0, product=product,
                disclaimer=product.disclaimer)
    if weight is None:
        return FeverCalculationResult(status=STATUS_INVALID_WEIGHT, warning_message=WARN_INVALID_WEIGHT, **base)
    weight_kg = weight
    if months < INFANT_BLOCK_MONTHS:
        return FeverCalculationResult(status=STATUS_BLOCKED_INFANT, warning_message=WARN_INFANT_UNDER_3M, **base)
    if months < NSAID_MIN_MONTHS and product.ingredient.is_nsaid:
        return FeverCalculationResult(status=STATUS_BLOCKED_NSAID, warning_message=WARN_NSAID_UNDER_6M, **base)
    if months < int(product.min_age_months or 0):
        return FeverCalculationResult(status=STATUS_BLOCKED_MIN_AGE, warning_message=WARN_PRODUCT_MIN_AGE, **base)

    raw_mg = weight_kg * product.recommended_mg_kg
    capped_mg = min(raw_mg, product.max_single_mg)
    dose_ml = round(capped_mg / product.concentration_mg_ml, 1)
    max_daily_mg = min(weight_kg * product.max_daily_mg_kg, product.max_single_mg * product.max_times_per_day)
    max_daily_ml = round(max_daily_mg / product.concentration_mg_ml, 1)
    return FeverCalculationResult(
        status=STATUS_OK,
        recommended_ml=dose_ml,
        recommended_mg=round(capped_mg, 1),
        max_daily_ml=max_daily_ml,
        interval_hours=product.interval_hours,
        max_times_per_day=product.max_times_per_day,
        **base,
    )


def calculate_all(
    weight_kg: float, age_months: int, products: dict[str, MedicineProduct] | None = None
) -> dict[IngredientType, FeverCalculationResult]:
    """One result per ingredient (first product wins per ingredient), catalogue order preserved."""
    results: dict[IngredientType, FeverCalculationResult] = {}
    for product in (products or DEFAULT_MEDICINES).values():
        if product.ingredient not in results:
            results[product.ingredient] = calculate_dose(weight_kg, age_months, product)
    return results


def check_cross_dosing_safety(first_ing: IngredientType, second_ing: IngredientType) -> str:
    if first_ing == second_ing:
        return CROSS_SAME_INGREDIENT
    if first_ing.is_nsaid and second_ing.is_nsaid:
        return CROSS_BLOCKED_SAME_NSAID
    return CROSS_ALLOWED_WITH_CAUTION


def cross_dosing_candidates(given_ingredient: IngredientType, age_months: int) -> list[IngredientType]:
    """Other-class ingredients a cross-timer may mention. 이부프로펜 ↔ 덱시부프로펜 is NEVER a candidate.
    Under 6 months (or unknown age) -> [] (NSAID 금지, and acetaminophen-only infants get no cross timer)."""
    try:
        months = int(age_months)
    except (TypeError, ValueError):
        return []
    if months < NSAID_MIN_MONTHS:
        return []
    given = IngredientType(given_ingredient)
    if given == IngredientType.ACETAMINOPHEN:
        return [IngredientType.IBUPROFEN, IngredientType.DEXIBUPROFEN]
    if given.is_nsaid:
        return [IngredientType.ACETAMINOPHEN]
    return []


def _parse_given_at(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).strip()).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def check_24h_dose_limit(recent_events: list[dict], ingredient: IngredientType, weight_kg: float,
                         product: MedicineProduct, now_dt: datetime) -> tuple[bool, str]:
    """(allowed, reason). Counts SAME-ingredient doses within (now - 24h, now]: blocks when the count already
    reached product.max_times_per_day or when accumulated mg + this single dose > weight * max_daily_mg_kg.
    Fail-closed: invalid weight -> blocked; an event whose time cannot be parsed counts as within 24h."""
    weight = _valid_weight(weight_kg)
    if weight is None:
        return False, WARN_INVALID_WEIGHT
    ingredient = IngredientType(ingredient)
    now = now_dt.replace(tzinfo=None)
    since = now - timedelta(hours=24)
    count, total_mg = 0, 0.0
    for ev in recent_events or []:
        ev_ing = str(ev.get("ingredient") or "")
        if ev_ing != ingredient.value:
            continue
        given = _parse_given_at(ev.get("given_at"))
        if given is not None and not (since < given <= now):
            continue
        count += 1
        try:
            total_mg += max(float(ev.get("dose_mg") or 0.0), 0.0)
        except (TypeError, ValueError):
            pass
    if count >= int(product.max_times_per_day):
        return False, (f"⛔ 최근 24시간 {ingredient.label} {count}회 복용 — 1일 최대 {product.max_times_per_day}회에 "
                       "도달했습니다. 추가 복용 전 의사·약사와 상담하세요.")
    this_mg = min(weight * product.recommended_mg_kg, product.max_single_mg)
    limit_mg = weight * product.max_daily_mg_kg
    if total_mg + this_mg > limit_mg + 1e-9:
        return False, (f"⛔ 최근 24시간 {ingredient.label} 누적 {total_mg:g}mg + 이번 {this_mg:g}mg 이 "
                       f"1일 최대량 {limit_mg:g}mg 을 넘습니다. 추가 복용 전 의사·약사와 상담하세요.")
    return True, "OK"
