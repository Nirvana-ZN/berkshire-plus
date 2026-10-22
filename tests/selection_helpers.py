"""Synthetic evidence fixtures shared by selection contract tests."""

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256

from discovery.fundamental_types import EvidenceRecord, evidence_identity


def evidence(period: str, **values: str) -> EvidenceRecord:
    digest = sha256(repr((period, sorted(values.items()))).encode()).hexdigest()
    at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    row = EvidenceRecord(digest, "000001.SZ", "financial", period, at, at,
                         None, "https://example.invalid/fixture", digest,
                         values, "verified", "fixture-consolidated-cny")
    return replace(row, evidence_id=evidence_identity(row))
