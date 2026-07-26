"""One-shot cleanup: normalize mPower cpt_code values and backfill derived columns.

Fixes historical rows ingested before cpt_code was normalized at the top of the ingest
pipeline. mPower rows arrived as pandas-coerced floats (`'70450.0'`) instead of ints
(`'70450'`); the dedup_key hash payload was already normalized so cross-source dedup
worked, but the stored `cpt_code` column was not, which meant:

  1. `cpt_division` lookups returned NaN for those rows (→ NULL in the DB).
  2. For multi-division attendings whose `division` falls back to `cpt_division`, the
     `division` column also ended up NULL, silently excluding those rows from every
     `WHERE division = 'NEURO'` (etc.) aggregation in queries.py / staffing.py.

Does:
  - Strip `.0` from each `cpt_code` (via `_normalize_cpt`, same helper ingest uses).
  - Recompute `cpt_division` from the current cpt_divisions.yaml.
  - Backfill `division` ONLY where it's currently NULL, using the ingest fallback
    (single-division attending mapping first, then new `cpt_division`).

Does NOT:
  - Retroactively apply the CTA-H&N 70496→70471 remap. That reflects a real change in
    department billing; historical rows were coded differently and should stay put.
  - Apply the spine-MRI NEURO reclassification to historical rows. Left as a separate
    policy decision — run explicitly if wanted.
  - Overwrite non-NULL `division` values. Preserves any prior correct assignment,
    including spine-MRI reclass overrides that ingest may have already applied.

Usage:
    docker compose exec backend python -m backend.cleanup_cpt_normalization --dry-run
    docker compose exec backend python -m backend.cleanup_cpt_normalization
"""

import argparse
import logging
from pathlib import Path

from .database import get_connection, get_db_path
from .config import load_all_configs
from .ingest import _normalize_cpt

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


VALID_DIVISIONS = ['NEURO', 'MSK', 'BODY', 'CHEST', 'IR', 'NUCLEAR', 'NIR', 'MAMMO']


def normalize_cpt_in_db(con, config_path: Path, dry_run: bool = False) -> int:
    """Strip '.0' from cpt_code, recompute cpt_division, backfill NULL division.

    Idempotent: operates only on rows whose cpt_code still has a '.0' segment, so a second
    run (or a run against an already-clean DB) is a no-op after the initial candidate SELECT.
    Returns the number of rows updated. Safe to call at startup — see app._run_migrations.
    """
    configs = load_all_configs(config_path)
    attending_divisions = configs['attending_divisions']
    cpt_divisions = configs['cpt_divisions']

    # Same split logic ingest_csv_files uses — single-div attendings get their division
    # from the attending map; multi-div attendings fall back to cpt_division.
    single_division_attendings = {}
    for attending, division in attending_divisions.items():
        divisions = [d.strip() for d in division.split(',')]
        valid = [d for d in divisions if d in VALID_DIVISIONS]
        if len(valid) == 1:
            single_division_attendings[attending] = valid[0]

    df = con.execute("""
        SELECT accession_number, report_finalized_by, cpt_code, cpt_division, division,
               work_professional_rvu
        FROM exams
        WHERE cpt_code LIKE '%.0' OR cpt_code LIKE '%.0,%'
    """).fetchdf()
    logger.info(f"Loaded {len(df):,} candidate rows (cpt_code with a '.0' segment)")

    if df.empty:
        logger.info("No rows need normalization.")
        return 0

    df['new_cpt_code'] = df['cpt_code'].map(_normalize_cpt)
    df['new_cpt_division'] = df['new_cpt_code'].map(cpt_divisions)
    # Backfill division ONLY where currently NULL. Preserves overrides that ingest already
    # applied on rows with non-NULL division (spine-MRI reclass, etc.).
    df['from_att'] = df['report_finalized_by'].map(single_division_attendings)
    df['recomputed_division'] = df['from_att'].fillna(df['new_cpt_division'])
    df['new_division'] = df['division'].fillna(df['recomputed_division'])

    def _neq(a, b):
        return a.fillna('') != b.fillna('')

    cpt_changed = _neq(df['cpt_code'], df['new_cpt_code'])
    cpt_div_changed = _neq(df['cpt_division'], df['new_cpt_division'])
    div_changed = _neq(df['division'], df['new_division'])
    any_changed = cpt_changed | cpt_div_changed | div_changed

    logger.info(f"  cpt_code changes:     {int(cpt_changed.sum()):,}")
    logger.info(f"  cpt_division changes: {int(cpt_div_changed.sum()):,}")
    logger.info(f"  division changes:     {int(div_changed.sum()):,}")

    newly_neuro_mask = df['division'].isna() & (df['new_division'] == 'NEURO')
    if newly_neuro_mask.any():
        rvu_sum = df.loc[newly_neuro_mask, 'work_professional_rvu'].fillna(0).sum()
        logger.info(
            f"  Rows becoming division='NEURO' (was NULL): {int(newly_neuro_mask.sum()):,} "
            f"→ +{rvu_sum:,.1f} wRVU newly visible in NEURO aggregations"
        )

    if dry_run:
        logger.info("--dry-run set: no changes made.")
        return 0

    updates = df.loc[any_changed, ['accession_number', 'new_cpt_code', 'new_cpt_division', 'new_division']].copy()
    updates = updates.rename(columns={
        'new_cpt_code': 'cpt_code',
        'new_cpt_division': 'cpt_division',
        'new_division': 'division',
    })
    if updates.empty:
        return 0

    con.register('cpt_cleanup_updates', updates)
    con.execute("""
        UPDATE exams
        SET cpt_code = cpt_cleanup_updates.cpt_code,
            cpt_division = cpt_cleanup_updates.cpt_division,
            division = cpt_cleanup_updates.division
        FROM cpt_cleanup_updates
        WHERE exams.accession_number = cpt_cleanup_updates.accession_number
    """)
    con.unregister('cpt_cleanup_updates')
    logger.info(f"Updated {len(updates):,} rows.")
    return len(updates)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true',
                    help='Report what would change without touching the DB.')
    ap.add_argument('--config-dir', default='config',
                    help='Config directory for cpt_divisions.yaml / attending_divisions.yaml.')
    args = ap.parse_args()

    config_path = Path(args.config_dir)
    if not config_path.is_absolute():
        config_path = Path(__file__).parent.parent / config_path

    con = get_connection(get_db_path())
    normalize_cpt_in_db(con, config_path, dry_run=args.dry_run)
    con.close()


if __name__ == '__main__':
    main()
