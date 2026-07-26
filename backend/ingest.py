"""CSV ingestion module for RVU dashboard.

Uses pandas for preprocessing and DuckDB for bulk loading.
"""

import logging
from pathlib import Path
from typing import Dict, Optional
import pandas as pd
from zoneinfo import ZoneInfo

from .database import get_db_path, get_connection, create_schema, get_row_count
from .config import load_all_configs, load_shifts, load_schedule


def _normalize_cpt(v) -> str:
    """Canonicalize a CPT cell so ingest-format differences don't produce distinct dedup
    keys for the same underlying billing line. Same exam sourced via PowerScribe CSV
    (int CPT like `'70450'`) and mPower CSV (pandas-coerced float like `'70450.0'`)
    must hash identically. Also normalizes multi-CPT comma-lists piecewise.
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ''
    s = str(v).strip()
    parts = []
    for p in s.split(','):
        p = p.strip()
        # Drop trailing ".0" (pandas float coercion) but preserve legitimate decimals.
        if p.endswith('.0'):
            p = p[:-2]
        parts.append(p)
    return ', '.join(parts)


def compute_dedup_key(exam_completed_date, report_finalized_date, cpt_code, report_finalized_by) -> str:
    """Deterministic SHA-256 of (exam_completed_date, report_finalized_date, cpt_code,
    report_finalized_by), all normalized to second precision in naive Pacific.

    Same exam → same key, regardless of whether the source CSV came from PowerScribe
    (real accession) or mPower (no accession).

    The exam_completed_date is critical to distinguish batch-finalized reports: a rad
    finalizing 5 screening chest X-rays with one click gives them all the same
    finalization microsecond, but each patient's scan completed at a distinct real
    time seconds/minutes apart. Without exam_completed_date the key would collapse
    those legitimately distinct exams together.

    Second precision (microseconds dropped) so mPower's second-precision timestamps
    still match PowerScribe's microsecond-precision ones on the same event.

    NOTE: For bulk computation (backfill, ingest of large files) prefer
    `compute_dedup_keys_bulk` — it's 20-50× faster because it vectorizes the payload
    string construction and parallelizes the SHA-256 across CPU cores.
    """
    import hashlib

    def _fmt(dt):
        if dt is None or (isinstance(dt, float) and pd.isna(dt)):
            return ''
        if pd.isna(dt):
            return ''
        ts = pd.Timestamp(dt)
        if getattr(ts, 'tzinfo', None) is not None:
            ts = ts.tz_convert(PACIFIC_TZ).tz_localize(None)
        return ts.strftime('%Y-%m-%dT%H:%M:%S')

    ec = _fmt(exam_completed_date)
    rf = _fmt(report_finalized_date)
    cpt = _normalize_cpt(cpt_code)
    by = '' if report_finalized_by is None or (isinstance(report_finalized_by, float) and pd.isna(report_finalized_by)) else str(report_finalized_by).strip()
    payload = f'{ec}|{rf}|{cpt}|{by}'
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _hash_chunk(chunk: list) -> list:
    """Module-level so it's picklable by ProcessPoolExecutor."""
    import hashlib
    return [hashlib.sha256(b).hexdigest() for b in chunk]


def compute_dedup_keys_bulk(df: pd.DataFrame, n_workers: Optional[int] = None) -> pd.Series:
    """Vectorized + parallel version of compute_dedup_key for a whole DataFrame.

    Accepts a DataFrame with columns 'exam_completed_date', 'report_finalized_date',
    'cpt_code', 'report_finalized_by'. Returns a pandas Series of hex digests aligned
    to df.index.

    Two-stage speedup:
      1. Payload strings are built via vectorized pandas ops (no per-row .apply()).
      2. SHA-256 is computed in parallel via ProcessPoolExecutor. hashlib only
         releases the GIL for inputs ≥2048 bytes; our payloads are ~80 bytes, so
         threads would serialize on the GIL. Processes each have their own GIL and
         actually parallelize.

    n_workers=None uses os.cpu_count(); pass an int to override.
    """
    import os
    from concurrent.futures import ProcessPoolExecutor

    def _to_second_iso(s: pd.Series) -> pd.Series:
        s = pd.to_datetime(s, errors='coerce')
        if getattr(s.dt, 'tz', None) is not None:
            s = s.dt.tz_convert(PACIFIC_TZ).dt.tz_localize(None)
        s = s.astype('datetime64[s]')
        return s.dt.strftime('%Y-%m-%dT%H:%M:%S').fillna('')

    ec = _to_second_iso(df['exam_completed_date'])
    rf = _to_second_iso(df['report_finalized_date'])
    # cpt_code: match _normalize_cpt() — strip .0 from each comma-separated segment so
    # PowerScribe's '70450' and mPower's '70450.0' hash to the same key.
    cpt = df['cpt_code'].fillna('').astype(str).map(_normalize_cpt)
    by = df['report_finalized_by'].fillna('').astype(str).str.strip()
    payload = ec + '|' + rf + '|' + cpt + '|' + by
    payload_bytes = [p.encode('utf-8') for p in payload]

    n_workers = n_workers or os.cpu_count() or 1
    # For small batches the process-startup overhead exceeds any parallelism win.
    if len(payload_bytes) < 5000 or n_workers == 1:
        return pd.Series(_hash_chunk(payload_bytes), index=df.index)

    chunk_size = max(1, (len(payload_bytes) + n_workers - 1) // n_workers)
    chunks = [payload_bytes[i:i + chunk_size] for i in range(0, len(payload_bytes), chunk_size)]

    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        results = list(ex.map(_hash_chunk, chunks))
    flat = [h for group in results for h in group]
    return pd.Series(flat, index=df.index)


def load_weekend_er_assignments(config_dir: Path = Path("config")) -> Dict[str, str]:
    """Read config/weekend_er_assignments.csv → {date_iso: attending_id}.

    Empty dict if the file is missing (back-compat with environments that haven't run
    extract_weekend_er yet). Falls back gracefully on read errors."""
    import csv as _csv
    path = config_dir / "weekend_er_assignments.csv"
    if not path.exists():
        return {}
    out: Dict[str, str] = {}
    try:
        with open(path, newline='') as f:
            for row in _csv.DictReader(f):
                if row.get('date') and row.get('attending_id'):
                    out[row['date']] = row['attending_id']
    except Exception as ex:
        logger.warning(f"Failed to load weekend_er_assignments: {ex}")
    return out

# Pacific timezone for shift window comparisons
PACIFIC_TZ = ZoneInfo('America/Los_Angeles')

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _load_holiday_set() -> set:
    """Return the scheduler module's HOLIDAYS as a Python set of date objects, or empty
    on failure (matches queries._holidays_sql_list's silent-fallback behavior for
    consistency)."""
    try:
        import schedule as _scheduler
        return set(_scheduler.HOLIDAYS)
    except Exception as ex:
        logger.warning(
            "Could not load scheduler HOLIDAYS — is_after_hours will not treat holidays "
            "specially (weekends still work): %s", ex,
        )
        return set()


def compute_is_after_hours_series(df: pd.DataFrame, holidays: set) -> pd.Series:
    """Compute is_after_hours for every row, exactly matching queries._AFTER_HOURS_PREDICATE:

        NOT Weekend Call
        AND NOT evening ER
        AND (weekend OR holiday OR hour < 8 OR hour >= 17)

    all keyed on report_finalized_date (already normalized to naive Pacific by ingest).
    Returns a bool Series aligned to df.index. Rows without report_finalized_date
    (extremely rare — dropna filters most before this runs) return False.
    """
    rf = pd.to_datetime(df['report_finalized_date'], errors='coerce')
    hour = rf.dt.hour
    dow = rf.dt.dayofweek  # 0 = Mon .. 6 = Sun
    date = rf.dt.date
    holiday_mask = date.isin(holidays) if holidays else pd.Series(False, index=df.index)
    # dow 5=Sat, 6=Sun; matches SQL EXTRACT(dow FROM ...) IN (0, 6) which uses Sun=0, Sat=6.
    weekend_mask = dow.isin([5, 6])
    hour_mask = (hour < 8) | (hour >= 17)
    time_ah = weekend_mask | holiday_mask | hour_mask

    not_weekend_call = df.get('shift_name', pd.Series(index=df.index, dtype=object)) != 'Weekend Call'
    ev = df.get('is_evening_er', pd.Series(False, index=df.index))
    not_evening_er = ~(ev.fillna(False).astype(bool))

    result = (not_weekend_call & not_evening_er & time_ah)
    # NaT report_finalized_date → time_ah is False → result is False. Explicit for clarity:
    result = result.where(rf.notna(), False)
    return result.astype(bool)


def assign_shifts_in_dataframe(df: pd.DataFrame, shifts: list, schedule: dict,
                                weekend_er_assignments: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    """
    Assign shifts to all exams in the DataFrame using pandas apply.

    Pre-calculates shift time windows as minutes from reference date midnight
    for fast integer comparison.

    Args:
        df: DataFrame with exam data
        shifts: List of shift definitions
        schedule: Schedule dict
        weekend_er_assignments: Optional {date_iso: attending_id} mapping. When provided, only
            the assigned coverer's evening weekend reads get tagged 'Weekend Evening ER' (and
            is_evening_er=True). Everyone else's evening weekend reads stay 'Moonlight' so
            they earn the after-hours $/RVU bonus. Falls back to time-based heuristic when
            None for backward compat.

    Returns:
        DataFrame with shift columns added
    """
    from .schedule import parse_time_str
    from datetime import time

    # Initialize shift columns
    df['shift_name'] = None
    df['is_flex'] = False
    df['is_evening_er'] = False
    df['evening_er_type'] = None

    # Skip if no shifts defined
    if not shifts:
        return df

    # Normalize to naive Pacific. Two callers:
    #   - ingest_csv_files already converted these columns → input is naive Pacific. No-op.
    #   - reassign_shifts_in_db pulled from DB → input is naive Pacific. No-op.
    # Belt-and-suspenders: if anyone ever passes tz-aware values, convert. If they're string
    # or some other type, parse via UTC then localize to Pacific.
    def _ensure_naive_pacific(s):
        if pd.api.types.is_datetime64_any_dtype(s):
            if getattr(s.dt, 'tz', None) is not None:
                return s.dt.tz_convert(PACIFIC_TZ).dt.tz_localize(None)
            return s
        # Non-datetime input (rare) — parse via UTC then drop to naive Pacific.
        return pd.to_datetime(s, errors='coerce', utc=True).dt.tz_convert(PACIFIC_TZ).dt.tz_localize(None)
    df['exam_completed_date'] = _ensure_naive_pacific(df['exam_completed_date'])
    df['report_finalized_date'] = _ensure_naive_pacific(df['report_finalized_date'])

    # Fill missing values
    df['patient_type'] = df['patient_type'].fillna('OUTPATIENT')
    df['cpt_division'] = df['cpt_division'].fillna('')

    logger.info(f"Assigning shifts for {len(df)} exams...")

    # Helper: convert time + day_offset to minutes from reference date midnight
    def time_to_minutes(t: time, day_offset: int) -> int:
        """Convert a time and day offset to minutes from reference midnight."""
        minutes_from_midnight = t.hour * 60 + t.minute
        return minutes_from_midnight + day_offset * 24 * 60

    # Pre-parse shift definitions (convert times to minutes, done once)
    parsed_shifts = []
    for shift in shifts:
        valid_from = pd.to_datetime(shift['valid_from'])
        valid_until = pd.to_datetime(shift['valid_until']) if shift.get('valid_until') else None
        scope = shift.get('scope', {})

        parsed_shift = {
            'name': shift['name'],
            'valid_from': valid_from,
            'valid_until': valid_until,
            'is_flex': shift.get('is_flex', False),
            'is_evening_er': shift.get('is_evening_er', False),
            'on_call_excluded': scope.get('on_call_excluded', False),
            'day_type': scope.get('day_type', 'all'),
            'patient_types': scope.get('patient_types'),
            'division': scope.get('division'),
        }

        # Pre-calculate exam_performed_window as minutes from reference midnight
        # This is the PRIMARY time window used for shift matching
        performed_window = scope.get('exam_performed_window')
        if performed_window:
            start_time = parse_time_str(performed_window['start']['time'])
            end_time = parse_time_str(performed_window['end']['time'])
            start_offset = performed_window['start'].get('day_offset', 0)
            end_offset = performed_window['end'].get('day_offset', 0)
            parsed_shift['performed_start_min'] = time_to_minutes(start_time, start_offset)
            parsed_shift['performed_end_min'] = time_to_minutes(end_time, end_offset)
        else:
            parsed_shift['performed_start_min'] = None

        # Pre-calculate exam_finalized_window as minutes from reference midnight (legacy/backup)
        finalized_window = scope.get('exam_finalized_window')
        if finalized_window:
            start_time = parse_time_str(finalized_window['start']['time'])
            end_time = parse_time_str(finalized_window['end']['time'])
            start_offset = finalized_window['start'].get('day_offset', 0)
            end_offset = finalized_window['end'].get('day_offset', 0)
            parsed_shift['finalized_start_min'] = time_to_minutes(start_time, start_offset)
            parsed_shift['finalized_end_min'] = time_to_minutes(end_time, end_offset)
        else:
            parsed_shift['finalized_start_min'] = None

        parsed_shifts.append(parsed_shift)

    def assign_shift_to_row(row):
        """Assign shift to a single row.

        The attending's shift is determined by the report finalized date
        (the day they're actually working). The exam performed date is used
        for weekend/holiday detection and evening ER time windows.
        """
        exam_completed = row['exam_completed_date']
        report_finalized = row['report_finalized_date']
        attending = row['report_finalized_by']
        cpt_division = row['cpt_division']
        division = row['division']

        if pd.isna(report_finalized) or pd.isna(attending):
            return None, False, False, None

        # Only assign shifts to Neuro division cases
        if division != 'NEURO':
            return None, False, False, None

        # Convert to Pacific timezone
        if report_finalized.tzinfo is None:
            report_finalized = report_finalized.replace(tzinfo=PACIFIC_TZ)
        else:
            report_finalized = report_finalized.astimezone(PACIFIC_TZ)

        if pd.notna(exam_completed):
            if exam_completed.tzinfo is None:
                exam_completed = exam_completed.replace(tzinfo=PACIFIC_TZ)
            else:
                exam_completed = exam_completed.astimezone(PACIFIC_TZ)

        # Use report finalized date for schedule/shift lookup
        report_date = report_finalized.date()
        report_date_str = report_date.strftime('%Y-%m-%d')

        # Use exam performed date for weekend/holiday and time-window checks
        if pd.notna(exam_completed):
            exam_date = exam_completed.date()
        else:
            exam_date = report_date
        exam_date_str = exam_date.strftime('%Y-%m-%d')

        # Check report date schedule for attending's shift and on-call status
        is_weekend = report_date.weekday() >= 5
        is_holiday = False
        on_call_attending = None
        assigned_shift_name = None

        if schedule and report_date_str in schedule:
            day_schedule = schedule[report_date_str]
            is_holiday = day_schedule.get('_is_holiday', False)
            on_call_attending = day_schedule.get('_on_call')
            assigned_shift_name = day_schedule.get(attending)

        # If attending has a scheduled shift on report date, use it
        if assigned_shift_name:
            for shift in parsed_shifts:
                if shift['name'] == assigned_shift_name:
                    evening_er_type = None
                    if shift['is_evening_er']:
                        evening_er_type = 'ER-NEURO' if cpt_division == 'NEURO' else 'ER-GENERAL'
                    return shift['name'], shift['is_flex'], shift['is_evening_er'], evening_er_type

        # If attending is on call on report date, it's Weekend Call
        if attending == on_call_attending:
            return 'Weekend Call', False, False, None

        # No scheduled shift on report date — on weekdays, check exam performed date
        # (attending reading yesterday's leftover exams on their shift).
        # On weekends/holidays, skip this — unscheduled reads are moonlighting.
        if not is_weekend and not is_holiday and pd.notna(exam_completed) and exam_date_str != report_date_str:
            if schedule and exam_date_str in schedule:
                exam_day_schedule = schedule[exam_date_str]
                exam_day_shift = exam_day_schedule.get(attending)
                if exam_day_shift:
                    for shift in parsed_shifts:
                        if shift['name'] == exam_day_shift:
                            evening_er_type = None
                            if shift['is_evening_er']:
                                evening_er_type = 'ER-NEURO' if cpt_division == 'NEURO' else 'ER-GENERAL'
                            return shift['name'], shift['is_flex'], shift['is_evening_er'], evening_er_type

        # No scheduled shift on either date — moonlighting or weekend evening ER
        if is_weekend or is_holiday:
            # Only tag as 'Weekend Evening ER' when this attending is the OFFICIAL Weekend ER
            # coverer for the day per weekend_er_assignments.csv. Anyone else reading evening
            # inpatient/ER cases on a weekend is moonlighting (and earns after-hours $/RVU
            # instead of the per-shift weekend ER stipend the coverer gets).
            is_assigned_we_er_coverer = (
                weekend_er_assignments is not None
                and weekend_er_assignments.get(report_date_str) == attending
            )
            if is_assigned_we_er_coverer and pd.notna(exam_completed):
                patient_type = row.get('patient_type', '')
                exam_minutes = exam_completed.hour * 60 + exam_completed.minute
                # [16:00, 22:00) matches extract_weekend_er.py's `hour >= 16 AND hour < 22`
                # window. Inclusive-end was a footgun: an exam completed exactly at 22:00
                # would count as evening ER here but wouldn't influence which attending
                # gets picked as coverer over there, so the two paths could disagree.
                is_evening_time = 960 <= exam_minutes < 1320
                is_evening_patient = patient_type in ['INPATIENT', 'ER']

                if is_evening_time and is_evening_patient:
                    evening_er_type = 'ER-NEURO' if cpt_division == 'NEURO' else 'ER-GENERAL'
                    return 'Weekend Evening ER', False, True, evening_er_type

            return 'Moonlight', False, False, None
        else:
            return 'Moonlight', False, False, None

    # Apply shift assignment to each row
    results = df.apply(assign_shift_to_row, axis=1)
    df['shift_name'] = results.map(lambda x: x[0] if x else None)
    df['is_flex'] = results.map(lambda x: x[1] if x else False)
    df['is_evening_er'] = results.map(lambda x: x[2] if x else False)
    df['evening_er_type'] = results.map(lambda x: x[3] if x else None)

    # Post-processing: On weekdays, the Inpatient A attending's first 22 exams
    # (by report finalized time) are overnight moonlight reads, not Inpatient A work.
    MOONLIGHT_EXAM_COUNT = 22

    inpatient_a_mask = (df['shift_name'] == 'Inpatient A') & (df['division'] == 'NEURO')
    if inpatient_a_mask.any():
        inpatient_a_df = df[inpatient_a_mask].copy()

        # Group by the SHIFT day (report_finalized_date) and sort within by finalized time.
        # Using exam_completed_date would misgroup: an IA attending signing Sunday's leftover
        # exams on Monday would have those grouped with Sunday's completions (may have no IA
        # scheduled, and weekday=6 fails the < 5 filter) and never appear in Monday's first-22
        # retag. The retag intent is "the first 22 exams the IA attending SIGNS on their shift
        # day," which is a report_finalized_date concept.
        inpatient_a_df['_shift_date'] = inpatient_a_df['report_finalized_date'].dt.date
        inpatient_a_df['_weekday'] = inpatient_a_df['report_finalized_date'].dt.weekday
        weekday_mask = inpatient_a_df['_weekday'] < 5

        retag_indices = []
        for _sdate, group in inpatient_a_df[weekday_mask].groupby('_shift_date'):
            sorted_group = group.sort_values('report_finalized_date')
            first_n = sorted_group.head(MOONLIGHT_EXAM_COUNT)
            retag_indices.extend(first_n.index.tolist())

        if retag_indices:
            df.loc[retag_indices, 'shift_name'] = 'Moonlight'
            logger.info(f"  Re-tagged {len(retag_indices)} Inpatient A exams as Moonlight (first {MOONLIGHT_EXAM_COUNT}/day)")

    # Materialize is_after_hours AFTER shift assignment (needs final shift_name for the
    # Weekend Call exclusion) and after IA→Moonlight retag (doesn't affect it since both
    # are still after-hours by time, but conceptually correct).
    df['is_after_hours'] = compute_is_after_hours_series(df, _load_holiday_set())

    # Log shift distribution
    shift_counts = df['shift_name'].value_counts()
    logger.info("Shift distribution:")
    for shift_name, count in shift_counts.items():
        logger.info(f"  {shift_name or 'NULL'}: {count:,}")

    unassigned = df['shift_name'].isna().sum()
    logger.info(f"  Unassigned: {unassigned:,}")

    # Log details of unassigned cases for debugging
    if unassigned > 0:
        logger.info("Unassigned Neuro cases details:")
        unassigned_df = df[df['shift_name'].isna() & (df['division'] == 'NEURO')]
        for _, row in unassigned_df.iterrows():
            if pd.notna(row['exam_completed_date']):
                # Convert to Pacific timezone for display
                if row['exam_completed_date'].tzinfo is None:
                    exam_dt = row['exam_completed_date'].replace(tzinfo=PACIFIC_TZ)
                else:
                    exam_dt = row['exam_completed_date'].astimezone(PACIFIC_TZ)
                exam_time = exam_dt.strftime('%Y-%m-%d %H:%M:%S')
            else:
                exam_time = 'N/A'
            logger.info(f"  {row['report_finalized_by']}: {row['patient_type']}, exam={exam_time}, division={row.get('division', 'N/A')}, poc={row.get('point_of_care', 'N/A')}, desc={row.get('exam_description', 'N/A')}")

    return df


def ingest_csv_files(data_dir: str = "data",
                    config_dir: str = "config",
                    pattern: str = "*_anon.csv",
                    db_path: Optional[str] = None) -> int:
    """
    Ingest all matching CSV files into DuckDB database using pandas preprocessing.

    Args:
        data_dir: Directory containing CSV files
        config_dir: Directory containing config files
        pattern: Glob pattern for CSV files
        db_path: Path to database file (uses default if None)

    Returns:
        Number of rows ingested
    """
    # Convert to absolute paths from script location
    script_dir = Path(__file__).parent.parent
    config_path = script_dir / config_dir if not Path(config_dir).is_absolute() else Path(config_dir)
    data_path = script_dir / data_dir if not Path(data_dir).is_absolute() else Path(data_dir)

    # Load configurations
    logger.info(f"Loading configurations from {config_path}...")
    configs = load_all_configs(config_path)
    attending_divisions = configs['attending_divisions']
    cpt_divisions = configs['cpt_divisions']
    shifts = configs.get('shifts', [])

    logger.info(f"Loaded {len(attending_divisions)} attending divisions")
    logger.info(f"Loaded {len(cpt_divisions)} CPT divisions")
    logger.info(f"Loaded {len(shifts)} shift definitions")

    # Load schedule for shift assignment
    schedule = load_schedule(config_path)
    logger.info(f"Loaded {len(schedule)} days of schedule")

    # Connect to database
    if db_path is None:
        db_path = data_path / "rvu.db"
    else:
        db_path = Path(db_path)

    logger.info(f"Connecting to database: {db_path}")
    con = get_connection(db_path)

    # Create schema if needed
    logger.info("Ensuring database schema exists...")
    create_schema(con)

    # Get initial row count
    initial_count = get_row_count(con)
    logger.info(f"Initial row count: {initial_count:,}")

    # Find CSV files
    data_path = Path(data_dir)
    csv_files = sorted(data_path.glob(pattern))

    if not csv_files:
        logger.warning(f"No CSV files found matching pattern '{pattern}' in {data_dir}")
        con.close()
        return 0

    logger.info(f"Found {len(csv_files)} CSV files to process")

    # Define valid divisions
    valid_divisions = ['NEURO', 'MSK', 'BODY', 'CHEST', 'IR', 'NUCLEAR', 'NIR', 'MAMMO']

    # Separate attendings into single-division and multi-division
    single_division_attendings = {}
    multi_division_attendings = {}

    for attending, division in attending_divisions.items():
        divisions = [d.strip() for d in division.split(',')]
        valid_atts_divs = [d for d in divisions if d in valid_divisions]

        if len(valid_atts_divs) == 1:
            single_division_attendings[attending] = valid_atts_divs[0]
        elif len(valid_atts_divs) > 1:
            multi_division_attendings[attending] = valid_atts_divs

    logger.info(f"Single-division attendings: {len(single_division_attendings)}")
    logger.info(f"Multi-division attendings: {len(multi_division_attendings)}")

    # Process each CSV file
    for csv_file in csv_files:
        logger.info(f"Loading {csv_file.name}...")

        # Load CSV into pandas
        df = pd.read_csv(csv_file)
        logger.info(f"  Loaded {len(df):,} rows into pandas")

        # Rename columns to match database schema
        column_mapping = {
            'Accession Number': 'accession_number',
            'Point of Care': 'point_of_care',
            'Source System': 'source_system',
            'Modality': 'modality',
            'Exam Code': 'exam_code',
            'Exam Description': 'exam_description',
            'CPT Code': 'cpt_code',
            'Is Stat': 'is_stat',
            'Patient Status': 'patient_status_raw',
            'Ordered Date': 'ordered_date',
            'Scheduled Date': 'scheduled_date',
            'Patient Arrived Date': 'patient_arrived_date',
            'Exam Started Date': 'exam_started_date',
            'Exam Completed Date': 'exam_completed_date',
            'Report Created Date': 'report_created_date',
            'Preliminary Report Date': 'preliminary_report_date',
            'Report Finalized Date': 'report_finalized_date',
            'Report Addendum Date': 'report_addendum_date',
            'Report Finalized By': 'report_finalized_by',
            'Work (RVU)': 'work_rvu',
            'PE (RVU)': 'pe_rvu',
            'MP (RVU)': 'mp_rvu',
            'Work (Professional) (RVU)': 'work_professional_rvu',
            'PE (Professional) (RVU)': 'pe_professional_rvu',
            'MP (Professional) (RVU)': 'mp_professional_rvu',
            'Work (Technical) (RVU)': 'work_technical_rvu',
            'PE (Technical) (RVU)': 'pe_technical_rvu',
            'MP (Technical) (RVU)': 'mp_technical_rvu',
            'Total (RVU)': 'total_rvu',
            'Total (Professional) (RVU)': 'total_professional_rvu',
            'Total (Technical) (RVU)': 'total_technical_rvu',
        }

        df = df.rename(columns=column_mapping)

        # Generate synthetic accession numbers if missing
        if 'accession_number' not in df.columns:
            import hashlib
            logger.info("  No accession_number column — generating synthetic IDs from row content")
            df['accession_number'] = df.apply(
                lambda row: hashlib.sha256(row.to_string().encode('utf-8')).hexdigest(),
                axis=1
            )

        # Trim whitespace from report_finalized_by
        df['report_finalized_by'] = df['report_finalized_by'].str.strip()

        # Convert is_stat to boolean
        if 'is_stat' in df.columns:
            df['is_stat'] = df['is_stat'] == 'True'

        # Convert date columns
        date_columns = [
            'ordered_date', 'scheduled_date', 'patient_arrived_date',
            'exam_started_date', 'exam_completed_date', 'report_created_date',
            'preliminary_report_date', 'report_finalized_date', 'report_addendum_date'
        ]
        for col in date_columns:
            if col in df.columns:
                # See `_to_pacific_naive` in assign_shifts_in_dataframe — same logic, inlined
                # to keep this loop simple. UTC parse handles mixed PST/PDT offsets across the
                # DST boundary, then we land on naive Pacific wall-clock for DB storage.
                df[col] = (
                    pd.to_datetime(df[col], errors='coerce', utc=True)
                      .dt.tz_convert(PACIFIC_TZ)
                      .dt.tz_localize(None)
                )

        # Convert RVU columns to numeric
        rvu_columns = [
            'work_rvu', 'pe_rvu', 'mp_rvu',
            'work_professional_rvu', 'pe_professional_rvu', 'mp_professional_rvu',
            'work_technical_rvu', 'pe_technical_rvu', 'mp_technical_rvu',
            'total_rvu', 'total_professional_rvu', 'total_technical_rvu'
        ]
        for col in rvu_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # Canonicalize CPT once so ALL downstream CPT logic (CTA H&N remap, cpt_divisions
        # lookup, spine-MRI reclassification, dedup_key hashing) sees the same value.
        # mPower rows arrive as pandas-coerced floats ('70450.0') while PowerScribe uses
        # ints ('70450'); without this, mPower's cpt_division lookup silently returns NaN
        # (→ NULL division → excluded from every WHERE division='NEURO' aggregation) and
        # the 70496→70471 remap misses every mPower H&N CTA.
        # Cast to object first — if the CSV happens to have a homogeneous numeric column
        # (e.g. all floats), pandas infers float64 dtype and the string-valued assignment
        # from _normalize_cpt fails with a dtype error. Only normalize non-null cells so
        # the later dropna(subset=['cpt_code']) still fires on rows that arrived with a
        # missing CPT (_normalize_cpt would coerce NaN → '').
        if 'cpt_code' in df.columns:
            df['cpt_code'] = df['cpt_code'].astype('object')
            cpt_mask = df['cpt_code'].notna()
            df.loc[cpt_mask, 'cpt_code'] = df.loc[cpt_mask, 'cpt_code'].map(_normalize_cpt)

        # Map patient_type. Accept both PowerScribe long form ('Emergency', 'Inpatient',
        # 'Outpatient') and mPower single-char codes ('E', 'I', 'O').
        df['patient_type'] = df['patient_status_raw'].str.lower().str.strip().map({
            'emergency': 'ER', 'e': 'ER',
            'inpatient': 'INPATIENT', 'i': 'INPATIENT',
            'outpatient': 'OUTPATIENT', 'o': 'OUTPATIENT',
        }).fillna('OUTPATIENT')

        # CTA H&N combined-code remap: PowerScribe stores combined CT angiogram of head + neck
        # as a single 70496 row credited at 1.71 wRVU; department billing uses 2026 code 70471
        # at 2.5 wRVU per study. Remap to align with dept billing.
        cta_hn_mask = (
            (df['cpt_code'].astype(str) == '70496')
            & df['exam_description'].fillna('').str.upper().str.contains('HEAD AND NECK')
        )
        if cta_hn_mask.any():
            n_remapped = int(cta_hn_mask.sum())
            df.loc[cta_hn_mask, 'cpt_code'] = '70471'
            df.loc[cta_hn_mask, 'work_professional_rvu'] = 2.5
            logger.info(f"  Remapped {n_remapped:,} 'HEAD AND NECK' rows from 70496 to 70471 (2.5 wRVU)")

        # Map cpt_division
        df['cpt_division'] = df['cpt_code'].map(cpt_divisions)

        # Drop records with missing required fields
        pre_count = len(df)
        df = df.dropna(subset=['report_finalized_by', 'cpt_code'])
        logger.info(f"  Dropped {pre_count - len(df):,} records missing required fields")

        # Filter out external points of care (starting with PLA)
        pre_filter_count = len(df)
        df = df[~df['point_of_care'].str.startswith('PLA', na=False)]
        filtered_count = pre_filter_count - len(df)
        if filtered_count > 0:
            logger.info(f"  Filtered out {filtered_count:,} records with external point of care (PLA*)")

        # Assign division
        df['division'] = df['report_finalized_by'].map(single_division_attendings)
        # For multi-division attendings, use cpt_division
        df['division'] = df['division'].fillna(df['cpt_division'])

        # Spine MRI reclassification: for neuro division attendings,
        # spine MRIs (thoracic/lumbar) that map to MSK by CPT should be NEURO
        neuro_attending_ids = set(configs.get('neuro_config', {}).get('attendings', {}).keys())
        spine_mri_cpts = {'72146', '72147', '72148', '72149', '72157', '72158'}
        spine_neuro_mask = (
            df['report_finalized_by'].isin(neuro_attending_ids) &
            df['cpt_code'].isin(spine_mri_cpts) &
            (df['division'] != 'NEURO')
        )
        if spine_neuro_mask.any():
            df.loc[spine_neuro_mask, 'division'] = 'NEURO'
            logger.info(f"  Reclassified {spine_neuro_mask.sum():,} spine MRIs as NEURO for neuro attendings")

        # Assign shifts in pandas (after filtering)
        if shifts:
            logger.info("Assigning shifts in pandas...")
            we_er_assignments = load_weekend_er_assignments(config_path)
            df = assign_shifts_in_dataframe(df, shifts, schedule, we_er_assignments)
        else:
            # Initialize shift columns
            df['shift_name'] = None
            df['is_flex'] = False
            df['is_evening_er'] = False
            df['evening_er_type'] = None

        # Compute cross-source dedup key from (finalized_date, cpt_code, finalized_by).
        # See compute_dedup_key() for the rationale — same exam → same key regardless of
        # whether the row came from PowerScribe (with accession) or mPower (without).
        logger.info("  Computing dedup_key for %s rows (parallel)", len(df))
        df['dedup_key'] = compute_dedup_keys_bulk(df)

        # Drop processing-only columns that aren't in the database schema
        # Get expected columns from database schema (must match exactly - order matters!)
        expected_columns = [
            'accession_number', 'point_of_care', 'source_system', 'modality', 'exam_code', 'exam_description',
            'cpt_code', 'is_stat', 'patient_status_raw', 'ordered_date', 'scheduled_date', 'patient_arrived_date',
            'exam_started_date', 'exam_completed_date', 'report_created_date', 'preliminary_report_date',
            'report_finalized_date', 'report_addendum_date', 'report_finalized_by', 'work_rvu', 'pe_rvu', 'mp_rvu',
            'work_professional_rvu', 'pe_professional_rvu', 'mp_professional_rvu', 'work_technical_rvu',
            'pe_technical_rvu', 'mp_technical_rvu', 'total_rvu', 'total_professional_rvu', 'total_technical_rvu',
            'division', 'cpt_division', 'patient_type', 'is_evening_er', 'evening_er_type', 'is_flex',
            'flex_shift_name',  # Legacy field (deprecated, always NULL)
            'shift_name',
            'is_after_hours',
            'dedup_key',
        ]

        # Add flex_shift_name if not present (legacy field, always NULL)
        if 'flex_shift_name' not in df.columns:
            df['flex_shift_name'] = None

        # Keep only expected columns in the right order
        df = df[[col for col in expected_columns if col in df.columns]]

        # Intra-batch dedup FIRST. Two conditions can produce within-batch duplicates:
        #   1. mPower pagination overlap returning the same report twice.
        #   2. The synthesized-accession fallback (sha256(row.to_string())) collapsing two
        #      identical rows to the same key — rare but possible when a report emits
        #      the same CPT twice.
        # Deduping here guarantees the INSERT sees at most one row per (dedup_key,
        # accession_number) and never hits the PK constraint from within the batch.
        # Tiebreaker: highest work_professional_rvu, ties broken by lowest accession_number.
        # Match dedupe_by_key.py so ingest-path and cleanup-path pick the same survivor —
        # otherwise on cross-source duplicates ingest could keep the older-RBRVS 1.71 wRVU
        # row over the current 2.5 wRVU row purely because of glob order.
        before = len(df)
        df = df.sort_values(
            ['work_professional_rvu', 'accession_number'],
            ascending=[False, True],
            na_position='last',
        )
        df = df.drop_duplicates(subset='dedup_key', keep='first')
        df = df.drop_duplicates(subset='accession_number', keep='first')
        if len(df) < before:
            logger.warning(f"  Dropped {before - len(df):,} intra-batch duplicates before dedup vs DB")

        # Dedup by EITHER key. Both are indexed and unique-ish:
        #  - accession_number is the primary key; a match means we're re-ingesting the
        #    exact same source row (even if a later export nudged some field's precision
        #    enough to change the dedup_key).
        #  - dedup_key catches cross-source duplicates (same exam from PowerScribe and
        #    mPower, which have different accession values).
        # Skipping on either match keeps the pipeline safe against both PK violations
        # and cross-source double-counting.
        logger.info("  Inserting into database...")
        existing_dedup = set(
            r[0] for r in con.execute(
                'SELECT dedup_key FROM exams WHERE dedup_key IS NOT NULL'
            ).fetchall()
        )
        existing_accession = set(
            r[0] for r in con.execute('SELECT accession_number FROM exams').fetchall()
        )
        dupe_mask = (
            df['dedup_key'].isin(existing_dedup)
            | df['accession_number'].isin(existing_accession)
        )
        dupes = int(dupe_mask.sum())
        if dupes:
            logger.warning(f"  Skipping {dupes:,} duplicate rows (by dedup_key or accession)")
            df = df[~dupe_mask]
        con.register('exams_df', df)
        # BY NAME matches source→target columns by name, not position. Required because a
        # migrated DB (columns added via ALTER TABLE, which appends at the end) has a
        # different physical column order than a freshly create_schema'd one (CREATE TABLE
        # order). Positional `SELECT *` would misalign is_after_hours / dedup_key on any
        # ALTER-migrated DB and fail the type check. Target columns absent from the
        # DataFrame get NULL.
        con.execute('INSERT INTO exams BY NAME SELECT * FROM exams_df')
        con.unregister('exams_df')

        logger.info(f"  Inserted {len(df):,} rows")

    # Get final row count
    final_count = get_row_count(con)
    new_rows = final_count - initial_count

    logger.info(f"Ingestion complete:")
    logger.info(f"  - New rows added: {new_rows:,}")
    logger.info(f"  - Final row count: {final_count:,}")

    # Close connection
    con.close()

    return final_count


def reassign_shifts_in_db(config_dir: str = "config", db_path: Optional[str] = None,
                          since: Optional[str] = None) -> int:
    """Re-run shift assignment against rows already in the database, without re-ingesting CSVs.

    Use this after the schedule has been updated (or the shift criteria changed) and you want
    to refresh shift_name / is_flex / is_evening_er / evening_er_type on existing rows. The CSV
    ingest path skips duplicates by accession_number, so plain re-ingest doesn't re-classify.

    Args:
        since: ISO date string. If provided, only rows with
            report_finalized_date >= since are re-tagged. Used by the weekly cron to
            refresh just the current window (rows that were tagged during stage 2 with
            a stale weekend_er_assignments.csv, before stage 3 regenerated it). Full-DB
            re-tag remains available by omitting the flag.

    Pipeline (mirrors the relevant portion of `ingest_csv_files`):
      1. Load the current schedule and shift definitions.
      2. SELECT the relevant exam columns for every row in the DB (or window).
      3. Call `assign_shifts_in_dataframe` — same function ingest uses.
      4. Apply the Inpatient A → Moonlight pre-shift re-tag (first 22 IA exams per weekday).
      5. UPDATE every row's shift fields via a DataFrame join.

    Returns the number of rows updated.
    """
    from pathlib import Path
    config_path = Path(config_dir) if Path(config_dir).is_absolute() else Path(__file__).parent.parent / config_dir

    logger.info("Loading shift definitions and schedule...")
    configs = load_all_configs(config_path)
    shifts = configs.get('shifts', [])
    schedule = load_schedule(config_path)
    logger.info(f"Loaded {len(shifts)} shift definitions, {len(schedule)} schedule days")

    if not shifts:
        logger.error("No shift definitions loaded; aborting.")
        return 0

    if db_path is None:
        from .database import get_db_path
        db_path = get_db_path()
    con = get_connection(db_path)

    initial_count = get_row_count(con)
    logger.info(f"DB has {initial_count:,} exam rows")

    exam_cols = [
        'accession_number', 'exam_completed_date', 'report_finalized_date',
        'report_finalized_by', 'patient_type', 'cpt_division', 'division',
    ]
    where_clause = ''
    if since:
        # Guard against SQL injection by validating the ISO date format via fromisoformat.
        from datetime import date as _d
        _d.fromisoformat(since)  # raises ValueError on bad input
        where_clause = f" WHERE report_finalized_date >= '{since}'"
        logger.info(f"Scoping re-assignment to rows with report_finalized_date >= {since}")
    logger.info("Loading exam rows from DB...")
    df = con.execute(f"SELECT {','.join(exam_cols)} FROM exams{where_clause}").fetchdf()
    logger.info(f"Loaded {len(df):,} rows")

    logger.info("Re-deriving shift tags for the selected rows (full shift assignment + IA first-22 "
                "retag + is_after_hours). Reconciles per-day aggregates against the now-complete data; "
                "also picks up any schedule/shift-criteria change when run without --since.")
    we_er_assignments = load_weekend_er_assignments(config_path)
    df = assign_shifts_in_dataframe(df, shifts, schedule, we_er_assignments)
    # NOTE: assign_shifts_in_dataframe already includes the Inpatient A → Moonlight pre-shift
    # re-tag (first 22 weekday IA exams per day). Do NOT repeat it here — a second pass would
    # consume another 22 from the remaining IA-tagged exams, zeroing out IA entirely for many days.

    logger.info("Distribution after re-assignment:")
    for shift_name, count in df['shift_name'].value_counts(dropna=False).items():
        logger.info(f"  {shift_name or 'NULL'}: {count:,}")

    update_df = df[['accession_number', 'shift_name', 'is_flex', 'is_evening_er', 'evening_er_type', 'is_after_hours']].copy()
    update_df['is_flex'] = update_df['is_flex'].astype(bool)
    update_df['is_evening_er'] = update_df['is_evening_er'].astype(bool)
    update_df['is_after_hours'] = update_df['is_after_hours'].astype(bool)

    logger.info("Applying UPDATE...")
    con.register('reassign_updates', update_df)
    con.execute("""
        UPDATE exams
        SET shift_name = reassign_updates.shift_name,
            is_flex = reassign_updates.is_flex,
            is_evening_er = reassign_updates.is_evening_er,
            evening_er_type = reassign_updates.evening_er_type,
            is_after_hours = reassign_updates.is_after_hours
        FROM reassign_updates
        WHERE exams.accession_number = reassign_updates.accession_number
    """)
    con.unregister('reassign_updates')

    final_count = get_row_count(con)
    logger.info(f"Re-assignment complete. Row count: {final_count:,} (unchanged: {final_count == initial_count})")
    con.close()
    return len(update_df)


def main():
    """CLI entry point for ingestion."""
    import argparse

    parser = argparse.ArgumentParser(description='Ingest RVU data from CSV files into DuckDB')
    parser.add_argument('--data-dir', default='data', help='Directory containing CSV files')
    parser.add_argument('--config-dir', default='config', help='Directory containing config files')
    parser.add_argument('--pattern', default='*_anon.csv', help='Glob pattern for CSV files')
    parser.add_argument('--db-path', help='Path to database file')
    parser.add_argument('--reassign-shifts-only', action='store_true',
                        help="Skip CSV ingest; re-apply shift assignment to existing DB rows only. "
                             "Use after schedule edits to refresh shift_name on already-ingested exams.")
    parser.add_argument('--since', default=None,
                        help="Combined with --reassign-shifts-only, only re-tag rows with "
                             "report_finalized_date >= YYYY-MM-DD. Weekly cron uses this to "
                             "avoid rewriting the entire DB on every run.")

    args = parser.parse_args()

    if args.reassign_shifts_only:
        reassign_shifts_in_db(
            config_dir=args.config_dir,
            db_path=args.db_path,
            since=args.since,
        )
    else:
        ingest_csv_files(
            data_dir=args.data_dir,
            config_dir=args.config_dir,
            pattern=args.pattern,
            db_path=args.db_path
        )


if __name__ == '__main__':
    main()
