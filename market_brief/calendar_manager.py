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
})

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
            is_open = (
                self.is_trading_day("KR", d)
                and time(9, 0) <= kst_now.time() <= time(15, 30)
            )
            return SessionInfo(
                market="KR",
                trade_date=d,
                is_open=is_open,
                session_label="KRX 정규장 (09:00~15:30 KST)" if is_open else "KRX 장외/마감",
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

    def should_run_regular_briefing(self, kind: str, now: datetime) -> bool:
        """Checks if 07:30 morning or 15:40 closing briefing should run on KST business day."""
        kst_now = now.astimezone(KST)
        if not self.is_trading_day("KR", kst_now.date()):
            return False
        if kind == "morning":
            return time(7, 30) <= kst_now.time() < time(7, 45)
        if kind == "closing":
            return time(15, 40) <= kst_now.time() < time(15, 55)
        return False
