"""Channel-neutral market briefing models and alert policy."""
from dataclasses import dataclass
from datetime import date
from typing import Protocol

@dataclass(frozen=True)
class AssetSummary:
    label: str
    indicator: str
    change: str
    keyword: str

@dataclass(frozen=True)
class Briefing:
    kind: str  # morning, closing, urgent
    headline: str
    as_of: str
    assets: tuple[AssetSummary, ...]
    report_url: str

class DeliveryChannel(Protocol):
    def send(self, recipient: str, briefing: Briefing) -> str: ...


def is_alert_threshold_met(change_pct: float) -> bool:
    return abs(change_pct) >= 3.0


def alert_key(market: str, market_trade_date: date) -> str:
    if market not in {"KR", "US"}:
        raise ValueError("market must be KR or US")
    return f"{market}:{market_trade_date.isoformat()}"


def format_briefing(briefing: Briefing) -> str:
    items = [f"[{briefing.kind.upper()}] {briefing.headline}", f"기준: {briefing.as_of}"]
    items += [f"{a.label} | {a.indicator} {a.change} | {a.keyword}" for a in briefing.assets]
    items += [f"상세 리포트: {briefing.report_url}"]
    return "\n".join(items)
