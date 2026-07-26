"""One-shot cleanup: remove rows that share a `dedup_key` with another row.

Prerequisite: run `backend.backfill_dedup_key --force` first so every row's dedup_key
reflects the current (normalized) hash algorithm. Otherwise this script won't collapse
duplicates that only differ in CPT formatting.

For each dedup_key group with more than one row, keeps the row with the highest
`work_professional_rvu` (ties broken by min accession_number for determinism) and
deletes the rest. Higher wRVU is preferred because when two ingest formats disagree
on the RVU value for the same CPT, the more recent RBRVS book tends to be higher.

Usage:
    docker compose exec backend python -m backend.dedupe_by_key --dry-run
    docker compose exec backend python -m backend.dedupe_by_key
"""

import argparse
import logging

from .database import get_connection, get_db_path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true',
                    help='Report what would be deleted without touching the DB.')
    args = ap.parse_args()

    con = get_connection(get_db_path())

    # Rows to keep: for each dedup_key group, the one with highest wRVU (tie → min accession).
    # Rows to delete: everything else in a multi-row dedup_key group.
    kept_cte = """
        WITH ranked AS (
            SELECT accession_number, dedup_key, work_professional_rvu,
                   ROW_NUMBER() OVER (
                       PARTITION BY dedup_key
                       ORDER BY work_professional_rvu DESC, accession_number ASC
                   ) AS rn
            FROM exams
            WHERE dedup_key IS NOT NULL
        )
        SELECT accession_number, dedup_key
        FROM ranked WHERE rn > 1
    """

    stats = con.execute(f"""
        WITH victims AS ({kept_cte})
        SELECT COUNT(*) AS victim_count,
               COALESCE(SUM((SELECT work_professional_rvu FROM exams e
                             WHERE e.accession_number = victims.accession_number)), 0) AS lost_rvu
        FROM victims
    """).fetchone()
    logger.info(f"Duplicate rows to delete: {stats[0]:,}")
    logger.info(f"wRVU removed by dedup: {stats[1]:,.1f}")

    if args.dry_run:
        logger.info("--dry-run set: no changes made.")
        return

    if stats[0] == 0:
        logger.info("Nothing to do.")
        return

    con.execute(f"""
        DELETE FROM exams
        WHERE accession_number IN (
            WITH ranked AS (
                SELECT accession_number,
                       ROW_NUMBER() OVER (
                           PARTITION BY dedup_key
                           ORDER BY work_professional_rvu DESC, accession_number ASC
                       ) AS rn
                FROM exams
                WHERE dedup_key IS NOT NULL
            )
            SELECT accession_number FROM ranked WHERE rn > 1
        )
    """)

    remaining = con.execute("SELECT COUNT(*) FROM exams").fetchone()[0]
    logger.info(f"Deleted {stats[0]:,} duplicate rows. {remaining:,} exam rows remain.")


if __name__ == '__main__':
    main()
