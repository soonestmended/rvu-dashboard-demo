#!/usr/bin/env python3
"""
Stage 1 preprocessing: De-identify CSV files on secure server.

This script:
- Takes a list of input CSV files
- Removes all PHI columns
- Hashes Accession Number to use as unique ID
- De-identifies attending names using attending_id_map.csv
- Keeps only the columns needed for analysis
- Outputs a single combined CSV file
"""

import argparse
import hashlib
import sys
from pathlib import Path
from typing import List

import pandas as pd


def hash_value(value: str) -> str:
    """Hash a string value using SHA-256."""
    if pd.isna(value) or not str(value).strip():
        return value
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def load_attending_id_map(config_dir: Path) -> dict:
    """Load the attending name to ID mapping."""
    map_file = config_dir / "attending_id_map.csv"
    if not map_file.exists():
        print(f"Warning: {map_file} not found, attending names will not be de-identified")
        return {}

    df = pd.read_csv(map_file)
    # Create reverse mapping: name -> id
    # Normalize names (strip, uppercase for matching)
    name_to_id = {}
    for _, row in df.iterrows():
        name = str(row['attending_name']).strip().upper()
        att_id = row['attending_id']
        name_to_id[name] = att_id

    print(f"Loaded {len(name_to_id)} attending name mappings")
    return name_to_id


def deidentify_attending(name: str, name_to_id: dict) -> str:
    """Convert attending name to de-identified ID."""
    if pd.isna(name) or not str(name).strip():
        return name

    # Normalize name for lookup
    normalized = str(name).strip().upper()

    if normalized in name_to_id:
        return name_to_id[normalized]

    # Not found - return original (or could hash it)
    return name


# Columns to remove (PHI)
COLUMNS_TO_REMOVE = [
    "Patient MRN",
    "Patient First Name",
    "Patient Last Name",
    "Patient Sex",
    "Patient Age",
    "Report Text",
    "Organization",
]

# Provider columns to remove (keep only Report Finalized By)
PROVIDER_COLUMNS_TO_REMOVE = [
    "Ordered By",
    "Scheduled By",
    "Patient Arrived By",
    "Exam Started By",
    "Exam Completed By",
    "Report Created By",
    "Preliminary Report By",
    "Report Addendum By",
]

# Time delta columns to remove (all "X to Y (minutes)" columns)
TIME_DELTA_PATTERNS = [
    "Ordered to Scheduled (minutes)",
    "Ordered to Exam Started (minutes)",
    "Ordered to Exam Completed (minutes)",
    "Ordered to Report Created (minutes)",
    "Ordered to Preliminary Report (minutes)",
    "Ordered to Report Finalized (minutes)",
    "Ordered to Report Addendum (minutes)",
    "Scheduled to Exam Started (minutes)",
    "Scheduled to Exam Completed (minutes)",
    "Scheduled to Report Created (minutes)",
    "Scheduled to Preliminary Report (minutes)",
    "Scheduled to Report Finalized (minutes)",
    "Scheduled to Report Addendum (minutes)",
    "Exam Started to Exam Completed (minutes)",
    "Exam Started to Report Created (minutes)",
    "Exam Started to Preliminary Report (minutes)",
    "Exam Started to Report Finalized (minutes)",
    "Exam Started to Report Addendum (minutes)",
    "Exam Completed to Report Created (minutes)",
    "Exam Completed to Preliminary Report (minutes)",
    "Exam Completed to Report Finalized (minutes)",
    "Exam Completed to Report Addendum (minutes)",
    "Report Created to Preliminary Report (minutes)",
    "Report Created to Report Finalized (minutes)",
    "Report Created to Report Addendum (minutes)",
    "Preliminary Report to Report Finalized (minutes)",
    "Preliminary Report to Report Addendum (minutes)",
    "Report Finalized to Report Addendum (minutes)",
]

# Columns to keep in the final output
COLUMNS_TO_KEEP = [
    # Hashed ID
    "Accession Number",
    # Exam Metadata
    "Point of Care",
    "Source System",
    "Modality",
    "Exam Code",
    "Exam Description",
    "CPT Code",
    "Is Stat",
    "Patient Status",
    # Timestamps
    "Ordered Date",
    "Scheduled Date",
    "Patient Arrived Date",
    "Exam Started Date",
    "Exam Completed Date",
    "Report Created Date",
    "Preliminary Report Date",
    "Report Finalized Date",
    "Report Addendum Date",
    # Providers
    "Report Finalized By",
    # RVU Metrics
    "Work (RVU)",
    "PE (RVU)",
    "MP (RVU)",
    "Work (Professional) (RVU)",
    "PE (Professional) (RVU)",
    "MP (Professional) (RVU)",
    "Work (Technical) (RVU)",
    "PE (Technical) (RVU)",
    "MP (Technical) (RVU)",
    "Total (RVU)",
    "Total (Professional) (RVU)",
    "Total (Technical) (RVU)",
]


def preprocess_csv(input_files: List[Path], output_file: Path, config_dir: Path = None) -> None:
    """
    Preprocess and combine CSV files.

    Args:
        input_files: List of input CSV file paths
        output_file: Path to output combined CSV file
        config_dir: Directory containing config files (for attending_id_map.csv)
    """
    # Load attending ID map
    if config_dir is None:
        # When bundled with PyInstaller, data files are in sys._MEIPASS
        base = Path(getattr(sys, '_MEIPASS', Path(__file__).parent.parent))
        config_dir = base / "config"
    name_to_id = load_attending_id_map(config_dir)

    all_data = []

    for input_file in input_files:
        print(f"Processing: {input_file}")

        # Read CSV
        df = pd.read_csv(input_file)

        # Hash Accession Number
        if "Accession Number" in df.columns:
            df["Accession Number"] = df["Accession Number"].apply(hash_value)
            print(f"  - Hashed {len(df)} accession numbers")

        # De-identify attending names
        if "Report Finalized By" in df.columns and name_to_id:
            original_count = df["Report Finalized By"].notna().sum()
            df["Report Finalized By"] = df["Report Finalized By"].apply(
                lambda x: deidentify_attending(x, name_to_id)
            )
            # Count how many were successfully mapped
            mapped_count = df["Report Finalized By"].str.startswith("ATT", na=False).sum()
            print(f"  - De-identified {mapped_count}/{original_count} attending names")

        # Select only the columns we want to keep
        # This implicitly removes all PHI and other unwanted columns
        existing_columns = [col for col in COLUMNS_TO_KEEP if col in df.columns]
        df = df[existing_columns]

        all_data.append(df)
        print(f"  - Loaded {len(df):,} rows")

    # Combine all dataframes
    print("\nCombining files...")
    combined_df = pd.concat(all_data, ignore_index=True)

    # Write output
    print(f"Writing output to: {output_file}")
    combined_df.to_csv(output_file, index=False)

    print(f"\nDone!")
    print(f"  - Total rows: {len(combined_df):,}")
    print(f"  - Total columns: {len(combined_df.columns)}")
    print(f"\nColumns in output:")
    for col in combined_df.columns:
        print(f"  - {col}")


def main():
    parser = argparse.ArgumentParser(
        description="Stage 1 preprocessing: De-identify and combine CSV files"
    )
    parser.add_argument(
        "input_files",
        nargs="+",
        type=Path,
        help="Input CSV file(s) to process",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("rvu_anon.csv"),
        help="Output CSV file (default: rvu_anon.csv)",
    )
    parser.add_argument(
        "-c", "--config-dir",
        type=Path,
        default=None,
        help="Directory containing config files (default: ../config relative to script)",
    )

    args = parser.parse_args()

    # Validate input files exist
    for f in args.input_files:
        if not f.exists():
            print(f"Error: File not found: {f}", file=sys.stderr)
            sys.exit(1)

    preprocess_csv(args.input_files, args.output, args.config_dir)


if __name__ == "__main__":
    main()
