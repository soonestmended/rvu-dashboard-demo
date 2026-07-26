"""End-to-end ingest test on synthetic fixture CSVs.

Runs `ingest_csv_files` against a temp DuckDB using real configs. Covers the
cross-source dedup path — PowerScribe row + mPower row for the same exam should
collapse to one row. Also covers the CPT normalization fix — mPower's `70450.0`
should not create a duplicate row alongside PowerScribe's `70450`.

Slower than the other tiers (a few seconds for schema + ingest of ~5 rows). Skip
in the fast test path if you want.
"""

import shutil
import pytest
from pathlib import Path

from backend.ingest import ingest_csv_files


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def temp_env(tmp_path):
    """Copy fixture CSVs into a temp data dir alongside the real config dir. Ingest
    writes to a temp DB, so nothing touches production state."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for csv in FIXTURES.glob("*_anon.csv"):
        shutil.copy(csv, data_dir / csv.name)
    return {
        "data_dir": str(data_dir),
        "config_dir": "/app/config",
        "db_path": str(tmp_path / "test.db"),
    }


def _count(db_path: str, where: str = "1=1") -> int:
    import duckdb
    con = duckdb.connect(db_path, read_only=True)
    try:
        return con.execute(f"SELECT COUNT(*) FROM exams WHERE {where}").fetchone()[0]
    finally:
        con.close()


class TestCrossSourceDedup:
    def test_same_exam_two_sources_dedups_to_one(self, temp_env):
        """PowerScribe row (cpt='70450') and mPower row (cpt='70450.0') for the same
        exam (same finalized_date, same attending) must produce ONE row after ingest.
        Regression for both the CPT normalization bug and the dedup_key mechanism."""
        ingest_csv_files(**temp_env)
        db = temp_env["db_path"]

        # TEST001 appears in both fixtures with the same finalized date/attending/CPT.
        # After ingest, should be exactly one row.
        n = _count(db, "report_finalized_by = 'TEST001'")
        assert n == 1, f"TEST001 should dedup cross-source, got {n} rows"


class TestCptNormalization:
    def test_no_dot_zero_survives(self, temp_env):
        """After ingest, no cpt_code should contain '.0' — even though mPower fixture
        has '70450.0' and '72147.0'. Regression for the CPT normalization fix."""
        ingest_csv_files(**temp_env)
        db = temp_env["db_path"]

        import duckdb
        con = duckdb.connect(db, read_only=True)
        try:
            leaked = con.execute(
                "SELECT COUNT(*) FROM exams WHERE cpt_code LIKE '%.0' OR cpt_code LIKE '%.0,%'"
            ).fetchone()[0]
        finally:
            con.close()
        assert leaked == 0, f"{leaked} rows with '.0'-suffix leaked past ingest normalization"

    def test_normalized_cpt_lookup_populates_cpt_division(self, temp_env):
        """After normalization, cpt_division should be populated (from cpt_divisions.yaml).
        Before the fix, mPower's '70450.0' didn't match any config key and cpt_division
        stayed NULL, cascading into NULL division for multi-division attendings."""
        ingest_csv_files(**temp_env)
        db = temp_env["db_path"]

        import duckdb
        con = duckdb.connect(db, read_only=True)
        try:
            # 70450 (CT head) is in the standard cpt_divisions map — the mPower-format row
            # (from '70450.0' → '70450') should now have a non-NULL cpt_division.
            null_cpt_div = con.execute(
                "SELECT COUNT(*) FROM exams WHERE cpt_code = '70450' AND cpt_division IS NULL"
            ).fetchone()[0]
        finally:
            con.close()
        assert null_cpt_div == 0, "CT head 70450 rows have NULL cpt_division post-ingest"


class TestIngestSurvives:
    def test_row_count(self, temp_env):
        """Basic sanity: fixtures have 3 + 2 = 5 raw rows. TEST001 dedups cross-source,
        so 4 unique rows should land in the DB. If we get anything else, either dedup
        is broken or the normalization/cleanup is dropping rows unexpectedly."""
        ingest_csv_files(**temp_env)
        db = temp_env["db_path"]
        assert _count(db) == 4


class TestMigratedColumnOrder:
    """Regression: ingest must insert correctly into a DB whose physical column order
    differs from create_schema's CREATE TABLE order.

    A DB migrated via ALTER TABLE (dedup_key, then is_after_hours appended at the end)
    has a different column order than a fresh create_schema'd one. The ingest INSERT used
    positional `SELECT *`, which misaligned is_after_hours (BOOLEAN) and dedup_key (VARCHAR)
    on any migrated DB and failed the type check — the real 'stage2 FAIL' on the server.
    INSERT ... BY NAME fixes it. This test builds a migrated-order table and confirms
    values land in the right columns.
    """

    def test_insert_by_name_handles_column_order_mismatch(self, tmp_path):
        import duckdb
        import pandas as pd

        db = str(tmp_path / "migrated.db")
        con = duckdb.connect(db)
        # A table where is_after_hours comes AFTER dedup_key (ALTER-appended order),
        # the opposite of the ingest DataFrame's column order.
        con.execute("""
            CREATE TABLE exams (
                accession_number VARCHAR PRIMARY KEY,
                cpt_code VARCHAR,
                report_finalized_by VARCHAR,
                dedup_key VARCHAR,
                is_after_hours BOOLEAN
            )
        """)
        # DataFrame with the columns in the OTHER order (is_after_hours before dedup_key).
        df = pd.DataFrame([{
            "accession_number": "ACC1",
            "cpt_code": "70450",
            "report_finalized_by": "ATT1",
            "is_after_hours": True,
            "dedup_key": "hash_abc",
        }])
        con.register("exams_df", df)
        con.execute("INSERT INTO exams BY NAME SELECT * FROM exams_df")
        con.unregister("exams_df")

        row = con.execute(
            "SELECT is_after_hours, dedup_key FROM exams WHERE accession_number = 'ACC1'"
        ).fetchone()
        con.close()
        # If this had used positional SELECT *, is_after_hours would hold 'hash_abc'
        # (a VARCHAR into BOOLEAN → error) and dedup_key would hold TRUE.
        assert row[0] is True, f"is_after_hours wrong: {row[0]!r}"
        assert row[1] == "hash_abc", f"dedup_key wrong: {row[1]!r}"
