"""Market trading calendar & session manager with US DST and holiday handling."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")

# Sample official holidays (can be extended or loaded from DB/config)
DEFAULT_KR_HOLIDAYS = frozenset({
    date(2026, 1, 1),
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 3, 2),
    date(2026, 5, 5), date(2026, 5, 25),
    date(2026, 6, 3),
    date(2026, 8, 17),
    date(2026, 9, 24), date(2026, 9, 25),
    date(2026, 10, 5), date(2026, 10, 9),
    date(2026, 12, 25),
    date(2026, 12, 31),  # KRX 연말 휴장일 (year-end settlement holiday)
})

# 대학수학능력시험일: KRX 개장 1시간 지연 (10:00~16:30 KST)
CSAT_DATES_KR = frozenset({date(2026, 11, 19)})
# 신년 증시 개장식: KRX 개장 1시간 지연 (10:00~15:30 KST)
NEW_YEAR_OPENING_DATES_KR = frozenset({date(2026, 1, 2)})

_FOMC = "🇺🇸 미 FOMC 기준금리 발표 (새벽 04:00 KST)"
_BOK = "🇰🇷 한국은행 금융통화위원회 기준금리 결정"
_WITCHING = "⚡ 국내 선물·옵션 동시 만기일 (변동성 유의)"
_CSAT = "⏰ 대학수학능력시험일: 증시 개장 지연 (10:00~16:30)"


def _build_economic_events_2026() -> dict[date, tuple[str, ...]]:
    # Curated static calendar (sources: Fed FOMC 2026 schedule — announcement day in KST,
    # 한국은행 2026 통화정책방향 결정회의 일정, KRX 파생상품 만기일, 교육부 수능 시행일).
    events: dict[date, list[str]] = {}
    groups = (
        ((date(2026, 1, 29), date(2026, 3, 19), date(2026, 5, 7), date(2026, 6, 18),
          date(2026, 7, 30), date(2026, 9, 17), date(2026, 11, 5), date(2026, 12, 17)), _FOMC),
        ((date(2026, 1, 15), date(2026, 2, 26), date(2026, 4, 16), date(2026, 5, 28),
          date(2026, 7, 9), date(2026, 8, 27), date(2026, 10, 15), date(2026, 11, 26)), _BOK),
        ((date(2026, 3, 12), date(2026, 6, 11), date(2026, 9, 10), date(2026, 12, 10)), _WITCHING),
        ((date(2026, 11, 19),), _CSAT),
    )
    for dates, label in groups:
        for d in dates:
            events.setdefault(d, []).append(label)
    return {d: tuple(v) for d, v in events.items()}


ECONOMIC_EVENTS_2026: dict[date, tuple[str, ...]] = _build_economic_events_2026()

DEFAULT_US_HOLIDAYS = frozenset({
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
})


@dataclass(frozen=True)
class SessionInfo:
    market: str
    trade_date: date
    is_open: bool
    session_label: str
    is_dst: bool


class MarketCalendar:
    """Determines trading dates, regular session open status, and US DST automatically."""

    def __init__(self, kr_holidays: set[date] | frozenset[date] = DEFAULT_KR_HOLIDAYS,
                 us_holidays: set[date] | frozenset[date] = DEFAULT_US_HOLIDAYS):
        self.kr_holidays = frozenset(kr_holidays)
        self.us_holidays = frozenset(us_holidays)

    def is_trading_day(self, market: str, d: date) -> bool:
        if d.weekday() >= 5:
            return False
        if market == "KR":
            return d not in self.kr_holidays
        if market == "US":
            return d not in self.us_holidays
        return True

    def get_session_info(self, market: str, now: datetime) -> SessionInfo:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        if market == "KR":
            kst_now = now.astimezone(KST)
            d = kst_now.date()
            open_t, close_t = self.get_kr_session_window(d)
            is_open = (
                self.is_trading_day("KR", d)
                and open_t <= kst_now.time() <= close_t
            )
            if d in CSAT_DATES_KR:
                open_label = "KRX 정규장 (10:00~16:30 KST - 수능일)"
            elif d in NEW_YEAR_OPENING_DATES_KR:
                open_label = "KRX 정규장 (10:00~15:30 KST - 증시 개장식)"
            else:
                open_label = "KRX 정규장 (09:00~15:30 KST)"
            return SessionInfo(
                market="KR",
                trade_date=d,
                is_open=is_open,
                session_label=open_label if is_open else "KRX 장외/마감",
                is_dst=False,
            )

        if market == "US":
            et_now = now.astimezone(ET)
            d = et_now.date()
            is_dst = bool(et_now.dst())
            is_open = (
                self.is_trading_day("US", d)
                and time(9, 30) <= et_now.time() <= time(16, 0)
            )
            kst_window = "22:30~05:00 KST (서머타임)" if is_dst else "23:30~06:00 KST (표준시)"
            return SessionInfo(
                market="US",
                trade_date=d,
                is_open=is_open,
                session_label=f"미국 정규장 ({kst_window})" if is_open else f"미국 장외/마감 ({kst_window})",
                is_dst=is_dst,
            )

        if market == "GLOBAL":
            kst_now = now.astimezone(KST)
            # FX / Global commodities session rolls over at 06:00 KST
            trade_d = kst_now.date() if kst_now.time() >= time(6, 0) else (kst_now.date() - timedelta(days=1))
            is_open = trade_d.weekday() < 5
            return SessionInfo(
                market="GLOBAL",
                trade_date=trade_d,
                is_open=is_open,
                session_label="글로벌 24시간/고시 시장",
                is_dst=False,
            )

        raise ValueError(f"Unsupported market: {market}")

    @staticmethod
    def get_kr_session_window(d: date) -> tuple[time, time]:
        """Canonical KRX regular session (open, close) in KST, incl. CSAT/New Year delayed opens."""
        if d in CSAT_DATES_KR:
            return time(10, 0), time(16, 30)
        if d in NEW_YEAR_OPENING_DATES_KR:
            return time(10, 0), time(15, 30)
        return time(9, 0), time(15, 30)

    @staticmethod
    def get_regular_briefing_window(kind: str, d: date) -> tuple[time, time, time]:
        """Returns (start, window_end, hard_cutoff) KST for a regular briefing.

        Start window is half-open [start, window_end). Closing shifts +1h on CSAT day.
        """
        if kind == "morning":
            return time(7, 50), time(8, 5), time(8, 50)
        if kind == "closing":
            if d in CSAT_DATES_KR:
                return time(16, 40), time(16, 55), time(19, 0)
            return time(15, 40), time(15, 55), time(18, 0)
        raise ValueError(f"Unsupported regular briefing kind: {kind}")

    def should_run_regular_briefing(self, kind: str, now: datetime) -> bool:
        """Checks if the morning/closing briefing start window [start, window_end) is active on a KR business day."""
        kst_now = now.astimezone(KST)
        d = kst_now.date()
        if not self.is_trading_day("KR", d):
            return False
        if kind not in ("morning", "closing"):
            return False
        start, window_end, _ = self.get_regular_briefing_window(kind, d)
        return start <= kst_now.time() < window_end

    def get_economic_events(self, d: date) -> list[str]:
        return list(ECONOMIC_EVENTS_2026.get(d, ()))

    def get_session_notice(self, d: date) -> str:
        if d in CSAT_DATES_KR:
            return "⏰ [증시 개장 안내] 오늘은 대학수학능력시험일로 정규장이 10:00~16:30에 운영됩니다."
        if d in NEW_YEAR_OPENING_DATES_KR:
            return "⏰ [증시 개장 안내] 오늘은 증시 개장식으로 정규장이 10:00~15:30에 운영됩니다."
        return ""
