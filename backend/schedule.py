"""Schedule parsing and shift assignment logic.

Handles:
- CSV schedule parsing
- Shift matching based on schedule assignment + exam criteria
- On-call exclusion
"""

import csv
from datetime import datetime, time, timedelta, date
from pathlib import Path
from typing import Dict, List, Optional, Any, Set
from collections import defaultdict


def parse_time_str(time_str: str) -> time:
    """Parse time string in HH:MM or HH:MM:SS format."""
    # Add seconds if missing for consistent parsing
    if time_str.count(':') == 1:
        time_str = f"{time_str}:00"
    return time.fromisoformat(time_str)


def datetime_in_window(
    check_dt: datetime,
    window_start_time: time,
    window_end_time: time,
    start_offset: int = 0,
    end_offset: int = 0,
    reference_date: datetime = None
) -> bool:
    """
    Check if a datetime falls within a time window, accounting for day offsets.

    This handles overnight windows correctly by comparing full datetimes.

    Args:
        check_dt: Datetime to check
        window_start_time: Start time of window
        window_end_time: End time of window
        start_offset: Day offset for window start (0=same day, -1=previous day, 1=next day)
        end_offset: Day offset for window end
        reference_date: Reference date for offset calculations (uses check_dt if None)

    Returns:
        True if check_dt falls within the window
    """
    if reference_date is None:
        reference_date = check_dt

    # Calculate the start and end datetimes with offsets
    # Start from reference date at midnight and add time + offset
    ref_midnight = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)

    start_dt = ref_midnight + timedelta(days=start_offset) + timedelta(
        hours=window_start_time.hour,
        minutes=window_start_time.minute,
        seconds=window_start_time.second
    )
    end_dt = ref_midnight + timedelta(days=end_offset) + timedelta(
        hours=window_end_time.hour,
        minutes=window_end_time.minute,
        seconds=window_end_time.second
    )

    # Check if within window
    return start_dt <= check_dt <= end_dt


def parse_schedule_csv(csv_path: Path) -> Dict[str, Dict[str, Any]]:
    """
    Parse schedule CSV file.

    Expected CSV format:
    date,shift_name,attending,on_call,notes
    2025-02-01,Inpatient A,"Fussell, David",,
    2025-02-01,Evening ER,"Soun, Jennifer E",true,
    2025-02-02,Weekend Morning,"Chow, Daniel S",,

    Args:
        csv_path: Path to schedule CSV file

    Returns:
        Dict mapping date string to list of shift assignments
        Format: { "YYYY-MM-DD": [{"shift_name": "...", "attending": "...", "on_call": bool, ...}, ...] }
    """
    schedule = defaultdict(list)

    if not csv_path.exists():
        return dict(schedule)

    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)

        for row in reader:
            date = row.get('date', '').strip()
            shift_name = row.get('shift_name', '').strip()
            attending = row.get('attending', '').strip()

            if not date or not shift_name or not attending:
                continue

            # Parse on_call field
            on_call_raw = row.get('on_call', '').strip().lower()
            on_call = on_call_raw in ('true', 'yes', '1', 'y')

            schedule[date].append({
                'shift_name': shift_name,
                'attending': attending,
                'on_call': on_call,
                'notes': row.get('notes', '').strip()
            })

    return dict(schedule)


def parse_neuro_schedule_gsheet(csv_path: Path, attending_id_map: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """
    Parse Neuro division schedule from Google Sheet export format.

    The CSV is in wide/matrix format:
    - Row 1: Headers (Month, Date, Day, Call, then attending names)
    - Rows 2+: Each row is a date with shift assignments for each attending

    Shift names to extract: InpatientA, InpatientB, Flex, Flex/Nights, OutpatientA, OutpatientB
    Everything else (Weekend, Vacation, Holiday, Academic, Conference, etc.) is ignored.

    Args:
        csv_path: Path to neuro schedule CSV file
        attending_id_map: Dict mapping real names to ATT-x codes

    Returns:
        Dict mapping date string (YYYY-MM-DD) to dict mapping attending_id to shift_name
        Format: { "2025-07-01": {"ATT000001": "Inpatient A", "ATT000002": "Outpatient A", ...} }
    """
    import pandas as pd

    if not csv_path.exists():
        return {}

    # Read CSV
    df = pd.read_csv(csv_path, header=0)

    # Infer academic year from filename (e.g., "24-25" -> 2024 for dates before July)
    # Academic years run July-June, so 24-25 = July 2024 to June 2025
    default_year = None
    filename = csv_path.stem
    if "neuro_schedule_" in filename:
        year_part = filename.replace("neuro_schedule_", "")
        if "-" in year_part:
            try:
                start_year = int("20" + year_part.split("-")[0])
                default_year = start_year
            except:
                pass

    # Find the date column (either "Date" or second column)
    date_col = None
    for col in df.columns:
        if col == "Date":
            date_col = col
            break

    if date_col is None:
        # Try second column
        date_col = df.columns[1] if len(df.columns) > 1 else None

    if date_col is None:
        return {}

    # Find attending name columns (skip Month, Date, Day, Call, and Unnamed columns)
    attending_columns = []
    for col in df.columns:
        if col not in ["Month", "Date", "Day", "Call"] and not col.startswith("Unnamed"):
            attending_columns.append(col)

    # Normalize attending names to match the ID map
    def normalize_name(name):
        return str(name).strip().upper() if pd.notna(name) and str(name).strip() else ""

    # Create reverse mapping: normalized name -> attending_id
    name_to_id = {}
    for real_name, att_id in attending_id_map.items():
        normalized = normalize_name(real_name)
        name_to_id[normalized] = att_id

    # Valid shift names (normalized, with variations)
    valid_shifts = {
        "INPATIENTA": "Inpatient A",
        "INPATIENTB": "Inpatient B",
        "FLEX": "Flex",
        "FLEX/NIGHTS": "Flex/Nights",
        "OUTPATIENTA": "Outpatient A",
        "OUTPATIENTB": "Outpatient B",
    }

    # Also accept variations
    shift_variations = {
        "INPATIENTA": ["INPATIENTA", "INPATIENT A", "INPATIENT-A"],
        "INPATIENTB": ["INPATIENTB", "INPATIENT B", "INPATIENT-B"],
        "FLEX": ["FLEX"],
        "FLEX/NIGHTS": ["FLEX/NIGHTS", "FLEX/NIGHTS", "FLEX-NIGHTS"],
        "OUTPATIENTA": ["OUTPATIENTA", "OUTPATIENT A", "OUTPATIENT-A"],
        "OUTPATIENTB": ["OUTPATIENTB", "OUTPATIENT B", "OUTPATIENT-B"],
    }

    def normalize_shift(shift):
        return shift.replace(" ", "").replace("/", "").replace("-", "").upper()

    def is_valid_shift(shift_str):
        normalized = normalize_shift(shift_str)
        for canonical, variations in shift_variations.items():
            if normalized in [normalize_shift(v) for v in variations]:
                return valid_shifts[canonical]  # Return "Inpatient A" not "INPATIENTA"
        return None

    def classify_status(cell_str):
        """Map a non-shift cell value to a normalized status label, or None if blank."""
        if not cell_str:
            return None
        s = cell_str.strip()
        if not s:
            return None
        lower = s.lower()
        if 'vacation' in lower:
            return 'Vacation'
        if 'academic' in lower or lower == 'academics':
            return 'Academic'
        if 'conference' in lower or 'converence' in lower:
            return 'Conference'
        # 'sick' covers 'Sick', 'Sick Day', 'Out Sick', 'Sick Leave', etc. Also covid as a variant.
        if 'sick' in lower or 'covid' in lower:
            return 'Sick'
        if lower == 'ucsf':
            return 'UCSF'
        if 'holiday' in lower:
            return 'Holiday'
        if 'weekend' in lower:
            return 'Weekend'
        if 'no call' in lower:
            return 'No Call'
        # Unrecognized — preserve raw (trimmed) for display
        return s

    # Parse schedule
    schedule = {}
    on_call_by_date = {}  # Track who's on call each date
    holidays_by_date = {}  # Track which dates are holidays

    for _, row in df.iterrows():
        date_val = row[date_col]
        if pd.isna(date_val):
            continue

        # Parse date - handle M/D/YY or M/D format
        date_str = str(date_val).strip()
        try:
            parts = date_str.split('/')
            if len(parts) == 3:
                # M/D/YY or M/D/YYYY format
                month, day, year = parts
                year_int = int(year)
                # Only pivot 2-digit years; a 4-digit year like "2025" would otherwise
                # trip the >= 50 branch and become 3925, silently blanking that day's
                # shift assignments.
                if len(year.strip()) <= 2:
                    year_int += 2000 if year_int < 50 else 1900
                iso_date = f"{year_int}-{month.zfill(2)}-{day.zfill(2)}"
            elif len(parts) == 2 and default_year:
                # M/D format - infer year from academic year context
                # Academic years run July-June (e.g., 24-25 = July 2024 to June 2025)
                month, day = parts
                month_int = int(month)
                # July-Dec use default_year, Jan-Jun use default_year + 1
                if month_int >= 7:
                    year_int = default_year
                else:
                    year_int = default_year + 1
                iso_date = f"{year_int}-{month.zfill(2)}-{day.zfill(2)}"
            else:
                continue
        except:
            continue

        # Track who's on call
        call_val = row.get("Call", "")
        if pd.notna(call_val) and str(call_val).strip():
            call_name = normalize_name(call_val)
            if call_name in name_to_id:
                on_call_by_date[iso_date] = name_to_id[call_name]

        # Detect holidays: if there's at least one "Holiday" in the row and someone is on call
        has_holiday = False
        for col in attending_columns:
            shift_val = row[col]
            if pd.notna(shift_val) and str(shift_val).strip():
                shift_str = str(shift_val).strip().lower()
                if shift_str == "holiday":
                    has_holiday = True
                    break

        # Mark as holiday if detected and someone is on call
        if has_holiday and iso_date in on_call_by_date:
            holidays_by_date[iso_date] = True

        # Parse shift assignments for each attending
        date_shifts = {}
        date_statuses = {}
        for col in attending_columns:
            shift_val = row[col]
            if pd.notna(shift_val) and str(shift_val).strip():
                shift_str = str(shift_val).strip()
                attending_name = normalize_name(col)
                if attending_name not in name_to_id:
                    continue
                att_id = name_to_id[attending_name]
                # Check if it's a valid shift first
                valid_shift = is_valid_shift(shift_str)
                if valid_shift:
                    date_shifts[att_id] = valid_shift
                else:
                    # Otherwise, capture as a status (Vacation, Academic, etc.)
                    status = classify_status(shift_str)
                    if status:
                        date_statuses[att_id] = status

        # Include date in schedule if there are valid shifts/statuses OR on-call info
        # (needed for Weekend Call shift which relies on on-call info)
        if date_shifts or date_statuses or iso_date in on_call_by_date:
            schedule[iso_date] = date_shifts

        # Stash statuses in metadata so they survive to consumers that want them
        # (without polluting count_shifts_by_attending which iterates the top-level dict).
        if iso_date in schedule and date_statuses:
            schedule[iso_date]['_statuses'] = date_statuses

    # Add on-call info and holiday flag to schedule
    for date_val, shifts in schedule.items():
        shifts["_on_call"] = on_call_by_date.get(date_val)
        shifts["_is_holiday"] = date_val in holidays_by_date

    return schedule


def load_schedule_from_yaml(config_dir: Path) -> Dict[str, Dict[str, Any]]:
    """
    Load schedule from legacy YAML format (for backward compatibility).

    Args:
        config_dir: Path to config directory

    Returns:
        Dict mapping date string to list of shift assignments
    """
    import yaml

    schedule_file = config_dir / "schedule.yaml"

    if not schedule_file.exists():
        return {}

    with open(schedule_file, 'r') as f:
        data = yaml.safe_load(f)

    # Convert legacy on_call format to new format
    schedule = defaultdict(list)

    if 'on_call' in data:
        for date_str, assignment in data['on_call'].items():
            if isinstance(assignment, dict):
                schedule[date_str].append({
                    'shift_name': 'On Call',
                    'attending': assignment.get('attending', ''),
                    'on_call': True,
                    'notes': ''
                })

    return dict(schedule)


def get_shift_for_date(
    exam_date: str,
    attending: str,
    schedule: Dict[str, Dict[str, Any]]
) -> Optional[str]:
    """
    Get the shift assigned to an attending for a specific date.

    Args:
        exam_date: Date string (YYYY-MM-DD)
        attending: Attending name
        schedule: Schedule dict from parse_schedule_csv

    Returns:
        Shift name if found, None otherwise
    """
    if exam_date not in schedule:
        return None

    for assignment in schedule[exam_date]:
        if assignment['attending'] == attending:
            return assignment['shift_name']

    return None


def is_attending_on_call(
    exam_date: str,
    attending: str,
    schedule: Dict[str, Dict[str, Any]]
) -> bool:
    """
    Check if an attending is on-call for a specific date.

    Args:
        exam_date: Date string (YYYY-MM-DD)
        attending: Attending name
        schedule: Schedule dict from parse_schedule_csv

    Returns:
        True if attending is on-call, False otherwise
    """
    if exam_date not in schedule:
        return False

    for assignment in schedule[exam_date]:
        if assignment['attending'] == attending and assignment['on_call']:
            return True

    return False


def time_in_window(
    check_time: time,
    window_start: time,
    window_end: time,
    start_offset: int = 0,
    end_offset: int = 0
) -> bool:
    """
    Check if a time falls within a time window, accounting for day offsets.

    Args:
        check_time: Time to check
        window_start: Start of window
        window_end: End of window
        start_offset: Day offset for window start (0=same day, -1=previous day, 1=next day)
        end_offset: Day offset for window end

    Returns:
        True if check_time falls within the window
    """
    # Convert to minutes for easier comparison
    check_minutes = check_time.hour * 60 + check_time.minute
    start_minutes = window_start.hour * 60 + window_start.minute
    end_minutes = window_end.hour * 60 + window_end.minute

    # Handle day offsets
    if start_offset != 0:
        start_minutes += start_offset * 24 * 60

    if end_offset != 0:
        end_minutes += end_offset * 24 * 60

    # Check if within window
    # Handle windows that cross day boundaries (e.g., 22:00 to 08:00)
    if start_minutes <= end_minutes:
        # Normal window within same day or consecutive days
        return start_minutes <= check_minutes <= end_minutes
    else:
        # Window crosses day boundary (e.g., 22:00 with offset -1 to 08:00 with offset 0)
        return check_minutes >= start_minutes or check_minutes <= end_minutes


def exam_matches_shift_criteria(
    exam_performed: Optional[datetime],
    exam_finalized: datetime,
    patient_type: str,
    cpt_division: Optional[str],
    shift: Dict[str, Any],
    exam_date: str
) -> bool:
    """
    Check if an exam matches a shift's criteria.

    Uses minute-based comparisons for fast evaluation.

    Args:
        exam_performed: When exam was completed (exam_completed_date)
        exam_finalized: When report was finalized (report_finalized_date)
        patient_type: Patient type (ER, INPATIENT, OUTPATIENT)
        cpt_division: CPT division
        shift: Shift definition from shifts.yaml
        exam_date: Date string for the exam (YYYY-MM-DD)

    Returns:
        True if exam matches all shift criteria
    """
    scope = shift.get('scope', {})

    # Helper: convert time + day_offset to minutes from reference date midnight
    def time_to_minutes(t: time, day_offset: int) -> int:
        """Convert a time and day offset to minutes from reference midnight."""
        minutes_from_midnight = t.hour * 60 + t.minute
        return minutes_from_midnight + day_offset * 24 * 60

    # Parse exam date if it's a string, otherwise use as-is
    if isinstance(exam_date, str):
        exam_dt = datetime.fromisoformat(exam_date)
    elif isinstance(exam_date, date):
        exam_dt = datetime.combine(exam_date, datetime.min.time())
    else:
        exam_dt = exam_date

    # Check day_type
    day_type = scope.get('day_type', 'all')
    if day_type != 'all':
        is_weekend = exam_dt.weekday() >= 5  # Saturday=5, Sunday=6

        if day_type == 'weekday' and is_weekend:
            return False
        if day_type == 'weekend' and not is_weekend:
            return False

    # Get report date as reference point
    report_date = exam_finalized.date()

    # Calculate report finalized time as minutes from report date midnight
    report_min = exam_finalized.hour * 60 + exam_finalized.minute

    # Calculate exam completed time as minutes from report date midnight
    if exam_performed:
        exam_date_obj = exam_performed.date()
        days_diff = (exam_date_obj - report_date).days
        exam_min = exam_performed.hour * 60 + exam_performed.minute + days_diff * 24 * 60
    else:
        exam_min = None

    # Check exam_finalized_window (required)
    finalized_window = scope.get('exam_finalized_window')
    if finalized_window:
        start_info = finalized_window['start']
        end_info = finalized_window['end']

        start_time = parse_time_str(start_info['time'])
        end_time = parse_time_str(end_info['time'])
        start_offset = start_info.get('day_offset', 0)
        end_offset = end_info.get('day_offset', 0)

        start_min = time_to_minutes(start_time, start_offset)
        end_min = time_to_minutes(end_time, end_offset)

        if not (start_min <= report_min <= end_min):
            return False

    # Check exam_performed_window (optional)
    performed_window = scope.get('exam_performed_window')
    if performed_window and exam_min is not None:
        start_info = performed_window['start']
        end_info = performed_window['end']

        start_time = parse_time_str(start_info['time'])
        end_time = parse_time_str(end_info['time'])
        start_offset = start_info.get('day_offset', 0)
        end_offset = end_info.get('day_offset', 0)

        # `weekend_holiday_lookback`: if the day at (report_date + start_offset) was a weekend,
        # walk start_offset further back until it lands on a weekday. This handles the Monday
        # OPA/OPB case where the attending reads studies performed over the weekend — without
        # this extension, those reads fall outside the 24-hour window and get mis-tagged as Flex.
        if performed_window.get('weekend_holiday_lookback'):
            from datetime import timedelta as _td
            safety_limit = start_offset - 7  # don't run forever
            while start_offset > safety_limit:
                cand_date = report_date + _td(days=start_offset)
                if cand_date.weekday() < 5:  # Monday=0 .. Friday=4
                    break
                start_offset -= 1

        start_min = time_to_minutes(start_time, start_offset)
        end_min = time_to_minutes(end_time, end_offset)

        if not (start_min <= exam_min <= end_min):
            return False

    # Check patient_types
    patient_types = scope.get('patient_types')
    if patient_types and patient_type not in patient_types:
        return False

    # Check cpt_division
    required_cpt_division = scope.get('cpt_division')
    if required_cpt_division and cpt_division != required_cpt_division:
        return False

    # All criteria passed
    return True


def assign_shift_to_exam(
    exam_performed: Optional[datetime],
    exam_finalized: datetime,
    attending: str,
    patient_type: str,
    cpt_division: Optional[str],
    exam_date: str,
    shifts: List[Dict[str, Any]],
    schedule: Dict[str, Dict[str, Any]]
) -> tuple[Optional[str], Optional[str], bool, bool]:
    """
    Assign shift to an exam based on schedule and criteria.

    Args:
        exam_performed: When exam was completed
        exam_finalized: When report was finalized
        attending: Attending name
        patient_type: Patient type
        cpt_division: CPT division
        exam_date: Date string (YYYY-MM-DD)
        shifts: List of shift definitions
        schedule: Schedule dict

    Returns:
        Tuple of (shift_name, evening_er_type, is_flex, is_evening_er)
    """
    # First, check if schedule assigns a shift to this attending on this date
    assigned_shift_name = get_shift_for_date(exam_date, attending, schedule)

    # Check on_call exclusion
    on_call = is_attending_on_call(exam_date, attending, schedule)

    # If assigned a shift, check if exam matches that shift's criteria
    if assigned_shift_name:
        # Find the shift definition that's valid for this date
        exam_dt = datetime.fromisoformat(exam_date)

        for shift in shifts:
            if shift['name'] != assigned_shift_name:
                continue

            # Check validity period
            valid_from = datetime.fromisoformat(shift['valid_from'])
            valid_until = datetime.fromisoformat(shift['valid_until']) if shift.get('valid_until') else None

            if valid_until and exam_dt > valid_until:
                continue
            if exam_dt < valid_from:
                continue

            # Check if shift excludes on-call and this attending is on-call
            if shift.get('scope', {}).get('on_call_excluded') and on_call:
                return None, None, False, False

            # Check if exam matches shift criteria
            if exam_matches_shift_criteria(
                exam_performed, exam_finalized, patient_type, cpt_division, shift, exam_date
            ):
                return (
                    shift['name'],
                    'ER-NEURO' if shift.get('is_evening_er') and cpt_division == 'NEURO' else (
                        'ER-GENERAL' if shift.get('is_evening_er') else None
                    ),
                    shift.get('is_flex', False),
                    shift.get('is_evening_er', False)
                )

            # Shift assigned but criteria don't match - don't assign
            return None, None, False, False

    # No schedule assignment or didn't match - fall back to criteria-only matching
    # Find first shift whose criteria match
    exam_dt = datetime.fromisoformat(exam_date)

    for shift in shifts:
        # Check validity period
        valid_from = datetime.fromisoformat(shift['valid_from'])
        valid_until = datetime.fromisoformat(shift['valid_until']) if shift.get('valid_until') else None

        if valid_until and exam_dt > valid_until:
            continue
        if exam_dt < valid_from:
            continue

        # Check on-call exclusion
        if shift.get('scope', {}).get('on_call_excluded') and on_call:
            continue

        # Check criteria
        if exam_matches_shift_criteria(
            exam_performed, exam_finalized, patient_type, cpt_division, shift, exam_date
        ):
            return (
                shift['name'],
                'ER-NEURO' if shift.get('is_evening_er') and cpt_division == 'NEURO' else (
                    'ER-GENERAL' if shift.get('is_evening_er') else None
                ),
                shift.get('is_flex', False),
                shift.get('is_evening_er', False)
            )

    # No shift matched
    return None, None, False, False


def count_shifts_by_attending(
    schedule: Dict[str, Dict[str, Any]],
    start_date: str,
    end_date: str,
    neuro_config: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    Count scheduled shifts per attending within a date range.

    Args:
        schedule: Schedule dict from parse_neuro_schedule_gsheet
                  Format: { "YYYY-MM-DD": {"ATT000001": "Inpatient A", "_on_call": "ATT000002", "_is_holiday": False, ...} }
        start_date: Start date string (YYYY-MM-DD)
        end_date: End date string (YYYY-MM-DD)
        neuro_config: Neuro configuration with attending info

    Returns:
        List of dicts with {attending_id, attending_name, shift_name, count}
    """
    from collections import Counter

    # Get attending name map from neuro_config
    attending_name_map = {}
    if neuro_config and 'attendings' in neuro_config:
        for att_id, info in neuro_config['attendings'].items():
            attending_name_map[att_id] = info.get('name', att_id)

    # Count shifts per attending
    shift_counts = Counter()

    # Filter dates within range
    start_dt = datetime.fromisoformat(start_date)
    end_dt = datetime.fromisoformat(end_date)

    for date_str, date_schedule in schedule.items():
        try:
            date_dt = datetime.fromisoformat(date_str)
        except ValueError:
            continue

        if not (start_dt <= date_dt <= end_dt):
            continue

        # Iterate through attending assignments (skip metadata keys)
        for key, value in date_schedule.items():
            if key.startswith('_'):
                continue

            # key is attending_id, value is shift_name
            attending_id = key
            shift_name = value

            if shift_name:  # Only count if there's an actual shift assignment
                shift_counts[(attending_id, shift_name)] += 1

    # Convert to list format
    results = []
    for (attending_id, shift_name), count in shift_counts.items():
        results.append({
            'attending_id': attending_id,
            'attending_name': attending_name_map.get(attending_id, attending_id),
            'shift_name': shift_name,
            'count': count
        })

    # Merge in weekend evening ER assignments (extracted empirically from data,
    # not present in the gsheet schedule).
    return _merge_weekend_er(results, schedule, start_date, end_date, attending_name_map)


def _merge_weekend_er(results, schedule, start_date, end_date, attending_name_map):
    """Add 'Weekend Evening ER' counts from weekend_er_assignments.csv to a results list."""
    from collections import Counter
    from .extract_weekend_er import load_assignments
    weekend_er = load_assignments()
    weekend_counts = Counter()
    start_dt = datetime.fromisoformat(start_date)
    end_dt = datetime.fromisoformat(end_date)
    for date_str, att_id in weekend_er.items():
        try:
            d = datetime.fromisoformat(date_str)
        except ValueError:
            continue
        if start_dt <= d <= end_dt:
            weekend_counts[att_id] += 1
    for att_id, count in weekend_counts.items():
        results.append({
            'attending_id': att_id,
            'attending_name': attending_name_map.get(att_id, att_id),
            'shift_name': 'Weekend Evening ER',
            'count': count,
        })
    return results


def get_full_schedule(
    schedule: Dict[str, Dict[str, Any]],
    start_date: str,
    end_date: str,
    neuro_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return the per-day schedule between start_date and end_date inclusive.

    Each entry: {date, weekday, is_holiday, on_call, assignments: {att_id: shift_name}}.
    Used by the schedule planner to display the imported schedule and as a starting
    point for editable candidates.
    """
    start_dt = datetime.fromisoformat(start_date).date()
    end_dt = datetime.fromisoformat(end_date).date()
    out = []

    name_map = {}
    if neuro_config and 'attendings' in neuro_config:
        for att_id, info in neuro_config['attendings'].items():
            name_map[att_id] = info.get('name', att_id)

    cur = start_dt
    while cur <= end_dt:
        date_str = cur.isoformat()
        day_data = schedule.get(date_str, {}) or {}
        on_call = day_data.get('_on_call')
        is_holiday = bool(day_data.get('_is_holiday'))
        # Shifts are top-level keys; statuses live under '_statuses' (Vacation/Academic/etc.).
        # Merge both so the frontend gets a unified per-attending value per day.
        assignments = {
            k: v for k, v in day_data.items()
            if not k.startswith('_') and v
        }
        statuses = day_data.get('_statuses') or {}
        for att_id, status in statuses.items():
            if att_id not in assignments:
                assignments[att_id] = status
        out.append({
            'date': date_str,
            'weekday': cur.strftime('%a'),
            'day_of_week': cur.weekday(),  # 0=Monday, 6=Sunday
            'is_weekend': cur.weekday() >= 5,
            'is_holiday': is_holiday,
            'on_call': on_call,
            'on_call_name': name_map.get(on_call) if on_call else None,
            'assignments': assignments,
        })
        cur += timedelta(days=1)
    return out


