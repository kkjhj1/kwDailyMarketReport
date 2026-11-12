"""User-configured intraday volatility target watches for `brief_bot` (L2).

Separation of concerns:
- Regular 06:30 / 15:40 KST briefings stay universal FINANCE broadcasts (`MarketBriefingPipeline`).
- Intraday alerts are *per-user* target watches evaluated here (`MarketWatchEvaluator`).
- The legacy global ±3% broadcast is gated by `LEGACY_URGENT_BROADCAST_ENABLED` (default "false").

Invariants:
- Only live, fresh, non-delayed NAVER quotes inside the asset's open session are evaluated
  (`MOCK_FIXTURE`, `FIXTURE:*`, `FALLBACK`, `delayed_minutes > 0` are rejected and never counted).
- A watch's first valid observation only records the baseline (never triggers retroactively).
- A trigger needs 2 consecutive *distinct* valid crossing readings while armed; re-arm needs a return across a
  hysteresis band (half threshold for CHANGE_*, small buffer for LEVEL). State persists in SQLite (restart-safe).
- Delivery ledger key `mw:v1:{watch_id}:{session_id}:{condition_version}`: SENT / IN_FLIGHT / UNKNOWN are never
  re-sent; only an explicit FAILED is re-claimable. One grouped message per user per tick ([TG-01]/[TG-02]).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import html
import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from market_brief.calendar_manager import MarketCalendar
from market_brief.market_data.models import Quote
from market_brief.storage.db import MARKET_WATCH_TIER_LIMITS, MarketBriefDatabase

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

CONDITION_CHANGE_PCT = "CHANGE_PCT"
CONDITION_CHANGE_BP = "CHANGE_BP"
CONDITION_LEVEL = "LEVEL"
DIRECTIONS = ("UP", "DOWN", "BOTH")
DIRECTION_CODES = {"U": "UP", "D": "DOWN", "B": "BOTH"}
DIRECTION_LABELS = {"UP": "상승", "DOWN": "하락", "BOTH": "양방향"}

MAX_QUOTE_AGE_SEC = 120
MAX_QUOTE_FUTURE_SEC = 120
REQUIRED_CONFIRMATIONS = 2
WATCH_TICK_INTERVAL_SEC = 120.0
MAX_VISIBLE_CHARS = 1800
MAX_HTML_CHARS = 3500
LEVEL_INPUT_TTL_SEC = 600.0
QUICK_DEFAULT_SYMBOLS = ("KOSPI", "SP500")

_LIVE_SOURCE_PREFIXES = ("NAVER_", "NAVER:")
_REJECTED_SOURCE_MARKERS = ("MOCK", "FIXTURE", "FALLBACK")


def legacy_urgent_broadcast_enabled() -> bool:
    """Legacy global ±3% FINANCE broadcast gate (default OFF)."""
    return os.environ.get("LEGACY_URGENT_BROADCAST_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AssetSpec:
    symbol: str
    label: str
    emoji: str
    session_market: str  # KR | US | GLOBAL (calendar session used for watch evaluation)
    quote_unit: str      # INDEX | FX | PRICE | PERCENT_YIELD
    group: str           # EQUITY | MACRO | BOND

    @property
    def change_condition(self) -> str:
        return CONDITION_CHANGE_BP if self.quote_unit == "PERCENT_YIELD" else CONDITION_CHANGE_PCT

    @property
    def change_unit(self) -> str:
        return "BP" if self.quote_unit == "PERCENT_YIELD" else "PERCENT"


ASSET_CATALOG: dict[str, AssetSpec] = {
    "KOSPI": AssetSpec("KOSPI", "코스피", "🇰🇷", "KR", "INDEX", "EQUITY"),
    "KOSDAQ": AssetSpec("KOSDAQ", "코스닥", "🇰🇷", "KR", "INDEX", "EQUITY"),
    "KOSPI200": AssetSpec("KOSPI200", "코스피200", "🇰🇷", "KR", "INDEX", "EQUITY"),
    "SP500": AssetSpec("SP500", "S&P500", "🇺🇸", "US", "INDEX", "EQUITY"),
    "NASDAQ_COMPOSITE": AssetSpec("NASDAQ_COMPOSITE", "나스닥", "🇺🇸", "US", "INDEX", "EQUITY"),
    "USDKRW": AssetSpec("USDKRW", "원/달러", "💱", "GLOBAL", "FX", "MACRO"),
    "WTI": AssetSpec("WTI", "WTI유가", "🛢️", "GLOBAL", "PRICE", "MACRO"),
    "GOLD": AssetSpec("GOLD", "국제금", "🥇", "GLOBAL", "PRICE", "MACRO"),
    "KR3Y": AssetSpec("KR3Y", "국고채3년", "🏦", "KR", "PERCENT_YIELD", "BOND"),
    "KR10Y": AssetSpec("KR10Y", "국고채10년", "🏦", "KR", "PERCENT_YIELD", "BOND"),
    "US10Y": AssetSpec("US10Y", "미국채10년", "🏛️", "GLOBAL", "PERCENT_YIELD", "BOND"),
    "US2Y": AssetSpec("US2Y", "미국채2년", "🏛️", "GLOBAL", "PERCENT_YIELD", "BOND"),
}

# Callback preset codes (ASCII) -> Decimal threshold
PCT_PRESETS: dict[str, Decimal] = {"150": Decimal("1.5"), "200": Decimal("2.0"), "300": Decimal("3.0")}
BP_PRESETS: dict[str, Decimal] = {"5": Decimal("5"), "10": Decimal("10"), "15": Decimal("15")}


def presets_for(spec: AssetSpec) -> dict[str, Decimal]:
    return BP_PRESETS if spec.quote_unit == "PERCENT_YIELD" else PCT_PRESETS


def dec_str(value: Decimal) -> str:
    """Canonical normalized Decimal string (`2.0` -> `2`, `1E+3` -> `1000`) used for storage + duplicate guard."""
    norm = value.normalize()
    text = format(norm, "f")
    return text if text not in {"-0", ""} else "0"


def parse_level_input(text: str) -> Decimal:
    """Validates a manual LEVEL target. Rejects non-numeric, NaN, Inf, `<= 0` and absurd precision."""
    raw = (text or "").strip().replace(",", "").replace(" ", "")
    raw = raw.rstrip("%")
    if not raw or len(raw) > 20 or not re.fullmatch(r"[0-9]*\.?[0-9]+", raw):
        raise ValueError("숫자만 입력해 주세요. (예: 1400 또는 3.25)")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("숫자 형식이 올바르지 않습니다.") from exc
    if not value.is_finite():
        raise ValueError("유효한 숫자가 아닙니다.")
    if value <= 0:
        raise ValueError("0보다 큰 값을 입력해 주세요.")
    return value


# =============================================================================
# Freshness / live-source gate
# =============================================================================


def check_live_quote(quote: Quote, now: datetime, calendar: MarketCalendar) -> tuple[bool, str]:
    """Returns (accepted, reason). Only live NAVER quotes, open session, fresh <=2min (and <=2min future)."""
    spec = ASSET_CATALOG.get(quote.symbol)
    if spec is None:
        return False, "UNSUPPORTED_SYMBOL"
    src = (quote.source or "").upper()
    if any(marker in src for marker in _REJECTED_SOURCE_MARKERS):
        return False, "REJECTED_SOURCE"
    if not (quote.source or "").startswith(_LIVE_SOURCE_PREFIXES):
        return False, "NON_LIVE_SOURCE"
    if quote.delayed_minutes > 0:
        return False, "DELAYED_QUOTE"
    if now.tzinfo is None or quote.as_of.tzinfo is None:
        return False, "NAIVE_TIMESTAMP"
    session = calendar.get_session_info(spec.session_market, now)
    if not session.is_open:
        return False, "SESSION_CLOSED"
    age = (now - quote.as_of).total_seconds()
    if age > MAX_QUOTE_AGE_SEC:
        return False, "STALE_QUOTE"
    if -age > MAX_QUOTE_FUTURE_SEC:
        return False, "FUTURE_QUOTE"
    if quote.previous_close is None:
        return False, "NO_PREVIOUS_CLOSE"
    return True, "OK"


def watch_metric(condition_type: str, quote: Quote) -> Decimal | None:
    if condition_type == CONDITION_CHANGE_PCT:
        return quote.change_pct
    if condition_type == CONDITION_CHANGE_BP:
        return quote.change_bp
    if condition_type == CONDITION_LEVEL:
        return quote.value
    return None


# =============================================================================
# Pure crossing / confirmation / hysteresis state machine
# =============================================================================


@dataclass
class WatchState:
    has_baseline: bool
    is_armed: bool
    armed_side: str
    confirm_streak: int
    confirm_side: str


def _level_buffer(threshold: Decimal, quote_unit: str) -> Decimal:
    if quote_unit == "PERCENT_YIELD":
        return Decimal("0.02")  # 2bp
    return threshold * Decimal("0.002")  # 0.2%


def _baseline_side(cond: str, direction: str, m: Decimal, t: Decimal) -> str | None:
    """Armed side if the baseline reading is NOT already in breach, else None (disarmed)."""
    if cond == CONDITION_LEVEL:
        if direction == "UP":
            return "UP" if m < t else None
        if direction == "DOWN":
            return "DOWN" if m > t else None
        if m < t:
            return "UP"
        if m > t:
            return "DOWN"
        return None
    if direction == "UP":
        return "UP" if m < t else None
    if direction == "DOWN":
        return "DOWN" if m > -t else None
    return "BOTH" if -t < m < t else None


def _rearm_side(cond: str, direction: str, m: Decimal, t: Decimal, quote_unit: str) -> str | None:
    """Armed side once the reading returned across the hysteresis band, else None."""
    if cond == CONDITION_LEVEL:
        buf = _level_buffer(t, quote_unit)
        if direction in ("UP", "BOTH") and m <= t - buf:
            return "UP"
        if direction in ("DOWN", "BOTH") and m >= t + buf:
            return "DOWN"
        return None
    half = t / 2
    if direction == "UP":
        return "UP" if m <= half else None
    if direction == "DOWN":
        return "DOWN" if m >= -half else None
    return "BOTH" if abs(m) <= half else None


def _breach_side(cond: str, armed_side: str, m: Decimal, t: Decimal) -> str | None:
    if cond == CONDITION_LEVEL:
        if armed_side == "UP" and m >= t:
            return "UP"
        if armed_side == "DOWN" and m <= t:
            return "DOWN"
        return None
    if armed_side in ("UP", "BOTH") and m >= t:
        return "UP"
    if armed_side in ("DOWN", "BOTH") and m <= -t:
        return "DOWN"
    return None


def evaluate_reading(
    state: WatchState,
    *,
    condition_type: str,
    direction: str,
    threshold: Decimal,
    quote_unit: str,
    metric: Decimal,
    is_new_session: bool,
    required_confirmations: int = REQUIRED_CONFIRMATIONS,
) -> tuple[WatchState, str | None]:
    """Applies one *distinct valid* reading. Returns (new_state, fired_side | None)."""
    cond, t, m = condition_type, threshold, metric
    if not state.has_baseline:
        side = _baseline_side(cond, direction, m, t)
        return WatchState(True, side is not None, side or direction, 0, ""), None

    st = WatchState(True, state.is_armed, state.armed_side, state.confirm_streak, state.confirm_side)
    if is_new_session:
        st.confirm_streak, st.confirm_side = 0, ""
        if cond != CONDITION_LEVEL:
            # CHANGE_* restarts from the new previous close each session -> re-arm on the configured direction.
            st.is_armed, st.armed_side = True, direction

    if not st.is_armed:
        side = _rearm_side(cond, direction, m, t, quote_unit)
        if side is not None:
            st.is_armed, st.armed_side = True, side
        st.confirm_streak, st.confirm_side = 0, ""
        return st, None

    side = _breach_side(cond, st.armed_side, m, t)
    if side is None:
        st.confirm_streak, st.confirm_side = 0, ""
        return st, None
    if st.confirm_side == side:
        st.confirm_streak += 1
    else:
        st.confirm_streak, st.confirm_side = 1, side
    if st.confirm_streak >= required_confirmations:
        st.is_armed, st.confirm_streak, st.confirm_side = False, 0, ""
        return st, side
    return st, None


# =============================================================================
# Rendering helpers ([TG-01] / [TG-02] / [TG-03])
# =============================================================================


def _fmt_value(value: Decimal, quote_unit: str) -> str:
    q = Decimal("0.001") if quote_unit == "PERCENT_YIELD" else Decimal("0.01")
    v = value.quantize(q, rounding=ROUND_HALF_UP)
    text = f"{v:,f}"
    return f"{text}%" if quote_unit == "PERCENT_YIELD" else text


def _fmt_threshold(value: Decimal) -> str:
    return f"{value.normalize():,f}" if value == value.to_integral_value() else f"{value.normalize():f}"


def describe_watch(w: dict[str, Any]) -> str:
    """Plain-text (unescaped) one-line description of a watch."""
    spec = ASSET_CATALOG.get(w["symbol"])
    label = spec.label if spec else w["symbol"]
    t = Decimal(str(w["threshold_value"]))
    direction = w["direction"]
    if w["condition_type"] == CONDITION_LEVEL:
        suffix = "%" if w.get("quote_unit") == "PERCENT_YIELD" else ""
        op = {"UP": "이상", "DOWN": "이하", "BOTH": "돌파(양방향)"}[direction]
        return f"{label} {_fmt_threshold(t)}{suffix} {op}"
    unit = "bp" if w["condition_type"] == CONDITION_CHANGE_BP else "%"
    sign = {"UP": "+", "DOWN": "-", "BOTH": "±"}[direction]
    return f"{label} {sign}{_fmt_threshold(t)}{unit} ({DIRECTION_LABELS[direction]})"


def visible_len(html_text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def render_watch_alert_html(items: list[dict[str, Any]], now: datetime) -> str:
    """Fixed slim template, one line per triggered watch. Max 20 watches/user keeps it < 1800 visible chars."""
    lines = ["🔔 <b>[내 변동성 타겟 알림]</b>", "━━━━━━━━━━━━━━━━━━"]
    for it in items:
        w, q, side = it["watch"], it["quote"], it["side"]
        spec = ASSET_CATALOG.get(w["symbol"])
        emoji = spec.emoji if spec else "📊"
        arrow = "📈" if side == "UP" else "📉"
        unit = w.get("quote_unit") or (spec.quote_unit if spec else "INDEX")
        if w["condition_type"] == CONDITION_CHANGE_BP and q.change_bp is not None:
            chg = f"{q.change_bp.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP):+f}bp"
        elif q.change_pct is not None:
            chg = f"{q.change_pct.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):+f}%"
        else:
            chg = "-"
        lines.append(
            f"{arrow}{emoji} <b>{html.escape(spec.label if spec else w['symbol'])}</b> "
            f"{html.escape(_fmt_value(q.value, unit))} (<b>{html.escape(chg)}</b>)"
        )
        lines.append(f"   ↳ 조건: {html.escape(describe_watch(w))} · {DIRECTION_LABELS.get(side, side)} 도달")
    lines.append("")
    lines.append(f"🕒 {html.escape(now.astimezone(KST).strftime('%Y-%m-%d %H:%M'))} KST 실시간 기준")
    lines.append("⚙️ 조건 변경·삭제: /watch")
    text = "\n".join(lines)
    if visible_len(text) > MAX_VISIBLE_CHARS or len(text) > MAX_HTML_CHARS:
        raise ValueError("watch alert exceeds [TG-01] limits")
    return text


# =============================================================================
# Telegram sender (SafeTelegramDispatcher: 429 / timeout UNKNOWN isolation)
# =============================================================================


class _HtmlSendAdapter:
    """Adapts `TelegramBotTransport.send_html` to the dispatcher's `tg.send(chat_id, payload)` contract."""

    def __init__(self, transport):
        self.transport = transport

    def send(self, chat_id: str, payload: Any) -> str:
        res = self.transport.send_html(chat_id, str(payload))
        return str(((res or {}).get("result") or {}).get("message_id") or "")


class TelegramWatchSender:
    def __init__(self, token: str, dispatcher=None, transport=None):
        from market_brief.channels.telegram_limiter import SafeTelegramDispatcher, TelegramRateLimiter
        from market_brief.multi_bot_manager import TelegramBotTransport

        self.adapter = _HtmlSendAdapter(transport or TelegramBotTransport(token))
        self.dispatcher = dispatcher or SafeTelegramDispatcher(
            rate_limiter=TelegramRateLimiter(max_global_rate=25.0, min_chat_interval=1.0)
        )

    def __call__(self, chat_id: str, html_text: str) -> tuple[str, str | None, str | None]:
        from market_brief.storage.models import Recipient

        recipient = Recipient(chat_id=str(chat_id), name="watch", subscribed_topics="FINANCE")
        return self.dispatcher.send_to_recipient(self.adapter, recipient, html_text, db=None)


# =============================================================================
# Evaluator (one tick)
# =============================================================================


SendFn = Callable[[str, str], tuple[str, "str | None", "str | None"]]


@dataclass
class WatchTickResult:
    evaluated: int = 0
    fired: int = 0
    claimed: int = 0
    sent: int = 0
    failed: int = 0
    unknown: int = 0
    rejected_quotes: dict[str, str] = field(default_factory=dict)
    messages: list[tuple[str, str]] = field(default_factory=list)
    skipped_reason: str = ""


class MarketWatchEvaluator:
    """Evaluates all active watches once: symbol-union fetch -> live/session/freshness gate -> crossing -> claim -> send."""

    def __init__(
        self,
        db: MarketBriefDatabase,
        *,
        provider=None,
        calendar: MarketCalendar | None = None,
        sender: SendFn | None = None,
        runner_id: str | None = None,
        shadow: bool = False,
    ):
        self.db = db
        self.provider = provider
        self.calendar = calendar or MarketCalendar()
        self.sender = sender
        self.runner_id = runner_id or f"mw_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self.shadow = shadow or sender is None

    def _fetch(self, symbols: list[str]) -> list[Quote]:
        provider = self.provider
        if provider is None:
            from market_brief.market_data.naver_provider import NaverFinanceProvider

            provider = self.provider = NaverFinanceProvider()
        quotes: list[Quote] = []
        for sym in symbols:  # per-symbol isolation, NO fixture fallback
            try:
                quotes.extend(provider.fetch([sym]))
            except Exception as exc:
                logger.debug("[MarketWatch] fetch failed for %s: %s", sym, exc)
        return quotes

    def run_once(self, now: datetime | None = None) -> WatchTickResult:
        now = now or datetime.now(KST)
        result = WatchTickResult()
        watches = self.db.list_evaluable_market_watches()
        if not watches:
            result.skipped_reason = "NO_ACTIVE_WATCHES"
            return result

        sessions = {m: self.calendar.get_session_info(m, now) for m in ("KR", "US", "GLOBAL")}
        symbols = sorted(
            {w["symbol"] for w in watches if w["symbol"] in ASSET_CATALOG and sessions[ASSET_CATALOG[w["symbol"]].session_market].is_open}
        )
        if not symbols:
            result.skipped_reason = "ALL_SESSIONS_CLOSED"
            return result

        valid: dict[str, Quote] = {}
        for q in self._fetch(symbols):
            ok, reason = check_live_quote(q, now, self.calendar)
            if ok:
                valid[q.symbol] = q
            else:
                result.rejected_quotes[q.symbol] = reason

        fired_by_chat: dict[str, list[dict[str, Any]]] = {}
        for w in watches:
            q = valid.get(w["symbol"])
            if q is None:
                continue  # stale / fallback / closed readings never count toward streaks
            as_of_iso = q.as_of.isoformat()
            if w.get("last_observed_as_of") == as_of_iso:
                continue  # same reading already counted (no double-counting)
            metric = watch_metric(w["condition_type"], q)
            if metric is None:
                continue
            spec = ASSET_CATALOG[w["symbol"]]
            session = sessions[spec.session_market]
            trade_date = session.trade_date.isoformat()
            state = WatchState(
                has_baseline=bool(w.get("last_observed_as_of")),
                is_armed=bool(w["is_armed"]),
                armed_side=w.get("armed_side") or w["direction"],
                confirm_streak=int(w.get("confirm_streak") or 0),
                confirm_side=w.get("confirm_side") or "",
            )
            is_new_session = state.has_baseline and (w.get("last_observed_trade_date") or "") != trade_date
            new_state, fired_side = evaluate_reading(
                state,
                condition_type=w["condition_type"],
                direction=w["direction"],
                threshold=Decimal(str(w["threshold_value"])),
                quote_unit=w["quote_unit"],
                metric=metric,
                is_new_session=is_new_session,
            )
            result.evaluated += 1
            change = q.change_bp if w["condition_type"] == CONDITION_CHANGE_BP else q.change_pct
            persisted = self.db.update_market_watch_state(
                int(w["watch_id"]),
                int(w["condition_version"]),
                is_armed=new_state.is_armed,
                armed_side=new_state.armed_side,
                confirm_streak=new_state.confirm_streak,
                confirm_side=new_state.confirm_side,
                last_observed_value=dec_str(q.value),
                last_observed_change=dec_str(change) if change is not None else None,
                last_observed_as_of=as_of_iso,
                last_observed_trade_date=trade_date,
            )
            if fired_side and persisted:
                result.fired += 1
                fired_by_chat.setdefault(str(w["chat_id"]), []).append(
                    {
                        "watch": w,
                        "quote": q,
                        "side": fired_side,
                        "session_id": f"{spec.session_market}:{trade_date}",
                        "trade_date": trade_date,
                        "prior_armed_side": state.armed_side,
                        "change": change,
                    }
                )

        for chat_id, items in fired_by_chat.items():
            self._deliver_group(chat_id, items, now, result)
        return result

    def _deliver_group(self, chat_id: str, items: list[dict[str, Any]], now: datetime, result: WatchTickResult) -> None:
        if self.shadow:
            logger.info("[MarketWatch][SHADOW] %d trigger(s) for chat %s (no send)", len(items), chat_id)
            return
        claimed: list[tuple[dict[str, Any], str, str]] = []
        for it in items:
            w = it["watch"]
            key = f"mw:v1:{w['watch_id']}:{it['session_id']}:{w['condition_version']}"
            ok, reason, token = self.db.claim_market_watch_delivery(
                delivery_key=key,
                watch_id=int(w["watch_id"]),
                chat_id=chat_id,
                symbol=w["symbol"],
                market=ASSET_CATALOG[w["symbol"]].session_market,
                session_id=it["session_id"],
                trade_date=it["trade_date"],
                trigger_side=it["side"],
                observed_value=dec_str(it["quote"].value),
                observed_change=dec_str(it["change"]) if it["change"] is not None else None,
                quote_as_of=it["quote"].as_of.isoformat(),
                lease_owner=self.runner_id,
            )
            if ok and token:
                claimed.append((it, key, token))
            else:
                logger.info("[MarketWatch] claim rejected %s: %s", key, reason)
        if not claimed:
            return
        result.claimed += len(claimed)
        html_text = render_watch_alert_html([c[0] for c in claimed], now)
        try:
            status, msg_id, err = self.sender(chat_id, html_text)  # type: ignore[misc]
        except Exception as exc:  # ambiguous -> never auto-resend
            status, msg_id, err = "UNKNOWN", None, f"SENDER_EXCEPTION: {exc}"
        status = status if status in {"SENT", "FAILED", "UNKNOWN"} else "UNKNOWN"
        result.messages.append((chat_id, html_text))
        triggered_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for it, key, token in claimed:
            self.db.finalize_market_watch_delivery(key, token, status, provider_msg_id=msg_id, error_detail=err)
            w = it["watch"]
            current = self.db.get_market_watch(int(w["watch_id"])) or {}
            if status == "FAILED":
                # Explicit FAILED only: re-arm so a later *confirmed* crossing may re-claim (bounded attempts).
                self.db.update_market_watch_state(
                    int(w["watch_id"]),
                    int(w["condition_version"]),
                    is_armed=True,
                    armed_side=it["prior_armed_side"],
                    confirm_streak=0,
                    confirm_side="",
                    last_observed_value=current.get("last_observed_value"),
                    last_observed_change=current.get("last_observed_change"),
                    last_observed_as_of=current.get("last_observed_as_of"),
                    last_observed_trade_date=current.get("last_observed_trade_date"),
                )
            else:
                self.db.update_market_watch_state(
                    int(w["watch_id"]),
                    int(w["condition_version"]),
                    is_armed=bool(current.get("is_armed")),
                    armed_side=current.get("armed_side") or w["direction"],
                    confirm_streak=int(current.get("confirm_streak") or 0),
                    confirm_side=current.get("confirm_side") or "",
                    last_observed_value=current.get("last_observed_value"),
                    last_observed_change=current.get("last_observed_change"),
                    last_observed_as_of=current.get("last_observed_as_of"),
                    last_observed_trade_date=current.get("last_observed_trade_date"),
                    last_triggered_at=triggered_at,
                )
        if status == "SENT":
            result.sent += 1
        elif status == "FAILED":
            result.failed += 1
        else:
            result.unknown += 1


# =============================================================================
# Telegram UX service (`/watch`, `/alert`, `mbw:*` callbacks)
# =============================================================================


def _cb(data: str) -> str:
    if len(data.encode("utf-8")) > 64 or not data.isascii():
        raise ValueError(f"callback_data violates [TG-04]: {data!r}")
    return data


class MarketWatchService:
    """Pure UI/state service. Callers MUST pass the authenticated `from.id` as `uid` (ownership guard)."""

    def __init__(self, db: MarketBriefDatabase):
        self.db = db
        self._pending_level: dict[str, tuple[str, float]] = {}
        self._pending_value: dict[str, tuple[str, Decimal, float]] = {}

    # ------------------------------------------------------------------ helpers
    def _limit(self, uid: str) -> int:
        with self.db._connect() as conn:
            row = conn.execute("SELECT tier FROM users WHERE telegram_user_id = ?", (uid,)).fetchone()
        return MARKET_WATCH_TIER_LIMITS.get(((row["tier"] if row else None) or "WHITE").upper(), 3)

    @staticmethod
    def _out(html_text: str, kb: list[list[dict[str, Any]]], toast: str, code: str) -> dict[str, Any]:
        return {"html": html_text, "inline_keyboard": kb, "toast": toast, "code": code}

    def has_pending_input(self, uid: str) -> bool:
        item = self._pending_level.get(uid)
        if item and (time.monotonic() - item[1]) <= LEVEL_INPUT_TTL_SEC:
            return True
        self._pending_level.pop(uid, None)
        return False

    def _create(self, uid: str, chat_id: str, spec: AssetSpec, cond: str, direction: str, threshold: Decimal) -> dict[str, Any]:
        unit = spec.change_unit if cond != CONDITION_LEVEL else spec.quote_unit
        return self.db.create_market_watch(
            uid,
            chat_id,
            symbol=spec.symbol,
            market=spec.session_market,
            quote_unit=spec.quote_unit,
            condition_type=cond,
            direction=direction,
            threshold_value=dec_str(threshold),
            threshold_unit=unit,
        )

    def _created_reply(self, res: dict[str, Any], desc: str) -> dict[str, Any]:
        back = [[{"text": "📋 내 타겟 목록", "callback_data": _cb("mbw:m")}, {"text": "➕ 추가 설정", "callback_data": _cb("mbw:a")}]]
        if res["success"]:
            body = (
                "✅ <b>[변동성 타겟 저장 완료]</b>\n"
                f"🎯 {html.escape(desc)}\n"
                f"📊 사용 중: <b>{res['active_count']} / {res['limit']}</b>개\n"
                "💡 <i>저장 시점에 이미 조건을 만족해도 소급 발송하지 않으며, 새로 돌파할 때 알려드립니다.</i>"
            )
            return self._out(body, back, "✅ 저장되었습니다.", "WATCH_CREATED")
        if res["code"] == "DUPLICATE":
            return self._out(f"ℹ️ 이미 같은 조건이 등록되어 있습니다.\n🎯 {html.escape(desc)}", back, "이미 등록된 조건입니다.", "WATCH_DUPLICATE")
        if res["code"] == "LIMIT_EXCEEDED":
            return self._out(
                f"🚫 <b>등급별 타겟 한도 초과</b>\n현재 <b>{res['active_count']} / {res['limit']}</b>개 사용 중입니다.\n기존 타겟을 삭제한 뒤 다시 시도해 주세요.",
                back,
                "⚠️ 한도를 초과했습니다.",
                "WATCH_LIMIT_EXCEEDED",
            )
        return self._out("⚠️ 타겟을 저장할 수 없습니다.", back, "저장 실패", res.get("code") or "WATCH_ERROR")

    # ------------------------------------------------------------------ screens
    def render_home(self, uid: str, notice: str = "") -> dict[str, Any]:
        watches = self.db.list_market_watches(uid)
        limit = self._limit(uid)
        lines = []
        if notice:
            lines += [notice, "━━━━━━━━━━━━━━━━━━"]
        lines += ["🔔 <b>[내 장중 변동성 타겟]</b>", f"📊 사용 중: <b>{len(watches)} / {limit}</b>개", ""]
        kb: list[list[dict[str, Any]]] = []
        if watches:
            for i, w in enumerate(watches, 1):
                lines.append(f"{i}. {html.escape(describe_watch(w))}")
                kb.append([{"text": f"🗑 {i}번 삭제", "callback_data": _cb(f"mbw:x:{int(w['watch_id'])}")}])
        else:
            lines.append("등록된 타겟이 없습니다. 원하는 자산과 조건을 골라 주세요.")
        lines += ["", "💡 <i>정기 06:30/15:40 브리핑은 그대로 받고, 장중 알림은 설정한 조건에만 발송됩니다.</i>"]
        kb.insert(0, [{"text": "➕ 새 타겟 추가", "callback_data": _cb("mbw:a")}, {"text": "⚡ ±3% 기본 알림", "callback_data": _cb("mbw:q3")}])
        return self._out("\n".join(lines), kb, "", "WATCH_HOME")

    def render_asset_picker(self) -> dict[str, Any]:
        kb: list[list[dict[str, Any]]] = []
        row: list[dict[str, Any]] = []
        for spec in ASSET_CATALOG.values():
            row.append({"text": f"{spec.emoji} {spec.label}", "callback_data": _cb(f"mbw:s:{spec.symbol}")})
            if len(row) == 3:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        kb.append([{"text": "⬅️ 내 타겟 목록", "callback_data": _cb("mbw:m")}])
        return self._out("🎯 <b>[1단계] 감시할 자산을 선택하세요</b>", kb, "", "WATCH_PICK_ASSET")

    def render_threshold_picker(self, spec: AssetSpec) -> dict[str, Any]:
        unit = "bp" if spec.quote_unit == "PERCENT_YIELD" else "%"
        row = [
            {"text": f"±{_fmt_threshold(v)}{unit}", "callback_data": _cb(f"mbw:t:{spec.symbol}:{code}")}
            for code, v in presets_for(spec).items()
        ]
        kb = [row, [{"text": "✏️ 목표가 직접입력", "callback_data": _cb(f"mbw:l:{spec.symbol}")}], [{"text": "⬅️ 자산 다시 선택", "callback_data": _cb("mbw:a")}]]
        basis = "전일 대비 금리 변동폭" if spec.quote_unit == "PERCENT_YIELD" else "전일 대비 등락률"
        body = f"📐 <b>[2단계] {html.escape(spec.label)} 조건 선택</b>\n• 빠른 설정: {basis}\n• 직접입력: 목표 가격/지수/금리 레벨"
        return self._out(body, kb, "", "WATCH_PICK_THRESHOLD")

    def render_direction_picker(self, spec: AssetSpec, prefix: str, desc: str) -> dict[str, Any]:
        kb = [
            [
                {"text": "📈 상승", "callback_data": _cb(f"{prefix}:U")},
                {"text": "📉 하락", "callback_data": _cb(f"{prefix}:D")},
                {"text": "↕️ 양방향", "callback_data": _cb(f"{prefix}:B")},
            ],
            [{"text": "⬅️ 조건 다시 선택", "callback_data": _cb(f"mbw:s:{spec.symbol}")}],
        ]
        return self._out(f"🧭 <b>[3단계] 방향 선택</b>\n🎯 {html.escape(desc)}", kb, "", "WATCH_PICK_DIRECTION")

    # ------------------------------------------------------------------ entry points
    def handle_callback(self, uid: str, chat_id: str, data: str) -> dict[str, Any]:
        uid = str(uid).strip()
        parts = (data or "").split(":")
        if len(parts) < 2 or parts[0] != "mbw":
            return self.render_home(uid)
        action = parts[1]
        # Navigating away from a manual-level flow must drop stale pending input,
        # otherwise has_pending_input() keeps intercepting chat numbers until TTL.
        if action != "l":
            self._pending_level.pop(uid, None)
        if action != "ld":
            self._pending_value.pop(uid, None)

        if action == "m":
            return self.render_home(uid)
        if action == "a":
            return self.render_asset_picker()
        if action == "q3":
            created, notes = 0, []
            for sym in QUICK_DEFAULT_SYMBOLS:
                spec = ASSET_CATALOG[sym]
                res = self._create(uid, chat_id, spec, spec.change_condition, "BOTH", Decimal("3.0"))
                if res["success"]:
                    created += 1
                    notes.append(f"✅ {spec.label} ±3%")
                elif res["code"] == "DUPLICATE":
                    notes.append(f"ℹ️ {spec.label} ±3% (이미 등록)")
                elif res["code"] == "LIMIT_EXCEEDED":
                    notes.append(f"🚫 {spec.label} ±3% (한도 초과)")
            notice = "⚡ <b>±3% 기본 알림</b>\n" + "\n".join(html.escape(n) for n in notes)
            out = self.render_home(uid, notice=notice)
            out["toast"] = f"⚡ ±3% 기본 알림 {created}건 등록" if created else "변경 사항이 없습니다."
            out["code"] = "WATCH_QUICK_DEFAULT"
            return out

        spec = ASSET_CATALOG.get(parts[2]) if len(parts) >= 3 else None
        if action in {"s", "t", "d", "l", "ld"} and spec is None:
            return self.render_asset_picker()

        if action == "s":
            return self.render_threshold_picker(spec)  # type: ignore[arg-type]
        if action == "t" and len(parts) == 4:
            presets = presets_for(spec)  # type: ignore[arg-type]
            if parts[3] not in presets:
                return self.render_threshold_picker(spec)  # type: ignore[arg-type]
            unit = "bp" if spec.quote_unit == "PERCENT_YIELD" else "%"  # type: ignore[union-attr]
            desc = f"{spec.label} ±{_fmt_threshold(presets[parts[3]])}{unit}"  # type: ignore[union-attr]
            return self.render_direction_picker(spec, f"mbw:d:{spec.symbol}:{parts[3]}", desc)  # type: ignore[arg-type,union-attr]
        if action == "d" and len(parts) == 5:
            presets = presets_for(spec)  # type: ignore[arg-type]
            direction = DIRECTION_CODES.get(parts[4])
            if parts[3] not in presets or direction is None:
                return self.render_threshold_picker(spec)  # type: ignore[arg-type]
            res = self._create(uid, chat_id, spec, spec.change_condition, direction, presets[parts[3]])  # type: ignore[arg-type,union-attr]
            desc = describe_watch(
                {"symbol": spec.symbol, "condition_type": spec.change_condition, "direction": direction,  # type: ignore[union-attr]
                 "threshold_value": dec_str(presets[parts[3]]), "quote_unit": spec.quote_unit}  # type: ignore[union-attr]
            )
            return self._created_reply(res, desc)
        if action == "l":
            self._pending_level[uid] = (spec.symbol, time.monotonic())  # type: ignore[union-attr]
            self._pending_value.pop(uid, None)
            hint = "예: 3.25 (금리 %)" if spec.quote_unit == "PERCENT_YIELD" else "예: 1400"  # type: ignore[union-attr]
            body = f"✏️ <b>{html.escape(spec.label)} 목표값 직접입력</b>\n채팅창에 숫자만 입력해 주세요. ({hint})"  # type: ignore[union-attr]
            kb = [[{"text": "↩️ 입력 취소", "callback_data": _cb("mbw:m")}]]
            return self._out(body, kb, "✏️ 목표값을 입력해 주세요.", "WATCH_AWAIT_LEVEL")
        if action == "ld" and len(parts) == 4:
            pending = self._pending_value.get(uid)
            direction = DIRECTION_CODES.get(parts[3])
            if not pending or pending[0] != spec.symbol or direction is None or (time.monotonic() - pending[2]) > LEVEL_INPUT_TTL_SEC:  # type: ignore[union-attr]
                self._pending_value.pop(uid, None)
                out = self.render_home(uid, notice="⌛ 입력이 만료되었습니다. 다시 설정해 주세요.")
                out["code"] = "WATCH_INPUT_EXPIRED"
                return out
            self._pending_value.pop(uid, None)
            res = self._create(uid, chat_id, spec, CONDITION_LEVEL, direction, pending[1])  # type: ignore[arg-type]
            desc = describe_watch(
                {"symbol": spec.symbol, "condition_type": CONDITION_LEVEL, "direction": direction,  # type: ignore[union-attr]
                 "threshold_value": dec_str(pending[1]), "quote_unit": spec.quote_unit}  # type: ignore[union-attr]
            )
            return self._created_reply(res, desc)
        if action == "x" and len(parts) == 3 and parts[2].isdigit():
            w = self.db.get_market_watch(int(parts[2]))
            if not w or str(w["telegram_user_id"]) != uid or not w["is_active"]:
                out = self.render_home(uid, notice="⚠️ 삭제할 수 없는 타겟입니다.")
                out["code"] = "WATCH_NOT_OWNED"
                return out
            kb = [[
                {"text": "🗑 삭제 확정", "callback_data": _cb(f"mbw:xy:{int(w['watch_id'])}")},
                {"text": "↩️ 취소", "callback_data": _cb("mbw:m")},
            ]]
            return self._out(f"❓ 아래 타겟을 삭제할까요?\n🎯 {html.escape(describe_watch(w))}", kb, "", "WATCH_DELETE_CONFIRM")
        if action == "xy" and len(parts) == 3 and parts[2].isdigit():
            ok = self.db.deactivate_market_watch(uid, int(parts[2]))
            out = self.render_home(uid, notice="🗑 삭제되었습니다." if ok else "⚠️ 삭제할 수 없는 타겟입니다.")
            out["toast"] = "🗑 삭제되었습니다." if ok else "삭제할 수 없습니다."
            out["code"] = "WATCH_DELETED" if ok else "WATCH_NOT_OWNED"
            return out
        return self.render_home(uid)

    def handle_level_text(self, uid: str, text: str) -> dict[str, Any]:
        uid = str(uid).strip()
        pending = self._pending_level.get(uid)
        if not pending:
            return self.render_home(uid)
        spec = ASSET_CATALOG[pending[0]]
        try:
            value = parse_level_input(text)
        except ValueError as err:
            kb = [[{"text": "↩️ 입력 취소", "callback_data": _cb("mbw:m")}]]
            return self._out(f"⚠️ {html.escape(str(err))}\n다시 입력해 주세요.", kb, "", "WATCH_LEVEL_INVALID")
        self._pending_level.pop(uid, None)
        self._pending_value[uid] = (spec.symbol, value, time.monotonic())
        suffix = "%" if spec.quote_unit == "PERCENT_YIELD" else ""
        desc = f"{spec.label} 목표 {_fmt_threshold(value)}{suffix}"
        return self.render_direction_picker(spec, f"mbw:ld:{spec.symbol}", desc)

    def deactivate_all(self, uid: str, reason: str) -> int:
        self._pending_level.pop(str(uid), None)
        self._pending_value.pop(str(uid), None)
        return self.db.deactivate_all_market_watches(str(uid), reason=reason)


def iter_callback_samples() -> Iterable[str]:
    """Every callback_data shape the watch UI can emit (used by [TG-04] byte-length tests)."""
    yield from ("mbw:m", "mbw:a", "mbw:q3")
    for spec in ASSET_CATALOG.values():
        yield f"mbw:s:{spec.symbol}"
        yield f"mbw:l:{spec.symbol}"
        for code in presets_for(spec):
            yield f"mbw:t:{spec.symbol}:{code}"
            for d in DIRECTION_CODES:
                yield f"mbw:d:{spec.symbol}:{code}:{d}"
        for d in DIRECTION_CODES:
            yield f"mbw:ld:{spec.symbol}:{d}"
    yield "mbw:x:999999999"
    yield "mbw:xy:999999999"
