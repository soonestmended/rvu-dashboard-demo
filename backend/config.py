"""Configuration loader for RVU dashboard.

Loads YAML configuration files for:
- Attending to division mappings
- CPT code to division mappings
- Shift definitions (all shift types)
- Schedule information (CSV or YAML)
- Neuro division staffing configuration
"""

import yaml
import os
from pathlib import Path
from typing import Dict, List, Optional, Any


# Default config directory (can be overridden by environment variable)
CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', 'config'))


def load_attending_id_map(config_dir: Path = CONFIG_DIR) -> Dict[str, str]:
    """
    Load attending name to ID mapping from CSV file.

    Args:
        config_dir: Path to config directory

    Returns:
        Dict mapping normalized attending names to ATT-x IDs
    """
    import pandas as pd

    map_file = config_dir / "attending_id_map.csv"
    if not map_file.exists():
        return {}

    df = pd.read_csv(map_file)
    # Create reverse mapping: name -> id
    # Normalize names (strip, uppercase for matching)
    name_to_id = {}
    for _, row in df.iterrows():
        name = str(row['attending_name']).strip().upper()
        att_id = row['attending_id']
        name_to_id[name] = att_id

    return name_to_id


def load_attending_divisions(config_dir: Path = CONFIG_DIR) -> Dict[str, str]:
    """
    Load attending name to division mappings.

    Args:
        config_dir: Path to config directory

    Returns:
        Dict mapping attending names to divisions
    """
    config_file = config_dir / "attending_divisions.yaml"

    if not config_file.exists():
        return {}

    with open(config_file, 'r') as f:
        data = yaml.safe_load(f)

    return data.get('divisions', {})


def save_attending_divisions(data: Dict[str, Any], config_dir: Path = CONFIG_DIR) -> None:
    """
    Save attending name to division mappings.

    Args:
        data: Dict containing 'divisions' key with mappings
        config_dir: Path to config directory
    """
    config_file = config_dir / "attending_divisions.yaml"
    config_file.parent.mkdir(parents=True, exist_ok=True)

    with open(config_file, 'w') as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=True)


def load_cpt_divisions(config_dir: Path = CONFIG_DIR) -> Dict[str, str]:
    """
    Load CPT code to division mappings.

    Args:
        config_dir: Path to config directory

    Returns:
        Dict mapping CPT codes to divisions
    """
    config_file = config_dir / "cpt_divisions.yaml"

    if not config_file.exists():
        return {}

    with open(config_file, 'r') as f:
        data = yaml.safe_load(f)

    # Combine both cpt_divisions and manual_review sections
    mappings = {}
    if 'cpt_divisions' in data:
        mappings.update(data['cpt_divisions'])
    if 'manual_review' in data:
        mappings.update(data['manual_review'])

    return mappings


def load_flex_shifts(config_dir: Path = CONFIG_DIR) -> List[Dict[str, Any]]:
    """
    Load flex shift rule definitions.

    Args:
        config_dir: Path to config directory

    Returns:
        List of flex shift rule definitions
    """
    config_file = config_dir / "flex_shifts.yaml"

    if not config_file.exists():
        return []

    with open(config_file, 'r') as f:
        data = yaml.safe_load(f)

    return data.get('flex_shifts', [])


def load_schedule(config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    """
    Load schedule information (e.g., on-call assignments).

    Args:
        config_dir: Path to config directory

    Returns:
        Dict with schedule information
    """
    config_file = config_dir / "schedule.yaml"

    if not config_file.exists():
        return {}

    with open(config_file, 'r') as f:
        data = yaml.safe_load(f)

    return data


def get_fte_for_date(att_info: Dict[str, Any], target_date) -> float:
    """Return the FTE that was effective for an attending on a given date.

    Schema (all fields under each attending record, all optional except `fte`):

        attendings:
          ATT001132:
            name: "Floriolli, David"
            fte: 0.8                                       # current/default value
            start_date: "2025-09-15"                       # before this → FTE 0
            end_date:   "2027-06-30"                       # after this → FTE 0
            fte_history:
              - { effective: "2025-09-15", fte: 1.0 }
              - { effective: "2026-01-01", fte: 0.8 }

    Lookup rules (in order):
      1. If `start_date` is set and `target_date < start_date` → 0.0 (pre-hire / pre-contract).
      2. If `end_date` is set and `target_date > end_date` → 0.0 (post-departure).
      3. Otherwise, walk `fte_history` and use the most recent entry whose `effective` date is
         ≤ `target_date`.
      4. If no history entry applies (or none exists), fall back to the standalone `fte` field.

    `target_date` may be a `datetime.date`, `datetime.datetime`, or ISO string.
    """
    # Normalize target to ISO string for lexicographic comparison
    if hasattr(target_date, 'isoformat'):
        target_iso = target_date.isoformat()[:10]
    else:
        target_iso = str(target_date)[:10]

    # Hard-zero outside the employment window
    start = att_info.get('start_date')
    end = att_info.get('end_date')
    if start and target_iso < str(start)[:10]:
        return 0.0
    if end and target_iso > str(end)[:10]:
        return 0.0

    fallback = float(att_info.get('fte', 1.0))
    history = att_info.get('fte_history') or []
    if not history:
        return fallback

    sorted_h = sorted(history, key=lambda e: e['effective'])
    current = None
    for entry in sorted_h:
        if entry['effective'] <= target_iso:
            current = float(entry['fte'])
        else:
            break
    return current if current is not None else fallback


def get_period_target(att_info: Dict[str, Any], annual: float,
                      period_start, period_end_inclusive) -> float:
    """Sum FTE-prorated target across a date range, day by day.

    Each day contributes `annual × FTE_for_day / days_in_its_month / 12`. Summing across:
      - a full month = annual × FTE / 12 (the standard monthly target)
      - a partial month = correctly prorated to days_in_window
      - a month that contains a start/end_date or FTE change = exact day-weighted prorate

    Use this any time you need an FTE-prorated target for an arbitrary period. The math
    respects per-day FTE (from start_date / end_date / fte_history) so attendings who joined
    mid-month or had their FTE change mid-month get the right number.
    """
    from datetime import timedelta as _td
    from calendar import monthrange
    if not annual:
        return 0.0
    total = 0.0
    cur = period_start
    while cur <= period_end_inclusive:
        fte = get_fte_for_date(att_info, cur)
        if fte > 0:
            days_in_month = monthrange(cur.year, cur.month)[1]
            total += annual * fte / days_in_month / 12.0
        cur = cur + _td(days=1)
    return total


def get_month_fte_avg(att_info: Dict[str, Any], year: int, month: int) -> float:
    """Return the average FTE across the days of a calendar month. Used for displaying a
    representative "FTE for this month" when the FTE varies within the month (e.g., the
    attending started mid-month, so days 1–14 are 0 and 15–31 are 1.0)."""
    from datetime import date as _date_t, timedelta as _td
    from calendar import monthrange
    days_in_month = monthrange(year, month)[1]
    first = _date_t(year, month, 1)
    fte_sum = 0.0
    for i in range(days_in_month):
        fte_sum += get_fte_for_date(att_info, first + _td(days=i))
    return fte_sum / days_in_month


def load_neuro_config(config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    """
    Load neuro division configuration.

    Args:
        config_dir: Path to config directory

    Returns:
        Dict with neuro configuration (attendings, FTE, annual RVU expectation)
    """
    config_file = config_dir / "neuro_config.yaml"

    if not config_file.exists():
        return {'annual_rvu_expectation': 5000, 'attendings': {}}

    with open(config_file, 'r') as f:
        data = yaml.safe_load(f)

    return data


def load_neuro_attending_map(config_dir: Path = CONFIG_DIR) -> Dict[str, str]:
    """
    Load neuro division attending map from CSV file.

    The neuro_att_map.csv has format (no header):
    ATT000767,"Chang, Peter"
    ATT000772,"Chow, Daniel S"

    This creates mappings from both full name and last name to ATT ID.

    Args:
        config_dir: Path to config directory

    Returns:
        Dict mapping normalized names to ATT-x IDs
    """
    import pandas as pd

    map_file = config_dir / "neuro_att_map.csv"
    if not map_file.exists():
        return {}

    # Read CSV without header - first column is ATT ID, second is name
    df = pd.read_csv(map_file, header=None, names=['attending_id', 'attending_name'])
    name_to_id = {}

    for _, row in df.iterrows():
        att_id = row['attending_id']
        full_name = str(row['attending_name']).strip()

        # Normalize full name
        full_name_upper = full_name.upper()
        name_to_id[full_name_upper] = att_id

        # Extract last name (before comma) and add mapping
        if ',' in full_name:
            last_name = full_name.split(',')[0].strip().upper()
            name_to_id[last_name] = att_id

    return name_to_id


def load_shifts(config_dir: Path = CONFIG_DIR) -> List[Dict[str, Any]]:
    """
    Load shift definitions.

    Args:
        config_dir: Path to config directory

    Returns:
        List of shift definitions with validity periods and scope
    """
    config_file = config_dir / "shifts.yaml"

    if not config_file.exists():
        return []

    with open(config_file, 'r') as f:
        data = yaml.safe_load(f)

    return data.get('shifts', [])


_FEATURE_FLAGS_DEFAULTS = {
    'pay_projection_visible_to_individuals': False,
}


def load_feature_flags(config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    """Load runtime-toggleable feature flags. Missing keys fall back to defaults so the app
    works without a config file. Edited via /api/settings/feature-flags (admin-only)."""
    path = config_dir / "feature_flags.yaml"
    flags = dict(_FEATURE_FLAGS_DEFAULTS)
    if path.exists():
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            for k in flags:
                if k in data:
                    flags[k] = bool(data[k])
        except Exception:
            pass
    return flags


def save_feature_flags(flags: Dict[str, Any], config_dir: Path = CONFIG_DIR) -> None:
    """Persist feature flags to YAML. Atomic write via tempfile + os.replace."""
    import os
    import tempfile
    path = config_dir / "feature_flags.yaml"
    # Only persist known keys to avoid stray data accumulating.
    safe = {k: bool(flags.get(k, _FEATURE_FLAGS_DEFAULTS[k])) for k in _FEATURE_FLAGS_DEFAULTS}
    fd, tmp = tempfile.mkstemp(dir=str(config_dir), prefix='.feature_flags.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            yaml.safe_dump(safe, f, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


# Process-lifetime cache for the parsed schedule, keyed by (config_dir, mtime tuple of CSVs).
# Invalidates automatically when any source file is edited.
_schedule_cache: Dict[Any, Dict[str, Any]] = {}


def load_schedule(config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    """
    Load schedule information.

    Tries to load from neuro_schedule*.csv (Google Sheet export) first,
    falls back to schedule.csv, then schedule.yaml.

    Args:
        config_dir: Path to config directory

    Returns:
        Dict mapping date strings to dict of attending_id -> shift_name
        Also includes "_on_call" key with attending_id who is on call
    """
    # Try neuro schedule CSV files first (Google Sheet exports)
    gsheet_files = sorted(config_dir.glob("neuro_schedule*.csv"))
    if gsheet_files:
        # Cache key includes each file's mtime so an edit invalidates immediately.
        cache_key = (str(config_dir), tuple((f.name, f.stat().st_mtime_ns) for f in gsheet_files))
        cached = _schedule_cache.get(cache_key)
        if cached is not None:
            return cached

        # Load neuro attending map (uses last names)
        from .schedule import parse_neuro_schedule_gsheet
        attending_id_map = load_neuro_attending_map(config_dir)

        # Parse all schedule files and merge them
        merged_schedule = {}
        for schedule_file in gsheet_files:
            print(f"Loading schedule from: {schedule_file}")
            schedule = parse_neuro_schedule_gsheet(schedule_file, attending_id_map)
            # Merge schedules - later files override earlier ones for same dates
            merged_schedule.update(schedule)

        print(f"Loaded {len(gsheet_files)} schedule files with {len(merged_schedule)} total days")
        _schedule_cache.clear()  # only keep one entry at a time
        _schedule_cache[cache_key] = merged_schedule
        return merged_schedule

    # Try regular schedule.csv
    csv_file = config_dir / "schedule.csv"
    if csv_file.exists():
        from .schedule import parse_schedule_csv
        return parse_schedule_csv(csv_file)

    # Fall back to YAML
    from .schedule import load_schedule_from_yaml
    return load_schedule_from_yaml(config_dir)


def load_all_configs(config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    """
    Load all configuration files.

    Args:
        config_dir: Path to config directory

    Returns:
        Dict with all configuration data
    """
    return {
        'attending_divisions': load_attending_divisions(config_dir),
        'cpt_divisions': load_cpt_divisions(config_dir),
        'flex_shifts': load_flex_shifts(config_dir),
        'schedule': load_schedule(config_dir),
        'neuro_config': load_neuro_config(config_dir),
        'shifts': load_shifts(config_dir),
    }


def get_division_for_attending(attending_name: str,
                               attending_divisions: Dict[str, str]) -> Optional[str]:
    """
    Get the division for an attending radiologist.

    Args:
        attending_name: Name of the attending (as it appears in data)
        attending_divisions: Dict of attending name to division mappings

    Returns:
        Division name or None if not found
    """
    # Try exact match first
    if attending_name in attending_divisions:
        return attending_divisions[attending_name]

    # Try case-insensitive match
    attending_lower = attending_name.lower()
    for name, division in attending_divisions.items():
        if name.lower() == attending_lower:
            return division

    return None


def get_division_for_cpt(cpt_code: str,
                         cpt_divisions: Dict[str, str]) -> Optional[str]:
    """
    Get the division for a CPT code.

    Args:
        cpt_code: CPT code (may have multiple codes separated by comma)
        cpt_divisions: Dict of CPT code to division mappings

    Returns:
        Division name or None if not found
    """
    # Try exact match first
    if cpt_code in cpt_divisions:
        return cpt_divisions[cpt_code]

    # Handle multi-code CPT entries (e.g., "72195, 74181, 76376")
    # Use the first code for lookup
    if ',' in cpt_code:
        first_code = cpt_code.split(',')[0].strip()
        if first_code in cpt_divisions:
            return cpt_divisions[first_code]

    return None
