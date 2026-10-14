"""Fail-closed freshness/session/scope checks and SanityChecker spike/anomaly filter."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from .models import Quote, DataValidationError

URGENT_SYMBOLS = {'KR': frozenset({'KOSPI', 'KOSDAQ'}), 'US': frozenset({'SP500', 'NASDAQ_COMPOSITE'})}


def validate_batch(quotes: list[Quote], required: set[str], *,
                   now: datetime, max_age: timedelta | None = None,
                   require_final: bool = False) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be aware')
    grouped = {}
    for q in quotes:
        if q.symbol in grouped:
            raise DataValidationError(f'duplicate symbol: {q.symbol}')
        grouped[q.symbol] = q
        if q.as_of > now + timedelta(minutes=2):
            raise DataValidationError(f'future timestamp {q.symbol}')
        if max_age is not None and now - q.as_of > max_age:
            raise DataValidationError(f'stale quote {q.symbol}')
        if require_final and not q.is_final:
            raise DataValidationError(f'non-final quote {q.symbol}')
    if not required.issubset(grouped):
        raise DataValidationError('missing required: ' + ','.join(sorted(required - set(grouped))))


def validate_urgent_quote(quote: Quote, *, now: datetime, market_open: bool) -> Decimal:
    if not market_open:
        raise DataValidationError('outside confirmed regular session')
    if quote.symbol not in URGENT_SYMBOLS.get(quote.market, ()):
        raise DataValidationError('symbol not configured for urgent alert')
    if quote.session != 'REGULAR' or quote.unit != 'INDEX':
        raise DataValidationError('urgent alert requires regular-session index')
    validate_batch([quote], {quote.symbol}, now=now, max_age=timedelta(minutes=2))
    if quote.delayed_minutes != 0:
        raise DataValidationError('delayed index is ineligible for immediate alert')
    if quote.previous_close is None or quote.change_pct is None:
        raise DataValidationError('previous close required')
    return quote.change_pct


class SanityChecker:
    """Production sanity filter against crawling spikes, corrupt ticks, and unconfirmed alerts."""

    def __init__(self, max_daily_move_pct: Decimal = Decimal("20.0"),
                 max_tick_jump_pct: Decimal = Decimal("8.0")):
        self.max_daily_move_pct = max_daily_move_pct
        self.max_tick_jump_pct = max_tick_jump_pct
        self._last_seen: dict[str, Quote] = {}
        self._breach_streaks: dict[str, int] = {}

    def verify_quote(self, quote: Quote) -> None:
        """Rejects physically implausible spikes caused by HTML/JSON parsing errors."""
        if quote.unit != "PERCENT_YIELD" and quote.change_pct is not None:
            if abs(quote.change_pct) > self.max_daily_move_pct:
                raise DataValidationError(
                    f"sanity check failed for {quote.symbol}: abnormal daily move {quote.change_pct:.2f}%"
                )
        else:
            if quote.change_bp is not None and abs(quote.change_bp) > Decimal("150.0"):
                raise DataValidationError(
                    f"sanity check failed for {quote.symbol}: abnormal yield jump {quote.change_bp:.1f}bp"
                )

        prev_tick = self._last_seen.get(quote.symbol)
        if prev_tick is not None and quote.unit != "PERCENT_YIELD" and prev_tick.value > 0:
            tick_jump = abs((quote.value / prev_tick.value - 1) * 100)
            if tick_jump > self.max_tick_jump_pct:
                raise DataValidationError(
                    f"sanity check failed for {quote.symbol}: single-tick spike {tick_jump:.2f}% vs previous observation"
                )
        self._last_seen[quote.symbol] = quote

    def confirm_urgent_breach(self, quote: Quote, *, threshold_pct: Decimal = Decimal("3.0"),
                              required_confirmations: int = 2) -> bool:
        """Requires N consecutive observations >= ±3.0% before firing an urgent alert."""
        self.verify_quote(quote)
        if quote.change_pct is None or abs(quote.change_pct) < threshold_pct:
            self._breach_streaks[quote.symbol] = 0
            return False
        self._breach_streaks[quote.symbol] = self._breach_streaks.get(quote.symbol, 0) + 1
        return self._breach_streaks[quote.symbol] >= required_confirmations
