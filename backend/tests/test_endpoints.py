"""Endpoint smoke tests. Hit the running Flask backend via HTTP (localhost:5001 inside
the container). Covers auth boundary + the SQL-injection guard + a few core endpoints
to make sure they return 200 and shaped-JSON.

If the backend isn't running these all skip — they're deploy-time regression tests, not
useful during a plain unit-test run.
"""

import os
import pytest
import requests

BASE = os.environ.get("TEST_BACKEND_URL", "http://localhost:5001")

# Admin session for tests. On the deployed server, the test runner should export
# TEST_ADMIN_USER and TEST_ADMIN_PASSWORD before running pytest; without them the
# admin-endpoint checks are skipped and only the auth-boundary + injection tests run.
ADMIN_USER = os.environ.get("TEST_ADMIN_USER")
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD")


@pytest.fixture(scope="session")
def health_check():
    """Verify the backend is up. If not, subsequent tests should skip."""
    try:
        r = requests.get(f"{BASE}/api/health", timeout=2)
        r.raise_for_status()
    except Exception as ex:
        pytest.skip(f"backend not reachable at {BASE}: {ex}")


@pytest.fixture(scope="session")
def unauth_session(health_check):
    """Fresh session with no login."""
    return requests.Session()


@pytest.fixture(scope="session")
def admin_session(health_check):
    """Logged-in admin session. Skips the tests that use it if no test creds provided."""
    if not (ADMIN_USER and ADMIN_PASSWORD):
        pytest.skip("TEST_ADMIN_USER / TEST_ADMIN_PASSWORD not set")
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}, timeout=5)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: HTTP {r.status_code} {r.text[:120]}")
    return s


# ---------- Auth boundary ----------

class TestAuthBoundary:
    def test_health_public(self, unauth_session):
        r = unauth_session.get(f"{BASE}/api/health")
        assert r.status_code == 200

    def test_me_unauth_401(self, unauth_session):
        r = unauth_session.get(f"{BASE}/api/auth/me")
        # Backend returns 401 when no session; app also treats /auth/me as returning null,
        # depending on the code path. Either "no user" outcome is fine.
        assert r.status_code in (200, 401)
        if r.status_code == 200:
            assert r.json() is None or r.json() == {}

    def test_admin_endpoint_unauth_401(self, unauth_session):
        r = unauth_session.get(f"{BASE}/api/rvus/by-division?start_date=2026-01-01&end_date=2026-06-30")
        assert r.status_code in (401, 403)


# ---------- SQL injection guards (regression for bug #1) ----------

class TestSqlInjectionGuard:
    """The before_request hook rejects malformed date query params. Without it, a caller
    could pass a raw SQL fragment through start_date/end_date and read arbitrary rows."""

    MALFORMED = [
        "2020-01-01' UNION SELECT 1--",
        "not-a-date",
        "2020/01/01",  # wrong separator
        "2020-1-1",    # not zero-padded — fromisoformat is strict about that
        "",
    ]

    @pytest.mark.parametrize("bad_date", MALFORMED)
    def test_bad_start_date_rejected(self, admin_session, bad_date):
        r = admin_session.get(
            f"{BASE}/api/rvus/by-division",
            params={"start_date": bad_date, "end_date": "2026-06-30"},
        )
        # 400 (validation) is expected; empty-string may bypass to the "required" check → 400.
        assert r.status_code == 400, f"bad start_date {bad_date!r} not rejected: {r.status_code} {r.text[:120]}"

    @pytest.mark.parametrize("bad_date", MALFORMED)
    def test_bad_end_date_rejected(self, admin_session, bad_date):
        r = admin_session.get(
            f"{BASE}/api/rvus/by-division",
            params={"start_date": "2026-01-01", "end_date": bad_date},
        )
        assert r.status_code == 400


# ---------- Core endpoints return valid shapes ----------

class TestCoreEndpoints:
    """Just check they return 200 and non-error JSON. Deep correctness lives in
    test_db_invariants — here we're catching endpoint-level regressions (500s,
    accidentally-broken query construction, etc.)."""

    def test_rvus_by_division(self, admin_session):
        r = admin_session.get(
            f"{BASE}/api/rvus/by-division",
            params={"start_date": "2026-01-01", "end_date": "2026-06-30"},
        )
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        for row in rows:
            assert "division" in row
            assert "total_rvu" in row

    def test_rvus_by_attending(self, admin_session):
        r = admin_session.get(
            f"{BASE}/api/rvus/by-attending",
            params={"start_date": "2026-01-01", "end_date": "2026-06-30"},
        )
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_neuro_staffing_metrics(self, admin_session):
        r = admin_session.get(
            f"{BASE}/api/staffing/neuro-metrics",
            params={"start_date": "2026-01-01", "end_date": "2026-06-30"},
        )
        assert r.status_code == 200

    def test_shift_rvu_averages(self, admin_session):
        r = admin_session.get(
            f"{BASE}/api/schedule/shift-rvu-averages",
            params={"start_date": "2026-04-01", "end_date": "2026-06-30"},
        )
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        for row in rows:
            assert "shift_name" in row and "avg_rvu" in row

    def test_date_range(self, admin_session):
        r = admin_session.get(f"{BASE}/api/date-range")
        assert r.status_code == 200
        j = r.json()
        assert "min_date" in j and "max_date" in j
