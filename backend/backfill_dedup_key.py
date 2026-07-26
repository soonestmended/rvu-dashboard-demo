"""One-shot backfill of the `dedup_key` column on existing rows.

Run after adding the `dedup_key` column via the auto-migration in database.create_schema.
Computes the deterministic SHA-256 of (report_finalized_date, cpt_code, report_finalized_by)
for every row that doesn't yet have a value, using the same normalization the ingest path
uses so PowerScribe- and mPower-sourced rows for the same exam land on the same key.

Usage:
    docker compose exec backend python -m backend.backfill_dedup_key
"""

import argparse
import logging

import pandas as pd

from .database import get_connection, get_db_path, create_schema
from .ingest import compute_dedup_keys_bulk

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--batch-size', type=int, default=50_000,
                    help='Rows per batch. DuckDB updates rewrite column groups, so larger batches trade memory for fewer rewrites.')
    ap.add_argument('--force', action='store_true',
                    help='Recompute dedup_key even on rows that already have one. Use if the key algorithm changes.')
    args = ap.parse_args()

    con = get_connection(get_db_path())
    # Ensure column exists (idempotent).
    create_schema(con)

    where = '' if args.force else 'WHERE dedup_key IS NULL'
    n_todo = con.execute(f'SELECT COUNT(*) FROM exams {where}').fetchone()[0]
    logger.info(f'Rows to backfill: {n_todo:,}')
    if n_todo == 0:
        return

    # Fetch everything at once — 480K × 4 cols is a few MB, well within RAM. Batching
    # the SELECT with OFFSET has correctness pitfalls (DuckDB doesn't guarantee stable
    # ORDER without an explicit ORDER BY, and with --force the WHERE-based iteration
    # runs away since UPDATE doesn't reduce the row count). Simpler + faster: one
    # SELECT, one parallel hash, batched UPDATE for the DuckDB rewrite step.
    logger.info(f'Fetching {n_todo:,} rows...')
    rows = con.execute(f"""
        SELECT accession_number, exam_completed_date, report_finalized_date,
               cpt_code, report_finalized_by
        FROM exams {where}
    """).fetchall()
    df = pd.DataFrame(rows, columns=['accession_number', 'exam_completed_date',
                                    'report_finalized_date', 'cpt_code',
                                    'report_finalized_by'])
    logger.info(f'Computing dedup keys (parallel, ~all cores)...')
    import time as _t
    t0 = _t.perf_counter()
    df['dedup_key'] = compute_dedup_keys_bulk(df)
    logger.info(f'  hashed {len(df):,} in {_t.perf_counter()-t0:.1f}s')

    # UPDATE in batches so DuckDB's per-batch column-rewrite cost is bounded.
    logger.info(f'Applying UPDATE in batches of {args.batch_size:,}...')
    updated = 0
    for start in range(0, len(df), args.batch_size):
        chunk = df.iloc[start:start + args.batch_size][['accession_number', 'dedup_key']].copy()
        con.register('backfill_updates', chunk)
        con.execute("""
            UPDATE exams
            SET dedup_key = backfill_updates.dedup_key
            FROM backfill_updates
            WHERE exams.accession_number = backfill_updates.accession_number
        """)
        con.unregister('backfill_updates')
        updated += len(chunk)
        logger.info(f'  ...updated {updated:,} / {len(df):,}')

    # Sanity check — how many distinct dedup_keys vs total rows? Collisions >0 mean either
    # true duplicates (same exam ingested twice from different sources) or hash collisions
    # (astronomically unlikely for SHA-256).
    total_rows = con.execute('SELECT COUNT(*) FROM exams').fetchone()[0]
    distinct_keys = con.execute('SELECT COUNT(DISTINCT dedup_key) FROM exams').fetchone()[0]
    logger.info(f'Total rows: {total_rows:,}  Distinct dedup_keys: {distinct_keys:,}')
    if distinct_keys < total_rows:
        logger.warning(
            f'{total_rows - distinct_keys:,} rows share a dedup_key with another row — '
            f'likely true duplicates you can clean up with a follow-up query.'
        )
    con.close()


if __name__ == '__main__':
    main()
