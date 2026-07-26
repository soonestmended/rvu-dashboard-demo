"""Shift assignment during ingestion.

Applies shift assignments to exams based on schedule and criteria.
"""

import logging
from datetime import datetime

from .database import get_connection, get_db_path, get_row_count
from .config import load_shifts, load_schedule
from .schedule import assign_shift_to_exam

logger = logging.getLogger(__name__)


def assign_shifts_to_exams(config_dir="config", db_path=None):
    """
    Assign shifts to all exams in the database.

    This process:
    1. Loads shift definitions and schedule
    2. For each exam, determines the appropriate shift
    3. Updates shift_name, is_flex, is_evening_er, and evening_er_type

    Args:
        config_dir: Directory containing config files
        db_path: Path to database file (uses default if None)
    """
    from pathlib import Path
    config_path = Path(config_dir) if not Path(config_dir).is_absolute() else Path(config_dir)

    logger.info("Loading shift definitions and schedule...")
    shifts = load_shifts(config_path)
    schedule = load_schedule(config_path)

    logger.info(f"Loaded {len(shifts)} shift definitions")
    logger.info(f"Loaded {len(schedule)} days of schedule")

    if not shifts:
        logger.warning("No shift definitions found, skipping shift assignment")
        return

    # Connect to database
    if db_path is None:
        from .database import get_db_path
        db_path = get_db_path()

    con = get_connection(db_path)

    # Get all exams that need shift assignment
    logger.info("Fetching exams for shift assignment...")
    result = con.execute("""
        SELECT
            accession_number,
            exam_completed_date,
            report_finalized_date,
            report_finalized_by,
            patient_type,
            cpt_division
        FROM exams
        WHERE report_finalized_by IS NOT NULL
        ORDER BY report_finalized_date
    """)

    rows = result.fetchall()
    logger.info(f"Processing {len(rows):,} exams for shift assignment...")

    # Track statistics
    stats = {
        'total': len(rows),
        'assigned': 0,
        'unassigned': 0,
        'flex': 0,
        'evening_er': 0,
    }

    # Process each exam
    for i, row in enumerate(rows):
        if i > 0 and i % 10000 == 0:
            logger.info(f"  Processed {i:,}/{len(rows):,} exams...")

        accession_number = row[0]
        exam_completed = row[1]
        report_finalized = row[2]
        attending = row[3]
        patient_type = row[4] or 'OUTPATIENT'
        cpt_division = row[5]

        # Skip if missing critical data
        if not report_finalized or not attending:
            stats['unassigned'] += 1
            continue

        # Get date string for schedule lookup
        exam_date = report_finalized.date().isoformat()

        # Assign shift
        shift_name, evening_er_type, is_flex, is_evening_er = assign_shift_to_exam(
            exam_performed=exam_completed,
            exam_finalized=report_finalized,
            attending=attending,
            patient_type=patient_type,
            cpt_division=cpt_division,
            exam_date=exam_date,
            shifts=shifts,
            schedule=schedule
        )

        # Update database
        if shift_name:
            con.execute("""
                UPDATE exams
                SET shift_name = ?,
                    is_flex = ?,
                    is_evening_er = ?,
                    evening_er_type = ?
                WHERE accession_number = ?
            """, [shift_name, is_flex, is_evening_er, evening_er_type, accession_number])

            stats['assigned'] += 1
            if is_flex:
                stats['flex'] += 1
            if is_evening_er:
                stats['evening_er'] += 1
        else:
            # Clear shift fields if no shift assigned
            con.execute("""
                UPDATE exams
                SET shift_name = NULL,
                    is_flex = FALSE,
                    is_evening_er = FALSE
                WHERE accession_number = ?
            """, [accession_number])
            stats['unassigned'] += 1

    # Print statistics
    logger.info("Shift assignment complete:")
    logger.info(f"  Total exams processed: {stats['total']:,}")
    logger.info(f"  Assigned to shift: {stats['assigned']:,} ({stats['assigned']/stats['total']*100:.1f}%)")
    logger.info(f"  Unassigned: {stats['unassigned']:,} ({stats['unassigned']/stats['total']*100:.1f}%)")
    logger.info(f"  Flex shifts: {stats['flex']:,}")
    logger.info(f"  Evening ER: {stats['evening_er']:,}")

    # Show shift distribution
    logger.info("Shift distribution:")
    result = con.execute("""
        SELECT shift_name, COUNT(*) as count
        FROM exams
        GROUP BY shift_name
        ORDER BY count DESC
    """).fetchall()

    for shift_name, count in result:
        logger.info(f"  {shift_name or 'NULL'}: {count:,}")

    con.close()


def main():
    """CLI entry point for shift assignment."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description='Assign shifts to exams based on schedule and criteria')
    parser.add_argument('--config-dir', default='config', help='Directory containing config files')
    parser.add_argument('--db-path', help='Path to database file')

    args = parser.parse_args()

    try:
        assign_shifts_to_exams(config_dir=args.config_dir, db_path=args.db_path)
    except Exception as e:
        logger.error(f"Error during shift assignment: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
