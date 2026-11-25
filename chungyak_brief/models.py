"""Domain models for chungyak_bot (아파트 청약 알림봇).

- All `UserProfile` fields are nullable except `chat_id` (unknown != false; empty profile -> PARTIAL_UNKNOWN).
- Monetary/area numerics use `Decimal` (never float) and are persisted as TEXT.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
from typing import Any, ClassVar, Optional


class NoticeType(str, Enum):
    APT_GENERAL = "APT_GENERAL"  # APT 일반분양(민영)
    MOOSUNWI = "MOOSUNWI"        # 무순위(사후접수)
    IMUI = "IMUI"                # 임의공급
    CANCEL_RE = "CANCEL_RE"      # 계약취소주택 재공급
    PUBLIC = "PUBLIC"            # 공공분양(국민주택)

    @property
    def label(self) -> str:
        return {
            NoticeType.APT_GENERAL: "APT 일반",
            NoticeType.MOOSUNWI: "무순위",
            NoticeType.IMUI: "임의공급",
            NoticeType.CANCEL_RE: "취소후재공급",
            NoticeType.PUBLIC: "공공분양",
        }[self]


class HouseType(str, Enum):
    APT = "APT"
    OFFICETEL = "OFFICETEL"
    URBAN_LIFE = "URBAN_LIFE"  # 도시형생활주택


class EligibilityStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    PARTIAL_UNKNOWN = "PARTIAL_UNKNOWN"
    INELIGIBLE = "INELIGIBLE"

    @property
    def badge(self) -> str:
        return {
            EligibilityStatus.ELIGIBLE: "🟢 자격 충족",
            EligibilityStatus.PARTIAL_UNKNOWN: "🟡 정보 부족",
            EligibilityStatus.INELIGIBLE: "🔴 자격 미달",
        }[self]


class DeliveryStatus(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"
    QUEUED_MORNING = "QUEUED_MORNING"


# SENT / UNKNOWN are terminal "treated as delivered" states ([PROD-LIVE] §3) — never auto re-sent.
DELIVERED_LIKE_STATUSES: frozenset[str] = frozenset({DeliveryStatus.SENT.value, DeliveryStatus.UNKNOWN.value})


class ChannelType(str, Enum):
    CUSTOM_MATCH = "CUSTOM_MATCH"    # 맞춤 조건 매칭 즉시 알림 (야간은 QUEUED_MORNING)
    REGULAR_DAILY = "REGULAR_DAILY"  # 08:00 정기 다이제스트


class TriggerContext(str, Enum):
    CRON_TICK = "CRON_TICK"
    STARTUP_INIT = "STARTUP_INIT"
    MANUAL = "MANUAL"


SPECIAL_SUPPLY_TYPES: tuple[str, ...] = (
    "NEWLYWED", "FIRST_LIFE", "MULTI_CHILD", "ELDERLY_PARENT", "NEWBORN", "YOUTH",
)

# ASCII callback-safe sido codes -> Korean short names used by 청약홈 (SUBSCRPT_AREA_CODE_NM).
SIDO_CODES: dict[str, str] = {
    "SEL": "서울", "BSN": "부산", "DGU": "대구", "ICN": "인천", "GWJ": "광주", "DJN": "대전",
    "ULS": "울산", "SJG": "세종", "GGI": "경기", "GWN": "강원", "CCB": "충북", "CCN": "충남",
    "JLB": "전북", "JLN": "전남", "GSB": "경북", "GSN": "경남", "JJU": "제주",
}
SIDO_NAME_TO_CODE: dict[str, str] = {v: k for k, v in SIDO_CODES.items()}

_SIDO_ALIASES: dict[str, str] = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천",
    "광주광역시": "광주", "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종",
    "경기도": "경기", "강원도": "강원", "강원특별자치도": "강원", "충청북도": "충북", "충청남도": "충남",
    "전라북도": "전북", "전북특별자치도": "전북", "전라남도": "전남", "경상북도": "경북",
    "경상남도": "경남", "제주특별자치도": "제주", "제주도": "제주",
}


def normalize_sido(value: Any) -> Optional[str]:
    """Maps '서울특별시' / '서울' / 'SEL' -> '서울'. Unknown/empty -> None."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.upper() in SIDO_CODES:
        return SIDO_CODES[raw.upper()]
    if raw in SIDO_NAME_TO_CODE:
        return raw
    if raw in _SIDO_ALIASES:
        return _SIDO_ALIASES[raw]
    head = raw.split()[0]
    if head in _SIDO_ALIASES:
        return _SIDO_ALIASES[head]
    if head in SIDO_NAME_TO_CODE:
        return head
    return None


def to_decimal(value: Any) -> Optional[Decimal]:
    """Decimal-safe parse (`Decimal(str(v))`); None/''/invalid/non-finite -> None. Never float()."""
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip().replace(",", "")
    if not raw:
        return None
    try:
        dec = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return dec if dec.is_finite() else None


def decimal_text(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    raw = str(value).strip().replace(".", "-").replace("/", "-")
    if not raw:
        return None
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


@dataclass
class ChungyakNotice:
    notice_key: str
    notice_type: NoticeType
    house_name: str
    house_type: HouseType = HouseType.APT
    house_manage_no: str = ""
    pblanc_no: str = ""
    region_sido: Optional[str] = None
    region_sigungu: Optional[str] = None
    address: str = ""
    supply_households: Optional[int] = None
    announce_date: Optional[date] = None
    receipt_start: Optional[date] = None
    receipt_end: Optional[date] = None
    winner_date: Optional[date] = None
    homepage_url: str = ""
    min_area_m2: Optional[Decimal] = None
    max_area_m2: Optional[Decimal] = None
    min_price_eok: Optional[Decimal] = None
    is_regulated: Optional[bool] = None  # 투기과열/조정대상 (None = unknown)
    special_supply_types: list[str] = field(default_factory=list)
    is_sample: bool = False
    content_hash: str = ""
    revision: int = 1

    def hash_payload(self) -> dict[str, Any]:
        """Canonical, deterministic subset used for amendment (정정공고) detection."""
        return {
            "notice_type": self.notice_type.value,
            "house_type": self.house_type.value,
            "house_name": self.house_name.strip(),
            "region_sido": self.region_sido,
            "region_sigungu": self.region_sigungu,
            "address": self.address.strip(),
            "supply_households": self.supply_households,
            "announce_date": self.announce_date.isoformat() if self.announce_date else None,
            "receipt_start": self.receipt_start.isoformat() if self.receipt_start else None,
            "receipt_end": self.receipt_end.isoformat() if self.receipt_end else None,
            "winner_date": self.winner_date.isoformat() if self.winner_date else None,
            "homepage_url": self.homepage_url.strip(),
            "min_area_m2": decimal_text(self.min_area_m2),
            "max_area_m2": decimal_text(self.max_area_m2),
            "min_price_eok": decimal_text(self.min_price_eok),
            "is_regulated": self.is_regulated,
            "special_supply_types": sorted(set(self.special_supply_types)),
        }

    def compute_content_hash(self) -> str:
        canonical = json.dumps(self.hash_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def ensure_hash(self) -> "ChungyakNotice":
        self.content_hash = self.compute_content_hash()
        return self


@dataclass
class UserProfile:
    """Every field except `chat_id` is nullable: None means "not answered yet" (unknown)."""

    chat_id: str
    residence_sido: Optional[str] = None
    residence_sigungu: Optional[str] = None
    residence_years: Optional[int] = None
    is_homeless: Optional[bool] = None
    is_household_head: Optional[bool] = None
    account_months: Optional[int] = None
    deposit_manwon: Optional[int] = None
    payment_count: Optional[int] = None
    preferred_regions: Optional[list[str]] = None
    max_price_eok: Optional[Decimal] = None
    desired_area_m2: Optional[Decimal] = None
    special_types: Optional[list[str]] = None
    include_officetel: Optional[bool] = None
    custom_alert_enabled: Optional[bool] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    ANSWER_FIELDS: ClassVar[tuple[str, ...]] = (
        "residence_sido", "residence_years", "is_homeless", "is_household_head", "account_months",
        "deposit_manwon", "payment_count", "preferred_regions", "max_price_eok", "desired_area_m2",
        "special_types",
    )

    def is_empty(self) -> bool:
        return all(getattr(self, name) in (None, []) for name in self.ANSWER_FIELDS)

    @property
    def alerts_on(self) -> bool:
        # Default ON (None) for custom alerts once the user has a profile row.
        return self.custom_alert_enabled is not False


@dataclass
class EligibilityResult:
    status: EligibilityStatus
    reasons: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    residence_priority: str = "UNKNOWN"  # LOCAL / AREA / NATIONWIDE / NONE / UNKNOWN
    required_deposit_manwon: Optional[int] = None
    special_matches: list[str] = field(default_factory=list)
