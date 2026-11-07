"""Benefit matching & reliability grade engine (부모님 조건 ➔ A/B/C 신뢰도).

Rules (worst grade wins):
- Hard exclude: 올해 도달 만 나이 < min_age, 지역 불일치, 독거 전용인데 독거 아님, 저소득 전용인데 '해당 아님'.
- GRADE_C: 지자체 사업(LOCAL)이면서 전국 시군구별 시행(region_code=ALL) ➔ 지자체 확인 필요.
- GRADE_B: 생일 미도래로 연령 미확정, 소득 '모름', 진단서 필요, 특정 시도 지자체 사업.
- GRADE_A: 전국 공통 사업이고 모든 조건이 확정 일치.
"""
from __future__ import annotations
from datetime import date
from typing import Iterable, Optional

from silver_care.models import (
    BenefitCategory,
    BenefitMatch,
    LivingType,
    ParentProfile,
    ReliabilityGrade,
    SilverBenefit,
    TargetIncome,
    TargetLiving,
    sido_name,
)

_CATEGORY_ORDER = {c.value: i for i, c in enumerate(BenefitCategory)}


def _income_check(benefit: SilverBenefit, parent: ParentProfile) -> tuple[str, Optional[bool]]:
    """Returns (label, True=일치 / None=확인 필요 / False=불일치→제외)."""
    target = TargetIncome(benefit.target_income)
    pension = parent.is_pension_receiver == 1
    low = parent.is_low_income == 1
    if target == TargetIncome.ALL:
        return "소득 무관", True
    if target == TargetIncome.PENSION_70:
        return "기초연금 수급(소득하위 70%)", True if (pension or low) else None
    if target == TargetIncome.MEDIAN_140:
        return "중위소득 140% 이하 또는 기초연금", True if (pension or low) else None
    # LOW_INCOME
    if low:
        return "기초수급·차상위", True
    if parent.is_low_income == 0:
        return "기초수급·차상위", False
    return "기초수급·차상위", None


def evaluate_benefit(
    benefit: SilverBenefit, parent: ParentProfile, ref_year: Optional[int] = None
) -> Optional[BenefitMatch]:
    """Grades one benefit for one parent. Returns None when the parent is clearly ineligible."""
    year = ref_year or date.today().year
    age_reached = parent.age_in(year)
    checks: list[tuple[str, Optional[bool]]] = []
    cautions: list[str] = []

    # 1. Age (birth year only → 생일 전이면 한 살 적음)
    if age_reached < benefit.min_age:
        return None
    if age_reached - 1 >= benefit.min_age:
        checks.append((f"만 {benefit.min_age}세 이상", True))
    else:
        checks.append((f"만 {benefit.min_age}세 이상 (올해 생일 이후)", None))

    # 2. Region
    if benefit.region_code == "ALL":
        checks.append(("전국 공통" if not benefit.is_local else "전국 지자체별 운영", True))
    elif benefit.region_code == parent.sido_code:
        checks.append((f"{sido_name(benefit.region_code)} 거주", True))
    else:
        return None

    # 3. Living type
    if benefit.target_living == TargetLiving.ALONE.value:
        if parent.living_type != LivingType.ALONE.value:
            return None
        checks.append(("혼자 사시는 어르신", True))

    # 4. Income
    label, ok = _income_check(benefit, parent)
    if ok is False:
        return None
    checks.append((label, ok))

    # 5. Documents
    if benefit.requires_prescription:
        checks.append(("의사 진단서·처방전", None))
        cautions.append("📄 의사 진단서·처방전 필요")
    if benefit.is_pre_approval_required:
        cautions.append("🚨 수술·구입 전 사전신청 필수 (사후 신청 불가)")

    grade = ReliabilityGrade.GRADE_A if all(ok is True for _, ok in checks) else ReliabilityGrade.GRADE_B
    if benefit.is_local:
        if benefit.region_code == "ALL":
            grade = ReliabilityGrade.GRADE_C
            cautions.append("🏛 시군구별 시행 여부·금액 상이")
        else:
            grade = ReliabilityGrade.worst(grade, ReliabilityGrade.GRADE_B)
            cautions.append("🏛 시군구별 세부 조건 상이")

    return BenefitMatch(benefit=benefit, grade=grade, checks=checks, cautions=cautions)


def match_benefits(
    benefits: Iterable[SilverBenefit], parent: ParentProfile, ref_year: Optional[int] = None
) -> list[BenefitMatch]:
    """Grades all benefits and returns eligible ones sorted by grade (A→C), category, id."""
    matches = [m for b in benefits if (m := evaluate_benefit(b, parent, ref_year)) is not None]
    matches.sort(key=lambda m: (m.grade.rank, _CATEGORY_ORDER[m.benefit.category], m.benefit.id))
    return matches
