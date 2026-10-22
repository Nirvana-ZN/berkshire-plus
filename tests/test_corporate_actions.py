"""Synthetic, reviewed capital-action evidence; no production database or network."""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import init_db
from discovery.fundamental_types import evidence_identity
from tests.selection_helpers import evidence


def changed(row, **changes):
    row = replace(row, **changes)
    return replace(row, evidence_id=evidence_identity(row))


def action(**values):
    row = evidence('2026', economic_event_id='fixture-cancel-1', action='buyback_cancel',
                   stage='implemented', effective_at='2026-06-01T00:00:00+08:00',
                   sequence='1', shares_before='100', shares_after='90', factor='1', **values)
    return changed(row, kind='capital_event')


def fixture_payloads():
    return json.loads((Path(__file__).parent / 'fixtures/selection/capital-events.json').read_text(encoding='utf-8'))


def test_proposed_bonus_is_not_an_effective_event():
    from market.corporate_actions import parse_capital_events
    row = replace(evidence('2026', stage='proposed', action='bonus'), kind='capital_event')
    assert parse_capital_events((row,)) == ()


def test_same_cancellation_in_progress_notices_counts_once():
    from market.corporate_actions import parse_capital_events
    row = action()
    progress = changed(row, source_url='https://example.invalid/progress', document_sha256='b' * 64)
    events = parse_capital_events((progress, row, row))
    assert len(events) == 1
    assert events[0].event_id == 'fixture-cancel-1'
    assert events[0].shares_after == Decimal('90')
    assert parse_capital_events((row, progress)) == events


def test_same_day_events_follow_verified_sequence_and_preserve_share_bridge():
    from market.corporate_actions import parse_capital_events
    from market.evidence import parse_source_evidence
    rows = tuple(parse_source_evidence(p) for p in fixture_payloads()['records'])
    events = parse_capital_events(tuple(reversed(rows)))
    assert [event.kind for event in events] == ['bonus', 'issuance', 'buyback_cancel']
    assert [event.sequence for event in events] == [1, 2, 3]
    assert [(event.shares_before, event.shares_after, event.factor) for event in events] == [
        (Decimal('100'), Decimal('200'), Decimal('2')),
        (Decimal('200'), Decimal('220'), Decimal('1')),
        (Decimal('220'), Decimal('210'), Decimal('1')),
    ]
    assert events[0].effective_at == datetime(2026, 5, 31, 16, tzinfo=timezone.utc)


@pytest.mark.parametrize('kind,before,after,factor', [
    ('split', '100', '200', '2'), ('consolidation', '100', '50', '0.5'),
    ('rights_issue', '100', '110', '1'), ('stock_compensation', '100', '105', '1'),
])
def test_supported_implemented_action_kinds(kind, before, after, factor):
    from market.corporate_actions import parse_capital_events
    row = action()
    row = changed(row, values={**row.values, 'action': kind, 'shares_before': before,
                               'shares_after': after, 'factor': factor})
    event, = parse_capital_events((row,))
    assert (event.kind, event.shares_before, event.shares_after, event.factor) == (
        kind, Decimal(before), Decimal(after), Decimal(factor))


@pytest.mark.parametrize('kind,stage,status', [
    ('capital_plan', 'proposed', 'verified'), ('buyback_trade', 'traded', 'verified'),
    ('capital_event', 'implemented', 'untrusted'), ('capital_event', 'approved', 'verified'),
])
def test_nonimplemented_or_unverified_records_never_change_capital(kind, stage, status):
    from market.corporate_actions import parse_capital_events
    row = action()
    row = changed(row, kind=kind, status=status, values={**row.values, 'stage': stage})
    assert parse_capital_events((row,)) == ()


def test_verified_revisions_and_withdrawals_do_not_mutate_original():
    from market.corporate_actions import parse_capital_events
    old = action()
    corrected = changed(old, supersedes_id=old.evidence_id, values={**old.values, 'shares_after': '89'})
    withdrawn = changed(corrected, supersedes_id=corrected.evidence_id,
                        values={**corrected.values, 'stage': 'withdrawn'})
    assert parse_capital_events((old,))[0].shares_after == Decimal('90')
    assert parse_capital_events((old, corrected))[0].shares_after == Decimal('89')
    assert parse_capital_events((withdrawn, old, corrected)) == ()
    assert parse_capital_events((old, changed(withdrawn, status='untrusted'))) == parse_capital_events((old,))


def test_unlinked_conflicting_progress_or_ambiguous_order_fails_closed():
    from market.corporate_actions import parse_capital_events
    old = action()
    conflict = changed(old, values={**old.values, 'shares_after': '89'})
    with pytest.raises(ValueError, match='conflict'):
        parse_capital_events((old, conflict))
    duplicate_sequence = changed(old, values={**old.values, 'economic_event_id': 'other-cancellation'})
    with pytest.raises(ValueError, match='sequence'):
        parse_capital_events((old, duplicate_sequence))


@pytest.mark.parametrize('field,value', [
    ('shares_before', 'NaN'), ('shares_after', 'Infinity'), ('factor', '0'),
    ('shares_before', '-1'), ('sequence', '1.5'), ('sequence', '-1'),
    ('effective_at', '2026-06-01'), ('economic_event_id', None),
    ('action', 'buyback_trade'), ('shares_after', None),
])
def test_malformed_implemented_facts_fail_closed(field, value):
    from market.corporate_actions import parse_capital_events
    row = action()
    row = changed(row, values={**row.values, field: value})
    with pytest.raises(ValueError):
        parse_capital_events((row,))


def reviewed_coverage(rows, **changes):
    payload = fixture_payloads()['coverage']
    payload['values']['evidence_ids'] = json.dumps([row.evidence_id for row in rows])
    payload.update(changes)
    return payload


def test_explicit_reviewed_coverage_and_two_import_retries_are_append_only(tmp_path):
    from discovery.evidence_store import append_coverage, append_evidence, load_coverage
    from market.evidence import parse_event_coverage, parse_source_evidence
    rows = tuple(parse_source_evidence(p) for p in fixture_payloads()['records'])
    receipt = parse_source_evidence(reviewed_coverage(rows))
    complete = parse_event_coverage(receipt, rows)
    assert complete.complete is True
    assert complete.evidence_ids == tuple(row.evidence_id for row in rows) + (receipt.evidence_id,)
    engine = init_db(tmp_path / 'actions.db')
    try:
        with Session(engine) as session:
            incomplete = replace(complete, complete=False, evidence_ids=())
            append_coverage(session, incomplete)
            for _ in range(2):
                for row in (*rows, receipt):
                    append_evidence(session, row)
                append_coverage(session, complete)
            session.commit()
            assert load_coverage(session, rows[0].code) == (incomplete, complete)
            assert session.execute(text('SELECT count(*) FROM fundamental_evidence')).scalar_one() == 4
            assert session.execute(text('SELECT count(*) FROM fundamental_coverage')).scalar_one() == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize('problem', ['untrusted', 'wrong_ids', 'outside_interval', 'wrong_code', 'incomplete'])
def test_coverage_cannot_promote_unreviewed_incomplete_or_unrelated_facts(problem):
    from market.evidence import parse_event_coverage, parse_source_evidence
    rows = tuple(parse_source_evidence(p) for p in fixture_payloads()['records'])
    payload = reviewed_coverage(rows)
    if problem == 'untrusted':
        payload['status'] = 'untrusted'
    elif problem == 'wrong_ids':
        payload['values']['evidence_ids'] = '[]'
    elif problem == 'outside_interval':
        payload['values']['end'] = '2026-05-01'
    elif problem == 'wrong_code':
        payload['code'] = '000002.SZ'
    else:
        payload['values']['complete'] = 'false'
    receipt = parse_source_evidence(payload)
    if problem in ('untrusted', 'incomplete'):
        assert parse_event_coverage(receipt, rows).complete is False
    else:
        with pytest.raises(ValueError):
            parse_event_coverage(receipt, rows)


def test_reviewed_zero_event_window_is_explicit_and_archive_self_assertions_rejected():
    from market.evidence import parse_event_coverage, parse_source_evidence
    payload = reviewed_coverage(())
    receipt = parse_source_evidence(payload)
    assert parse_event_coverage(receipt, ()).complete is True
    with pytest.raises(ValueError, match='archive'):
        parse_source_evidence({**payload, 'archive_verified': True})


def test_same_business_day_uses_verified_sequence_even_when_effective_times_differ():
    from market.corporate_actions import parse_capital_events
    first = action()
    second = changed(first, values={**first.values, 'economic_event_id': 'fixture-second',
        'effective_at': '2026-05-31T16:01:00+00:00', 'sequence': '2'})
    first = changed(first, values={**first.values, 'effective_at': '2026-06-01T09:00:00+08:00'})
    assert [event.event_id for event in parse_capital_events((second, first))] == ['fixture-cancel-1', 'fixture-second']
    second = changed(second, values={**second.values, 'sequence': '1'})
    with pytest.raises(ValueError, match='sequence'):
        parse_capital_events((first, second))


def test_untrusted_withdrawal_does_not_hide_verified_parent_at_repository_cutoff(tmp_path):
    from discovery.evidence_store import append_evidence, load_evidence_as_of
    from market.corporate_actions import parse_capital_events
    old = action()
    withdrawal = changed(old, status='untrusted', supersedes_id=old.evidence_id,
                         values={**old.values, 'stage': 'withdrawn'})
    engine = init_db(tmp_path / 'withdrawal.db')
    try:
        with Session(engine) as session:
            append_evidence(session, old)
            append_evidence(session, withdrawal)
            session.commit()
            visible = load_evidence_as_of(session, old.code, old.observed_at)
            assert {row.evidence_id for row in visible} == {old.evidence_id, withdrawal.evidence_id}
            assert parse_capital_events(visible)[0].shares_after == Decimal('90')
    finally:
        engine.dispose()


@pytest.mark.parametrize('field', ['shares_before', 'shares_after', 'factor', 'sequence'])
def test_repository_independently_rejects_nonfinite_action_numbers(tmp_path, field):
    from discovery.evidence_store import append_evidence
    row = action()
    row = changed(row, values={**row.values, field: 'Infinity'})
    engine = init_db(tmp_path / 'nonfinite.db')
    try:
        with Session(engine) as session, pytest.raises(ValueError, match=field):
            append_evidence(session, row)
    finally:
        engine.dispose()


def test_repository_rejects_supersession_of_different_economic_event(tmp_path):
    from discovery.evidence_store import append_evidence
    old = action()
    unrelated = changed(old, supersedes_id=old.evidence_id,
                        values={**old.values, 'economic_event_id': 'different-event'})
    engine = init_db(tmp_path / 'identity.db')
    try:
        with Session(engine) as session:
            append_evidence(session, old)
            with pytest.raises(ValueError, match='economic'):
                append_evidence(session, unrelated)
    finally:
        engine.dispose()


def test_coverage_unknown_action_stage_cannot_certify_a_zero_event_interval():
    from market.evidence import parse_event_coverage, parse_source_evidence
    row = parse_source_evidence(fixture_payloads()['records'][0])
    row = changed(row, values={**row.values, 'stage': 'not_structurally_understood'})
    receipt = parse_source_evidence(reviewed_coverage((row,)))
    assert parse_event_coverage(receipt, (row,)).complete is False


def test_verified_correction_can_finish_an_explicit_chain_through_untrusted_draft(tmp_path):
    from discovery.evidence_store import append_evidence, load_evidence_as_of
    from market.corporate_actions import parse_capital_events
    old = action()
    draft = changed(old, status='untrusted', supersedes_id=old.evidence_id,
                    values={**old.values, 'shares_after': '88'})
    reviewed = changed(draft, status='verified', supersedes_id=draft.evidence_id)
    assert parse_capital_events((old, draft, reviewed))[0].shares_after == Decimal('88')
    engine = init_db(tmp_path / 'reviewed-chain.db')
    try:
        with Session(engine) as session:
            for row in (old, draft, reviewed):
                append_evidence(session, row)
            session.commit()
            visible = load_evidence_as_of(session, old.code, old.observed_at)
            assert visible == (reviewed,)
            assert parse_capital_events(visible)[0].shares_after == Decimal('88')
    finally:
        engine.dispose()


def test_normalized_fixture_is_a_self_contained_reviewed_import():
    from market.evidence import parse_event_coverage, parse_source_evidence
    payloads = fixture_payloads()
    rows = tuple(parse_source_evidence(payload) for payload in payloads['records'])
    receipt = parse_source_evidence(payloads['coverage'])
    window = parse_event_coverage(receipt, rows)
    assert window.complete is True
    assert len(window.evidence_ids) == 4
