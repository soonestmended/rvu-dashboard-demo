"""Pure-function unit tests. No DB, no network. Fast."""

import pytest
import pandas as pd
from datetime import date, timedelta
from pathlib import Path

from backend.ingest import _normalize_cpt, compute_dedup_key


# ---------- _normalize_cpt ----------

class TestNormalizeCpt:
    def test_int_string_unchanged(self):
        assert _normalize_cpt("70450") == "70450"

    def test_strips_float_suffix(self):
        """mPower rows arrive as pandas-coerced floats like '70450.0' — must strip .0."""
        assert _normalize_cpt("70450.0") == "70450"

    def test_comma_list_piecewise(self):
        assert _normalize_cpt("70450.0, 70551.0") == "70450, 70551"

    def test_preserves_legitimate_decimals(self):
        """CPTs never end in .0 normally, but if they end in .5 or other, keep them."""
        assert _normalize_cpt("70450.5") == "70450.5"

    def test_nan_empty(self):
        assert _normalize_cpt(float("nan")) == ""
        assert _normalize_cpt(None) == ""

    def test_idempotent(self):
        """Safe to call on already-normalized values (matters because both hash-time and
        ingest-time pipelines call it — must agree after N passes)."""
        assert _normalize_cpt(_normalize_cpt("70450.0")) == "70450"


# ---------- compute_dedup_key ----------

class TestDedupKey:
    def _row(self, ec="2026-06-15T10:00:00", rf="2026-06-15T14:30:00", cpt="70450", by="ATT001"):
        return compute_dedup_key(pd.Timestamp(ec), pd.Timestamp(rf), cpt, by)

    def test_deterministic(self):
        assert self._row() == self._row()

    def test_different_attending_different_key(self):
        assert self._row(by="ATT001") != self._row(by="ATT002")

    def test_cross_format_cpt_matches(self):
        """The whole point of the CPT normalization fix — same exam via two ingest paths
        must produce the same key."""
        powerscribe = self._row(cpt="70450")
        mpower = self._row(cpt="70450.0")
        assert powerscribe == mpower

    def test_microsecond_precision_dropped(self):
        """mPower gives seconds, PowerScribe gives microseconds — must hash the same at
        second precision, else same-exam-two-sources dedup fails."""
        a = self._row(rf="2026-06-15T14:30:00")
        b = self._row(rf="2026-06-15T14:30:00.987654")
        assert a == b


# ---------- 2-digit-year parser ----------

class TestScheduleDateParse:
    """Regression: 4-digit years used to trip the pivot logic and become e.g. 3925."""

    def _parse(self, date_str, default_year=2025):
        """Isolate the parse block from schedule.py's parse_neuro_schedule."""
        parts = date_str.split('/')
        if len(parts) == 3:
            month, day, year = parts
            year_int = int(year)
            if len(year.strip()) <= 2:
                year_int += 2000 if year_int < 50 else 1900
            return f"{year_int}-{month.zfill(2)}-{day.zfill(2)}"
        return None

    def test_2digit_year_pivots(self):
        assert self._parse("1/15/25") == "2025-01-15"
        assert self._parse("1/15/99") == "1999-01-15"

    def test_4digit_year_passthrough(self):
        """Would previously become 3925."""
        assert self._parse("1/15/2025") == "2025-01-15"
        assert self._parse("12/31/2026") == "2026-12-31"


# ---------- _auto_days ----------
# mpower_fetch runs on the HOST via uv, not in the backend container — its module isn't
# on the container's path. These tests will skip when run inside docker exec and run
# normally when pytest is invoked from the repo root on the host.

class TestAutoDays:
    def _import_auto_days(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy"))
        try:
            from mpower_fetch import _auto_days
            return _auto_days
        except ImportError:
            pytest.skip("deploy/mpower_fetch not importable (expected inside backend container)")

    def test_computes_from_max_date(self, tmp_path):
        """Verify (today − MAX(report_finalized_date)) + 1 day."""
        _auto_days = self._import_auto_days()
        import duckdb
        from backend.database import create_schema
        db = tmp_path / "test.db"
        con = duckdb.connect(str(db))
        create_schema(con)
        yesterday = date.today() - timedelta(days=3)
        con.execute(
            "INSERT INTO exams (accession_number, cpt_code, report_finalized_by, report_finalized_date) "
            "VALUES (?, ?, ?, ?)",
            ["TEST001", "70450", "ATT001", yesterday],
        )
        con.close()
        assert _auto_days(db) == 4  # 3-day gap + 1 overlap

    def test_falls_back_when_empty_db(self, tmp_path):
        _auto_days = self._import_auto_days()
        import duckdb
        from backend.database import create_schema
        db = tmp_path / "empty.db"
        con = duckdb.connect(str(db))
        create_schema(con)
        con.close()
        assert _auto_days(db, fallback=14) == 14

    def test_falls_back_when_no_db_file(self, tmp_path):
        _auto_days = self._import_auto_days()
        assert _auto_days(tmp_path / "missing.db", fallback=14) == 14
