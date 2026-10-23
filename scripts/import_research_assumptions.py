"""Append reviewed valuation versions locally, without legacy migrations.

Input: JSON array of {user_id, code, payload}. The payload uses the named fields
in research.valuation.assumptions_from_payload plus explicit source selections,
metric_basis/period, rationale and human review. Existing initialized database
and retained imported sources are required. Dry-run opens the database read-only.
Example: python -m scripts.import_research_assumptions --db PATH --input PATH --dry-run
"""

import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import create_db_engine
from app.selection_models import ResearchAssumption
from research.valuation import append_assumptions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        batch = json.loads(args.input.read_text(encoding='utf-8-sig'))
        if not isinstance(batch, list) or not batch:
            raise ValueError('input must be a nonempty JSON array')
        if any(not isinstance(item, dict) or set(item) != {'user_id', 'code', 'payload'} for item in batch):
            raise ValueError('each version requires user_id, code and payload')
        if not args.db.is_file():
            raise ValueError('an existing initialized application database is required')
        engine = (create_engine(f'sqlite+pysqlite:///file:{args.db.resolve().as_posix()}?mode=ro&uri=true')
                  if args.dry_run else create_db_engine(args.db))
        try:
            if not args.dry_run:
                # Only the additive table; never init_db/legacy migrations.
                ResearchAssumption.__table__.create(engine, checkfirst=True)
            with Session(engine) as session, session.begin():
                versions = [append_assumptions(session, **item, dry_run=args.dry_run) for item in batch]
        finally:
            engine.dispose()
        print(json.dumps({'dry-run': args.dry_run, 'versions': versions}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, TypeError, SQLAlchemyError) as error:
        parser.exit(2, f'assumption import rejected: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
