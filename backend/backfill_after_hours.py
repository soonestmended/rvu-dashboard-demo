"""One-shot backfill: populate is_after_hours on every row per the shared rule.

Use this when:
  - You just added the `is_after_hours` column (fresh DB migration).
  - The rule changed (e.g. after-hours now starts at 6pm, not 5pm) — edit the SQL below
    and re-run.
  - The holiday list changed and you want retroactive re-tagging.

The rule is materialized in SQL so we don't have to load every row into pandas. Mirrors
`queries._AFTER_HOURS_PREDICATE` and the pandas helper `ingest.compute_is_after_hours_series`
so all three paths agree. Any drift here is a bug.

Usage:
    docker compose exec backend python -m backend.backfill_after_hours --dry-run
    docker compose exec backend python -m backend.backfill_after_hours
    docker compose exec backend python -m backend.backfill_after_hours --since 2026-01-01
"""

import argparse
import logging

from .database import get_connection, get_db_path
from .queries import _holidays_sql_list

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _predicate_sql() -> str:
    """Return the after-hours SQL predicate as a self-contained fragment. Deliberately a
    local copy — keeping it here instead of importing queries._AFTER_HOURS_PREDICATE lets
    this script survive if queries.py is refactored later."""
    holidays_sql = _holidays_sql_list()
    holiday_match = (
        f"CAST(report_finalized_date AS DATE) IN ({holidays_sql})" if holidays_sql else "FALSE"
    )
    return f"""(
        shift_name IS DISTINCT FROM 'Weekend Call'
        AND (is_evening_er IS NULL OR is_evening_er = FALSE)
        AND (
            EXTRACT(dow FROM report_finalized_date) IN (0, 6)
            OR {holiday_match}
            OR EXTRACT(hour FROM report_finalized_date) < 8
            OR EXTRACT(hour FROM report_finalized_date) >= 17
        )
    )"""


def backfill_nulls_only(con) -> int:
    """Populate is_after_hours ONLY on rows where it's currently NULL. Used by the
    startup migration to self-heal a freshly-added column without rewriting rows that
    already have a value. Returns the number of rows updated."""
    predicate = _predicate_sql()
    null_count = con.execute(
        "SELECT COUNT(*) FROM exams WHERE is_after_hours IS NULL"
    ).fetchone()[0]
    if null_count == 0:
        return 0
    con.execute(f"UPDATE exams SET is_after_hours = {predicate} WHERE is_after_hours IS NULL")
    return null_count


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true',
                    help='Report change count without touching the DB.')
    ap.add_argument('--since', default=None,
                    help='Only recompute rows with report_finalized_date >= YYYY-MM-DD. '
                         'Use after adding a single new holiday to avoid rewriting the whole DB.')
    args = ap.parse_args()

    where_clause = ''
    if args.since:
        from datetime import date as _d
        _d.fromisoformat(args.since)  # validate to prevent injection
        where_clause = f"WHERE report_finalized_date >= '{args.since}'"

    con = get_connection(get_db_path())
    predicate = _predicate_sql()

    # Count rows whose current is_after_hours disagrees with the computed value. NULLs
    # count as a mismatch against any non-null recompute (i.e. first-run migration).
    diff_count = con.execute(f"""
        SELECT COUNT(*) FROM exams
        {where_clause}
        {'AND' if where_clause else 'WHERE'} (is_after_hours IS NULL
              OR is_after_hours IS DISTINCT FROM {predicate})
    """).fetchone()[0]

    total_rows = con.execute(f"SELECT COUNT(*) FROM exams {where_clause}").fetchone()[0]
    logger.info(f"Rows in scope: {total_rows:,}")
    logger.info(f"Rows with stale/missing is_after_hours: {diff_count:,}")

    if args.dry_run:
        # Break down what would change
        after_hours_count = con.execute(f"""
            SELECT COUNT(*) FROM exams {where_clause}
            {'AND' if where_clause else 'WHERE'} {predicate}
        """).fetchone()[0]
        logger.info(f"Would set is_after_hours=TRUE on {after_hours_count:,} rows in scope")
        logger.info("--dry-run set: no changes made.")
        return

    if diff_count == 0:
        logger.info("No rows need updating. Done.")
        return

    con.execute(f"""
        UPDATE exams
        SET is_after_hours = {predicate}
        {where_clause}
    """)
    logger.info(f"Updated is_after_hours on {diff_count:,} rows.")


if __name__ == '__main__':
    main()
