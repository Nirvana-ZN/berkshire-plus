"""Reconcile verified capital events before measuring economic dilution.

Callers supply already selected, implemented CapitalEvents for one issuer and
the requested window, plus explicit reviewed coverage. No coverage is inferred
from unchanged shares. This module does not select evidence versions or dates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, DecimalException, localcontext

from discovery.constants import CHINA_MARKET_TIMEZONE
from discovery.fundamental_types import CapitalEvent, MetricResult
from discovery.metrics import _context, _working_precision


_STRUCTURAL = frozenset({'split', 'bonus', 'consolidation'})
_ISSUANCE = frozenset({'issuance', 'rights_issue', 'stock_compensation'})
_KEYS = ('economic_dilution', 'gross_issuance_rate', 'cancellation_rate', 'bridge_error')
_ZERO = Decimal('0')
_ONE = Decimal('1')
_RELATIVE_TOLERANCE = Decimal('.0001')


def _unavailable(status: str, reason: str, ids: tuple[str, ...]) -> dict[str, MetricResult]:
    return {key: MetricResult(None, status, reason, ids) for key in _KEYS}


def economic_dilution(
    start_shares: Decimal,
    end_shares: Decimal,
    events: Sequence[CapitalEvent],
    *,
    coverage_complete: bool,
) -> MetricResult:
    """Return end / (start * structural factors) - 1 after reconciliation."""
    return capital_change_audit(start_shares, end_shares, events,
                                coverage_complete=coverage_complete)['economic_dilution']


def capital_change_audit(
    start_shares: Decimal,
    end_shares: Decimal,
    events: Sequence[CapitalEvent],
    *,
    coverage_complete: bool,
) -> Mapping[str, MetricResult]:
    """Return economic_dilution, gross_issuance_rate, cancellation_rate, bridge_error.

    Rates are decimal fractions. Gross issued/cancelled shares are converted
    to the common starting basis using cumulative structural factors, then
    divided by start_shares. bridge_error is the largest absolute discrepancy
    in actual shares across endpoint links and structural-factor checks.

    Incomplete coverage/missing endpoints return missing with reasons
    capital_actions_incomplete_coverage/capital_shares_missing. Invalid
    economics, duplicate economic identities, ambiguous order, mixed issuers
    and bridge failures return untrusted capital_actions_unreconciled. A
    measured failing bridge_error alone stays verified with that reason;
    callers must inspect the rate statuses. Invalid inputs never imply zero.
    Bounded-arithmetic failure returns untrusted capital_actions_numeric_range.
    Evidence IDs identify source versions, ordered by Shanghai business date
    and verified sequence. The parser owns version selection and deduplication.
    """
    if type(coverage_complete) is not bool:
        raise TypeError('coverage_complete must be a boolean')
    for value in (start_shares, end_shares):
        if value is not None and not isinstance(value, Decimal):
            raise TypeError('share endpoints must be Decimal or None')
    supplied = tuple(events)
    if any(not isinstance(event, CapitalEvent) for event in supplied):
        raise TypeError('events must contain CapitalEvent values')
    ordered = sorted(supplied, key=lambda event: (
        event.effective_at.astimezone(CHINA_MARKET_TIMEZONE).date(), event.sequence,
        event.event_id))
    ids = tuple(dict.fromkeys(event.evidence_id for event in ordered))
    if not coverage_complete:
        return _unavailable('missing', 'capital_actions_incomplete_coverage', ids)
    if start_shares is None or end_shares is None:
        return _unavailable('missing', 'capital_shares_missing', ids)
    failure = _unavailable('untrusted', 'capital_actions_unreconciled', ids)
    if any(not value.is_finite() or value <= _ZERO
           for value in (start_shares, end_shares)):
        return failure
    identities = {event.event_id for event in ordered}
    orders = {(event.effective_at.astimezone(CHINA_MARKET_TIMEZONE).date(),
               event.sequence) for event in ordered}
    if (len(identities) != len(ordered) or len(orders) != len(ordered)
            or len({event.code for event in ordered}) > 1):
        return failure
    for event in ordered:
        before, after, factor = event.shares_before, event.shares_after, event.factor
        if min(before, after, factor) <= _ZERO or event.sequence < 0:
            return failure
        if event.kind in {'split', 'bonus'}:
            valid = factor > _ONE and after > before
        elif event.kind == 'consolidation':
            valid = factor < _ONE and after < before
        elif event.kind in _ISSUANCE:
            valid = factor == _ONE and after > before
        elif event.kind == 'buyback_cancel':
            valid = factor == _ONE and after < before
        else:  # DTO validation also guards this; keep the economic boundary closed.
            valid = False
        if not valid:
            return failure
    values = (start_shares, end_shares, *(value for event in ordered
              for value in (event.shares_before, event.shares_after, event.factor)))
    # Reserve exact product coefficient space in addition to the input span.
    product_digits = sum(len(event.factor.as_tuple().digits)
                         for event in ordered if event.kind in _STRUCTURAL)
    precision = _working_precision(values, extra=40 + product_digits)
    numeric_failure = _unavailable('untrusted', 'capital_actions_numeric_range', ids)
    if precision is None:
        return numeric_failure
    try:
        with localcontext(_context(precision)):
            max_error = _ZERO
            current = start_shares
            reconciled = True

            def check(expected: Decimal, actual: Decimal, *,
                      shares_before: Decimal | None = None) -> None:
                nonlocal max_error, reconciled
                difference = abs(actual - expected)
                max_error = max(max_error, difference)
                reference = expected if shares_before is None else shares_before
                tolerance = max(_ONE, max(reference, actual) * _RELATIVE_TOLERANCE)
                if difference > tolerance:
                    reconciled = False

            for event in ordered:
                check(current, event.shares_before)
                if event.kind in _STRUCTURAL:
                    check(event.shares_before * event.factor, event.shares_after,
                          shares_before=event.shares_before)
                current = event.shares_after
            check(current, end_shares)
            if not reconciled:
                failure['bridge_error'] = MetricResult(
                    max_error, 'verified', 'capital_actions_unreconciled', ids)
                return failure
            factor = _ONE
            issued = cancelled = _ZERO
            for event in ordered:
                if event.kind in _STRUCTURAL:
                    factor *= event.factor
                elif event.kind in _ISSUANCE:
                    issued += (event.shares_after - event.shares_before) / factor
                else:
                    cancelled += (event.shares_before - event.shares_after) / factor
            results = (end_shares / (start_shares * factor) - _ONE,
                       issued / start_shares, cancelled / start_shares, max_error)
            if not all(value.is_finite() for value in results):
                return numeric_failure
            return {key: MetricResult(value, 'verified', None, ids)
                    for key, value in zip(_KEYS, results, strict=True)}
    except DecimalException:
        return numeric_failure
