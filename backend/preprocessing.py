"""Preprocessing logic for RVU dashboard data.

Handles:
- Division assignment (from attending and CPT code)
- Patient type mapping
- Evening ER tagging
- Flex shift tagging
"""

from datetime import datetime, time
from typing import Dict, Optional, Any, List
import re


def map_patient_type(patient_status_raw: str) -> str:
    """
    Map raw Patient Status to standardized patient type.

    Handles both PowerScribe CSV values ('Emergency', 'Inpatient', 'Outpatient')
    and mPower API single-char codes ('E', 'I', 'O'). Fall back to OUTPATIENT
    for missing/unknown values.
    """
    if not patient_status_raw:
        return "OUTPATIENT"

    status_lower = str(patient_status_raw).lower().strip()

    if status_lower in ("emergency", "e"):
        return "ER"
    elif status_lower in ("inpatient", "i"):
        return "INPATIENT"
    elif status_lower in ("outpatient", "o"):
        return "OUTPATIENT"
    else:
        # Default to OUTPATIENT for any unknown values.
        return "OUTPATIENT"


def assign_attending_division(report_finalized_by: str,
                             attending_divisions: Dict[str, str]) -> Optional[str]:
    """
    Assign division based on attending radiologist name.

    Args:
        report_finalized_by: Name of attending who finalized report
        attending_divisions: Dict mapping attending names to divisions

    Returns:
        Division name or None if attending not found
    """
    if not report_finalized_by:
        return None

    # Try exact match
    if report_finalized_by in attending_divisions:
        return attending_divisions[report_finalized_by]

    # Try case-insensitive match
    name_lower = report_finalized_by.lower()
    for attending, division in attending_divisions.items():
        if attending.lower() == name_lower:
            return division

    return None


def assign_cpt_division(cpt_code: str,
                        cpt_divisions: Dict[str, str]) -> Optional[str]:
    """
    Assign division based on CPT code.

    Args:
        cpt_code: CPT code from exam (may have multiple codes)
        cpt_divisions: Dict mapping CPT codes to divisions

    Returns:
        Division name or None if CPT not found
    """
    if not cpt_code:
        return None

    # Try exact match
    if cpt_code in cpt_divisions:
        return cpt_divisions[cpt_code]

    # Handle multi-code entries (e.g., "72195, 74181, 76376")
    # Use the first code for lookup
    if ',' in cpt_code:
        first_code = cpt_code.split(',')[0].strip()
        if first_code in cpt_divisions:
            return cpt_divisions[first_code]

    return None


def is_evening_er_time(exam_completed_date: Optional[datetime]) -> bool:
    """
    Check if exam was completed during evening ER hours (4pm-10pm).

    Args:
        exam_completed_date: DateTime when exam was completed

    Returns:
        True if between 4pm-10pm (16:00-22:00), False otherwise
    """
    if not exam_completed_date:
        return False

    # Check if time is between 4pm (16:00) and 10pm (22:00)
    return time(16, 0) <= exam_completed_date.time() < time(22, 0)


def tag_evening_er(exam_completed_date: Optional[datetime],
                  patient_type: str,
                  cpt_division: Optional[str]) -> tuple[bool, Optional[str]]:
    """
    Tag exams that occurred during evening ER swing shifts.

    Rules:
    - Time: 4pm-10pm (based on exam_completed_date)
    - Patient type: ER or INPATIENT only (exclude OUTPATIENT)
    - Type: ER-NEURO if CPT division is NEURO, else ER-GENERAL

    Args:
        exam_completed_date: DateTime when exam was completed
        patient_type: Standardized patient type (ER/INPATIENT/OUTPATIENT)
        cpt_division: Division based on CPT code

    Returns:
        Tuple of (is_evening_er: bool, evening_er_type: Optional[str])
    """
    # Check time window
    if not is_evening_er_time(exam_completed_date):
        return (False, None)

    # Check patient type (only ER and INPATIENT)
    if patient_type not in ("ER", "INPATIENT"):
        return (False, None)

    # Determine type based on CPT division
    if cpt_division == "NEURO":
        return (True, "ER-NEURO")
    else:
        return (True, "ER-GENERAL")


def check_flex_shift(row: Dict[str, Any],
                    flex_shifts: List[Dict[str, Any]],
                    schedule: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """
    Check if exam matches flex shift criteria (Neuro division only).

    Flex shift rules are complex and may include:
    - Case count thresholds (e.g., "22 inpatient cases before 8AM")
    - Time windows (e.g., "before noon", "before 8AM")
    - Day type (weekday vs weekend)
    - Exclusions (e.g., "not the on-call attending that day")
    - Case type (inpatient vs outpatient)

    Args:
        row: Dictionary of exam data
        flex_shifts: List of flex shift rule definitions from config
        schedule: Schedule info (e.g., on-call assignments)

    Returns:
        Tuple of (is_flex: bool, flex_shift_name: Optional[str])
    """
    # TODO: Implement flex shift logic
    # This is complex and will be implemented in next iteration
    # For now, return False for all exams
    return (False, None)


def canonicalize_attending_name(name: str) -> str:
    """
    Standardize attending name format.

    Args:
        name: Raw attending name

    Returns:
        Canonicalized name (trimmed, standardized format)
    """
    if not name:
        return ""

    # Trim whitespace
    name = name.strip()

    # Standardize to "Last, First M." format if possible
    # This is a simple version - may need enhancement based on actual data
    return name


def preprocess_exam_row(row: Dict[str, Any],
                       attending_divisions: Dict[str, str],
                       cpt_divisions: Dict[str, str],
                       flex_shifts: List[Dict[str, Any]],
                       schedule: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply all preprocessing logic to a single exam row.

    Args:
        row: Raw exam data from CSV
        attending_divisions: Attending name to division mappings
        cpt_divisions: CPT code to division mappings
        flex_shifts: Flex shift rule definitions
        schedule: Schedule information

    Returns:
        Dict with computed fields added
    """
    # Parse datetime fields
    exam_completed_date = None
    if row.get('Exam Completed Date'):
        try:
            exam_completed_date = datetime.fromisoformat(row['Exam Completed Date'])
        except (ValueError, TypeError):
            pass

    # Map patient type
    patient_type = map_patient_type(row.get('Patient Status', ''))

    # Assign divisions
    division = assign_attending_division(
        row.get('Report Finalized By', ''),
        attending_divisions
    )
    cpt_div = assign_cpt_division(
        row.get('CPT Code', ''),
        cpt_divisions
    )

    # Tag evening ER
    is_evening_er, evening_er_type = tag_evening_er(
        exam_completed_date,
        patient_type,
        cpt_div
    )

    # Check flex shifts (TODO)
    is_flex, flex_shift_name = check_flex_shift(
        row,
        flex_shifts,
        schedule
    )

    # Build computed fields dict
    computed = {
        'division': division,
        'cpt_division': cpt_div,
        'patient_type': patient_type,
        'is_evening_er': is_evening_er,
        'evening_er_type': evening_er_type,
        'is_flex': is_flex,
        'flex_shift_name': flex_shift_name,
        'patient_status_raw': row.get('Patient Status', ''),
        'report_finalized_by': canonicalize_attending_name(row.get('Report Finalized By', '')),
    }

    return computed
