"""Parse verified actions from evidence already selected at a decision cutoff.

Normalized capital_event values require economic_event_id, action, stage,
effective_at (aware ISO datetime), sequence, shares_before, shares_after and
factor. Plans use capital_plan/proposed, trades buyback_trade/traded; neither
creates a capital change. A cancellation uses capital_event/implemented and
action=buyback_cancel. A verified capital_event/withdrawn supersedes its prior
assertion. Raw evidence is retained by the append-only repository.

CapitalEvent.event_id is the stable economic identity; evidence_id identifies
the selected source version. Only explicit supersedes_id links resolve changed
facts. The caller owns point-in-time selection; this parser never backdates it.
Share reconciliation and economic dilution calculations belong to T05.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import timezone
from decimal import Decimal, InvalidOperation
import re

from discovery.evidence_store import available_at
from discovery.constants import CHINA_MARKET_TIMEZONE
from discovery.fundamental_types import CapitalEvent, EvidenceRecord
from market.evidence import _text, _time


def _amount(row: EvidenceRecord, field: str, *, positive: bool = False) -> Decimal:
    value = row.values.get(field)
    try:
        amount = Decimal(_text(value, field))
    except InvalidOperation as exc:
        raise ValueError(f'{field} must be a finite decimal') from exc
    if not amount.is_finite() or amount < 0 or (positive and amount == 0):
        raise ValueError(f'{field} must be a finite {"positive" if positive else "nonnegative"} decimal')
    return amount


def _event(row: EvidenceRecord) -> CapitalEvent:
    sequence = _text(row.values.get('sequence'), 'sequence')
    if not re.fullmatch(r'[0-9]+', sequence):
        raise ValueError('sequence must be a verified nonnegative integer')
    return CapitalEvent(
        event_id=_text(row.values.get('economic_event_id'), 'economic_event_id'),
        code=row.code, kind=_text(row.values.get('action'), 'action'),
        effective_at=_time(row.values.get('effective_at'), 'effective_at').astimezone(timezone.utc),
        sequence=int(sequence), shares_before=_amount(row, 'shares_before', positive=True),
        shares_after=_amount(row, 'shares_after'), factor=_amount(row, 'factor', positive=True),
        evidence_id=row.evidence_id,
    )


def parse_capital_events(records: Sequence[EvidenceRecord]) -> tuple[CapitalEvent, ...]:
    """Fail closed on conflicting facts or ambiguous verified event order.

    Identical progress notices dedupe deterministically. Missing structure in a
    verified implemented assertion raises rather than implying no action.
    """
    all_rows = {row.evidence_id: row for row in records if row.kind == 'capital_event'}
    rows = {row.evidence_id: row for row in all_rows.values() if row.status == 'verified'
            and row.values.get('stage') in {'implemented', 'withdrawn'}}
    superseded = set()
    for row in rows.values():
        seen = {row.evidence_id}
        parent_id = row.supersedes_id
        while parent_id in all_rows:
            if parent_id in seen:
                raise ValueError('cyclic capital event supersession')
            seen.add(parent_id)
            superseded.add(parent_id)
            parent = all_rows[parent_id]
            if (row.code, row.period, row.basis_id, row.values.get('economic_event_id')) != (
                parent.code, parent.period, parent.basis_id, parent.values.get('economic_event_id')
            ) or row.observed_at < parent.observed_at:
                raise ValueError('capital event supersession must retain its economic identity and observation order')
            parent_id = parent.supersedes_id
    grouped: dict[tuple[str, str], list[EvidenceRecord]] = {}
    for row in rows.values():
        if row.evidence_id not in superseded:
            key = (row.code, _text(row.values.get('economic_event_id'), 'economic_event_id'))
            grouped.setdefault(key, []).append(row)
    events = []
    for versions in grouped.values():
        versions.sort(key=lambda row: (available_at(row), row.evidence_id))
        selected = versions[-1]
        if selected.values['stage'] == 'withdrawn':
            if any(row.values['stage'] != 'withdrawn' for row in versions):
                raise ValueError('conflicting capital event withdrawal without supersession')
            continue
        event = _event(selected)
        for row in versions[:-1]:
            if row.values['stage'] != 'implemented' or row.basis_id != selected.basis_id:
                raise ValueError('conflicting capital event versions without supersession')
            other = _event(row)
            if replace(other, evidence_id=event.evidence_id) != event:
                raise ValueError('conflicting capital event versions without supersession')
        events.append(event)
    events.sort(key=lambda event: (event.code, event.effective_at.astimezone(CHINA_MARKET_TIMEZONE).date(),
                                   event.sequence, event.event_id))
    orders = {(event.code, event.effective_at.astimezone(CHINA_MARKET_TIMEZONE).date(), event.sequence)
              for event in events}
    if len(orders) != len(events):
        raise ValueError('capital events require distinct verified sequence on the same business date')
    return tuple(events)
