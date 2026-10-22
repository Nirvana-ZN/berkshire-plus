from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import init_db
from discovery.fundamental_types import CoverageWindow, evidence_identity
from tests.selection_helpers import evidence


def identity(row, **changes):
    row = replace(row, **changes)
    return replace(row, evidence_id=evidence_identity(row))


@pytest.fixture
def store(tmp_path):
    # init_db must register the models while retaining existing immutable data.
    engine = init_db(tmp_path / 'evidence.db')
    yield engine
    engine.dispose()


def test_append_is_idempotent_and_reloads_aware_utc(store):
    from discovery.evidence_store import append_evidence, load_evidence_as_of
    row = identity(evidence('2025', inventory='0'),
                   source_updated_at=datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=8))))
    with Session(store) as session:
        assert append_evidence(session, row) == row.evidence_id
        assert append_evidence(session, row) == row.evidence_id
        session.commit()
    with Session(store) as session:
        loaded = load_evidence_as_of(session, row.code, row.observed_at)
        assert loaded == (row,)
        assert loaded[0].published_at.tzinfo is timezone.utc
        assert loaded[0].source_updated_at.tzinfo is timezone.utc
        assert session.execute(text('SELECT count(*) FROM fundamental_evidence')).scalar_one() == 1


def test_conflicting_or_noncanonical_identity_cannot_be_inserted(store):
    from discovery.evidence_store import append_evidence
    row = evidence('2025', inventory='100')
    with Session(store) as session:
        append_evidence(session, row)
        with pytest.raises(ValueError, match='identity|conflict'):
            append_evidence(session, replace(row, values={'inventory': '101'}))
        with pytest.raises(ValueError, match='identity'):
            append_evidence(session, replace(row, evidence_id='caller-chosen-id'))


@pytest.mark.parametrize('statement', [
    "UPDATE fundamental_evidence SET status='missing'",
    'DELETE FROM fundamental_evidence',
    "INSERT OR REPLACE INTO fundamental_evidence SELECT * FROM fundamental_evidence",
])
def test_evidence_database_rejects_rewrite_even_via_raw_sql(store, statement):
    from discovery.evidence_store import append_evidence
    with Session(store) as session:
        append_evidence(session, evidence('2025'))
        session.commit()
    with store.begin() as conn:
        with pytest.raises(IntegrityError, match='immutable|append-only'):
            conn.execute(text(statement))


def test_first_observation_is_not_backdated():
    from discovery.evidence_store import available_at
    row = identity(evidence('2025'), published_at=datetime(2026, 4, 1, tzinfo=timezone.utc))
    assert available_at(row) == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert available_at(row, historical=True) == row.observed_at
    assert available_at(identity(row, archive_verified=True), historical=True) == row.published_at


@pytest.mark.parametrize(('published', 'expected'), [
    ('2026-04-01T23:59:00+08:00', '2026-04-01T16:00:00+00:00'),
    ('2026-03-31T18:00:00+00:00', '2026-04-01T16:00:00+00:00'),
])
def test_date_only_release_closes_china_day(published, expected):
    from discovery.evidence_store import available_at
    row = identity(evidence('2025'), date_only=True, archive_verified=True,
                   published_at=datetime.fromisoformat(published))
    assert available_at(row, historical=True) == datetime.fromisoformat(expected)


def test_later_restatement_cannot_replace_old_cutoff(store):
    from discovery.evidence_store import append_evidence, load_evidence_as_of
    old = identity(evidence('2025', inventory='100'),
                   published_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
                   observed_at=datetime(2026, 4, 2, tzinfo=timezone.utc))
    revised = identity(old, values={'inventory': '120'}, supersedes_id=old.evidence_id,
                       observed_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    with Session(store) as session:
        append_evidence(session, old)
        append_evidence(session, revised)
        session.commit()
        assert load_evidence_as_of(session, old.code, datetime(2026, 5, 1, tzinfo=timezone.utc)) == (old,)
        assert load_evidence_as_of(session, old.code, revised.observed_at) == (revised,)
        # A live-fetched restatement cannot replace original historical disclosure.
        assert load_evidence_as_of(session, old.code, old.published_at, historical=True) == ()


def test_supersession_must_reference_same_company_kind_and_period(store):
    from discovery.evidence_store import append_evidence
    old = evidence('2025')
    with Session(store) as session:
        append_evidence(session, old)
        with pytest.raises(ValueError, match='supersed'):
            append_evidence(session, identity(old, code='000002.SZ', supersedes_id=old.evidence_id))


def test_coverage_is_append_only_and_failed_window_stays_incomplete(store):
    from app.selection_models import FundamentalCoverage
    from discovery.evidence_store import append_coverage, load_coverage
    window = CoverageWindow('000001.SZ', 'dividend', date(2025, 1, 1), date(2025, 12, 31), False, ())
    with Session(store) as session:
        key = append_coverage(session, window)
        assert append_coverage(session, window) == key
        session.commit()
        assert load_coverage(session, window.code) == (window,)
        assert session.scalars(select(FundamentalCoverage)).one().complete is False
    with store.begin() as conn:
        with pytest.raises(IntegrityError, match='append-only|immutable'):
            conn.execute(text('DELETE FROM fundamental_coverage'))


def test_cutoff_requires_timezone(store):
    from discovery.evidence_store import load_evidence_as_of
    with Session(store) as session, pytest.raises(ValueError, match='timezone'):
        load_evidence_as_of(session, '000001.SZ', datetime(2026, 10, 1))


def test_coverage_cannot_be_replaced_by_reusing_integer_primary_key(store):
    from discovery.evidence_store import append_coverage
    window = CoverageWindow('000001.SZ', 'dividend', date(2025, 1, 1), date(2025, 12, 31), False, ())
    with Session(store) as session:
        append_coverage(session, window)
        session.commit()
    with store.begin() as conn, pytest.raises(IntegrityError, match='immutable|append-only'):
        conn.execute(text("INSERT OR REPLACE INTO fundamental_coverage (id,code,kind,start_date,end_date,complete,evidence_ids_json,created_at,digest) SELECT id,code,kind,start_date,end_date,1,evidence_ids_json,created_at,'changed' FROM fundamental_coverage"))


def test_historical_reload_distinguishes_validated_archive_from_live_response(store):
    from discovery.evidence_store import append_evidence, load_evidence_as_of
    published = datetime(2026, 4, 1, tzinfo=timezone.utc)
    archive = identity(evidence('2024', inventory='11'), published_at=published, archive_verified=True)
    live = identity(evidence('2025', inventory='12'), published_at=published)
    cutoff = datetime(2026, 4, 2, tzinfo=timezone.utc)
    with Session(store) as session:
        append_evidence(session, archive)
        append_evidence(session, live)
        session.commit()
    with Session(store) as session:
        assert load_evidence_as_of(session, live.code, cutoff) == ()
        assert load_evidence_as_of(session, live.code, cutoff, historical=True) == (archive,)


def test_coverage_created_after_cutoff_cannot_certify_old_reports(store):
    from discovery.evidence_store import append_coverage, append_evidence, load_coverage
    row = identity(evidence('2020', inventory='10'),
                   published_at=datetime(2020, 4, 1, tzinfo=timezone.utc),
                   observed_at=datetime(2020, 4, 2, tzinfo=timezone.utc))
    window = CoverageWindow(row.code, 'financial', date(2020, 1, 1), date(2020, 12, 31), True, (row.evidence_id,))
    with Session(store) as session:
        append_evidence(session, row)
        append_coverage(session, window)
        session.commit()
        assert load_coverage(session, row.code) == (window,)
        assert load_coverage(session, row.code, cutoff=datetime(2021, 1, 1, tzinfo=timezone.utc)) == ()


@pytest.mark.parametrize(('published', 'observed', 'archive_verified'), [
    (datetime(3000, 1, 1, tzinfo=timezone.utc), datetime(2020, 4, 2, tzinfo=timezone.utc), False),
    (datetime(2020, 4, 1, tzinfo=timezone.utc), datetime(3000, 1, 1, tzinfo=timezone.utc), True),
])
def test_coverage_references_cannot_become_visible_before_production_availability(store, published, observed, archive_verified):
    from discovery.evidence_store import append_coverage, append_evidence, load_coverage
    row = identity(evidence('2020', inventory='10'), published_at=published,
                   observed_at=observed, archive_verified=archive_verified)
    window = CoverageWindow(row.code, 'financial', date(2020, 1, 1), date(2020, 12, 31), True, (row.evidence_id,))
    cutoff = datetime(2999, 1, 1, tzinfo=timezone.utc)
    with Session(store) as session:
        append_evidence(session, row)
        append_coverage(session, window)
        session.commit()
        assert load_coverage(session, row.code) == (window,)
        assert load_coverage(session, row.code, cutoff=cutoff) == ()
        assert load_coverage(session, row.code, cutoff=datetime(3000, 1, 1, tzinfo=timezone.utc)) == (window,)


def test_coverage_cutoff_requires_timezone(store):
    from discovery.evidence_store import load_coverage
    with Session(store) as session, pytest.raises(ValueError, match='timezone'):
        load_coverage(session, '000001.SZ', cutoff=datetime(2026, 10, 1))


def test_trusted_capture_time_preserves_coverage_cutoff_and_duplicate_creation(store):
    from discovery.evidence_store import append_coverage, append_evidence, load_coverage
    from app.selection_models import FundamentalCoverage
    from sqlalchemy import select
    from datetime import timedelta
    row = evidence('2025', inventory='10')
    cutoff = row.observed_at + timedelta(days=1)
    first = CoverageWindow(row.code, 'financial', date(2025, 1, 1), date(2025, 12, 31), True, (row.evidence_id,))
    later = replace(first, end=date(2026, 1, 1))
    with Session(store) as session:
        append_evidence(session, row)
        digest = append_coverage(session, first, captured_at=cutoff.astimezone(timezone(timedelta(hours=8))))
        assert append_coverage(session, first, captured_at=cutoff - timedelta(days=10)) == digest
        assert append_coverage(session, first, captured_at=cutoff + timedelta(days=10)) == digest
        stored = session.scalar(select(FundamentalCoverage).where(FundamentalCoverage.digest == digest))
        assert stored.created_at == cutoff
        assert stored.created_at.utcoffset() == timedelta(0)
        append_coverage(session, later, captured_at=cutoff + timedelta(microseconds=1))
        assert load_coverage(session, row.code, cutoff=cutoff) == (first,)
        assert load_coverage(session, row.code, cutoff=cutoff + timedelta(microseconds=1)) == (first, later)


def test_coverage_capture_rejects_naive_timestamp(store):
    from discovery.evidence_store import append_coverage
    window = CoverageWindow('000001.SZ', 'financial', date(2025, 1, 1), date(2025, 12, 31), False, ())
    with Session(store) as session, pytest.raises(ValueError, match='timezone'):
        append_coverage(session, window, captured_at=datetime(2026, 10, 1))


@pytest.mark.parametrize(('field', 'value'), [
    ('inventory', 'NaN'),
    ('inventory', 'sNaN'),
    ('inventory', 'Infinity'),
    ('inventory', '-Infinity'),
    ('inventory', 'not-a-number'),
    ('revenue', 'NaN'),
    ('roe', 'not-a-number'),
])
def test_direct_append_rejects_nonfinite_or_nonnumeric_registered_financial_values(store, field, value):
    from discovery.evidence_store import append_evidence
    row = evidence('2025', **{field: value})
    with Session(store) as session, pytest.raises(ValueError, match=field):
        append_evidence(session, row)
    with store.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM fundamental_evidence')).scalar_one() == 0


def test_direct_append_preserves_exact_numeric_strings_nulls_and_text_metadata(store):
    from discovery.evidence_store import append_evidence, load_evidence_as_of
    row = identity(evidence('2025'), values={
        'inventory': '0.00', 'restricted_cash': None, 'revenue': '9007199254740993.01',
        'disclosure_text': 'No non-operating occupation reported.',
        '_report_location': 'physical page 12; consolidated balance sheet',
        '_verification': 'Human review; literal NaN is provenance text here.',
    })
    with Session(store) as session:
        assert append_evidence(session, row) == row.evidence_id
        session.commit()
    with Session(store) as session:
        loaded, = load_evidence_as_of(session, row.code, row.observed_at)
        assert loaded == row
        assert loaded.evidence_id == row.evidence_id
        assert loaded.values['inventory'] == '0.00'
        assert loaded.values['restricted_cash'] is None
        assert loaded.values['revenue'] == '9007199254740993.01'
        assert loaded.values['disclosure_text'] == 'No non-operating occupation reported.'
        assert loaded.values['_verification'] == 'Human review; literal NaN is provenance text here.'
