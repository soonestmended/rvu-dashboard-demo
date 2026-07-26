"""Flask application for RVU Dashboard API."""

from datetime import datetime, date as _date_cls
from pathlib import Path

import os
from flask import Flask, jsonify, request, Response, session
from flask_cors import CORS

from .database import get_db_path, get_connection
from .queries import (
    _AFTER_HOURS_PREDICATE,
    get_rvus_by_division,
    get_rvus_by_location,
    get_rvus_by_attending,
    get_divisions,
    get_locations,
    get_attendings,
    get_date_range,
    get_rvus_by_shift,
    get_rvus_by_attending_and_shift,
    get_shift_rvu_averages,
    get_per_day_attending_production,
    get_monthly_rvus_for_attending,
    get_daily_rvus_for_attending,
)
from .config import load_attending_divisions, save_attending_divisions, load_neuro_config, load_schedule, get_fte_for_date, get_period_target, get_month_fte_avg, load_feature_flags, save_feature_flags
from .staffing import get_neuro_staffing_metrics, get_neuro_division_summary, get_moonlight_compensation_model
from .schedule import count_shifts_by_attending, get_full_schedule
from . import auth as auth_module

# Create Flask app
app = Flask(__name__)
# Session cookies are signed with this key. FLASK_SECRET_KEY MUST be set in the environment
# to a long random value (`python -c 'import secrets; print(secrets.token_hex(32))'`).
# Well-known defaults are refused at startup — anyone who knows the string could forge
# an admin session. Fail loudly here rather than boot with a forgeable key.
_KNOWN_INSECURE_SECRETS = {
    'dev-secret-set-FLASK_SECRET_KEY-in-prod',
    'dev-only-replace-me-with-a-real-secret',
    '', 'change-me', 'changeme', 'secret', 'password',
}
_secret = os.environ.get('FLASK_SECRET_KEY', '')
if _secret in _KNOWN_INSECURE_SECRETS or len(_secret) < 16:
    raise RuntimeError(
        "FLASK_SECRET_KEY must be set to a secure random value (≥16 chars, not a known "
        "placeholder). Generate one with: python -c 'import secrets; print(secrets.token_hex(32))'"
    )
app.secret_key = _secret
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# Opt-in Secure flag — when the deploy terminates HTTPS in front of the app, set
# SESSION_COOKIE_SECURE=true so cookies won't be sent over plain HTTP. Default off so
# local dev over http://localhost keeps working.
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', '').lower() in ('1', 'true', 'yes')
from datetime import timedelta as _td_session
app.config['PERMANENT_SESSION_LIFETIME'] = _td_session(days=30)
# CORS must allow credentials so the browser sends/receives the session cookie.
CORS(app, supports_credentials=True)


# Endpoints that are reachable without a valid session. Login obviously, plus health (so
# infra monitors don't trip auth). Everything else under /api/ requires a session — see the
# before_request hook below.
_PUBLIC_ENDPOINTS = {'/api/auth/login', '/api/health'}


@app.before_request
def _auth_gate():
    """Default-deny on /api/* routes — only logged-in users can reach data endpoints. CORS
    preflight (OPTIONS) is always allowed. Non-API routes are not affected (the frontend
    serves its own assets separately)."""
    if request.method == 'OPTIONS':
        return None
    if not request.path.startswith('/api/'):
        return None
    if request.path in _PUBLIC_ENDPOINTS:
        return None
    if auth_module.current_user() is None:
        return jsonify({'error': 'authentication required'}), 401
    return None


# Query-string names that carry ISO dates and end up spliced into SQL somewhere. Every
# downstream query in queries.py interpolates these raw into f-strings — parameterizing
# each callsite would be a large refactor. Validating here is the load-bearing sanitization:
# any request that reaches an endpoint is guaranteed to have well-formed dates, so the
# f-string interpolation is safe (an ISO date `\d{4}-\d\d-\d\d` cannot contain a quote
# or a semicolon). Keep this list in sync when adding new date-typed query params.
_ISO_DATE_ARG_NAMES = ('start_date', 'end_date')


@app.before_request
def _validate_iso_date_args():
    """Reject requests where a date-typed query param is not a valid YYYY-MM-DD.

    SQL-injection defense — see `_ISO_DATE_ARG_NAMES` above. Without this, a caller could
    pass e.g. `start_date=2020-01-01' UNION SELECT ... --` and read data past the query's
    intended authz scope (individual users bypassing the `report_finalized_by` filter to
    read another attending's cases).
    """
    if request.method == 'OPTIONS':
        return None
    if not request.path.startswith('/api/'):
        return None
    for name in _ISO_DATE_ARG_NAMES:
        v = request.args.get(name)
        if not v:
            continue
        try:
            _date_cls.fromisoformat(v)
        except ValueError:
            return jsonify({'error': f'{name} must be a YYYY-MM-DD date'}), 400
    return None


def _require_admin():
    """Helper for endpoints that should only be reachable by admins. Returns a Response if
    the caller isn't an admin, else None. Usage: `err = _require_admin(); if err: return err`."""
    u = auth_module.current_user()
    if not u or u.get('role') != 'admin':
        return jsonify({'error': 'admin access required'}), 403
    return None


@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    """POST {username, password}. On success, sets a session cookie and returns user info."""
    body = request.get_json(silent=True) or {}
    username = (body.get('username') or '').strip()
    password = body.get('password') or ''
    if not username or not password:
        return jsonify({'error': 'username and password required'}), 400
    user = auth_module.find_user(username)
    if not user or not auth_module.verify_password(password, user.get('password_hash', '')):
        return jsonify({'error': 'invalid credentials'}), 401
    session.clear()
    session['username'] = username
    session.permanent = True
    # `password_is_default` echoed so the post-login UI can immediately show the rotate-me
    # nudge without an extra round trip to /api/auth/me.
    return jsonify({
        'username': user['username'],
        'role': user.get('role', 'individual'),
        'attending_id': user.get('attending_id'),
        'password_is_default': auth_module.verify_password(username, user.get('password_hash', '')),
        # Mirror /api/auth/me so the freshly-logged-in user object drives demo behavior (e.g.
        # blurred $ figures) immediately, without needing a page reload.
        'demo_mode': bool(os.environ.get('DEMO_MODE')),
    })


@app.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    session.clear()
    return jsonify({'success': True})


@app.route('/api/settings/feature-flags', methods=['GET'])
def api_settings_feature_flags_get():
    """Return the current feature flags. Available to any authed user — the frontend uses
    the values to decide which tabs to render (the backend independently enforces
    admin-only writes + the pay-projection gate)."""
    return jsonify(load_feature_flags())


@app.route('/api/settings/feature-flags', methods=['POST'])
@auth_module.require_admin
def api_settings_feature_flags_set():
    """Admin-only: update one or more feature flag values."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({'error': 'expected JSON object of flag: bool pairs'}), 400
    current = load_feature_flags()
    current.update({k: bool(v) for k, v in body.items()})
    try:
        save_feature_flags(current)
    except Exception as ex:
        return jsonify({'error': f'persist failed: {ex}'}), 500
    return jsonify(current)


@app.route('/api/auth/users', methods=['GET'])
@auth_module.require_admin
def api_auth_users():
    """List all users (admin only). Returns username, role, attending_id, and a flag
    `password_is_default` for each — used by the admin "Users" tab to show who's still on
    their initial password vs who's rotated."""
    out = []
    for u in auth_module.load_users():
        username = u.get('username') or ''
        out.append({
            'username': username,
            'role': u.get('role', 'individual'),
            'attending_id': u.get('attending_id'),
            'password_is_default': auth_module.verify_password(
                username, u.get('password_hash', '')
            ),
        })
    return jsonify(out)


@app.route('/api/auth/reset-password', methods=['POST'])
@auth_module.require_admin
def api_auth_reset_password():
    """Admin resets a user's password back to its initial value (= their username). Returns
    the temp password in the response so the admin can relay it. The user will see the
    rotate-me banner on next login."""
    body = request.get_json(silent=True) or {}
    username = (body.get('username') or '').strip()
    if not username:
        return jsonify({'error': 'username required'}), 400
    target = auth_module.find_user(username)
    if not target:
        return jsonify({'error': f'no such user: {username}'}), 404
    new_hash = auth_module.hash_password(username)  # initial-password convention
    try:
        auth_module.update_user_password(username, new_hash)
    except Exception as ex:
        return jsonify({'error': f'failed to reset: {ex}'}), 500
    return jsonify({'success': True, 'username': username, 'temp_password': username})


@app.route('/api/auth/change-password', methods=['POST'])
def api_auth_change_password():
    """Logged-in user changes their own password. Requires old_password to match the stored
    hash. New password must be ≥8 chars. Rewrites users.yaml atomically with the new hash.
    """
    u = auth_module.current_user()
    if not u:
        return jsonify({'error': 'authentication required'}), 401
    body = request.get_json(silent=True) or {}
    old_password = body.get('old_password') or ''
    new_password = body.get('new_password') or ''
    if not old_password or not new_password:
        return jsonify({'error': 'old_password and new_password required'}), 400
    if len(new_password) < 8:
        return jsonify({'error': 'new password must be at least 8 characters'}), 400
    if new_password == old_password:
        return jsonify({'error': 'new password must differ from old password'}), 400

    # Re-fetch the stored hash (current_user() doesn't include it).
    full = auth_module.find_user(u['username'])
    if not full or not auth_module.verify_password(old_password, full.get('password_hash', '')):
        return jsonify({'error': 'current password is incorrect'}), 401

    new_hash = auth_module.hash_password(new_password)
    try:
        auth_module.update_user_password(u['username'], new_hash)
    except Exception as ex:
        return jsonify({'error': f'failed to update password: {ex}'}), 500
    return jsonify({'success': True})


@app.route('/api/auth/me', methods=['GET'])
def api_auth_me():
    """Return the current session's user info. 401 if not logged in. Includes attending_name
    when the user is bound to a specific attending so the UI can show 'Hi, Floriolli'. Also
    returns `password_is_default: true` if the user hasn't changed their initial (username =
    password) credential, so the frontend can nudge them to rotate it."""
    u = auth_module.current_user()
    if not u:
        return jsonify({'error': 'not logged in'}), 401
    if u.get('attending_id'):
        neuro = load_neuro_config()
        info = (neuro.get('attendings') or {}).get(u['attending_id']) or {}
        u['attending_name'] = info.get('name', u['attending_id'])
    # Cheap heuristic for "still using the initial password": current hash verifies against
    # the username itself. We re-load the full user record here (current_user strips the hash).
    full = auth_module.find_user(u['username']) or {}
    u['password_is_default'] = auth_module.verify_password(u['username'], full.get('password_hash', ''))
    # Signals the fabricated demo stack (DEMO_MODE=1) regardless of the session's role — the demo
    # logs in as an admin, so role can't distinguish it. The UI uses this to seed demo-friendly
    # defaults (e.g. a clean, open schedule-generation window).
    u['demo_mode'] = bool(os.environ.get('DEMO_MODE'))
    return jsonify(u)


@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat()
    })


@app.route('/api/rvus/by-division', methods=['GET'])
@auth_module.require_admin_or_demo
def api_rvus_by_division():
    """
    Get RVU totals by division.

    Query params:
        start_date: ISO date string (required)
        end_date: ISO date string (required)
        date_field: 'exam_completed_date' or 'report_finalized_date' (default: exam_completed_date)
        attending: filter by attending name (optional)
        patient_type: filter by patient type (optional, comma-separated: ER,INPATIENT,OUTPATIENT)
    """
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        exam_categories = request.args.get('exam_categories')
        attending = request.args.get('attending')
        patient_type = request.args.get('patient_type')
        date_field = request.args.get('date_field')

        if not start_date or not end_date:
            return jsonify({'error': 'start_date and end_date are required'}), 400

        con = get_connection(get_db_path())
        results = get_rvus_by_division(
            con,
            start_date,
            end_date,
            exam_categories=exam_categories,
            attending=attending,
            patient_type=patient_type,
            date_field=date_field,
        )
        con.close()

        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/rvus/by-location', methods=['GET'])
@auth_module.require_admin_or_demo
def api_rvus_by_location():
    """
    Get RVU totals by location.

    Query params:
        start_date: ISO date string (required)
        end_date: ISO date string (required)
        date_field: 'exam_completed_date' or 'report_finalized_date' (default: exam_completed_date)
        patient_type: filter by patient type (optional, comma-separated)
    """
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        exam_categories = request.args.get('exam_categories')
        patient_type = request.args.get('patient_type')
        date_field = request.args.get('date_field')

        if not start_date or not end_date:
            return jsonify({'error': 'start_date and end_date are required'}), 400

        con = get_connection(get_db_path())
        results = get_rvus_by_location(
            con,
            start_date,
            end_date,
            exam_categories=exam_categories,
            patient_type=patient_type,
            date_field=date_field,
        )
        con.close()

        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/rvus/by-attending', methods=['GET'])
@auth_module.require_admin_or_demo
def api_rvus_by_attending():
    """
    Get RVU totals by attending.

    Query params:
        start_date: ISO date string (required)
        end_date: ISO date string (required)
        date_field: 'exam_completed_date' or 'report_finalized_date' (default: exam_completed_date)
        division: filter by division (optional)
        patient_type: filter by patient type (optional, comma-separated)
    """
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        division = request.args.get('division')
        exam_categories = request.args.get('exam_categories')
        patient_type = request.args.get('patient_type')
        date_field = request.args.get('date_field')

        if not start_date or not end_date:
            return jsonify({'error': 'start_date and end_date are required'}), 400

        con = get_connection(get_db_path())
        results = get_rvus_by_attending(
            con,
            start_date,
            end_date,
            division=division,
            exam_categories=exam_categories,
            patient_type=patient_type,
            date_field=date_field,
        )
        con.close()

        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/divisions', methods=['GET'])
@auth_module.require_admin_or_demo
def api_divisions():
    """Get list of all divisions."""
    try:
        con = get_connection(get_db_path())
        results = get_divisions(con)
        con.close()
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/locations', methods=['GET'])
@auth_module.require_admin_or_demo
def api_locations():
    """Get list of all locations."""
    try:
        con = get_connection(get_db_path())
        results = get_locations(con)
        con.close()
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/attendings', methods=['GET'])
@auth_module.require_admin_or_demo
def api_attendings():
    """Get list of all attendings with their divisions."""
    try:
        con = get_connection(get_db_path())
        results = get_attendings(con)
        con.close()
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/attending-divisions', methods=['GET'])
@auth_module.require_admin_or_demo
def api_attending_divisions():
    """Get all attending to division mappings."""
    try:
        mappings = load_attending_divisions()
        return jsonify(mappings)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/date-range', methods=['GET'])
def api_date_range():
    """Get the min and max dates from the database. Optional `division=NEURO` scopes to a division."""
    try:
        date_field = request.args.get('date_field')
        division = request.args.get('division')
        con = get_connection(get_db_path())
        results = get_date_range(con, date_field=date_field, division=division)
        con.close()
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/staffing/neuro-config', methods=['GET'])
@auth_module.require_admin_or_demo
def api_neuro_config():
    """Get neuro division configuration."""
    try:
        config = load_neuro_config()
        return jsonify(config)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/staffing/neuro-metrics', methods=['GET'])
@auth_module.require_admin_or_demo
def api_neuro_staffing_metrics():
    """
    Get staffing metrics for neuro division attendings.

    Query params:
        start_date: ISO date string (required)
        end_date: ISO date string (required)
        date_field: 'exam_completed_date' or 'report_finalized_date' (default: exam_completed_date)
        patient_type: filter by patient type (optional, comma-separated)
    """
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        exam_categories = request.args.get('exam_categories')
        patient_type = request.args.get('patient_type')
        date_field = request.args.get('date_field')
        attending = request.args.get('attending')

        if not start_date or not end_date:
            return jsonify({'error': 'start_date and end_date are required'}), 400

        neuro_config = load_neuro_config()

        # If attending filter provided, limit neuro config to those attendings
        if attending:
            att_set = set(a.strip() for a in attending.split(','))
            filtered_attendings = {k: v for k, v in neuro_config.get('attendings', {}).items() if k in att_set}
            neuro_config = {**neuro_config, 'attendings': filtered_attendings}

        con = get_connection(get_db_path())
        results = get_neuro_staffing_metrics(
            con,
            start_date,
            end_date,
            neuro_config,
            exam_categories=exam_categories,
            patient_type=patient_type,
            date_field=date_field,
        )
        con.close()

        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/staffing/neuro-summary', methods=['GET'])
@auth_module.require_admin_or_demo
def api_neuro_division_summary():
    """
    Get summary staffing metrics for neuro division.

    Query params:
        start_date: ISO date string (required)
        end_date: ISO date string (required)
        date_field: 'exam_completed_date' or 'report_finalized_date' (default: exam_completed_date)
        patient_type: filter by patient type (optional, comma-separated)
    """
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        exam_categories = request.args.get('exam_categories')
        patient_type = request.args.get('patient_type')
        date_field = request.args.get('date_field')

        if not start_date or not end_date:
            return jsonify({'error': 'start_date and end_date are required'}), 400

        neuro_config = load_neuro_config()
        con = get_connection(get_db_path())
        results = get_neuro_division_summary(
            con,
            start_date,
            end_date,
            neuro_config,
            exam_categories=exam_categories,
            patient_type=patient_type,
            date_field=date_field,
        )
        con.close()

        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/shifts/by-shift', methods=['GET'])
@auth_module.require_admin_or_demo
def api_shifts_by_shift():
    """
    Get RVU totals grouped by shift for Neuro division.

    Query params:
        start_date: ISO date string (required)
        end_date: ISO date string (required)
        date_field: 'exam_completed_date' or 'report_finalized_date' (default: exam_completed_date)
        division: filter by division (default: NEURO)
        patient_type: filter by patient type (optional, comma-separated)
    """
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        division = request.args.get('division', 'NEURO')
        exam_categories = request.args.get('exam_categories')
        patient_type = request.args.get('patient_type')
        date_field = request.args.get('date_field')
        attending = request.args.get('attending')

        if not start_date or not end_date:
            return jsonify({'error': 'start_date and end_date are required'}), 400

        con = get_connection(get_db_path())
        results = get_rvus_by_shift(
            con,
            start_date,
            end_date,
            division=division,
            exam_categories=exam_categories,
            patient_type=patient_type,
            date_field=date_field,
            attending=attending,
        )
        con.close()

        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/shifts/by-attending', methods=['GET'])
@auth_module.require_admin_or_demo
def api_shifts_by_attending():
    """
    Get RVU totals grouped by attending and shift for Neuro division.

    Query params:
        start_date: ISO date string (required)
        end_date: ISO date string (required)
        date_field: 'exam_completed_date' or 'report_finalized_date' (default: exam_completed_date)
        division: filter by division (default: NEURO)
        patient_type: filter by patient type (optional, comma-separated)
    """
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        division = request.args.get('division', 'NEURO')
        exam_categories = request.args.get('exam_categories')
        patient_type = request.args.get('patient_type')
        date_field = request.args.get('date_field')
        attending = request.args.get('attending')

        if not start_date or not end_date:
            return jsonify({'error': 'start_date and end_date are required'}), 400

        con = get_connection(get_db_path())
        results = get_rvus_by_attending_and_shift(
            con,
            start_date,
            end_date,
            division=division,
            exam_categories=exam_categories,
            patient_type=patient_type,
            date_field=date_field,
            attending=attending,
        )
        con.close()

        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/schedule/shift-counts', methods=['GET'])
@auth_module.require_admin_or_demo
def api_schedule_shift_counts():
    """
    Get shift counts from the schedule for each attending.

    Query params:
        start_date: ISO date string (required)
        end_date: ISO date string (required)

    Returns:
        List of {attending_id, attending_name, shift_name, count}
    """
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')

        if not start_date or not end_date:
            return jsonify({'error': 'start_date and end_date are required'}), 400

        schedule = load_schedule()
        neuro_config = load_neuro_config()
        results = count_shifts_by_attending(schedule, start_date, end_date, neuro_config)

        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Process-lifetime cache: shift-RVU averages are section-wide (no per-attending filtering)
# so the same call is repeated dozens of times across requests. Keyed by query params + the
# DB's mtime so a re-ingest invalidates immediately.
_shift_avg_cache: dict = {}


def _shift_rvu_averages_with_schedule_counts(start_date: str, end_date: str, date_field: str | None):
    """Compute per-shift average wRVU using exam totals as the numerator and
    schedule-derived (date, attending) pairs as the denominator. Returns the
    same row shape as `get_shift_rvu_averages`. Falls back to exam-derived
    instance counts if the schedule lookup fails.

    Used by both the dashboard's shift-averages endpoint AND the solver's
    /generate path so the two views always reference the same numbers.
    """
    try:
        db_mtime_ns = os.stat(get_db_path()).st_mtime_ns
    except OSError:
        db_mtime_ns = 0
    cache_key = (start_date, end_date, date_field, db_mtime_ns)
    cached = _shift_avg_cache.get(cache_key)
    if cached is not None:
        return cached

    con = get_connection(get_db_path())
    try:
        results = get_shift_rvu_averages(con, start_date, end_date, date_field=date_field)
    finally:
        con.close()

    try:
        from .schedule import get_full_schedule
        sched = load_schedule()
        neuro_config = load_neuro_config()
        full = get_full_schedule(sched, start_date, end_date, neuro_config)
        scheduled_counts = {}
        # Weekday work shifts (OPA/OPB/IA/IB/Flex/Flex-Nights) — count attending assignments on
        # non-weekend, non-holiday days.
        for day in full:
            if day.get('is_weekend') or day.get('is_holiday'):
                continue
            for shift_name in (day.get('assignments') or {}).values():
                if not shift_name:
                    continue
                scheduled_counts[shift_name] = scheduled_counts.get(shift_name, 0) + 1
        # Weekend Call and Weekend Evening ER — both cover the same physical weekend/holiday
        # days, so they share a denominator: count of weekend/holiday days with a daytime on-call
        # attending in the schedule. Using a unified denominator eliminates the off-by-one that
        # comes from late-Friday read spillover (exam attribution) or missing entries in
        # weekend_er_assignments.csv (empirical extraction). Both shifts get the same instance count.
        weekend_attending_days = sum(
            1 for day in full
            if (day.get('is_weekend') or day.get('is_holiday')) and day.get('on_call')
        )
        if weekend_attending_days > 0:
            scheduled_counts['Weekend Call'] = weekend_attending_days
            scheduled_counts['Weekend Evening ER'] = weekend_attending_days
        for r in results:
            sn = r.get('shift_name')
            sched_n = scheduled_counts.get(sn)
            if sched_n and sched_n > 0:
                r['instances'] = sched_n
                r['avg_rvu'] = (r.get('total_rvu', 0) or 0) / sched_n
                r['avg_exams'] = (r.get('total_exams', 0) or 0) / sched_n
    except Exception as ex:
        print(f"[shift-rvu-averages] schedule-count override failed (non-fatal): {ex}")

    _shift_avg_cache.clear()  # only keep the most recent (params, db_mtime) combo
    _shift_avg_cache[cache_key] = results
    return results


@app.route('/api/schedule/shift-rvu-averages', methods=['GET'])
@auth_module.require_admin_or_demo
def api_shift_rvu_averages():
    """Per-shift average wRVU production over the selected period.

    Query params:
        start_date, end_date: ISO date strings (required)
        date_field: 'exam_completed_date' or 'report_finalized_date' (default: exam_completed_date)

    Instance counts are taken from the SCHEDULE (one per (date, attending) actually scheduled
    to that shift) rather than from exam attribution — so paired shifts (OPA/OPB) have identical
    denominators. Days where the attending was scheduled to the shift but logged zero exams
    count as a 0-wRVU instance, which is what the projection actually needs.
    """
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        date_field = request.args.get('date_field')

        if not start_date or not end_date:
            return jsonify({'error': 'start_date and end_date are required'}), 400

        results = _shift_rvu_averages_with_schedule_counts(start_date, end_date, date_field)
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/schedule/actual-production', methods=['GET'])
@auth_module.require_admin_or_demo
def api_actual_production():
    """Per-(date, attending) actual exam count and wRVU for the date range.

    Used by the schedule tab to populate hover tooltips on past shifts.
    """
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')

        if not start_date or not end_date:
            return jsonify({'error': 'start_date and end_date are required'}), 400

        con = get_connection(get_db_path())
        results = get_per_day_attending_production(con, start_date, end_date)
        con.close()
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _worksheet_name_for_date(d):
    """Map a date to its academic-year worksheet name (e.g. '26-27 Faculty Schedule').
    Academic year runs July through June."""
    start_year = d.year if d.month >= 7 else d.year - 1
    end_year = start_year + 1
    return f"{start_year % 100:02d}-{end_year % 100:02d} Faculty Schedule"


def _read_worksheet_df(sheet, name):
    """Read a worksheet by name into a DataFrame; return None if it doesn't exist."""
    import pandas as pd
    try:
        ws = sheet.worksheet(name)
    except Exception:
        return None
    rows = ws.get_all_values()
    if not rows:
        return None
    headers = [h.strip() if h.strip() else f"col_{i}" for i, h in enumerate(rows[0])]
    return pd.DataFrame(rows[1:], columns=headers)


def _load_combined_schedule_df(sheet, start_dt, end_dt):
    """Load all worksheets covering the requested range plus 12-week history lookback.

    The academic-year split (Jul–Jun) means a generation in early Jul needs lock-ins from
    the new year's tab AND historical context from the prior year's tab. Returns the
    concatenated DataFrame; later tabs override earlier ones on Date collisions (none expected
    since academic years don't overlap).
    """
    import pandas as pd
    from datetime import timedelta
    # Cover the lookback window too, so load_history finds historical shifts in the prior year's tab
    needed_dates = [start_dt - timedelta(weeks=14), start_dt, end_dt]
    names = list(dict.fromkeys(_worksheet_name_for_date(d) for d in needed_dates))
    dfs = []
    loaded = []
    for name in names:
        df = _read_worksheet_df(sheet, name)
        if df is not None:
            dfs.append(df)
            loaded.append((name, len(df)))
    if not dfs:
        return pd.DataFrame(), loaded
    if len(dfs) == 1:
        return dfs[0], loaded
    combined = pd.concat(dfs, ignore_index=True, sort=False).fillna('')
    if 'Date' in combined.columns:
        combined = combined.drop_duplicates(subset='Date', keep='first').reset_index(drop=True)
    return combined, loaded


def _load_demo_name_map():
    """Read config-demo/name_map.csv → (fake_to_real, real_to_fake) last-name dicts, both keyed
    by UPPER-cased last name. Used by the DEMO_MODE solver path to swap the demo's fake attending
    names for the real ones the scheduler's rules are keyed to (and back for display)."""
    import os, csv as _csv
    from pathlib import Path
    # name_map.csv stores real last names UPPER-cased, but the scheduler's rules/roster are keyed
    # to a specific casing (e.g. 'Chang', 'McLouth'). Canonicalize each real name to the solver's
    # roster casing so the un-renamed schedule columns and FTE keys match what the solver expects
    # (an uppercase mismatch here silently drops every worker → INFEASIBLE).
    try:
        import schedule as _sm
        _canon = {str(w).upper(): str(w) for w in getattr(_sm, 'workers', [])}
    except Exception:
        _canon = {}
    p = Path(os.environ.get('CONFIG_DIR', '/app/config')) / 'name_map.csv'
    fake_to_real, real_to_fake = {}, {}
    if p.exists():
        with open(p, newline='') as f:
            for row in _csv.DictReader(f):
                real, fake = row['real_last'].strip(), row['fake_last'].strip()
                real = _canon.get(real.upper(), real)  # solver casing when the name is on the roster
                fake_to_real[fake.upper()] = real
                real_to_fake[real.upper()] = fake
    return fake_to_real, real_to_fake


def _load_demo_schedule_df(start_dt, end_dt):
    """DEMO_MODE twin of _load_combined_schedule_df: build the schedule DataFrame from the LOCAL
    fabricated CSVs (config-demo/neuro_schedule_*.csv) instead of the live Google Sheet, covering
    the request range + 12-week history lookback. Columns come back as fake last names; the caller
    un-renames them to real for the solver."""
    import os, pandas as pd
    from pathlib import Path
    from datetime import timedelta
    config_dir = Path(os.environ.get('CONFIG_DIR', '/app/config'))
    needed = [start_dt - timedelta(weeks=14), start_dt, end_dt]

    def _ay_short(d):
        start_year = d.year if d.month >= 7 else d.year - 1
        return f"{start_year % 100:02d}-{(start_year + 1) % 100:02d}"

    names = list(dict.fromkeys(_ay_short(d) for d in needed))
    dfs, loaded = [], []
    for nm in names:
        f = config_dir / f"neuro_schedule_{nm}.csv"
        if f.exists():
            df = pd.read_csv(f, dtype=str).fillna('')
            dfs.append(df)
            loaded.append((f.name, len(df)))
    if not dfs:
        return pd.DataFrame(), loaded
    combined = pd.concat(dfs, ignore_index=True, sort=False).fillna('') if len(dfs) > 1 else dfs[0]
    if 'Date' in combined.columns:
        combined = combined.drop_duplicates(subset='Date', keep='first').reset_index(drop=True)
    return combined, loaded


@app.route('/api/schedule/publish', methods=['POST'])
@auth_module.require_admin
def api_schedule_publish():
    """Publish a candidate schedule to a new (or overwritten) tab on the live spreadsheet.

    Request body:
        {
            "candidate": { "header": [...], "rows": [[...], ...] },
            "tab_name": "schedule-bot-flex5"   # optional; defaults to scheduler convention
            "dry_run": false                   # optional; if true, validates without touching the sheet
        }

    Never modifies the canonical academic-year tabs (25-26 / 26-27 Faculty Schedule).
    """
    import os
    if os.environ.get('DEMO_MODE'):
        # The demo must never write to the real spreadsheet. Candidates can be generated and
        # viewed, but publishing is a no-op.
        return jsonify({'status': 'skipped', 'reason': 'demo mode — publishing is disabled'})
    try:
        import schedule as scheduler_module
        import pandas as pd

        body = request.get_json(silent=True) or {}
        candidate = body.get('candidate') or {}
        tab_name = (body.get('tab_name') or 'schedule-bot-flex5').strip()
        dry_run = bool(body.get('dry_run'))

        # Hard guardrail: refuse to publish over the canonical academic-year tabs.
        protected = {'25-26 Faculty Schedule', '26-27 Faculty Schedule', '24-25 Faculty Schedule'}
        if tab_name in protected:
            return jsonify({
                'error': f"Refusing to publish over protected tab '{tab_name}'. Pick a different tab name.",
            }), 400

        header = candidate.get('header') or []
        rows = candidate.get('rows') or []
        if not header or not rows:
            return jsonify({'error': 'Candidate must include non-empty header and rows.'}), 400

        df = pd.DataFrame(rows, columns=header)

        if dry_run:
            return jsonify({
                'success': True,
                'dry_run': True,
                'tab_name': tab_name,
                'rows': len(df),
                'columns': list(df.columns),
                'sample_first_row': df.iloc[0].astype(str).tolist() if len(df) else None,
            })

        sa_path = os.environ.get('SCHEDULER_SERVICE_ACCOUNT', '/app/scheduler/service_account.json')
        sheet = scheduler_module.open_sheet('NEURORAD SECTION MEGA SPREADSHEET', service_account_json=sa_path)

        # Merge into the target tab instead of wiping it. Rows whose Date falls inside the
        # candidate's date range are overwritten by the candidate; rows outside that range
        # (past schedules, future schedules from earlier publishes) are preserved. New tabs
        # are created on-demand. Columns are aligned by name so order differences don't matter.
        from datetime import date as _date
        from gspread.exceptions import WorksheetNotFound

        def _parse_short_date(s):
            if not isinstance(s, str): return None
            parts = s.split('/')
            if len(parts) != 3: return None
            try:
                m, d, y = int(parts[0]), int(parts[1]), int(parts[2])
                y = 2000 + y if y < 50 else (1900 + y if y < 100 else y)
                return _date(y, m, d)
            except (ValueError, TypeError):
                return None

        candidate_cols = list(df.columns)
        if 'Date' not in candidate_cols:
            return jsonify({'error': "Candidate is missing the 'Date' column."}), 400
        cand_date_idx = candidate_cols.index('Date')

        candidate_dates = set()
        for v in df['Date']:
            d = _parse_short_date(v)
            if d: candidate_dates.add(d)

        try:
            ws = sheet.worksheet(tab_name)
            existing_values = ws.get_all_values()
        except WorksheetNotFound:
            ws = sheet.add_worksheet(
                title=tab_name,
                rows=str(len(df) + 100),
                cols=str(len(candidate_cols) + 5),
            )
            existing_values = []

        # Build the output header as the UNION of existing header + candidate cols. This is
        # what prevents columns from silently disappearing when the frontend's cached
        # candidate lacks a column that's already on the tab (e.g., a newly-added attending
        # whose column exists on the sheet but wasn't in the last-generated candidate).
        existing_header = existing_values[0] if existing_values else []
        existing_data = existing_values[1:] if existing_values else []
        output_cols = list(existing_header)  # keep original order as the base
        for cc in candidate_cols:
            if cc not in output_cols:
                output_cols.append(cc)

        # Map candidate columns onto their output-header index (for writing candidate rows).
        cand_to_out = {cc: output_cols.index(cc) for cc in candidate_cols}
        # Map existing columns onto their output-header index (for preserved rows).
        exist_to_out = {ec: output_cols.index(ec) for ec in existing_header}
        # Where's Date on the output row?
        out_date_idx = output_cols.index('Date') if 'Date' in output_cols else cand_to_out[candidate_cols[cand_date_idx]]

        # Preserved rows: pull each cell into its output position; leave gaps blank.
        preserved_rows = []
        existing_date_idx = existing_header.index('Date') if 'Date' in existing_header else None
        if existing_date_idx is not None:
            for row in existing_data:
                if len(row) <= existing_date_idx:
                    continue
                d = _parse_short_date(row[existing_date_idx])
                if d is None:
                    continue                            # Skip non-date rows (header dupes, summary rows, etc.)
                if d in candidate_dates:
                    continue                            # Will be overwritten by candidate
                out_row = [''] * len(output_cols)
                for src_idx, ec in enumerate(existing_header):
                    if src_idx < len(row):
                        out_row[exist_to_out[ec]] = row[src_idx]
                preserved_rows.append(out_row)

        # Candidate rows: same treatment — put each candidate cell in its output-header position.
        cand_values = df.astype(str).values.tolist()
        candidate_rows_list = []
        for row in cand_values:
            out_row = [''] * len(output_cols)
            for src_idx, cc in enumerate(candidate_cols):
                out_row[cand_to_out[cc]] = row[src_idx]
            candidate_rows_list.append(out_row)

        merged = preserved_rows + candidate_rows_list

        def _row_sort_key(r):
            d = _parse_short_date(r[out_date_idx]) if len(r) > out_date_idx else None
            return d or _date(1900, 1, 1)
        merged.sort(key=_row_sort_key)

        final_values = [output_cols] + merged
        ws.resize(rows=len(final_values), cols=len(output_cols))
        ws.update('A1', final_values)

        # Conditional formatting needs to cover ALL rows in the merged sheet, not just the
        # candidate's rows. Pass a dummy DF of the merged length to apply_label_colors so its
        # row-range math (header_row + len(df)) sweeps the whole tab.
        merged_df = pd.DataFrame(merged, columns=output_cols)

        # Best-effort formatting — match what the scheduler's __main__ flow applies.
        try:
            workers = scheduler_module.workers
            scheduler_module.apply_label_colors(ws, merged_df, worker_cols=workers, label_colors=scheduler_module.LABEL_COLORS)
        except Exception as e:
            print(f"[publish] apply_label_colors failed (non-fatal): {e}")
        try:
            from gspread_formatting import set_frozen
            set_frozen(ws, rows=1, cols=3)
        except Exception as e:
            print(f"[publish] set_frozen failed (non-fatal): {e}")
        try:
            scheduler_module.set_global_font(ws, "Calibri", 11)
        except Exception as e:
            print(f"[publish] set_global_font failed (non-fatal): {e}")

        sheet_url = None
        try:
            sheet_url = sheet.url
        except Exception:
            pass

        return jsonify({
            'success': True,
            'tab_name': tab_name,
            'rows': len(merged),
            'candidate_rows': len(df),
            'preserved_rows': len(preserved_rows),
            'sheet_url': sheet_url,
            'last_published': datetime.utcnow().isoformat() + 'Z',
        })
    except Exception as e:
        import traceback
        return jsonify({
            'error': str(e),
            'traceback': traceback.format_exc().splitlines()[-10:],
        }), 500


@app.route('/api/schedule/refresh-from-sheets', methods=['POST'])
@auth_module.require_admin_or_demo
def api_refresh_schedule_from_sheets():
    """Pull the latest schedule from the Google Sheet and overwrite the local CSV cache.

    Sweeps every academic-year worksheet that exists ('25-26 Faculty Schedule',
    '26-27 Faculty Schedule', etc.) and updates the corresponding cache file. Idempotent —
    a fresh pull just re-writes the same rows; no user data is destroyed.
    """
    import os
    if os.environ.get('DEMO_MODE'):
        # The demo runs on a FABRICATED schedule. It must never pull the real Google Sheet —
        # doing so would overwrite the fake schedule with real attending names (PHI-ish) and
        # break shift attribution (real names don't match the demo DB's ATT ids). No-op.
        return jsonify({'status': 'skipped', 'reason': 'demo mode — schedule is fabricated',
                        'worksheets': []})
    try:
        from pathlib import Path
        import schedule as scheduler_module

        sa_path = os.environ.get('SCHEDULER_SERVICE_ACCOUNT', '/app/scheduler/service_account.json')
        sheet = scheduler_module.open_sheet('NEURORAD SECTION MEGA SPREADSHEET', service_account_json=sa_path)

        config_dir = Path(os.environ.get('CONFIG_DIR', '/app/config'))
        results = []
        for ay_start in range(2024, 2031):
            ay_short = f"{ay_start % 100:02d}-{(ay_start + 1) % 100:02d}"
            ws_name = f"{ay_short} Faculty Schedule"
            df = _read_worksheet_df(sheet, ws_name)
            if df is None:
                continue
            target = config_dir / f"neuro_schedule_{ay_short}.csv"
            df.to_csv(target, index=False)
            results.append({'worksheet': ws_name, 'target_file': str(target), 'rows': len(df)})
        return jsonify({
            'success': True,
            'refreshed': results,
            'last_updated': datetime.utcnow().isoformat() + 'Z',
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/schedule/generate', methods=['POST'])
@auth_module.require_admin
def api_schedule_generate():
    """Run the CP-SAT solver against the live Google Sheet for a date range.

    Streams solver stdout back as NDJSON in real time so the frontend can show progress.
    DOES NOT publish anything back to the sheet — that's a separate /publish endpoint.

    Request body: { "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD" }
    Response: NDJSON stream where each line is one of:
        {"type": "log",   "line": "..."}
        {"type": "ping",  "elapsed_s": 12.3}
        {"type": "result", "header": [...], "rows": [...], "runtime_s": ..., "objective_cost": ...}
        {"type": "error",  "error": "..."}
    """
    body = request.get_json(silent=True) or {}
    start_date = body.get('start_date')
    end_date = body.get('end_date')
    if not start_date or not end_date:
        return jsonify({'error': 'start_date and end_date are required'}), 400

    def stream_solve():
        import os, sys, time, json as _json, threading, queue, traceback as tb
        import schedule as scheduler_module
        import pandas as pd
        from datetime import datetime as dt

        log_q = queue.Queue()
        result_holder = {}
        error_holder = {}

        class QueueWriter:
            def write(self, s):
                if s:
                    log_q.put(s)
            def flush(self):
                pass

        def run_solver():
            old_stdout = sys.stdout
            sys.stdout = QueueWriter()
            try:
                t0 = time.time()
                s = dt.fromisoformat(start_date).date()
                e = dt.fromisoformat(end_date).date()

                # DEMO_MODE: solve on the FABRICATED local schedule instead of the live sheet.
                # The demo attendings are the real ones with fake display names (same ATT id,
                # same FTE), so we un-rename the schedule columns fake→real, let the solver's
                # real-name-keyed rules apply, and re-rename the output real→fake below. Nothing
                # touches the real Google Sheet — the demo stays isolated.
                _demo = bool(os.environ.get('DEMO_MODE'))
                _fake_to_real, _real_to_fake = ({}, {})
                if _demo:
                    print(f"[{time.strftime('%H:%M:%S')}] DEMO MODE — solving the fabricated local schedule (no live sheet)…")
                    _fake_to_real, _real_to_fake = _load_demo_name_map()
                    df, loaded = _load_demo_schedule_df(s, e)
                    df = df.rename(columns={c: _fake_to_real[str(c).strip().upper()]
                                            for c in df.columns
                                            if str(c).strip().upper() in _fake_to_real})
                else:
                    sa_path = os.environ.get('SCHEDULER_SERVICE_ACCOUNT', '/app/scheduler/service_account.json')
                    print(f"[{time.strftime('%H:%M:%S')}] Connecting to Google Sheet…")
                    sheet = scheduler_module.open_sheet(
                        'NEURORAD SECTION MEGA SPREADSHEET',
                        service_account_json=sa_path,
                    )
                    # Load every academic-year worksheet that covers the request window OR the
                    # 12-week history lookback. New academic year (26-27) is in its own tab; we
                    # need both 25-26 and 26-27 when the start straddles fiscal-year boundary.
                    df, loaded = _load_combined_schedule_df(sheet, s, e)
                if df.empty:
                    raise RuntimeError('No schedule worksheet found covering the requested range')
                for ws_name, n in loaded:
                    print(f"[{time.strftime('%H:%M:%S')}] Loaded {n} rows from '{ws_name}'")
                weekdays, _ = scheduler_module.split_weekdays_and_saturdays(s, e)
                call_dates = scheduler_module.compute_call_dates(s, e, scheduler_module.HOLIDAYS)
                print(f"[{time.strftime('%H:%M:%S')}] Loading 12-week historical context…")
                history = scheduler_module.load_history(df, s)

                # The sheet only has rows for the current academic year. If the requested range
                # extends past Jun (or before Jul of the prior year), append blank rows so the solver
                # has dates to write into. Nothing here touches the live sheet.
                from datetime import timedelta
                existing_dates = set()
                for d_str in df['Date'].astype(str):
                    parts = d_str.split('/')
                    if len(parts) != 3:
                        continue
                    try:
                        m, dd, yy = int(parts[0]), int(parts[1]), int(parts[2])
                        yy = yy + 2000 if yy < 50 else yy + 1900 if yy < 100 else yy
                        existing_dates.add(dt(yy, m, dd).date())
                    except Exception:
                        continue
                cur = s
                new_rows = []
                while cur <= e:
                    if cur not in existing_dates:
                        new_row = {col: '' for col in df.columns}
                        new_row['Date'] = f"{cur.month}/{cur.day}/{cur.year - 2000:02d}"
                        new_row['Day'] = cur.strftime('%A')
                        if 'Month' in df.columns:
                            new_row['Month'] = cur.strftime('%B')
                        new_rows.append(new_row)
                    cur += timedelta(days=1)
                if new_rows:
                    print(f"[{time.strftime('%H:%M:%S')}] Adding {len(new_rows)} blank rows for dates outside the live sheet (won't touch the sheet)")
                    df = pd.concat([df, pd.DataFrame(new_rows, columns=df.columns)], ignore_index=True)

                # Try to enable CP-SAT search progress logging so the long solve isn't silent.
                # We monkey-patch the solver after build_and_solve creates it. Works when scheduler
                # exposes the solver via the result dict (it does, key 'solver').
                # Easier path: pre-set a global hint that scheduler picks up. It doesn't, so we'll
                # rely on the print statements that already exist in build_and_solve plus the
                # print we just emitted.

                # Compute per-shift wRVU averages from the last 3 months of retrospective data and
                # pass them to the solver. Anchor to the LATEST date in the exams table (not today)
                # so that ingestion lag doesn't dilute the averages with weeks of empty days.
                # Window ends at min(candidate_start, max_data_date) and looks back 92 days.
                from datetime import timedelta as _td, date as _dt
                from .database import get_connection as _gc
                _con = _gc()
                _max_row = _con.execute(
                    "SELECT MAX(CAST(report_finalized_date AS DATE)) FROM exams WHERE division='NEURO'"
                ).fetchone()
                _con.close()
                _max_data_date = _max_row[0] if _max_row and _max_row[0] else _dt.today()
                if isinstance(_max_data_date, str):
                    _max_data_date = _dt.fromisoformat(_max_data_date)
                avg_end = min(s, _max_data_date)
                avg_start = avg_end - _td(days=92)
                try:
                    # Use the same helper the dashboard endpoint uses so solver and UI cards
                    # are guaranteed to reference identical numbers (schedule-derived denominators).
                    _rows = _shift_rvu_averages_with_schedule_counts(
                        avg_start.isoformat(), avg_end.isoformat(), 'report_finalized_date'
                    )
                    DB_TO_SCHED = {
                        'Inpatient A': 'InpatientA',
                        'Inpatient B': 'InpatientB',
                        'Outpatient A': 'OutpatientA',
                        'Outpatient B': 'OutpatientB',
                        'Flex/Nights': 'Flex/Nights',
                        'Flex': 'Flex',
                    }
                    avg_override = {}
                    for r in _rows:
                        key = DB_TO_SCHED.get(r['shift_name'])
                        if key and r['instances'] >= 5:  # skip noisy low-sample averages
                            avg_override[key] = round(r['avg_rvu'])
                    print(f"[{time.strftime('%H:%M:%S')}] Computed last-3-months shift averages ({avg_start} → {avg_end}): {avg_override}")
                except Exception as _ex:
                    print(f"[{time.strftime('%H:%M:%S')}] Could not fetch retrospective shift averages ({_ex}); using scheduler defaults")
                    avg_override = None

                # Build per-worker per-month FTE map from neuro_config.fte_history. The scheduler
                # keys workers by normalized last name (`_norm_id("Floriolli")` → "floriolli"),
                # so we map ATT IDs → last names → norm IDs, then look up each month's FTE.
                ftes_by_worker_month = None
                try:
                    neuro_cfg = load_neuro_config()
                    norm_id = scheduler_module._norm_id
                    months_in_period = []
                    _y, _m = s.year, s.month
                    while (_y, _m) <= (e.year, e.month):
                        months_in_period.append(f'{_y:04d}-{_m:02d}')
                        _y, _m = (_y + 1, 1) if _m == 12 else (_y, _m + 1)
                    ftes_by_worker_month = {}
                    for att_id, info in (neuro_cfg.get('attendings') or {}).items():
                        last_name = (info.get('name') or '').split(',')[0].strip()
                        if not last_name:
                            continue
                        # DEMO: neuro_config carries fake last names; map back to real so the FTE
                        # keys match the solver's real-name-keyed roster.
                        if _demo:
                            last_name = _fake_to_real.get(last_name.upper(), last_name)
                        worker_id = norm_id(last_name)
                        per_month = {}
                        for mk in months_in_period:
                            yy, mm = mk.split('-')
                            # Day-weighted average so mid-month start_date / fte_history changes
                            # prorate correctly in the solver's monthly RVU floor target.
                            per_month[mk] = float(get_month_fte_avg(info, int(yy), int(mm)))
                        ftes_by_worker_month[worker_id] = per_month
                    print(f"[{time.strftime('%H:%M:%S')}] Built per-worker per-month FTE map for {len(ftes_by_worker_month)} workers")
                except Exception as _ex:
                    print(f"[{time.strftime('%H:%M:%S')}] Could not build per-month FTE map ({_ex}); solver uses default FTE")
                    ftes_by_worker_month = None

                print(f"[{time.strftime('%H:%M:%S')}] Building model and solving (~60 s wall-clock)…")
                result = scheduler_module.build_and_solve(df, s, e, weekdays, call_dates, history,
                    avg_rvu_by_shift=avg_override,
                    ftes_by_worker_month=ftes_by_worker_month,
                )
                print(f"[{time.strftime('%H:%M:%S')}] Solve complete; building output…")
                # Print per-worker per-month RVU floor debug so we can see who fell short
                # and by how much in the solver's view (work + IA bonus + call constant).
                if result.get("MONTHLY_RVU_DEBUG"):
                    scheduler_module.print_monthly_rvu_debug(
                        result["MONTHLY_RVU_DEBUG"], result["solver"], sort_by="worst_shortfall"
                    )

                out_df = scheduler_module.fill_df_from_solution(
                    df, date_col="Date",
                    worker_cols=scheduler_module.workers,
                    shifts=scheduler_module.shifts,
                    statuses=scheduler_module.statuses,
                    P=result["P"], D=result["D"], S=result["S"], T=result["T"],
                    X=result["X"], Z=result["Z"],
                    solver=result["solver"],
                    preserve_text=result["preserve_text"],
                )
                out_df = scheduler_module.mark_weekends_and_holidays(
                    out_df, date_col="Date",
                    worker_cols=scheduler_module.workers,
                    holidays=scheduler_module.HOLIDAYS,
                    weekend_label="Weekend", holiday_label="Holiday",
                )

                def in_range(d):
                    try:
                        parts = d.split('/')
                        if len(parts) != 3:
                            return False
                        m, dd, yy = int(parts[0]), int(parts[1]), int(parts[2])
                        yy = yy + 2000 if yy < 50 else yy + 1900 if yy < 100 else yy
                        return s <= dt(yy, m, dd).date() <= e
                    except Exception:
                        return False

                mask = out_df["Date"].astype(str).apply(in_range)
                scoped_df = out_df[mask].copy()
                runtime_s = time.time() - t0

                obj = None
                try:
                    obj = float(result["solver"].ObjectiveValue())
                except Exception:
                    pass

                header = list(scoped_df.columns)
                rows = scoped_df.astype(str).fillna('').values.tolist()

                # DEMO: re-label the solved schedule real→fake for display. Rename the column
                # headers AND scrub any real last name that appears in cell values (e.g. the
                # Call column), so no real name ever reaches the demo UI.
                if _demo and _real_to_fake:
                    import re as _re_demo
                    header = [_real_to_fake.get(str(h).strip().upper(), h) for h in header]
                    _name_pat = _re_demo.compile(
                        r"\b(" + "|".join(_re_demo.escape(rl) for rl in sorted(_real_to_fake, key=len, reverse=True)) + r")\b",
                        _re_demo.IGNORECASE,
                    )
                    def _relabel(v):
                        return _name_pat.sub(lambda mo: _real_to_fake[mo.group(0).upper()], v) if isinstance(v, str) else v
                    rows = [[_relabel(c) for c in row] for row in rows]

                result_holder['data'] = {
                    'header': header,
                    'rows': rows,
                    'objective_cost': obj,
                    'runtime_s': round(runtime_s, 2),
                    'rows_in_scope': int(mask.sum()),
                }
                print(f"[{time.strftime('%H:%M:%S')}] Done. Objective cost = {obj}, {int(mask.sum())} rows.")
            except Exception as ex:
                error_holder['error'] = str(ex)
                error_holder['traceback'] = tb.format_exc()
            finally:
                sys.stdout = old_stdout
                log_q.put(None)  # sentinel

        # DEMO: the solver's stdout prints REAL last names (its worker roster, the per-worker
        # monthly RVU debug table, "Building schedule from…" lines). Those stream live to the demo
        # UI, so scrub every log line real→fake before emitting. The result payload is scrubbed
        # separately inside run_solver; this covers the progress stream.
        _demo_scrub = None
        if os.environ.get('DEMO_MODE'):
            import re as _re_log
            _f2r_log, _r2f_log = _load_demo_name_map()
            if _r2f_log:
                _pat_log = _re_log.compile(
                    r"\b(" + "|".join(_re_log.escape(rl) for rl in sorted(_r2f_log, key=len, reverse=True)) + r")\b",
                    _re_log.IGNORECASE)
                def _demo_scrub(txt):
                    return _pat_log.sub(lambda mo: _r2f_log[mo.group(0).upper()], txt)

        thread = threading.Thread(target=run_solver, daemon=True)
        t_start = time.time()
        thread.start()

        buffer = ""
        last_emit = time.time()
        while True:
            try:
                chunk = log_q.get(timeout=2)
            except queue.Empty:
                # Keep-alive ping every ~2 s of silence so the client knows we're still alive
                yield _json.dumps({"type": "ping", "elapsed_s": round(time.time() - t_start, 1)}) + "\n"
                last_emit = time.time()
                continue
            if chunk is None:
                break
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if line.strip():
                    if _demo_scrub:
                        line = _demo_scrub(line)
                    yield _json.dumps({"type": "log", "line": line}) + "\n"
                    last_emit = time.time()
        if buffer.strip():
            yield _json.dumps({"type": "log", "line": _demo_scrub(buffer) if _demo_scrub else buffer}) + "\n"

        if 'error' in error_holder:
            yield _json.dumps({"type": "error", "error": error_holder['error'],
                               "traceback": error_holder.get('traceback', '')}) + "\n"
        else:
            yield _json.dumps({"type": "result", **result_holder.get('data', {})}) + "\n"

    return Response(stream_solve(), mimetype='application/x-ndjson',
                    headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'})


@app.route('/api/schedule/date-range', methods=['GET'])
@auth_module.require_admin_or_demo
def api_schedule_date_range():
    """Min/max dates for the loaded schedule.

    `max_date` = last date with a real work-shift assignment (Inpatient A/B, Outpatient
    A/B, Flex, Flex/Nights). This is what the Schedule (Future) tab uses to snap its
    view to "today → last scheduled day". Raw sheet rows extend to the end of the
    academic year even where no daily shifts have been drafted yet — using the raw
    max would overshoot into empty months.
    """
    try:
        schedule = load_schedule()
        dates = sorted(d for d in schedule.keys() if not d.startswith('_'))
        if not dates:
            return jsonify({'min_date': None, 'max_date': None})
        RVU_SHIFTS = {'Inpatient A', 'Inpatient B', 'Outpatient A', 'Outpatient B',
                      'Flex/Nights', 'Flex'}
        max_worked = None
        for d in dates:
            entry = schedule[d]
            if not isinstance(entry, dict):
                continue
            for k, v in entry.items():
                if k.startswith('ATT') and v in RVU_SHIFTS:
                    max_worked = d
                    break
        return jsonify({'min_date': dates[0], 'max_date': max_worked or dates[-1]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/schedule/full', methods=['GET'])
@auth_module.require_admin_or_demo
def api_schedule_full():
    """Full per-day schedule between start_date and end_date inclusive."""
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')

        if not start_date or not end_date:
            return jsonify({'error': 'start_date and end_date are required'}), 400

        schedule = load_schedule()
        neuro_config = load_neuro_config()
        results = get_full_schedule(schedule, start_date, end_date, neuro_config)
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/me/pay-projection', methods=['GET'])
def api_me_pay_projection():
    """Per-attending salary projection for the current FY based on the schedule + historical
    after-hours rate. Individual users get their own data; admins can pass any attending_id.

    Components included:
      - TNS (monthly_tns × 12, with FTE-weighting if fte_history applies)
      - TAT (5% of TNS)
      - Section bonus (4% of TNS — assumes section meets benchmark)
      - $/RVU after-hours bonus (past actual + projected future at recent daily rate)
      - Weekday evening ER pay (count of Flex/Nights × $1,840)
      - Weekend ER pay (user-modeled count × $2,080)

    Returns the per-day schedule list too so the UI can render it with hover details.

    Query params:
      attending_id     — required for admins; ignored for individuals (forced to session)
      weekend_er_count — slider value; default 0
    """
    try:
        u = auth_module.current_user()
        # Feature-flagged off for individuals by default — admins can toggle in Settings.
        if u.get('role') == 'individual' and not load_feature_flags().get('pay_projection_visible_to_individuals'):
            return jsonify({'error': 'pay projection is not enabled for individual users'}), 403
        if u.get('role') == 'individual':
            attending_id = u.get('attending_id')
        else:
            attending_id = request.args.get('attending_id')
        if not attending_id:
            return jsonify({'error': 'attending_id required'}), 400
        try:
            weekend_er_count = int(request.args.get('weekend_er_count', '0'))
        except ValueError:
            weekend_er_count = 0

        from datetime import date as _date_t, timedelta as _td
        today = _date_t.today()

        # Determine which fiscal years have any schedule data so we can offer them in a
        # dropdown and pick a sensible default. FY = Jul–Jun, labeled by start year.
        try:
            sched_for_fys = load_schedule()
            sched_dates = sorted(d for d in sched_for_fys.keys() if not d.startswith('_'))
        except Exception:
            sched_dates = []
        available_fys = []
        for d in sched_dates:
            y, m = int(d[:4]), int(d[5:7])
            fy_y = y if m >= 7 else y - 1
            if fy_y not in available_fys:
                available_fys.append(fy_y)
        available_fys.sort()

        # FY containing today, fallback if no schedule data.
        today_fy = today.year if today.month >= 7 else today.year - 1
        # Smart default: if today is in the last 30 days of the current FY AND the NEXT FY
        # has scheduled data, prefer the next FY so users planning ahead see future shifts
        # without manually flipping. Override via ?fy=YYYY.
        fy_end_of_today = _date_t(today_fy + 1, 6, 30)
        days_until_fy_end = (fy_end_of_today - today).days
        default_fy = today_fy
        if days_until_fy_end <= 30 and (today_fy + 1) in available_fys:
            default_fy = today_fy + 1

        try:
            fy_start_year = int(request.args.get('fy', default_fy))
        except (TypeError, ValueError):
            fy_start_year = default_fy
        fy_start = _date_t(fy_start_year, 7, 1)
        fy_end = _date_t(fy_start_year + 1, 6, 30)

        neuro_cfg = load_neuro_config()
        att_info = (neuro_cfg.get('attendings') or {}).get(attending_id)
        if not att_info:
            return jsonify({'error': f'attending {attending_id} not in config'}), 404
        attending_name = att_info.get('name', attending_id)
        monthly_tns = float(att_info.get('monthly_tns', 38000))
        dollars_per_rvu = float((neuro_cfg.get('moonlight_pay') or {}).get('default_dollars_per_rvu', 51))
        weekday_er_pay = 1840.0
        weekend_er_pay = 2080.0
        section_bonus_pct = 4.0  # assume section meets benchmark
        tat_pct = 5.0

        # Max data date so we can split FY into past vs future at the right boundary.
        con = get_connection(get_db_path())
        max_row = con.execute(
            "SELECT MAX(CAST(report_finalized_date AS DATE)) FROM exams WHERE division='NEURO'"
        ).fetchone()
        max_data_date = max_row[0] if max_row and max_row[0] else today
        if isinstance(max_data_date, str):
            max_data_date = _date_t.fromisoformat(max_data_date)
        # Past = FY start through max_data_date; Future = next day through FY end.
        past_end = min(max_data_date, fy_end)
        future_start = past_end + _td(days=1)

        # Past actuals
        daily_actuals = get_daily_rvus_for_attending(
            con, attending_id, fy_start.isoformat(), past_end.isoformat(),
            date_field='report_finalized_date',
        ) if past_end >= fy_start else []
        actuals_by_date = {r['date']: r for r in daily_actuals}
        past_total_rvu = sum(r['total_rvu'] for r in daily_actuals)
        past_daytime_rvu = sum(r['daytime_rvu'] for r in daily_actuals)
        past_after_hours_rvu = sum(r['after_hours_rvu'] for r in daily_actuals)

        # weekend_er_shifts_past is computed below from the per-day walk so it stays
        # consistent with the per_day is_weekend_er_coverer flags surfaced to the UI.

        # Historical daily after-hours rate (last 92 days of data) — used to extrapolate
        # future after-hours wRVU. Simple per-calendar-day average.
        rate_window_start = max_data_date - _td(days=92)
        rate_rows = get_daily_rvus_for_attending(
            con, attending_id, rate_window_start.isoformat(), max_data_date.isoformat(),
            date_field='report_finalized_date',
        )
        rate_after_hours_sum = sum(r['after_hours_rvu'] for r in rate_rows)
        rate_days = (max_data_date - rate_window_start).days + 1
        after_hours_rate_per_day = rate_after_hours_sum / rate_days if rate_days > 0 else 0.0

        # Pull schedule and section per-shift averages
        try:
            from .schedule import get_full_schedule
            sched_dict = load_schedule()
            full_sched = get_full_schedule(sched_dict, fy_start.isoformat(), fy_end.isoformat(), neuro_cfg)
            sched_by_date = {d['date']: d for d in full_sched}
        except Exception as ex:
            print(f"[pay-projection] schedule load failed ({ex})")
            sched_by_date = {}

        try:
            avg_start_date = max_data_date - _td(days=92)
            avg_rows = _shift_rvu_averages_with_schedule_counts(
                avg_start_date.isoformat(), max_data_date.isoformat(), 'report_finalized_date'
            )
            shift_avg = {r['shift_name']: r['avg_rvu'] for r in avg_rows}
        except Exception:
            shift_avg = {}

        # Per-date Weekend ER assignments — lets the UI flag past WE ER days with the
        # $2,080 per-shift bonus in the calendar tooltip.
        try:
            from .ingest import load_weekend_er_assignments
            from .config import CONFIG_DIR
            we_er_by_date = load_weekend_er_assignments(CONFIG_DIR)
        except Exception as ex:
            print(f"[pay-projection] weekend ER load failed: {ex}")
            we_er_by_date = {}

        IA_PRESHIFT_BONUS = 32
        CALL_DAY_RVU = 70
        WORK_SHIFTS = {'Inpatient A','Inpatient B','Outpatient A','Outpatient B','Flex','Flex/Nights'}
        # Flex* hack: Chang's 20% clinical is bought out by the hospital, so he's listed as
        # 'Flex*' weekly for bookkeeping but a different attending actually covers the shift.
        # Both attendings appear with 'Flex*' on the same day; the non-Chang one earns a
        # $2000 pay-out and gets normal Flex RVU volume. Chang's Flex* projects 0.
        CHANG_ID = 'ATT000767'
        FLEX_STAR_PAY_PER_SHIFT = 2000.0

        # Walk every day of the FY, build the per-day schedule entry.
        per_day = []
        weekday_er_count_past = 0
        weekday_er_count_future = 0
        future_scheduled_rvu = 0.0
        flex_star_shifts_past = 0
        flex_star_shifts_future = 0
        weekend_er_shifts_past = 0
        # Future weekdays that have NO assignment for this attending — used to estimate
        # additional Flex/Nights based on historical pace when the schedule isn't published yet.
        future_unscheduled_weekdays = 0
        cur = fy_start
        while cur <= fy_end:
            iso = cur.isoformat()
            sched_entry = sched_by_date.get(iso) or {}
            assignments_today = sched_entry.get('assignments') or {}
            assignment = assignments_today.get(attending_id)
            on_call = sched_entry.get('on_call') == attending_id
            is_past = cur <= past_end
            is_holiday = bool(sched_entry.get('is_holiday'))
            is_weekend = bool(sched_entry.get('is_weekend'))

            # Is this attending the non-Chang 'Flex*' coverer today?
            is_flex_star_coverer = (
                assignment == 'Flex*'
                and attending_id != CHANG_ID
                and any(att_id != CHANG_ID and shift == 'Flex*'
                        for att_id, shift in assignments_today.items())
            )
            # Is this attending the assigned Weekend ER coverer for this date?
            is_weekend_er_coverer = (
                (is_weekend or is_holiday)
                and we_er_by_date.get(iso) == attending_id
            )

            entry = {
                'date': iso,
                'weekday': cur.strftime('%a'),
                'shift': assignment,
                'on_call': on_call,
                'is_holiday': is_holiday,
                'is_weekend': is_weekend,
                'is_past': is_past,
                'is_weekend_er_coverer': is_weekend_er_coverer,
            }
            if is_past:
                a = actuals_by_date.get(iso)
                entry['actual_rvu'] = round(a['total_rvu'], 1) if a else 0.0
                entry['actual_daytime_rvu'] = round(a['daytime_rvu'], 1) if a else 0.0
                entry['actual_after_hours_rvu'] = round(a['after_hours_rvu'], 1) if a else 0.0
                entry['actual_exam_count'] = a['exam_count'] if a else 0
                if assignment == 'Flex/Nights' and not is_weekend and not is_holiday:
                    weekday_er_count_past += 1
                if is_flex_star_coverer:
                    flex_star_shifts_past += 1
                if is_weekend_er_coverer:
                    weekend_er_shifts_past += 1
            else:
                proj_rvu = 0.0
                if assignment in WORK_SHIFTS:
                    proj_rvu = shift_avg.get(assignment, 0.0)
                    if assignment == 'Inpatient A' and not is_holiday and not is_weekend:
                        proj_rvu += IA_PRESHIFT_BONUS
                elif is_flex_star_coverer:
                    # Treat as a regular Flex day for RVU projection.
                    proj_rvu = shift_avg.get('Flex', 0.0)
                elif (is_weekend or is_holiday) and on_call:
                    proj_rvu = CALL_DAY_RVU
                entry['projected_rvu'] = round(proj_rvu, 1)
                future_scheduled_rvu += proj_rvu
                if assignment == 'Flex/Nights' and not is_weekend and not is_holiday:
                    weekday_er_count_future += 1
                if is_flex_star_coverer:
                    flex_star_shifts_future += 1
                # No assignment AND it's a weekday → eligible to be filled in later. We use
                # the count of these days to allocate the historical-rate estimate.
                if assignment is None and not is_weekend and not is_holiday:
                    future_unscheduled_weekdays += 1
            per_day.append(entry)
            cur = cur + _td(days=1)

        # Historical weekday-evening-ER rate: distinct Flex/Nights weekday dates this attending
        # covered over the past 365 days. Used to estimate additional shifts when the FY's
        # schedule is only partially published (most of FY 26-27 is blank, for example).
        hist_window_start = max_data_date - _td(days=365)
        hist_we_row = con.execute("""
            SELECT COUNT(DISTINCT CAST(report_finalized_date AS DATE))
            FROM exams
            WHERE division='NEURO'
              AND report_finalized_by = ?
              AND shift_name = 'Flex/Nights'
              AND CAST(report_finalized_date AS DATE) BETWEEN ? AND ?
        """, [attending_id, hist_window_start.isoformat(), max_data_date.isoformat()]).fetchone()
        hist_weekday_er_per_year = int(hist_we_row[0] or 0)

        # If the FY's scheduled-so-far weekday ER count is below the attending's historical
        # annual count AND there are unscheduled weekdays left to absorb the difference,
        # estimate the shortfall as additional shifts. Capped by future_unscheduled_weekdays
        # so we don't over-credit when the schedule IS fully filled in.
        scheduled_we_in_fy = weekday_er_count_past + weekday_er_count_future
        shortfall = max(0, hist_weekday_er_per_year - scheduled_we_in_fy)
        weekday_er_count_estimated = min(shortfall, future_unscheduled_weekdays)

        weekday_er_count_total = (
            weekday_er_count_past + weekday_er_count_future + weekday_er_count_estimated
        )
        flex_star_shifts_total = flex_star_shifts_past + flex_star_shifts_future

        # Future after-hours wRVU = days_remaining × historical daily rate.
        future_days = (fy_end - future_start).days + 1 if future_start <= fy_end else 0
        projected_future_after_hours_rvu = max(0.0, after_hours_rate_per_day * future_days)
        total_after_hours_rvu = past_after_hours_rvu + projected_future_after_hours_rvu

        # TNS — full annual salary regardless of FTE. Per the comp model, monthly_tns is already
        # the agreed monthly figure for this attending, so the annual is just × 12.
        tns_total = monthly_tns * 12
        tat_total = tns_total * tat_pct / 100.0
        section_bonus_total = tns_total * section_bonus_pct / 100.0
        after_hours_pay_total = total_after_hours_rvu * dollars_per_rvu
        weekday_er_pay_total = weekday_er_count_total * weekday_er_pay
        weekend_er_pay_total = weekend_er_count * weekend_er_pay
        flex_star_pay_total = flex_star_shifts_total * FLEX_STAR_PAY_PER_SHIFT
        salary_total = (tns_total + tat_total + section_bonus_total
                        + after_hours_pay_total + weekday_er_pay_total + weekend_er_pay_total
                        + flex_star_pay_total)

        con.close()
        return jsonify({
            'attending_id': attending_id,
            'attending_name': attending_name,
            'fy_start': fy_start.isoformat(),
            'fy_end': fy_end.isoformat(),
            'fy_start_year': fy_start_year,
            'available_fys': available_fys,
            'past_end': past_end.isoformat(),
            'weekend_er_count_modeled': weekend_er_count,
            'components': {
                'tns': {
                    'monthly_rate': round(monthly_tns, 2),
                    'months': 12,
                    'annual': round(tns_total, 2),
                },
                'tat': {
                    'rate_pct': tat_pct,
                    'annual': round(tat_total, 2),
                },
                'section_bonus': {
                    'rate_pct': section_bonus_pct,
                    'annual': round(section_bonus_total, 2),
                    'note': 'assumes section meets benchmark',
                },
                'after_hours_pay': {
                    'past_rvu': round(past_after_hours_rvu, 1),
                    'projected_future_rvu': round(projected_future_after_hours_rvu, 1),
                    'total_rvu': round(total_after_hours_rvu, 1),
                    'rate_per_rvu': dollars_per_rvu,
                    'historical_rate_per_day': round(after_hours_rate_per_day, 2),
                    'annual': round(after_hours_pay_total, 2),
                },
                'weekday_er_pay': {
                    'shifts_past': weekday_er_count_past,
                    'shifts_future': weekday_er_count_future,
                    'shifts_estimated': weekday_er_count_estimated,
                    'shifts_total': weekday_er_count_total,
                    'rate_per_shift': weekday_er_pay,
                    'historical_annual_count': hist_weekday_er_per_year,
                    'annual': round(weekday_er_pay_total, 2),
                },
                'weekend_er_pay': {
                    # 'shifts_modeled' is the total annual count the slider represents.
                    # 'shifts_past' is how many of those have actually happened (set the
                    # slider initial value to this so the projection starts realistic).
                    'shifts_modeled': weekend_er_count,
                    'shifts_past': weekend_er_shifts_past,
                    'rate_per_shift': weekend_er_pay,
                    'annual': round(weekend_er_pay_total, 2),
                },
                'flex_star_pay': {
                    'shifts_past': flex_star_shifts_past,
                    'shifts_future': flex_star_shifts_future,
                    'shifts_total': flex_star_shifts_total,
                    'rate_per_shift': FLEX_STAR_PAY_PER_SHIFT,
                    'annual': round(flex_star_pay_total, 2),
                },
            },
            'salary_total': round(salary_total, 2),
            'per_day': per_day,
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'traceback': traceback.format_exc().splitlines()[-8:]}), 500


@app.route('/api/me/after-hours-cases.csv', methods=['GET'])
def api_me_after_hours_csv():
    """Download CSV of after-hours cases for an attending in a date range.

    Same after-hours definition as the My Stats split: reads outside Mon–Fri 8am–5pm PST,
    excluding Weekend Call and evening ER. Individual users get their own data only; admins
    can pass any `attending_id`.

    Columns: date_finalized, time_finalized, cpt_code, exam_description, wrvu.
    """
    u = auth_module.current_user()  # before_request guarantees authed
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    if u.get('role') == 'individual':
        attending_id = u.get('attending_id')
    else:
        attending_id = request.args.get('attending_id')
    if not attending_id or not start_date or not end_date:
        return jsonify({'error': 'attending_id, start_date, end_date are required'}), 400

    from .queries import _AFTER_HOURS_PREDICATE
    con = get_connection(get_db_path())
    try:
        rows = con.execute(f"""
            SELECT
                report_finalized_date,
                cpt_code,
                exam_description,
                work_professional_rvu
            FROM exams
            WHERE division = 'NEURO'
              AND report_finalized_by = '{attending_id.replace(chr(39), chr(39) + chr(39))}'
              AND report_finalized_date >= '{start_date}'
              AND report_finalized_date < '{end_date}'::DATE + INTERVAL '1 day'
              AND {_AFTER_HOURS_PREDICATE}
            ORDER BY report_finalized_date
        """).fetchall()
    finally:
        con.close()

    import io, csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['date_finalized', 'time_finalized', 'cpt_code', 'exam_description', 'wrvu'])
    total_rvu = 0.0
    for fin, cpt, desc, rvu in rows:
        date_str = fin.strftime('%Y-%m-%d') if fin else ''
        time_str = fin.strftime('%H:%M') if fin else ''
        w.writerow([
            date_str, time_str,
            cpt or '',
            desc or '',
            f"{rvu:.2f}" if rvu is not None else '0.00',
        ])
        if rvu is not None:
            total_rvu += rvu
    # Trailing total row — blank line separator then labeled summary so it's obvious in Excel.
    w.writerow([])
    w.writerow(['', '', '', f'TOTAL ({len(rows)} cases)', f'{total_rvu:.2f}'])

    filename = f"after-hours-{attending_id}-{start_date}-to-{end_date}.csv"
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@app.route('/api/me/stats', methods=['GET'])
def api_me_stats():
    """Per-attending productivity vs FTE-prorated 65th-percentile benchmark.

    Powers the "My Stats" view. Authorization:
      - Individual users: `attending_id` is FORCED to their session attending_id regardless
        of what they pass in the query — this is the load-bearing check that prevents one
        attending from viewing another's stats.
      - Admin users: may pass any `attending_id` (used for impersonation in the admin preview).

    Query params:
        attending_id: ATT id (required for admins; ignored for individuals).
        start_date, end_date: ISO date strings (required).
        date_field: 'exam_completed_date' or 'report_finalized_date' (default: report_finalized_date)
    """
    try:
        u = auth_module.current_user()  # before_request already guaranteed it's non-None
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        date_field = request.args.get('date_field', 'report_finalized_date')

        if u.get('role') == 'individual':
            # Hard override — never trust the query param for non-admins.
            attending_id = u.get('attending_id')
            if not attending_id:
                return jsonify({'error': 'no attending_id bound to your account; contact admin'}), 403
        else:
            attending_id = request.args.get('attending_id')

        if not attending_id or not start_date or not end_date:
            return jsonify({'error': 'attending_id, start_date, and end_date are required'}), 400

        neuro_config = load_neuro_config()
        att_info = (neuro_config.get('attendings') or {}).get(attending_id)
        if not att_info:
            return jsonify({'error': f'Attending {attending_id} not found in neuro config'}), 404

        annual_65 = float(neuro_config.get('annual_rvu_expectation', 10179))
        # $/RVU rate paid on after-hours wRVU under the new comp plan. Sourced from neuro_config
        # (key `moonlight_pay.default_dollars_per_rvu`) so it's tunable without code changes.
        # The legacy "moonlight_pay" key is reused — it now applies to all after-hours work, not
        # just moonlight-tagged exams.
        dollars_per_rvu = float((neuro_config.get('moonlight_pay') or {}).get('default_dollars_per_rvu', 51))
        attending_name = att_info.get('name', attending_id)
        # `fte` (single value) is kept for the response payload — it's the CURRENT FTE as of
        # the end of the period. Per-month targets below use `get_fte_for_date` so the math
        # respects historical FTE changes (e.g., attending went from 1.0 → 0.8 mid-FY).
        from datetime import date as _date_t, timedelta as _td
        s_dt = _date_t.fromisoformat(start_date)
        e_dt = _date_t.fromisoformat(end_date)
        fte = get_fte_for_date(att_info, e_dt)
        period_days = (e_dt - s_dt).days + 1
        period_months = period_days / (365.25 / 12)

        con = get_connection(get_db_path())
        try:
            monthly = get_monthly_rvus_for_attending(
                con, attending_id, start_date, end_date, date_field=date_field
            )
            # $/RVU-bonus qualification (current + next quarter) — same shared helper the
            # Compensation Review tab uses, so My Stats shows the identical numbers.
            qual = compute_qualification(con, attending_id, att_info, annual_65)
        finally:
            con.close()

        # Sum actual past production from the monthly query, split into daytime / after-hours.
        actual_by_month = {r['month']: float(r['total_rvu']) for r in monthly}
        daytime_by_month = {r['month']: float(r['daytime_rvu']) for r in monthly}
        after_hours_by_month = {r['month']: float(r['after_hours_rvu']) for r in monthly}
        total_rvus = sum(actual_by_month.values())
        total_daytime_rvus = sum(daytime_by_month.values())
        total_after_hours_rvus = sum(after_hours_by_month.values())

        # FY containing end_date (Jul–Jun).
        fy_start_year = e_dt.year if e_dt.month >= 7 else e_dt.year - 1
        fy_start = _date_t(fy_start_year, 7, 1)
        fy_end = _date_t(fy_start_year + 1, 6, 30)

        # Per-day FTE-prorated target — handles start_date / end_date / fte_history mid-month
        # changes correctly (each day contributes based on its own FTE).
        target_rvus = get_period_target(att_info, annual_65, s_dt, e_dt)
        pct_of_target = (total_rvus / target_rvus * 100) if target_rvus > 0 else None
        annual_target = get_period_target(att_info, annual_65, fy_start, fy_end)

        # ----- Schedule-aware projection from (e_dt+1) to fy_end -----
        # For each future day:
        #   - If the day appears in the schedule and the attending has a work shift, add the
        #     section average wRVU for that shift (+32 IA pre-shift moonlight on non-holiday IA days).
        #   - If the day appears in the schedule and the attending is on_call (weekend/holiday),
        #     add the constant per-call-day RVU.
        #   - If the day appears in the schedule but the attending has a status (Vacation/Academic/
        #     Sick/etc.), contribute 0.
        #   - If the day is NOT in the schedule at all (e.g., schedule hasn't been entered yet),
        #     fall back to historical daily pace.
        # Bucket projected RVUs by month so the per-month table can show actual + projected per row.
        projection = None
        sched_future = 0.0
        unsched_future = 0.0
        scheduled_workdays = 0
        unscheduled_days = 0
        projected_by_month = {}  # month_key → projected wRVU
        proj_start = e_dt + _td(days=1)
        if proj_start <= fy_end:
            # Pull schedule for the projection window.
            try:
                sched_dict = load_schedule()
                full = get_full_schedule(sched_dict, proj_start.isoformat(), fy_end.isoformat(), neuro_config)
                # Index by date for O(1) lookups.
                sched_by_date = {d['date']: d for d in full}
            except Exception as ex:
                print(f"[me/stats] schedule load failed for projection ({ex})")
                sched_by_date = {}

            # Pull recent section averages (last 3 months ending at max data date) — same source
            # the solver uses. Map "Inpatient A" → its avg, etc.
            try:
                from .database import get_connection as _gc
                _con = _gc()
                _max_row = _con.execute(
                    "SELECT MAX(CAST(report_finalized_date AS DATE)) FROM exams WHERE division='NEURO'"
                ).fetchone()
                _max_data_date = _max_row[0] if _max_row and _max_row[0] else _date_t.today()
                _con.close()
                if isinstance(_max_data_date, str):
                    _max_data_date = _date_t.fromisoformat(_max_data_date)
                avg_start_date = _max_data_date - _td(days=92)
                avg_rows = _shift_rvu_averages_with_schedule_counts(
                    avg_start_date.isoformat(), _max_data_date.isoformat(), 'report_finalized_date'
                )
                shift_avg_map = {r['shift_name']: r['avg_rvu'] for r in avg_rows}
            except Exception as ex:
                print(f"[me/stats] shift averages fetch failed ({ex})")
                shift_avg_map = {}

            IA_MOONLIGHT_BONUS = 32   # IA pre-shift overnight reads — added on non-holiday IA days
            CALL_DAY_RVU = 70         # fallback per-call-day credit if we can't compute a per-attending average
            ANNUAL_CALL_DAYS = 10     # expected total call days per attending per FY (rough — same for everyone for now)

            # Historical rates over a ROLLING 92-day window, ignoring FY boundaries.
            # FY-to-date-only pace is unusable early in a new FY (July 6 of FY26-27 = 6
            # calendar days, half of which are weekend/holiday → daily pace ≈ 0). The
            # rolling window stays representative regardless of when the projection runs.
            #
            # daily_pace excludes historical call-day production from BOTH numerator and
            # denominator so we can credit future call days separately (from a per-attending
            # avg_call_day_rvu) without double-counting.
            daily_pace = 0.0
            avg_call_day_rvu = CALL_DAY_RVU
            after_hours_rate_per_day = 0.0
            weekend_er_rate_per_day = 0.0
            try:
                from .database import get_connection as _gc3
                _pcon = _gc3()
                pace_end = min(e_dt, _max_data_date)
                pace_start = pace_end - _td(days=92)
                att_esc = attending_id.replace(chr(39), chr(39)+chr(39))
                # One round trip for every rate we need:
                #  - non_call_rvu / non_call_days      → daily_pace (for "everything else" days)
                #  - call_rvu, call_days               → avg_call_day_rvu
                #  - after_hours_rvu                   → after_hours_rate_per_day (time-based
                #    predicate — matches queries._AFTER_HOURS_PREDICATE, so it's the same
                #    definition used for $/RVU pay eligibility).
                #  - weekend_er_rvu                    → weekend_er_rate_per_day (still counts
                #    toward total production but is paid per-shift, so tracked separately).
                #  - ia_weekday_days                   → for subtracting the IA_MOONLIGHT_BONUS
                #    portion out of after_hours_rvu (avoids double-counting when we credit
                #    the +32 bonus explicitly on future IA weekdays).
                r = _pcon.execute(f"""
                    SELECT
                      COALESCE(SUM(CASE WHEN shift_name IS DISTINCT FROM 'Weekend Call'
                                        THEN work_professional_rvu ELSE 0 END), 0) AS non_call_rvu,
                      COALESCE(SUM(CASE WHEN shift_name = 'Weekend Call'
                                        THEN work_professional_rvu ELSE 0 END), 0) AS call_rvu,
                      COUNT(DISTINCT CASE WHEN shift_name = 'Weekend Call'
                                          THEN CAST(report_finalized_date AS DATE) END) AS call_days,
                      -- Use shared predicate so this matches queries._AFTER_HOURS_PREDICATE
                      -- exactly (including the holiday list).
                      COALESCE(SUM(CASE WHEN {_AFTER_HOURS_PREDICATE}
                          THEN work_professional_rvu ELSE 0 END), 0) AS after_hours_rvu,
                      COALESCE(SUM(CASE WHEN shift_name = 'Weekend Evening ER'
                                        THEN work_professional_rvu ELSE 0 END), 0) AS weekend_er_rvu,
                      COUNT(DISTINCT CASE WHEN shift_name = 'Inpatient A'
                                          AND EXTRACT(dow FROM report_finalized_date) NOT IN (0, 6)
                                          THEN CAST(report_finalized_date AS DATE) END) AS ia_weekday_days
                    FROM exams
                    WHERE division = 'NEURO'
                      AND report_finalized_by = '{att_esc}'
                      AND CAST(report_finalized_date AS DATE) BETWEEN '{pace_start.isoformat()}' AND '{pace_end.isoformat()}'
                """).fetchone()
                _pcon.close()
                non_call_rvu = float(r[0] or 0)
                hist_call_rvu = float(r[1] or 0)
                hist_call_days = int(r[2] or 0)
                after_hours_rvu = float(r[3] or 0)
                weekend_er_rvu = float(r[4] or 0)
                ia_weekday_days = int(r[5] or 0)

                pace_calendar_days = (pace_end - pace_start).days + 1
                # daily_pace denominator excludes call days — this is the pace for a
                # non-call day, applied to "everything else" future days.
                non_call_days = max(1, pace_calendar_days - hist_call_days)
                daily_pace = non_call_rvu / non_call_days
                if hist_call_days > 0:
                    avg_call_day_rvu = hist_call_rvu / hist_call_days
                # Subtract the explicit IA pre-shift bonus from historical after-hours so we
                # don't double-count when the projection loop adds +32 on future IA days.
                after_hours_net = max(0.0, after_hours_rvu - ia_weekday_days * IA_MOONLIGHT_BONUS)
                after_hours_rate_per_day = after_hours_net / pace_calendar_days
                weekend_er_rate_per_day = weekend_er_rvu / pace_calendar_days
            except Exception as ex:
                print(f"[me/stats] pace calc failed, falling back to FY-to-date ({ex})")
                past_days = (e_dt - s_dt).days + 1
                daily_pace = (total_rvus / past_days) if past_days > 0 else 0.0

            # Count past call days this FY so we know how many of the yearly quota have
            # already been served.
            past_call_days = 0
            try:
                from .database import get_connection as _gc4
                _ccon = _gc4()
                r = _ccon.execute(f"""
                    SELECT COUNT(DISTINCT CAST(report_finalized_date AS DATE))
                    FROM exams
                    WHERE division = 'NEURO'
                      AND report_finalized_by = '{attending_id.replace(chr(39), chr(39)+chr(39))}'
                      AND shift_name = 'Weekend Call'
                      AND CAST(report_finalized_date AS DATE) BETWEEN '{s_dt.isoformat()}' AND '{e_dt.isoformat()}'
                """).fetchone()
                _ccon.close()
                past_call_days = int(r[0] or 0)
            except Exception as ex:
                print(f"[me/stats] past call-day count failed ({ex})")

            # after_hours_rate_per_day and weekend_er_rate_per_day are computed in the
            # combined pace query above (single round trip). See the SQL there for details.
            # after_hours uses the time-based predicate (matches _AFTER_HOURS_PREDICATE), so
            # $/RVU pay eligibility and My Stats projection use the same definition.
            extra_rate_per_day = after_hours_rate_per_day + weekend_er_rate_per_day

            # Assignment strings that mean "definitely not producing" — no shift, no moonlight.
            # Everything ELSE that isn't a work shift or an on-call weekend/holiday falls
            # through to daily_pace (which now excludes call-day production so we can credit
            # future call days separately without double-counting).
            OFF_STATUSES = {
                'Vacation', 'Academic', 'Conference', 'Sick', 'Leave',
                'UCSF', 'Blocked', 'No Call',
            }
            cur = proj_start
            # Track after-hours and weekend ER separately in the projection so the response
            # can show each. Both add to scheduled-shift + call days (daily_pace already
            # includes them for "everything else" days).
            sched_after_hours_future = 0.0
            sched_weekend_er_future = 0.0
            future_scheduled_call_days = 0
            while cur <= fy_end:
                day_iso = cur.isoformat()
                day_mk = f'{cur.year:04d}-{cur.month:02d}'
                day = sched_by_date.get(day_iso)
                day_rvu = 0.0
                assignments = (day or {}).get('assignments') or {}
                on_call = (day or {}).get('on_call')
                assigned = assignments.get(attending_id)

                if assigned in shift_avg_map:
                    # Real work shift — best-signal projection.
                    day_rvu = shift_avg_map[assigned]
                    if assigned == 'Inpatient A' and not day.get('is_holiday') and not day.get('is_weekend'):
                        day_rvu += IA_MOONLIGHT_BONUS
                    scheduled_workdays += 1
                    sched_future += day_rvu
                    day_rvu += extra_rate_per_day
                    sched_after_hours_future += after_hours_rate_per_day
                    sched_weekend_er_future += weekend_er_rate_per_day
                elif day is not None and (day.get('is_weekend') or day.get('is_holiday')) and on_call == attending_id:
                    # Weekend/holiday call — per-attending avg call-day credit + extras.
                    day_rvu = avg_call_day_rvu
                    scheduled_workdays += 1
                    future_scheduled_call_days += 1
                    sched_future += day_rvu
                    day_rvu += extra_rate_per_day
                    sched_after_hours_future += after_hours_rate_per_day
                    sched_weekend_er_future += weekend_er_rate_per_day
                elif assigned in OFF_STATUSES:
                    # Attending is explicitly off. Contributes zero.
                    day_rvu = 0.0
                else:
                    # No shift, no call, no explicit off-status — either the schedule day
                    # isn't populated yet, or this attending's cell is blank/weekend-marker.
                    # daily_pace (non-call rate) is the honest estimate; it already contains
                    # after-hours + weekend ER in its historical numerator.
                    day_rvu = daily_pace
                    unsched_future += day_rvu
                    unscheduled_days += 1
                if day_rvu > 0:
                    projected_by_month[day_mk] = projected_by_month.get(day_mk, 0.0) + day_rvu
                cur = cur + _td(days=1)

            # Unscheduled future call days: allocate the remainder of the ANNUAL_CALL_DAYS
            # quota to the projection. Assumes call rotation is roughly fixed per attending
            # over the FY. Credits per-attending avg_call_day_rvu for each remaining day.
            unscheduled_call_days_future = max(
                0, ANNUAL_CALL_DAYS - past_call_days - future_scheduled_call_days
            )
            unscheduled_call_rvu_future = unscheduled_call_days_future * avg_call_day_rvu

            projected_year_end = (
                total_rvus + sched_future
                + sched_after_hours_future + sched_weekend_er_future
                + unsched_future + unscheduled_call_rvu_future
            )
            # Fold the projected extra call days into the per-month table too, distributing
            # them evenly across the projection window so the monthly pace looks smooth.
            if unscheduled_call_rvu_future > 0 and projected_by_month:
                per_month_add = unscheduled_call_rvu_future / max(1, len(projected_by_month))
                for mk in projected_by_month:
                    projected_by_month[mk] += per_month_add
            projection = {
                'fy_start': fy_start.isoformat(),
                'fy_end': fy_end.isoformat(),
                'annual_target': round(annual_target, 1),
                'past_rvus': round(total_rvus, 1),
                'scheduled_future_rvus': round(sched_future, 1),
                # Renamed from projected_moonlight_rvus. Uses the time-based after-hours
                # predicate — same definition as $/RVU pay eligibility on Pay Projection.
                'projected_after_hours_rvus': round(sched_after_hours_future, 1),
                'after_hours_rate_per_day': round(after_hours_rate_per_day, 2),
                # Weekend Evening ER wRVU are paid per-shift ($2,080), but still count
                # toward total production, so tracked as a separate line.
                'projected_weekend_er_rvus': round(sched_weekend_er_future, 1),
                'weekend_er_rate_per_day': round(weekend_er_rate_per_day, 2),
                'unscheduled_future_rvus': round(unsched_future, 1),
                'projected_year_end': round(projected_year_end, 1),
                'pct_of_annual': round(projected_year_end / annual_target * 100, 1) if annual_target > 0 else None,
                'scheduled_workdays_remaining': scheduled_workdays,
                'unscheduled_days_remaining': unscheduled_days,
                'daily_pace': round(daily_pace, 1),
                # Call-day breakdown so the UI (or a diagnostic) can show it.
                'past_call_days': past_call_days,
                'scheduled_call_days_future': future_scheduled_call_days,
                'unscheduled_call_days_future': unscheduled_call_days_future,
                'avg_call_day_rvu': round(avg_call_day_rvu, 1),
                'annual_call_days_assumed': ANNUAL_CALL_DAYS,
                'unscheduled_call_rvu_future': round(unscheduled_call_rvu_future, 1),
            }

        # Build per-month table from start_date's month through fy_end. Each month entry has both
        # actual (past, from DB) and projected (future, from schedule walk above). Classify each
        # row as 'actual' (entirely past), 'projected' (entirely future), or 'mix' (straddles end_date).
        by_month = []
        e_mk = f'{e_dt.year:04d}-{e_dt.month:02d}'  # the month containing end_date
        y, m = s_dt.year, s_dt.month
        while (y, m) <= (fy_end.year, fy_end.month):
            mk = f'{y:04d}-{m:02d}'
            actual_rvu = actual_by_month.get(mk, 0.0)
            proj_rvu = projected_by_month.get(mk, 0.0)
            total_rvu = actual_rvu + proj_rvu
            # Per-day target across the full month — handles mid-month start_date / end_date /
            # fte_history exactly. Reported `fte` is the day-weighted average for that month.
            first_of_m = _date_t(y, m, 1)
            ny2, nm2 = (y + 1, 1) if m == 12 else (y, m + 1)
            last_of_m = _date_t(ny2, nm2, 1) - _td(days=1)
            month_target = get_period_target(att_info, annual_65, first_of_m, last_of_m)
            fte_this_month = get_month_fte_avg(att_info, y, m)
            pct = (total_rvu / month_target * 100) if month_target > 0 else None
            if mk == e_mk and proj_rvu > 0:
                kind = 'mix'
            elif mk > e_mk:
                kind = 'projected'
            else:
                kind = 'actual'
            after_hrs_rvu = after_hours_by_month.get(mk, 0.0)
            by_month.append({
                'month': mk,
                'kind': kind,
                'actual_rvus': round(actual_rvu, 1),
                'actual_daytime_rvus': round(daytime_by_month.get(mk, 0.0), 1),
                'actual_after_hours_rvus': round(after_hrs_rvu, 1),
                'after_hours_pay': round(after_hrs_rvu * dollars_per_rvu, 2),
                'projected_rvus': round(proj_rvu, 1),
                'rvus': round(total_rvu, 1),
                'target_rvus': round(month_target, 1),
                'pct_of_target': round(pct, 1) if pct is not None else None,
                'fte': fte_this_month,
            })
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            y, m = ny, nm

        return jsonify({
            'attending_id': attending_id,
            'attending_name': attending_name,
            'fte': fte,
            'annual_65th_rvu': annual_65,
            'period_start': start_date,
            'period_end': end_date,
            'period_months': round(period_months, 2),
            'total_rvus': round(total_rvus, 1),
            'total_daytime_rvus': round(total_daytime_rvus, 1),
            'total_after_hours_rvus': round(total_after_hours_rvus, 1),
            'total_after_hours_pay': round(total_after_hours_rvus * dollars_per_rvu, 2),
            'dollars_per_rvu': dollars_per_rvu,
            'target_rvus': round(target_rvus, 1),
            'pct_of_target': round(pct_of_target, 1) if pct_of_target is not None else None,
            'by_month': by_month,
            'projection': projection,
            **qual,  # qualified_for_rvu_bonus, is_new_hire, qualifying_* (curr + next)
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'traceback': traceback.format_exc().splitlines()[-8:]}), 500


@app.route('/api/compensation/moonlight-monthly', methods=['GET'])
@auth_module.require_admin_or_demo
def api_moonlight_compensation():
    """
    Get monthly moonlight compensation comparison data.

    Query params:
        start_date: ISO date string (required)
        end_date: ISO date string (required)
        date_field: 'exam_completed_date' or 'report_finalized_date' (default: exam_completed_date)
    """
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        date_field = request.args.get('date_field')

        if not start_date or not end_date:
            return jsonify({'error': 'start_date and end_date are required'}), 400

        neuro_config = load_neuro_config()
        schedule = load_schedule()
        con = get_connection(get_db_path())
        results = get_moonlight_compensation_model(
            con, start_date, end_date, neuro_config, schedule,
            date_field=date_field,
        )
        con.close()

        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _qualification_window(start_dt):
    """Return (window_start, window_end_exclusive) for the new 3-month qualification window.

    Eligibility for $/RVU bonus in the quarter containing start_dt is determined by RVUs in:
      - M3 of the quarter-before-previous, plus
      - M1 and M2 of the previous quarter.
    The month immediately preceding the current quarter (M3 of the previous quarter) is skipped
    to allow billing reconciliation time.

    Fiscal quarters: Q1=Jul-Sep, Q2=Oct-Dec, Q3=Jan-Mar, Q4=Apr-Jun.

    Examples (start_dt in current quarter → window):
      Q3 (Jan-Mar Y)  → Sep (Y-1) + Oct (Y-1) + Nov (Y-1)
      Q4 (Apr-Jun Y)  → Dec (Y-1) + Jan (Y) + Feb (Y)
      Q1 (Jul-Sep Y)  → Mar (Y) + Apr (Y) + May (Y)
      Q2 (Oct-Dec Y)  → Jun (Y) + Jul (Y) + Aug (Y)
    """
    from datetime import date
    y, m = start_dt.year, start_dt.month
    if 1 <= m <= 3:        # Q3 → window = Sep + Oct + Nov of Y-1
        return date(y - 1, 9, 1), date(y - 1, 12, 1)
    elif 4 <= m <= 6:      # Q4 → window = Dec (Y-1) + Jan (Y) + Feb (Y)
        return date(y - 1, 12, 1), date(y, 3, 1)
    elif 7 <= m <= 9:      # Q1 → window = Mar + Apr + May of Y
        return date(y, 3, 1), date(y, 6, 1)
    else:                  # Q2 (10-12) → window = Jun + Jul + Aug of Y
        return date(y, 6, 1), date(y, 9, 1)


def _next_quarter_start(d):
    """First day of the fiscal quarter AFTER the one containing d.
    Fiscal quarters: Q1=Jul-Sep, Q2=Oct-Dec, Q3=Jan-Mar, Q4=Apr-Jun."""
    from datetime import date
    m = d.month
    if 7 <= m <= 9:      return date(d.year, 10, 1)      # Q1 → Q2
    if 10 <= m <= 12:    return date(d.year + 1, 1, 1)   # Q2 → Q3 (next calendar year)
    if 1 <= m <= 3:      return date(d.year, 4, 1)       # Q3 → Q4
    return date(d.year, 7, 1)                            # Q4 → Q1 (next FY)


def compute_qualification(con, att_id, att_info, annual_65):
    """$/RVU-bonus qualification for the CURRENT fiscal quarter AND the next quarter.

    Anchored to TODAY, NOT the selected review period — the columns always answer "are they
    qualified right now, and on track for next quarter," regardless of which date range the
    user is looking at. (Anchoring on the review-range start meant a Jan-1 start showed the
    qualification window for Q3 instead of the quarter we're actually in.)

    Single source of truth, shared by /api/compensation/proposed-model and /api/me/stats so
    the two can't drift. Production is summed by exam_completed_date over each 3-month window
    (_qualification_window); the threshold is the FTE-prorated 65th-percentile over that window.
    New-hire grace: if the attending has no NEURO exams before the current window, they
    auto-qualify. The NEXT-quarter window may still be in progress, so its production is a
    running total.

    Returns a dict of qualifying_* fields ready to splice into an API response.
    """
    from datetime import timedelta as _td_q, datetime as _dt_q
    from zoneinfo import ZoneInfo as _ZI
    # "Current quarter" = the fiscal quarter containing today's Pacific date.
    anchor = _dt_q.now(_ZI("America/Los_Angeles")).date()

    def _window(qs, qe_excl):
        threshold = get_period_target(att_info, annual_65, qs, qe_excl - _td_q(days=1))
        row = con.execute(
            "SELECT COALESCE(SUM(work_professional_rvu), 0) FROM exams "
            "WHERE division = 'NEURO' AND report_finalized_by = ? "
            "AND exam_completed_date >= ? AND exam_completed_date < ?",
            [att_id, qs.isoformat(), qe_excl.isoformat()],
        ).fetchone()
        return float(row[0]), threshold

    qs, qe = _qualification_window(anchor)
    cur_rvus, cur_thresh = _window(qs, qe)
    pre = con.execute(
        "SELECT COUNT(*) FROM exams WHERE division = 'NEURO' AND report_finalized_by = ? "
        "AND exam_completed_date < ?",
        [att_id, qs.isoformat()],
    ).fetchone()
    is_new_hire = (pre[0] == 0)
    qualified = is_new_hire or (cur_rvus >= cur_thresh)

    nqs, nqe = _qualification_window(_next_quarter_start(anchor))
    next_rvus, next_thresh = _window(nqs, nqe)

    return {
        'qualified_for_rvu_bonus': qualified,
        'is_new_hire': is_new_hire,
        'qualifying_rvus': round(cur_rvus, 1),
        'qualifying_benchmark': round(cur_thresh, 1),
        'qualifying_window_start': qs.isoformat(),
        'qualifying_window_end': qe.isoformat(),
        'qualifying_rvus_next': round(next_rvus, 1),
        'qualifying_benchmark_next': round(next_thresh, 1),
        'qualifying_window_next_start': nqs.isoformat(),
        'qualifying_window_next_end': nqe.isoformat(),
    }


@app.route('/api/compensation/proposed-model', methods=['GET'])
@auth_module.require_admin_or_demo
def api_proposed_compensation():
    """
    Compute per-attending compensation under the proposed model.

    Query params:
        start_date: ISO date string (required)
        end_date: ISO date string (required)
        date_field: 'exam_completed_date' or 'report_finalized_date' (default: exam_completed_date)
    """
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        date_field = request.args.get('date_field')

        if not start_date or not end_date:
            return jsonify({'error': 'start_date and end_date are required'}), 400

        from .queries import _resolve_date_field
        df = _resolve_date_field(date_field)

        neuro_config = load_neuro_config()
        attendings = neuro_config.get('attendings', {})
        annual_70 = neuro_config.get('annual_rvu_expectation_70th', 10509)
        annual_65 = neuro_config.get('annual_rvu_expectation', 10179)

        from datetime import datetime as dt
        start_dt = dt.fromisoformat(start_date).date()
        end_dt = dt.fromisoformat(end_date).date()
        from datetime import timedelta
        end_dt_inclusive = end_dt + timedelta(days=1)
        days_in_period = (end_dt_inclusive - start_dt).days
        period_frac = days_in_period / 365.25

        # Qualification window (see _qualification_window): M3 of the quarter-before-previous
        # plus M1+M2 of the previous quarter — a 3-month window. An attending qualifies for the
        # $/RVU bonus if they exceeded the FTE-prorated 65th-percentile threshold during this
        # window, OR if they have no exam data prior to it (new-hire grace).
        con = get_connection(get_db_path())

        results = []
        for att_id, att_info in attendings.items():
            # `fte` reported in the response = the FTE at end-of-period (current value).
            fte = get_fte_for_date(att_info, end_dt)
            name = att_info.get('name', att_id)
            # Per-day FTE-prorated benchmarks. Day-level precision handles mid-month
            # start_date / end_date / fte_history changes correctly.
            ind_70 = get_period_target(att_info, annual_70, start_dt, end_dt)

            r = con.execute(f"""
                SELECT
                    COALESCE(SUM(work_professional_rvu), 0),
                    COALESCE(SUM(CASE WHEN shift_name != 'Moonlight' AND is_evening_er != TRUE AND shift_name != 'Weekend Call'
                        THEN work_professional_rvu ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN shift_name = 'Weekend Call' THEN work_professional_rvu ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN is_evening_er = TRUE THEN work_professional_rvu ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN shift_name = 'Moonlight' THEN work_professional_rvu ELSE 0 END), 0),
                    COUNT(*),
                    -- Split evening_er into weekday vs weekend by exam_completed_date day-of-week
                    -- (DuckDB dow: 0=Sun, 6=Sat). Weekday Flex/Nights IS in the gsheet schedule;
                    -- weekend is the empirical 4-10pm ER coverage that is NOT in the schedule.
                    COALESCE(SUM(CASE WHEN is_evening_er = TRUE
                        AND EXTRACT(dow FROM exam_completed_date) NOT IN (0, 6)
                        THEN work_professional_rvu ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN is_evening_er = TRUE
                        AND EXTRACT(dow FROM exam_completed_date) IN (0, 6)
                        THEN work_professional_rvu ELSE 0 END), 0),
                    -- After-hours read the canonical materialized flag; matches everywhere.
                    -- Previously an inline copy of the predicate — and quietly wrong: it
                    -- omitted the holiday match, so Christmas 10am reads got counted as
                    -- daytime here but as after-hours by Pay Projection using the shared
                    -- predicate in queries.py.
                    COALESCE(SUM(CASE WHEN is_after_hours = TRUE
                        THEN work_professional_rvu ELSE 0 END), 0)
                FROM exams
                WHERE {df} >= '{start_date}'
                  AND {df} < '{end_date}'::DATE + INTERVAL '1 day'
                  AND division = 'NEURO'
                  AND report_finalized_by = '{att_id}'
            """).fetchone()
            total, daytime, call, evening, moonlight, exams, weekday_evening_er, weekend_evening_er, after_hours = r

            # Qualification (current + next quarter) via the shared helper.
            qual = compute_qualification(con, att_id, att_info, annual_65)
            qualified = qual['qualified_for_rvu_bonus']
            # New model pays $/RVU on all qualifying after-hours wRVU (not just moonlight),
            # so the bonus-eligible bucket is the broader after_hours sum when qualified.
            bonus_rvus = after_hours if qualified else 0.0

            monthly_tns = att_info.get('monthly_tns', 38000)

            results.append({
                'attending_id': att_id,
                'attending_name': name,
                'fte': fte,
                'monthly_tns': monthly_tns,
                'total_rvus': round(total, 1),
                'daytime_rvus': round(daytime, 1),
                'call_rvus': round(call, 1),
                'evening_er_rvus': round(evening, 1),
                'weekday_evening_er_rvus': round(weekday_evening_er, 1),
                'weekend_evening_er_rvus': round(weekend_evening_er, 1),
                'moonlight_rvus': round(moonlight, 1),
                'after_hours_rvus': round(after_hours, 1),
                'benchmark_70th': round(ind_70, 1),
                'bonus_eligible_rvus': round(bonus_rvus, 1),
                **qual,  # qualified_for_rvu_bonus, is_new_hire, qualifying_* (curr + next)
                'exam_count': exams,
            })

        con.close()
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


import os


def _warm_caches():
    """Pre-populate the schedule and shift-averages caches on startup so the first
    user-facing pay-projection / schedule request doesn't pay the ~500ms cold cost.
    Failures here are non-fatal — endpoints recompute on demand."""
    import time as _t
    t0 = _t.perf_counter()
    try:
        load_schedule()
    except Exception as ex:
        print(f"[warm-cache] load_schedule failed (non-fatal): {ex}", flush=True)
    try:
        from datetime import date as _d, timedelta as _td
        con = get_connection(get_db_path())
        max_row = con.execute(
            "SELECT MAX(CAST(report_finalized_date AS DATE)) FROM exams WHERE division='NEURO'"
        ).fetchone()
        con.close()
        max_dt = max_row[0] if max_row and max_row[0] else _d.today()
        if isinstance(max_dt, str):
            max_dt = _d.fromisoformat(max_dt)
        start = (max_dt - _td(days=92)).isoformat()
        _shift_rvu_averages_with_schedule_counts(start, max_dt.isoformat(), 'report_finalized_date')
    except Exception as ex:
        print(f"[warm-cache] shift averages failed (non-fatal): {ex}", flush=True)
    print(f"[warm-cache] done in {(_t.perf_counter() - t0)*1000:.0f} ms", flush=True)


def _run_migrations():
    """Apply idempotent schema + data migrations at startup so a deploy self-heals an
    existing DB that predates a code change. Every step is a no-op once applied, so this
    is safe to run on every boot. Non-fatal: if the DB is locked or unavailable the server
    still starts and endpoints surface the error on demand.

    Ordering matters: CPT normalization (which recomputes `division`) runs before anything
    that reads division. The is_after_hours backfill doesn't depend on division, but we run
    it last so any future division-dependent migration slots in between cleanly.

    Each of these has a standalone CLI too (backend.cleanup_cpt_normalization,
    backend.backfill_after_hours) for manual re-runs with dry-run / scoping flags.
    """
    from pathlib import Path as _Path
    config_dir = os.environ.get('CONFIG_DIR', 'config')
    config_path = _Path(config_dir) if _Path(config_dir).is_absolute() else _Path(__file__).parent.parent / config_dir
    try:
        from .database import create_schema
        from .backfill_after_hours import backfill_nulls_only
        from .cleanup_cpt_normalization import normalize_cpt_in_db
        con = get_connection(get_db_path())
        create_schema(con)
        # 1. CPT normalization: strip '.0' from mPower-format cpt_code, recompute
        #    cpt_division, backfill NULL division. Only touches rows still carrying a '.0'.
        try:
            cpt_n = normalize_cpt_in_db(con, config_path)
        except Exception as ex:
            cpt_n = 0
            print(f"[migrate] cpt normalization skipped ({ex})", flush=True)
        # 2. Self-heal a freshly-migrated is_after_hours column (all-NULL on first add).
        ah_n = backfill_nulls_only(con)
        con.close()
        parts = ["schema ensured"]
        if cpt_n:
            parts.append(f"normalized cpt on {cpt_n:,} rows")
        if ah_n:
            parts.append(f"backfilled is_after_hours on {ah_n:,} rows")
        print(f"[migrate] {'; '.join(parts)}", flush=True)
    except Exception as ex:
        print(f"[migrate] migration failed (non-fatal): {ex}", flush=True)


def run_server(host='0.0.0.0', port=None, debug=False):
    """Run the Flask development server."""
    if port is None:
        port = int(os.environ.get('PORT', 5001))
    # Run migrations BEFORE serving — a deploy may have added a column queries reference,
    # or left existing rows in a pre-cleanup state. Synchronous so no request hits stale data.
    if not debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        _run_migrations()
    # Warm caches in a background thread so the server starts accepting connections
    # immediately; the first dashboard load benefits even if it lands mid-warm. When debug
    # mode is on Flask spawns a reloader child process — we only warm in the child (or when
    # debug is off entirely) so we don't waste cycles in the parent that exits anyway.
    import threading
    if not debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        threading.Thread(target=_warm_caches, name='warm-caches', daemon=True).start()
    app.run(host=host, port=port, debug=debug)


if __name__ == '__main__':
    # Debug (Werkzeug reloader + interactive debugger) defaults on for local dev, but the
    # interactive debugger is an RCE vector, so any internet-facing deploy MUST set
    # FLASK_DEBUG=0. The public demo compose does exactly that.
    run_server(debug=os.environ.get('FLASK_DEBUG', '1') == '1')
