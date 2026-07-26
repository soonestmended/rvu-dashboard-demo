"""Database query functions for RVU Dashboard API."""

import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


ALL_EXAM_CATEGORIES = ['WEEKDAY', 'CALL', 'WEEKDAY_EVENING_ER', 'WEEKEND_EVENING_ER', 'AFTER_HOURS']

VALID_DATE_FIELDS = {'exam_completed_date', 'report_finalized_date'}


def _resolve_date_field(date_field: Optional[str] = None) -> str:
    """Validate and return the date field to filter on."""
    if date_field is None:
        return 'exam_completed_date'
    if date_field not in VALID_DATE_FIELDS:
        raise ValueError(f"Invalid date_field: {date_field}. Must be one of {VALID_DATE_FIELDS}")
    return date_field


def _category_conditions(exam_categories: Optional[str] = None) -> list:
    """Build SQL conditions for exam category filtering.

    Categories: WEEKDAY, CALL, WEEKDAY_EVENING_ER, WEEKEND_EVENING_ER, MOONLIGHT.
    When all are selected (or param is absent), no filtering — all exams shown.
    When a subset is selected, filter to ONLY matching exams.

    Returns a list of SQL condition strings (may be empty).
    """
    if not exam_categories:
        return []

    selected = set(c.strip() for c in exam_categories.split(','))

    # All selected = no filter
    if selected >= set(ALL_EXAM_CATEGORIES):
        return []

    # Build OR conditions for selected categories
    parts = []
    for cat in selected:
        if cat == 'WEEKDAY':
            # Daytime weekday reads: excludes Weekend Call, evening ER, and anything the
            # materialized flag calls after-hours. That last exclusion is what makes
            # WEEKDAY and AFTER_HOURS mutually exclusive (previously they overlapped:
            # an IA read at 6pm was WEEKDAY-tagged AND after-hours-tagged).
            parts.append(
                "(is_evening_er != TRUE "
                "AND COALESCE(shift_name, '') NOT IN ('Moonlight', 'Weekend Call') "
                "AND (is_after_hours IS NULL OR is_after_hours = FALSE))"
            )
        elif cat == 'CALL':
            parts.append(
                "(shift_name = 'Weekend Call')"
            )
        elif cat == 'WEEKDAY_EVENING_ER':
            parts.append(
                "(is_evening_er = TRUE AND EXTRACT(DOW FROM exam_completed_date) BETWEEN 1 AND 5)"
            )
        elif cat == 'WEEKEND_EVENING_ER':
            parts.append(
                "(is_evening_er = TRUE AND EXTRACT(DOW FROM exam_completed_date) IN (0, 6))"
            )
        elif cat == 'AFTER_HOURS':
            # NEW canonical: time-based after-hours, matches the comp API's bonus bucket.
            # Was previously tag-based (`shift_name = 'Moonlight'`) — narrower.
            parts.append("(is_after_hours = TRUE)")

    if not parts:
        # Categories param was provided but no valid categories — match nothing
        return ["1=0"]

    return ["(" + " OR ".join(parts) + ")"]


def get_rvus_by_division(
    con,
    start_date: str,
    end_date: str,
    exam_categories: Optional[str] = None,
    attending: Optional[str] = None,
    patient_type: Optional[str] = None,
    date_field: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Get RVU totals grouped by division."""
    df = _resolve_date_field(date_field)
    conditions = _category_conditions(exam_categories)
    if attending:
        # Support comma-separated list of attendings
        attending_list = [a.strip() for a in attending.split(',')]
        attending_conditions = ", ".join([f"'{a}'" for a in attending_list])
        conditions.append(f"report_finalized_by IN ({attending_conditions})")
    if patient_type:
        # Support comma-separated list of patient types
        pt_list = [pt.strip().upper() for pt in patient_type.split(',')]
        pt_conditions = ", ".join([f"'{pt}'" for pt in pt_list])
        conditions.append(f"patient_type IN ({pt_conditions})")

    where_clause = ""
    if conditions:
        where_clause = "AND " + " AND ".join(conditions)

    query = f"""
        SELECT
            division,
            COUNT(*) as exam_count,
            SUM(work_professional_rvu) as total_rvu,
            SUM(CASE WHEN is_evening_er = TRUE THEN work_professional_rvu ELSE 0 END) as evening_er_rvu,
            SUM(CASE WHEN is_flex = TRUE THEN work_professional_rvu ELSE 0 END) as flex_rvu
        FROM exams
        WHERE {df} >= '{start_date}'
          AND {df} < '{end_date}'::DATE + INTERVAL '1 day'
          {where_clause}
        GROUP BY division
        ORDER BY total_rvu DESC
    """

    result = con.execute(query)
    rows = result.fetchall()

    return [
        {
            'division': row[0],
            'exam_count': row[1],
            'total_rvu': row[2] if row[2] else 0,
            'evening_er_rvu': row[3] if row[3] else 0,
            'flex_rvu': row[4] if row[4] else 0,
        }
        for row in rows
        if row[0]  # Filter out NULL divisions
    ]


def get_rvus_by_location(
    con,
    start_date: str,
    end_date: str,
    exam_categories: Optional[str] = None,
    patient_type: Optional[str] = None,
    date_field: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Get RVU totals grouped by location (point_of_care)."""
    df = _resolve_date_field(date_field)
    conditions = _category_conditions(exam_categories)
    if patient_type:
        pt_list = [pt.strip().upper() for pt in patient_type.split(',')]
        pt_conditions = ", ".join([f"'{pt}'" for pt in pt_list])
        conditions.append(f"patient_type IN ({pt_conditions})")

    where_clause = ""
    if conditions:
        where_clause = "AND " + " AND ".join(conditions)

    query = f"""
        SELECT
            point_of_care,
            COUNT(*) as exam_count,
            SUM(work_professional_rvu) as total_rvu,
            SUM(CASE WHEN is_evening_er = TRUE THEN work_professional_rvu ELSE 0 END) as evening_er_rvu,
            SUM(CASE WHEN is_flex = TRUE THEN work_professional_rvu ELSE 0 END) as flex_rvu
        FROM exams
        WHERE {df} >= '{start_date}'
          AND {df} < '{end_date}'::DATE + INTERVAL '1 day'
          AND point_of_care IS NOT NULL
          {where_clause}
        GROUP BY point_of_care
        ORDER BY total_rvu DESC
    """

    result = con.execute(query)
    rows = result.fetchall()

    return [
        {
            'location': row[0],
            'exam_count': row[1],
            'total_rvu': row[2] if row[2] else 0,
            'evening_er_rvu': row[3] if row[3] else 0,
            'flex_rvu': row[4] if row[4] else 0,
        }
        for row in rows
    ]


def get_rvus_by_attending(
    con,
    start_date: str,
    end_date: str,
    division: Optional[str] = None,
    exam_categories: Optional[str] = None,
    patient_type: Optional[str] = None,
    date_field: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Get RVU totals grouped by attending."""
    df = _resolve_date_field(date_field)
    conditions = _category_conditions(exam_categories)
    if division:
        conditions.append(f"division = '{division}'")
    if patient_type:
        pt_list = [pt.strip().upper() for pt in patient_type.split(',')]
        pt_conditions = ", ".join([f"'{pt}'" for pt in pt_list])
        conditions.append(f"patient_type IN ({pt_conditions})")

    where_clause = ""
    if conditions:
        where_clause = "AND " + " AND ".join(conditions)

    query = f"""
        SELECT
            report_finalized_by,
            division,
            COUNT(*) as exam_count,
            SUM(work_professional_rvu) as total_rvu,
            SUM(CASE WHEN is_evening_er = TRUE THEN work_professional_rvu ELSE 0 END) as evening_er_rvu,
            SUM(CASE WHEN is_flex = TRUE THEN work_professional_rvu ELSE 0 END) as flex_rvu
        FROM exams
        WHERE {df} >= '{start_date}'
          AND {df} < '{end_date}'::DATE + INTERVAL '1 day'
          AND report_finalized_by IS NOT NULL
          {where_clause}
        GROUP BY report_finalized_by, division
        ORDER BY total_rvu DESC
    """

    result = con.execute(query)
    rows = result.fetchall()

    return [
        {
            'attending_id': row[0],
            'division': row[1],
            'exam_count': row[2],
            'total_rvu': row[3] if row[3] else 0,
            'evening_er_rvu': row[4] if row[4] else 0,
            'flex_rvu': row[5] if row[5] else 0,
        }
        for row in rows
    ]


def get_divisions(con) -> List[str]:
    """Get list of all unique divisions."""
    result = con.execute("""
        SELECT DISTINCT division
        FROM exams
        WHERE division IS NOT NULL
        ORDER BY division
    """)
    rows = result.fetchall()
    return [row[0] for row in rows]


def get_locations(con) -> List[str]:
    """Get list of all unique locations (point_of_care)."""
    result = con.execute("""
        SELECT DISTINCT point_of_care
        FROM exams
        WHERE point_of_care IS NOT NULL
        ORDER BY point_of_care
    """)
    rows = result.fetchall()
    return [row[0] for row in rows]


def get_attendings(con) -> List[Dict[str, str]]:
    """Get list of all attendings with their divisions."""
    result = con.execute("""
        SELECT DISTINCT report_finalized_by, division
        FROM exams
        WHERE report_finalized_by IS NOT NULL
        ORDER BY report_finalized_by
    """)
    rows = result.fetchall()
    return [
        {
            'attending_id': row[0],
            'division': row[1],
        }
        for row in rows
    ]


def get_date_range(
    con,
    date_field: Optional[str] = None,
    division: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Get the min and max dates from the database, optionally scoped to a division."""
    df = _resolve_date_field(date_field)
    where = [f"{df} IS NOT NULL"]
    if division:
        # Division is a controlled enum from the ingestion pipeline, but parameterize anyway
        # to avoid quoting bugs and to follow the same pattern as the rest of this module.
        where.append(f"division = '{division.replace(chr(39), chr(39)+chr(39))}'")
    result = con.execute(f"""
        SELECT
            MIN({df})::DATE as min_date,
            MAX({df})::DATE as max_date
        FROM exams
        WHERE {' AND '.join(where)}
    """)
    row = result.fetchone()
    return {
        'min_date': str(row[0]) if row[0] else None,
        'max_date': str(row[1]) if row[1] else None,
    }


def get_monthly_neuro_rvus(
    con,
    start_date: str,
    end_date: str,
    date_field: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Get monthly total RVU totals and exam counts for neuro division."""
    df = _resolve_date_field(date_field)
    query = f"""
        SELECT
            strftime({df}, '%Y-%m') as month,
            COUNT(*) as exam_count,
            SUM(work_professional_rvu) as total_rvu
        FROM exams
        WHERE {df} >= '{start_date}'
          AND {df} < '{end_date}'::DATE + INTERVAL '1 day'
          AND division = 'NEURO'
        GROUP BY strftime({df}, '%Y-%m')
        ORDER BY month
    """

    result = con.execute(query)
    rows = result.fetchall()

    return [
        {
            'month': row[0],
            'exam_count': row[1],
            'total_rvu': row[2] if row[2] else 0,
        }
        for row in rows
    ]


# "After-hours" classification per the new comp plan: any exam read outside Mon–Fri 8am–5pm
# Pacific time, EXCLUDING those tagged 'Weekend Call' and those tagged as evening ER.
#
# Materialized on the row as `is_after_hours` (computed at ingest, refreshed by
# reassign_shifts_only, rebuildable via backend.backfill_after_hours). Query-side we just
# read the column — one canonical field, one source of truth, no predicate drift.
#
# The helper below is retained ONLY so backend.backfill_after_hours can share the exact
# holiday-list SQL when it rebuilds the column. Nothing else in queries.py should be
# constructing the predicate directly.
def _holidays_sql_list() -> str:
    """Return the scheduler module's HOLIDAYS set formatted as a SQL VALUES-style literal
    for the after-hours BACKFILL predicate. Falls back to an empty list if the scheduler
    module isn't importable — the backfill then can't tag holidays specially, so weekends
    still work but Dec 25 / July 4 daytime reads stay tagged as daytime. Logs LOUDLY on
    failure so ops sees it (silent under-counting would be worse)."""
    try:
        import schedule as _scheduler  # scheduler package mounted at /app/scheduler
        return ",".join(f"DATE '{d.isoformat()}'" for d in sorted(_scheduler.HOLIDAYS))
    except Exception as ex:
        logger.error(
            "Failed to load HOLIDAYS from scheduler module — is_after_hours backfill "
            "will not tag holidays specially. Fix by ensuring /app/scheduler is mounted "
            "with the scheduler package, then re-run backfill_after_hours. (%s: %s)",
            type(ex).__name__, ex,
        )
        return ""


# Every downstream query uses this constant instead of open-coding the predicate.
# Keeping the name `_AFTER_HOURS_PREDICATE` for grep-friendliness during the transition.
_AFTER_HOURS_PREDICATE = "is_after_hours = TRUE"


def get_daily_rvus_for_attending(
    con,
    attending_id: str,
    start_date: str,
    end_date: str,
    division: str = 'NEURO',
    date_field: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Per-day wRVU totals for a single attending, split into daytime + after-hours.

    Returns rows: {date, total_rvu, daytime_rvu, after_hours_rvu, exam_count}.
    """
    df = _resolve_date_field(date_field)
    query = f"""
        SELECT
            CAST({df} AS DATE) AS day,
            COUNT(*) AS exam_count,
            SUM(work_professional_rvu) AS total_rvu,
            SUM(CASE WHEN NOT {_AFTER_HOURS_PREDICATE} THEN work_professional_rvu ELSE 0 END) AS daytime_rvu,
            SUM(CASE WHEN     {_AFTER_HOURS_PREDICATE} THEN work_professional_rvu ELSE 0 END) AS after_hours_rvu
        FROM exams
        WHERE {df} >= '{start_date}'
          AND {df} < '{end_date}'::DATE + INTERVAL '1 day'
          AND division = '{division}'
          AND report_finalized_by = '{attending_id.replace(chr(39), chr(39) + chr(39))}'
        GROUP BY CAST({df} AS DATE)
        ORDER BY day
    """
    rows = con.execute(query).fetchall()
    return [
        {
            'date': r[0].isoformat() if r[0] else None,
            'exam_count': r[1],
            'total_rvu': float(r[2]) if r[2] else 0.0,
            'daytime_rvu': float(r[3]) if r[3] else 0.0,
            'after_hours_rvu': float(r[4]) if r[4] else 0.0,
        }
        for r in rows
    ]


def get_monthly_rvus_for_attending(
    con,
    attending_id: str,
    start_date: str,
    end_date: str,
    division: str = 'NEURO',
    date_field: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Per-month wRVU totals for a single attending, split into daytime vs after-hours.

    Returns a list of {month, exam_count, total_rvu, daytime_rvu, after_hours_rvu} ordered
    by month. Months with zero production are NOT present — caller can fill in zeros for
    any month in [start_date, end_date] that's absent.
    """
    df = _resolve_date_field(date_field)
    query = f"""
        SELECT
            strftime({df}, '%Y-%m') AS month,
            COUNT(*) AS exam_count,
            SUM(work_professional_rvu) AS total_rvu,
            SUM(CASE WHEN NOT {_AFTER_HOURS_PREDICATE} THEN work_professional_rvu ELSE 0 END) AS daytime_rvu,
            SUM(CASE WHEN     {_AFTER_HOURS_PREDICATE} THEN work_professional_rvu ELSE 0 END) AS after_hours_rvu
        FROM exams
        WHERE {df} >= '{start_date}'
          AND {df} < '{end_date}'::DATE + INTERVAL '1 day'
          AND division = '{division}'
          AND report_finalized_by = '{attending_id.replace(chr(39), chr(39) + chr(39))}'
        GROUP BY strftime({df}, '%Y-%m')
        ORDER BY month
    """
    rows = con.execute(query).fetchall()
    return [
        {
            'month': r[0],
            'exam_count': r[1],
            'total_rvu': float(r[2]) if r[2] else 0.0,
            'daytime_rvu': float(r[3]) if r[3] else 0.0,
            'after_hours_rvu': float(r[4]) if r[4] else 0.0,
        }
        for r in rows
    ]


def get_rvus_by_shift(
    con,
    start_date: str,
    end_date: str,
    division: str = 'NEURO',
    exam_categories: Optional[str] = None,
    patient_type: Optional[str] = None,
    date_field: Optional[str] = None,
    attending: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Get RVU totals grouped by shift."""
    df = _resolve_date_field(date_field)
    conditions = [f"division = '{division}'"]
    if attending:
        attending_list = [a.strip() for a in attending.split(',')]
        attending_conditions = ", ".join([f"'{a}'" for a in attending_list])
        conditions.append(f"report_finalized_by IN ({attending_conditions})")
    conditions.extend(_category_conditions(exam_categories))
    if patient_type:
        pt_list = [pt.strip().upper() for pt in patient_type.split(',')]
        pt_conditions = ", ".join([f"'{pt}'" for pt in pt_list])
        conditions.append(f"patient_type IN ({pt_conditions})")

    where_clause = "AND " + " AND ".join(conditions)

    query = f"""
        SELECT
            COALESCE(shift_name, 'Unassigned') as shift_name,
            COUNT(*) as exam_count,
            SUM(work_professional_rvu) as total_rvu,
            SUM(CASE WHEN is_evening_er = TRUE THEN work_professional_rvu ELSE 0 END) as evening_er_rvu,
            SUM(CASE WHEN is_flex = TRUE THEN work_professional_rvu ELSE 0 END) as flex_rvu
        FROM exams
        WHERE {df} >= '{start_date}'
          AND {df} < '{end_date}'::DATE + INTERVAL '1 day'
          {where_clause}
        GROUP BY shift_name
        ORDER BY total_rvu DESC
    """

    result = con.execute(query)
    rows = result.fetchall()

    return [
        {
            'shift_name': row[0],
            'exam_count': row[1],
            'total_rvu': row[2] if row[2] else 0,
            'evening_er_rvu': row[3] if row[3] else 0,
            'flex_rvu': row[4] if row[4] else 0,
        }
        for row in rows
    ]


def get_rvus_by_attending_and_shift(
    con,
    start_date: str,
    end_date: str,
    division: str = 'NEURO',
    exam_categories: Optional[str] = None,
    patient_type: Optional[str] = None,
    date_field: Optional[str] = None,
    attending: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Get RVU totals grouped by attending and shift."""
    df = _resolve_date_field(date_field)
    conditions = [f"division = '{division}'"]
    if attending:
        attending_list = [a.strip() for a in attending.split(',')]
        attending_conditions = ", ".join([f"'{a}'" for a in attending_list])
        conditions.append(f"report_finalized_by IN ({attending_conditions})")
    conditions.extend(_category_conditions(exam_categories))
    if patient_type:
        pt_list = [pt.strip().upper() for pt in patient_type.split(',')]
        pt_conditions = ", ".join([f"'{pt}'" for pt in pt_list])
        conditions.append(f"patient_type IN ({pt_conditions})")

    where_clause = "AND " + " AND ".join(conditions)

    query = f"""
        SELECT
            report_finalized_by as attending_id,
            COALESCE(shift_name, 'Unassigned') as shift_name,
            COUNT(*) as exam_count,
            SUM(work_professional_rvu) as total_rvu
        FROM exams
        WHERE {df} >= '{start_date}'
          AND {df} < '{end_date}'::DATE + INTERVAL '1 day'
          {where_clause}
        GROUP BY report_finalized_by, shift_name
        ORDER BY report_finalized_by, shift_name
    """

    result = con.execute(query)
    rows = result.fetchall()

    return [
        {
            'attending_id': row[0],
            'shift_name': row[1],
            'exam_count': row[2],
            'total_rvu': row[3] if row[3] else 0,
        }
        for row in rows
    ]


def get_shift_rvu_averages(
    con,
    start_date: str,
    end_date: str,
    date_field: Optional[str] = None,
    division: str = 'NEURO',
) -> List[Dict[str, Any]]:
    """Per-shift average wRVU production.

    For each shift_name, returns the total wRVU summed across the period and the
    number of distinct (date, attending) "shift instances" that contributed.
    avg_rvu = total_rvu / instances. Used by the schedule-planning tool to
    project per-attending RVUs from a candidate schedule.
    """
    df = _resolve_date_field(date_field)
    # Timestamps are stored as naive values already in Pacific time, so CAST(... AS DATE)
    # gives the local-PST day directly — no AT TIME ZONE chain needed.
    #
    # shift_date MUST come from report_finalized_date regardless of the caller's date_field
    # filter. Shift assignment in ingest.py keys on report_finalized_date (that's the
    # attending's shift day). Using exam_completed_date here would count a single shift
    # instance as N when the attending signed exams from N different completion-days on it,
    # inflating the denominator and deflating avg_rvu — worst on Mondays after weekends and
    # post-vacation catch-up days.
    query = f"""
        WITH shifted AS (
            SELECT
                shift_name,
                CAST(report_finalized_date AS DATE) AS shift_date,
                report_finalized_by,
                work_professional_rvu
            FROM exams
            WHERE {df} >= '{start_date}'
              AND {df} < '{end_date}'::DATE + INTERVAL '1 day'
              AND division = '{division}'
              AND shift_name IS NOT NULL
        )
        SELECT
            shift_name,
            COUNT(DISTINCT (shift_date, report_finalized_by)) AS instances,
            SUM(work_professional_rvu) AS total_rvu,
            COUNT(*) AS total_exams
        FROM shifted
        GROUP BY shift_name
        ORDER BY total_rvu DESC
    """
    rows = con.execute(query).fetchall()
    return [
        {
            'shift_name': row[0],
            'instances': row[1],
            'total_rvu': float(row[2]) if row[2] else 0.0,
            'total_exams': row[3] if row[3] else 0,
            'avg_rvu': (float(row[2]) / row[1]) if (row[2] and row[1]) else 0.0,
            'avg_exams': (row[3] / row[1]) if (row[3] and row[1]) else 0.0,
        }
        for row in rows
    ]


def get_per_day_attending_production(
    con,
    start_date: str,
    end_date: str,
    division: str = 'NEURO',
) -> List[Dict[str, Any]]:
    """Per-(work-day, attending) actual exam count and wRVU for a date range.

    Indexed by `report_finalized_date` — the day the attending actually finalized the report,
    which matches "the schedule day they did the work" much better than exam_completed_date.
    A study performed late one day and signed the next morning shows up on the *signing* day.
    """
    query = f"""
        SELECT
            CAST(report_finalized_date AS DATE) AS work_date,
            report_finalized_by,
            COUNT(*) AS exams,
            SUM(work_professional_rvu) AS rvu
        FROM exams
        WHERE report_finalized_date >= '{start_date}'
          AND report_finalized_date < '{end_date}'::DATE + INTERVAL '1 day'
          AND division = '{division}'
          AND report_finalized_by IS NOT NULL
        GROUP BY work_date, report_finalized_by
    """
    rows = con.execute(query).fetchall()
    return [
        {
            'date': row[0].isoformat(),
            'attending_id': row[1],
            'exams': row[2],
            'rvu': float(row[3]) if row[3] else 0.0,
        }
        for row in rows
    ]
