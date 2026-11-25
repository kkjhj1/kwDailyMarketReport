"""3-State eligibility engine (ELIGIBLE / PARTIAL_UNKNOWN / INELIGIBLE) for 청약 notices.

Invariants:
- Unknown (None) profile answers never count as satisfied: they yield PARTIAL_UNKNOWN, never ELIGIBLE.
- An empty profile (only `chat_id`) is always PARTIAL_UNKNOWN unless a hard exclusion applies.
- Prices/areas are compared as `Decimal` (never float).
- Status describes 1순위(또는 특별공급) 신청 가능성 — 최종 자격은 입주자모집공고 원문으로 확인해야 한다.
"""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Optional

from chungyak_brief.models import (
    ChungyakNotice,
    EligibilityResult,
    EligibilityStatus,
    UserProfile,
    decimal_text,
)

RULES_PATH = Path(__file__).resolve().with_name("rules_2026.json")
_RULES_CACHE: dict[str, Any] | None = None


def load_rules(path: str | Path | None = None) -> dict[str, Any]:
    global _RULES_CACHE
    if path is None and _RULES_CACHE is not None:
        return _RULES_CACHE
    rules = json.loads(Path(path or RULES_PATH).read_text(encoding="utf-8"))
    if path is None:
        _RULES_CACHE = rules
    return rules


class EligibilityEngine:
    def __init__(self, rules: Optional[dict[str, Any]] = None) -> None:
        self.rules = rules or load_rules()
        self._groups: dict[str, str] = {}
        for group, members in self.rules["region_groups"].items():
            for sido in members:
                self._groups[sido] = group
        self._capital = set(self.rules["capital_area"])

    # ------------------------------------------------------------------ helpers
    def region_group(self, sido: Optional[str]) -> Optional[str]:
        if not sido:
            return None
        return self._groups.get(sido, "OTHER")

    def region_group_label(self, sido: Optional[str]) -> Optional[str]:
        group = self.region_group(sido)
        return self.rules["region_group_labels"].get(group) if group else None

    @staticmethod
    def area_band(area_m2: Optional[Decimal]) -> str:
        if area_m2 is None:
            return "85"
        if area_m2 <= Decimal("85"):
            return "85"
        if area_m2 <= Decimal("102"):
            return "102"
        if area_m2 <= Decimal("135"):
            return "135"
        return "all"

    def required_deposit(self, residence_sido: Optional[str], area_m2: Optional[Decimal]) -> Optional[int]:
        """민영주택 1순위 예치기준금액(만원) — 신청자 거주지 기준."""
        group = self.region_group(residence_sido)
        if group is None:
            return None
        return int(self.rules["deposit_table_manwon"][group][self.area_band(area_m2)])

    def _requirement(self, table: str, notice: ChungyakNotice) -> tuple[int, bool]:
        """Returns (required value, is_conservative_guess). Unknown regulation -> strictest value."""
        req = self.rules[table]
        if notice.is_regulated is True:
            return int(req["regulated"]), False
        if notice.is_regulated is None:
            return int(req["regulated"]), True
        key = "capital_area" if notice.region_sido in self._capital else "non_capital"
        return int(req[key]), False

    def residence_priority(self, notice: ChungyakNotice, profile: UserProfile) -> str:
        if notice.notice_type.value in self.rules["nationwide_types"]:
            return "NATIONWIDE"
        if not profile.residence_sido or not notice.region_sido:
            return "UNKNOWN"
        if profile.residence_sido == notice.region_sido:
            return "LOCAL"
        if profile.residence_sido in self._capital and notice.region_sido in self._capital:
            return "AREA"
        return "NONE"

    # ------------------------------------------------------------------ main
    def evaluate(self, notice: ChungyakNotice, profile: UserProfile) -> EligibilityResult:
        rules = self.rules
        nt = notice.notice_type.value
        hard: list[str] = []
        unknown: list[str] = []
        reasons: list[str] = []
        general_fail: list[str] = []
        general_unknown: list[str] = []

        # 0) 오피스텔·도시형생활주택: 기본 제외 (명시적 opt-in 시에만 포함)
        if notice.house_type.value in rules["excluded_house_types_default"] and profile.include_officetel is not True:
            hard.append("오피스텔·도시형 제외 (옵트인 필요)")

        # 1) 관심 지역
        if profile.preferred_regions:
            if notice.region_sido and notice.region_sido not in profile.preferred_regions:
                hard.append("관심 지역 아님")
            elif not notice.region_sido:
                unknown.append("공고 지역 미상")

        # 2) 예산 상한 (Decimal)
        if profile.max_price_eok is not None:
            if notice.min_price_eok is None:
                unknown.append("분양가 미공개")
            elif notice.min_price_eok > profile.max_price_eok:
                hard.append(
                    f"최저 분양가 {decimal_text(notice.min_price_eok)}억 > 상한 {decimal_text(profile.max_price_eok)}억"
                )
            else:
                reasons.append("예산 이내")

        # 3) 거주지 우선순위
        priority = self.residence_priority(notice, profile)
        if priority == "UNKNOWN":
            unknown.append("거주지" if not profile.residence_sido else "공고 지역 미상")
        elif priority == "LOCAL" and notice.is_regulated is not False:
            min_years = int(rules["local_priority_min_residence_years_regulated"])
            if profile.residence_years is None:
                if notice.is_regulated is True:
                    unknown.append("거주기간")
            elif profile.residence_years < min_years and notice.is_regulated is True:
                priority = "AREA"
                reasons.append(f"거주 {min_years}년 미만 → 기타지역 순위")
        if priority == "NONE" and nt in rules["residence_required_types"]:
            hard.append("공급 지역 거주 요건 미충족")
        elif priority == "LOCAL":
            reasons.append("해당지역 우선")
        elif priority == "AREA":
            reasons.append("기타지역(광역권)")
        elif priority == "NATIONWIDE":
            reasons.append("전국 신청 가능")

        # 4) 무주택 요건
        if nt in rules["homeless_required_types"]:
            if profile.is_homeless is None:
                unknown.append("무주택 여부")
            elif profile.is_homeless is False:
                hard.append("무주택 요건 미충족")

        # 5) 1순위 통장 요건 (일반공급 경로)
        required_deposit: Optional[int] = None
        if nt in rules["account_required_types"]:
            if nt == "PUBLIC":
                need, guessed = self._requirement("public_first_priority_min_payments", notice)
                if profile.payment_count is None:
                    general_unknown.append("납입 횟수")
                elif profile.payment_count < need:
                    if guessed and profile.payment_count >= int(
                        rules["public_first_priority_min_payments"]["non_capital"]
                    ):
                        general_unknown.append("규제지역 여부")
                    else:
                        general_fail.append(f"납입 {profile.payment_count}회 < {need}회")
            else:
                need, guessed = self._requirement("first_priority_min_account_months", notice)
                if profile.account_months is None:
                    general_unknown.append("청약통장 가입기간")
                elif profile.account_months < need:
                    if guessed and profile.account_months >= int(
                        rules["first_priority_min_account_months"]["non_capital"]
                    ):
                        general_unknown.append("규제지역 여부")
                    else:
                        general_fail.append(f"가입 {profile.account_months}개월 < {need}개월")
                area = profile.desired_area_m2 if profile.desired_area_m2 is not None else notice.min_area_m2
                required_deposit = self.required_deposit(profile.residence_sido, area)
                if required_deposit is None:
                    general_unknown.append("예치금 기준(거주지)")
                elif profile.deposit_manwon is None:
                    general_unknown.append("예치금")
                elif profile.deposit_manwon < required_deposit:
                    general_fail.append(f"예치금 {profile.deposit_manwon}만 < {required_deposit}만")
                if notice.is_regulated is True:
                    if profile.is_household_head is None:
                        general_unknown.append("세대주 여부")
                    elif profile.is_household_head is False:
                        general_fail.append("규제지역 1순위 세대주 요건")

        # 6) 특별공급 (본인 신고 유형 ∩ 공고 유형, 무주택 필수)
        special_matches: list[str] = []
        user_sp = set(profile.special_types or [])
        notice_sp = set(notice.special_supply_types or [])
        sp_rules = rules["special_supply"]
        for code in sorted(user_sp & notice_sp):
            meta = sp_rules.get(code)
            if not meta:
                continue
            if meta.get("requires_homeless") and profile.is_homeless is not True:
                continue
            special_matches.append(meta["label"])
        special_ok = bool(special_matches)
        if special_ok:
            reasons.append("특별공급: " + "·".join(special_matches))

        # ---- verdict
        if hard:
            return EligibilityResult(
                status=EligibilityStatus.INELIGIBLE,
                reasons=hard + reasons,
                unknowns=unknown,
                residence_priority=priority,
                required_deposit_manwon=required_deposit,
                special_matches=special_matches,
            )
        if general_fail and not special_ok:
            return EligibilityResult(
                status=EligibilityStatus.INELIGIBLE,
                reasons=general_fail + reasons,
                unknowns=unknown + general_unknown,
                residence_priority=priority,
                required_deposit_manwon=required_deposit,
                special_matches=special_matches,
            )
        # 일반공급이 막혔지만 특별공급 경로가 열린 경우 일반공급 미확인 항목은 무관하다.
        all_unknown = list(unknown) + ([] if (general_fail and special_ok) else list(general_unknown))
        if all_unknown or profile.is_empty():
            if profile.is_empty() and "프로필 미입력" not in all_unknown:
                all_unknown.insert(0, "프로필 미입력")
            status = EligibilityStatus.PARTIAL_UNKNOWN
        else:
            status = EligibilityStatus.ELIGIBLE
        return EligibilityResult(
            status=status,
            reasons=reasons + general_fail,
            unknowns=all_unknown,
            residence_priority=priority,
            required_deposit_manwon=required_deposit,
            special_matches=special_matches,
        )


def should_notify(result: EligibilityResult, profile: UserProfile) -> bool:
    """Custom-match alert policy: ELIGIBLE always; PARTIAL_UNKNOWN only with an explicit region interest
    (prevents an empty profile from matching every notice nationwide); INELIGIBLE never."""
    if result.status == EligibilityStatus.ELIGIBLE:
        return True
    if result.status == EligibilityStatus.PARTIAL_UNKNOWN:
        return bool(profile.preferred_regions)
    return False
