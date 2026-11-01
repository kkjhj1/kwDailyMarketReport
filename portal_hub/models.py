"""Data models for Hub Portal Bot.
Synced directly with alerts.sqlite3 (users, subscriptions, bots, tier_policies).
Includes BotCategory for scalable 20-30 bot discovery and search keyword tags.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class UserTier(str, Enum):
    """Membership Tiers matching alerts.sqlite3 tier_policies."""
    WHITE = "WHITE"     # 프리 / 화이트 (기본 1개 슬롯)
    FREE = "FREE"       # Alias for WHITE
    GREEN = "GREEN"     # 그린 등급 (최대 3개 슬롯)
    RED = "RED"         # 레드 등급 (최대 5개 슬롯)
    BLACK = "BLACK"     # 블랙 등급 (무제한 VIP)

    @property
    def display_name(self) -> str:
        names = {
            UserTier.WHITE: "프리 (WHITE)",
            UserTier.FREE: "프리 (FREE)",
            UserTier.GREEN: "그린 (GREEN)",
            UserTier.RED: "레드 (RED)",
            UserTier.BLACK: "블랙 (BLACK VIP)",
        }
        return names.get(self, self.value)

    @property
    def badge(self) -> str:
        badges = {
            UserTier.WHITE: "⚪ [FREE]",
            UserTier.FREE: "⚪ [FREE]",
            UserTier.GREEN: "🟢 [GREEN]",
            UserTier.RED: "🔴 [RED]",
            UserTier.BLACK: "⚫ [BLACK VIP]",
        }
        return badges.get(self, "⚪")

    @property
    def max_slots(self) -> int:
        slots = {
            UserTier.WHITE: 1,
            UserTier.FREE: 1,
            UserTier.GREEN: 3,
            UserTier.RED: 5,
            UserTier.BLACK: 999999,
        }
        return slots.get(self, 1)

    @property
    def gauge_char(self) -> str:
        chars = {
            UserTier.WHITE: "◽",
            UserTier.FREE: "◽",
            UserTier.GREEN: "🟩",
            UserTier.RED: "🟥",
            UserTier.BLACK: "⬛",
        }
        return chars.get(self, "■")


class BotCategory(str, Enum):
    """Category classification for 20-30+ bot scalability."""
    FINANCE = "FINANCE"   # 금융 / 재테크
    EDU = "EDU"           # 교육 / 입시
    LIFE = "LIFE"         # 라이프 / 여가
    BIZ = "BIZ"           # 업무 / 생산성

    @property
    def title(self) -> str:
        titles = {
            BotCategory.FINANCE: "💰 금융 / 재테크",
            BotCategory.EDU: "🎓 교육 / 입시",
            BotCategory.LIFE: "✈️ 라이프 / 여가",
            BotCategory.BIZ: "💼 업무 / 생산성",
        }
        return titles.get(self, self.value)

    @property
    def icon(self) -> str:
        icons = {
            BotCategory.FINANCE: "💰",
            BotCategory.EDU: "🎓",
            BotCategory.LIFE: "✈️",
            BotCategory.BIZ: "💼",
        }
        return icons.get(self, "📂")


class BotServiceType(str, Enum):
    """Catalog of available specialized bots in alerts.sqlite3."""
    BRIEF = "brief_bot"          # 매일 아침 시황 브리핑
    ADMISSION = "admission_bot"  # 에듀브리프 입시 나침반
    TRAVEL = "travel_bot"        # 트래블 패스파인더 여행 특가

    @property
    def title(self) -> str:
        titles = {
            BotServiceType.BRIEF: "📈 스마트 시황 브리핑",
            BotServiceType.ADMISSION: "🎓 대입/수시 입시 나침반",
            BotServiceType.TRAVEL: "✈️ 트래블 패스파인더",
        }
        return titles.get(self, self.value)

    @property
    def short_name(self) -> str:
        names = {
            BotServiceType.BRIEF: "시황 브리핑",
            BotServiceType.ADMISSION: "입시 정보",
            BotServiceType.TRAVEL: "여행 특가",
        }
        return names.get(self, self.value)

    @property
    def category(self) -> BotCategory:
        cats = {
            BotServiceType.BRIEF: BotCategory.FINANCE,
            BotServiceType.ADMISSION: BotCategory.EDU,
            BotServiceType.TRAVEL: BotCategory.LIFE,
        }
        return cats.get(self, BotCategory.FINANCE)

    @property
    def icon(self) -> str:
        icons = {
            BotServiceType.BRIEF: "📈",
            BotServiceType.ADMISSION: "🎓",
            BotServiceType.TRAVEL: "✈️",
        }
        return icons.get(self, "🤖")

    @property
    def one_line_desc(self) -> str:
        descs = {
            BotServiceType.BRIEF: "매일 06:30 글로벌 증시/환율 & 장전 3줄 요약",
            BotServiceType.ADMISSION: "고교/수험생 주간 필수 To-Do & 대입 설명회 요약",
            BotServiceType.TRAVEL: "주간 최저가 항공권 발굴 & 테마 여행지 큐레이션",
        }
        return descs.get(self, "")

    @property
    def keywords(self) -> list[str]:
        """Search keywords for instant query matching."""
        kw = {
            BotServiceType.BRIEF: ["시황", "증시", "환율", "유가", "금리", "나스닥", "코스피", "장전", "주식", "금융", "finance", "stock"],
            BotServiceType.ADMISSION: ["입시", "수시", "정시", "대입", "수능", "고1", "고2", "고3", "설명회", "학평", "모평", "학교", "교육", "edu"],
            BotServiceType.TRAVEL: ["여행", "특가", "항공권", "호텔", "비행기", "휴가", "주말", "엔화", "환전", "해외여행", "travel", "flight"],
        }
        return kw.get(self, [])

    @property
    def telegram_handle(self) -> str:
        handles = {
            BotServiceType.BRIEF: os.environ.get("MARKET_BOT_USERNAME", "KwDailyMarketReportBot"),
            BotServiceType.ADMISSION: os.environ.get("ADMISSION_BOT_USERNAME", "EduBriefAdmissionBot"),
            BotServiceType.TRAVEL: os.environ.get("TRAVEL_BOT_USERNAME", "TravelBriefBot"),
        }
        raw = handles.get(self, "")
        return raw.strip().lstrip("@")


@dataclass
class UserProfile:
    """User account state loaded from alerts.sqlite3."""
    user_id: int
    chat_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    tier: UserTier = UserTier.WHITE
    status: str = "ACTIVE"
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    updated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
