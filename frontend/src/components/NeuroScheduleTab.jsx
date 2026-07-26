import React, { useEffect, useMemo, useState, useRef } from 'react';
import { fetchShiftRvuAverages, fetchFullSchedule, fetchActualProduction, refreshScheduleFromSheets, generateScheduleStream, publishCandidateToSheet, fetchDateRange, fetchScheduleDateRange } from '../api';
import { computeProposedComp, evenIntegerSplit } from '../lib/proposedComp';
import DateRangePicker from './DateRangePicker';
import Redacted from './Redacted';

// Color palette mirroring the scheduler module's LABEL_COLORS so the dashboard view matches the Sheet.
// Module-level cache for the schedule + actuals fetch. Survives component unmount/remount
// (i.e., tab switches) so switching away and back is instant. Keyed by (mode, viewStart,
// viewEnd) so different windows don't share data. TTL bounds staleness — 5 min is long
// enough that tab switches never re-fetch, short enough that "come back to the page later"
// picks up fresh data on the next mount.
const _scheduleCache = new Map();
const _maxDataDateCache = { value: '', ts: 0 };
const _shiftAveragesCache = new Map();
const SCHEDULE_CACHE_TTL_MS = 5 * 60 * 1000;
// Future-mode bootstrap state (refreshScheduleFromSheets + auto-snap to today → last
// scheduled day). Promoted to module scope so switching tabs and coming back doesn't
// re-run the whole procedure. `userPicked` marks that the user has moved the picker at
// any point in this page session, which locks out further auto-snapping.
const _futureBootstrap = {
  done: false,
  userPicked: false,
  start: '',
  end: '',
};

const SHIFT_COLORS = {
  'Inpatient A':    '#b6d7a8',
  'Inpatient B':    '#e06666',
  'Outpatient A':   '#c9daf8',
  'Outpatient B':   '#6fa8dc',
  'Flex/Nights':    '#7030a0',
  'Flex':           '#ead1dc',
  'Academic':       '#fff2cc',
  'Conference':     '#ffc000',
  'Vacation':       '#c00000',
  'Sick':           '#a64d79',
  'Blocked':        '#e0e0e0',
  'Weekend':        '#f3f3f3',
  'Holiday':        '#f3f3f3',
  'UCSF':           '#d9d2e9',
  'No Call':        '#e0e0e0',
};

const DARK_BG_COLORS = new Set(['#7030a0', '#c00000', '#e06666', '#a64d79']);

// Shifts that contribute RVUs in the projection (matches the scheduler's "work shifts").
const RVU_SHIFTS = new Set(['Inpatient A', 'Inpatient B', 'Outpatient A', 'Outpatient B', 'Flex/Nights', 'Flex']);

// Flex* hack: Chang's 20% clinical is bought out by the hospital. He's listed as 'Flex*' weekly
// for bookkeeping; a different attending actually covers the shift, getting normal Flex volume
// and a $2,000 payout. See backend/app.py for the per-attending pay-projection version.
const CHANG_ID = 'ATT000767';
const PETER_COVERAGE_PAY = 2000;
// Return the non-Chang attending who is covering Flex* on a given day, or null if none.
function findPeterCoverer(assignments) {
  if (!assignments) return null;
  for (const [attId, shift] of Object.entries(assignments)) {
    if (attId !== CHANG_ID && shift === 'Flex*') return attId;
  }
  return null;
}

// Off-clinical statuses — any reads on these days were incidental, not part of the schedule.
// Tooltip is suppressed for these so we don't surface misleading actuals.
const OFF_CLINICAL = new Set(['Academic', 'Conference', 'Vacation', 'Sick', 'UCSF', 'Leave', 'Blocked', 'No Call']);

// Cells whose value is one of these are lock-ins from the live sheet (real-world commitments)
// and are NOT draggable or droppable. Academic is solver-assigned (unfilled slots), so it stays
// editable. Holiday/Weekend cells are filtered separately by `is_weekend || is_holiday`.
const LOCK_IN_STATUSES = new Set(['Vacation', 'Conference', 'Blocked', 'UCSF', 'Leave', 'Sick']);

// Moonlight shift archetypes — per-shift wRVU and per-shift exam-count assumptions.
// Used in the Future tab to fold projected moonlight RVUs into each attending's total.
const INPATIENT_A_MOONLIGHT_RVU_PER_SHIFT = 32;  // 22 inpatient/ER reads × ~1.45 wRVU/exam
const AM_FLEX_RVU_PER_SHIFT             = 44;   // 30 inpatient/ER reads × ~1.45 wRVU/exam
const FLEX_OUT_RVU_PER_SHIFT            = 54;   // 30 outpatient MR reads × ~1.8 wRVU/exam
const WEEKEND_ER_RVU_PER_SHIFT          = 49;   // empirical avg from past Weekend Evening ER coverage
const CALL_DAY_RVU                      = 70;   // empirical avg per weekend/holiday call day (per attending)

// Attending IDs that don't take certain moonlight/ER shifts by default.
// (Chow is already filtered out via ATT000772 elsewhere — left section.)
const CHANG_ID_SCHED = 'ATT000767';
const FLORIOLLI_ID_SCHED = 'ATT001132';
const SADIGH_ID_SCHED = 'ATT003605';
const MOONLIGHT_INELIGIBLE = new Set([CHANG_ID_SCHED, FLORIOLLI_ID_SCHED]);  // AM Flex + Flex Out
const WEEKEND_ER_INELIGIBLE = new Set([CHANG_ID_SCHED, FLORIOLLI_ID_SCHED, SADIGH_ID_SCHED]);  // Weekend Evening ER

const CANDIDATES_KEY = 'rvu_schedule_candidates';

function loadCandidatesFromStorage() {
  try { return JSON.parse(localStorage.getItem(CANDIDATES_KEY) || '{}'); }
  catch { return {}; }
}
function saveCandidatesToStorage(map) {
  localStorage.setItem(CANDIDATES_KEY, JSON.stringify(map));
}

// Wide-format CSV (Date,Day,Call,attendings...) → per-day shape matching get_full_schedule.
function widePayloadToFullSchedule(payload, neuroAttendings) {
  const nameToId = {};
  Object.entries(neuroAttendings || {}).forEach(([id, info]) => {
    const lastName = (info.name || '').split(',')[0].trim().toUpperCase();
    if (lastName) nameToId[lastName] = id;
  });
  const norm = s => (s || '').replace(/\s+/g, '').replace(/-/g, '').replace(/\//g, '').toUpperCase();
  const SHIFT_NAME_MAP = {
    'INPATIENTA': 'Inpatient A', 'INPATIENTB': 'Inpatient B',
    'OUTPATIENTA': 'Outpatient A', 'OUTPATIENTB': 'Outpatient B',
    'FLEXNIGHTS': 'Flex/Nights', 'FLEX': 'Flex',
  };
  const normShiftCell = s => {
    if (!s) return '';
    const trailingStar = s.trim().endsWith('*');
    const base = s.trim().replace(/\*$/, '');
    const key = norm(base);
    const out = SHIFT_NAME_MAP[key] || base;
    return trailingStar ? out + '*' : out;
  };

  const header = payload.header || [];
  const dateIdx = header.findIndex(h => h === 'Date');
  const dayIdx = header.findIndex(h => h === 'Day');
  const callIdx = header.findIndex(h => h === 'Call');
  const attCols = [];
  header.forEach((h, i) => {
    const upper = (h || '').trim().toUpperCase();
    if (nameToId[upper]) attCols.push({ idx: i, att_id: nameToId[upper] });
  });

  return (payload.rows || []).map(row => {
    const rawDate = row[dateIdx] || '';
    const parts = rawDate.split('/');
    if (parts.length !== 3) return null;
    let yy = parseInt(parts[2], 10);
    if (Number.isNaN(yy)) return null;
    yy = yy < 50 ? 2000 + yy : (yy < 100 ? 1900 + yy : yy);
    const m = parseInt(parts[0], 10), dd = parseInt(parts[1], 10);
    if (Number.isNaN(m) || Number.isNaN(dd)) return null;
    const isoDate = `${yy}-${String(m).padStart(2, '0')}-${String(dd).padStart(2, '0')}`;
    // JS Date: month is 0-indexed
    const dt = new Date(yy, m - 1, dd);
    const dow = dt.getDay();  // 0 = Sun
    const day = row[dayIdx] || dt.toLocaleDateString('en-US', { weekday: 'short' });
    const callName = (row[callIdx] || '').trim();
    const callId = nameToId[callName.toUpperCase()] || null;

    const assignments = {};
    for (const { idx, att_id } of attCols) {
      const v = normShiftCell(row[idx]);
      if (v) assignments[att_id] = v;
    }
    return {
      date: isoDate,
      weekday: day.slice(0, 3),
      day_of_week: dow,
      is_weekend: dow === 0 || dow === 6,
      is_holiday: Object.values(assignments).some(v => v === 'Holiday'),
      on_call: callId,
      on_call_name: callName ? (Object.entries(neuroAttendings || {}).find(([id]) => id === callId)?.[1]?.name || callName) : null,
      assignments,
    };
  }).filter(Boolean);
}

const fmt$ = v => '$' + Math.round(v).toLocaleString();
const fmtN = v => Math.round(v).toLocaleString();
// "2026-03-01" -> "Mar 2026, Apr 2026, May 2026" — the 3 months of a qualification window.
const windowMonthsLabel = (startIso) => {
  if (!startIso) return '';
  const [y, m] = startIso.split('-').map(Number);
  return [0, 1, 2]
    .map(k => new Date(y, (m - 1) + k, 1).toLocaleDateString('en-US', { month: 'short', year: 'numeric' }))
    .join(', ');
};

// Compute the default date range based on tab mode.
// Past = previous full calendar year. Future = next full fiscal quarter (Jul–Sep, Oct–Dec, Jan–Mar, Apr–Jun).
function defaultDatesForMode(mode) {
  const today = new Date();
  if (mode === 'past') {
    const lastYear = today.getFullYear() - 1;
    return { start: `${lastYear}-01-01`, end: `${lastYear}-12-31` };
  }
  // Future: next FY quarter
  const m = today.getMonth();  // 0 = Jan
  let qStartMonth, qStartYear;
  if (m < 3) { qStartMonth = 3; qStartYear = today.getFullYear(); }            // currently Q3 FY → next is Apr-Jun
  else if (m < 6) { qStartMonth = 6; qStartYear = today.getFullYear(); }       // currently Q4 FY → next is Jul-Sep
  else if (m < 9) { qStartMonth = 9; qStartYear = today.getFullYear(); }       // currently Q1 FY → next is Oct-Dec
  else { qStartMonth = 0; qStartYear = today.getFullYear() + 1; }              // currently Q2 FY → next is Jan-Mar (next yr)
  const start = new Date(qStartYear, qStartMonth, 1);
  const end = new Date(qStartYear, qStartMonth + 3, 0);  // last day of the third month
  const fmt = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  return { start: fmt(start), end: fmt(end) };
}

export default function NeuroScheduleTab({
  startDate, endDate, onStartDateChange, onEndDateChange,
  neuroConfig, proposedCompData, scheduleShiftCounts,
  mode = 'future', hideMoney = false, readOnly = false, isDemo = false, redactMoney = false,
}) {
  // In the demo, the schedule is filled through end of July and August is open. Default the
  // "generate candidate" window to the empty month (August) so Generate works on the first click
  // and stays fast even on a small (1-vCPU) server — a full clean month solves quickly.
  const DEMO_GEN_START = '2026-08-01';
  const DEMO_GEN_END = '2026-08-31';
  // Demo default VIEW window: the whole filled month (July) plus the open month (August) so the
  // "last month done, plan next month" story is visible at a glance.
  const DEMO_VIEW_START = '2026-07-01';
  // Note: `maxDataDate` is intentionally NOT taken from props — this tab fetches its own
  // NEURO-scoped max data date below (line ~617) so preset "to date" ranges anchor to
  // when NEURO exams were last ingested, not the global max across divisions.
  // mode = 'past':   retrospective view — schedule + actuals only, no candidate/projection UI
  // mode = 'future': forward planning — candidate generation, projected RVUs, qualification metrics
  const isPast = mode === 'past';
  const isFuture = mode === 'future';
  // Past mode uses the App-owned shared date range (so a change here reflects on every other
  // tab). Future mode keeps its own local range — its natural default (auto-snap to today
  // → last scheduled day) doesn't make sense to share with the retrospective tabs.
  const initialDefaults = useMemo(() => defaultDatesForMode(mode), [mode]);
  // Future-mode dates: seed from module-level bootstrap cache if we've bootstrapped once
  // already (tab-switch remount path); otherwise seed with the mode's default. Wrapped
  // setters below mirror into the cache so remounts always see the latest values.
  const [localFutureStart, setLocalFutureStartRaw] = useState(
    _futureBootstrap.done && _futureBootstrap.start ? _futureBootstrap.start : initialDefaults.start
  );
  const [localFutureEnd, setLocalFutureEndRaw] = useState(
    _futureBootstrap.done && _futureBootstrap.end ? _futureBootstrap.end : initialDefaults.end
  );
  const setLocalFutureStart = (v) => { _futureBootstrap.start = v; setLocalFutureStartRaw(v); };
  const setLocalFutureEnd = (v) => { _futureBootstrap.end = v; setLocalFutureEndRaw(v); };
  const viewStart = isPast ? startDate : localFutureStart;
  const viewEnd = isPast ? endDate : localFutureEnd;
  const setViewStart = isPast ? (onStartDateChange || (() => {})) : setLocalFutureStart;
  const setViewEnd = isPast ? (onEndDateChange || (() => {})) : setLocalFutureEnd;
  // In past mode the shared range is already persisted by App.jsx, so anything user-set
  // survives navigation — treat it as user-picked from the get-go. In future mode we
  // read from the module-level flag so a "user picked once" state survives remounts.
  const userPickedDates = useRef(isPast || _futureBootstrap.userPicked);

  // Bootstrap-pending gate. On the first Future-mode mount of a page session, we don't
  // want to fire loadAll for the initial default dates only to immediately re-fire when
  // bootstrap snaps them to (today → end of schedule) — the user briefly sees wrong-range
  // data before the modal returns for the correct-range fetch. This flag makes loadAll a
  // no-op until bootstrap resolves. On remount (bootstrap already done) it starts false.
  const [bootstrapPending, setBootstrapPending] = useState(
    isFuture && !_futureBootstrap.done && !_futureBootstrap.userPicked
  );

  const [shiftAverages, setShiftAverages] = useState([]);
  const [fullSchedule, setFullSchedule] = useState([]);
  const [actualProduction, setActualProduction] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [hoverCell, setHoverCell] = useState(null);  // {date, attId, x, y, info}
  const [refreshing, setRefreshing] = useState(false);
  const [lastRefresh, setLastRefresh] = useState(null);

  // Moonlight shift counts (Future tab only). {attId: {amFlex, flexOut}}
  // Inpatient A moonlight is derived from the schedule (auto), not user-edited.
  const [moonlightCounts, setMoonlightCounts] = useState({});
  const [moonlightExpanded, setMoonlightExpanded] = useState(false);

  // Candidate schedules — generated by the solver and stored in localStorage.
  // {[name]: {schedule: [...per-day...], generated_at, start_date, end_date, runtime_s, objective_cost}}
  const [candidates, setCandidates] = useState(loadCandidatesFromStorage);
  const [activeCandidate, setActiveCandidate] = useState(null);  // name or null = imported
  const [generating, setGenerating] = useState(false);
  const [generateModalOpen, setGenerateModalOpen] = useState(false);
  const [genStart, setGenStart] = useState(isDemo ? DEMO_GEN_START : startDate);
  const [genEnd, setGenEnd] = useState(isDemo ? DEMO_GEN_END : endDate);
  const [genName, setGenName] = useState(`candidate-${new Date().toISOString().slice(0, 10)}`);
  const [genError, setGenError] = useState(null);
  const [genLog, setGenLog] = useState([]);          // ['line1', 'line2', ...]
  const [genElapsed, setGenElapsed] = useState(0);    // seconds since start, ticks during generate

  // Anonymize display — toggles attending names to ATT IDs in the schedule grid (column
  // headers, Call column, hover tooltip). Useful when screen-sharing or sending screenshots.
  const [anonymize, setAnonymize] = useState(false);

  // Whether the $/RVU after-hours bonus requires the qualification gate (cleared the
  // FTE-prorated 65th pctile over the 3-month qualification window — M3 of the
  // quarter-before-previous plus M1+M2 of the previous quarter — or new hire). ON = use each
  // attending's real qualification status; OFF = treat everyone as qualified, so you can see
  // what after-hours pay would look like if the gate were dropped.
  const [requireAhQual, setRequireAhQual] = useState(true);
  const displayName = (att) => {
    if (!att) return '';
    if (anonymize) return att.id || '';
    return (att.name || '').split(',')[0];
  };
  // For raw on_call IDs (when we don't have the att object handy), look up via attendingList.
  const displayNameById = (attId, fallbackName) => {
    if (!attId) return '';
    if (anonymize) return attId;
    return (fallbackName || '').split(',')[0];
  };

  // Drag-and-drop swap state. {date, attId} of the cell currently being dragged, or null.
  // Drags are same-date-only and only enabled when an active candidate exists (imported live
  // schedules are read-only). On drop, we swap the two cells' shift values.
  const [dragging, setDragging] = useState(null);

  // Undo stack of swap operations: [{date, attA, attB}, ...]. Swap is involutive — re-running
  // the same {date, attA, attB} swap reverses it. Capped at 50 entries; cleared when the user
  // switches candidates since the operations wouldn't make sense against a different schedule.
  const [undoStack, setUndoStack] = useState([]);
  useEffect(() => { setUndoStack([]); }, [activeCandidate]);

  // Internal: swap without recording undo (used by performSwap and undo). Updates BOTH the
  // structured `schedule` (what the UI reads) and the `wide_rows` payload (what Publish ships
  // to the Google Sheet) — they must stay in lockstep or publish will write stale data.
  const applySwap = (date, attA, attB) => {
    if (!activeCandidate || !candidates[activeCandidate]) return;
    const cand = candidates[activeCandidate];

    // Look up each attending's column in the wide payload (header maps last-name → idx).
    let wideRowsNext = cand.wide_rows;
    let wideHeader = cand.wide_header;
    if (wideHeader && wideRowsNext) {
      const lastNameById = {};
      attendingList.forEach(a => {
        const last = (a.name || '').split(',')[0].trim().toUpperCase();
        if (last) lastNameById[a.id] = last;
      });
      const colByAtt = {};
      wideHeader.forEach((h, i) => {
        const upper = (h || '').trim().toUpperCase();
        Object.entries(lastNameById).forEach(([id, last]) => {
          if (upper === last) colByAtt[id] = i;
        });
      });
      const colA = colByAtt[attA];
      const colB = colByAtt[attB];
      // Match the wide row by date — wide stores as M/D/YY, schedule stores ISO YYYY-MM-DD.
      const [y, m, d] = date.split('-').map(Number);
      const wideDate = `${m}/${d}/${String(y).slice(-2)}`;
      const dateIdx = wideHeader.findIndex(h => h === 'Date');
      if (colA != null && colB != null && dateIdx >= 0) {
        wideRowsNext = wideRowsNext.map(row => {
          if (row[dateIdx] !== wideDate) return row;
          const copy = [...row];
          [copy[colA], copy[colB]] = [copy[colB], copy[colA]];
          return copy;
        });
      }
    }

    const next = {
      ...candidates,
      [activeCandidate]: {
        ...cand,
        schedule: cand.schedule.map(day => {
          if (day.date !== date) return day;
          const a = day.assignments?.[attA];
          const b = day.assignments?.[attB];
          const next_assignments = { ...(day.assignments || {}) };
          if (b === undefined) delete next_assignments[attA]; else next_assignments[attA] = b;
          if (a === undefined) delete next_assignments[attB]; else next_assignments[attB] = a;
          return { ...day, assignments: next_assignments };
        }),
        wide_rows: wideRowsNext,
        modified_at: new Date().toISOString(),
      },
    };
    setCandidates(next);
    saveCandidatesToStorage(next);
  };

  // Public: swap and record on the undo stack. Refuses to swap if either cell is a lock-in.
  const swapShifts = (date, attA, attB) => {
    if (!activeCandidate || !candidates[activeCandidate]) return;
    const day = candidates[activeCandidate].schedule.find(d => d.date === date);
    if (!day) return;
    const valA = day.assignments?.[attA];
    const valB = day.assignments?.[attB];
    if (LOCK_IN_STATUSES.has(valA) || LOCK_IN_STATUSES.has(valB)) return;
    applySwap(date, attA, attB);
    setUndoStack(prev => [...prev.slice(-49), { date, attA, attB }]);
  };

  const undoLastSwap = () => {
    setUndoStack(prev => {
      if (prev.length === 0) return prev;
      const last = prev[prev.length - 1];
      applySwap(last.date, last.attA, last.attB);
      return prev.slice(0, -1);
    });
  };

  // Ctrl+Z (Cmd+Z on Mac) → undo. Ignored when typing in an input/textarea/select. The handler
  // is rebound whenever undoStack / activeCandidate / candidates change so its closure over
  // those values stays fresh.
  useEffect(() => {
    const onKey = (e) => {
      if (!(e.ctrlKey || e.metaKey) || e.key !== 'z' || e.shiftKey) return;
      const t = e.target;
      const tag = t && t.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (undoStack.length === 0) return;
      e.preventDefault();
      undoLastSwap();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [undoStack, activeCandidate, candidates]);

  // Publish-to-Sheet state
  const [publishModalOpen, setPublishModalOpen] = useState(false);
  const [publishTabName, setPublishTabName] = useState('schedule-bot-flex5');
  const [publishDryRun, setPublishDryRun] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [publishResult, setPublishResult] = useState(null);   // { tab_name, sheet_url, last_published }
  const [publishError, setPublishError] = useState(null);

  // Tunable model parameters for the on-tab comp projection (mirrors the Compensation tab defaults).
  const [dollarsPerRvu] = useState(51);
  const [sectionBonusPct] = useState(4);
  const [tnsIncreasePct] = useState(2.2);

  // (Shift RVU averages effect moved below — it depends on `lastDataDate` which is declared later.)

  // Schedule grid + actuals tooltips track the view window, which can be set independently
  // (e.g., to a future candidate's date range). Actuals are fetched for one extra month BEFORE
  // viewStart so the qualification-window calc can pull real RVUs for M3 of Q-1.
  //
  // Cache is module-level (outside the component) so it survives tab-switch unmount/remount.
  // Keyed by (mode, viewStart, viewEnd) with a TTL — coming back to the tab within the TTL
  // window reuses cached data without hitting the backend. Manual refresh (button below) or
  // TTL expiry re-fetches. Cache doesn't invalidate on backend data changes; TTL bounds staleness.
  const loadAll = ({ force = false } = {}) => {
    if (!viewStart || !viewEnd) return Promise.resolve();
    const key = `${mode}|${viewStart}|${viewEnd}`;
    const cached = !force ? _scheduleCache.get(key) : null;
    if (cached && Date.now() - cached.ts < SCHEDULE_CACHE_TTL_MS) {
      setFullSchedule(cached.fullSchedule);
      setActualProduction(cached.actualProduction);
      setError(null);
      setLoading(false);
      return Promise.resolve();
    }
    setLoading(true);
    setError(null);
    // Fetch actuals from one month before viewStart
    const [yy, mm, dd] = viewStart.split('-').map(Number);
    const priorFirst = new Date(yy, mm - 2, 1);  // first day of (viewStart.month - 1)
    const priorIso = `${priorFirst.getFullYear()}-${String(priorFirst.getMonth() + 1).padStart(2, '0')}-01`;
    return Promise.all([
      fetchFullSchedule({ start_date: viewStart, end_date: viewEnd }),
      fetchActualProduction({ start_date: priorIso, end_date: viewEnd }),
    ]).then(([sched, actual]) => {
      const fullSchedule = sched || [];
      const actualProduction = actual || [];
      _scheduleCache.set(key, { fullSchedule, actualProduction, ts: Date.now() });
      setFullSchedule(fullSchedule);
      setActualProduction(actualProduction);
    }).catch(e => setError(String(e)))
      .finally(() => setLoading(false));
  };

  // Check cache eagerly (no debounce delay) — a tab-switch remount should render instantly
  // if data is cached. Only debounce the network fetch on cache miss, since that's what
  // needs coalescing during date-picker changes. Skip entirely while future-mode bootstrap
  // is pending so the initial default-range fetch doesn't fire before bootstrap snaps to
  // the correct range.
  useEffect(() => {
    if (!viewStart || !viewEnd) return;
    if (bootstrapPending) return;
    const key = `${mode}|${viewStart}|${viewEnd}`;
    const cached = _scheduleCache.get(key);
    if (cached && Date.now() - cached.ts < SCHEDULE_CACHE_TTL_MS) {
      setFullSchedule(cached.fullSchedule);
      setActualProduction(cached.actualProduction);
      setLoading(false);
      setError(null);
      return;
    }
    const t = setTimeout(() => { loadAll(); }, 400);
    return () => clearTimeout(t);
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [viewStart, viewEnd, mode, bootstrapPending]);

  // Future-mode bootstrap: FIRST TIME PER PAGE LOAD, pull the latest schedule from the
  // Google Sheet, then snap the view window to "today → last scheduled day". The `done`
  // flag lives in module scope so a tab-switch remount doesn't re-fire the whole
  // procedure (sheets refresh + date-range fetch + view snap). Skipped once the user
  // has picked dates themselves so we never fight their selection.
  useEffect(() => {
    if (!isFuture || _futureBootstrap.done || userPickedDates.current) return;
    _futureBootstrap.done = true;
    (async () => {
      setRefreshing(true);
      try {
        await refreshScheduleFromSheets();
        setLastRefresh(new Date());
      } catch (e) {
        // Refresh is best-effort; even if it fails we can still default the window.
        console.warn('initial refresh-from-sheets failed:', e);
      } finally {
        setRefreshing(false);
      }
      try {
        const r = await fetchScheduleDateRange();
        if (r?.max_date && !userPickedDates.current) {
          const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
          const todayIso = iso(new Date());
          if (isDemo) {
            // Demo: show the filled recent month + the open month generation defaults to,
            // rather than "today → last scheduled day" (which would be a sliver of end-of-month).
            setViewStart(DEMO_VIEW_START);
            setViewEnd(DEMO_GEN_END);
          } else if (r.max_date >= todayIso) {
            // Normal case: schedule extends into the future — show today → last scheduled day.
            setViewStart(todayIso);
            setViewEnd(r.max_date);
          } else {
            // The last *assigned* day is already in the past (e.g. the demo's schedule is real
            // through its data-end and open/blank afterward). A today→max window would be
            // inverted and render nothing, so show the last scheduled month through a forward
            // horizon — the real tail plus the open future you can plan are both visible.
            const maxD = new Date(r.max_date + 'T00:00:00');
            setViewStart(iso(new Date(maxD.getFullYear(), maxD.getMonth(), 1)));
            setViewEnd(iso(new Date(maxD.getFullYear(), maxD.getMonth() + 3, 0)));
          }
        }
      } catch (e) {
        console.warn('schedule date-range fetch failed:', e);
      } finally {
        // Release the gate — the viewStart/viewEnd setState above (batched with this) will
        // trigger the loadAll effect one time with the correct range.
        setBootstrapPending(false);
      }
    })();
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [isFuture]);

  // Active schedule data — either the imported live schedule or a candidate from localStorage.
  // Past mode always uses the imported live schedule.
  const activeSchedule = useMemo(() => {
    if (isFuture && activeCandidate && candidates[activeCandidate]) {
      return candidates[activeCandidate].schedule;
    }
    return fullSchedule;
  }, [isFuture, activeCandidate, candidates, fullSchedule]);

  const handleGenerate = async () => {
    setGenerating(true);
    setGenError(null);
    setGenLog([]);
    setGenElapsed(0);
    const t0 = Date.now();
    const tick = setInterval(() => setGenElapsed((Date.now() - t0) / 1000), 250);
    try {
      const { result, error } = await generateScheduleStream(
        { start_date: genStart, end_date: genEnd },
        (evt) => {
          if (evt.type === 'log') {
            setGenLog(prev => [...prev, evt.line]);
          } else if (evt.type === 'ping') {
            // Optional: surface ping by appending an ellipsis line if the log has been quiet
            // Skip — the elapsed timer is already showing motion.
          }
        }
      );
      if (error) {
        setGenError(error.error || 'unknown error');
        return;
      }
      if (!result) {
        setGenError('No result returned from solver.');
        return;
      }
      const schedulePerDay = widePayloadToFullSchedule(result, neuroConfig?.attendings || {});
      // Diagnostics to see why a candidate might come back empty
      console.log('[generate] result header:', result.header);
      console.log('[generate] result rows:', result.rows?.length, 'rows');
      console.log('[generate] sample row:', result.rows?.[0]);
      console.log('[generate] parsed schedulePerDay:', schedulePerDay.length, 'days');
      console.log('[generate] sample parsed day:', schedulePerDay[0]);
      const generatedDates = new Set(schedulePerDay.map(d => d.date));
      const surroundingDays = fullSchedule.filter(d => !generatedDates.has(d.date));
      const merged = [...schedulePerDay, ...surroundingDays].sort((a, b) => a.date.localeCompare(b.date));
      console.log('[generate] merged schedule:', merged.length, 'days');

      const next = {
        ...candidates,
        [genName]: {
          schedule: merged,
          // Keep the raw wide-format payload from the API so Publish can round-trip without
          // lossy reconstruction. Phase 3 (manual editing) will need to update this on edit.
          wide_header: result.header,
          wide_rows: result.rows,
          generated_at: new Date().toISOString(),
          start_date: genStart,
          end_date: genEnd,
          runtime_s: result.runtime_s,
          objective_cost: result.objective_cost,
        },
      };
      setCandidates(next);
      saveCandidatesToStorage(next);
      setActiveCandidate(genName);
      setViewStart(genStart);
      setViewEnd(genEnd);
      userPickedDates.current = true;
      _futureBootstrap.userPicked = true;
      setGenerateModalOpen(false);
    } catch (e) {
      setGenError(String(e));
    } finally {
      clearInterval(tick);
      setGenerating(false);
    }
  };

  const handlePublish = async () => {
    if (!activeCandidate || !candidates[activeCandidate]) {
      setPublishError('Pick a candidate first.');
      return;
    }
    const c = candidates[activeCandidate];
    if (!c.wide_header || !c.wide_rows) {
      setPublishError('This candidate is missing the wide-format payload. Re-generate to enable publishing.');
      return;
    }
    setPublishing(true);
    setPublishError(null);
    setPublishResult(null);
    try {
      const res = await publishCandidateToSheet({
        candidate: { header: c.wide_header, rows: c.wide_rows },
        tab_name: publishTabName,
        dry_run: publishDryRun,
      });
      if (res.error) {
        setPublishError(res.error);
        return;
      }
      setPublishResult(res);
      // Stamp the candidate with last-published metadata so the user can see it later
      const next = {
        ...candidates,
        [activeCandidate]: {
          ...c,
          last_published_tab: res.tab_name,
          last_published_at: res.last_published || new Date().toISOString(),
        },
      };
      setCandidates(next);
      saveCandidatesToStorage(next);
    } catch (e) {
      setPublishError(String(e));
    } finally {
      setPublishing(false);
    }
  };

  const handleDeleteCandidate = (name) => {
    if (!confirm(`Delete candidate "${name}"?`)) return;
    const next = { ...candidates };
    delete next[name];
    setCandidates(next);
    saveCandidatesToStorage(next);
    if (activeCandidate === name) setActiveCandidate(null);
  };

  const handleRefreshFromSheets = async () => {
    setRefreshing(true);
    try {
      const result = await refreshScheduleFromSheets();
      if (result.error) {
        setError(`Sheet refresh failed: ${result.error}`);
      } else {
        setLastRefresh(new Date());
        await loadAll();  // reload schedule with the new CSV cache
      }
    } catch (e) {
      setError(`Sheet refresh failed: ${e}`);
    } finally {
      setRefreshing(false);
    }
  };

  // Map: shift_name -> {avg_rvu, avg_exams} (for projection)
  const avgByShift = useMemo(() => {
    const m = {};
    for (const s of shiftAverages) m[s.shift_name] = { avg_rvu: s.avg_rvu, avg_exams: s.avg_exams };
    return m;
  }, [shiftAverages]);

  // Index actual production by `${date}|${att_id}` for fast cell lookup
  const actualByCell = useMemo(() => {
    const m = {};
    for (const r of actualProduction) {
      m[`${r.date}|${r.attending_id}`] = { exams: r.exams, rvu: r.rvu };
    }
    return m;
  }, [actualProduction]);

  // Last date with data (for past-vs-future detection within the loaded window).
  const lastDataDate = useMemo(() => {
    let max = '';
    for (const r of actualProduction) if (r.date > max) max = r.date;
    return max;
  }, [actualProduction]);

  // Latest date in the exams table — scoped to NEURO so the anchor matches what the solver
  // and shift-averages query actually use. Other divisions (MSK, BODY, etc.) may be ingested
  // slightly later than NEURO, so the global max would overshoot. Fetched once on mount, but
  // cached module-level so tab-switch remounts within the TTL window skip the round trip.
  const [maxDataDate, setMaxDataDate] = useState(
    _maxDataDateCache.value && Date.now() - _maxDataDateCache.ts < SCHEDULE_CACHE_TTL_MS
      ? _maxDataDateCache.value : ''
  );
  useEffect(() => {
    if (_maxDataDateCache.value && Date.now() - _maxDataDateCache.ts < SCHEDULE_CACHE_TTL_MS) return;
    fetchDateRange({ date_field: 'report_finalized_date', division: 'NEURO' })
      .then(r => {
        if (r && r.max_date) {
          const iso = String(r.max_date).slice(0, 10);
          _maxDataDateCache.value = iso;
          _maxDataDateCache.ts = Date.now();
          setMaxDataDate(iso);
        }
      })
      .catch(() => {});
  }, []);

  // Shift RVU averages source depends on tab mode:
  //   Future (planning) — last 3 months of AVAILABLE data (anchored to `maxDataDate`, the
  //     latest date in the exams table) so the cards aren't diluted by ingestion lag. Matches
  //     what the solver uses internally for its monthly RVU floor projection.
  //   Past (retrospective) — uses the view window itself, so when the user inspects e.g. Q1 2025
  //     the cards show actual averages from that quarter, lining up with the projection-vs-actual
  //     comparison column below.
  useEffect(() => {
    let start, end;
    if (isFuture) {
      if (!maxDataDate) return;
      end = new Date(maxDataDate);
      start = new Date(maxDataDate); start.setDate(start.getDate() - 92);
    } else {
      if (!viewStart || !viewEnd) return;
      start = new Date(viewStart);
      end = new Date(viewEnd);
    }
    const iso = d => d.toISOString().slice(0, 10);
    const startIso = iso(start), endIso = iso(end);
    const key = `${startIso}|${endIso}`;
    const cached = _shiftAveragesCache.get(key);
    if (cached && Date.now() - cached.ts < SCHEDULE_CACHE_TTL_MS) {
      setShiftAverages(cached.value);
      return;
    }
    fetchShiftRvuAverages({ start_date: startIso, end_date: endIso, date_field: 'report_finalized_date' })
      .then(avgs => {
        const value = avgs || [];
        _shiftAveragesCache.set(key, { value, ts: Date.now() });
        setShiftAverages(value);
      })
      .catch(e => setError(String(e)));
  }, [isFuture, viewStart, viewEnd, maxDataDate]);

  // List of attendings to display (from neuroConfig). Order: by name.
  // Chow (ATT000772) is excluded — he left the section. Solver also doesn't include him.
  const attendingList = useMemo(() => {
    if (!neuroConfig?.attendings) return [];
    return Object.entries(neuroConfig.attendings)
      .filter(([id]) => id !== 'ATT000772')  // Chow — departed
      .map(([id, info]) => ({
        id,
        name: info.name,
        fte: info.fte,
      })).sort((a, b) => a.name.localeCompare(b.name));
  }, [neuroConfig]);

  // Period length in fractional months (used for TNS × months, Annualized × 12 / months,
  // and periodFrac benchmark proration). Fractional so a 45-day view prorates as ~1.48
  // months rather than rounding to 1 and understating the target by a third.
  const periodMonths = useMemo(() => {
    if (!viewStart || !viewEnd) return 0;
    const s = new Date(viewStart);
    const e = new Date(viewEnd);
    const diffMs = e - s + 24 * 60 * 60 * 1000;  // inclusive end
    return Math.max(0, diffMs / (1000 * 60 * 60 * 24 * 30.4375));
  }, [viewStart, viewEnd]);

  const periodFrac = useMemo(() => periodMonths / 12, [periodMonths]);

  // List of YYYY-MM month keys covered by the view window (e.g., ['2026-07','2026-08','2026-09']).
  const monthsInView = useMemo(() => {
    if (!viewStart || !viewEnd) return [];
    const [sy, sm] = viewStart.split('-').map(Number);
    const [ey, em] = viewEnd.split('-').map(Number);
    if (!sy || !sm || !ey || !em) return [];
    const out = [];
    let y = sy, m = sm;
    while (y < ey || (y === ey && m <= em)) {
      out.push(`${y}-${String(m).padStart(2, '0')}`);
      m += 1;
      if (m > 12) { m = 1; y += 1; }
    }
    return out;
  }, [viewStart, viewEnd]);

  // Weekend + holiday day counts per month in the view window — each month's moonlight pool size.
  const weekendHolidayDaysByMonth = useMemo(() => {
    const m = {};
    for (const key of monthsInView) m[key] = 0;
    for (const day of activeSchedule) {
      if (!day.date) continue;
      if (viewStart && day.date < viewStart) continue;
      if (viewEnd && day.date > viewEnd) continue;
      if (!(day.is_weekend || day.is_holiday)) continue;
      const key = day.date.slice(0, 7);
      if (key in m) m[key] += 1;
    }
    return m;
  }, [activeSchedule, viewStart, viewEnd, monthsInView]);

  // Total weekend + holiday days in the view window — sum across months.
  const totalMoonlightDays = useMemo(
    () => Object.values(weekendHolidayDaysByMonth).reduce((s, n) => s + n, 0),
    [weekendHolidayDaysByMonth]
  );

  // Eligible attendings for AM Flex / Flex Out (everyone except Chang, Floriolli; Chow already filtered).
  const moonlightEligibleIds = useMemo(
    () => attendingList.filter(a => !MOONLIGHT_INELIGIBLE.has(a.id)).map(a => a.id),
    [attendingList]
  );
  // Eligible for Weekend Evening ER (everyone except Sadigh; Chow already filtered).
  const weekendErEligibleIds = useMemo(
    () => attendingList.filter(a => !WEEKEND_ER_INELIGIBLE.has(a.id)).map(a => a.id),
    [attendingList]
  );

  // Qualification window: M3 of Q-1 + M1 of Q + M2 of Q (where Q is the candidate quarter).
  // - Window START = first day of the calendar month BEFORE viewStart (M3 of Q-1) — historical actuals.
  // - Window END   = last day of the SECOND calendar month after viewStart (candidate M2) — projected.
  // The month immediately preceding the candidate's qualifying quarter (Q.M3 = candidate M3) is
  // skipped per the new comp model. Projecting these 3 months tells the user whether the candidate
  // is likely to qualify attendings for the $/RVU bonus in the next eligible quarter.
  const qualiWindowStartDate = useMemo(() => {
    if (!viewStart) return null;
    const [yy, mm] = viewStart.split('-').map(Number);
    if (!yy || !mm) return null;
    const prior = new Date(yy, mm - 2, 1);  // first day of viewStart.month - 1
    const py = prior.getFullYear();
    const pm = String(prior.getMonth() + 1).padStart(2, '0');
    return `${py}-${pm}-01`;
  }, [viewStart]);

  // Quali window = exactly 3 months — keep calendar-aligned so the target matches 10,179 × FTE × 3/12.
  // qualiPreview (below) derives the three month keys from qualiWindowStartDate directly, so a
  // separate end-date is no longer needed.
  const qualiWindowFrac = 3 / 12;

  // Per-attending, per-month projected RVUs. Every month in the candidate matters because
  // every month feeds into some future qualification window — so we want each month above its
  // FTE-prorated 65th threshold. Returns {attId: {monthKey: projectedRvus}}.
  const monthlyProjection = useMemo(() => {
    const perAtt = {};
    attendingList.forEach(a => {
      perAtt[a.id] = {};
      for (const monthKey of monthsInView) perAtt[a.id][monthKey] = 0;
    });

    // Work-shift RVUs + IA moonlight + weekend/holiday call, per month
    for (const day of activeSchedule) {
      if (!day.date) continue;
      if (viewStart && day.date < viewStart) continue;
      if (viewEnd && day.date > viewEnd) continue;
      const monthKey = day.date.slice(0, 7);
      const isWeekday = !day.is_weekend;
      const peterCoverer = findPeterCoverer(day.assignments);
      for (const [attId, shiftName] of Object.entries(day.assignments || {})) {
        if (!(attId in perAtt)) continue;
        if (!(monthKey in perAtt[attId])) continue;
        if (RVU_SHIFTS.has(shiftName)) {
          perAtt[attId][monthKey] += avgByShift[shiftName]?.avg_rvu || 0;
          if (shiftName === 'Inpatient A' && isWeekday && !day.is_holiday) {
            perAtt[attId][monthKey] += INPATIENT_A_MOONLIGHT_RVU_PER_SHIFT;
          }
        } else if (shiftName === 'Flex*' && attId === peterCoverer) {
          // Non-Chang Flex* coverer earns Flex volume in the projection.
          perAtt[attId][monthKey] += avgByShift['Flex']?.avg_rvu || 0;
        }
      }
      // Weekend/holiday call: credit a constant per-call-day RVU contribution to whoever's on call.
      // Mirrors the solver's monthly RVU floor, which now also folds call into each (worker, month).
      if ((day.is_weekend || day.is_holiday) && day.on_call && day.on_call in perAtt && monthKey in perAtt[day.on_call]) {
        perAtt[day.on_call][monthKey] += CALL_DAY_RVU;
      }
    }

    // Moonlight-panel contributions per month
    for (const id of Object.keys(perAtt)) {
      const monthly = moonlightCounts[id] || {};
      for (const monthKey of monthsInView) {
        perAtt[id][monthKey] += (monthly[monthKey]?.amFlex || 0) * AM_FLEX_RVU_PER_SHIFT;
        perAtt[id][monthKey] += (monthly[monthKey]?.flexOut || 0) * FLEX_OUT_RVU_PER_SHIFT;
        perAtt[id][monthKey] += (monthly[monthKey]?.weekendEr || 0) * WEEKEND_ER_RVU_PER_SHIFT;
      }
    }
    return perAtt;
  }, [activeSchedule, attendingList, avgByShift, viewStart, viewEnd, moonlightCounts, monthsInView]);

  const annual65 = neuroConfig?.annual_rvu_expectation || 10179;
  const annual70 = neuroConfig?.annual_rvu_expectation_70th || 10509;

  // Planning qualification preview — projects each attending's production over the SAME
  // 3-month window the backend qualification gate uses, so the "Qual %" column previews
  // whether a candidate schedule would clear the bonus threshold. Window (from
  // qualiWindowStartDate): [month before viewStart, viewStart month, month after]. The
  // pre-viewStart month is historical actuals (fetched one month before viewStart for exactly
  // this); the two candidate months are projected from the schedule. Threshold = 65th × FTE ×
  // 3/12. A window month beyond viewEnd (short candidate) simply contributes 0 — the preview
  // then under-states, which is the safe direction.
  const qualiPreview = useMemo(() => {
    const out = {};
    if (!qualiWindowStartDate || !viewStart) return out;
    const [wy, wm] = qualiWindowStartDate.split('-').map(Number);
    const windowMonths = [0, 1, 2].map(k => {
      const d = new Date(wy, (wm - 1) + k, 1);
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
    });
    const viewStartMonth = viewStart.slice(0, 7);
    // Per-(attending, month) historical actuals for window months before the candidate starts.
    const actualByAttMonth = {};
    for (const r of actualProduction) {
      const mk = String(r.date).slice(0, 7);
      (actualByAttMonth[r.attending_id] ||= {});
      actualByAttMonth[r.attending_id][mk] = (actualByAttMonth[r.attending_id][mk] || 0) + (r.rvu || 0);
    }
    for (const a of attendingList) {
      let windowRvu = 0;
      for (const mk of windowMonths) {
        windowRvu += mk < viewStartMonth
          ? (actualByAttMonth[a.id]?.[mk] || 0)          // historical actual month
          : (monthlyProjection[a.id]?.[mk] || 0);        // projected candidate month
      }
      const threshold = annual65 * a.fte * qualiWindowFrac;
      out[a.id] = {
        windowRvu,
        threshold,
        pct: threshold > 0 ? (windowRvu / threshold) * 100 : null,
        months: windowMonths,
      };
    }
    return out;
  }, [qualiWindowStartDate, viewStart, actualProduction, attendingList, monthlyProjection, annual65, qualiWindowFrac]);

  // Linearly interpolate a percentile from RVUs given the 65th and 70th anchor benchmarks.
  // Outside [65, 70] this is extrapolation — useful as a relative score, not a true percentile.
  const rvuToPercentile = (rvus, target65, target70) => {
    if (target70 <= target65) return null;
    return 65 + (rvus - target65) / (target70 - target65) * 5;
  };

  // Per-attending shift counts and projected RVUs from the imported schedule.
  // Also separates moonlight RVUs from work-shift RVUs so the comp engine can apply $/RVU
  // bonus to moonlight specifically.
  const projection = useMemo(() => {
    const perAtt = {};
    attendingList.forEach(a => {
      perAtt[a.id] = {
        shifts: {}, total_shifts: 0,
        work_shift_rvus: 0,            // Inpatient A/B, Outpatient A/B, Flex, Flex/Nights
        ia_moonlight_count: 0,         // Inpatient A weekday shifts (auto → moonlight)
        moonlight_rvus: 0,             // sum of all moonlight (IA pre-shift + AM Flex + Flex Out)
        call_day_count: 0,             // weekend + holiday call days
        call_rvus: 0,                  // call_day_count × CALL_DAY_RVU
        peter_coverage_count: 0,       // Flex* days covered for Chang (non-Chang only)
        projected_rvus: 0,             // total = work_shift + moonlight + weekend ER + call + Peter coverage Flex RVUs
      };
    });
    for (const day of activeSchedule) {
      // Filter to the view window — candidates can carry surrounding historical days from
      // the merge at generation time, which would otherwise inflate the projection.
      if (!day.date) continue;
      if (viewStart && day.date < viewStart) continue;
      if (viewEnd && day.date > viewEnd) continue;
      // Inpatient A weekday → that attending also worked Inpatient A moonlight (22 pre-shift reads)
      // Use is_weekend (set correctly on both backend and candidate-parser sides) — day_of_week
      // conventions differ (Python: 0=Mon, JS: 0=Sun) and were causing wrong IA-moonlight counts.
      const isWeekday = !day.is_weekend;
      const peterCoverer = findPeterCoverer(day.assignments);
      for (const [attId, shiftName] of Object.entries(day.assignments || {})) {
        if (!perAtt[attId]) continue;
        if (RVU_SHIFTS.has(shiftName)) {
          perAtt[attId].shifts[shiftName] = (perAtt[attId].shifts[shiftName] || 0) + 1;
          perAtt[attId].total_shifts += 1;
          perAtt[attId].work_shift_rvus += avgByShift[shiftName]?.avg_rvu || 0;
          if (shiftName === 'Inpatient A' && isWeekday && !day.is_holiday) {
            perAtt[attId].ia_moonlight_count += 1;
            perAtt[attId].moonlight_rvus += INPATIENT_A_MOONLIGHT_RVU_PER_SHIFT;
          }
        } else if (shiftName === 'Flex*' && attId === peterCoverer) {
          // Non-Chang Flex* coverage: count it AND credit Flex volume to the coverer.
          perAtt[attId].peter_coverage_count += 1;
          perAtt[attId].work_shift_rvus += avgByShift['Flex']?.avg_rvu || 0;
        }
      }
      // Weekend/holiday call → fixed RVU credit to whoever's on call that day.
      if ((day.is_weekend || day.is_holiday) && day.on_call && perAtt[day.on_call]) {
        perAtt[day.on_call].call_day_count += 1;
        perAtt[day.on_call].call_rvus += CALL_DAY_RVU;
      }
    }
    // Add user-edited AM Flex / Flex Out / Weekend ER shifts (sum across all months in view).
    // Weekend ER reads contribute to total wRVUs but NOT to bonus-eligible moonlight (paid per-shift).
    for (const id of Object.keys(perAtt)) {
      const monthly = moonlightCounts[id] || {};
      let amFlexSum = 0, flexOutSum = 0, weekendErSum = 0;
      for (const monthKey of monthsInView) {
        amFlexSum += monthly[monthKey]?.amFlex || 0;
        flexOutSum += monthly[monthKey]?.flexOut || 0;
        weekendErSum += monthly[monthKey]?.weekendEr || 0;
      }
      perAtt[id].moonlight_rvus += amFlexSum * AM_FLEX_RVU_PER_SHIFT;
      perAtt[id].moonlight_rvus += flexOutSum * FLEX_OUT_RVU_PER_SHIFT;
      perAtt[id].weekend_er_rvus = weekendErSum * WEEKEND_ER_RVU_PER_SHIFT;
      perAtt[id].am_flex_count = amFlexSum;
      perAtt[id].flex_out_count = flexOutSum;
      perAtt[id].weekend_er_count = weekendErSum;
      perAtt[id].projected_rvus =
        perAtt[id].work_shift_rvus + perAtt[id].moonlight_rvus + perAtt[id].weekend_er_rvus + perAtt[id].call_rvus;
    }
    return perAtt;
  }, [activeSchedule, attendingList, avgByShift, moonlightCounts, monthsInView, viewStart, viewEnd]);

  // Default counts per month: distribute that month's weekend+holiday days evenly across eligible.
  // Each month, each shift type uses its own eligibility list:
  //   - amFlex / flexOut → moonlightEligibleIds (excludes Chang, Floriolli)
  //   - weekendEr        → weekendErEligibleIds (excludes Sadigh)
  // Shape: { [attId]: { [monthKey]: { amFlex, flexOut, weekendEr } } }
  const buildDefaultMoonlightCounts = () => {
    const out = {};
    attendingList.forEach(a => { out[a.id] = {}; });
    for (const monthKey of monthsInView) {
      attendingList.forEach(a => {
        out[a.id][monthKey] = { amFlex: 0, flexOut: 0, weekendEr: 0 };
      });
      const monthDays = weekendHolidayDaysByMonth[monthKey] || 0;
      if (moonlightEligibleIds.length > 0) {
        const split = evenIntegerSplit(monthDays, moonlightEligibleIds.length);
        moonlightEligibleIds.forEach((id, i) => {
          out[id][monthKey].amFlex = split[i];
          out[id][monthKey].flexOut = split[i];
        });
      }
      if (weekendErEligibleIds.length > 0) {
        const split = evenIntegerSplit(monthDays, weekendErEligibleIds.length);
        weekendErEligibleIds.forEach((id, i) => {
          out[id][monthKey].weekendEr = split[i];
        });
      }
    }
    return out;
  };

  // Reset moonlight counts whenever schedule, view range, or eligibility changes
  useEffect(() => {
    if (!isFuture) return;
    setMoonlightCounts(buildDefaultMoonlightCounts());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isFuture, activeSchedule, totalMoonlightDays, moonlightEligibleIds.join('|'), weekendErEligibleIds.join('|'), monthsInView.join('|')]);

  // Per-month assigned/unassigned tracking. Each month has its own pool size, each shift type its own counter.
  const moonlightAssignedByMonth = useMemo(() => {
    const out = {};
    for (const monthKey of monthsInView) {
      let am = 0, fo = 0, we = 0;
      for (const attCounts of Object.values(moonlightCounts)) {
        am += attCounts?.[monthKey]?.amFlex || 0;
        fo += attCounts?.[monthKey]?.flexOut || 0;
        we += attCounts?.[monthKey]?.weekendEr || 0;
      }
      out[monthKey] = { amFlex: am, flexOut: fo, weekendEr: we };
    }
    return out;
  }, [moonlightCounts, monthsInView]);

  const moonlightAssignedTotal = useMemo(() => {
    let am = 0, fo = 0, we = 0;
    for (const m of Object.values(moonlightAssignedByMonth)) {
      am += m.amFlex; fo += m.flexOut; we += m.weekendEr;
    }
    return { amFlex: am, flexOut: fo, weekendEr: we };
  }, [moonlightAssignedByMonth]);

  const moonlightUnassignedTotal = {
    amFlex: Math.max(0, totalMoonlightDays - moonlightAssignedTotal.amFlex),
    flexOut: Math.max(0, totalMoonlightDays - moonlightAssignedTotal.flexOut),
    weekendEr: Math.max(0, totalMoonlightDays - moonlightAssignedTotal.weekendEr),
  };

  // Edit a single attending's moonlight count for a specific month — clamp against that month's pool.
  const setMoonlightFor = (id, monthKey, key, value) => {
    setMoonlightCounts(prev => {
      const oldVal = prev[id]?.[monthKey]?.[key] || 0;
      const newVal = Math.max(0, Math.round(value));
      const delta = newVal - oldVal;
      const monthPool = weekendHolidayDaysByMonth[monthKey] || 0;
      let finalVal = newVal;
      if (delta > 0) {
        const monthAssigned = Object.values(prev).reduce((s, attCounts) => s + (attCounts?.[monthKey]?.[key] || 0), 0);
        const available = Math.max(0, monthPool - monthAssigned);
        finalVal = oldVal + Math.min(delta, available);
      }
      return {
        ...prev,
        [id]: {
          ...(prev[id] || {}),
          [monthKey]: { ...(prev[id]?.[monthKey] || {}), [key]: finalVal },
        },
      };
    });
  };

  const distributeMoonlightEvenlyForMonth = (monthKey, key) => {
    // Each shift type uses its own eligibility list
    const recipients = key === 'weekendEr' ? weekendErEligibleIds : moonlightEligibleIds;
    if (recipients.length === 0) return;
    const monthDays = weekendHolidayDaysByMonth[monthKey] || 0;
    const split = evenIntegerSplit(monthDays, recipients.length);
    setMoonlightCounts(prev => {
      const next = { ...prev };
      Object.keys(next).forEach(id => {
        next[id] = { ...(next[id] || {}), [monthKey]: { ...(next[id]?.[monthKey] || {}), [key]: 0 } };
      });
      recipients.forEach((id, i) => {
        next[id] = { ...next[id], [monthKey]: { ...next[id][monthKey], [key]: split[i] } };
      });
      return next;
    });
  };

  const resetMoonlight = () => setMoonlightCounts(buildDefaultMoonlightCounts());

  // Per-attending actual production scoped to the local view window — used both for the
  // Actual column AND to scale the dashboard-global moonlight number down to the window so
  // the Moonlight pay column reflects in-window earnings instead of full-period earnings.
  const actualByAttInWindow = useMemo(() => {
    const out = {};
    for (const r of actualProduction) {
      if (viewStart && r.date < viewStart) continue;
      if (viewEnd && r.date > viewEnd) continue;
      out[r.attending_id] = (out[r.attending_id] || 0) + (r.rvu || 0);
    }
    return out;
  }, [actualProduction, viewStart, viewEnd]);

  // Build comp projection rows. We synthesize attending objects with projected_rvus → moonlight=0
  // so this initial display shows projected total RVUs only; the comp formula uses moonlight for $/RVU.
  // Since the imported schedule doesn't include moonlight, the rvuBonus column will be 0 for projections
  // — that's expected; this view validates total volume and shift-distribution mapping, not bonus pay.
  const compProjection = useMemo(() => {
    if (!proposedCompData?.length) return null;
    // Use the actual qualification status from the API for each attending; layer the projected total RVUs on top.
    const compAttendings = attendingList
      .filter(a => a.id !== 'ATT000772')  // exclude Chow
      .map(a => {
      const real = proposedCompData.find(x => x.attending_id === a.id) || {};
      const projData = projection[a.id] || {};
      // Past tab: real.after_hours_rvus / moonlight_rvus are over the dashboard-global period
      // (often a full year), not the local view window. Scale by the share of the attending's
      // production that falls inside the window: in-window_actual / dashboard_actual.
      const realTotal = real.total_rvus || 0;
      const inWindow = actualByAttInWindow[a.id] || 0;
      const windowScale = realTotal > 0 ? inWindow / realTotal : 1;
      // New comp model: $/RVU bonus paid on all qualifying after-hours wRVU, not just
      // moonlight-tagged reads. Schedule's "After Hours" column reflects this.
      const afterHours = isFuture
        ? (projData.moonlight_rvus || 0)   // future projection still uses moonlight estimate
        : ((real.after_hours_rvus ?? real.moonlight_rvus ?? 0) * windowScale);
      return {
        attending_id: a.id,
        attending_name: a.name,
        fte: a.fte,
        monthly_tns: real.monthly_tns || 38000,
        benchmark_70th: real.benchmark_70th || 0,
        daytime_rvus: 0,
        call_rvus: isFuture ? (projData.call_rvus || 0) : 0,
        evening_er_rvus: 0,
        after_hours_rvus: afterHours,
        // moonlight_rvus kept around for any code that still reads it (e.g., debug); engine
        // prefers after_hours_rvus.
        moonlight_rvus: afterHours,
        total_rvus: projData.projected_rvus || 0,
        // Pass through Peter coverage shift count so the comp engine adds the $2k payout.
        peter_coverage_count: projData.peter_coverage_count || 0,
        // When the qualification gate is off, everyone is treated as qualified so the
        // bonus reflects all after-hours wRVU regardless of the qualification-window threshold.
        qualified_for_rvu_bonus: requireAhQual ? (real.qualified_for_rvu_bonus ?? true) : true,
        is_new_hire: real.is_new_hire ?? false,
      };
    });

    // ER shift counts:
    //   - weekday Flex/Nights → counted from the active schedule WITHIN the view window
    //     (for a Future candidate that's the candidate's Flex/Nights assignments; for Past, the
    //     imported live schedule for the selected period). The dashboard-global
    //     scheduleShiftCounts covers the wrong date range, so it's not used here.
    //   - weekend Evening ER → from the user-edited per-month moonlight panel (Future tab).
    const erCounts = {};
    attendingList.forEach(a => { erCounts[a.id] = { weekday: 0, weekend: 0 }; });
    // Count weekday Flex/Nights from activeSchedule, scoped to viewStart..viewEnd
    for (const day of activeSchedule) {
      if (!day.date) continue;
      if (viewStart && day.date < viewStart) continue;
      if (viewEnd && day.date > viewEnd) continue;
      if (day.is_weekend || day.is_holiday) continue;  // weekday only
      for (const [attId, shiftName] of Object.entries(day.assignments || {})) {
        if (shiftName === 'Flex/Nights' && erCounts[attId]) {
          erCounts[attId].weekday += 1;
        }
      }
    }
    if (isFuture) {
      for (const id of Object.keys(erCounts)) {
        erCounts[id].weekend = projection[id]?.weekend_er_count || 0;
      }
    }

    return computeProposedComp({
      attendings: compAttendings,
      erShiftCounts: erCounts,
      modelParams: { dollarsPerRvu, sectionBonusPct, tnsIncreasePct, periodMonths },
    });
  }, [attendingList, projection, proposedCompData, activeSchedule, viewStart, viewEnd, isFuture, dollarsPerRvu, sectionBonusPct, tnsIncreasePct, periodMonths, actualByAttInWindow, requireAhQual]);

  // Comparison: actual RVUs from proposedCompData vs projected from this tool, per attending.
  // Also carries 65th-target and projected-percentile (full + M1+M2) fields per request.
  const comparison = useMemo(() => {
    // (actualByAttInWindow is lifted above so compProjection can reuse it.)
    return attendingList.map(a => {
      const real = (proposedCompData || []).find(x => x.attending_id === a.id) || {};
      const proj = projection[a.id]?.projected_rvus || 0;
      const monthly = monthlyProjection[a.id] || {};
      // Per-month FTE-prorated 65th threshold (annual_65 × FTE × 1/12)
      const monthlyTarget65 = annual65 * a.fte / 12;
      const monthlyPcts = monthsInView.map(mk => ({
        month: mk,
        proj: monthly[mk] || 0,
        pct: monthlyTarget65 > 0 ? ((monthly[mk] || 0) / monthlyTarget65) * 100 : null,
      }));
      const worstMonth = monthlyPcts.reduce((acc, m) => (acc === null || (m.pct != null && m.pct < acc.pct) ? m : acc), null);
      const monthsBelow65 = monthlyPcts.filter(m => m.pct != null && m.pct < 100).length;
      // Actual is now window-scoped — sums per-day production within viewStart..viewEnd.
      const actual = actualByAttInWindow[a.id] || 0;
      // For the actual-vs-projected comparison we want to subtract production that isn't
      // captured by the projection (moonlight, weekend evening ER). proposedCompData's
      // moonlight breakdown is whole-period; we scale it by the fraction of that period
      // that falls inside the local view window so the subtraction is consistent.
      const realPeriodRvu = real.total_rvus || 0;
      const realMoonlight = real.moonlight_rvus || 0;
      const realWeekendEveningEr = real.weekend_evening_er_rvus ?? 0;
      const subtractScale = realPeriodRvu > 0 ? actual / realPeriodRvu : 1;
      const actualOnSchedule = actual - (realMoonlight + realWeekendEveningEr) * subtractScale;
      // proposedCompData is already fetched for the tab's date range (past mode shares
      // startDate/endDate with the tab's viewStart/viewEnd), so use its after-hours total
      // directly.
      const afterHoursInWindow = real.after_hours_rvus ?? real.moonlight_rvus ?? 0;
      // FTE-prorated benchmarks for the candidate's full period
      const target65 = annual65 * a.fte * periodFrac;
      const target70 = annual70 * a.fte * periodFrac;
      return {
        id: a.id,
        name: a.name,
        actual,
        actual_excl_after_hours: actualOnSchedule,
        after_hours_in_window: afterHoursInWindow,
        projected: proj,
        target_65: target65,
        target_70: target70,
        // Pct of 65th-percentile benchmark — 100 = at threshold, qualified for $/RVU bonus
        pct_of_65: target65 > 0 ? (proj / target65) * 100 : null,
        // Per-month metrics — every month feeds into some qualification window
        monthly_pcts: monthlyPcts,
        worst_month_pct: worstMonth?.pct ?? null,
        worst_month_key: worstMonth?.month ?? null,
        months_below_65: monthsBelow65,
        monthly_target_65: monthlyTarget65,
        // Δ = actual − projected: positive = actual exceeded prediction, negative = fell short
        diff: actualOnSchedule - proj,
        diffPct: proj > 0 ? (actualOnSchedule - proj) / proj * 100 : 0,
      };
    });
  }, [proposedCompData, attendingList, projection, monthlyProjection, monthsInView, periodFrac, annual65, annual70, actualByAttInWindow]);

  // Column totals for the per-attending table footer. Sums the comp-engine rows (which
  // already exclude Chow) plus the comparison rows for RVU/target columns. Percentage
  // columns are section-weighted where meaningful and blank otherwise.
  const tableTotals = useMemo(() => {
    const compRows = compProjection?.rows || [];
    const cmpVisible = (comparison || []).filter(c => c.id !== 'ATT000772');
    const attVisible = attendingList.filter(a => a.id !== 'ATT000772');
    const sc = (f) => compRows.reduce((s, r) => s + (f(r) || 0), 0);
    const sp = (f) => cmpVisible.reduce((s, c) => s + (f(c) || 0), 0);
    const actual = sp(c => c.actual);
    const projected = sp(c => c.projected);
    const target65 = sp(c => c.target_65);
    return {
      fte: attVisible.reduce((s, a) => s + (a.fte || 0), 0),
      shifts: attVisible.reduce((s, a) => s + (projection[a.id]?.total_shifts || 0), 0),
      actual, projected, target65,
      pctOf65: target65 > 0 ? (actual / target65) * 100 : null,
      projPctOf65: target65 > 0 ? (projected / target65) * 100 : null,
      afterHrsWrvu: sp(c => c.after_hours_in_window),
      tns: sc(r => r.tns), tat: sc(r => r.tat), sectionBonus: sc(r => r.sectionBonus),
      erPay: sc(r => r.erPay), rvuBonus: sc(r => r.rvuBonus), total: sc(r => r.total),
    };
  }, [compProjection, comparison, attendingList, projection]);

  // The qualification windows are section-wide (same for every attending), so read them off
  // any row to label the Qual column headers with the actual 3 months being counted.
  const _sampleComp = (proposedCompData || [])[0] || {};
  const currQualMonths = windowMonthsLabel(_sampleComp.qualifying_window_start);
  const nextQualMonths = windowMonthsLabel(_sampleComp.qualifying_window_next_start);

  // First-load overlay for future mode. Bootstrap does a Google Sheets refresh which can
  // take 10-20s over a cold network; a plain "Loading…" line got mistaken for a stuck app.
  // Show a modal with a spinner and phase-appropriate copy through the WHOLE bootstrap:
  // refreshing (Sheets pull) → bootstrapPending gate (waiting for date-range fetch) →
  // loading (final DB fetch for the snapped range). Only clears when all three are done
  // AND we have data to render.
  const showInitialModal = !fullSchedule.length && (refreshing || loading || bootstrapPending);
  if (showInitialModal) {
    return (
      <div className="bg-white rounded-lg shadow p-12 flex flex-col items-center justify-center gap-3 min-h-[300px]">
        <svg className="animate-spin h-8 w-8 text-blue-600" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
        </svg>
        <div className="text-gray-700 font-medium">
          {refreshing ? 'Refreshing from Google Sheet…' : 'Loading schedule…'}
        </div>
        {refreshing && (
          <div className="text-xs text-gray-500 max-w-sm text-center">
            First load of the session — pulling the latest schedule from Sheets. This can
            take 10-20 seconds. Subsequent tab switches are cached and instant.
          </div>
        )}
      </div>
    );
  }
  if (error) {
    return <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">Failed to load schedule: {error}</div>;
  }
  if (!fullSchedule.length) {
    return <div className="bg-white rounded-lg shadow p-6 text-gray-500">No schedule data for this period.</div>;
  }

  return (
    <div className="space-y-4 relative">
      {/* Generate candidate modal */}
      {generateModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-lg shadow-xl p-6 w-[640px] max-h-[90vh] flex flex-col">
            <h2 className="text-lg font-semibold text-gray-900 mb-2">Generate candidate schedule</h2>
            <p className="text-sm text-gray-500 mb-4">
              Runs the CP-SAT solver against the live Google Sheet for the chosen window.
              Lock-ins (vacations, conferences, prior assignments) are read from the sheet.
              Result is saved as a candidate in this browser only — nothing is published back.
              Typically takes ~60 seconds.
            </p>
            <div className="space-y-3">
              <label className="block">
                <span className="text-sm font-medium text-gray-700">Candidate name</span>
                <input type="text" value={genName} onChange={e => setGenName(e.target.value)} disabled={generating}
                       className="mt-1 block w-full border border-gray-300 rounded px-3 py-1.5 text-sm disabled:bg-gray-100" />
              </label>
              <div className="grid grid-cols-2 gap-3">
                <label className="block">
                  <span className="text-sm font-medium text-gray-700">Start date</span>
                  <input type="date" value={genStart} onChange={e => setGenStart(e.target.value)} disabled={generating}
                         className="mt-1 block w-full border border-gray-300 rounded px-3 py-1.5 text-sm disabled:bg-gray-100" />
                </label>
                <label className="block">
                  <span className="text-sm font-medium text-gray-700">End date</span>
                  <input type="date" value={genEnd} onChange={e => setGenEnd(e.target.value)} disabled={generating}
                         className="mt-1 block w-full border border-gray-300 rounded px-3 py-1.5 text-sm disabled:bg-gray-100" />
                </label>
              </div>
              {(generating || genLog.length > 0) && (
                <div className="mt-2">
                  <div className="flex items-center justify-between text-xs text-gray-500 mb-1">
                    <span>Solver output</span>
                    {generating && <span>elapsed {genElapsed.toFixed(1)} s</span>}
                  </div>
                  <pre className="bg-gray-900 text-green-300 text-xs font-mono rounded p-3 max-h-64 overflow-y-auto whitespace-pre-wrap break-all"
                       ref={el => { if (el) el.scrollTop = el.scrollHeight; }}>
                    {genLog.length === 0 && generating ? 'starting…\n' : genLog.join('\n')}
                  </pre>
                </div>
              )}
              {genError && (
                <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2 whitespace-pre-wrap">
                  {genError}
                </div>
              )}
            </div>
            <div className="flex justify-end gap-2 mt-6">
              <button
                onClick={() => setGenerateModalOpen(false)}
                disabled={generating}
                className="px-4 py-2 rounded-md text-sm font-medium bg-gray-200 text-gray-700 hover:bg-gray-300 disabled:opacity-50"
              >Cancel</button>
              <button
                onClick={handleGenerate}
                disabled={generating || !genName || !genStart || !genEnd}
                className="px-4 py-2 rounded-md text-sm font-medium bg-purple-600 text-white hover:bg-purple-700 disabled:opacity-50"
              >
                {generating ? `Generating… (${genElapsed.toFixed(0)} s)` : 'Generate'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Publish to Google Sheet modal */}
      {publishModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-lg shadow-xl p-6 w-[520px]">
            <h2 className="text-lg font-semibold text-gray-900 mb-2">Publish candidate to Google Sheet</h2>
            <p className="text-sm text-gray-500 mb-4">
              Writes the active candidate to a tab on "NEURORAD SECTION MEGA SPREADSHEET".
              The default name <code className="bg-gray-100 px-1 rounded">schedule-bot-flex5</code> matches
              the original scheduler script — overwriting it on each publish keeps things tidy.
              The canonical academic-year tabs are protected and can't be selected.
            </p>
            {activeCandidate && candidates[activeCandidate] && (
              <div className="text-xs text-gray-500 mb-3">
                Candidate: <span className="font-medium">★ {activeCandidate}</span> · {candidates[activeCandidate].start_date} → {candidates[activeCandidate].end_date}
                {candidates[activeCandidate].last_published_at && (
                  <span> · last published {new Date(candidates[activeCandidate].last_published_at).toLocaleString()} to "{candidates[activeCandidate].last_published_tab}"</span>
                )}
              </div>
            )}
            <div className="space-y-3">
              <label className="block">
                <span className="text-sm font-medium text-gray-700">Tab name</span>
                <input type="text" value={publishTabName} onChange={e => setPublishTabName(e.target.value)} disabled={publishing}
                       className="mt-1 block w-full border border-gray-300 rounded px-3 py-1.5 text-sm disabled:bg-gray-100" />
              </label>
              <label className="flex items-center gap-2 text-sm text-gray-700">
                <input type="checkbox" checked={publishDryRun} onChange={e => setPublishDryRun(e.target.checked)} disabled={publishing} />
                Dry run (validate only, don't touch the sheet)
              </label>
              {publishError && (
                <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2 whitespace-pre-wrap">
                  {publishError}
                </div>
              )}
              {publishResult && (
                <div className="text-sm text-green-700 bg-green-50 border border-green-200 rounded px-3 py-2">
                  {publishResult.dry_run ? '✓ Dry run validated' : `✓ Published`} to tab <strong>{publishResult.tab_name}</strong> ({publishResult.rows} rows)
                  {publishResult.sheet_url && (
                    <div className="mt-1">
                      <a href={publishResult.sheet_url} target="_blank" rel="noreferrer" className="text-blue-600 underline">Open spreadsheet ↗</a>
                    </div>
                  )}
                </div>
              )}
            </div>
            <div className="flex justify-end gap-2 mt-6">
              <button onClick={() => setPublishModalOpen(false)} disabled={publishing}
                      className="px-4 py-2 rounded-md text-sm font-medium bg-gray-200 text-gray-700 hover:bg-gray-300 disabled:opacity-50">
                Close
              </button>
              <button onClick={handlePublish} disabled={publishing || !publishTabName}
                      className="px-4 py-2 rounded-md text-sm font-medium bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50">
                {publishing ? 'Publishing…' : (publishDryRun ? 'Run dry test' : 'Publish')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Cell hover popup — rendered at top of tree so it isn't clipped by the schedule's overflow */}
      {hoverCell && (
        <div className="fixed z-50 pointer-events-none bg-gray-900 text-white text-xs rounded shadow-lg px-3 py-2"
             style={{ left: hoverCell.x, top: hoverCell.y, transform: 'translateX(-50%)' }}>
          <div className="font-semibold">{hoverCell.attName} — {hoverCell.date}</div>
          <div className="text-gray-300">{hoverCell.shift}</div>
          {hoverCell.actual ? (
            <div className="mt-1">
              <span className="text-green-300">Actual:</span> {Math.round(hoverCell.actual.rvu)} wRVU · {hoverCell.actual.exams} exams
              {hoverCell.actual.exams === 0 && <div className="text-gray-400 text-[10px]">no reports finalized this day</div>}
            </div>
          ) : hoverCell.projected ? (
            <div className="mt-1">
              <span className="text-blue-300">Projected:</span> {hoverCell.projected.avg_rvu.toFixed(1)} wRVU · {hoverCell.projected.avg_exams.toFixed(1)} exams
              <div className="text-gray-400 text-[10px]">section avg for this shift</div>
            </div>
          ) : null}
        </div>
      )}

      {/* Tab-local date range — independent of the global dashboard picker. Past (Compensation
          Review) caps at the last ingested data date; Future (Schedule Planning) must allow
          picking dates beyond the data end — generating future schedules is the whole point. */}
      <div className="bg-white rounded-lg shadow p-4 flex items-center justify-between">
        <DateRangePicker
          startDate={viewStart}
          endDate={viewEnd}
          onStartDateChange={(v) => { setViewStart(v); userPickedDates.current = true; _futureBootstrap.userPicked = true; }}
          onEndDateChange={(v) => { setViewEnd(v); userPickedDates.current = true; _futureBootstrap.userPicked = true; }}
          maxDate={isPast ? maxDataDate : undefined}
        />
        <div className="text-xs text-gray-500 max-w-md text-right">
          Schedule tab uses its own date range so future candidates show up regardless of the
          dashboard's retrospective window. Switching to a saved candidate auto-syncs the dates.
        </div>
      </div>

      {/* Shift RVU averages */}
      <div className="bg-white rounded-lg shadow p-6">
        <div className="flex items-baseline gap-3 mb-3">
          <h3 className="text-lg font-semibold text-gray-900">Shift RVU averages</h3>
          <span className="text-sm text-gray-500">
            section avg wRVU per shift instance ·{' '}
            {isFuture
              ? (maxDataDate
                  ? `last 3 months of data (through ${maxDataDate}) — used by solver`
                  : 'last 3 months (loading…)')
              : `${viewStart} → ${viewEnd}`}
          </span>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
          {/* Moonlight aggregates very different things (overnight pre-shift reads, holiday
              catch-up, weekend off-hours, etc.) so an average across them is misleading. Skip it. */}
          {shiftAverages.filter(s => s.shift_name !== 'Moonlight').map(s => {
            // Inpatient A days include 22 pre-shift overnight moonlight reads that get re-tagged
            // as Moonlight in the DB. The IA card's avg_rvu therefore excludes those — add a
            // hard-coded +22 wRVU to surface the full IA-day contribution.
            // Demo hides the moonlight parenthetical (no after-hours/comp mechanics on display).
            const showMoonlight = s.shift_name === 'Inpatient A' && !isDemo;
            const withMoonlight = s.avg_rvu + 22;
            return (
              <div key={s.shift_name} className="border border-gray-200 rounded p-3"
                   style={{ borderLeftColor: SHIFT_COLORS[s.shift_name] || '#e5e7eb', borderLeftWidth: 4 }}>
                <div className="text-xs uppercase tracking-wide text-gray-500">{s.shift_name}</div>
                <div className="text-xl font-bold text-gray-900">
                  {s.avg_rvu.toFixed(1)} <span className="text-xs font-normal text-gray-500">wRVU</span>
                  {showMoonlight && (
                    <span className="ml-1">
                      ({withMoonlight.toFixed(1)} <span className="text-xs font-normal text-gray-500">w/ moonlight</span>)
                    </span>
                  )}
                </div>
                <div className="text-xs text-gray-500">{fmtN(s.instances)} instances · total {fmtN(s.total_rvu)}</div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Per-attending summary table — different columns for past vs future */}
      <div className="bg-white rounded-lg shadow overflow-hidden">
        <div className="flex items-baseline gap-3 px-6 py-4 border-b">
          <h3 className="text-lg font-semibold text-gray-900">
            {isPast ? 'Per-attending actuals' : 'Per-attending projection'}
            {isFuture && activeCandidate && <span className="text-purple-700"> — ★ {activeCandidate}</span>}
            {isFuture && !activeCandidate && <span className="text-gray-500"> — Imported (live)</span>}
          </h3>
          <span className="text-sm text-gray-500 flex-1">
            {isPast
              ? 'total wRVUs each attending finalized in the period (includes moonlight, evening ER, call)'
              : 'projected wRVUs from the schedule + planned moonlight, weekend Evening ER, and weekend / holiday call'}
          </span>
          {isPast && !hideMoney && (
            <label
              className="flex items-center gap-1.5 text-sm text-gray-700 cursor-pointer select-none ml-auto"
              title="When on, the $/RVU after-hours bonus is only paid to attendings who cleared the qualification gate (FTE-prorated 65th pctile over the 3-month qualification window — M3 of the quarter-before-previous plus M1+M2 of the previous quarter — or new hire). Turn off to model paying the bonus on everyone's after-hours wRVU."
            >
              <input
                type="checkbox"
                checked={requireAhQual}
                onChange={(e) => setRequireAhQual(e.target.checked)}
                className="rounded border-gray-300"
              />
              Require after-hours qualification
            </label>
          )}
          <label className={`flex items-center gap-1.5 text-sm text-gray-700 cursor-pointer select-none ${isPast && !hideMoney ? '' : 'ml-auto'}`}>
            <input
              type="checkbox"
              checked={anonymize}
              onChange={(e) => setAnonymize(e.target.checked)}
              className="rounded border-gray-300"
            />
            Hide names
          </label>
        </div>
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Attending</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">FTE</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Shifts</th>
                {isFuture && (
                  <>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title="Sum of avg_rvu × shift_count for the candidate">Projected RVUs</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title="FTE-prorated 65th-percentile target for this period (annual_65 × FTE × period_frac)">65th target</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title="Projected production over the 3-month qualification window (month before the candidate start + the candidate's first two months) as a percent of the FTE-prorated 65th target (65th × FTE × 3/12). ≥100 means the candidate would clear the $/RVU bonus qualification threshold.">Qual %</th>
                  </>
                )}
                {isPast && (
                  <>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title="Total actual wRVUs the attending finalized in this period — includes moonlight, evening ER, weekend call, everything.">Actual RVUs</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title="FTE-prorated 65th-percentile target for this period">65th target</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title="Total actual RVUs as a percent of the 65th-percentile target.">% of 65th</th>
                    {hideMoney && <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title="After-hours wRVUs the attending finalized in this window (outside Mon-Fri 8a-5p, excluding Weekend Call and evening ER).">After-hrs wRVU</th>}
                  </>
                )}
                {!hideMoney && (
                  <>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title="Total Negotiated Salary × period">TNS</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title="Turnaround time bonus = 5% of TNS">TAT</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title="Section bonus = sectionBonusPct × TNS">Section</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title="Per-shift ER pay: weekday × $1,840 + weekend × $2,080">ER</th>
                    {isPast && <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title={currQualMonths ? `Counting: ${currQualMonths}` : 'Current-quarter qualification window (3 months)'}>Qual (curr Q)</th>}
                    {isPast && <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title={nextQualMonths ? `Counting: ${nextQualMonths}` : 'Next-quarter qualification window (3 months)'}>Qual (next Q)</th>}
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title="$/RVU on qualifying after-hours wRVU (when qualified). New comp model — broader than legacy moonlight; includes any read outside Mon-Fri 8a-5p Pacific that isn't on Weekend Call or evening ER.">After Hours</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Total</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title="Total scaled to a full year (×12/periodMonths)">Annualized</th>
                  </>
                )}
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
              {attendingList.map(a => {
                if (a.id === 'ATT000772') return null;  // Chow excluded from comp modeling
                const cmp = comparison?.find(c => c.id === a.id);
                const compRow = compProjection?.rows.find(r => r.attending_id === a.id);
                const sign = cmp && cmp.diff > 0 ? '+' : '';
                const diffColor = cmp && Math.abs(cmp.diffPct) < 10 ? 'text-gray-500' : (cmp.diff > 0 ? 'text-green-700' : 'text-red-700');
                const fmtPct = (v) => (v == null ? '—' : `${v.toFixed(0)}%`);
                const pctOf65Color = (v) => {
                  if (v == null) return 'text-gray-500';
                  if (v >= 100) return 'text-green-700';
                  if (v >= 95) return 'text-amber-700';
                  return 'text-red-700';
                };
                const actualPctOf65 = cmp && cmp.target_65 > 0 ? (cmp.actual / cmp.target_65) * 100 : null;
                // Actual % of 65th over the qualification window (past mode). The backend
                // already computes production and threshold over the real 3-month window.
                const real = (proposedCompData || []).find(x => x.attending_id === a.id) || {};
                const qualiPct = real.qualifying_benchmark > 0
                  ? (real.qualifying_rvus / real.qualifying_benchmark) * 100 : null;
                // Next-quarter qualification (running — its window may still be in progress).
                const qualiPctNext = real.qualifying_benchmark_next > 0
                  ? (real.qualifying_rvus_next / real.qualifying_benchmark_next) * 100 : null;
                return (
                  <tr key={a.id} className="hover:bg-gray-50">
                    <td className="px-4 py-2 whitespace-nowrap text-sm font-medium text-gray-900">{anonymize ? a.id : a.name}</td>
                    <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-500 text-right">{a.fte.toFixed(2)}</td>
                    <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-500 text-right">{projection[a.id]?.total_shifts || 0}</td>
                    {isFuture && (
                      <>
                        <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-900 text-right font-medium">{fmtN(cmp?.projected || 0)}</td>
                        <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-500 text-right">{fmtN(cmp?.target_65 || 0)}</td>
                        <td className={`px-4 py-2 whitespace-nowrap text-sm text-right font-medium ${pctOf65Color(qualiPreview[a.id]?.pct)}`}
                            title={qualiPreview[a.id]
                              ? `Qualification window ${qualiPreview[a.id].months.join(', ')}: projected ${fmtN(qualiPreview[a.id].windowRvu)} / 65th×FTE×3/12 target ${fmtN(qualiPreview[a.id].threshold)}`
                              : ''}>
                          {fmtPct(qualiPreview[a.id]?.pct)}
                        </td>
                      </>
                    )}
                    {isPast && (
                      <>
                        <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-900 text-right font-medium">{fmtN(cmp?.actual || 0)}</td>
                        <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-500 text-right">{fmtN(cmp?.target_65 || 0)}</td>
                        <td className={`px-4 py-2 whitespace-nowrap text-sm text-right font-medium ${pctOf65Color(actualPctOf65)}`}
                            title={cmp ? `Actual ${fmtN(cmp.actual)} / 65th target ${fmtN(cmp.target_65)}` : ''}>
                          {fmtPct(actualPctOf65)}
                        </td>
                        {hideMoney && <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-500 text-right">{fmtN(cmp?.after_hours_in_window || 0)}</td>}
                      </>
                    )}
                    {!hideMoney && (
                      <>
                        <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-500 text-right"><Redacted on={redactMoney}>{compRow ? fmt$(compRow.tns) : '—'}</Redacted></td>
                        <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-500 text-right"><Redacted on={redactMoney}>{compRow ? fmt$(compRow.tat) : '—'}</Redacted></td>
                        <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-500 text-right"><Redacted on={redactMoney}>{compRow ? fmt$(compRow.sectionBonus) : '—'}</Redacted></td>
                        <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-500 text-right"><Redacted on={redactMoney}>{compRow ? fmt$(compRow.erPay) : '—'}</Redacted></td>
                        {isPast && <td className={`px-4 py-2 whitespace-nowrap text-sm text-right font-medium ${pctOf65Color(qualiPct)}`}
                            title={real.qualifying_window_start ? `${fmtN(real.qualifying_rvus)} / ${fmtN(real.qualifying_benchmark)} over ${real.qualifying_window_start}..${real.qualifying_window_end}` : ''}>
                          {fmtPct(qualiPct)}
                        </td>}
                        {isPast && <td className={`px-4 py-2 whitespace-nowrap text-sm text-right font-medium ${pctOf65Color(qualiPctNext)}`}
                            title={real.qualifying_window_next_start ? `${fmtN(real.qualifying_rvus_next)} / ${fmtN(real.qualifying_benchmark_next)} over ${real.qualifying_window_next_start}..${real.qualifying_window_next_end} (running — window may be in progress)` : ''}>
                          {fmtPct(qualiPctNext)}
                        </td>}
                        <td className={`px-4 py-2 whitespace-nowrap text-sm text-right ${compRow && compRow.rvuBonus > 0 ? 'text-blue-600 font-medium' : 'text-gray-400'}`}><Redacted on={redactMoney}>{compRow ? fmt$(compRow.rvuBonus) : '—'}</Redacted></td>
                        <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-900 text-right font-medium"><Redacted on={redactMoney}>{compRow ? fmt$(compRow.total) : '—'}</Redacted></td>
                        <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-900 text-right font-medium">
                          <Redacted on={redactMoney}>{compRow && periodMonths > 0 ? fmt$(compRow.total * 12 / periodMonths) : '—'}</Redacted>
                        </td>
                      </>
                    )}
                  </tr>
                );
              })}
            </tbody>
            <tfoot className="bg-gray-50 border-t-2 border-gray-300">
              <tr className="font-semibold text-gray-900">
                <td className="px-4 py-2 whitespace-nowrap text-sm">Total</td>
                <td className="px-4 py-2 whitespace-nowrap text-sm text-right">{tableTotals.fte.toFixed(2)}</td>
                <td className="px-4 py-2 whitespace-nowrap text-sm text-right">{tableTotals.shifts}</td>
                {isFuture && (
                  <>
                    <td className="px-4 py-2 whitespace-nowrap text-sm text-right">{fmtN(tableTotals.projected)}</td>
                    <td className="px-4 py-2 whitespace-nowrap text-sm text-right">{fmtN(tableTotals.target65)}</td>
                    <td className="px-4 py-2 whitespace-nowrap text-sm text-right">{tableTotals.projPctOf65 != null ? `${tableTotals.projPctOf65.toFixed(0)}%` : '—'}</td>
                  </>
                )}
                {isPast && (
                  <>
                    <td className="px-4 py-2 whitespace-nowrap text-sm text-right">{fmtN(tableTotals.actual)}</td>
                    <td className="px-4 py-2 whitespace-nowrap text-sm text-right">{fmtN(tableTotals.target65)}</td>
                    <td className="px-4 py-2 whitespace-nowrap text-sm text-right">{tableTotals.pctOf65 != null ? `${tableTotals.pctOf65.toFixed(0)}%` : '—'}</td>
                    {hideMoney && <td className="px-4 py-2 whitespace-nowrap text-sm text-right">{fmtN(tableTotals.afterHrsWrvu)}</td>}
                  </>
                )}
                {!hideMoney && (
                  <>
                    <td className="px-4 py-2 whitespace-nowrap text-sm text-right"><Redacted on={redactMoney}>{fmt$(tableTotals.tns)}</Redacted></td>
                    <td className="px-4 py-2 whitespace-nowrap text-sm text-right"><Redacted on={redactMoney}>{fmt$(tableTotals.tat)}</Redacted></td>
                    <td className="px-4 py-2 whitespace-nowrap text-sm text-right"><Redacted on={redactMoney}>{fmt$(tableTotals.sectionBonus)}</Redacted></td>
                    <td className="px-4 py-2 whitespace-nowrap text-sm text-right"><Redacted on={redactMoney}>{fmt$(tableTotals.erPay)}</Redacted></td>
                    {isPast && <td className="px-4 py-2 whitespace-nowrap text-sm text-right text-gray-400">—</td>}
                    {isPast && <td className="px-4 py-2 whitespace-nowrap text-sm text-right text-gray-400">—</td>}
                    <td className="px-4 py-2 whitespace-nowrap text-sm text-right text-blue-700"><Redacted on={redactMoney}>{fmt$(tableTotals.rvuBonus)}</Redacted></td>
                    <td className="px-4 py-2 whitespace-nowrap text-sm text-right"><Redacted on={redactMoney}>{fmt$(tableTotals.total)}</Redacted></td>
                    <td className="px-4 py-2 whitespace-nowrap text-sm text-right"><Redacted on={redactMoney}>{periodMonths > 0 ? fmt$(tableTotals.total * 12 / periodMonths) : '—'}</Redacted></td>
                  </>
                )}
              </tr>
            </tfoot>
          </table>
        </div>
        <div className="px-6 py-3 text-xs text-gray-500 border-t">
          {isFuture
            ? "Projected RVUs = scheduled shifts × section-average wRVU per shift, plus planned moonlight (IA / AM Flex / Flex Out), planned weekend Evening ER, and weekend / holiday call. Comp columns use the same proposed-model formula as the Compensation tab."
            : "Actual RVUs = every wRVU each attending finalized in the window — includes moonlight, weekend Evening ER, and weekend / holiday call. Qual (curr Q) / Qual (next Q) = production over each quarter's 3-month qualification window vs the 65th×FTE×3/12 threshold (≥100 clears the $/RVU bonus gate); the next-quarter window may still be in progress, so its % is a running total. Comp columns use the same proposed-model formula as the Compensation tab."}
        </div>
      </div>

      {/* Moonlight shifts panel — Future tab only. Each month gets its own pool & per-attending grid. */}
      {isFuture && (
        <div className="bg-white rounded-lg shadow">
          <div className="flex items-center justify-between px-6 py-3 border-b">
            <button onClick={() => setMoonlightExpanded(!moonlightExpanded)}
                    className="flex items-center gap-2 text-left font-medium text-gray-700 hover:text-gray-900">
              <span className="text-gray-400">{moonlightExpanded ? '▼' : '▶'}</span>
              <span>Moonlight Shifts</span>
              <span className="text-sm font-normal text-gray-500 ml-2">
                IA: auto · AM Flex {moonlightAssignedTotal.amFlex}/{totalMoonlightDays}
                {moonlightUnassignedTotal.amFlex > 0 && <span className="text-amber-700"> ({moonlightUnassignedTotal.amFlex} u)</span>}
                {' · '}Flex Out {moonlightAssignedTotal.flexOut}/{totalMoonlightDays}
                {moonlightUnassignedTotal.flexOut > 0 && <span className="text-amber-700"> ({moonlightUnassignedTotal.flexOut} u)</span>}
                {' · '}Wknd ER {moonlightAssignedTotal.weekendEr}/{totalMoonlightDays}
                {moonlightUnassignedTotal.weekendEr > 0 && <span className="text-amber-700"> ({moonlightUnassignedTotal.weekendEr} u)</span>}
              </span>
            </button>
            <button onClick={resetMoonlight}
                    className="px-3 py-1.5 rounded-md text-sm bg-gray-200 text-gray-700 hover:bg-gray-300">
              Reset all months
            </button>
          </div>
          {moonlightExpanded && (
            <div className="px-6 py-4 space-y-6">
              <div className="text-xs text-gray-500">
                Counts edited per month so M1+M2 calculations reflect when shifts actually happen.
                AM Flex / Flex Out exclude Chang and Floriolli; Weekend ER excludes Sadigh.
                Inpatient A moonlight is automatic — the IA weekday attending picks up 22 inpatient/ER pre-shift reads (~{INPATIENT_A_MOONLIGHT_RVU_PER_SHIFT} wRVU).
                AM Flex ≈ {AM_FLEX_RVU_PER_SHIFT}, Flex Out ≈ {FLEX_OUT_RVU_PER_SHIFT}, Weekend ER ≈ {WEEKEND_ER_RVU_PER_SHIFT} wRVU/shift.
                {hideMoney
                  ? 'Weekend ER reads count toward total RVUs and the 65th threshold but are paid per-shift, not via $/RVU.'
                  : 'Weekend ER reads count toward total RVUs and the 65th threshold but are paid per-shift ($2,080), not via $/RVU.'}
              </div>
              {monthsInView.map((monthKey, monthIdx) => {
                const [yy, mm] = monthKey.split('-');
                const monthLabel = new Date(Number(yy), Number(mm) - 1, 1).toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
                const monthDays = weekendHolidayDaysByMonth[monthKey] || 0;
                const monthAssigned = moonlightAssignedByMonth[monthKey] || { amFlex: 0, flexOut: 0 };
                const monthUnassigned = {
                  amFlex: Math.max(0, monthDays - monthAssigned.amFlex),
                  flexOut: Math.max(0, monthDays - monthAssigned.flexOut),
                };
                // Every month feeds into some future qualification window, so no month gets singled out.
                const isQualiMonth = false;
                return (
                  <div key={monthKey} className={`border rounded-lg ${isQualiMonth ? 'border-blue-300 bg-blue-50' : 'border-gray-200'}`}>
                    <div className="flex items-center justify-between px-4 py-2 border-b border-inherit">
                      <div className="text-sm font-medium text-gray-900">
                        {monthLabel}
                        {/* No per-month qualification badge — every month feeds into some future window */}
                        <span className="ml-3 text-xs font-normal text-gray-500">
                          {monthDays} wknd/hol days · AM Flex {monthAssigned.amFlex}/{monthDays}{monthUnassigned.amFlex > 0 && <span className="text-amber-700"> ({monthUnassigned.amFlex} u)</span>}
                          {' · '}Flex Out {monthAssigned.flexOut}/{monthDays}{monthUnassigned.flexOut > 0 && <span className="text-amber-700"> ({monthUnassigned.flexOut} u)</span>}
                          {' · '}Wknd ER {monthAssigned.weekendEr}/{monthDays}{monthUnassigned.weekendEr > 0 && <span className="text-amber-700"> ({monthUnassigned.weekendEr} u)</span>}
                        </span>
                      </div>
                      <div className="flex gap-2">
                        <button onClick={() => distributeMoonlightEvenlyForMonth(monthKey, 'amFlex')}
                                className="px-2 py-1 rounded text-xs bg-purple-600 text-white hover:bg-purple-700">
                          AM Flex even
                        </button>
                        <button onClick={() => distributeMoonlightEvenlyForMonth(monthKey, 'flexOut')}
                                className="px-2 py-1 rounded text-xs bg-purple-600 text-white hover:bg-purple-700">
                          Flex Out even
                        </button>
                        <button onClick={() => distributeMoonlightEvenlyForMonth(monthKey, 'weekendEr')}
                                className="px-2 py-1 rounded text-xs bg-purple-600 text-white hover:bg-purple-700">
                          Wknd ER even
                        </button>
                      </div>
                    </div>
                    <div className="px-4 py-3">
                      <div className="grid grid-cols-[1fr_auto_auto_auto] gap-x-6 gap-y-1 items-center max-w-3xl">
                        <div className="text-xs font-medium text-gray-500 uppercase">Attending</div>
                        <div className="text-xs font-medium text-gray-500 uppercase text-center">AM Flex</div>
                        <div className="text-xs font-medium text-gray-500 uppercase text-center">Flex Out</div>
                        <div className="text-xs font-medium text-gray-500 uppercase text-center">Wknd ER</div>
                        {attendingList.map(a => {
                          const c = moonlightCounts[a.id]?.[monthKey] || { amFlex: 0, flexOut: 0, weekendEr: 0 };
                          const moonlightAllowed = !MOONLIGHT_INELIGIBLE.has(a.id);
                          const weekendErAllowed = !WEEKEND_ER_INELIGIBLE.has(a.id);
                          const StepInput = ({ value, onChange, disabled, atCap }) => (
                            <div className="flex items-center gap-1 justify-center">
                              <button onClick={() => onChange(value - 1)} disabled={disabled || value <= 0}
                                      className="w-6 h-6 rounded bg-gray-200 hover:bg-gray-300 text-gray-700 text-xs disabled:opacity-30 disabled:cursor-not-allowed">−</button>
                              <input type="number" min="0" value={value} disabled={disabled}
                                     onChange={(e) => onChange(Number(e.target.value))}
                                     className="w-12 border border-gray-300 rounded px-1.5 py-0.5 text-right text-sm disabled:bg-gray-100 disabled:text-gray-400" />
                              <button onClick={() => onChange(value + 1)} disabled={disabled || atCap}
                                      title={atCap && !disabled ? 'Month pool is empty' : undefined}
                                      className="w-6 h-6 rounded bg-gray-200 hover:bg-gray-300 text-gray-700 text-xs disabled:opacity-30 disabled:cursor-not-allowed">+</button>
                            </div>
                          );
                          return (
                            <React.Fragment key={a.id}>
                              <div className="text-sm text-gray-900">{anonymize ? a.id : a.name}</div>
                              <StepInput value={c.amFlex} disabled={!moonlightAllowed}
                                         atCap={monthUnassigned.amFlex <= 0}
                                         onChange={(v) => setMoonlightFor(a.id, monthKey, 'amFlex', v)} />
                              <StepInput value={c.flexOut} disabled={!moonlightAllowed}
                                         atCap={monthUnassigned.flexOut <= 0}
                                         onChange={(v) => setMoonlightFor(a.id, monthKey, 'flexOut', v)} />
                              <StepInput value={c.weekendEr} disabled={!weekendErAllowed}
                                         atCap={monthUnassigned.weekendEr <= 0}
                                         onChange={(v) => setMoonlightFor(a.id, monthKey, 'weekendEr', v)} />
                            </React.Fragment>
                          );
                        })}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Imported schedule grid — bounded height so the wrapper itself becomes the scroll container,
          which is what sticky thead positioning needs to work alongside horizontal scroll. */}
      <div className="bg-white rounded-lg shadow">
        <div className="flex items-center justify-between gap-3 px-6 py-4 border-b">
          <div className="flex items-center gap-3">
            <h3 className="text-lg font-semibold text-gray-900">
              Schedule {isPast ? <span className="text-sm font-normal text-gray-500">— retrospective</span> : <span className="text-sm font-normal text-gray-500">— planning</span>}
            </h3>
            {isFuture && (
              <select
                value={activeCandidate || ''}
                onChange={e => {
                  const name = e.target.value || null;
                  setActiveCandidate(name);
                  if (name && candidates[name]) {
                    setViewStart(candidates[name].start_date);
                    setViewEnd(candidates[name].end_date);
                    userPickedDates.current = true;
                    _futureBootstrap.userPicked = true;
                  }
                }}
                className="text-sm border border-gray-300 rounded px-2 py-1"
              >
                <option value="">Imported (live)</option>
                {Object.keys(candidates).sort().map(name => (
                  <option key={name} value={name}>★ {name}</option>
                ))}
              </select>
            )}
            {isFuture && activeCandidate && candidates[activeCandidate] && (
              <span className="text-xs text-gray-500">
                {candidates[activeCandidate].start_date} → {candidates[activeCandidate].end_date} ·
                {' '}generated {new Date(candidates[activeCandidate].generated_at).toLocaleString()}
                {candidates[activeCandidate].runtime_s != null && ` · ${candidates[activeCandidate].runtime_s}s`}
                {' '}· <span className="text-blue-600">drag a cell to swap with another attending on the same date · ⌘/Ctrl+Z to undo · lock-ins protected</span>
              </span>
            )}
            {(isPast || !activeCandidate) && lastRefresh && (
              <span className="text-xs text-green-700">Refreshed {lastRefresh.toLocaleTimeString()}</span>
            )}
          </div>
          <div className="flex gap-2">
            {(!readOnly || isDemo) && isFuture && activeCandidate && (
              <button
                onClick={() => handleDeleteCandidate(activeCandidate)}
                className="px-3 py-1.5 rounded-md text-sm font-medium bg-red-100 text-red-700 hover:bg-red-200"
              >
                Delete candidate
              </button>
            )}
            {(!readOnly || isDemo) && isFuture && (
              <button
                onClick={() => { setGenStart(isDemo ? DEMO_GEN_START : viewStart); setGenEnd(isDemo ? DEMO_GEN_END : viewEnd); setGenerateModalOpen(true); }}
                className="px-3 py-1.5 rounded-md text-sm font-medium bg-purple-600 text-white hover:bg-purple-700"
              >
                + Generate candidate
              </button>
            )}
            {!readOnly && isFuture && activeCandidate && (
              <button
                onClick={() => { setPublishResult(null); setPublishError(null); setPublishModalOpen(true); }}
                className="px-3 py-1.5 rounded-md text-sm font-medium bg-emerald-600 text-white hover:bg-emerald-700"
              >
                ↗ Publish to Sheet
              </button>
            )}
            {!readOnly && (
              <button
                onClick={handleRefreshFromSheets}
                disabled={refreshing}
                className={`px-3 py-1.5 rounded-md text-sm font-medium ${
                  refreshing
                    ? 'bg-gray-200 text-gray-500 cursor-not-allowed'
                    : 'bg-blue-600 text-white hover:bg-blue-700'
                }`}
              >
                {refreshing ? 'Refreshing…' : '↻ Refresh from Sheet'}
              </button>
            )}
          </div>
        </div>
        <div className="overflow-auto rounded-b-lg" style={{ maxHeight: '75vh' }}>
          <table className="min-w-full text-xs border-separate" style={{ borderSpacing: 0 }}>
            <thead>
              <tr>
                <th className="px-2 py-2 text-left font-medium text-gray-500 uppercase whitespace-nowrap sticky top-0 left-0 z-30 bg-gray-50 border-b">Date</th>
                <th className="px-2 py-2 text-left font-medium text-gray-500 uppercase sticky top-0 z-20 bg-gray-50 border-b">Day</th>
                <th className="px-2 py-2 text-left font-medium text-gray-500 uppercase sticky top-0 z-20 bg-gray-50 border-b">Call</th>
                {attendingList.map(a => (
                  <th key={a.id} className="px-2 py-2 text-left font-medium text-gray-500 uppercase whitespace-nowrap sticky top-0 z-20 bg-gray-50 border-b">
                    {displayName(a)}
                    <div className="text-[10px] font-normal text-gray-400">FTE {a.fte.toFixed(2)}</div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {activeSchedule.map(day => {
                const isWeekend = day.is_weekend;
                const rowBg = day.is_holiday ? '#fffbeb' : isWeekend ? '#f9fafb' : '#ffffff';
                return (
                  <tr key={day.date}>
                    <td className="px-2 py-1.5 whitespace-nowrap font-medium text-gray-700 sticky left-0 z-10 border-b border-gray-100"
                        style={{ backgroundColor: rowBg }}>{day.date.slice(5)}</td>
                    <td className="px-2 py-1.5 text-gray-500 border-b border-gray-100" style={{ backgroundColor: rowBg }}>{day.weekday}</td>
                    <td className="px-2 py-1.5 text-gray-500 whitespace-nowrap border-b border-gray-100" style={{ backgroundColor: rowBg }}>
                      {displayNameById(day.on_call, day.on_call_name)}
                    </td>
                    {attendingList.map(a => {
                      const shift = day.assignments?.[a.id] || (isWeekend ? 'Weekend' : day.is_holiday ? 'Holiday' : '');
                      // Asterisk suffix in the source sheet = shift was reassigned (someone covered).
                      // Color these magenta so they stand out.
                      const isReassigned = typeof shift === 'string' && shift.endsWith('*');
                      const bg = isReassigned ? '#ff66cc' : SHIFT_COLORS[shift];
                      const dark = !isReassigned && DARK_BG_COLORS.has(bg);
                      const cellKey = `${day.date}|${a.id}`;
                      const rawActual = actualByCell[cellKey];
                      const isOffClinical = OFF_CLINICAL.has(shift);
                      const proj = RVU_SHIFTS.has(shift) ? avgByShift[shift] : null;
                      // For past dates (within the data window), treat absence-of-row as 0 actuals
                      // rather than falling through to projected. Future dates use projected.
                      // Named dayInDataWindow (not isPast) to avoid shadowing the outer `isPast`
                      // that means mode === 'past'. A future refactor referencing `isPast` in this
                      // scope would silently get per-day semantics instead of the mode flag.
                      const dayInDataWindow = lastDataDate && day.date <= lastDataDate;
                      const actual = rawActual || (dayInDataWindow ? { exams: 0, rvu: 0 } : null);
                      const hasActual = !isOffClinical && !!actual;
                      const hasInfo = hasActual || (!dayInDataWindow && !!proj);
                      const onEnter = (e) => {
                        if (!hasInfo) return;
                        const rect = e.currentTarget.getBoundingClientRect();
                        setHoverCell({
                          x: rect.left + rect.width / 2,
                          y: rect.bottom + 4,
                          attName: displayName(a),
                          date: day.date,
                          shift,
                          actual: hasActual ? actual : null,
                          projected: proj || null,
                        });
                      };
                      const onLeave = () => setHoverCell(null);

                      // Drag-and-drop swap. Enabled only on weekday non-holiday cells when an
                      // active candidate exists (imported live schedule is read-only). Lock-in
                      // statuses (Vacation, Conference, Blocked, UCSF, Leave) are protected — they
                      // represent real-world commitments from the live sheet and can't be moved.
                      const isLockIn = LOCK_IN_STATUSES.has(shift);
                      const dragEnabled = !!activeCandidate && !isWeekend && !day.is_holiday && !isLockIn;
                      const isDragSource = dragging && dragging.date === day.date && dragging.attId === a.id;
                      // Drop target eligibility: same date as source, different attending, and not a lock-in.
                      const isDropTarget = dragging && dragging.date === day.date && dragging.attId !== a.id && !isLockIn;
                      const onDragStart = (e) => {
                        if (!dragEnabled) { e.preventDefault(); return; }
                        e.dataTransfer.effectAllowed = 'move';
                        e.dataTransfer.setData('text/plain', JSON.stringify({date: day.date, attId: a.id}));
                        setDragging({date: day.date, attId: a.id});
                      };
                      const onDragEnd = () => setDragging(null);
                      const onDragOver = (e) => {
                        if (!isDropTarget) return;
                        e.preventDefault();
                        e.dataTransfer.dropEffect = 'move';
                      };
                      const onDrop = (e) => {
                        e.preventDefault();
                        let src;
                        try { src = JSON.parse(e.dataTransfer.getData('text/plain')); } catch { return; }
                        if (!src || src.date !== day.date || src.attId === a.id) return;
                        swapShifts(day.date, src.attId, a.id);
                        setDragging(null);
                      };
                      return (
                        <td key={a.id}
                            draggable={dragEnabled}
                            onDragStart={onDragStart}
                            onDragEnd={onDragEnd}
                            onDragOver={onDragOver}
                            onDrop={onDrop}
                            className={`px-2 py-1.5 whitespace-nowrap border-b border-gray-100 ${isDropTarget ? 'ring-2 ring-blue-400 ring-inset' : ''}`}
                            style={{
                              backgroundColor: bg || rowBg,
                              color: dark ? 'white' : 'inherit',
                              cursor: dragEnabled ? (dragging ? 'grabbing' : 'grab') : (hasInfo ? 'help' : 'default'),
                              opacity: isDragSource ? 0.4 : 1,
                            }}
                            onMouseEnter={onEnter}
                            onMouseLeave={onLeave}>
                          {shift}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
