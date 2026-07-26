"""Staffing metrics calculations for neuro division.

Calculates expected RVUs based on FTE and annual expectations.
"""

from datetime import datetime, date, timedelta
from typing import List, Dict, Any
from decimal import Decimal
from calendar import monthrange


def get_neuro_staffing_metrics(
    con,
    start_date: str,
    end_date: str,
    neuro_config: Dict[str, Any],
    exam_categories: str = None,
    patient_type: str = None,
    date_field: str = None
) -> List[Dict[str, Any]]:
    """Get staffing metrics for neuro division attendings."""
    from .queries import _category_conditions, _resolve_date_field
    df = _resolve_date_field(date_field)

    annual_rvu_expectation = neuro_config.get('annual_rvu_expectation', 5000)
    annual_rvu_expectation_70th = neuro_config.get('annual_rvu_expectation_70th')
    neuro_attendings = neuro_config.get('attendings', {})

    # Get attending codes for neuro division
    attending_codes = list(neuro_attendings.keys())

    if not attending_codes:
        return []

    # Build WHERE clause
    conditions = _category_conditions(exam_categories)
    if patient_type:
        pt_list = [pt.strip().upper() for pt in patient_type.split(',')]
        pt_conditions = ", ".join([f"'{pt}'" for pt in pt_list])
        conditions.append(f"patient_type IN ({pt_conditions})")

    # Filter to neuro attending codes
    att_conditions = ", ".join([f"'{att}'" for att in attending_codes])
    conditions.append(f"report_finalized_by IN ({att_conditions})")

    where_clause = "AND " + " AND ".join(conditions)

    # Query to get actual RVUs for each neuro attending
    query = f"""
        SELECT
            report_finalized_by,
            COUNT(*) as exam_count,
            SUM(work_professional_rvu) as total_rvu,
            SUM(CASE WHEN is_evening_er = TRUE THEN work_professional_rvu ELSE 0 END) as evening_er_rvu,
            SUM(CASE WHEN is_flex = TRUE THEN work_professional_rvu ELSE 0 END) as flex_rvu
        FROM exams
        WHERE {df} >= '{start_date}'
          AND {df} < '{end_date}'::DATE + INTERVAL '1 day'
          {where_clause}
        GROUP BY report_finalized_by
    """

    result = con.execute(query)
    rows = result.fetchall()

    # Create a map of attending code to actual RVUs
    actual_rvus = {row[0]: row[2] if row[2] else 0 for row in rows}

    # Calculate period expectation based on days in period
    start_dt = datetime.fromisoformat(start_date)
    end_dt = datetime.fromisoformat(end_date)
    days_in_period = (end_dt - start_dt).days
    days_in_year = 365.25  # Account for leap years
    period_fraction = days_in_period / days_in_year

    from .config import get_fte_for_date, get_period_target
    sd = start_dt.date() if hasattr(start_dt, 'date') else start_dt
    ed = end_dt.date() if hasattr(end_dt, 'date') else end_dt

    # Build metrics for each neuro attending
    metrics = []
    for att_code, att_info in neuro_attendings.items():
        # Reported FTE = current (end-of-period) value.
        fte = get_fte_for_date(att_info, ed)
        name = att_info.get('name', att_code)
        actual_rvu = actual_rvus.get(att_code, 0)

        # FTE-prorated benchmarks computed per-day so mid-month start_date / fte_history changes
        # prorate correctly.
        expected_rvu = get_period_target(att_info, annual_rvu_expectation, sd, ed)
        expected_rvu_70th = (get_period_target(att_info, annual_rvu_expectation_70th, sd, ed)
                             if annual_rvu_expectation_70th else None)

        variance = actual_rvu - expected_rvu
        variance_pct = (actual_rvu / expected_rvu - 1) * 100 if expected_rvu > 0 else 0
        variance_70th = (actual_rvu - expected_rvu_70th) if expected_rvu_70th is not None else None
        variance_pct_70th = ((actual_rvu / expected_rvu_70th - 1) * 100) if expected_rvu_70th else None

        # Get exam count and breakdown from query results
        row_data = next((r for r in rows if r[0] == att_code), None)
        exam_count = row_data[1] if row_data else 0
        evening_er_rvu = row_data[3] if row_data and row_data[3] else 0
        flex_rvu = row_data[4] if row_data and row_data[4] else 0

        entry = {
            'attending_code': att_code,
            'attending_name': name,
            'fte': fte,
            'exam_count': exam_count,
            'actual_rvu': round(actual_rvu, 2),
            'expected_rvu': round(expected_rvu, 2),
            'variance': round(variance, 2),
            'variance_pct': round(variance_pct, 2),
            'evening_er_rvu': round(evening_er_rvu, 2),
            'flex_rvu': round(flex_rvu, 2),
        }
        # Skip the 70th block when the benchmark is zero (e.g., attending's start_date is
        # after the window ends → no expected RVUs → variance % would divide by zero and
        # round(None) would crash).
        if expected_rvu_70th:
            entry['expected_rvu_70th'] = round(expected_rvu_70th, 2)
            entry['variance_70th'] = round(variance_70th, 2)
            entry['variance_pct_70th'] = round(variance_pct_70th, 2)

        metrics.append(entry)

    # Sort by name
    metrics.sort(key=lambda x: x['attending_name'])

    return metrics


def get_neuro_division_summary(
    con,
    start_date: str,
    end_date: str,
    neuro_config: Dict[str, Any],
    exam_categories: str = None,
    patient_type: str = None,
    date_field: str = None
) -> Dict[str, Any]:
    """Get summary staffing metrics for neuro division."""
    metrics = get_neuro_staffing_metrics(
        con, start_date, end_date, neuro_config,
        exam_categories, patient_type, date_field=date_field
    )

    if not metrics:
        return {
            'total_fte': 0,
            'total_expected_rvu': 0,
            'total_actual_rvu': 0,
            'total_variance': 0,
            'total_variance_pct': 0,
            'attending_count': 0,
        }

    total_fte = sum(m['fte'] for m in metrics)
    total_expected = sum(m['expected_rvu'] for m in metrics)
    total_actual = sum(m['actual_rvu'] for m in metrics)
    total_variance = total_actual - total_expected
    total_variance_pct = (total_actual / total_expected - 1) * 100 if total_expected > 0 else 0

    return {
        'total_fte': round(total_fte, 2),
        'total_expected_rvu': round(total_expected, 2),
        'total_actual_rvu': round(total_actual, 2),
        'total_variance': round(total_variance, 2),
        'total_variance_pct': round(total_variance_pct, 2),
        'attending_count': len(metrics),
    }


def get_moonlight_compensation_model(
    con,
    start_date: str,
    end_date: str,
    neuro_config: Dict[str, Any],
    schedule: Dict[str, Any],
    date_field: str = None,
) -> List[Dict[str, Any]]:
    """Get monthly moonlight compensation comparison data.

    Compares fixed daily moonlight pay vs a $/RVU model based on
    excess RVUs (total produced minus monthly expectation).
    """
    from .queries import get_monthly_neuro_rvus

    moonlight_pay = neuro_config.get('moonlight_pay', {})
    day_rates = {
        'monday': moonlight_pay.get('monday', 0),
        'tuesday': moonlight_pay.get('tuesday', 0),
        'wednesday': moonlight_pay.get('wednesday', 0),
        'thursday': moonlight_pay.get('thursday', 0),
        'friday': moonlight_pay.get('friday', 0),
        'saturday': moonlight_pay.get('saturday', 1500),
        'sunday': moonlight_pay.get('sunday', 1500),
    }
    holiday_rate = moonlight_pay.get('holiday', 2000)
    day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

    # Get monthly total RVU data for neuro division
    monthly_rvus = get_monthly_neuro_rvus(con, start_date, end_date, date_field=date_field)
    rvu_by_month = {r['month']: r for r in monthly_rvus}

    # Generate all months in range
    start_dt = datetime.fromisoformat(start_date).date()
    end_dt = datetime.fromisoformat(end_date).date()

    results = []
    current = start_dt.replace(day=1)
    while current < end_dt:
        month_key = current.strftime('%Y-%m')
        _, days_in_month = monthrange(current.year, current.month)

        # Count weekend days and holidays for this month
        day_breakdown = {}
        for dn in day_names:
            day_breakdown[dn] = 0
        day_breakdown['holiday'] = 0

        fixed_cost = 0
        weekend_days = 0
        holiday_days = 0

        for d in range(1, days_in_month + 1):
            dt = date(current.year, current.month, d)
            if dt < start_dt or dt >= end_dt:
                continue

            date_str = dt.isoformat()
            day_of_week = day_names[dt.weekday()]

            # Check if this date is a holiday from the schedule
            is_holiday = False
            if date_str in schedule:
                is_holiday = schedule[date_str].get('_is_holiday', False)

            if is_holiday:
                holiday_days += 1
                day_breakdown['holiday'] += 1
                fixed_cost += holiday_rate
            else:
                rate = day_rates[day_of_week]
                if rate > 0:
                    day_breakdown[day_of_week] += 1
                    fixed_cost += rate
                if dt.weekday() >= 5:
                    weekend_days += 1

        rvu_data = rvu_by_month.get(month_key, {})
        total_rvus = rvu_data.get('total_rvu', 0)

        # Count actual days in this month within the date range
        active_days = sum(1 for d in range(1, days_in_month + 1)
                         if start_dt <= date(current.year, current.month, d) < end_dt)

        results.append({
            'month': month_key,
            'total_rvus': round(total_rvus, 2),
            'total_exams': rvu_data.get('exam_count', 0),
            'days_in_month': active_days,
            'fixed_cost': fixed_cost,
            'weekend_days': weekend_days,
            'holiday_days': holiday_days,
            'day_breakdown': {k: v for k, v in day_breakdown.items() if v > 0},
        })

        # Advance to next month
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)

    return results
