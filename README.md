# RVU Dashboard — Neuroradiology Productivity, Compensation & AI Scheduling

An integrated web platform for an academic neuroradiology section that unifies three things
that normally live in separate silos — **productivity** (wRVUs from the reporting systems),
**compensation** (a spreadsheet), and the **call/vacation schedule** (a shared Google Sheet) —
into one place where every attending and the section chief see the same numbers.

Built for the **SIIM-CAIMI26 Builder Showcase**.

---

## 🔗 Live demo

**https://159-89-151-94.sslip.io** — login **`demo`** / **`caimi2026`**

The demo runs on a **fully fabricated dataset** — fake attending names, no PHI. Because it's a
public demo, **every dollar figure is blurred** (the underlying numbers are fake anyway; the
blur just keeps salary-looking figures off-screen). All non-dollar values — wRVUs, percentages,
shift counts — are shown normally.

**The one thing to try:** open **Schedule Planning → + Generate candidate** and watch the
CP-SAT solver build next month's schedule live (it defaults to an open month so it solves in a
few seconds).

The demo logs in as an admin, so all six user-facing tabs are visible. (The admin-only
**Settings** tab — user management and feature flags — is hidden in the demo, since its config
is read-only there.)

---

## Concepts glossary (read this first)

The tabs assume some section-specific vocabulary. None of it is obvious from the UI alone:

| Term | What it means |
|---|---|
| **wRVU** | Work RVU — the professional-work productivity unit credited for reading a study. The whole app is denominated in wRVUs. |
| **Benchmark (65th / 70th)** | MGMA-style national productivity targets. The section uses an **annual 65th-percentile** wRVU target (~10,179) as the baseline and a **70th** (~10,509) as a cap. Every personal target is **FTE-prorated** (× FTE × fraction-of-year). |
| **% of benchmark** | Actual wRVUs ÷ that person's FTE-prorated target. Color scale used everywhere: green ≥ 100%, amber ≥ 95%, red below. |
| **TNS** | *Total Negotiated Salary* — base pay (not FTE-scaled). |
| **TAT bonus** | *Turnaround-Time* bonus = a flat **5% of TNS** (essentially always earned). |
| **Section bonus** | Tiered bonus on the section's **total** volume: +1% of TNS per percentile from the 65th to the 70th (max +5%). Rewards the group, not the individual. |
| **Evening ER pay** | Per-shift stipends for covering the evening ER: **$1,840** weekday, **$2,080** weekend. |
| **After-hours $/RVU bonus** | The core of the *proposed* comp model. wRVUs read **after hours** (outside Mon–Fri 8a–5p Pacific, excluding weekend call and evening ER) that push a person above the 70th percentile are paid at a per-RVU rate (demo default ~**$51/RVU**) — **but only if they "qualify"** (below). |
| **Qualification** | The gate for the $/RVU bonus. Over a rolling **3-month qualification window**, production must clear the FTE-prorated 65th percentile (**65th × FTE × 3/12**). New hires auto-qualify for their first quarter. |
| **Qualification window** | The three months counted: *the last month of the quarter-before-previous, plus the first two months of the previous quarter*. (In forward planning it's *the month before the candidate starts + the candidate's first two months*.) This lag exists so a full quarter of data is settled before the bonus is paid. |
| **Shift types** | `Inpatient A/B`, `Outpatient A/B`, `Flex`, `Flex/Nights` are the wRVU-generating clinical shifts. `Academic`, `Vacation`, `Conference`, `Sick`, `Holiday`, `Weekend`, `Call` are non-clinical / status entries. |
| **`Flex*` (trailing asterisk)** | A **coverage annotation**. When one attending's clinical time is bought out but still listed on the sheet for bookkeeping, a *different* attending actually covers it. The `*` marks the covered/reassigned cell (rendered magenta in the grid); the real coverer earns the shift's read volume **plus** a per-shift coverage stipend. |
| **CP-SAT** | Google OR-Tools' constraint solver, used to generate candidate schedules. |

---

## Tab-by-tab walkthrough

### 1. Productivity
*How much is each attending producing, relative to expectation, and in what mix of shifts?*

- **Productivity table** — one row per attending (sortable; a bold section **Total** row sits on top): `Total Exams`, `Total RVUs`, `Expected RVUs` (their FTE-prorated benchmark), and a **`Multiplier`** = actual ÷ expected (e.g. `1.05x`, color-coded). A benchmark selector (65th / 70th) sets what "expected" means.
- **Shift Distribution table** — a pivot of attendings × shift types. Each cell stacks three numbers: the **count** of that shift, its **share** of the attending's shifts (`NN%`), and the **average exams per shift** (`N.N ex/sh`) — so you can see both how often someone works a shift and how heavy that shift runs.

### 2. Compensation Review
*Retrospective: what each attending actually produced in a past window, and what they'd earn under the proposed comp model.*

The tab has its **own date range** (independent of other tabs). Above the table, **Shift RVU averages** cards show the section-average wRVU per shift type over the window — these averages are what the projections and the solver use.

The **per-attending actuals** table (dollar columns blurred in the demo):
- `Actual RVUs` — every wRVU finalized in the window (includes moonlight, evening ER, weekend call).
- `65th target` and `% of 65th` — the FTE-prorated target and how close they came.
- `TNS`, `TAT`, `Section`, `ER`, `After Hours`, `Total`, `Annualized` — the proposed-model comp breakdown (same formula as the Comp — What-If tab).
- **`Qual (curr Q)`** and **`Qual (next Q)`** — production over each quarter's 3-month qualification window as a % of the 65th × FTE × 3/12 threshold. **≥ 100% clears the after-hours bonus gate.** The tooltips name the exact three months being counted; the next-quarter figure is a *running* total because that window may still be in progress.

A **`Require after-hours qualification`** checkbox lets you model the difference between paying the $/RVU bonus only to those who cleared the gate vs. everyone. A **`Hide names`** checkbox swaps names for IDs (for screen-sharing).

Below is a **read-only schedule grid** for the window (date × attending, cells colored by shift); hovering a day shows the actual wRVU/exams read that day.

### 3. Comp — What-If
*An interactive model of the whole section's compensation — move the levers, see total comp change.*

Global sliders (top): **RVU growth %**, **TNS increase %**, **$/RVU rate** (default ~51), **Section bonus %**, a **Project to one year** toggle, and buttons to **Distribute evenly by FTE** or **Reset to actual**.

The heart of the tab is the per-attending table where **each row's Total RVUs is a slider with a lock (🔒/🔓)**. Dragging one attending's RVUs up redistributes the difference across the *unlocked* others (by FTE), so the **section RVU total is held constant** — this models "what if this person read more and that person less." Summary cards up top track the section RVU target vs. what's currently allocated (drift shown in amber).

Columns include `70th Bench`, `Daytime+Call`, `After-Hours`, a **`Qual?`** flag (✓ / ✓\* new-hire / ✗ for the bonus gate), `Bonus RVUs`, and the comp components (`$/RVU Payout`, `ER Pay`, `TNS`, `TAT`, `Sect Bonus`, `Total Comp` — all blurred in the demo). A collapsible **ER Shift Allocation** panel lets you hand out weekday/weekend ER shifts (some attendings are excluded per section rules) and see the pay impact.

### 4. Schedule Planning
*Forward planning: generate a candidate schedule with the solver, edit it, preview its RVU/qualification impact, and (in the real app) publish it back to the Google Sheet.*

This is the showcase feature.

- **+ Generate candidate** opens a modal, picks a date window (the demo defaults to the open month), and runs the **CP-SAT solver**. It honors lock-ins (vacations, conferences, prior assignments) and section rules (per-attending shift eligibility, FTE-weighted monthly RVU floors, call/coverage), and **streams the solver's progress live**. The result is saved as a **candidate in your browser only** — nothing is published.
- A generated candidate is **drag-editable** (swap two attendings on the same day; ⌘/Ctrl-Z to undo; lock-ins are protected).
- The **per-attending projection** table shows each attending's `Projected RVUs`, `65th target`, and **`Qual %`** — i.e. whether this schedule would put them over the qualification threshold — plus the projected comp columns (blurred in the demo).
- A **Moonlight Shifts** panel lets you distribute the optional after-hours shifts per month (with the wRVU each is worth), since *when* a shift happens affects which qualification window it lands in.
- **↗ Publish to Sheet** writes the candidate to a working tab on the section spreadsheet. *(Disabled in the demo — it never touches a real sheet.)*

### 5. My Stats
*A single attending's personal view — "am I hitting my benchmark, and do I qualify for the bonus?"*

Admins get an **impersonation picker** to preview any attending's view; individual users see only their own.

- A big headline **`% of your FTE-prorated 65th-percentile benchmark`**, split into daytime vs. after-hours contribution.
- An **After-hours bonus qualification** card with two tiles — **this quarter** (qualified ✓ / not) and **next quarter — on track?** (a running %) — each naming the three months counted.
- A **Fiscal-year projection**: past actual production + wRVUs expected from shifts already on the schedule + a fallback historical pace for unscheduled days, giving a projected year-end total vs. the annual target. A breakdown table itemizes each piece.
- A **per-month table** (with an after-hours CSV download); future months are projected and shown in blue italics.

### 6. Pay Projection
*An annualized salary projection for the fiscal year, component by component.*

- A top-line **projected FY total** (blurred in the demo), = past actual + future scheduled + projected after-hours.
- A **weekend-ER what-if slider** — drag to model doing more weekend ER shifts (each $2,080) and watch the total update instantly.
- A **Salary components** table: `TNS`, `TAT`, `Section bonus`, `After-hours $/RVU`, `Weekday evening ER`, `Weekend evening ER`, and (when applicable) the `Flex*` **coverage** stipend, each with a plain-language "detail" of how it's computed.
- A **fiscal-year calendar** colored by shift, with per-day hover popups (actual reads for past days, projections for future days; call days flagged).

### 7. Settings *(admin-only — not shown in the public demo)*
In the full app: toggle whether individual users can see the Pay Projection tab, and manage
user accounts (reset a forgotten password back to its initial value). Nothing patient- or
salary-related. Hidden in the demo because its config is mounted read-only there.

---

## Architecture

```
React (Vite) + Tailwind ──HTTP──> Flask API ──> DuckDB (exam / wRVU ledger)
                                      │
                                      └──> OR-Tools CP-SAT scheduler (scheduler/schedule.py)

nginx serves the SPA and proxies /api → Flask.  In production, Caddy terminates HTTPS in front.
```

- `backend/` — Flask API; DuckDB access; the ingest pipeline that normalizes **PowerScribe360** and **mPower** exports into one wRVU ledger; the compensation and after-hours-qualification logic; and the schedule-generation endpoint. `make_demo_dataset.py` builds the de-identified demo dataset from a real database.
- `frontend/` — the React SPA (every tab above; `components/Redacted.jsx` is the demo's dollar-blur).
- `scheduler/schedule.py` — the CP-SAT model and solver.
- `docker-compose.public.yml` + `deploy/Caddyfile` — the containerized public deployment.

## Data & privacy

This repo is **code only**. No patient data, no real database, and no credentials are committed
(see `.gitignore`). The app runs against a **demo dataset that is generated separately** and
mounted at runtime — it never lives in the repo:

- `data/demo.db` — a scrubbed clone of the production DuckDB (fabricated names).
- `data/config-demo/` — fabricated section config.

These are produced by `python -m backend.make_demo_dataset` against a real database and copied
to the deployment server. The live demo above is the result. Real data, the real config, the
Google service-account key, and comp/finance files all stay off GitHub.

## Running the demo locally

With a generated `data/demo.db` + `data/config-demo/` in place:

```bash
export SITE_ADDRESS=localhost
export DEMO_FLASK_SECRET_KEY=$(openssl rand -hex 32)
docker compose -f docker-compose.public.yml -p rvu-demo up -d --build
```

The stack runs with `DEMO_MODE=1` (no external data pulls; publish/refresh are no-ops) and
`FLASK_DEBUG=0`.

## Status

Deployed and in real-world use for a neuroradiology section's productivity and compensation
analysis. This public repository is the de-identified demo build.
