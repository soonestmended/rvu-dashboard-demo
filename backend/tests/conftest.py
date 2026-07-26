"""Shared pytest fixtures and helpers.

Two things worth knowing:

  1. `soft_assert` is a warn-vs-fail helper. Use it for invariants that legitimately have
     small pre-existing violations you don't want to block deploys on — but that should
     trigger loud output if they grow. `actual > threshold` fails hard; `0 < actual <= threshold`
     warns; `actual == 0` is silent.

  2. Tests read a SNAPSHOT COPY of the production DB, not the live file. The backend Flask
     process holds a read-write lock on rvu.db; DuckDB's process-level locking means a
     separate pytest process can't open the same file even read-only while a writer holds
     it (raises IOException). The deploy runs these tests right after the backend starts,
     so the collision is guaranteed there even though it's timing-dependent locally.
     Copying the file to a temp path and opening the copy read-only sidesteps the lock
     entirely. The snapshot is a committed-state view — fine for invariant checks.
"""

import shutil
import tempfile
import warnings
import pytest
import duckdb
from pathlib import Path


DB_PATH = Path("/app/data/rvu.db")
CONFIG_DIR = Path("/app/config")


def soft_assert(actual_count: int, threshold: int, message: str):
    """Warn-vs-fail invariant check.
      - actual == 0:               silent pass
      - 0 < actual <= threshold:   emit WARNING and pass (visible in pytest summary)
      - actual > threshold:        hard fail
    """
    if actual_count == 0:
        return
    if actual_count <= threshold:
        warnings.warn(
            UserWarning(f"[SOFT] {message}: {actual_count} (threshold {threshold})"),
            stacklevel=2,
        )
        return
    pytest.fail(f"{message}: {actual_count} exceeded threshold {threshold}")


@pytest.fixture(scope="session")
def db_ro():
    """Read-only connection to a SNAPSHOT of the production DB.

    We copy rvu.db (plus its .wal sidecar, if present) to a temp path and open the copy.
    This avoids DuckDB's process-level write lock held by the running backend — a direct
    read-only connect to the live file raises IOException while a writer holds it.

    First try a direct read-only connect anyway (works when no backend is running, e.g.
    local dev), and fall back to the snapshot only when the file is locked. Keeps the fast
    path fast and the deploy path reliable.
    """
    if not DB_PATH.exists():
        pytest.skip(f"DB not found at {DB_PATH}")

    # Fast path: no writer holding the file (local runs with backend stopped).
    try:
        con = duckdb.connect(str(DB_PATH), read_only=True)
        yield con
        con.close()
        return
    except duckdb.IOException:
        pass  # locked by the backend — fall through to snapshot

    tmpdir = tempfile.mkdtemp(prefix="rvu_db_snapshot_")
    snap = Path(tmpdir) / "rvu.db"
    shutil.copy2(DB_PATH, snap)
    # Copy the WAL sidecar too so the snapshot reflects not-yet-checkpointed writes. Absent
    # in the common (checkpointed) case; copied best-effort when present.
    wal = DB_PATH.with_suffix(DB_PATH.suffix + ".wal")
    if wal.exists():
        shutil.copy2(wal, snap.with_suffix(snap.suffix + ".wal"))

    con = duckdb.connect(str(snap), read_only=True)
    try:
        yield con
    finally:
        con.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
