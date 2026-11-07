"""Pediatric antipyretic dose engine (소아 해열제 안전 계산) — medical P0.

Invariants (checked in this order, fail-closed):
  1. weight_kg <= 0 or > 100            → INVALID_WEIGHT
  2. age_months < 4 (생후 3개월 미만 구간) → BLOCKED_INFANT_UNDER_3M for every product
  3. age_months < 6 and NSAID           → BLOCKED_NSAID_UNDER_6M
Blocked / invalid results always carry recommended_ml == 0.0.
"""
from __future__ import annotations

from kids_compass.models import FeverCalculationResult, IngredientType, MedicineProduct

STATUS_OK = "OK"
STATUS_INVALID_WEIGHT = "INVALID_WEIGHT"
STATUS_BLOCKED_INFANT = "BLOCKED_INFANT_UNDER_3M"
STATUS_BLOCKED_NSAID = "BLOCKED_NSAID_UNDER_6M"

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


def calculate_dose(weight_kg: float, age_months: int, product: MedicineProduct) -> FeverCalculationResult:
    base = dict(age_months=int(age_months), weight_kg=float(weight_kg), product=product, disclaimer=product.disclaimer)
    if not (MIN_WEIGHT_KG < float(weight_kg) <= MAX_WEIGHT_KG):
        return FeverCalculationResult(status=STATUS_INVALID_WEIGHT, warning_message=WARN_INVALID_WEIGHT, **base)
    if age_months < INFANT_BLOCK_MONTHS:
        return FeverCalculationResult(status=STATUS_BLOCKED_INFANT, warning_message=WARN_INFANT_UNDER_3M, **base)
    if age_months < NSAID_MIN_MONTHS and product.ingredient.is_nsaid:
        return FeverCalculationResult(status=STATUS_BLOCKED_NSAID, warning_message=WARN_NSAID_UNDER_6M, **base)

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
