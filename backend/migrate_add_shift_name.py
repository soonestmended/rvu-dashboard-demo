"""Migration script to add shift_name column and backfill existing data.

Run this after updating the schema to add shift_name column.
"""

import logging
from pathlib import Path

from .database import get_db_path, get_connection

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def migrate_add_shift_name():
    """Add shift_name column and backfill existing data."""

    logger.info("Starting migration: Add shift_name column")

    con = get_connection(get_db_path())

    # Check if column already exists
    result = con.execute("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_name = 'exams' AND column_name = 'shift_name'
    """).fetchone()

    if result[0] > 0:
        logger.info("Column shift_name already exists, skipping column addition")
    else:
        logger.info("Adding shift_name column to exams table")
        con.execute("ALTER TABLE exams ADD COLUMN shift_name VARCHAR")

    # Get row counts before migration
    total_rows = con.execute("SELECT COUNT(*) FROM exams").fetchone()[0]
    logger.info(f"Total rows in exams table: {total_rows:,}")

    # Backfill shift_name based on existing is_evening_er and flex flags
    # For now, use a simple mapping - this will be improved once schedule integration is added

    logger.info("Backfilling shift_name for existing records...")

    con.execute("""
        UPDATE exams
        SET shift_name = CASE
            -- Evening ER Neuro cases
            WHEN is_evening_er = TRUE AND evening_er_type = 'ER-NEURO' THEN 'Evening ER - Neuro'
            -- Evening ER General cases
            WHEN is_evening_er = TRUE THEN 'Evening ER'
            -- Flex shifts (keep existing flex_shift_name if available)
            WHEN is_flex = TRUE AND flex_shift_name IS NOT NULL THEN flex_shift_name
            WHEN is_flex = TRUE THEN 'Flex Shift'
            -- Regular shifts - determine by day of week and time
            WHEN EXTRACT(DOW FROM exam_completed_date) IN (0, 6) THEN 'Regular Weekend'
            ELSE 'Regular Day'
        END
        WHERE shift_name IS NULL
    """)

    updated_rows = con.execute("SELECT COUNT(*) FROM exams WHERE shift_name IS NOT NULL").fetchone()[0]
    logger.info(f"Updated {updated_rows:,} rows with shift_name")

    # Summary of shift distribution
    logger.info("Shift name distribution:")
    result = con.execute("""
        SELECT shift_name, COUNT(*) as count
        FROM exams
        GROUP BY shift_name
        ORDER BY count DESC
    """).fetchall()

    for shift_name, count in result:
        logger.info(f"  {shift_name or 'NULL'}: {count:,}")

    con.close()
    logger.info("Migration complete")


def main():
    """CLI entry point."""
    migrate_add_shift_name()


if __name__ == '__main__':
    main()
