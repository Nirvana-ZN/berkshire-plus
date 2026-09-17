"""Pure common safety classification for frozen weekly discovery inputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import isfinite

from discovery.snapshot import FrozenCompanyInput, FrozenValue
from ledger.quotes import is_a_share_code
from market.cheap import CheapRow, security_state_eligible


class _ImmutableMetrics(dict[str, object]):
    def _immutable(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("common metrics are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


@dataclass(frozen=True)
class CommonVerdict:
    code: str
    verdict: str
    reason: str | None
    metrics: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.code) is not str:
            raise TypeError("common code must be a string")
        if self.verdict not in {"pass", "drop", "insufficient"}:
            raise ValueError("unknown common verdict")
        if self.reason is not None and type(self.reason) is not str:
            raise TypeError("common reason must be a string or None")
        if not isinstance(self.metrics, Mapping) or any(
            type(key) is not str
            or type(value) not in {type(None), bool, int, float, str}
            or (type(value) is float and not isfinite(value))
            for key, value in self.metrics.items()
        ):
            raise TypeError("common metrics must contain plain JSON values")
        object.__setattr__(self, "metrics", _ImmutableMetrics(self.metrics))


def _nonempty_text(value: object) -> str | None:
    return value if type(value) is str and bool(value.strip()) else None


def _iso_date(value: object) -> str | None:
    if type(value) is not str:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.isoformat() == value else None


def _calendar(
    trading_dates: Mapping[str, Sequence[str]],
    exchange: str,
) -> tuple[str, ...] | None:
    if not isinstance(trading_dates, Mapping) or exchange not in trading_dates:
        return None
    raw = trading_dates[exchange]
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        return ()
    values = tuple(raw)
    if (
        not values
        or any(_iso_date(item) is None for item in values)
        or tuple(sorted(values)) != values
        or len(set(values)) != len(values)
    ):
        return ()
    return values


def _price(value: FrozenValue | None) -> Decimal | None:
    if type(value) is bool or value is None:
        return None
    if type(value) not in {int, float, str}:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _metrics(
    *,
    exchange: object = None,
    trading_status: object = None,
    last_trade_date: object = None,
    latest_trading_date: object = None,
    price: Decimal | None = None,
    price_as_of: object = None,
    industry: object = None,
) -> Mapping[str, object]:
    values = {
        "exchange": exchange,
        "trading_status": trading_status,
        "last_trade_date": last_trade_date,
        "latest_trading_date": latest_trading_date,
        "price": _decimal_text(price) if price is not None else None,
        "price_as_of": price_as_of,
        "industry": industry,
    }
    return _ImmutableMetrics(
        {
            key: value
            for key, value in values.items()
            if (
                value is None
                or type(value) in {bool, int, str}
                or (type(value) is float and isfinite(value))
            )
        }
    )


def classify_common(
    company: FrozenCompanyInput,
    trading_dates: Mapping[str, Sequence[str]],
) -> CommonVerdict:
    """Classify only safety inputs shared by every discovery track."""

    if type(company) is not FrozenCompanyInput:
        raise TypeError("company must be a FrozenCompanyInput")
    security = company.security_status
    fields = company.fields
    sources = company.sources
    as_of = company.as_of
    if not all(
        isinstance(value, Mapping)
        for value in (security, fields, sources, as_of)
    ):
        raise TypeError("frozen company payloads must be mappings")

    code = company.code
    exchange = security.get("exchange")
    status = security.get("trading_status")
    last_trade_date = security.get("last_trade_date")
    industry = company.industry
    price_value = fields.get("price")
    price_as_of = as_of.get("price")

    def verdict(
        result: str,
        reason: str | None,
        *,
        calendar: tuple[str, ...] | None = None,
        parsed_price: Decimal | None = None,
    ) -> CommonVerdict:
        return CommonVerdict(
            code=code if type(code) is str else "",
            verdict=result,
            reason=reason,
            metrics=_metrics(
                exchange=exchange,
                trading_status=status,
                last_trade_date=last_trade_date,
                latest_trading_date=calendar[-1] if calendar else None,
                price=parsed_price,
                price_as_of=price_as_of,
                industry=industry,
            ),
        )

    if (
        type(code) is not str
        or code != code.strip().upper()
        or not is_a_share_code(code)
    ):
        return verdict("drop", "invalid_a_share_code")
    code_exchange = code.rsplit(".", 1)[-1]
    if code_exchange not in {"SH", "SZ"}:
        return verdict("drop", "unsupported_exchange")
    if exchange not in {"SH", "SZ"}:
        return verdict("insufficient", "exchange_unknown")
    if exchange != code_exchange:
        return verdict("drop", "exchange_code_mismatch")

    is_st = security.get("is_st")
    is_delisting = security.get("is_delisting")
    if is_st is True:
        return verdict("drop", "st_security")
    if is_delisting is True:
        return verdict("drop", "delisting_security")
    if type(is_st) is not bool:
        return verdict("insufficient", "st_state_unknown")
    if type(is_delisting) is not bool:
        return verdict("insufficient", "delisting_state_unknown")

    security_source = _nonempty_text(security.get("security_source"))
    if security_source is None:
        return verdict("insufficient", "security_provenance_missing")
    if (
        type(company.snapshot_at) is not datetime
        or company.snapshot_at.tzinfo is None
        or company.snapshot_at.utcoffset() != timedelta(0)
    ):
        return verdict("insufficient", "snapshot_timestamp_invalid")
    snapshot_date = company.snapshot_at.date().isoformat()
    listing_date = _iso_date(security.get("listing_date"))
    if listing_date is None:
        return verdict("insufficient", "listing_date_invalid")
    if listing_date > snapshot_date:
        return verdict("insufficient", "listing_date_future")
    if status not in {"trading", "suspended"}:
        return verdict("insufficient", "trading_status_unknown")

    calendar = _calendar(trading_dates, exchange)
    if calendar is None:
        return verdict("insufficient", "exchange_calendar_missing")
    if not calendar:
        return verdict("insufficient", "exchange_calendar_invalid")
    all_calendars: list[tuple[str, ...]] = []
    for calendar_exchange in trading_dates:
        if (
            calendar_exchange not in {"SH", "SZ"}
            or not (frozen_calendar := _calendar(
                trading_dates, calendar_exchange
            ))
        ):
            return verdict(
                "insufficient", "exchange_calendar_invalid", calendar=calendar
            )
        all_calendars.append(frozen_calendar)
    if any(
        item > snapshot_date
        for frozen_calendar in all_calendars
        for item in frozen_calendar
    ):
        return verdict(
            "insufficient", "exchange_calendar_future", calendar=calendar
        )
    normalized_last_trade_date = _iso_date(last_trade_date)
    if normalized_last_trade_date is None:
        return verdict(
            "insufficient", "last_trade_date_invalid", calendar=calendar
        )
    if normalized_last_trade_date > snapshot_date:
        return verdict(
            "insufficient", "last_trade_date_future", calendar=calendar
        )

    if _nonempty_text(industry) is None or industry.strip() == "-":
        return verdict("insufficient", "industry_missing", calendar=calendar)
    industry_as_of = _iso_date(security.get("industry_as_of"))
    if (
        _nonempty_text(security.get("industry_version")) is None
        or industry_as_of is None
    ):
        return verdict(
            "insufficient", "industry_provenance_missing", calendar=calendar
        )
    if industry_as_of > snapshot_date:
        return verdict(
            "insufficient", "industry_as_of_future", calendar=calendar
        )

    if price_value is None:
        return verdict("insufficient", "price_missing", calendar=calendar)
    parsed_price = _price(price_value)
    if parsed_price is None:
        return verdict("insufficient", "price_invalid", calendar=calendar)
    if _nonempty_text(sources.get("price")) is None:
        return verdict(
            "insufficient",
            "price_provenance_missing",
            calendar=calendar,
            parsed_price=parsed_price,
        )
    normalized_price_as_of = _iso_date(price_as_of)
    if normalized_price_as_of is None:
        return verdict(
            "insufficient",
            "price_as_of_invalid",
            calendar=calendar,
            parsed_price=parsed_price,
        )
    if normalized_price_as_of > snapshot_date:
        return verdict(
            "insufficient",
            "price_as_of_future",
            calendar=calendar,
            parsed_price=parsed_price,
        )

    row = CheapRow(
        code=code,
        name=company.name,
        price=parsed_price,
        pe=None,
        pb=None,
        mcap=None,
        industry=industry,
        exchange=exchange,
        listing_date=listing_date,
        is_st=is_st,
        is_delisting=is_delisting,
        trading_status=status,
        last_trade_date=last_trade_date,
        industry_version=security["industry_version"],
        industry_as_of=security["industry_as_of"],
        security_source=security_source,
        market_status_code=(
            security.get("market_status_code")
            if type(security.get("market_status_code")) is int
            else None
        ),
    )
    if not security_state_eligible(row, calendar):
        reason = (
            "trading_date_not_current"
            if status == "trading"
            else "suspension_too_old"
        )
        return verdict(
            "insufficient",
            reason,
            calendar=calendar,
            parsed_price=parsed_price,
        )
    if normalized_price_as_of != last_trade_date:
        return verdict(
            "insufficient",
            "price_date_mismatch",
            calendar=calendar,
            parsed_price=parsed_price,
        )
    return verdict(
        "pass", None, calendar=calendar, parsed_price=parsed_price
    )
