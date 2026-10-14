"""Provider-agnostic normalized financial observations (stdlib only)."""
from dataclasses import dataclass, asdict
from datetime import datetime, date, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

ASSET_CLASSES = frozenset({'KR_EQUITY','KR_BOND','US_EQUITY','US_BOND','ALTERNATIVE'})

class DataValidationError(ValueError):
    pass


def parse_decimal(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DataValidationError('invalid numeric value') from exc
    if not result.is_finite():
        raise DataValidationError('NaN and infinity are forbidden')
    return result


@dataclass(frozen=True)
class Quote:
    symbol: str
    asset_class: str
    market: str
    session: Literal['REGULAR', 'REFERENCE', 'INDICATIVE']
    trade_date: date
    as_of: datetime
    value: Decimal
    previous_close: Decimal | None
    unit: Literal['INDEX', 'PERCENT_YIELD', 'PRICE', 'FX']
    source: str
    delayed_minutes: int = 0
    is_final: bool = False

    def __post_init__(self):
        if not self.symbol or not self.source or self.asset_class not in ASSET_CLASSES:
            raise DataValidationError('invalid instrument or source')
        if self.market not in {'KR','US','GLOBAL'}:
            raise DataValidationError('invalid market')
        if self.session not in {'REGULAR','REFERENCE','INDICATIVE'}:
            raise DataValidationError('invalid session')
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise DataValidationError('timestamp must be timezone aware')
        if self.delayed_minutes < 0:
            raise DataValidationError('negative delay')
        for x in (self.value, self.previous_close):
            if x is not None and not x.is_finite():
                raise DataValidationError('nonfinite prices forbidden')
        if self.unit != 'PERCENT_YIELD' and self.value <= 0:
            raise DataValidationError('non-positive price/index')
        if self.previous_close is not None and self.previous_close <= 0 and self.unit != 'PERCENT_YIELD':
            raise DataValidationError('non-positive previous close')
        if self.trade_date > self.as_of.date():
            raise DataValidationError('future trade date compared to source timestamp')

    @property
    def change_pct(self) -> Decimal | None:
        if self.previous_close is None or self.unit == 'PERCENT_YIELD':
            return None
        return (self.value / self.previous_close - 1) * 100

    @property
    def change_bp(self) -> Decimal | None:
        if self.previous_close is None or self.unit != 'PERCENT_YIELD':
            return None
        return (self.value - self.previous_close) * 100

    def to_record(self) -> dict:
        return {**asdict(self), 'trade_date': self.trade_date.isoformat(),
                'as_of': self.as_of.isoformat(), 'value': str(self.value),
                'previous_close': str(self.previous_close) if self.previous_close is not None else None}


def from_record(record: dict) -> Quote:
    try:
        return Quote(symbol=record['symbol'], asset_class=record['asset_class'],
                     market=record['market'], session=record['session'],
                     trade_date=date.fromisoformat(record['trade_date']),
                     as_of=datetime.fromisoformat(record['as_of'].replace('Z','+00:00')),
                     value=parse_decimal(record['value']),
                     previous_close=parse_decimal(record['previous_close']) if record.get('previous_close') is not None else None,
                     unit=record['unit'], source=record['source'],
                     delayed_minutes=int(record.get('delayed_minutes',0)),
                     is_final=bool(record.get('is_final',False)))
    except (KeyError, ValueError, TypeError) as exc:
        raise DataValidationError(f'bad observation: {exc}') from exc
