"""DuckDB database connection and schema management for RVU dashboard."""

import duckdb
import os
from pathlib import Path
from typing import Optional


def get_db_path(db_dir: str = "data") -> Path:
    """Get the path to the DuckDB database file."""
    # Check for DB_PATH environment variable first
    db_path_env = os.environ.get('DB_PATH')
    if db_path_env:
        return Path(db_path_env)

    db_path = Path(db_dir) / "rvu.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def get_connection(db_path: Optional[str | Path] = None) -> duckdb.DuckDBPyConnection:
    """
    Get a DuckDB database connection.

    Args:
        db_path: Path to database file. If None, uses default location.

    Returns:
        DuckDB connection object
    """
    if db_path is None:
        db_path = get_db_path()

    return duckdb.connect(str(db_path))


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    """
    Create the database schema.

    Args:
        con: DuckDB connection
    """
    con.execute("""
        CREATE TABLE IF NOT EXISTS exams (
            accession_number VARCHAR PRIMARY KEY,
            point_of_care VARCHAR,
            source_system VARCHAR,
            modality VARCHAR,
            exam_code VARCHAR,
            exam_description VARCHAR,
            cpt_code VARCHAR,
            is_stat BOOLEAN,
            patient_status_raw VARCHAR,

            -- Dates
            ordered_date TIMESTAMP,
            scheduled_date TIMESTAMP,
            patient_arrived_date TIMESTAMP,
            exam_started_date TIMESTAMP,
            exam_completed_date TIMESTAMP,
            report_created_date TIMESTAMP,
            preliminary_report_date TIMESTAMP,
            report_finalized_date TIMESTAMP,
            report_addendum_date TIMESTAMP,

            -- Provider info (only report finalized by is preserved)
            report_finalized_by VARCHAR,

            -- RVUs
            work_rvu DOUBLE,
            pe_rvu DOUBLE,
            mp_rvu DOUBLE,
            work_professional_rvu DOUBLE,
            pe_professional_rvu DOUBLE,
            mp_professional_rvu DOUBLE,
            work_technical_rvu DOUBLE,
            pe_technical_rvu DOUBLE,
            mp_technical_rvu DOUBLE,
            total_rvu DOUBLE,
            total_professional_rvu DOUBLE,
            total_technical_rvu DOUBLE,

            -- Computed fields (added during preprocessing)
            division VARCHAR,
            cpt_division VARCHAR,
            patient_type VARCHAR,
            is_evening_er BOOLEAN,
            evening_er_type VARCHAR,
            is_flex BOOLEAN,
            flex_shift_name VARCHAR,
            shift_name VARCHAR,
            -- True iff the read counts as "after-hours" under the new comp model. Materialized
            -- so every downstream query filters on a single canonical field instead of the
            -- scattered predicate that used to live in queries._AFTER_HOURS_PREDICATE. Rule:
            -- NOT Weekend Call AND NOT evening_er AND (weekend OR holiday OR hour < 8 OR
            -- hour >= 17), all keyed on report_finalized_date. Recomputed on ingest and by
            -- reassign_shifts_only; a `backfill_after_hours.py` script rebuilds the whole
            -- column from scratch when the definition or holiday list changes.
            is_after_hours BOOLEAN,

            -- Cross-source dedup key. SHA-256 of (report_finalized_date, cpt_code,
            -- report_finalized_by) normalized. Populated for EVERY row regardless of source
            -- so a report coming from PowerScribe (real accession) and the same report
            -- coming from mPower (no accession) both hash to the same value. Preferred
            -- dedup column going forward; accession_number stays as-is for reference.
            dedup_key VARCHAR
        )
    """)

    # If the DB pre-dates the dedup_key column, add it now. DuckDB rejects "IF NOT EXISTS"
    # in ADD COLUMN, so we catch the CatalogException instead.
    try:
        con.execute("ALTER TABLE exams ADD COLUMN dedup_key VARCHAR")
    except Exception:
        pass  # column already exists
    try:
        con.execute("ALTER TABLE exams ADD COLUMN is_after_hours BOOLEAN")
    except Exception:
        pass  # column already exists

    # Create indexes for common queries
    con.execute("CREATE INDEX IF NOT EXISTS idx_exams_dedup_key ON exams(dedup_key)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_exams_division ON exams(division)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_exams_cpt_division ON exams(cpt_division)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_exams_patient_type ON exams(patient_type)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_exams_exam_completed_date ON exams(exam_completed_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_exams_report_finalized_date ON exams(report_finalized_date)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_exams_is_evening_er ON exams(is_evening_er)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_exams_is_after_hours ON exams(is_after_hours)")


def initialize_database(db_path: Optional[str | Path] = None) -> duckdb.DuckDBPyConnection:
    """
    Initialize database with schema.

    Args:
        db_path: Path to database file. If None, uses default location.

    Returns:
        DuckDB connection object
    """
    con = get_connection(db_path)
    create_schema(con)
    return con


def get_row_count(con: duckdb.DuckDBPyConnection, table: str = "exams") -> int:
    """Get the row count of a table."""
    result = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return result[0] if result else 0
