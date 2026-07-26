"""Extract weekend evening ER attending assignments empirically from exam data.

For each weekend day OR federal holiday, the neuro attending who read the most ER
cases between 16:00 and 22:00 (by exam_completed time) is taken to be the weekend
evening ER attending for that day. Output is written to config/weekend_er_assignments.csv.

Holidays are pulled from the scheduler module's HOLIDAYS set so this stays in sync
with the after-hours predicate.

Incremental by default: preserves entries in the existing CSV that are older than
`recompute_days` before the current max date, and only rescans the recent tail. This
matters because late-finalized reads can retroactively change who covered a given
weekend for up to ~2 weeks. Pass --rebuild to force a full recompute (needed after
any data cleanup that retroactively changes `division` or `report_finalized_by` on
old rows).
"""

import argparse
import csv
import logging
from datetime import date as _date, timedelta as _td
from pathlib import Path
from typing import Dict

from .database import get_connection, get_db_path
from .config import load_neuro_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


OUTPUT_CSV = Path(__file__).resolve().parent.parent / "config" / "weekend_er_assignments.csv"
CSV_FIELDS = ["date", "attending_id", "attending_name", "er_count"]


def _holidays_sql() -> str:
    """Return HOLIDAYS as a comma-separated list of DATE literals, or '' if the
    scheduler module isn't importable (then only Sat/Sun get considered)."""
    try:
        import schedule as _scheduler
        return ",".join(f"DATE '{d.isoformat()}'" for d in sorted(_scheduler.HOLIDAYS))
    except Exception:
        return ""


def _load_existing() -> Dict[str, dict]:
    """Load existing CSV as {date_iso: row_dict}. Empty if the file doesn't exist."""
    if not OUTPUT_CSV.exists():
        return {}
    out: Dict[str, dict] = {}
    with open(OUTPUT_CSV) as f:
        for row in csv.DictReader(f):
            out[row["date"]] = row
    return out


def extract(min_count: int = 3, recompute_days: int = 21, rebuild: bool = False) -> int:
    """Extract assignments and write config/weekend_er_assignments.csv. Returns row count.

    Incremental (default): preserves entries in the current file with date < (max_existing_date
    - recompute_days), and only rescans the tail. Merged output is written atomically.

    `min_count`: skip days where the top reader has fewer than this many ER cases (likely no
    real coverage that day).
    `recompute_days`: rescan this many days before the current max. Must exceed the practical
    finalization lag so a Sunday exam finalized 10 days later still influences that Sunday's
    coverer selection. 21 is generous.
    `rebuild`: ignore the existing file and recompute the whole history. Use after data
    cleanups (CPT normalization, division reassignment, etc.) that retroactively change rows.
    """
    nc = load_neuro_config()
    name_map = {aid: info["name"] for aid, info in nc["attendings"].items()}
    ids_str = ",".join(f"'{i}'" for i in nc["attendings"].keys())

    holidays_sql = _holidays_sql()
    holiday_match = (
        f"OR CAST(exam_completed_date AS DATE) IN ({holidays_sql})"
        if holidays_sql else ""
    )

    existing = {} if rebuild else _load_existing()

    # Recompute cutoff. Everything with date < cutoff in `existing` is preserved as-is;
    # everything on/after gets freshly computed from the DB.
    cutoff = None
    if existing:
        max_existing = max(existing.keys())
        cutoff = (_date.fromisoformat(max_existing) - _td(days=recompute_days)).isoformat()

    where_incremental = (
        f"AND CAST(exam_completed_date AS DATE) >= DATE '{cutoff}'" if cutoff else ""
    )
    if cutoff:
        logger.info(f"Incremental: preserving {sum(1 for d in existing if d < cutoff):,} entries "
                    f"with date < {cutoff}; rescanning from {cutoff} forward.")
    else:
        logger.info("Full extract: no existing entries preserved (rebuild or empty file).")

    con = get_connection(get_db_path())
    rows = con.execute(f"""
        WITH weekend_er AS (
          SELECT
            CAST(exam_completed_date AS DATE) AS day,
            report_finalized_by AS att,
            COUNT(*) AS er_count
          FROM exams
          WHERE (EXTRACT(dow FROM exam_completed_date) IN (0, 6) {holiday_match})
            AND EXTRACT(hour FROM exam_completed_date) >= 16
            AND EXTRACT(hour FROM exam_completed_date) < 22
            AND patient_type = 'ER'
            AND division = 'NEURO'
            AND report_finalized_by IN ({ids_str})
            {where_incremental}
          GROUP BY 1, 2
        ),
        ranked AS (
          SELECT day, att, er_count,
                 ROW_NUMBER() OVER (PARTITION BY day ORDER BY er_count DESC) AS rk
          FROM weekend_er
        )
        SELECT day, att, er_count
        FROM ranked
        WHERE rk = 1 AND er_count >= ?
        ORDER BY day
    """, [min_count]).fetchall()
    con.close()

    # Merge: keep preserved-region entries + fresh entries from the query. Fresh wins on any
    # overlap. Dates in the recompute region that used to qualify but no longer do get dropped
    # (correct — if the DB no longer supports the old coverer choice, don't hold on to it).
    merged: Dict[str, dict] = {}
    if cutoff:
        for date_str, row in existing.items():
            if date_str < cutoff:
                merged[date_str] = row
    for day, att, count in rows:
        merged[day.isoformat()] = {
            "date": day.isoformat(),
            "attending_id": att,
            "attending_name": name_map.get(att, att),
            "er_count": str(count),
        }

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    # Write to a tempfile then rename so a killed job doesn't leave a half-written CSV.
    tmp = OUTPUT_CSV.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_FIELDS)
        for date_str in sorted(merged.keys()):
            r = merged[date_str]
            w.writerow([r["date"], r["attending_id"], r["attending_name"], r["er_count"]])
    tmp.replace(OUTPUT_CSV)

    logger.info(f"Wrote {len(merged):,} weekend ER assignments to {OUTPUT_CSV}")
    return len(merged)


def load_assignments() -> Dict[str, str]:
    """Load weekend ER assignments. Returns {date_iso: attending_id}."""
    if not OUTPUT_CSV.exists():
        return {}
    out = {}
    with open(OUTPUT_CSV) as f:
        reader = csv.DictReader(f)
        for row in reader:
            out[row["date"]] = row["attending_id"]
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rebuild", action="store_true",
                    help="Full recompute from scratch. Use after data cleanups that "
                         "retroactively change old rows (CPT normalization, division re-tag, etc.).")
    ap.add_argument("--recompute-days", type=int, default=21,
                    help="Rescan this many days before the current file's max date. Must exceed "
                         "the practical finalization lag. Default 21.")
    ap.add_argument("--min-count", type=int, default=3,
                    help="Minimum ER cases for a day's top reader to count as coverer. Default 3.")
    args = ap.parse_args()
    extract(min_count=args.min_count, recompute_days=args.recompute_days, rebuild=args.rebuild)
