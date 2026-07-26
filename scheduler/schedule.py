import sys
import math
from typing import Iterable, Dict, List, Union, Tuple
import gspread
import pandas as pd
from google.oauth2.service_account import Credentials
from datetime import datetime, date, timedelta
from dateutil.parser import parse as parse_dt
from ortools.sat.python import cp_model

import re
from collections import defaultdict

from ortools.sat.python.cp_model import IntVar  # add this

# Labeled objective term = (human-readable label, integer weight, IntVar)
ObjTerm = Tuple[str, int, IntVar]

def _norm_id(s: str) -> str:
    return str(s).strip().replace(" ", "_").replace("/", "_")

CALL_COLUMN = "Call"
CALL_AVAILABILITY_START = {
    _norm_id("Odle"): date(2026, 2, 1),
    # Pham starts as attending 2026-08-17. Placeholder: 3 months of onboarding before
    # weekend call — adjust to when he's actually ready to take call.
    _norm_id("Pham"): date(2026, 11, 17),
}

CALL_SPACING_WINDOW = 8          # number of call slots (weekend pair counts once) to keep unique per worker
CALL_SPACING_WEIGHT = 900        # penalty weight for violating the spacing window

ACADEMIC_YEAR_START = date(2025, 7, 1)
ACADEMIC_YEAR_END = date(2026, 6, 30)

FORBIDS_GLOBAL = {
    "Chang":      ["Flex/Nights", "InpatientA"], 
    "Sadigh":        ["Flex/Nights"],
}

workers = ['Chang',	'Chu', 'Floriolli',	'Fussell',	'Kuoy',	'Li', 'McLouth', 'Odle', 'Pham', 'Sadigh', 'Soun', 'Yep']
WEEKLY_TARGETS = {
    _norm_id("Chang"):     1,  # each week
    _norm_id("Chu"):       3,
    _norm_id("Floriolli"): 4,
    _norm_id("Fussell"):   3,
    _norm_id("Kuoy"):      4,
    _norm_id("Li"):        4,
    _norm_id("McLouth"):   4,
    _norm_id("Odle"):      4,
    _norm_id("Pham"):      4,  # 1.0 FTE new hire, starts 2026-08-17
    _norm_id("Sadigh"):    1,
    _norm_id("Soun"):      3,
    _norm_id("Yep"):       4,
}

W_OVER = {
    _norm_id("Chang"):     150,  # each week
    _norm_id("Chu"):       110,
    _norm_id("Floriolli"): 350,
    _norm_id("Fussell"):   120,
    _norm_id("Kuoy"):      350,  # 1.0 FTE from 2026-07-01
    _norm_id("Li"):        100,
    _norm_id("McLouth"):   350,
    _norm_id("Odle"):      350,
    _norm_id("Pham"):      350,  # new hire — mirror Odle's onboarding weight
    _norm_id("Sadigh"):    150,
    _norm_id("Soun"):      110,
    _norm_id("Yep"):       350,
}

W_UNDER = {
    _norm_id("Chang"):     350,  # each week
    _norm_id("Chu"):       90,
    _norm_id("Floriolli"): 120,
    _norm_id("Fussell"):   80,
    _norm_id("Kuoy"):      120,  # 1.0 FTE from 2026-07-01
    _norm_id("Li"):        100,
    _norm_id("McLouth"):   120,
    _norm_id("Odle"):      120,
    _norm_id("Pham"):      120,  # mirrors Odle
    _norm_id("Sadigh"):    350,
    _norm_id("Soun"):      85,
    _norm_id("Yep"):       120,
}

# Workers excluded from moonlight-related fairness terms (M1+M2 floor, IA distribution, Flex/Nights pay).
# Chang and Floriolli don't pursue moonlight comp under the new model — but they still need
# baseline shifts to keep the section running, so the M1+M2 RVU floor uses a fractional target
# (see M1M2_TARGET_FRACTION) rather than a hard exclusion.
MOONLIGHT_EXCLUDED = {_norm_id("Chang"), _norm_id("Floriolli")}

# Per-worker M1+M2 RVU-floor target as a fraction of (annual_65 × FTE × 2/12).
# Default = 1.0 (hit 65th); Chang and Floriolli at 0.80 since they don't chase moonlight bonus
# but still need enough work to keep the section running and the schedule plausible.
M1M2_TARGET_FRACTION = {
    _norm_id("Chang"):     0.80,
    _norm_id("Floriolli"): 0.80,
    _norm_id("Chu"):       1.00,
    _norm_id("Fussell"):   1.00,
    _norm_id("Kuoy"):      1.00,
    _norm_id("Li"):        1.00,
    _norm_id("McLouth"):   1.00,
    _norm_id("Odle"):      1.00,
    _norm_id("Pham"):      1.00,
    _norm_id("Sadigh"):    1.00,
    _norm_id("Soun"):      1.00,
    _norm_id("Yep"):       1.00,
}

# Sadigh additionally doesn't take Flex/Nights — used for the Flex/Nights fairness target.
FLEX_NIGHTS_EXCLUDED = MOONLIGHT_EXCLUDED | {_norm_id("Sadigh")}

# Fraction of TOTAL work shifts that should be Flex/Nights for each worker.
# Eligible attendings split weekday Flex/Nights roughly evenly; lower for low-FTE / non-IA workers.
FLEX_NIGHTS_TARGETS = {
    _norm_id("Chang"):     0,
    _norm_id("Sadigh"):    0,
    _norm_id("Chu"):       .12,
    _norm_id("Floriolli"): .12,   # now covers ~2 Flex/Nights per month
    _norm_id("Fussell"):   .12,
    _norm_id("Kuoy"):      .12,
    _norm_id("Li"):        .12,
    _norm_id("McLouth"):   .12,
    _norm_id("Odle"):      .12,
    _norm_id("Pham"):      .12,
    _norm_id("Soun"):      .12,
    _norm_id("Yep"):       .12,
}

# Fraction of TOTAL work shifts that should be Inpatient A for each worker.
# IA shifts carry the IA-Moonlight pre-shift reads, the main per-shift moonlight contribution.
# Distribute roughly evenly among non-excluded workers; Sadigh gets a smaller share.
INPATIENT_A_TARGETS = {
    _norm_id("Chang"):     0,
    _norm_id("Floriolli"): 0,
    _norm_id("Chu"):       .12,
    _norm_id("Fussell"):   .12,
    _norm_id("Kuoy"):      .12,
    _norm_id("Li"):        .12,
    _norm_id("McLouth"):   .12,
    _norm_id("Odle"):      .12,
    _norm_id("Pham"):      .12,
    _norm_id("Sadigh"):    .08,
    _norm_id("Soun"):      .12,
    _norm_id("Yep"):       .12,
}

# Legacy constant retained for any references that may still exist; equals FLEX_NIGHTS_TARGETS.
PAY_PERCENT_TARGETS = FLEX_NIGHTS_TARGETS

# Per-attending monthly minimum number of Flex/Nights shifts. Uniform floor of 2/mo across
# all eligible attendings — the previous per-person derivation (ceil(0.12 × WEEKLY_TARGETS
# × 4.33), giving 2 or 3 per person) summed to 24 nights against only 20-22 available
# weekday Flex/Nights slots per month, making it infeasible on any month. A flat 2/mo
# gives ~15% slack for vacation-heavy months and remains a meaningful minimum.
#
# Acts as a near-hard soft floor (high penalty) so the solver tolerates vacation-heavy
# months when physically impossible to satisfy. Months not fully inside the solve window
# are skipped — partial-month floors would be unreachable when only a few days are in scope.
FLEX_NIGHTS_MONTHLY_FLOOR = {
    _norm_id("Chu"):       2,
    _norm_id("Floriolli"): 2,
    _norm_id("Fussell"):   2,
    _norm_id("Kuoy"):      2,
    _norm_id("Li"):        2,
    _norm_id("McLouth"):   2,
    _norm_id("Odle"):      2,
    _norm_id("Pham"):      2,
    _norm_id("Soun"):      2,
    _norm_id("Yep"):       2,
    # Chang, Sadigh: not in dict → no floor (they don't take Flex/Nights).
}


shifts = ['InpatientA', 'InpatientB', 'OutpatientA', 'OutpatientB', 'Flex/Nights', 'Flex']

# Fixed per-shift pay under the new comp model. Only Flex/Nights pays a fixed daily rate;
# everything else's monetary value flows through wRVUs and the $/RVU bonus.
shift_pay = {'InpatientA': 0, 'InpatientB': 0, 'OutpatientA': 0, 'OutpatientB': 0, 'Flex/Nights': 1840, 'Flex': 0}

# Estimated wRVU per shift instance (section averages from past data, used by the monthly RVU
# floor penalty). Matches the per-shift averages displayed in the Schedule dashboard summary
# cards. The solver does NOT separately add IA pre-shift moonlight — it's not a decision variable
# the solver controls (the dashboard adds +32 IA pre-shift in its display projection layered on
# top of these averages).
AVG_RVU_BY_SHIFT = {
    'InpatientA':   39,
    'InpatientB':   48,
    'OutpatientA':  54,
    'OutpatientB':  52,
    'Flex/Nights':  58,
    'Flex':         38,
}

# 65th-percentile annual benchmark — used to compute each worker's M1+M2 floor target.
ANNUAL_65TH_RVU = 10179

# Average wRVU credited per weekend/holiday call day (per attending). Empirical: ~70 wRVU/day
# from FY 24-25 + FY 25-26 weekend-call production. Used as a known constant when projecting
# each worker's monthly RVUs in `add_monthly_rvu_floor_penalty`, since call lockins are fixed
# at solve time. Without this offset, attendings drawing heavy call months look like they're
# falling short of their monthly threshold and the solver over-corrects with weekday shifts.
AVG_RVU_PER_CALL_DAY = 70

statuses = ['Academic', 'Conference', 'Vacation', 'Blocked', 'Sick']

HOLIDAYS = {
    # 2025
    date(2025, 7, 4),   # Independence Day
    date(2025, 9, 1),   # Labor Day
    date(2025, 11, 11), # Veterans Day
    date(2025, 11, 27), date(2025, 11, 28),  # Thanksgiving
    date(2025, 12, 24), date(2025, 12, 25), # Christmas
    date(2025, 12, 31), # New Year's Day (observed)
    # 2026
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents Day
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    # FY 26-27
    date(2026, 7, 3),   # Independence Day observed (Jul 4 falls on a Saturday)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 11), # Veterans Day
    date(2026, 11, 26), date(2026, 11, 27),  # Thanksgiving + day after
    date(2026, 12, 24), date(2026, 12, 25),  # Christmas Eve + Christmas
    date(2026, 12, 31), # New Year's Eve
    date(2027, 1, 1),   # New Year's Day
    date(2027, 1, 18),  # MLK Day
    date(2027, 2, 15),  # Presidents Day
    date(2027, 5, 31),  # Memorial Day
    date(2027, 6, 18),  # Juneteenth observed (Jun 19 falls on a Saturday)
}

# ---- permissive normalization helpers ----
_word_re = re.compile(r"[A-Za-z]+")

def _soft_norm(s: str) -> str:
    """Lowercase, remove all non-letters: 'Flex/Nights' -> 'flexnights', 'VACAY!!'->'vacay'."""
    return "".join(_word_re.findall(str(s).lower()))

def _looks_like(text: str, *candidates: str) -> bool:
    """Return True if any candidate substring appears in the soft-normalized text."""
    t = _soft_norm(text)
    return any(_soft_norm(c) in t for c in candidates)

def dates_inclusive(start_date: date, end_date: date) -> List[date]:
    if start_date > end_date:
        raise ValueError(f"Start date {start_date} is after end date {end_date}")
    return [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]

def partition_into_weeks(
    D: Tuple[date, ...] | List[date],
    week_start: int = 0,
    include_weekends: bool = False,
) -> List[List[date]]:
    """
    Partition sorted dates into calendar weeks and filter to eligible days.

    Parameters
    ----------
    D : dates to partition
    week_start : weekday index for start of week (0=Monday, default)
    include_weekends : if False (default), only weekdays are returned per week

    Returns
    -------
    List of lists, one per week, containing only eligible dates.
    """
    def sow(d):
        return d - timedelta(days=(d.weekday() - week_start) % 7)

    weeks_map: dict[date, list[date]] = {}
    for d in sorted(D):
        weeks_map.setdefault(sow(d), []).append(d)

    def elig(ds):
        return ds if include_weekends else [d for d in ds if d.weekday() < 5]

    return [elig(weeks_map[w]) for w in sorted(weeks_map.keys())]


def compute_call_dates(start_date: date, end_date: date, holidays: set[date]) -> List[date]:
    return [
        d for d in dates_inclusive(start_date, end_date)
        if d.weekday() >= 5 or d in holidays
    ]


def build_call_slots(call_dates: list[date]) -> List[Dict]:
    unique = sorted({(d.date() if isinstance(d, datetime) else d) for d in call_dates})
    slots = []
    i = 0
    while i < len(unique):
        d = unique[i]
        slot_dates = [d]
        if d.weekday() == 5 and i + 1 < len(unique):
            nxt = unique[i + 1]
            if nxt == d + timedelta(days=1):
                slot_dates.append(nxt)
                i += 1
        slots.append({"dates": tuple(slot_dates), "repr": slot_dates[0]})
        i += 1
    return slots

def read_call_history_sequence(
    df: pd.DataFrame,
    *,
    date_col: str,
    call_col: str,
    worker_names: list[str],
    cutoff_date: date,
) -> List[Dict]:
    date_keys = df[date_col].apply(_coerce_date)
    tokens = {"", "NA", "N/A", "-", "—", "--"}
    candidate_dates = {
        d for idx, d in date_keys.items()
        if d is not None and d < cutoff_date and str(df.at[idx, call_col]).strip().upper() not in tokens
    }
    if not candidate_dates:
        return []
    history_dates = sorted(candidate_dates)
    lockins = read_call_lockins_from_df(
        df,
        date_col=date_col,
        call_col=call_col,
        worker_names=worker_names,
        call_dates=history_dates,
    )
    if not lockins:
        return []
    by_date = {item["date"]: _norm_id(item["person_id"]) for item in lockins}
    slots = build_call_slots(sorted(by_date.keys()))
    history = []
    for slot in slots:
        chosen = by_date.get(slot["dates"][0])
        if not chosen:
            for d in slot["dates"][1:]:
                chosen = by_date.get(d)
                if chosen:
                    break
        if not chosen:
            continue
        history.append({"dates": slot["dates"], "worker": chosen})
    return history

def read_historical_shift_counts(
    df: pd.DataFrame,
    *,
    date_col: str,
    worker_cols: list[str],
    shifts: list[str],
    work_shift_ids: list[str],
    cutoff_date: date,
    start_date: date = None,  # lower bound - only count shifts on or after this date
    include_weekends: bool = False,
    start_dates_by_worker: dict = None,  # worker_id -> date when they started
) -> Dict[str, int]:
    """
    Read historical shift assignments from the dataframe and count work shifts per worker.

    Returns a dict mapping normalized worker_id -> count of work shifts worked between start_date and cutoff_date.

    If start_dates_by_worker is provided, only counts shifts on or after each worker's start date.
    """
    date_keys = df[date_col].apply(_coerce_date)
    work_shifts_norm = {_norm_id(s) for s in work_shift_ids}
    start_dates_norm = {k: v for k, v in (start_dates_by_worker or {}).items()}

    rows_by_date = defaultdict(list)
    for idx, d in date_keys.items():
        if d is not None and d < cutoff_date:
            if start_date is not None and d < start_date:
                continue  # Skip dates before the lookback start date
            if include_weekends or d.weekday() < 5:
                rows_by_date[d].append(idx)

    counts = {_norm_id(w): 0 for w in worker_cols}
    alias_cache = _shift_aliases_map(shifts)

    for d, idxs in rows_by_date.items():
        for idx in idxs:
            for worker in worker_cols:
                worker_norm = _norm_id(worker)
                if worker not in df.columns:
                    continue
                cell = df.at[idx, worker]
                kind, val, _ = classify_cell_text(cell, shifts, [], _alias_cache=alias_cache)

                if kind == "shift":
                    shift_norm = _norm_id(val)
                    if shift_norm in work_shifts_norm:
                        # Check if this worker has a start date constraint
                        worker_start = start_dates_norm.get(worker_norm)
                        if worker_start is not None and d < worker_start:
                            # Don't count shifts before this worker's start date
                            continue
                        counts[worker_norm] = counts.get(worker_norm, 0) + 1

    return counts

def collect_solver_call_assignments(P, call_dates, C, solver):
    assignments: Dict[date, str] = {}
    for d in call_dates:
        for p in P:
            key = (p, d)
            if key in C and solver.Value(C[key]) == 1:
                assignments[d] = p
                break
    return assignments

def print_academic_year_call_totals(
    df: pd.DataFrame,
    *,
    date_col: str,
    call_col: str,
    workers: list[str],
    holidays: set[date],
    year_start: date,
    year_end: date,
):
    year_dates = compute_call_dates(year_start, year_end, holidays)
    existing = read_call_lockins_from_df(
        df,
        date_col=date_col,
        call_col=call_col,
        worker_names=workers,
        call_dates=year_dates,
    )
    combined: Dict[date, str] = {}
    for item in existing:
        d = item["date"].date() if isinstance(item["date"], datetime) else item["date"]
        combined[d] = _norm_id(item["person_id"])

    totals = defaultdict(int)
    label_map = {_norm_id(w): w for w in workers}
    for w in workers:
        totals[_norm_id(w)]  # ensure presence
    for d in year_dates:
        worker = combined.get(d)
        if worker:
            totals[worker] += 1

    print(f"\n=== Academic Year Call Day Totals ({year_start} → {year_end}) ===")
    for worker in sorted(totals.keys(), key=lambda x: label_map.get(x, x)):
        label = label_map.get(worker, worker)
        print(f"{label:<12}: {totals[worker]}")

def build_call_slot_var_matrix(C, call_slots, P):
    matrix = []
    for slot in call_slots:
        rep = slot["repr"]
        matrix.append({p: C[(p, rep)] for p in P if (p, rep) in C})
    return matrix

def add_call_spacing_window_penalties(
    model: cp_model.CpModel,
    slot_vars: List[Dict[str, cp_model.IntVar]],
    *,
    P: Iterable[str],
    window_size: int,
    history_tail: List[str] | None,
    weight: int,
) -> List[ObjTerm]:
    if window_size <= 1 or not slot_vars:
        return []
    history_tail = [h for h in (history_tail or []) if h]
    terms: List[ObjTerm] = []
    for p in P:
        for idx, slot_dict in enumerate(slot_vars):
            cur = slot_dict[p]
            prev_start = max(0, idx - (window_size - 1))
            prev_vars = [slot_vars[j][p] for j in range(prev_start, idx)]
            prev_len = len(prev_vars)
            history_need = max(0, (window_size - 1) - prev_len)
            history_need = min(history_need, len(history_tail))
            hist_count = 0
            if history_need:
                recent = history_tail[-history_need:]
                hist_count = sum(1 for name in recent if name == p)
            capacity = max(0, 1 - hist_count)
            slack = model.NewIntVar(0, window_size, f"CALL_REC_SLACK_{p}_{idx}")
            model.Add(sum(prev_vars) + cur <= capacity + slack)
            if weight > 0:
                terms.append((f"call_spacing:{p}:slot{idx}", int(weight), slack))
    return terms

def _shift_aliases_map(shifts: list[str]) -> dict[str, str]:
    """
    Build a fuzzy alias map for shifts: 'flex/nights' gets aliases like ['flexnights','nights','night'].
    Keys are soft-normalized alias tokens; values are canonical shift ids (non-normalized label).
    """
    m = {}
    for sh in shifts:
        label = sh  # human label
        s = _soft_norm(sh)
        m[s] = label
        # some common extras
        if "inpatienta" in s: m["ipa"] = label
        if "inpatientb" in s: m["ipb"] = label
        if "outpatienta" in s: m["opa"] = label
        if "outpatientb" in s: m["opb"] = label
        if "flexnights" in s or ("flex" in s and "night" in s):
            for a in ("flexnight","flexnights","nights","night","call","pmcall"):
                m[a] = label
        if s == "flex":
            for a in ("flex", "float"):
                m[a] = label
    return m

# ---- status synonym sets (very permissive) ----
VAC_SYNS  = ("vac", "vacay", "vacation", "pto", "leave", "ooo", "out")
ACAD_SYNS = ("acad", "academic", "admin", "research", "scholar", "scholarly")
CONF_SYNS = ("conf", "conference", "meeting", "symposium", "cme", "course", "rsna", "asnr")
SICK_SYNS = ("sick", "covid")  # matches "sick", "Sick Day", "Out Sick", "covid", etc.

def classify_cell_text(cell: str, shifts: list[str], statuses: list[str], *, _alias_cache: dict[str, str] | None = None) -> tuple[str|None, str|None, bool]:
    """
    Returns ('status', canonical_status, preserve_original) or ('shift', canonical_shift, False) or ('unknown', None, False) or (None, None, False for empty).
    preserve_original=True means the original text should be preserved when writing back.

    Pass _alias_cache (from _shift_aliases_map) to avoid rebuilding it on every call.
    """
    if cell is None:
        return None, None, False
    raw = str(cell).strip()
    if raw == "" or raw.upper() in {"NA", "N/A", "—", "-"}:
        return None, None, False
    raw_upper = raw.upper()

    # 1) shifts FIRST (before statuses) to avoid "OutpatientA" matching "out" in VAC_SYNS
    alias = _alias_cache if _alias_cache is not None else _shift_aliases_map(shifts)
    norm = _soft_norm(raw)
    # exact alias hit
    if norm in alias:
        return "shift", alias[norm], False
    # substring hit
    for key, lab in alias.items():
        if key and key in norm:
            return "shift", lab, False

    # 2) statuses second - check for exact match first (case-insensitive), then synonyms
    # Special case: "LEAVE" (any case) should always be preserved as-is
    if raw_upper == "LEAVE" or raw == "Leave" or raw == "leave":
        return "status", "Vacation", True  # Treat as Vacation but preserve "LEAVE" text

    statuses_upper = {s.upper(): s for s in statuses}
    if raw_upper in statuses_upper:
        # Exact match to canonical status - preserve original text
        return "status", statuses_upper[raw_upper], True
    # Check SICK before VAC: "Out Sick" would otherwise match VAC_SYNS's "out" and be classified
    # as Vacation. The sick-specific tokens are narrower, so checking them first is safe.
    if _looks_like(raw, *SICK_SYNS):
        # Sick is a hard lock-in (solver must not reassign work to a sick attending). Fall back
        # to Vacation if the caller's `statuses` list doesn't include Sick — same effect.
        return "status", "Sick" if "Sick" in statuses else ("Vacation" if "Vacation" in statuses else (statuses[0] if statuses else "Sick")), True
    if _looks_like(raw, *VAC_SYNS):
        return "status", "Vacation" if "Vacation" in statuses else (statuses[0] if statuses else "Vacation"), False
    if _looks_like(raw, *ACAD_SYNS):
        return "status", "Academic" if "Academic" in statuses else (statuses[0] if statuses else "Academic"), False
    if _looks_like(raw, *CONF_SYNS):
        return "status", "Conference" if "Conference" in statuses else (statuses[0] if statuses else "Conference"), False

    # 3) unknown -> preserve text; solver must not assign this p,d
    return "unknown", None, False


def read_lockins_from_df(
    df: pd.DataFrame,
    *,
    date_col: str = "Date",
    worker_cols: list[str],
    shifts: list[str],
    statuses: list[str],
    dates_in_scope: list[date]
):
    """
    Scans df for rows with Date ∈ dates_in_scope and non-empty cells in worker cols.
    Returns:
      - shift_lockins: list of {person_id, date, shift_id}
      - status_lockins: list of {person_id, date, status}  (includes 'Blocked' for unknowns)
      - preserve_text: {(date, worker): original_string} for writing back unchanged when Blocked
    """
    date_keys = df[date_col].apply(_coerce_date)
    rows_by_date = defaultdict(list)
    for idx, d in date_keys.items():
        if d is not None:
            rows_by_date[d].append(idx)

    shift_lockins, status_lockins = [], []
    preserve_text = {}
    alias_cache = _shift_aliases_map(shifts)

    # Debug: print all dates being parsed for a specific worker
    debug_worker = _norm_id("Li")

    for d in dates_in_scope:
        if d not in rows_by_date:
            continue
        for idx in rows_by_date[d]:
            for w in worker_cols:
                if w not in df.columns:
                    continue
                cell = df.at[idx, w]
                kind, val, preserve_original = classify_cell_text(cell, shifts, statuses, _alias_cache=alias_cache)

                # Debug: print what we're reading for Li on dates around March 30
                if w == debug_worker and d >= date(2026, 3, 29) and d <= date(2026, 4, 1):
                    print(f"[LOCKIN_READ] {d}: {w} cell='{cell}' -> kind={kind}, val={val}, preserve={preserve_original}")

                if kind is None:
                    continue  # empty cell
                if kind == "status":
                    status_lockins.append({"person_id": w, "date": d, "status": val})
                    # Preserve original text if it's an exact match (e.g., "LEAVE" stays "LEAVE", but "Vac" becomes "Vacation")
                    if preserve_original:
                        original_cell = str(cell).strip()
                        if original_cell and original_cell != val:
                            preserve_text[(d, w)] = original_cell
                elif kind == "shift":
                    shift_lockins.append({"person_id": w, "date": d, "shift_id": val})
                else:
                    # unknown text -> treat as Blocked day + remember original text
                    status_lockins.append({"person_id": w, "date": d, "status": "Blocked"})
                    preserve_text[(d, w)] = str(cell)

    return shift_lockins, status_lockins, preserve_text

def read_call_lockins_from_df(
    df: pd.DataFrame,
    *,
    date_col: str,
    call_col: str,
    worker_names: list[str],
    call_dates: list[date],
) -> List[Dict]:
    if call_col not in df.columns:
        raise ValueError(f"Missing '{call_col}' column for Call assignments.")
    date_keys = df[date_col].apply(_coerce_date)
    rows_by_date = defaultdict(list)
    for idx, d in date_keys.items():
        if d is not None:
            rows_by_date[d].append(idx)

    name_by_norm = {_norm_id(w): w for w in worker_names}
    lockins = []
    for d in call_dates:
        idxs = rows_by_date.get(d, [])
        chosen = None
        for idx in idxs:
            raw = str(df.at[idx, call_col]).strip()
            if not raw or raw.upper() in {"NA", "N/A", "-", "—", "--"}:
                continue
            norm = _norm_id(raw)
            if norm not in name_by_norm:
                continue
            human = name_by_norm[norm]
            if chosen and chosen != human:
                raise ValueError(f"Multiple Call values on {d}: '{chosen}' vs '{human}'.")
            chosen = human
        if chosen:
            lockins.append({"person_id": chosen, "date": d})
    return lockins

def open_sheet(title_or_url: str, service_account_json="service_account.json"):
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(service_account_json, scopes=scopes)
    gc = gspread.authorize(creds)

    # Open by URL if it looks like one; otherwise by title
    if title_or_url.startswith("http"):
        return gc.open_by_url(title_or_url)
    return gc.open(title_or_url)


def parse_date(date_str: str) -> date:
    """Parse a date string in common formats (ISO, US-style)."""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date format: '{date_str}'. Expected YYYY-MM-DD or similar.")

def _fmt_date(d):
    """Format a date/datetime to YYYYMMDD for readable var names."""
    if isinstance(d, datetime):
        d = d.date()
    if not isinstance(d, date):
        raise TypeError(f"Expected date or datetime, got {type(d)}")
    return d.strftime("%Y%m%d")

def split_weekdays_and_saturdays(start_date: date, end_date: date) -> Tuple[List[date], List[date]]:
    """Return (weekdays, saturdays) lists for the inclusive range. Sundays are excluded."""
    if start_date > end_date:
        raise ValueError(f"Start date {start_date} is after end date {end_date}")
    weekdays, saturdays = [], []
    days = (end_date - start_date).days + 1
    for i in range(days):
        d = start_date + timedelta(days=i)
        wd = d.weekday()           # Mon=0 ... Sun=6
        if 0 <= wd <= 4:
            weekdays.append(d)
        elif wd == 5:
            saturdays.append(d)
        # wd == 6 (Sunday) -> skip
    return weekdays, saturdays

def get_weekdays_and_saturdays_from_args() -> Tuple[date, date, List[date], List[date]]:
    """
    Reads two CLI args: start_date end_date.
    Returns start_date, end_date, weekdays_list, saturdays_list.
    """
    if len(sys.argv) < 3:
        print("Usage: python schedule.py <start_date> <end_date>")
        print("Example: python schedule.py 2025-11-01 2025-11-30")
        sys.exit(1)

    start_date = parse_date(sys.argv[1])
    end_date   = parse_date(sys.argv[2])
    weekdays, saturdays = split_weekdays_and_saturdays(start_date, end_date)
    return start_date, end_date, weekdays, saturdays


# --------- Builders ---------

def build_assignment_model(workers, dates, shifts, *, prefix="x"):
    """As before: build BoolVars for (worker, date, shift)."""
    model = cp_model.CpModel()
    P = tuple(_norm_id(w) for w in workers)
    D = tuple(sorted({(d.date() if isinstance(d, datetime) else d) for d in dates}))
    S = tuple(_norm_id(s) for s in shifts)
    if any(not w for w in P): raise ValueError("Empty worker id")
    if any(not s for s in S): raise ValueError("Empty shift id")

    X = {}
    for p in P:
        for d in D:
            dtag = _fmt_date(d)
            for s in S:
                X[(p, d, s)] = model.NewBoolVar(f"{prefix}_{p}_{dtag}_{s}")
    return model, X, P, D, S

def add_status_variables(model, workers, dates, statuses, *, prefix="z"):
    """
    Build BoolVars for non-coverage statuses on each person-day.
    Returns Z[(worker, date, status)], and normalized T (statuses).
    """
    P = tuple(_norm_id(w) for w in workers)
    D = tuple(sorted({(d.date() if isinstance(d, datetime) else d) for d in dates}))
    T = tuple(_norm_id(t) for t in statuses)
    if any(not t for t in T): raise ValueError("Empty status id")

    Z = {}
    for p in P:
        for d in D:
            dtag = _fmt_date(d)
            for t in T:
                Z[(p, d, t)] = model.NewBoolVar(f"{prefix}_{p}_{dtag}_{t}")
    return Z, T

# --------- Core constraints tying shifts and statuses ---------

def add_one_thing_per_day(model, X, Z, P, D, S, T):
    """
    For each person-day: sum(shifts) + sum(statuses) <= 1
    Meaning: you can work one shift OR be on exactly one status, or be free.
    """
    for p in P:
        for d in D:
            model.Add(
                sum(X[(p, d, s)] for s in S) + sum(Z[(p, d, t)] for t in T) <= 1
            )

def add_coverage_constraints(model, X, P, D, S, required_by_day_shift):
    """
    required_by_day_shift: dict[(date, shift)] -> int
    Only shift vars X appear (statuses never satisfy coverage).
    """
    for d in D:
        for s in S:
            req = int(required_by_day_shift.get((d, s), 0))
            if req == 0:
                model.Add(sum(X[(p, d, s)] for p in P) == 0)
            else:
                model.Add(sum(X[(p, d, s)] for p in P) == req)

# --------- Lock-ins / Unavailability ---------

def apply_shift_lockins(model, X, lockins):
    """
    lockins: list of dicts {person_id, date, shift_id}
    Forces those X variables to 1.
    """
    for L in lockins:
        p = _norm_id(L["person_id"])
        d = L["date"].date() if isinstance(L["date"], datetime) else L["date"]
        s = _norm_id(L["shift_id"])
        model.Add(X[(p, d, s)] == 1)

def apply_status_lockins(model, Z, status_lockins):
    """
    status_lockins: list of dicts {person_id, date, status}
    Forces those Z variables to 1 (e.g., VACATION / CONFERENCE / ACADEMIC).
    """
    for L in status_lockins:
        p = _norm_id(L["person_id"])
        d = L["date"].date() if isinstance(L["date"], datetime) else L["date"]
        t = _norm_id(L["status"])
        model.Add(Z[(p, d, t)] == 1)

def forbid_any_assignment_on_status(model, X, Z, P, D, S, forbidden_statuses):
    """
    If a person is on one of these statuses, they cannot work any shift that day.
    With add_one_thing_per_day this is redundant but explicit (good for clarity/logs).
    """
    F = { _norm_id(t) for t in forbidden_statuses }
    for p in P:
        for d in D:
            for t in F:
                # For each forbidden status t: Z[p,d,t] = 1 -> all X[p,d,*] = 0
                for s in S:
                    # X <= 1 - Z  (i.e., X and Z can't both be 1)
                    model.Add(X[(p, d, s)] + Z[(p, d, t)] <= 1)

def add_soft_weekly_targets(
    model,
    X, P, D, S,
    *,
    work_shift_ids,                      # list[str] — which shifts count as "work"
    weekly_target_by_person,             # dict[str,int] — target # of shifts *per week* for each worker (by normalized id)
    weight_over_by_person=None,          # dict[str,int] or None — per-person penalty weight when over target
    weight_under_by_person=None,         # dict[str,int] or None — per-person penalty weight when under target
    include_weekends=False,
    week_start=0                         # Monday=0..Sunday=6
):
    """
    For each person p and week w:
        count[p,w] = number of WORK shifts assigned that week
        deviation modeled as count - target = over - under
    Objective adds:  weight_over[p] * over + weight_under[p] * under

    Returns a list of linear terms to add to your global objective.
    """
    WORK = { _norm_id(s) for s in work_shift_ids }

    weeks = partition_into_weeks(D, week_start, include_weekends)

    # Default uniform weights if not provided
    if weight_over_by_person is None:
        weight_over_by_person = {p: 1000 for p in P}
    if weight_under_by_person is None:
        weight_under_by_person = {p: 1000 for p in P}

    terms = []

    for wi, E in enumerate(weeks):
        ub_week = len(E)  # max # of shifts anyone could take that week
        if ub_week == 0:
            continue

        for p in P:
            tgt = int(weekly_target_by_person.get(p, 0))

            # count[p,wi] = sum over eligible days of "any work shift chosen"
            # We already have per-shift Booleans; we count all work shifts.
            cnt = model.NewIntVar(0, ub_week, f"WKCNT_{p}_{wi}")
            model.Add(cnt == sum(X[(p, d, s)] for d in E for s in S if s in WORK))

            # Model |cnt - tgt| asymmetrically:
            #   cnt - tgt = over - under, with over,under >= 0
            # Choose safe upper bounds for over/under:
            #   over <= max(0, ub_week - tgt)
            #   under <= max(tgt, ub_week)
            over_ub  = max(0, ub_week - tgt)
            under_ub = max(tgt, ub_week)

            over  = model.NewIntVar(0, over_ub,  f"OVER_{p}_{wi}")
            under = model.NewIntVar(0, under_ub, f"UNDER_{p}_{wi}")
            model.Add(cnt - tgt == over - under)

            w_over  = int(weight_over_by_person.get(p,  weight_over_by_person.get(p, 1000)))
            w_under = int(weight_under_by_person.get(p, weight_under_by_person.get(p, 1000)))

            # Add penalty terms
            if w_over  > 0: terms.append(w_over  * over)
            if w_under > 0: terms.append(w_under * under)

    return terms

def add_soft_weekly_targets_convex(
    model,
    X, P, D, S,
    *,
    work_shift_ids,                      # list[str] – which shifts count as "work"
    weekly_target_by_person,             # dict[str,int] – per-person weekly target (keys are normalized ids)
    weight_over_by_person=None,          # dict[str,int] – base multiplier when OVER target
    weight_under_by_person=None,         # dict[str,int] – base multiplier when UNDER target
    include_weekends=False,
    week_start=0,                        # Monday=0..Sunday=6
    penalty_shape="square",              # "square" or "geometric"
    geometric_base=2                     # only used if penalty_shape=="geometric"
) -> List[ObjTerm]:
    WORK = { _norm_id(s) for s in work_shift_ids }

    weeks = partition_into_weeks(D, week_start, include_weekends)

    if weight_over_by_person is None:
        weight_over_by_person = {p: 1 for p in P}
    if weight_under_by_person is None:
        weight_under_by_person = {p: 1 for p in P}

    def make_weights(ub, base_weight, shape, gbase):
        if ub <= 0:
            return []
        if shape == "square":
            w = [2*i - 1 for i in range(1, ub+1)]  # 1,3,5,...
        elif shape == "geometric":
            w = [int(gbase**(i-1)) for i in range(1, ub+1)]  # 1, g, g^2, ...
        else:
            raise ValueError("penalty_shape must be 'square' or 'geometric'")
        return [base_weight * wi for wi in w]

    labeled_terms: List[ObjTerm] = []

    for wi, E in enumerate(weeks):
        ub_week = len(E)
        if ub_week == 0:
            continue

        for p in P:
            tgt = int(weekly_target_by_person.get(p, 0))

            cnt = model.NewIntVar(0, ub_week, f"WKCNT_{p}_{wi}")
            model.Add(cnt == sum(X[(p, d, s)] for d in E for s in S if s in WORK))

            over_ub  = max(0, ub_week - tgt)
            under_ub = max(tgt, ub_week)

            over  = model.NewIntVar(0, over_ub,  f"OVER_{p}_{wi}")
            under = model.NewIntVar(0, under_ub, f"UNDER_{p}_{wi}")
            model.Add(cnt - tgt == over - under)

            # OVER
            if over_ub > 0:
                z_over = [model.NewBoolVar(f"ZOVER_{p}_{wi}_{k}") for k in range(1, over_ub+1)]
                model.Add(sum(z_over) == over)
                for k in range(1, over_ub):
                    model.Add(z_over[k-1] >= z_over[k])

                W_over = make_weights(
                    over_ub,
                    int(weight_over_by_person.get(p, 1)),
                    penalty_shape,
                    geometric_base
                )
                for k in range(over_ub):
                    labeled_terms.append((f"wk_over:{p}:w{wi}:step{k+1}", int(W_over[k]), z_over[k]))

            # UNDER
            if under_ub > 0:
                z_under = [model.NewBoolVar(f"ZUNDER_{p}_{wi}_{k}") for k in range(1, under_ub+1)]
                model.Add(sum(z_under) == under)
                for k in range(1, under_ub):
                    model.Add(z_under[k-1] >= z_under[k])

                W_under = make_weights(
                    under_ub,
                    int(weight_under_by_person.get(p, 1)),
                    penalty_shape,
                    geometric_base
                )
                for k in range(under_ub):
                    labeled_terms.append((f"wk_under:{p}:w{wi}:step{k+1}", int(W_under[k]), z_under[k]))

    return labeled_terms



def add_weekly_shift_mix_penalty(
    model,
    X, P, D, S,
    *,
    work_shift_ids,
    include_weekends=False,
    week_start=0,
    weight_repeat=50,
    min_free_repeat=1,
    ignore_shifts=None
) -> List[ObjTerm]:
    WORK = { _norm_id(s) for s in work_shift_ids }
    if ignore_shifts:
        WORK -= { _norm_id(s) for s in ignore_shifts }

    weeks = partition_into_weeks(D, week_start, include_weekends)

    labeled_terms: List[ObjTerm] = []

    for wi, E in enumerate(weeks):
        if not E:
            continue
        ub = len(E)
        for p in P:
            for s in S:
                if s not in WORK:
                    continue
                cnt = model.NewIntVar(0, ub, f"CNT_{p}_{wi}_{s}")
                model.Add(cnt == sum(X[(p, d, s)] for d in E))

                rep = model.NewIntVar(0, ub, f"REP_{p}_{wi}_{s}")
                model.Add(rep >= 0)
                model.Add(rep >= cnt - int(min_free_repeat))

                labeled_terms.append((f"mix:{p}:w{wi}:shift:{s}", int(weight_repeat), rep))

    return labeled_terms


def _coerce_date(x) -> date:
    if isinstance(x, date) and not isinstance(x, datetime):
        return x
    if isinstance(x, datetime):
        return x.date()
    s = str(x).strip()
    if not s:
        return None
    return parse_dt(s).date()

ACA = _norm_id('Academic')

def _previous_friday(d: date) -> date:
    if isinstance(d, datetime):
        d = d.date()
    if not isinstance(d, date):
        raise TypeError(f"Expected date/datetime, got {type(d)}")
    delta = (d.weekday() - 4) % 7
    return d - timedelta(days=delta)

def add_monday_after_call_academic_penalty(
    model: cp_model.CpModel,
    Z,
    *,
    P,
    D,
    T,
    call_lockins: List[Dict],
    weight: int = 2000,
) -> Tuple[List[ObjTerm], int]:
    aca = _norm_id("Academic")
    if aca not in T or weight <= 0:
        return [], 0

    Dset = set(D)
    seen: set[tuple[str, date]] = set()
    terms: List[ObjTerm] = []
    count = 0

    for lock in call_lockins:
        p = _norm_id(lock["person_id"])
        if p not in P:
            continue
        call_day = lock["date"].date() if isinstance(lock["date"], datetime) else lock["date"]
        delta = (0 - call_day.weekday()) % 7
        if delta == 0:
            delta = 7
        monday = call_day + timedelta(days=delta)
        key = (p, monday)
        if monday not in Dset or key in seen:
            continue
        seen.add(key)

        viol = model.NewBoolVar(f"POSTCALL_MON_{p}_{_fmt_date(monday)}")
        model.Add(Z[(p, monday, aca)] + viol == 1)
        terms.append((f"post_call_mon:{p}:{_fmt_date(monday)}", int(weight), viol))
        count += 1

    return terms, count


def add_no_flex_followed_by_inpatientA_penalty(
    model: cp_model.CpModel,
    X,
    *,
    P,
    D,
    S,
    flex_label: str = "Flex/Nights",
    inpatient_label: str = "InpatientA",
    weight: int = 1500,
) -> Tuple[List[ObjTerm], int]:
    flex = _norm_id(flex_label)
    ipa = _norm_id(inpatient_label)
    if flex not in S or ipa not in S or weight <= 0:
        return [], 0

    dates = sorted(D)
    terms: List[ObjTerm] = []
    count = 0
    for i in range(len(dates) - 1):
        d = dates[i]
        nxt = dates[i + 1]
        for p in P:
            viol = model.NewBoolVar(f"FLEX_IPA_{p}_{_fmt_date(d)}")
            model.Add(X[(p, d, flex)] + X[(p, nxt, ipa)] - viol <= 1)
            model.Add(viol <= X[(p, d, flex)])
            model.Add(viol <= X[(p, nxt, ipa)])
            terms.append((f"flex_follow_ipa:{p}:{_fmt_date(d)}", int(weight), viol))
            count += 1
    return terms, count


def add_kuoy_flex_penalties(
    model: cp_model.CpModel,
    X,
    *,
    P,
    D,
    S,
    call_lockins: List[Dict],
    thursday_weight: int = 800,
    friday_weight: int = 1200,
) -> Tuple[List[ObjTerm], int]:
    kuoy = _norm_id("Kuoy")
    flex = _norm_id("Flex/Nights")
    if kuoy not in P or flex not in S:
        return [], 0

    terms: List[ObjTerm] = []
    count = 0

    if thursday_weight > 0:
        for d in D:
            if d.weekday() == 3:
                terms.append((f"kuoy_thu_flex:{_fmt_date(d)}", int(thursday_weight), X[(kuoy, d, flex)]))
                count += 1

    if friday_weight > 0:
        fridays = set()
        for lock in call_lockins:
            if _norm_id(lock["person_id"]) != kuoy:
                continue
            call_day = lock["date"].date() if isinstance(lock["date"], datetime) else lock["date"]
            friday = _previous_friday(call_day)
            if friday in D and friday.weekday() == 4:
                fridays.add(friday)
        for d in sorted(fridays):
            terms.append((f"kuoy_pre_call_flex:{_fmt_date(d)}", int(friday_weight), X[(kuoy, d, flex)]))
            count += 1

    return terms, count


def add_wednesday_no_academic_penalty(
    model: cp_model.CpModel,
    Z,
    *,
    P,
    D,
    T,
    worker_names: list[str],
    weight: int = 2000,
) -> Tuple[List[ObjTerm], int]:
    """Penalize a worker for being Academic on Wednesdays."""
    aca = _norm_id("Academic")
    if aca not in T or weight <= 0:
        return [], 0

    terms: List[ObjTerm] = []
    count = 0
    for name in worker_names:
        pid = _norm_id(name)
        if pid not in P:
            continue
        for d in D:
            if d.weekday() != 2:  # Wednesday
                continue
            terms.append((f"wed_no_academic:{pid}:{_fmt_date(d)}", int(weight), Z[(pid, d, aca)]))
            count += 1
    return terms, count


def add_tuesday_shift_avoid_penalties(
    model: cp_model.CpModel,
    X,
    *,
    P,
    D,
    S,
    worker_names: list[str],
    weight: int = 1400,
    banned_shift_labels: list[str] | None = None,
) -> Tuple[List[ObjTerm], int]:
    banned_shift_labels = banned_shift_labels or [
        "InpatientA", "InpatientB", "OutpatientA", "OutpatientB", "Flex/Nights",
    ]
    banned = {_norm_id(s) for s in banned_shift_labels} & set(S)
    if not banned or weight <= 0:
        return [], 0

    terms: List[ObjTerm] = []
    count = 0
    for name in worker_names:
        pid = _norm_id(name)
        if pid not in P:
            continue
        for d in D:
            if d.weekday() != 1:
                continue
            for s in banned:
                terms.append((f"tue_no_shift:{pid}:{_fmt_date(d)}:{s}", int(weight), X[(pid, d, s)]))
                count += 1
    return terms, count

def freeze_nonacademic_unless_locked(model, Z, P, D, T, status_lockins):

    locked = {( _norm_id(L["person_id"]),
                (L["date"].date() if isinstance(L["date"], datetime) else L["date"]),
                _norm_id(L["status"]) )
              for L in status_lockins}

    for p in P:
        for d in D:
            for t in T:
                if t == ACA:
                    continue  # Academic is allowed
                if (p, d, t) not in locked:
                    model.Add(Z[(p, d, t)] == 0)  # forbid un-locked Vacation/Conference


def fill_df_from_solution(
    df: pd.DataFrame,
    *,
    date_col: str = "Date",
    worker_cols: list[str],
    shifts: list[str],
    statuses: list[str],
    P, D, S, T,
    X, Z,
    solver: cp_model.CpSolver,
    preserve_text: dict[tuple[date, str], str] | None = None,
) -> pd.DataFrame:
    out = df.copy()
    preserve_text = preserve_text or {}

    s_norm_to_label = { _norm_id(s): s for s in shifts }
    t_norm_to_label = { _norm_id(t): t for t in statuses }
    p_norm_to_label = { _norm_id(w): w for w in worker_cols }

    BLOCK = _norm_id('Blocked')

    date_keys = out[date_col].apply(_coerce_date)
    date_to_rows = {}
    for idx, d in date_keys.items():
        if d is None: continue
        date_to_rows.setdefault(d, []).append(idx)

    for d in D:
        if d not in date_to_rows:
            continue
        row_indices = date_to_rows[d]

        for p in P:
            human_worker = p_norm_to_label.get(p)
            if human_worker is None or human_worker not in out.columns:
                continue

            # Check if any status is set
            val = ""
            status_found = False
            for t in T:
                if solver.Value(Z[(p, d, t)]) == 1:
                    status_found = True
                    status_label = t_norm_to_label.get(t, t)
                    # Use preserved original text if available (e.g., "LEAVE"), otherwise use canonical name
                    val = preserve_text.get((d, human_worker), status_label)
                    break

            # If no status, check for shifts
            if not status_found:
                for s in S:
                    if solver.Value(X[(p, d, s)]) == 1:
                        val = s_norm_to_label.get(s, s)
                        break

            for idx in row_indices:
                out.at[idx, human_worker] = val


    return out

def publish_df_to_new_sheet(sh, worksheet_title: str, df: pd.DataFrame):
    # Create or open the destination worksheet
    try:
        ws = sh.worksheet(worksheet_title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=worksheet_title,
            rows=str(len(df) + 10),
            cols=str(len(df.columns) + 5),
        )

    # Resize to fit and write header + data
    ws.resize(rows=len(df) + 1, cols=len(df.columns))
    values = [list(df.columns)] + df.astype(str).values.tolist()
    ws.update("A1", values)

# ---------- Conditional formatting (colors by label) ----------
# palette you can customize; keys are EXACT labels written by the solver
LABEL_COLORS = {
    # Shifts
    "InpatientA":   "#b6d7a8",  # light green 2
    "InpatientB":   "#e06666",  # light red 1
    "OutpatientA":  "#c9daf8",  # light cornflower blue 3
    "OutpatientB":  "#6fa8dc",  # light blue 1
    "Flex/Nights":  "#7030a0",  # cutom purple
    "Flex":         "#ead1dc",  # light magenta 3

    # Statuses
    "Academic":     "#fff2cc",  # light yellow 3
    "Conference":   "#ffc000",  # custom orange
    "Vacation":     "#c00000",  # red
    "Sick":         "#a64d79",  # plum — distinct from Vacation/Conference so it stands out
    "BLOCKED":      "#e0e0e0",  # gray
}

def _hex_to_rgb01(hexstr: str) -> tuple[float, float, float]:
    h = hexstr.lstrip("#")
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return r, g, b

from gspread_formatting import (
    Color, CellFormat, TextFormat,
    ConditionalFormatRule, BooleanRule, BooleanCondition,
    GridRange, get_conditional_format_rules
)

def apply_label_colors(ws, df, worker_cols, label_colors, header_row: int = 1):
    """
    Applies one conditional format rule per label across all worker columns.
    - Vacation and Flex/Nights → white text
    - Weekend and Holiday → grey background
    """
    cols = list(df.columns)
    worker_idxs_1based = [cols.index(w) + 1 for w in worker_cols if w in cols]
    if not worker_idxs_1based:
        return

    start_col = min(worker_idxs_1based) - 1  # inclusive, 0-based
    end_col = max(worker_idxs_1based)        # exclusive, 0-based
    start_row = header_row                   # inclusive, 0-based
    end_row = header_row + len(df)           # exclusive, 0-based

    rng = GridRange(
        sheetId=ws.id,
        startRowIndex=start_row,
        endRowIndex=end_row,
        startColumnIndex=start_col,
        endColumnIndex=end_col,
    )

    rules = get_conditional_format_rules(ws)
    rules.clear()

    # 1️⃣ Color rules for all known labels
    for label, hexcolor in label_colors.items():
        r = int(hexcolor[1:3], 16) / 255.0
        g = int(hexcolor[3:5], 16) / 255.0
        b = int(hexcolor[5:7], 16) / 255.0

        txt_color = Color(red=0, green=0, blue=0)
        if label.lower() in ("vacation", "flex/nights"):
            txt_color = Color(red=1, green=1, blue=1)  # white

        fmt = CellFormat(
            backgroundColor=Color(red=r, green=g, blue=b),
            textFormat=TextFormat(bold=True, foregroundColor=txt_color),
        )

        rule = ConditionalFormatRule(
            ranges=[rng],
            booleanRule=BooleanRule(
                condition=BooleanCondition("TEXT_EQ", [label]),
                format=fmt,
            ),
        )
        rules.append(rule)

    # 2️⃣ Grey rule for "Weekend" and "Holiday"
    grey = Color(red=0.85, green=0.85, blue=0.85)
    fmt_grey = CellFormat(
        backgroundColor=grey,
        textFormat=TextFormat(bold=True, foregroundColor=Color(red=0, green=0, blue=0))
    )

    for word in ("Weekend", "Holiday"):
        rule = ConditionalFormatRule(
            ranges=[rng],
            booleanRule=BooleanRule(
                condition=BooleanCondition("TEXT_EQ", [word]),
                format=fmt_grey,
            ),
        )
        rules.append(rule)

    rules.save()
    
from gspread_formatting import set_frozen


def _dates_in_range_or_list(dates_spec, D) -> list[date]:
    """Resolve a date specification to concrete dates intersected with D.

    dates_spec can be:
      - a list/tuple of exactly 2 date objects → treated as (start, end) inclusive range
      - any other iterable of date objects → used as-is
    Only dates present in D are returned.
    """
    Dset = set(D)
    if (
        isinstance(dates_spec, (list, tuple))
        and len(dates_spec) == 2
        and isinstance(dates_spec[0], date)
        and isinstance(dates_spec[1], date)
        and dates_spec[1] > dates_spec[0]
    ):
        # Treat as (start_date, end_date) range
        return [d for d in Dset if dates_spec[0] <= d <= dates_spec[1]]
    # Otherwise treat as explicit list of dates
    return [d for d in dates_spec if d in Dset]


def add_worker_shift_forbids(
    model,
    X, P, D, S,
    *,
    forbids_global: Dict[str, Iterable[str]] = None,
    forbids_by_date: List[Dict] = None
):
    """
    Forbid specific worker/shift combos.

    Parameters
    ----------
    forbids_global : dict worker -> iterable of shift labels
        Applies every day in D. Example:
          {'Chow': ['Flex/Nights', 'InpatientA'], 'Li': ['InpatientB']}

    forbids_by_date : list of dicts
        Each item is:
          {
            "person_id": "Chow",
            "shift_id": "Flex/Nights",
            # EITHER a list/iterable of exact dates:
            "dates": [date(2026,2,3), date(2026,2,6)]
            # OR a closed range as a (start_date, end_date) tuple:
            # "dates": (date(2026,2,10), date(2026,2,14))
          }
        Applies only on those dates (intersected with D).

    Effect
    ------
    Adds constraints: X[(p, d, s)] == 0 for each forbidden triple.
    Names are matched case-insensitively with your _norm_id().
    Silently skips unknown workers/shifts.

    Returns
    -------
    int : number of constraints added
    """
    forbids_global = forbids_global or {}
    forbids_by_date = forbids_by_date or []

    Pset = set(P)
    Sset = set(S)

    added = 0

    # ---- Global forbids (apply to all days) ----
    for w_label, shift_list in forbids_global.items():
        p = _norm_id(w_label)
        if p not in Pset:
            continue
        for s_label in shift_list:
            s = _norm_id(s_label)
            if s not in Sset:
                continue
            for d in D:
                model.Add(X[(p, d, s)] == 0); added += 1

    # ---- Date-specific forbids ----
    for rule in forbids_by_date:
        w_label = rule.get("person_id", "")
        s_label = rule.get("shift_id", "")
        dates_spec = rule.get("dates", [])

        p = _norm_id(w_label)
        s = _norm_id(s_label)
        if p not in Pset or s not in Sset:
            continue

        days = _dates_in_range_or_list(dates_spec, D)
        for d in days:
            model.Add(X[(p, d, s)] == 0); added += 1

    return added

def add_horizon_cumulative_balance(
    model,
    X, P, D, S,
    *,
    work_shift_ids,
    weekly_target_by_worker,
    include_weekends=False,
    week_start=0,
    weight_abs=400,
    big_threshold=2,
    weight_big=1200,
    horizon_debug: dict | None = None,   # <-- NEW
) -> list[tuple[str, int, cp_model.IntVar]]:
    WORK = { _norm_id(s) for s in work_shift_ids }

    weeks = partition_into_weeks(D, week_start, include_weekends)
    if not weeks:
        return []

    num_weeks = len(weeks)

    elig_days_all = [d for wk in weeks for d in wk]
    ub_total = len(elig_days_all)

    terms = []
    for p in P:
        tw = model.NewIntVar(0, ub_total, f"TOTAL_WORK_{p}")
        model.Add(tw == sum(X[(p, d, s)] for d in elig_days_all for s in S if s in WORK))

        # resolve weekly target (accepts normalized or human keys)
        target_week = None
        if p in weekly_target_by_worker:
            target_week = int(weekly_target_by_worker[p])
        else:
            for k, v in weekly_target_by_worker.items():
                if _norm_id(k) == p:
                    target_week = int(v); break
        if target_week is None:
            target_week = 0

        target_total = target_week * num_weeks

        pos = model.NewIntVar(0, ub_total, f"HZ_POS_{p}")
        neg = model.NewIntVar(0, ub_total, f"HZ_NEG_{p}")
        model.Add(tw - int(target_total) == pos - neg)
        abs_dev = model.NewIntVar(0, ub_total, f"HZ_ABS_{p}")
        model.Add(abs_dev == pos + neg)

        excess = model.NewIntVar(0, ub_total, f"HZ_EXCESS_{p}")
        model.Add(excess >= 0)
        model.Add(excess >= abs_dev - int(big_threshold))

        # expose all internals for printing later
        if horizon_debug is not None:
            horizon_debug[p] = {
                "total_work": tw,
                "pos": pos,
                "neg": neg,
                "abs_dev": abs_dev,
                "excess": excess,
                "target_week": target_week,
                "num_weeks": num_weeks,
                "target_total": target_total,
            }

        terms.append((f"hz_abs:{p}",   int(weight_abs),  abs_dev))
        terms.append((f"hz_excess:{p}", int(weight_big), excess))

    return terms

def add_horizon_cumulative_balance_convex(
    model,
    X, P, D, S,
    *,
    work_shift_ids,                 # which shifts count as "work"
    weekly_target_by_worker,        # dict: worker label (norm or human) -> int target per week
    status_lockins=None,            # list of dicts with person_id, date, status - to adjust targets for unavailable days
    unavailable_statuses=None,      # which statuses reduce target (default: Vacation, Conference, Blocked)
    historical_counts_by_worker=None,  # dict: normalized worker_id -> historical shift count before this period
    historical_weeks=None,          # int or dict: number of weeks the historical data spans (for target calculation)
    historical_unavailable_days_by_worker=None,  # dict: worker_id -> unavailable days in historical period
    include_weekends=False,
    week_start=0,                   # Monday=0..Sunday=6
    # base weights for the convex *excess* cost (can be scalar or per-person dict)
    weight_abs=0,                   # optional linear |dev| term (set 0 if you want purely convex)
    weight_excess_by_person=None,   # dict[str,int] or scalar; multiplies convex steps
    big_threshold=1,                # free window for |dev| before counting "excess"
    penalty_shape="square",         # "square" or "geometric"
    geometric_base=2,               # for penalty_shape="geometric"
    horizon_debug: dict | None = None,  # optional debug capture
) -> list[tuple[str, int, cp_model.IntVar]]:
    """
    Horizon (full-range) balancing with convex (super-linear) penalty on 'excess' = max(0, |dev| - big_threshold).

    Can incorporate historical shift counts from before the current scheduling period.

    Variables per worker p:
      - hist[p]  : historical shifts worked (constant, not a variable)
      - tw[p]     : current horizon work shifts (variable)
      - total[p]  : hist[p] + tw[p] (combined total)
      - pos/neg   : total - target_total = pos - neg
      - abs_dev   : |total - target_total| = pos + neg
      - excess    : max(0, abs_dev - big_threshold)
      - z[k]      : 0/1 step vars s.t. sum z = excess and z[k] >= z[k+1] (prefix chain)

    Cost:
      weight_abs * abs_dev  +  sum_k (step_weight[k] * z[k]) where step_weight[k] is increasing

    Historical Data:
      - historical_counts_by_worker: dict of worker_id -> shifts worked before this period
      - historical_weeks: number (int) or dict of worker_id -> number of weeks the historical data spans
    """
    WORK = { _norm_id(s) for s in work_shift_ids }

    # Normalize historical_weeks to dict (support both int and dict)
    hist_weeks_by_worker = {}
    if historical_weeks is None:
        hist_weeks_by_worker = {p: 0 for p in P}
    elif isinstance(historical_weeks, (int, float)):
        hist_weeks_by_worker = {p: int(historical_weeks) for p in P}
    else:
        # It's a dict - normalize keys
        for p in P:
            if p in historical_weeks:
                hist_weeks_by_worker[p] = int(historical_weeks[p])
            else:
                # Try to find by human name
                found = None
                for k, v in historical_weeks.items():
                    if _norm_id(k) == p:
                        found = v
                        break
                hist_weeks_by_worker[p] = int(found) if found is not None else 0

    # Normalize historical counts
    historical_counts_by_worker = historical_counts_by_worker or {}
    hist_counts = {}
    for p in P:
        # Accept normalized or human keys
        if p in historical_counts_by_worker:
            hist_counts[p] = int(historical_counts_by_worker[p])
        else:
            # Try to find by human name
            found = None
            for k, v in historical_counts_by_worker.items():
                if _norm_id(k) == p:
                    found = v
                    break
            hist_counts[p] = int(found) if found is not None else 0

    weeks = partition_into_weeks(D, week_start, include_weekends)
    if not weeks:
        return []

    num_weeks = len(weeks)

    elig_days_all = [d for wk in weeks for d in wk]
    ub_total = len(elig_days_all)                      # safe UB for total shifts any person could work
    if ub_total == 0:
        return []

    # normalize weight_excess_by_person (accept scalar or dict)
    if weight_excess_by_person is None:
        weight_excess_by_person = {p: 1 for p in P}
    elif isinstance(weight_excess_by_person, (int, float)):
        weight_excess_by_person = {p: int(weight_excess_by_person) for p in P}
    else:
        # ensure every p has an entry (default 1)
        weight_excess_by_person = {p: int(weight_excess_by_person.get(p, 1)) for p in P}

    def resolve_weekly_target_for_p(p_norm: str) -> int:
        # accepts normalized or human keys
        if p_norm in weekly_target_by_worker:
            return int(weekly_target_by_worker[p_norm])
        for k, v in weekly_target_by_worker.items():
            if _norm_id(k) == p_norm:
                return int(v)
        return 0

    # Count unavailable days per worker (from status lockins)
    unavailable_statuses = unavailable_statuses or {"Vacation", "Conference", "Blocked", "Sick"}
    unavailable_norm = {_norm_id(s) for s in unavailable_statuses}

    unavailable_count_by_worker = {}
    if status_lockins:
        for lock in status_lockins:
            p = _norm_id(lock["person_id"])
            status = _norm_id(lock["status"])
            d = lock["date"].date() if isinstance(lock["date"], datetime) else lock["date"]
            if d in elig_days_all and status in unavailable_norm:
                unavailable_count_by_worker[p] = unavailable_count_by_worker.get(p, 0) + 1

    def make_step_weights(ub_steps: int, base_weight: int) -> list[int]:
        if ub_steps <= 0:
            return []
        if penalty_shape == "square":
            # 1,3,5,...  (sum of first k terms = k^2)
            w = [2*i - 1 for i in range(1, ub_steps+1)]
        elif penalty_shape == "geometric":
            w = [int(geometric_base**(i-1)) for i in range(1, ub_steps+1)]
        else:
            raise ValueError("penalty_shape must be 'square' or 'geometric'")
        return [base_weight * wi for wi in w]

    terms : list[tuple[str, int, cp_model.IntVar]] = []

    for p in P:
        # Current period work (variable)
        tw = model.NewIntVar(0, ub_total, f"TOTAL_WORK_{p}")
        model.Add(tw == sum(X[(p, d, s)] for d in elig_days_all for s in S if s in WORK))

        # Historical work (constant - not a variable)
        hist_count = hist_counts.get(p, 0)

        # Total work = historical + current
        # We create an IntVar for total to use in constraints
        # Upper bound is historical + max possible in current period
        total_ub = ub_total + hist_count
        total_work = model.NewIntVar(hist_count, total_ub, f"COMBINED_TOTAL_WORK_{p}")
        model.Add(total_work == hist_count + tw)

        # Adjust target for unavailable days (proportional to weekly target)
        # If someone works 1 day/week and takes 5 days off, they lose 1 shift from expectation
        # If someone works 4 days/week and takes 5 days off, they lose 4 shifts from expectation
        weekly_tgt = resolve_weekly_target_for_p(p)
        # Combine current period unavailable days with historical unavailable days
        hist_unavail = historical_unavailable_days_by_worker.get(p, 0) if historical_unavailable_days_by_worker else 0
        unavail_days = unavailable_count_by_worker.get(p, 0) + hist_unavail
        target_adjustment = (unavail_days * weekly_tgt) // 5  # proportional adjustment

        # Total weeks for this worker (historical + current)
        hist_weeks = hist_weeks_by_worker.get(p, 0)
        total_weeks_p = num_weeks + hist_weeks

        # Target total uses TOTAL weeks (historical + current)
        target_total = weekly_tgt * total_weeks_p - target_adjustment

        pos = model.NewIntVar(0, total_ub, f"HZ_POS_{p}")
        neg = model.NewIntVar(0, total_ub, f"HZ_NEG_{p}")
        model.Add(total_work - int(target_total) == pos - neg)

        abs_dev = model.NewIntVar(0, total_ub, f"HZ_ABS_{p}")
        model.Add(abs_dev == pos + neg)

        # excess = max(0, abs_dev - big_threshold)
        excess = model.NewIntVar(0, total_ub, f"HZ_EXCESS_{p}")
        model.Add(excess >= 0)
        model.Add(excess >= abs_dev - int(big_threshold))

        # Optional linear |dev| piece (keep small or 0 if you want purely convex)
        if weight_abs:
            terms.append((f"hz_abs:{p}", int(weight_abs), abs_dev))

        # Convex (super-linear) steps on 'excess'
        # We'll build z[1..ex_ub] with sum z = excess and z[k] >= z[k+1]
        ex_ub = total_ub
        if ex_ub > 0:
            z = [model.NewBoolVar(f"HZ_ZEX_{p}_{k}") for k in range(1, ex_ub+1)]
            model.Add(sum(z) == excess)
            for k in range(1, ex_ub):
                model.Add(z[k-1] >= z[k])

            step_weights = make_step_weights(ex_ub, int(weight_excess_by_person[p]))
            # Note: we label each step term with its index for logging clarity
            for k in range(ex_ub):
                # terms: (label, coefficient, BoolVar)
                terms.append((f"hz_ex_step:{p}:{k+1}", int(step_weights[k]), z[k]))

        if horizon_debug is not None:
            horizon_debug[p] = {
                "total_work": total_work,
                "historical_work": hist_count,
                "current_period_work": tw,
                "pos": pos,
                "neg": neg,
                "abs_dev": abs_dev,
                "excess": excess,
                "target_total": target_total,
                "target_week": weekly_tgt,
                "num_weeks_current": num_weeks,
                "num_weeks_historical": hist_weeks,
                "num_weeks_total": total_weeks_p,
                "unavailable_days": unavail_days,
                "target_adjustment": target_adjustment,
            }

    return terms

def add_paid_share_balance_convex(
    model,
    X, P, D, S,
    *,
    work_shift_ids,                          # list[str] which shifts count as "work"
    paid_shift_ids=None,                     # optional list[str]; if None, derive from shift_pay>0 via paid_shift_pay_map
    paid_shift_pay_map=None,                 # optional dict label->pay (used only to infer paid set if paid_shift_ids is None)
    target_pct_by_person=None,               # dict: worker (norm or human) -> target fraction (0..1) or percent (0..100)
    include_weekends=False,
    week_start=0,                            # Monday=0..Sunday=6
    # convex penalty controls (square-like):
    big_threshold_counts=0,                  # free window in *counts* (0 or 1 are common)
    base_weight_by_person=None,              # dict or scalar: multiplies the convex steps
    penalty_shape="square",                  # "square" or "geometric"
    geometric_base=2,                        # for "geometric"
    paid_share_debug: dict | None = None     # optional capture of debug vars per person
) -> list[tuple[str, int, cp_model.IntVar]]:
    """
    Penalize horizon-wide deviation between the actual paid-shift COUNT and the expected count
    implied by a target percentage: EP = floor(target_pct * TW).

    Variables per p:
      TW[p]     = total # of WORK shifts
      PAID[p]   = total # of PAID shifts (subset of WORK)
      EP[p]     = floor(T_pct[p] * TW[p]) using linear bounds:
                  SCALE*EP <= T_pct_scaled*TW <= SCALE*EP + (SCALE-1)
      DEV[p]    = |PAID - EP|
      EX[p]     = max(0, DEV - big_threshold_counts)
      z[k]      = convex step binaries with sum(z)=EX and z[k] >= z[k+1]
    Cost:
      sum_k ( step_weight[k] * z[k] )  where step_weight grows (square or geometric)
    """
    SCALE = 100  # integer scale for percentages

    def norm_target_to_scaled(val) -> int:
        # Accept 0..1 or 0..100
        if isinstance(val, (int, float)):
            if 0 <= val <= 1:
                return int(round(val * SCALE))
            if 0 <= val <= 100:
                return int(round(val))
        raise ValueError("target_pct values must be in [0,1] or [0,100]")

    # Resolve paid set
    WORK = { _norm_id(s) for s in work_shift_ids }
    if paid_shift_ids is None:
        if not paid_shift_pay_map:
            raise ValueError("Provide either paid_shift_ids or paid_shift_pay_map to infer paid shifts.")
        paid_shift_ids = [k for k, v in paid_shift_pay_map.items() if v and v > 0]
    PAIDSET = { _norm_id(s) for s in paid_shift_ids } & WORK

    weeks = partition_into_weeks(D, week_start, include_weekends)
    if not weeks:
        return []

    elig_days_all = [d for wk in weeks for d in wk]
    ub_total = len(elig_days_all)
    if ub_total == 0:
        return []

    # Resolve per-person targets and base weights
    target_pct_by_person = target_pct_by_person or {p: 0 for p in P}
    T_scaled = {}
    for p in P:
        # accept normalized or human keys
        if p in target_pct_by_person:
            T_scaled[p] = norm_target_to_scaled(target_pct_by_person[p])
        else:
            found = None
            for k, v in target_pct_by_person.items():
                if _norm_id(k) == p:
                    found = v; break
            T_scaled[p] = norm_target_to_scaled(found if found is not None else 0)

    if base_weight_by_person is None:
        base_weight_by_person = {p: 1 for p in P}
    elif isinstance(base_weight_by_person, (int, float)):
        base_weight_by_person = {p: int(base_weight_by_person) for p in P}
    else:
        base_weight_by_person = {p: int(base_weight_by_person.get(p, 1)) for p in P}

    def make_step_weights(ub: int, base_w: int) -> list[int]:
        if ub <= 0:
            return []
        if penalty_shape == "square":
            # 1,3,5,... so sum of first k steps = k^2
            steps = [2*i - 1 for i in range(1, ub+1)]
        elif penalty_shape == "geometric":
            steps = [int(geometric_base**(i-1)) for i in range(1, ub+1)]
        else:
            raise ValueError("penalty_shape must be 'square' or 'geometric'")
        return [base_w * s for s in steps]

    terms : list[tuple[str, int, cp_model.IntVar]] = []

    for p in P:
        # Total work and paid counts
        TW = model.NewIntVar(0, ub_total, f"PAID_TW_{p}")
        model.Add(TW == sum(X[(p, d, s)] for d in elig_days_all for s in S if s in WORK))

        PAID = model.NewIntVar(0, ub_total, f"PAID_CNT_{p}")
        model.Add(PAID == sum(X[(p, d, s)] for d in elig_days_all for s in S if s in PAIDSET))

        # EP = floor( T_scaled[p] * TW / SCALE )
        EP = model.NewIntVar(0, ub_total, f"PAID_EP_{p}")
        model.Add(SCALE * EP <= T_scaled[p] * TW)
        model.Add(T_scaled[p] * TW <= SCALE * EP + (SCALE - 1))

        # DEV = |PAID - EP|
        pos = model.NewIntVar(0, ub_total, f"PAID_POS_{p}")
        neg = model.NewIntVar(0, ub_total, f"PAID_NEG_{p}")
        model.Add(PAID - EP == pos - neg)
        DEV = model.NewIntVar(0, ub_total, f"PAID_DEV_{p}")
        model.Add(DEV == pos + neg)

        # EX = max(0, DEV - big_threshold_counts)
        EX = model.NewIntVar(0, ub_total, f"PAID_EX_{p}")
        model.Add(EX >= 0)
        model.Add(EX >= DEV - int(big_threshold_counts))

        # Convex penalty on EX via prefix-step binaries
        ex_ub = ub_total  # at most all worked shifts could be off in worst case
        if ex_ub > 0:
            z = [model.NewBoolVar(f"PAID_Z_{p}_{k}") for k in range(1, ex_ub+1)]
            model.Add(sum(z) == EX)
            for k in range(1, ex_ub):
                model.Add(z[k-1] >= z[k])

            step_w = make_step_weights(ex_ub, base_weight_by_person[p])
            for k in range(ex_ub):
                terms.append((f"paid_share:{p}:{k+1}", int(step_w[k]), z[k]))

        if paid_share_debug is not None:
            paid_share_debug[p] = {
                "TW": TW,
                "PAID": PAID,
                "EP": EP,
                "DEV": DEV,
                "EX": EX,
                "T_scaled_pct": T_scaled[p],  # 0..100
            }

    return terms


def add_monthly_rvu_floor_penalty(
    model,
    X,
    P,
    D,
    S,
    *,
    period_start: date,
    period_end: date,
    avg_rvu_by_shift: dict,
    ftes: dict,                                  # {worker_id: fte}  OR  {worker_id: {monthKey: fte}}
                                                  # Pass a flat float for constant FTE, or a
                                                  # dict-of-monthKey-to-float for per-month FTE
                                                  # (use this when attendings have FTE changes
                                                  # mid-period, e.g., via dashboard fte_history).
    annual_65_rvu: int = 10179,
    target_fraction_by_worker: dict | None = None,  # {worker_id: 0..1}; default 1.0; 0 → no constraint
    excluded: set | None = None,                 # legacy alias for fraction=0
    hard: bool = True,                           # hard constraint by default
    penalty_weight: int = 1000,                  # used when hard=False; default per-worker weight if no override
    penalty_weight_by_worker: dict | None = None,  # {worker_id: int} overrides penalty_weight for specific workers
    constant_rvu_by_worker_month: dict | None = None,  # {(p_norm, "YYYY-MM"): float} known-constant RVU offset (e.g., call)
    bonus_rvu_per_workday_shift: dict | None = None,   # {shift_name: extra_rvu} added only on non-holiday weekdays
    holidays: set | None = None,                 # set of date objects to exclude from workday-bonus application
    rvu_debug: dict | None = None,
):
    """Force or steer each worker's projected wRVUs in EVERY scheduled month to clear
    `target_fraction × annual_65 × FTE × 1/12`.

    Every month in the schedule contributes to some future qualification window, so the
    solver should keep each non-excluded worker above the monthly threshold every month
    rather than just clearing a 2- or 3-month aggregate.

    Projected month RVUs = sum_{day in month within [period_start, period_end]} sum_s X[p,d,s] * AVG_RVU[s].
    Monthly target        = annual_65_rvu * fte * 1/12 * target_fraction[p].

    With hard=True (default): adds `month_proj >= month_target` for every (worker, month).
    With hard=False: adds a linear penalty `penalty_weight * shortfall` per (worker, month).

    Doesn't model moonlight — only work shifts. Matches the user's intent: stay above the
    threshold every month from work alone.
    """
    excluded = excluded or set()
    target_fraction_by_worker = target_fraction_by_worker or {}
    constant_rvu_by_worker_month = constant_rvu_by_worker_month or {}
    bonus_rvu_per_workday_shift = bonus_rvu_per_workday_shift or {}
    penalty_weight_by_worker = penalty_weight_by_worker or {}
    holidays = holidays or set()
    if rvu_debug is None:
        rvu_debug = {}

    # Enumerate calendar months in [period_start, period_end]
    months = []
    y, m = period_start.year, period_start.month
    while True:
        first = date(y, m, 1)
        if first > period_end:
            break
        # First of next month
        ny = y + (1 if m == 12 else 0)
        nm = 1 if m == 12 else m + 1
        next_first = date(ny, nm, 1)
        # Intersection with [period_start, period_end]
        mo_start = max(first, period_start)
        mo_end_excl = min(next_first, date(period_end.year, period_end.month, period_end.day))
        # Add one day to make end inclusive→exclusive cleanly
        from datetime import timedelta
        mo_end_excl = min(next_first, period_end + timedelta(days=1))
        months.append((f"{y:04d}-{m:02d}", mo_start, mo_end_excl))
        y, m = ny, nm

    if not months:
        return []

    # Scale RVUs to integers (CP-SAT requires integer coefficients)
    SCALE = 10  # 1 decimal place precision
    scaled_avg = {s: int(round(avg_rvu_by_shift.get(s, 0) * SCALE)) for s in S}
    # Per-shift workday bonus (e.g., +32 IA pre-shift moonlight on non-holiday weekdays).
    # Resolve dict to normalized-shift keys so we match S correctly.
    scaled_bonus_workday = {
        s: int(round(bonus_rvu_per_workday_shift.get(s, 0) * SCALE)) for s in S
    }

    terms = []
    for p in P:
        if p in excluded:
            continue
        frac = target_fraction_by_worker.get(p, 1.0)
        if frac <= 0:
            continue
        # FTE can be a float (constant) or a dict-of-monthKey-to-float (per-month). Helper
        # picks the right value for a given month key. Falls back to 1.0 if neither shape
        # has a usable value.
        fte_entry = ftes.get(p, 1.0)
        def _fte_for_month(monthKey):
            if isinstance(fte_entry, dict):
                # Use the entry for the exact month; if missing, fall back to any value in the
                # dict (typically all months are present).
                return float(fte_entry.get(monthKey, next(iter(fte_entry.values()), 1.0)))
            return float(fte_entry)

        # Representative FTE for the debug header (use the first month's FTE)
        rvu_debug[p] = {
            "scale": SCALE,
            "fte": _fte_for_month(months[0][0]) if months else 1.0,
            "hard": hard,
            "months": [],   # list of {key, projected_var, shortfall_var_or_None, n_days}
        }

        for monthKey, mo_start, mo_end_excl in months:
            month_days = [d for d in D if mo_start <= d < mo_end_excl]
            if not month_days:
                continue

            # Per-month FTE → per-month target. Lets the solver respect mid-period FTE changes
            # (e.g., someone goes from 1.0 to 0.5 starting Jan).
            fte_this_month = _fte_for_month(monthKey)
            target_scaled = int(round(annual_65_rvu * fte_this_month * (1.0 / 12.0) * frac * SCALE))
            if target_scaled <= 0:
                continue

            # Known-constant RVU offset for this (worker, month) — typically projected call RVUs
            # from already-locked weekend/holiday call assignments. Folds into the threshold so
            # the solver doesn't double-count or ignore call contribution.
            const_rvu = constant_rvu_by_worker_month.get((p, monthKey), 0.0)
            const_scaled = int(round(const_rvu * SCALE))

            # Per-(day, shift) RVU coefficient: base shift average + workday bonus (if non-holiday).
            # Applied per day so bonuses don't accidentally credit holiday IA assignments where the
            # overnight pre-shift moonlight pattern doesn't apply.
            def coeff(d_, s_):
                base = scaled_avg.get(s_, 0)
                if d_ not in holidays:
                    base += scaled_bonus_workday.get(s_, 0)
                return base

            max_per_day = max(scaled_avg.values()) + (max(scaled_bonus_workday.values()) if scaled_bonus_workday else 0)
            ub = max_per_day * len(month_days) + target_scaled
            proj_work = model.NewIntVar(0, ub, f"MO_RVU_WORK_{p}_{monthKey}")
            model.Add(
                proj_work == sum(
                    X[(p, d, s)] * coeff(d, s)
                    for d in month_days for s in S
                    if coeff(d, s) > 0
                )
            )

            # Effective target after crediting the known call contribution. If call already
            # exceeds the threshold, eff_target_scaled goes ≤ 0 and the constraint becomes a no-op.
            eff_target_scaled = target_scaled - const_scaled

            if hard:
                if eff_target_scaled > 0:
                    model.Add(proj_work >= eff_target_scaled)
                shortfall = None
            else:
                if eff_target_scaled > 0:
                    shortfall = model.NewIntVar(0, eff_target_scaled, f"MO_SHORT_{p}_{monthKey}")
                    model.Add(shortfall >= eff_target_scaled - proj_work)
                    pw = int(penalty_weight_by_worker.get(p, penalty_weight))
                    terms.append((f"monthly_rvu_floor:{p}:{monthKey}", pw, shortfall))
                else:
                    shortfall = None

            rvu_debug[p]["months"].append({
                "key": monthKey,
                "projected": proj_work,
                "shortfall": shortfall,
                "n_days": len(month_days),
                "const_rvu": const_rvu,
                "target_scaled": target_scaled,  # per-month, may vary if FTE changes
                "fte": fte_this_month,
                "eff_target_scaled": eff_target_scaled,
            })
    return terms


def add_monthly_flex_nights_floor_penalty(
    model,
    X, P, D, S,
    *,
    period_start: date,
    period_end: date,
    monthly_floor_by_person: dict,            # {norm_id: int} per-attending monthly minimum
    flex_label: str = "Flex/Nights",
    full_month_threshold_days: int = 25,      # months with fewer covered days are skipped
    penalty_weight: int = 99999,              # near-hard
    floor_debug: dict | None = None,
) -> List[Tuple[str, int, "cp_model.IntVar"]]:
    """Per (worker, month) floor on Flex/Nights count. Implemented as a soft penalty so
    truly infeasible months (heavy vacation) don't break the solve, but the high weight
    makes the solver work hard to satisfy. Skips partial-coverage months at the edges
    of the solve window (any month with < `full_month_threshold_days` days in scope).
    """
    from datetime import date as _d
    if floor_debug is None:
        floor_debug = {}

    fn_norm = _norm_id(flex_label)
    if fn_norm not in S:
        return []

    # Enumerate months and their coverage within [period_start, period_end].
    months = []
    y, m = period_start.year, period_start.month
    while True:
        first = _d(y, m, 1)
        if first > period_end:
            break
        ny = y + (1 if m == 12 else 0)
        nm = 1 if m == 12 else m + 1
        next_first = _d(ny, nm, 1)
        days_covered = sum(1 for d in D if first <= d < next_first)
        months.append((f"{y:04d}-{m:02d}", first, next_first, days_covered))
        y, m = ny, nm

    terms = []
    for p in P:
        floor = int(monthly_floor_by_person.get(p, 0))
        if floor <= 0:
            continue
        floor_debug[p] = {"floor": floor, "months": []}
        for monthKey, mo_start, mo_end_excl, days_covered in months:
            if days_covered < full_month_threshold_days:
                floor_debug[p]["months"].append({"key": monthKey, "skipped": True, "days_covered": days_covered})
                continue
            month_days = [d for d in D if mo_start <= d < mo_end_excl]
            fn_vars = [X[(p, d, fn_norm)] for d in month_days if (p, d, fn_norm) in X]
            if not fn_vars:
                continue
            shortfall = model.NewIntVar(0, floor, f"fn_floor_short_{p}_{monthKey}")
            # actual + shortfall >= floor  →  shortfall = max(0, floor - actual)
            model.Add(sum(fn_vars) + shortfall >= floor)
            terms.append((f"fn_monthly_floor:{p}:{monthKey}", int(penalty_weight), shortfall))
            floor_debug[p]["months"].append({
                "key": monthKey, "floor": floor, "shortfall_var": shortfall, "days_covered": days_covered,
            })
    return terms


# Legacy alias to avoid breaking older callers — points at the new monthly floor.
# Old call signature passed only period_start; we approximate period_end as period_start + ~62 days
# for compatibility. New callers should use add_monthly_rvu_floor_penalty directly.
def add_m1m2_rvu_floor_penalty(*args, **kwargs):
    raise DeprecationWarning(
        "add_m1m2_rvu_floor_penalty is removed; call add_monthly_rvu_floor_penalty(period_start=, period_end=, ...) instead."
    )


def print_monthly_rvu_debug(rvu_debug: dict, solver, *, sort_by="worst_shortfall"):
    """Pretty-print per-month RVU floor results per worker."""
    if not rvu_debug:
        print("[monthly] No monthly RVU vars recorded.")
        return
    rows = []
    for p, d in rvu_debug.items():
        scale = d["scale"]
        per_month = []
        worst_short = 0.0
        total_target = 0.0
        for m in d.get("months", []):
            month_target = m.get("target_scaled", 0) / scale
            total_target += month_target
            proj_work = solver.Value(m["projected"]) / scale
            const_rvu = m.get("const_rvu", 0.0)
            proj_total = proj_work + const_rvu
            short = (solver.Value(m["shortfall"]) / scale) if m.get("shortfall") is not None else max(0, month_target - proj_total)
            per_month.append({
                "key": m["key"], "projected": proj_total, "work_rvu": proj_work,
                "call_rvu": const_rvu, "shortfall": short, "days": m["n_days"],
                "target": month_target, "fte": m.get("fte"),
            })
            if short > worst_short:
                worst_short = short
        rows.append({"worker": p, "fte": d["fte"], "target": total_target, "months": per_month,
                     "worst_shortfall": worst_short, "hard": d.get("hard", False)})
    if sort_by == "worst_shortfall":
        rows.sort(key=lambda r: -r["worst_shortfall"])
    else:
        rows.sort(key=lambda r: r["worker"])
    print()
    hard_label = " (hard floor)" if any(r["hard"] for r in rows) else ""
    print(f"=== Monthly RVU Floor{hard_label} (target = 65th × FTE × 1/12 per month) ===")
    for r in rows:
        print(f"{r['worker']:<16} fte={r['fte']:>4.2f} monthly_target={r['target']:>6.1f} worst_short={r['worst_shortfall']:>5.1f}")
        for m in r["months"]:
            mark = " " if m['shortfall'] == 0 else "!"
            print(
                f"  {mark} {m['key']}: proj={m['projected']:>6.1f} (work={m['work_rvu']:>6.1f}+call={m['call_rvu']:>5.1f})"
                f" short={m['shortfall']:>5.1f} ({m['days']} weekdays)"
            )


# Backward-compat alias for older callers — same data shape isn't compatible, prefer the new name.
print_m1m2_rvu_debug = print_monthly_rvu_debug


def print_horizon_debug(hz_vars: dict, solver: cp_model.CpSolver, *, sort_by="abs_dev"):
    """
    Pretty-print the horizon variables per worker:
      total_work, historical_work, current_period_work, target_total, pos, neg, abs_dev, excess,
      unavailable_days, target_adjustment
    sort_by ∈ {"abs_dev","excess","total_work","name"}
    """
    if not hz_vars:
        print("[horizon] No horizon vars recorded.")
        return

    rows = []
    for p, d in hz_vars.items():
        total_work = solver.Value(d["total_work"])
        hist_work = d.get("historical_work", 0)
        current_work = solver.Value(d["current_period_work"])
        pos    = solver.Value(d["pos"])
        neg    = solver.Value(d["neg"])
        absdev = solver.Value(d["abs_dev"])
        excess = solver.Value(d["excess"])
        tgtW   = d["target_week"]
        nW_current = d.get("num_weeks_current", d.get("num_weeks", 0))
        nW_hist = d.get("num_weeks_historical", 0)
        nW_total = d.get("num_weeks_total", nW_current)
        tgtT   = d["target_total"]
        unavail = d.get("unavailable_days", 0)
        adj    = d.get("target_adjustment", 0)
        rows.append({
            "worker": p,
            "total_work": total_work,
            "historical_work": hist_work,
            "current_work": current_work,
            "target_week": tgtW,
            "num_weeks_total": nW_total,
            "num_weeks_hist": nW_hist,
            "target_total": tgtT,
            "pos": pos,
            "neg": neg,
            "abs_dev": absdev,
            "excess": excess,
            "unavail": unavail,
            "adj": adj,
        })

    key = {
        "abs_dev":    lambda r: (-r["abs_dev"], r["worker"]),
        "excess":     lambda r: (-r["excess"], r["worker"]),
        "total_work": lambda r: (-r["total_work"], r["worker"]),
        "name":       lambda r: (r["worker"],),
    }.get(sort_by, lambda r: (-r["abs_dev"], r["worker"]))

    rows.sort(key=key)

    # print header
    print("\n=== Horizon totals (per worker) ===")
    print(f"{'worker':<16} {'total':>4}  {'hist':>4} {'curr':>4}  {'tgt_w':>5} {'wks':>4} {'wks_h':>4} {'tgt_T':>6}  {'unavail':>7} {'adj':>3}  {'pos':>4} {'neg':>4} {'|dev|':>5} {'excess':>6}")
    for r in rows:
        print(f"{r['worker']:<16} {r['total_work']:>4}  {r['historical_work']:>4} {r['current_work']:>4}  "
              f"{r['target_week']:>5} {r['num_weeks_total']:>4} {r['num_weeks_hist']:>4} {r['target_total']:>6}  "
              f"{r['unavail']:>7} {r['adj']:>3}  {r['pos']:>4} {r['neg']:>4} {r['abs_dev']:>5} {r['excess']:>6}")


from gspread_formatting import CellFormat, TextFormat, format_cell_range

def set_global_font(ws, font_family: str = "Arial", font_size: int = 10):
    """
    Apply a uniform font and size to the entire worksheet.
    """
    fmt = CellFormat(
        textFormat=TextFormat(fontFamily=font_family, fontSize=font_size)
    )
    # Apply to all used cells; 'A:Z' assumes your data fits within these columns
    # If you want to be dynamic, use ws.col_count and ws.row_count.
    range_all = f"A1:{chr(64 + ws.col_count)}{ws.row_count}"
    format_cell_range(ws, range_all, fmt)

def mark_weekends_and_holidays(
    df: pd.DataFrame,
    *,
    date_col: str = "Date",
    worker_cols: list[str],
    holidays: set[date] | None = None,
    frozen_cells: set[tuple[date, str]] | None = None,
    weekend_label: str = "Weekend",
    holiday_label: str = "Holiday",
) -> pd.DataFrame:
    """
    For any row whose date is a weekend (Sat/Sun) or in `holidays`,
    write 'Weekend' or 'Holiday' into all worker columns (unless the cell
    was frozen/preserved earlier). Returns a modified copy.
    """
    out = df.copy()
    holidays = holidays or set()
    frozen_cells = frozen_cells or set()

    # Build quick date -> row indices map
    date_keys = out[date_col].apply(_coerce_date)
    rows_by_date = {}
    for idx, d in date_keys.items():
        if d is not None:
            rows_by_date.setdefault(d, []).append(idx)

    for d, idxs in rows_by_date.items():
        if d in holidays:
            label = holiday_label
        elif d.weekday() in (5, 6):  # Sat=5, Sun=6
            label = weekend_label
        else:
            continue

        for w in worker_cols:
            if w not in out.columns:
                continue
            # Don't overwrite cells the user already typed something unrecognized into
            if (d, w) in frozen_cells:
                continue
            for idx in idxs:
                out.at[idx, w] = label

    return out

def preflight_feasibility_checks(
    *, P, D, S,
    shift_lockins, status_lockins, req,
    treat_status_as_unavailable=("Academic","Conference","Vacation","Blocked","Sick")
):
    """
    Raises ValueError with a helpful message if we detect contradictions:
      1) Same person-day locked to multiple shifts.
      2) Same person-day locked to a shift AND a status (unavailable).
      3) More shift-lockins for a given (date, shift) than the required coverage.
      4) Daily required shifts exceed the number of available people.
    """
    # normalize helpers
    def norm_worker(w): return _norm_id(w)
    def norm_shift(s):  return _norm_id(s)
    def norm_status(t): return _norm_id(t)

    UNAV = {norm_status(t) for t in treat_status_as_unavailable}

    # Index lockins
    shift_by_pd = {}         # (p,d) -> shift
    status_by_pd = {}        # (p,d) -> status
    count_shift_lockins = {} # (d,s) -> count

    for L in shift_lockins:
        p = norm_worker(L["person_id"]); d = L["date"]; s = norm_shift(L["shift_id"])
        if (p,d) in shift_by_pd:
            raise ValueError(f"Preflight: {p} has MULTIPLE shift lockins on {d} ({shift_by_pd[(p,d)]} and {s}).")
        shift_by_pd[(p,d)] = s
        count_shift_lockins[(d,s)] = count_shift_lockins.get((d,s), 0) + 1

    for L in status_lockins:
        p = norm_worker(L["person_id"]); d = L["date"]; t = norm_status(L["status"])
        if (p,d) in status_by_pd:
            raise ValueError(f"Preflight: {p} has MULTIPLE status lockins on {d} ({status_by_pd[(p,d)]} and {t}).")
        status_by_pd[(p,d)] = t

    # 1) shift+status on same person-day?
    both = [(p,d,shift_by_pd[(p,d)], status_by_pd[(p,d)])
            for (p,d) in shift_by_pd.keys() & status_by_pd.keys()]
    if both:
        lines = []
        for p,d,s,t in both:
            lines.append(f"  - {p} {d}: shift={s} conflicts with status={t}")
        raise ValueError("Preflight: person-day(s) locked to a shift AND to a status:\n"+"\n".join(lines))

    # 2) too many shift lockins vs coverage?
    for (d,s), c in count_shift_lockins.items():
        required = int(req.get((d,s), 0))
        if c > required:
            raise ValueError(f"Preflight: {d} shift {s} has {c} lockins but required={required}.")

    # 3) day capacity check: how many people are available vs total required?
    for d in D:
        # people unavailable due to status lockin (Academic/Conference/Vacation/Blocked)
        unavailable = { p for (p,dd), t in status_by_pd.items() if dd == d and t in UNAV }
        # (If someone is shift-locked, they are available that day by definition.)
        available_count = len(P) - len(unavailable)

        total_required = sum(int(req.get((d,s),0)) for s in S)
        if total_required > available_count:
            # Print debug info
            print(f"\n[DEBUG] Date: {d}")
            print(f"[DEBUG] Total workers (P): {len(P)}")
            print(f"[DEBUG] Unavailable (status lockins): {unavailable}")
            print(f"[DEBUG] Shift lockins for this date:")
            for (p, dd), s in shift_by_pd.items():
                if dd == d:
                    print(f"[DEBUG]   {p} -> {s}")
            raise ValueError(
                f"Preflight: {d} requires {total_required} shift(s) but only {available_count} person(s) are available.\n"
                f"  Tip: reduce requirements on that date, or remove some status/Blocked lockins."
            )
        

# ---- Objective breakdown (label -> term value) ----
def print_objective_breakdown(terms: List[ObjTerm], solver: cp_model.CpSolver, top_n: int = 40):
    rows = []
    total = 0
    for label, w, var in terms:
        v = solver.Value(var)
        cost = w * v
        if cost != 0:
            rows.append((cost, w, v, label))
        total += cost

    rows.sort(reverse=True)  # highest cost first

    print("\n=== Objective Cost Breakdown ===")
    print(f"Total objective: {total}")
    print(f"Non-zero contributors: {len(rows)}")
    print(f"Top {min(top_n, len(rows))} terms:")
    for i, (cost, w, v, label) in enumerate(rows[:top_n], 1):
        print(f"{i:>2}. {label:<35}  weight={w:<6} value={v:<3}  cost={cost}")

    # Optional: aggregate by category prefix before first ':'
    agg = {}
    for cost, w, v, label in rows:
        key = label.split(':', 1)[0] if ':' in label else label
        agg[key] = agg.get(key, 0) + cost
    print("\nBy category:")
    for k, c in sorted(agg.items(), key=lambda x: -x[1]):
        print(f"  {k:<12}  {c}")


def print_paid_share_debug(PAID_DEBUG: dict, solver: cp_model.CpSolver, *, sort_by="DEV"):
    """
    Pretty-print the paid-share stats per worker:
      TW, PAID, EP, DEV, EX, target_pct

    sort_by ∈ {"DEV","EX","PAID","worker"}
    """
    if not PAID_DEBUG:
        print("[paid_share] No debug vars recorded.")
        return

    rows = []
    for p, d in PAID_DEBUG.items():
        TW     = solver.Value(d["TW"])
        PAID   = solver.Value(d["PAID"])
        EP     = solver.Value(d["EP"])
        DEV    = solver.Value(d["DEV"])
        EX     = solver.Value(d["EX"])
        T_pct  = d["T_scaled_pct"] / 100.0

        rows.append({
            "worker": p,
            "TW": TW,
            "PAID": PAID,
            "EP": EP,
            "DEV": DEV,
            "EX": EX,
            "target_pct": T_pct,
        })

    keymap = {
        "DEV":    lambda r: (-r["DEV"], r["worker"]),
        "EX":     lambda r: (-r["EX"], r["worker"]),
        "PAID":   lambda r: (-r["PAID"], r["worker"]),
        "worker": lambda r: (r["worker"],),
    }
    rows.sort(key=keymap.get(sort_by, keymap["DEV"]))

    print("\n=== Paid-Shift Balance Debug ===")
    print(f"{'worker':<16} {'TW':>4} {'PAID':>5} {'EP':>5} {'DEV':>5} {'EX':>5}  {'target%':>8}")
    for r in rows:
        print(f"{r['worker']:<16} {r['TW']:>4} {r['PAID']:>5} {r['EP']:>5} {r['DEV']:>5} {r['EX']:>5}  {r['target_pct']*100:>7.2f}")

def build_call_assignment_variables(
    model: cp_model.CpModel,
    workers: list[str],
    call_dates: list[date],
    *,
    availability_start_by_worker: dict[str, date] | None = None,
    prefix: str = "call",
):
    P = tuple(_norm_id(w) for w in workers)
    D = tuple(sorted(call_dates))
    availability = {}
    if availability_start_by_worker:
        for label, start in availability_start_by_worker.items():
            availability[_norm_id(label)] = start
    C = {}
    allowed = {}
    for p in P:
        start_limit = availability.get(p)
        for d in D:
            var = model.NewBoolVar(f"{prefix}_{p}_{_fmt_date(d)}")
            C[(p, d)] = var
            ok = (start_limit is None) or d >= start_limit
            allowed[(p, d)] = ok
            if not ok:
                model.Add(var == 0)
    return C, allowed

def apply_call_lockins(model: cp_model.CpModel, C: Dict[Tuple[str, date], cp_model.IntVar], call_lockins: List[Dict]):
    for item in call_lockins:
        p = _norm_id(item["person_id"])
        d = item["date"].date() if isinstance(item["date"], datetime) else item["date"]
        key = (p, d)
        if key not in C:
            raise ValueError(f"Call lock-in {item['person_id']} on {d} is outside the call horizon or not permitted.")
        model.Add(C[key] == 1)

def add_call_coverage_constraints(model, C, workers, call_dates):
    if not call_dates:
        return
    P = tuple(_norm_id(w) for w in workers)
    for d in sorted(call_dates):
        vars_for_day = [C[(p, d)] for p in P if (p, d) in C]
        if not vars_for_day:
            raise ValueError(f"No call variables for {d}.")
        model.Add(sum(vars_for_day) == 1)

def load_spreadsheet(sheet_name='NEURORAD SECTION MEGA SPREADSHEET', worksheet_name='25-26 Faculty Schedule'):
    """Open the Google Sheet and return (sheet_handle, DataFrame)."""
    sheet = open_sheet(sheet_name)
    ws = sheet.worksheet(worksheet_name)
    rows = ws.get_all_values()
    headers = [h.strip() if h.strip() else f"col_{i}" for i, h in enumerate(rows[0])]
    print(headers)
    df = pd.DataFrame(rows[1:], columns=headers)
    return sheet, df


def load_history(df, s, lookback_weeks=12):
    """Read historical shift counts, call sequence, status lockins, and per-worker week counts.

    Returns a dict with keys: counts, call_tail, unavailable_days, weeks_by_worker.
    """
    historical_start = s - timedelta(weeks=lookback_weeks)

    # Historical shift counts
    historical_counts = read_historical_shift_counts(
        df,
        date_col="Date",
        worker_cols=workers,
        shifts=shifts,
        work_shift_ids=shifts,
        cutoff_date=s,
        start_date=historical_start,
        include_weekends=False,
        start_dates_by_worker=CALL_AVAILABILITY_START,
    )
    print(f"Historical shift counts (before {s}):")
    for w in workers:
        print(f"  {w}: {historical_counts.get(_norm_id(w), 0)}")

    # Call history sequence
    history_sequence = read_call_history_sequence(
        df,
        date_col="Date",
        call_col=CALL_COLUMN,
        worker_names=workers,
        cutoff_date=s,
    )
    history_tail_norm = [entry["worker"] for entry in history_sequence]

    # Historical status lockins (vacation/conference/leave days)
    historical_dates = []
    current = historical_start
    while current < s:
        if current.weekday() < 5:
            historical_dates.append(current)
        current += timedelta(days=1)

    hist_shift_lockins, hist_status_lockins, _ = read_lockins_from_df(
        df,
        date_col="Date",
        worker_cols=workers,
        shifts=shifts,
        statuses=statuses,
        dates_in_scope=historical_dates,
    )

    # Count unavailable days per worker
    hist_unavailable_days = {_norm_id(w): 0 for w in workers}
    unavail_norms = {_norm_id(s) for s in ["Vacation", "Conference", "Blocked", "Sick"]}

    mclouth_norm = _norm_id("McLouth")
    print(f"\nDEBUG: McLouth historical status lockins:")
    for lock in hist_status_lockins:
        if _norm_id(lock["person_id"]) == mclouth_norm:
            print(f"  Date: {lock['date']}, Status: '{lock['status']}' (normalized: '{_norm_id(lock['status'])}')")

    for lock in hist_status_lockins:
        status_norm = _norm_id(lock["status"])
        if status_norm in unavail_norms:
            worker_norm = _norm_id(lock["person_id"])
            hist_unavailable_days[worker_norm] = hist_unavailable_days.get(worker_norm, 0) + 1

    # Per-worker historical weeks (adjusted for late start dates)
    historical_weeks_by_worker = {}
    for w in workers:
        w_norm = _norm_id(w)
        if w_norm in CALL_AVAILABILITY_START:
            start_date = CALL_AVAILABILITY_START[w_norm]
            if start_date >= s:
                historical_weeks_by_worker[w_norm] = 0
            else:
                weeks_worked = (s - start_date).days / 7
                historical_weeks_by_worker[w_norm] = min(int(weeks_worked), lookback_weeks)
        else:
            historical_weeks_by_worker[w_norm] = lookback_weeks

    print("Historical weeks per worker:")
    for w in workers:
        print(f"  {w}: {historical_weeks_by_worker[_norm_id(w)]}")

    return {
        "counts": historical_counts,
        "call_tail": history_tail_norm,
        "unavailable_days": hist_unavailable_days,
        "weeks_by_worker": historical_weeks_by_worker,
        "status_lockins": hist_status_lockins,
    }


def build_and_solve(df, s, e, weekdays, call_dates, history, *,
                    m1m2_hard=True, avg_rvu_by_shift=None, ftes_by_worker_month=None):
    """Build the CP-SAT model, add all constraints and penalties, solve, and return results.

    `m1m2_hard` controls whether the M1+M2 RVU floor is a hard constraint or a soft penalty.
    If hard and infeasible, this function automatically retries with hard=False so the caller
    can see which workers fell short — diagnostic output will show their shortfalls and the
    user can adjust M1M2_TARGET_FRACTION accordingly.

    Returns a dict with keys: solver, model, X, Z, P, D, S, T, obj_terms,
    shift_lockins, status_lockins, preserve_text, call_lockins, HZ_DEBUG, PAID_DEBUG, ...
    """
    historical_counts = history["counts"]
    hist_unavailable_days = history["unavailable_days"]
    historical_weeks_by_worker = history["weeks_by_worker"]
    hist_status_lockins = history["status_lockins"]

    print(f"Building schedule from: {s} → {e}")

    D = weekdays

    # Build variables
    model, X, P, D, S = build_assignment_model(workers, D, shifts)
    Z, T = add_status_variables(model, workers, D, statuses)

    shift_lockins, status_lockins, preserve_text = read_lockins_from_df(
        df,
        date_col="Date",
        worker_cols=workers,
        shifts=shifts,
        statuses=statuses,
        dates_in_scope=D
    )

    # Coverage: 1 person per shift per non-holiday weekday
    req = {
        (d, _norm_id(s)): (
            1 if (d.weekday() < 5 and d not in HOLIDAYS and s in ['InpatientA','InpatientB','OutpatientA','OutpatientB','Flex/Nights'])
            or (d.weekday() < 5 and d not in HOLIDAYS and s == 'Flex')
            else 0
        )
        for d in D
        for s in shifts
    }

    preflight_feasibility_checks(
        P=P, D=D, S=S,
        shift_lockins=shift_lockins,
        status_lockins=status_lockins,
        req=req
    )

    added = add_worker_shift_forbids(
        model, X, P, D, S,
        forbids_global=FORBIDS_GLOBAL,
    )
    print(f"Applied {added} worker/shift forbid constraints.")

    # Apply lock-ins
    apply_shift_lockins(model, X, shift_lockins)
    apply_status_lockins(model, Z, status_lockins)

    call_lockins = read_call_lockins_from_df(
        df,
        date_col="Date",
        call_col=CALL_COLUMN,
        worker_names=workers,
        call_dates=call_dates,
    )

    # Structural constraints
    freeze_nonacademic_unless_locked(model, Z, P, D, T, status_lockins)
    add_one_thing_per_day(model, X, Z, P, D, S, T)
    add_coverage_constraints(model, X, P, D, S, req)
    forbid_any_assignment_on_status(model, X, Z, P, D, S, forbidden_statuses=["Academic", "Conference", "Vacation", "Sick"])

    # Force unfilled slots to be Academic days
    for p in P:
        for d in D:
            if d.weekday() < 5:
                model.Add(sum(X[(p, d, s)] for s in S) + sum(Z[(p, d, t)] for t in T if t != ACA) + Z[(p, d, ACA)] == 1)

    # ---- Soft penalties (objective terms) ----
    obj_terms: List[ObjTerm] = []

    obj_terms += add_soft_weekly_targets_convex(
        model, X, P, D, S,
        work_shift_ids=shifts,
        weekly_target_by_person=WEEKLY_TARGETS,
        weight_over_by_person=W_OVER,
        weight_under_by_person=W_UNDER,
        include_weekends=False,
        week_start=0,
        penalty_shape="geometric",
        geometric_base=4,
    )

    HZ_DEBUG: dict = {}

    obj_terms += add_horizon_cumulative_balance_convex(
        model, X, P, D, S,
        work_shift_ids=shifts,
        weekly_target_by_worker=WEEKLY_TARGETS,
        status_lockins=status_lockins,
        unavailable_statuses=["Vacation", "Conference", "Blocked", "Sick"],
        historical_counts_by_worker=historical_counts,
        historical_weeks=historical_weeks_by_worker,
        historical_unavailable_days_by_worker=hist_unavailable_days,
        include_weekends=False,
        week_start=0,
        # Horizon disabled: under the new comp model, each scheduled month must clear its own
        # 65th-percentile RVU threshold (qualification windows roll forward continuously). Trying
        # to pay back historical utilization debt by overscheduling a worker pulls shifts away
        # from someone whose own monthly target is on the bubble. The monthly RVU floor +
        # weekly-target-over penalty already keep distribution sensible within each period.
        weight_abs=0,
        weight_excess_by_person={p: 0 for p in P},
        big_threshold=3,
        penalty_shape="square",
        horizon_debug=HZ_DEBUG,
    )

    obj_terms += add_weekly_shift_mix_penalty(
        model, X, P, D, S,
        work_shift_ids=shifts,
        include_weekends=False,
        week_start=0,
        weight_repeat=50,
        min_free_repeat=1,
        ignore_shifts=None
    )

    # New comp model splits the legacy "paid share" objective into two distinct fairness terms:
    #   - Flex/Nights distribution (the only fixed-pay shift now)
    #   - Inpatient A distribution (carries the IA-Moonlight $/RVU exposure)
    # Plus a new M1+M2 RVU floor that pushes each non-excluded worker toward the 65th-pctile
    # qualification threshold via work shifts alone.
    PAID_DEBUG = {}

    obj_terms += add_paid_share_balance_convex(
        model, X, P, D, S,
        work_shift_ids=shifts,
        paid_shift_ids=['Flex/Nights'],
        paid_shift_pay_map=None,
        target_pct_by_person=FLEX_NIGHTS_TARGETS,
        include_weekends=False,
        week_start=0,
        big_threshold_counts=1,
        base_weight_by_person=400,
        penalty_shape="square",
        geometric_base=2,
        paid_share_debug=PAID_DEBUG,
    )

    # Per-month minimum Flex/Nights count per eligible attending. Reduces month-to-month
    # variance — without this, a quarter-long solve could land an attending at 0 + 4 + 2
    # while still hitting the horizon-wide 12% target. Skips partial-coverage months at
    # the solve-window edges.
    FN_FLOOR_DEBUG = {}
    obj_terms += add_monthly_flex_nights_floor_penalty(
        model, X, P, D, S,
        period_start=s,
        period_end=e,
        monthly_floor_by_person=FLEX_NIGHTS_MONTHLY_FLOOR,
        penalty_weight=99999,
        floor_debug=FN_FLOOR_DEBUG,
    )

    INPATIENT_A_DEBUG = {}
    obj_terms += add_paid_share_balance_convex(
        model, X, P, D, S,
        work_shift_ids=shifts,
        paid_shift_ids=['InpatientA'],
        paid_shift_pay_map=None,
        target_pct_by_person=INPATIENT_A_TARGETS,
        include_weekends=False,
        week_start=0,
        big_threshold_counts=1,
        base_weight_by_person=300,
        penalty_shape="square",
        geometric_base=2,
        paid_share_debug=INPATIENT_A_DEBUG,
    )

    # Build per-worker FTE map from the WEEKLY_TARGETS (target shifts/week ÷ 4 ≈ fte for a
    # ~4-shift full-time week in this section). Used by the M1+M2 RVU floor target.
    # Per-worker FTE for the monthly RVU floor. Default: derive a single FTE from WEEKLY_TARGETS
    # (4 shifts/week ≈ 1.0 FTE). Caller can override with `ftes_by_worker_month` (a dict-of-dicts
    # keyed by normalized worker id → monthKey → fte) so per-month FTE changes from neuro_config
    # are respected.
    fte_map = {p: max(0.1, WEEKLY_TARGETS.get(p, 4) / 4.0) for p in P}
    if ftes_by_worker_month:
        # Override entries the caller provided; leave others as-is.
        for p, per_month in ftes_by_worker_month.items():
            fte_map[p] = per_month   # dict will trigger per-month lookup inside the penalty

    # Project per-(worker, month) call RVUs from the locked weekend/holiday call assignments.
    # Call lockins are FIXED at solve time (the call rotation is pre-set in the sheet), so each
    # call day contributes a known constant RVU offset to that worker's monthly total. Without
    # this, the floor would ignore call production and over-subscribe weekday work shifts to
    # workers who are already covering heavy call months.
    call_rvu_by_worker_month: dict = {}
    for lock in call_lockins:
        p_norm = _norm_id(lock["person_id"])
        d = lock["date"].date() if isinstance(lock["date"], datetime) else lock["date"]
        mk = f"{d.year:04d}-{d.month:02d}"
        call_rvu_by_worker_month[(p_norm, mk)] = (
            call_rvu_by_worker_month.get((p_norm, mk), 0.0) + AVG_RVU_PER_CALL_DAY
        )

    # Per-worker shortfall penalty weight. Floriolli and Chang carry a higher weight so when the
    # section is structurally short of total capacity (sum of monthly targets exceeds available
    # shifts after vacations), the solver puts the deficit on attendings who are over their floor
    # rather than on the two who already have a reduced 80% target. Without this asymmetry the
    # solver spreads shortfalls evenly, which means Floriolli/Chang fall short of their already-
    # lowered targets even when capacity could have been redirected to them from a worker above 100%.
    monthly_rvu_penalty_weights = {p: 20000 for p in P}
    monthly_rvu_penalty_weights[_norm_id("Floriolli")] = 60000
    monthly_rvu_penalty_weights[_norm_id("Chang")] = 60000

    # Use caller-provided averages (computed from recent retrospective data) when present;
    # otherwise fall back to the module-level baseline. Caller is expected to compute these
    # from the last 3 months of data via the dashboard's `get_shift_rvu_averages` query.
    effective_avg_rvu = avg_rvu_by_shift if avg_rvu_by_shift else AVG_RVU_BY_SHIFT
    print(f"[monthly-rvu-floor] using avg_rvu_by_shift = {effective_avg_rvu}")

    MONTHLY_RVU_DEBUG = {}
    obj_terms += add_monthly_rvu_floor_penalty(
        model, X, P, D, S,
        period_start=s,
        period_end=e,
        avg_rvu_by_shift=effective_avg_rvu,
        ftes=fte_map,
        annual_65_rvu=ANNUAL_65TH_RVU,
        target_fraction_by_worker=M1M2_TARGET_FRACTION,
        hard=m1m2_hard,
        penalty_weight=20000,
        penalty_weight_by_worker=monthly_rvu_penalty_weights,
        constant_rvu_by_worker_month=call_rvu_by_worker_month,
        # IA pre-shift moonlight (~22 overnight reads × 1.45 wRVU). Applied to non-holiday weekday
        # IA assignments only — matches the dashboard's INPATIENT_A_MOONLIGHT_RVU_PER_SHIFT. Without
        # this, the solver underestimates IA shifts by ~32 wRVU each, which makes Kuoy/Chu look
        # like they're running at the edge of their floor when they're actually well above.
        bonus_rvu_per_workday_shift={'InpatientA': 32},
        holidays=HOLIDAYS,
        rvu_debug=MONTHLY_RVU_DEBUG,
    )

    post_call_terms, post_call_count = add_monday_after_call_academic_penalty(
        model, Z, P=P, D=D, T=T, call_lockins=call_lockins, weight=99999,
    )
    obj_terms += post_call_terms
    print(f"Added {post_call_count} Monday-after-call Academic penalty terms.")

    flex_chain_terms, flex_chain_count = add_no_flex_followed_by_inpatientA_penalty(
        model, X, P=P, D=D, S=S, weight=99999,
    )
    obj_terms += flex_chain_terms
    print(f"Added {flex_chain_count} Flex→InpatientA penalty terms.")

    kuoy_terms, kuoy_count = add_kuoy_flex_penalties(
        model, X, P=P, D=D, S=S, call_lockins=call_lockins,
        thursday_weight=99999, friday_weight=99999,
    )
    obj_terms += kuoy_terms
    print(f"Added {kuoy_count} Kuoy Flex/Nights penalty terms.")

    tuesday_terms, tuesday_count = add_tuesday_shift_avoid_penalties(
        model, X, P=P, D=D, S=S, worker_names=["Sadigh", "Yep"], weight=1400,
    )
    obj_terms += tuesday_terms
    print(f"Added {tuesday_count} Tuesday shift-avoid penalty terms.")

    wed_aca_terms, wed_aca_count = add_wednesday_no_academic_penalty(
        model, Z, P=P, D=D, T=T, worker_names=["Floriolli"], weight=2000,
    )
    obj_terms += wed_aca_terms
    print(f"Added {wed_aca_count} Floriolli Wednesday no-Academic penalty terms.")

    # McLouth Thursday shift-avoid penalties
    mclouth_id = _norm_id("McLouth")
    thursday_weight = 1400
    banned_shifts = [_norm_id(sh) for sh in ["InpatientA", "InpatientB", "OutpatientA", "OutpatientB", "Flex/Nights"]]
    mclouth_thursday_count = 0
    for d in D:
        if d.weekday() == 3:
            # NOTE: rename loop var to `sh` — `s` is the start_date parameter and shadowing it
            # broke the recursive retry path (period_start.year on a leaked str).
            for sh in banned_shifts:
                if sh in S:
                    obj_terms.append((f"thu_no_shift:{mclouth_id}:{_fmt_date(d)}:{sh}", int(thursday_weight), X[(mclouth_id, d, sh)]))
                    mclouth_thursday_count += 1
    print(f"Added {mclouth_thursday_count} McLouth Thursday shift-avoid penalty terms.")

    if obj_terms:
        model.Minimize(sum(w * var for (_, w, var) in obj_terms))

    # ---- Pre-solve debug ----
    print("\n=== Horizon Balance Debug Info ===")
    print(f"Schedule period: {s} to {e} ({len(D)} weekdays)")
    current_weeks = len(partition_into_weeks(D, week_start=0, include_weekends=False))

    print(f"Current period: {current_weeks} weeks")
    print("\nPer-worker historical data:")
    print(f"{'Worker':<15} {'Hist Wks':<10} {'Unavail':<9} {'Hist Cnt':<10} {'Wkly Tgt':<10} {'Expected':<10} {'Discrep':<10}")
    print("-" * 85)
    for w in workers:
        w_norm = _norm_id(w)
        hist_wks = historical_weeks_by_worker.get(w_norm, 0)
        unavail_days = hist_unavailable_days.get(w_norm, 0)
        hist_count = historical_counts.get(w_norm, 0)
        weekly_tgt = WEEKLY_TARGETS.get(w_norm, 0)
        target_adjustment = (unavail_days * weekly_tgt) // 5
        expected = weekly_tgt * hist_wks - target_adjustment
        discrepancy = hist_count - expected
        print(f"{w:<15} {hist_wks:<10} {unavail_days:<9} {hist_count:<10} {weekly_tgt:<10} {expected:<10} {discrepancy:+10}")

    # ---- Solve ----
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 60.0
    solver.parameters.max_deterministic_time = 20.0
    solver.parameters.num_search_workers = 8
    solver.parameters.log_search_progress = True
    solver.parameters.log_to_stdout = True

    res = solver.Solve(model)
    if res not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if m1m2_hard:
            print()
            print(">>> Hard M1+M2 RVU floor was infeasible. Retrying with soft constraint so")
            print(">>> the diagnostic output can show which workers fell short. Adjust their")
            print(">>> M1M2_TARGET_FRACTION values and re-run with hard=True if desired.")
            print()
            return build_and_solve(df, s, e, weekdays, call_dates, history, m1m2_hard=False,
                                    avg_rvu_by_shift=avg_rvu_by_shift,
                                    ftes_by_worker_month=ftes_by_worker_month)
        print("No feasible solution.")
        print(res)
        sys.exit(2)

    return {
        "solver": solver, "model": model,
        "X": X, "Z": Z, "P": P, "D": D, "S": S, "T": T,
        "obj_terms": obj_terms,
        "shift_lockins": shift_lockins, "status_lockins": status_lockins,
        "preserve_text": preserve_text, "call_lockins": call_lockins,
        "HZ_DEBUG": HZ_DEBUG,
        "PAID_DEBUG": PAID_DEBUG,                 # Flex/Nights distribution
        "INPATIENT_A_DEBUG": INPATIENT_A_DEBUG,   # InpatientA distribution
        "MONTHLY_RVU_DEBUG": MONTHLY_RVU_DEBUG,   # per-month RVU floor results
    }


def publish_results(sheet, df, result, tab_name="schedule-bot-flex5"):
    """Print diagnostics, write solution to DataFrame, and publish to Google Sheets."""
    solver = result["solver"]
    X, Z = result["X"], result["Z"]
    P, D, S, T = result["P"], result["D"], result["S"], result["T"]
    obj_terms = result["obj_terms"]
    preserve_text = result["preserve_text"]

    # ---- Diagnostics ----
    print_objective_breakdown(obj_terms, solver, top_n=60)
    print_horizon_debug(result["HZ_DEBUG"], solver, sort_by="abs_dev")
    print("\n=== Flex/Nights distribution ===")
    print_paid_share_debug(result.get("PAID_DEBUG", {}), solver, sort_by="DEV")
    if result.get("INPATIENT_A_DEBUG"):
        print("\n=== Inpatient A distribution ===")
        print_paid_share_debug(result["INPATIENT_A_DEBUG"], solver, sort_by="DEV")
    if result.get("MONTHLY_RVU_DEBUG"):
        print_monthly_rvu_debug(result["MONTHLY_RVU_DEBUG"], solver, sort_by="worst_shortfall")

    print("\n=== Call Day Totals ===")
    print_academic_year_call_totals(
        df,
        date_col="Date",
        call_col=CALL_COLUMN,
        workers=workers,
        holidays=HOLIDAYS,
        year_start=ACADEMIC_YEAR_START,
        year_end=ACADEMIC_YEAR_END,
    )

    # ---- Build output DataFrame ----
    out_df = fill_df_from_solution(
        df,
        date_col="Date",
        worker_cols=workers,
        shifts=shifts,
        statuses=statuses,
        P=P, D=D, S=S, T=T,
        X=X, Z=Z,
        solver=solver,
        preserve_text=preserve_text,
    )

    out_df = mark_weekends_and_holidays(
        out_df,
        date_col="Date",
        worker_cols=workers,
        holidays=HOLIDAYS,
        weekend_label="Weekend",
        holiday_label="Holiday",
    )

    # ---- Publish to Google Sheets ----
    publish_df_to_new_sheet(sheet, tab_name, out_df)
    ws = sheet.worksheet(tab_name)
    apply_label_colors(ws, out_df, worker_cols=workers, label_colors=LABEL_COLORS)
    set_frozen(ws, rows=1, cols=3)
    set_global_font(ws, "Calibri", 11)
    print(f"Published to tab: {tab_name}")


if __name__ == "__main__":
    sheet, df = load_spreadsheet()
    s, e, weekdays, saturdays = get_weekdays_and_saturdays_from_args()
    call_dates = compute_call_dates(s, e, HOLIDAYS)
    history = load_history(df, s)
    result = build_and_solve(df, s, e, weekdays, call_dates, history)
    publish_results(sheet, df, result)

