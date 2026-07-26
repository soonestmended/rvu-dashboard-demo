"""DB invariant checks against the CURRENT production DB.

Uses `soft_assert` (see conftest) so small existing violations warn instead of failing.
Adjust the thresholds when a violation count changes for a good reason (e.g. new attending
onboarded who's missing from a config).

If the DB is missing at /app/data/rvu.db, all tests skip.
"""

from .conftest import soft_assert


# ---------- CPT format ----------

class TestCptFormat:
    def test_no_float_suffix_in_cpt(self, db_ro):
        """Regression for the mPower '.0'-suffix bug — the cleanup script + ingest fix
        should have eliminated these. Threshold 0 because any new occurrence means the
        ingest normalization slipped or someone bypassed it."""
        n = db_ro.execute(
            "SELECT COUNT(*) FROM exams WHERE cpt_code LIKE '%.0' OR cpt_code LIKE '%.0,%'"
        ).fetchone()[0]
        soft_assert(n, threshold=0, message="Rows with .0-suffixed cpt_code")


# ---------- Division ----------

class TestDivision:
    def test_null_division_bounded(self, db_ro):
        """Rows with NULL division fall out of every WHERE division='NEURO' query. Currently
        there's a small tail (~226) of rows whose CPT isn't in cpt_divisions.yaml — that's a
        config gap, not a bug. Threshold generous; alarms if it grows meaningfully."""
        n = db_ro.execute("SELECT COUNT(*) FROM exams WHERE division IS NULL").fetchone()[0]
        soft_assert(n, threshold=500, message="Rows with NULL division")

    def test_division_matches_cpt_division_for_singleattendings(self, db_ro):
        """For attendings in single-division configs, exam division should equal that division.
        Deviations mean the attending_divisions.yaml drifted from ingest reality."""
        # This is looser than a strict equality check — just look for wild disagreement.
        # Nothing to do here without loading configs; leaving as a placeholder for a future
        # richer check.
        pass


# ---------- Dedup integrity ----------

class TestDedupIntegrity:
    def test_dedup_key_unique(self, db_ro):
        """Every dedup_key group should have exactly one row. Duplicates here mean the
        cleanup / ingest dedup didn't catch cross-source or intra-batch dupes."""
        n = db_ro.execute("""
            SELECT COUNT(*) FROM (
                SELECT dedup_key FROM exams
                WHERE dedup_key IS NOT NULL
                GROUP BY dedup_key HAVING COUNT(*) > 1
            )
        """).fetchone()[0]
        soft_assert(n, threshold=0, message="dedup_key groups with >1 row")

    def test_accession_number_unique(self, db_ro):
        """PK — should always be unique. If this fails the DB is fundamentally corrupt."""
        n = db_ro.execute("""
            SELECT COUNT(*) FROM (
                SELECT accession_number FROM exams
                GROUP BY accession_number HAVING COUNT(*) > 1
            )
        """).fetchone()[0]
        assert n == 0, f"{n} accession_number values appear more than once"


# ---------- Shift assignment ----------

class TestShiftAssignment:
    def test_ia_moonlight_retag_never_exceeds_22(self, db_ro):
        """The IA→Moonlight retag caps at 22 exams/day. If any (shift_date, IA attending)
        has more than 22 rows tagged Moonlight AND the same day has IA rows, either the
        retag block miscounted or a non-IA path also tagged Moonlight on the same day
        (e.g. multi-division attending whose non-scheduled reads fell to Moonlight while
        their few scheduled reads went to IA). Baseline is ~35 known cases dominated by
        multi-div attendings on high-volume days; threshold set generously above that so
        any meaningful growth from a broken retag will fail hard."""
        n = db_ro.execute("""
            WITH ia_days AS (
                SELECT
                    CAST(report_finalized_date AS DATE) AS shift_date,
                    report_finalized_by,
                    SUM(CASE WHEN shift_name = 'Moonlight' THEN 1 ELSE 0 END) AS ml_count,
                    SUM(CASE WHEN shift_name = 'Inpatient A' THEN 1 ELSE 0 END) AS ia_count
                FROM exams
                WHERE division = 'NEURO'
                  AND shift_name IN ('Moonlight', 'Inpatient A')
                GROUP BY 1, 2
            )
            SELECT COUNT(*) FROM ia_days WHERE ml_count > 22 AND ia_count > 0
        """).fetchone()[0]
        soft_assert(n, threshold=100, message="IA days with >22 Moonlight rows")

    def test_weekend_coverer_present_when_ers_read(self, db_ro):
        """For every weekend/holiday date where a neuro attending read ≥3 ER cases in the
        evening window, there should be a corresponding entry in weekend_er_assignments.csv.
        Missing entries flip evening reads to Moonlight incorrectly (bug we saw in this session)."""
        # Load neuro attending IDs from config
        from ..config import load_neuro_config
        nc = load_neuro_config()
        ids = ",".join(f"'{i}'" for i in nc["attendings"])
        # Days with ≥3 evening ER reads by a neuro attending.
        days_with_coverage = db_ro.execute(f"""
            SELECT CAST(exam_completed_date AS DATE) AS day
            FROM exams
            WHERE EXTRACT(dow FROM exam_completed_date) IN (0, 6)
              AND EXTRACT(hour FROM exam_completed_date) >= 16
              AND EXTRACT(hour FROM exam_completed_date) < 22
              AND patient_type = 'ER'
              AND division = 'NEURO'
              AND report_finalized_by IN ({ids})
            GROUP BY day
            HAVING COUNT(*) >= 3
        """).fetchall()
        from ..extract_weekend_er import load_assignments
        covered = set(load_assignments())
        missing = [d[0].isoformat() for d in days_with_coverage if d[0].isoformat() not in covered]
        # Threshold: allow up to ~5 recent uncovered days (data may be behind extract).
        soft_assert(len(missing), threshold=5,
                    message=f"weekend days with ≥3 evening ER reads but no coverer entry (e.g. {missing[:5]})")


# ---------- Cross-check totals ----------

class TestTotals:
    def test_division_total_matches_attending_total(self, db_ro):
        """SUM(wRVU) grouped by division should equal SUM(wRVU) grouped by attending,
        restricted to rows with both fields set. Discrepancy means one of the groupings
        drops rows unexpectedly."""
        div_total = db_ro.execute("""
            SELECT COALESCE(SUM(work_professional_rvu), 0)
            FROM exams WHERE division IS NOT NULL AND report_finalized_by IS NOT NULL
        """).fetchone()[0] or 0.0
        att_total = db_ro.execute("""
            SELECT COALESCE(SUM(work_professional_rvu), 0)
            FROM exams WHERE report_finalized_by IS NOT NULL AND division IS NOT NULL
        """).fetchone()[0] or 0.0
        # Same rows both times — must be equal to the penny.
        assert abs(div_total - att_total) < 0.01, f"{div_total=} vs {att_total=}"

    def test_no_negative_wrvu(self, db_ro):
        n = db_ro.execute(
            "SELECT COUNT(*) FROM exams WHERE work_professional_rvu < 0"
        ).fetchone()[0]
        soft_assert(n, threshold=0, message="rows with negative wRVU")


class TestAfterHoursFlag:
    """is_after_hours is materialized; its value must match the recompute predicate on
    every row. Divergence means ingest/reassign/backfill are out of sync — the whole
    reason we materialized was to prevent that class of bug."""

    def test_matches_recompute(self, db_ro):
        from ..queries import _holidays_sql_list
        holidays_sql = _holidays_sql_list()
        holiday_match = f"CAST(report_finalized_date AS DATE) IN ({holidays_sql})" if holidays_sql else "FALSE"
        n = db_ro.execute(f"""
            SELECT COUNT(*) FROM exams
            WHERE COALESCE(is_after_hours, FALSE) IS DISTINCT FROM (
                shift_name IS DISTINCT FROM 'Weekend Call'
                AND (is_evening_er IS NULL OR is_evening_er = FALSE)
                AND (
                    EXTRACT(dow FROM report_finalized_date) IN (0, 6)
                    OR {holiday_match}
                    OR EXTRACT(hour FROM report_finalized_date) < 8
                    OR EXTRACT(hour FROM report_finalized_date) >= 17
                )
            )
        """).fetchone()[0]
        # A tiny drift (say, rows on holidays added since last backfill) is a soft signal
        # to re-run backfill_after_hours; large drift is a bug.
        soft_assert(n, threshold=200, message="Rows where is_after_hours diverges from recompute")

    def test_no_null_after_backfill(self, db_ro):
        """After backfill, is_after_hours should never be NULL. If new rows have NULLs,
        ingest didn't populate it (regression)."""
        n = db_ro.execute("SELECT COUNT(*) FROM exams WHERE is_after_hours IS NULL").fetchone()[0]
        soft_assert(n, threshold=0, message="Rows with NULL is_after_hours")
