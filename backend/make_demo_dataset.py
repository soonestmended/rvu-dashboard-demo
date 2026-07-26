"""Generate a fully de-identified demo dataset from the real DB + config.

For the CAIMI conference demo: a scrubbed clone of the production data that the app can
run against with zero PHI risk. Produces two things next to the real ones:

  data/demo.db     — clone of rvu.db with every real attending NAME replaced by a stable
                     fake. Neuro-section attendings are stored as ATT ids (already
                     de-identified) so their exam rows are untouched; only the config names
                     they map to get faked. The remaining real names in report_finalized_by
                     (non-de-identified rads) are replaced with stable fakes. ATT ids are
                     internal codes, not PII, and are kept as-is.

  config-demo/     — parallel config: faked neuro_config, neuro_att_map, schedule CSVs,
                     weekend_er_assignments, and a FAKED attending_id_map (never a copy of
                     the real one — that file is the id->realname re-identification key).
                     No-PII configs (shifts, cpt_divisions, feature_flags,
                     attending_divisions) copy straight over. Plus a single demo admin login.

Deterministic: identities are sorted and assigned from a fixed name pool, so re-runs are
stable (same real input -> same fake output).

Usage:
    docker compose exec backend python -m backend.make_demo_dataset
    docker compose exec backend python -m backend.make_demo_dataset --demo-password caimi2026
"""

import argparse
import calendar
import csv
import logging
import re
import shutil
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import yaml

from .auth import hash_password

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
REAL_DB = REPO / "data" / "rvu.db"
DEMO_DB = REPO / "data" / "demo.db"
REAL_CONFIG = REPO / "config"
# Output under the mounted data/ dir so both artifacts are host-visible (only data/ and
# config/ are bind-mounted into the container; writing config-demo elsewhere would strand
# it inside the container).
DEMO_CONFIG = REPO / "data" / "config-demo"

ATT_ID_RE = re.compile(r"^ATT\d+$")

# ATT ids removed from the demo entirely. ATT000772 is a departed 0.4-FTE attending (no exams
# after Mar 2026) who otherwise clutters the demo roster. They stay in the identity map so name
# scrubbing remains leak-proof, then are dropped from every demo output: config roster, exams,
# att maps, and their (already-faked) schedule column/cells.
DEMO_EXCLUDE_IDS = {"ATT000772"}

# ~150 realistic surnames (distinct from the real neuro names) so every one of the ~132
# identities gets its own surname. Neuro attendings are assigned first, so they draw the
# leading (clean, distinct) surnames used as schedule column headers.
FAKE_SURNAMES = [
    "Alvarez", "Bennett", "Castellano", "Delgado", "Ellsworth", "Fairbanks", "Grimaldi",
    "Hollis", "Ishikawa", "Jennings", "Kowalski", "Lindqvist", "Montgomery", "Nakamura",
    "Okonkwo", "Prescott", "Quintero", "Rasmussen", "Sinclair", "Thornton", "Underwood",
    "Vasquez", "Whitfield", "Xanthos", "Yamamoto", "Zielinski", "Ashford", "Braithwaite",
    "Cavanaugh", "Donovan", "Escobar", "Fitzgerald", "Galloway", "Harrington", "Ivankov",
    "Jaskolski", "Kensington", "Lockhart", "Marchetti", "Nordstrom", "Ortega", "Pemberton",
    "Radcliffe", "Sandoval", "Tremblay", "Vandermeer", "Wexler", "Yancey", "Zaragoza",
    "Abernathy", "Bianchi", "Corrigan", "Dubois", "Emerson", "Falkner", "Giordano",
    "Hawthorne", "Idris", "Jozwiak", "Kaminski", "Larsen", "Mendez", "Novak", "Osei",
    "Pankratov", "Rios", "Steadman", "Toscano", "Uddin", "Vukovic", "Waverly", "Yost",
    "Zimmerman", "Adeyemi", "Broussard", "Cardoza", "Devereaux", "Engström", "Ferraro",
    "Gutierrez", "Halvorsen", "Imamura", "Jankowski", "Krause", "Lefebvre", "Moreau",
    "Nagata", "Oyelaran", "Petrov", "Quesada", "Reinhardt", "Salcedo", "Takahashi",
    "Ustinov", "Vega", "Wallenberg", "Yousef", "Ziegler", "Anastos", "Bergqvist",
    "Cifuentes", "Drummond", "Espinoza", "Fujimoto", "Grabowski", "Herrera", "Iqbal",
    "Jergensen", "Kobayashi", "Lombardi", "Matsuda", "Nwosu", "Ojeda", "Piotrowski",
    "Rahimi", "Sorensen", "Tanaka", "Valdez", "Wojcik", "Yamashita", "Zubov", "Agunbiade",
    "Belanger", "Choudhury", "Dahlberg", "Ferreira", "Goldschmidt", "Hidalgo", "Ismailova",
    "Jimenez", "Kuznetsov", "Lindgren", "Massaro", "Nilsson", "Ovchinnikov", "Petrenko",
    "Rossi", "Salvatore", "Tikhonov", "Villanueva", "Weinstein", "Yamada", "Zabala",
]

FAKE_FIRSTS = [
    "Adrian", "Beatriz", "Cyrus", "Dahlia", "Elena", "Felix", "Greta", "Hassan", "Ingrid",
    "Julius", "Katya", "Liam", "Mira", "Nadia", "Omar", "Priya", "Quinn", "Rafael",
    "Sofia", "Tobias", "Ulyana", "Viktor", "Wren", "Ximena", "Yusuf", "Zara", "Anton",
    "Camille", "Dmitri", "Esther", "Farhan", "Gwen", "Henrik", "Isla", "Javier", "Lena",
    "Marco", "Noor", "Otis", "Paloma",
]


def _fake_full_names(n: int) -> list:
    """Deterministic list of n distinct 'Last, First' names. Distinct surnames as long as
    n <= len(FAKE_SURNAMES)."""
    if n > len(FAKE_SURNAMES):
        raise ValueError(f"Need {n} fake names but only {len(FAKE_SURNAMES)} surnames available")
    return [f"{FAKE_SURNAMES[i]}, {FAKE_FIRSTS[i % len(FAKE_FIRSTS)]}" for i in range(n)]


def _last(full: str) -> str:
    """Last name (before comma), upper-cased for join-key matching."""
    return full.split(",")[0].strip()


def build_identity_map():
    """Return the mappings needed to fake every identity consistently.

    Returns dict with:
      neuro_ids        : ordered list of neuro ATT ids (from neuro_config)
      id_to_fake_full  : {ATT id -> 'FakeLast, FakeFirst'} for ALL ATT ids in exams
      realname_to_fake : {real full name -> fake full name} for the 21 non-ATT names
      real_last_to_fake_last : {RealLast(upper) -> FakeLast} for the neuro attendings
    """
    # Real neuro attendings (keep their id, fake the name). Ordered by id for stability.
    with open(REAL_CONFIG / "neuro_config.yaml") as f:
        neuro_cfg = yaml.safe_load(f)
    neuro_atts = neuro_cfg.get("attendings", {})
    neuro_ids = sorted(neuro_atts.keys())

    # Every distinct identity in exams.
    con = duckdb.connect(str(_snapshot_real_db()), read_only=True)
    identities = [r[0] for r in con.execute(
        "SELECT DISTINCT report_finalized_by FROM exams WHERE report_finalized_by IS NOT NULL"
    ).fetchall()]
    con.close()

    att_ids_in_exams = sorted(i for i in identities if ATT_ID_RE.match(str(i)))
    real_names = sorted(i for i in identities if not ATT_ID_RE.match(str(i)))

    # Assignment order: neuro ids first (clean leading surnames -> schedule columns), then
    # the other ATT ids, then the real names. Union preserves order, no duplicates.
    ordered_att = neuro_ids + [i for i in att_ids_in_exams if i not in set(neuro_ids)]
    total = len(ordered_att) + len(real_names)
    fakes = _fake_full_names(total)

    id_to_fake_full = {aid: fakes[i] for i, aid in enumerate(ordered_att)}
    realname_to_fake = {rn: fakes[len(ordered_att) + i] for i, rn in enumerate(real_names)}

    # Neuro real last name -> fake last name (for schedule header + Call column remap).
    real_last_to_fake_last = {}
    for aid in neuro_ids:
        real_full = neuro_atts[aid].get("name", aid)
        real_last_to_fake_last[_last(real_full).upper()] = _last(id_to_fake_full[aid])

    return {
        "neuro_cfg": neuro_cfg,
        "neuro_ids": neuro_ids,
        "id_to_fake_full": id_to_fake_full,
        "realname_to_fake": realname_to_fake,
        "real_last_to_fake_last": real_last_to_fake_last,
    }


def _parse_sched_date(s):
    """Parse a schedule 'M/D/YY' (or 'M/D/YYYY') Date cell → date, or None for blank/garbage."""
    try:
        mm, dd, yy = str(s).strip().split('/')
        yy = int(yy)
        yy = yy + 2000 if yy < 100 else yy
        return date(yy, int(mm), int(dd))
    except Exception:
        return None


# Work shifts the CP-SAT solver assigns. For open future dates ONLY these are cleared; every
# other cell — Vacation/Academic and their free-text request notes, pre-assigned Call, Holiday/
# Weekend markers, "Not hired yet", etc. — is a constraint the solver imports and plans around.
_SOLVER_WORK_SHIFTS = {'flex', 'flex/nights', 'inpatienta', 'inpatientb', 'outpatienta', 'outpatientb'}


def _is_work_shift(v):
    # 'Flex*' (a "who covers X's flex" pay annotation) normalizes to 'flex' and is cleared too.
    return str(v).strip().rstrip('*').strip().lower() in _SOLVER_WORK_SHIFTS


def _blank_schedule_after(df, data_end):
    """For schedule rows dated AFTER the demo's data-end, clear ONLY the solver-assigned work
    shifts, leaving vacation/academic requests, pre-assigned calls, and structural markers in
    place. The demo DB is a static snapshot, but the cloned schedule was fully booked through
    year-end — so future months arrive with work-shift lock-ins (including the 'covers X's Flex'
    annotations) that trip the solver's preflight. Clearing just the work shifts leaves a
    realistic set of constraints for the solver to import and plan around, so the demo can always
    generate a candidate for any future window."""
    if data_end is None or 'Date' not in df.columns or df.empty:
        return df
    future = df['Date'].map(lambda d: (_parse_sched_date(d) or date.min) > data_end)
    if not future.any():
        return df
    for col in df.columns:
        if col in (df.columns[0], 'Date', 'Day'):  # never touch Month/Date/Day; Call is kept below
            continue
        mask = future & df[col].map(_is_work_shift)  # Call/vacation/academic aren't work shifts → kept
        if mask.any():
            df.loc[mask, col] = ''
    return df


def _snapshot_real_db() -> Path:
    """Copy the (possibly backend-locked) real DB to a temp path for read-only inspection."""
    tmp = REPO / "data" / "_demo_src_snapshot.db"
    shutil.copy2(REAL_DB, tmp)
    return tmp


def write_demo_db(m):
    """Clone rvu.db -> demo.db and scrub the 21 real names in report_finalized_by."""
    if DEMO_DB.exists():
        DEMO_DB.unlink()
    shutil.copy2(REAL_DB, DEMO_DB)
    con = duckdb.connect(str(DEMO_DB))  # read-write on the fresh copy (no lock contention)
    updates = 0
    for real_name, fake in m["realname_to_fake"].items():
        n = con.execute(
            "SELECT COUNT(*) FROM exams WHERE report_finalized_by = ?", [real_name]
        ).fetchone()[0]
        if n:
            con.execute(
                "UPDATE exams SET report_finalized_by = ? WHERE report_finalized_by = ?",
                [fake, real_name],
            )
            updates += n
    # Drop excluded attendings' exams entirely (also removed from the demo roster/config).
    for aid in DEMO_EXCLUDE_IDS:
        n = con.execute("SELECT COUNT(*) FROM exams WHERE report_finalized_by = ?", [aid]).fetchone()[0]
        if n:
            con.execute("DELETE FROM exams WHERE report_finalized_by = ?", [aid])
            logger.info(f"demo.db: removed {n:,} exam rows for excluded attending {aid}")
    # Safety check: no real (non-ATT, non-fake) names should remain.
    fake_set = set(m["realname_to_fake"].values())
    leftover = con.execute("""
        SELECT DISTINCT report_finalized_by FROM exams
        WHERE report_finalized_by IS NOT NULL
          AND report_finalized_by NOT LIKE 'ATT%'
    """).fetchall()
    con.close()
    bad = [r[0] for r in leftover if r[0] not in fake_set]
    logger.info(f"demo.db: scrubbed {updates:,} exam rows across {len(m['realname_to_fake'])} real names")
    if bad:
        raise RuntimeError(f"Real names survived scrub: {bad[:10]}")
    logger.info("demo.db: verified no un-faked real names remain")


def write_demo_config(m, demo_password: str):
    """Write config-demo/ with faked names + a demo admin login."""
    DEMO_CONFIG.mkdir(exist_ok=True)

    # 1. neuro_config.yaml — replace each attending's name with its fake full name.
    cfg = m["neuro_cfg"]
    for aid, info in cfg.get("attendings", {}).items():
        info["name"] = m["id_to_fake_full"].get(aid, info.get("name", aid))
    for aid in DEMO_EXCLUDE_IDS:  # drop excluded attendings from the demo roster
        cfg.get("attendings", {}).pop(aid, None)
    with open(DEMO_CONFIG / "neuro_config.yaml", "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)

    # 2. neuro_att_map.csv — ATT id, "FakeLast, FakeFirst" (no header, matches real format).
    with open(DEMO_CONFIG / "neuro_att_map.csv", "w", newline="") as f:
        w = csv.writer(f)
        for aid in m["neuro_ids"]:
            if aid in DEMO_EXCLUDE_IDS:
                continue
            w.writerow([aid, m["id_to_fake_full"][aid]])

    # 2b. name_map.csv — real_last, fake_last for every neuro attending. Used ONLY by the
    #     DEMO_MODE scheduler path: it un-renames the demo schedule (fake→real) so the
    #     solver's real-name-keyed rules apply, then re-renames the output (real→fake) for
    #     display. This file necessarily contains real last names, so it lives with the
    #     (gitignored) demo config and is never displayed.
    with open(DEMO_CONFIG / "name_map.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["real_last", "fake_last"])
        for real_last_upper, fake_last in sorted(m["real_last_to_fake_last"].items()):
            w.writerow([real_last_upper, fake_last])

    # 3. attending_id_map.csv — FAKED (never copy the real one; it's the re-id key). Cover
    #    every ATT id that has a fake assigned.
    with open(DEMO_CONFIG / "attending_id_map.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["attending_id", "attending_name"])
        for aid, fake in sorted(m["id_to_fake_full"].items()):
            if aid in DEMO_EXCLUDE_IDS:
                continue
            w.writerow([aid, fake])

    # 4. schedule CSVs — rename attending last-name headers, then scrub every cell for any
    #    real last name (whole word). Cells can hold typed free-text notes like "Chu -ok for
    #    me", so a blanket substitution is needed, not just Call-column/header handling.
    #    Include the 21 non-neuro real names' last names too, in case a note references one.
    all_last_to_fake = dict(m["real_last_to_fake_last"])  # RealLastUpper -> FakeLast
    for real_full, fake_full in m["realname_to_fake"].items():
        all_last_to_fake[_last(real_full).upper()] = _last(fake_full)
    name_re = re.compile(
        r"\b(" + "|".join(re.escape(rl) for rl in sorted(all_last_to_fake, key=len, reverse=True)) + r")\b",
        re.IGNORECASE,
    )

    def _scrub_cell(v):
        if not isinstance(v, str):
            return v
        return name_re.sub(lambda mo: all_last_to_fake[mo.group(0).upper()], v)

    # Schedule fill-through: keep the real (faked) schedule complete through the END of the
    # data-end's month, and blank work shifts only AFTER that. This gives the demo a fully
    # scheduled recent month (e.g. all of July) sitting next to an OPEN next month (August) that
    # generation defaults to — a clean "last month done, plan next month" story. (Blanking is
    # work-shifts-only; vacation/academic/calls always carry through — see _blank_schedule_after.)
    fill_through = None
    try:
        _c = duckdb.connect(str(DEMO_DB), read_only=True)
        data_end = _c.execute(
            "SELECT MAX(CAST(report_finalized_date AS DATE)) FROM exams WHERE division='NEURO'"
        ).fetchone()[0]
        _c.close()
        if data_end is not None:
            last_day = calendar.monthrange(data_end.year, data_end.month)[1]
            fill_through = date(data_end.year, data_end.month, last_day)
    except Exception as _ex:
        logger.warning(f"Could not read demo data-end for schedule blanking ({_ex}); leaving schedule as-is")
    if fill_through is not None:
        logger.info(f"Filling demo schedule through {fill_through} (end of data-end month); blanking work shifts after")

    # Faked last names of excluded attendings — dropped from the schedule (column + any cell,
    # e.g. the Call column) so they vanish from the demo entirely. Safe to match by value: each
    # fake last name is unique to one attending.
    exclude_fake_last = {_last(m["id_to_fake_full"][aid]) for aid in DEMO_EXCLUDE_IDS
                         if aid in m["id_to_fake_full"]}
    for sched in sorted(REAL_CONFIG.glob("neuro_schedule_*.csv")):
        df = pd.read_csv(sched, dtype=str)
        # Header rename: any column whose upper() matches a real neuro last name.
        rename = {col: m["real_last_to_fake_last"][str(col).strip().upper()]
                  for col in df.columns
                  if str(col).strip().upper() in m["real_last_to_fake_last"]}
        df = df.rename(columns=rename)
        # Cell scrub: catches Call-column names AND free-text notes in any column.
        df = df.map(_scrub_cell)
        # Drop excluded attendings: their (faked) column, and any exact-match cell (Call, etc.).
        df = df.drop(columns=[c for c in df.columns if c in exclude_fake_last], errors="ignore")
        if exclude_fake_last:
            df = df.replace(list(exclude_fake_last), "", regex=False)
        # Open up the post-fill-through future so candidate generation never hits pre-booked lock-ins.
        df = _blank_schedule_after(df, fill_through)
        df.to_csv(DEMO_CONFIG / sched.name, index=False)

    # 5. weekend_er_assignments.csv — fake the name column, keep id + counts.
    we = REAL_CONFIG / "weekend_er_assignments.csv"
    if we.exists():
        wdf = pd.read_csv(we, dtype=str)
        if "attending_id" in wdf.columns:
            wdf = wdf[~wdf["attending_id"].isin(DEMO_EXCLUDE_IDS)]  # drop excluded attendings
            if "attending_name" in wdf.columns:
                wdf["attending_name"] = wdf["attending_id"].map(
                    lambda aid: m["id_to_fake_full"].get(aid, aid)
                )
        wdf.to_csv(DEMO_CONFIG / "weekend_er_assignments.csv", index=False)

    # 6. No-PII configs — copy straight over.
    for name in ["shifts.yaml", "cpt_divisions.yaml", "feature_flags.yaml",
                 "attending_divisions.yaml"]:
        src = REAL_CONFIG / name
        if src.exists():
            shutil.copy2(src, DEMO_CONFIG / name)

    # 7. users.yaml — a single demo admin login (full app on fake data).
    users = [{
        "username": "demo",
        "password_hash": hash_password(demo_password),
        "role": "admin",
    }]
    with open(DEMO_CONFIG / "users.yaml", "w") as f:
        yaml.safe_dump(users, f, sort_keys=False)

    logger.info(f"config-demo/: wrote faked configs + demo admin login (password: {demo_password!r})")

    # Safety sweep: no real last name (neuro OR the 21 scrubbed rads) should appear anywhere
    # in config-demo text files.
    hits = []
    for p in DEMO_CONFIG.iterdir():
        # name_map.csv legitimately contains real last names — it's the internal real↔fake
        # rename key used only by the DEMO_MODE solver path, never displayed. Skip it.
        if p.name == "name_map.csv":
            continue
        if p.suffix in (".csv", ".yaml"):
            text = p.read_text()
            for rl in all_last_to_fake:
                if re.search(rf"\b{re.escape(rl)}\b", text, re.IGNORECASE):
                    hits.append((p.name, rl))
    if hits:
        logger.warning(f"Possible real-name leak in config-demo: {sorted(set(hits))[:12]}")
    else:
        logger.info("config-demo/: verified no real last names present")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo-password", default="caimi2026",
                    help="Password for the demo admin login (default: caimi2026).")
    args = ap.parse_args()

    logger.info("Building identity map...")
    m = build_identity_map()
    logger.info(f"Faking {len(m['id_to_fake_full'])} ATT ids + {len(m['realname_to_fake'])} real names")

    write_demo_db(m)
    write_demo_config(m, args.demo_password)

    # Clean up the source snapshot.
    snap = REPO / "data" / "_demo_src_snapshot.db"
    if snap.exists():
        snap.unlink()

    logger.info("Done. demo.db + config-demo/ ready.")
    logger.info("Sample fake neuro attendings:")
    for aid in m["neuro_ids"][:5]:
        logger.info(f"  {aid} -> {m['id_to_fake_full'][aid]}")


if __name__ == "__main__":
    main()
