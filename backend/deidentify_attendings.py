"""De-identify attending names in the database.

Replaces all attending names with ID numbers and saves the mapping to a file.
"""

import logging
from pathlib import Path

from .database import get_db_path, get_connection

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def get_unique_attendings(con) -> list[str]:
    """Get all unique attending names from the database."""
    provider_fields = [
        'report_finalized_by'
    ]

    unique_names = set()

    for field in provider_fields:
        result = con.execute(f"SELECT DISTINCT {field} FROM exams WHERE {field} IS NOT NULL AND {field} != ''")
        names = [row[0] for row in result.fetchall()]
        unique_names.update(names)

    return sorted(unique_names)


def create_attending_id_map(names: list[str]) -> dict[str, str]:
    """Create ID mappings for attending names."""
    id_map = {}
    for i, name in enumerate(names, start=1):
        # Use ATT000001 format
        id_map[name] = f"ATT{i:06d}"
    return id_map


def save_id_map_to_file(id_map: dict[str, str], output_path: Path) -> None:
    """Save the ID mapping to a CSV file."""
    import csv

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['attending_id', 'attending_name'])
        for name, att_id in sorted(id_map.items(), key=lambda x: x[1]):
            writer.writerow([att_id, name])

    logger.info(f"Saved {len(id_map)} attending mappings to {output_path}")


def apply_deidentification(con, id_map: dict[str, str]) -> None:
    """Replace attending names with IDs in the database."""
    provider_fields = [
        'report_finalized_by'
    ]

    # For each field, update the values
    for field in provider_fields:
        logger.info(f"De-identifying {field}...")

        # Build CASE statement for all names
        when_clauses = []
        for name, att_id in id_map.items():
            # Escape single quotes in names
            escaped_name = name.replace("'", "''")
            when_clauses.append(f"WHEN {field} = '{escaped_name}' THEN '{att_id}'")

        when_sql = ' '.join(when_clauses)

        if when_sql:
            con.execute(f"""
                UPDATE exams
                SET {field} = CASE {when_sql} ELSE {field} END
                WHERE {field} IS NOT NULL
            """)

    logger.info("De-identification complete")


def deidentify_attendings(
    db_path: Path | str = None,
    output_path: Path | str = None
) -> dict[str, str]:
    """
    De-identify all attending names in the database.

    Args:
        db_path: Path to database file. If None, uses default location.
        output_path: Path to save the ID mapping CSV.

    Returns:
        Dictionary mapping original names to IDs
    """
    if db_path is None:
        db_path = get_db_path()
    else:
        db_path = Path(db_path)

    if output_path is None:
        output_path = Path(__file__).parent.parent / "config" / "attending_id_map.csv"
    else:
        output_path = Path(output_path)

    logger.info(f"Connecting to database: {db_path}")
    con = get_connection(db_path)

    # Get unique attending names
    logger.info("Extracting unique attending names...")
    names = get_unique_attendings(con)
    logger.info(f"Found {len(names)} unique attending names")

    # Create ID mappings
    id_map = create_attending_id_map(names)

    # Save mapping to file
    save_id_map_to_file(id_map, output_path)

    # Apply de-identification
    apply_deidentification(con, id_map)

    con.close()

    return id_map


def main():
    """CLI entry point for de-identification."""
    import argparse

    parser = argparse.ArgumentParser(description='De-identify attending names in RVU database')
    parser.add_argument('--db-path', help='Path to database file')
    parser.add_argument('--output-path', help='Path to save ID mapping CSV')

    args = parser.parse_args()

    deidentify_attendings(
        db_path=args.db_path,
        output_path=args.output_path
    )


if __name__ == '__main__':
    main()
