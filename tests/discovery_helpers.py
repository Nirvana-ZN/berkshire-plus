from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal


SNAPSHOT_AT = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
RULER_IDS = {
    "quality_discount": 1,
    "deep_value": 1,
    "garp": 1,
}


def ruler_payloads_json(
    ruler_ids: dict[str, int | None] | None = None,
) -> str:
    from discovery.snapshot import _sha256, canonical_json

    ids = ruler_ids or RULER_IDS
    values = {}
    for track, ruler_id in ids.items():
        if ruler_id is None:
            values[track] = None
            continue
        payload = {
            "scope": "weekly-discovery",
            "track": track,
            "algorithm_version": "discovery-v1",
        }
        values[track] = {
            "id": ruler_id,
            "payload": payload,
            "sha256": _sha256(payload),
        }
    return canonical_json(values)


def add_unfrozen_run(
    session,
    *,
    week_key: str,
    data_version: str = "data-v1",
    financial_version: str = "financial-v1",
    valuation_version: str = "valuation-v1",
    ruler_ids: dict[str, int] | None = None,
):
    from app.discovery_models import DiscoveryRun

    ids = ruler_ids or RULER_IDS
    run = DiscoveryRun(
        user_id=1,
        week_key=week_key,
        current=False,
        status="queued",
        created_at=SNAPSHOT_AT,
        updated_at=SNAPSHOT_AT,
        snapshot_at=None,
        data_version=data_version,
        financial_version=financial_version,
        valuation_version=valuation_version,
        quality_discount_ruler_version_id=ids["quality_discount"],
        deep_value_ruler_version_id=ids["deep_value"],
        garp_ruler_version_id=ids["garp"],
        algorithm_version="discovery-v1",
        ruler_payloads_json=ruler_payloads_json(ids),
        input_fingerprint=None,
    )
    session.add(run)
    session.flush()
    return run


def versions(
    *,
    data_version: str = "data-v1",
    financial_version: str = "financial-v1",
    valuation_version: str = "valuation-v1",
    ruler_ids: dict[str, int] | None = None,
):
    from discovery.snapshot import SnapshotVersions

    return SnapshotVersions(
        data_version=data_version,
        financial_version=financial_version,
        valuation_version=valuation_version,
        ruler_version_ids=ruler_ids or RULER_IDS,
        algorithm_version="discovery-v1",
    )


def company(
    code: str,
    *,
    name: str | None = None,
    industry: str | None = "制造业",
    amount: Decimal | None = Decimal("10.2500"),
    price: Decimal | None = Decimal("12.50"),
    source: str = "annual-report",
    as_of: str = "2025-12-31",
    recent_listing: bool = False,
    security_status: dict[str, object] | None = None,
):
    from discovery.snapshot import CompanySnapshotInput

    return CompanySnapshotInput(
        code=code,
        name=name or f"公司-{code}",
        industry=industry,
        security_status=security_status
        or {
            "exchange": code.rsplit(".", 1)[-1],
            "listing_date": "2026-08-01" if recent_listing else "2000-01-01",
            "is_st": False,
            "is_delisting": False,
            "trading_status": "trading",
            "last_trade_date": "2026-09-17",
            "industry_version": "test-industry-v1",
            "industry_as_of": "2026-09-17",
            "security_source": "test-exchange",
        },
        fields={
            "amount": amount,
            "price": price,
            "listing_date": "2026-08-01" if recent_listing else "2000-01-01",
            "recent_listing": recent_listing,
        },
        sources={
            "amount": source,
            "price": "test-market",
            "listing_date": "exchange",
        },
        as_of={
            "amount": as_of,
            "price": "2026-09-17",
            "listing_date": "2026-09-17",
        },
    )


def freeze_one(session, *, week_key: str = "2026-W53"):
    from discovery.snapshot import freeze_inputs

    run = add_unfrozen_run(session, week_key=week_key)
    freeze_inputs(
        session,
        run,
        [company("600000.SH", amount=None, recent_listing=True)],
        versions(),
        {"SH": ["2026-09-16", "2026-09-17"]},
        snapshot_at=SNAPSHOT_AT,
    )
    session.flush()
    return run
