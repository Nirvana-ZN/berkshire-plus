"""Review a local exposure version/selection; writes require explicit --apply.

--input JSON is {user_id, payload, supersedes_id?}. --select explicitly selects
the appended version. Alternatively --version-id ID --user-id ID --select picks
an existing owned version. Default validation uses a read-only database. Sources
must already exist via scripts.import_selection_evidence with retained excerpts.
"""

import argparse
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import create_db_engine
from app.selection_models import ExposureVersion, ExposureSelection
from ledger.exposure import append_exposure_version, select_exposure_version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--input', type=Path)
    source.add_argument('--version-id', type=int)
    parser.add_argument('--user-id', type=int)
    parser.add_argument('--select', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        if not args.db.is_file():
            raise ValueError('an existing initialized application database is required')
        if args.version_id is not None and (not args.select or args.user_id is None):
            raise ValueError('existing selection requires --select and --user-id')
        data = json.loads(args.input.read_text(encoding='utf-8-sig')) if args.input else None
        if data is not None and (not isinstance(data, dict) or not {'user_id', 'payload'} <= set(data)
                                 or set(data) - {'user_id', 'payload', 'supersedes_id'}):
            raise ValueError('input requires user_id, payload and optional supersedes_id')
        engine = (create_db_engine(args.db) if args.apply else
                  create_engine(f'sqlite+pysqlite:///file:{args.db.resolve().as_posix()}?mode=ro&uri=true'))
        try:
            if args.apply:
                for model in (ExposureVersion, ExposureSelection):
                    model.__table__.create(engine, checkfirst=True)
            with Session(engine) as session, session.begin():
                version = append_exposure_version(session, **data, dry_run=not args.apply) if data else args.version_id
                selection = None
                if args.select and (args.apply or data is None):
                    selection = select_exposure_version(session, user_id=data['user_id'] if data else args.user_id,
                        version_id=version, dry_run=not args.apply)
        finally:
            engine.dispose()
        print(json.dumps({'applied': args.apply, 'version': version, 'selection': selection,
                          'select_on_apply': args.select}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, TypeError, SQLAlchemyError) as error:
        parser.exit(2, f'exposure import rejected: {error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
