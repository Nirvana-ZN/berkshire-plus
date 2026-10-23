"""Import append-only normalized evidence without running legacy migrations.

Example: uv run python -m scripts.import_selection_evidence --db PATH --input PATH --dry-run
Dry runs validate the entire batch without opening or creating the database.
Verified manual imports require verification={method:human_review,reviewer,
reviewed_at} and HTTPS document URL, SHA256 and report_location. Standalone
imports reject archival trust; validated archive bundles belong to T16.
"""

import argparse
import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.db import create_db_engine
from app.selection_models import FundamentalCoverage, FundamentalEvidence
from discovery.evidence_store import append_evidence
from market.evidence import parse_source_evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding='utf-8-sig'))
        if not isinstance(payload, list):
            raise ValueError('input must be a JSON array of evidence objects')
        records = tuple(parse_source_evidence(item) for item in payload)
        if args.dry_run:
            print(f'validated {len(records)} evidence records; dry-run, no database changes')
            return 0
        engine = create_db_engine(args.db)
        try:
            # Only these new tables; init_db also migrates legacy data and must
            # never be used as an evidence-import side effect.
            FundamentalEvidence.__table__.create(engine, checkfirst=True)
            FundamentalCoverage.__table__.create(engine, checkfirst=True)
            with Session(engine) as session, session.begin():
                for record in records:
                    append_evidence(session, record)
        finally:
            engine.dispose()
        print(f'accepted {len(records)} evidence records (existing identities are idempotent)')
        return 0
    except (ValueError, OSError, TypeError) as exc:
        parser.exit(2, f'evidence import rejected: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
