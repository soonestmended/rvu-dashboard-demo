import { useState, useEffect, useCallback, useRef } from 'react';
import DateRangePicker from './components/DateRangePicker';
import Filters from './components/Filters';
import SummaryCards from './components/SummaryCards';
import DivisionChart from './components/DivisionChart';
import LocationChart from './components/LocationChart';
import AttendingTable from './components/AttendingTable';
import NeuroSubTabs from './components/NeuroSubTabs';
import NeuroRvuTab from './components/NeuroRvuTab';
import NeuroShiftDistributionTab from './components/NeuroShiftDistributionTab';
import NeuroCompensationTab from './components/NeuroCompensationTab';
import NeuroScheduleTab from './components/NeuroScheduleTab';
import NeuroMyStatsTab from './components/NeuroMyStatsTab';
import NeuroPayProjectionTab from './components/NeuroPayProjectionTab';
import NeuroSettingsTab from './components/NeuroSettingsTab';
import LoginPage from './components/LoginPage';
import ChangePasswordModal from './components/ChangePasswordModal';
import {
  fetchDivisions,
  fetchLocations,
  fetchAttendings,
  fetchDateRange,
  fetchRvusByDivision,
  fetchRvusByLocation,
  fetchRvusByAttending,
  fetchNeuroStaffingMetrics,
  fetchNeuroConfig,
  fetchShiftsByAttending,
  fetchScheduleShiftCounts,
  fetchMoonlightCompensation,
  fetchProposedCompensation,
  fetchMe,
  fetchFeatureFlags,
  logout as logoutApi,
} from './api';

function App() {
  // Auth gate. On mount, check /api/auth/me. If 401, show login page; if OK, store user
  // and render the rest of the app. `authLoading` keeps the initial render blank while we
  // resolve so we don't flash the dashboard for unauthenticated users.
  const [user, setUser] = useState(null);
  const [authLoading, setAuthLoading] = useState(true);
  useEffect(() => {
    fetchMe()
      .then(u => setUser(u))
      .catch(() => setUser(null))
      .finally(() => setAuthLoading(false));
  }, []);
  const handleLogout = async () => {
    await logoutApi();
    setUser(null);
  };
  const [showChangePassword, setShowChangePassword] = useState(false);
  // Briefly flashed banner after a successful password change.
  const [pwToast, setPwToast] = useState(null);
  useEffect(() => {
    if (!pwToast) return;
    const t = setTimeout(() => setPwToast(null), 4000);
    return () => clearTimeout(t);
  }, [pwToast]);
  const isAdmin = user?.role === 'admin';
  const isIndividual = user?.role === 'individual';
  // Demo role: sees Productivity, Schedule (Past/Future), and My Stats — but with all
  // salary/$ figures hidden. Used for over-the-shoulder walkthroughs where showing per-
  // attending TNS/pay would be inappropriate.
  const isDemo = user?.role === 'demo';
  // The fabricated demo stack (DEMO_MODE=1 backend) logs in as an admin, so `role` can't
  // identify it. `/api/auth/me` returns demo_mode for that case — used to seed demo-friendly
  // defaults (e.g. a clean schedule-generation window) without changing admin behavior.
  const isDemoStack = !!user?.demo_mode;

  // Feature flags from /api/settings/feature-flags. Refresh whenever the Settings tab
  // changes them so the sub-tab bar updates immediately. `featureFlagsLoaded` gates the
  // individual-user tab-snap effect below so an individual user isn't briefly bounced off
  // 'pay-projection' before the flag payload arrives (during that window the flag reads as
  // false → their persisted tab isn't in INDIVIDUAL_TABS → they get snapped to 'my-stats').
  const [featureFlags, setFeatureFlags] = useState({});
  const [featureFlagsLoaded, setFeatureFlagsLoaded] = useState(false);
  const refreshFeatureFlags = () =>
    fetchFeatureFlags()
      .then(f => { setFeatureFlags(f); setFeatureFlagsLoaded(true); })
      .catch(() => { setFeatureFlagsLoaded(true); });
  useEffect(() => { if (user) refreshFeatureFlags(); }, [user]);

  // Tabs visible to individual (non-admin) users. Pay Projection is feature-flagged.
  const INDIVIDUAL_TABS = ['my-stats',
    ...(featureFlags.pay_projection_visible_to_individuals ? ['pay-projection'] : [])];
  const DEMO_TABS = ['schedule-past', 'schedule-future', 'my-stats'];

  // Persist tab selection across reloads
  const [activeTab, setActiveTab] = useState(() => localStorage.getItem('rvu_active_tab') || 'neuro');
  const [neuroSubTab, setNeuroSubTab] = useState(() => {
    const saved = localStorage.getItem('rvu_neuro_sub_tab');
    // Migrate legacy ids:
    //   'schedule' → 'schedule-future'
    //   'shifts'   → 'rvu' (shift distribution is now folded into Productivity)
    //   'users'    → 'settings' (Users became one section of a broader Settings tab)
    if (saved === 'schedule') return 'schedule-future';
    if (saved === 'shifts') return 'rvu';
    if (saved === 'users') return 'settings';
    return saved || 'rvu';
  });
  useEffect(() => { localStorage.setItem('rvu_active_tab', activeTab); }, [activeTab]);
  useEffect(() => { localStorage.setItem('rvu_neuro_sub_tab', neuroSubTab); }, [neuroSubTab]);

  // Admin impersonation — which attending's "My Stats" / "Pay Projection" view is showing.
  // Shared across both tabs so toggling between them preserves the selection. Individual
  // users ignore this entirely (their session attending_id wins).
  const [adminAttendingId, setAdminAttendingId] = useState(
    () => localStorage.getItem('rvu_admin_attending_id') || ''
  );
  useEffect(() => {
    if (adminAttendingId) localStorage.setItem('rvu_admin_attending_id', adminAttendingId);
  }, [adminAttendingId]);

  // Pay Projection FY selection. Null = let backend pick the smart default; once the user
  // picks an FY, persist it across tab navigation and reloads so they don't have to re-pick.
  const [payProjectionFy, setPayProjectionFy] = useState(() => {
    const saved = localStorage.getItem('rvu_pay_projection_fy');
    return saved ? Number(saved) : null;
  });
  useEffect(() => {
    if (payProjectionFy != null) localStorage.setItem('rvu_pay_projection_fy', String(payProjectionFy));
    else localStorage.removeItem('rvu_pay_projection_fy');
  }, [payProjectionFy]);
  // If an individual user's persisted tab isn't one they can see, snap to 'my-stats'.
  // Wait for flags to arrive first — otherwise 'pay-projection' isn't in INDIVIDUAL_TABS
  // during the first-mount window and the user gets bounced away from their preference.
  useEffect(() => {
    if (!featureFlagsLoaded) return;
    if (isIndividual && !INDIVIDUAL_TABS.includes(neuroSubTab)) {
      setNeuroSubTab('my-stats');
    }
  }, [isIndividual, neuroSubTab, featureFlagsLoaded, featureFlags]);
  // Same snap for demo users — persisted tab from a prior admin session might be off-limits.
  useEffect(() => {
    if (isDemo && !DEMO_TABS.includes(neuroSubTab)) {
      setNeuroSubTab('schedule-past');
    }
  }, [isDemo, neuroSubTab]);
  // (The demo stack keeps the compensation tabs visible; dollar figures are blurred in-place
  // via redactMoney rather than the tabs being hidden.)
  // Shared date range across Productivity, Comp What-If, Compensation Review, and My Stats.
  // Persisted so a change on any tab sticks when switching tabs and across reloads. Default =
  // year-to-date (Jan 1 → today), clamped to the max data date. The data-load effect below
  // ADVANCES endDate to the new max when the user was riding the latest (see rvu_last_max_date),
  // so a nightly ingest surfaces automatically; a deliberately-set historical end date is kept.
  // Schedule Planning has its own separate range — it auto-snaps to today → last scheduled day.
  const [startDate, setStartDate] = useState(() => {
    const saved = localStorage.getItem('rvu_start_date');
    if (saved) return saved;
    return `${new Date().getFullYear()}-01-01`;
  });
  const [endDate, setEndDate] = useState(() => {
    const saved = localStorage.getItem('rvu_end_date');
    if (saved) return saved;
    const t = new Date();
    return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')}`;
  });
  useEffect(() => { if (startDate) localStorage.setItem('rvu_start_date', startDate); }, [startDate]);
  useEffect(() => { if (endDate) localStorage.setItem('rvu_end_date', endDate); }, [endDate]);
  // Most recent date that has data — used as the anchor for "to date" preset buttons in the
  // DateRangePicker, since data ingestion lags the calendar.
  const [maxDataDate, setMaxDataDate] = useState('');
  const [divisions, setDivisions] = useState([]);
  const [locations, setLocations] = useState([]);
  const [attendings, setAttendings] = useState([]);
  const [selectedDivisions, setSelectedDivisions] = useState([]);
  const [selectedLocations, setSelectedLocations] = useState([]);
  const [selectedAttendings, setSelectedAttendings] = useState([]);
  const [selectedPatientTypes, setSelectedPatientTypes] = useState(['ER', 'INPATIENT', 'OUTPATIENT']);
  const [selectedCategories, setSelectedCategories] = useState(['WEEKDAY', 'CALL', 'WEEKDAY_EVENING_ER', 'WEEKEND_EVENING_ER', 'AFTER_HOURS']);
  const [showLocations, setShowLocations] = useState(false);
  const [showAttendings, setShowAttendings] = useState(false);

  const [divisionData, setDivisionData] = useState([]);
  const [locationData, setLocationData] = useState([]);
  const [attendingData, setAttendingData] = useState([]);
  const [neuroStaffingData, setNeuroStaffingData] = useState([]);
  const [neuroConfig, setNeuroConfig] = useState(null);
  const [shiftByAttendingData, setShiftByAttendingData] = useState([]);
  const [scheduleShiftCounts, setScheduleShiftCounts] = useState([]);
  const [compensationData, setCompensationData] = useState([]);
  const [proposedCompData, setProposedCompData] = useState([]);
  // report_finalized_date matches "the day the attending did the work" — exams scanned late
  // one day and signed the next attribute correctly to the signing-day shift. Persisted so
  // that a tab change or reload keeps the same field selected across all tabs.
  const [dateField, setDateField] = useState(() => localStorage.getItem('rvu_date_field') || 'report_finalized_date');
  useEffect(() => { localStorage.setItem('rvu_date_field', dateField); }, [dateField]);
  const [benchmark, setBenchmark] = useState('65th');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Load divisions, locations, and date range on mount. Skipped for individual users —
  // those endpoints are admin-only and would 401/403. Individuals only need their own
  // attending info (already in the /api/auth/me response).
  useEffect(() => {
    if (!user || isIndividual) return;
    async function loadFilters() {
      try {
        const [divs, locs, atts, dateRange] = await Promise.all([
          fetchDivisions(),
          fetchLocations(),
          fetchAttendings(),
          fetchDateRange({ date_field: dateField })
        ]);
        setDivisions(divs);
        setLocations(locs);
        setAttendings(atts);
        setSelectedDivisions(divs);
        setSelectedLocations(locs);
        if (dateRange.max_date) {
          const maxIso = String(dateRange.max_date).slice(0, 10);
          setMaxDataDate(maxIso);
          // Advance the end date when the user was "riding the latest" — i.e., their persisted
          // end date matched the max data date as of the last load (rvu_last_max_date). That's
          // the common "show me through the newest data" case, and without this the range stays
          // frozen at an old max after a nightly ingest. A deliberately-set historical end date
          // (anything other than the previous max) is preserved untouched. Either way we never
          // let the end date exceed the current max (ingest cutoff).
          const savedLastMax = localStorage.getItem('rvu_last_max_date');
          setEndDate(prev => {
            if (prev && savedLastMax && prev === savedLastMax) return maxIso;  // tracking latest → advance
            if (prev && prev <= maxIso) return prev;                           // custom range → keep
            return maxIso;                                                     // unset / past cutoff → snap to max
          });
          localStorage.setItem('rvu_last_max_date', maxIso);
          if (dateRange.min_date) {
            setStartDate(prev => (prev && prev >= dateRange.min_date ? prev : dateRange.min_date));
          }
        }
      } catch (err) {
        console.error('Failed to load filters:', err);
      }
    }
    loadFilters();
    // dateField is a dep because the date-range endpoint filters by it — report_finalized_date
    // typically lags exam_completed_date, so switching field can change max_date. Without this
    // dep, flipping the field would leave endDate potentially past the new field's ingest cutoff
    // and silently truncate every query.
  }, [user, isIndividual, dateField]);

  // Fetch RVU data. Caller may pass { isCurrent } — a function that returns false if this
  // run has been superseded (see the effect below). Every setState is gated through it so
  // a stale run can't overwrite the current one.
  const fetchData = useCallback(async ({ isCurrent } = {}) => {
    const stillCurrent = isCurrent || (() => true);
    if (!startDate || !endDate) return;
    // Wait for the auth check to finish before touching the admin endpoints — otherwise on
    // first mount `user` is null, `isIndividual` is false, and the bail below wouldn't kick
    // in for individual users (they'd 403 on every request and land in the catch).
    if (!user) return;
    // Individual users only see "My Stats" — they have no admin tabs that need this data,
    // and the admin-only endpoints would 403 if we called them. Bail early.
    if (isIndividual) return;

    if (!stillCurrent()) return;
    setLoading(true);
    setError(null);

    const allCategories = ['WEEKDAY', 'CALL', 'WEEKDAY_EVENING_ER', 'WEEKEND_EVENING_ER', 'AFTER_HOURS'];
    const allCategoriesSelected = selectedCategories.length === allCategories.length;
    const allPatientTypesSelected = selectedPatientTypes.length === 3;

    const params = {
      start_date: startDate,
      end_date: endDate,
      date_field: dateField,
      ...(allPatientTypesSelected ? {} : { patient_type: selectedPatientTypes.join(',') || '_NONE_' }),
      ...(allCategoriesSelected ? {} : { exam_categories: selectedCategories.join(',') || 'NONE' }),
    };

    // For neuro tab, filter to Neuro division only
    const effectiveDivisions = activeTab === 'neuro' ? ['NEURO'] : selectedDivisions;

    // Add attending filter to division params if attendings are selected
    const divisionParams = { ...params };
    if (selectedAttendings.length > 0) {
      divisionParams.attending = selectedAttendings.join(',');
    }

    try {
      let neuroStaffing = null;
      let shiftByAttending = null;
      let scheduleCounts = null;

      // Fetch neuro staffing and shift data if on neuro tab
      if (activeTab === 'neuro') {
        // Load neuro config first to get attending list
        const config = await fetchNeuroConfig();
        if (!stillCurrent()) return;
        setNeuroConfig(config);
        const neuroAttendingIds = Object.keys(config.attendings || {});

        // Add neuro config attending filter to all neuro queries
        const neuroParams = { ...params, attending: neuroAttendingIds.join(',') };
        divisionParams.attending = neuroAttendingIds.join(',');

        let compensation = null;
        let proposedComp = null;
        [neuroStaffing, shiftByAttending, scheduleCounts, compensation, proposedComp] = await Promise.all([
          fetchNeuroStaffingMetrics(neuroParams),
          fetchShiftsByAttending(neuroParams),
          fetchScheduleShiftCounts(neuroParams),
          fetchMoonlightCompensation({ start_date: startDate, end_date: endDate, date_field: dateField }),
          fetchProposedCompensation({ start_date: startDate, end_date: endDate, date_field: dateField }),
        ]);
        if (!stillCurrent()) return;
        setCompensationData(compensation);
        setProposedCompData(proposedComp);

        // Get attending IDs that appear in the schedule
        const scheduledAttendingIds = new Set(scheduleCounts.map(s => s.attending_id));

        // Filter shiftByAttendingData to only include scheduled attendings
        const filteredShiftByAttending = shiftByAttending.filter(item =>
          scheduledAttendingIds.has(item.attending_id)
        );

        if (!stillCurrent()) return;
        setNeuroStaffingData(neuroStaffing);
        setShiftByAttendingData(filteredShiftByAttending);
        setScheduleShiftCounts(scheduleCounts);
      }

      const [division, location, attending] = await Promise.all([
        fetchRvusByDivision(divisionParams),
        fetchRvusByLocation(params),
        fetchRvusByAttending(params)
      ]);
      if (!stillCurrent()) return;

      // Filter by selected divisions/locations
      const filteredDivision = effectiveDivisions.length > 0
        ? division.filter(d => effectiveDivisions.includes(d.division))
        : division;

      // Only filter by location if showLocations is true
      const filteredLocation = showLocations && selectedLocations.length > 0
        ? location.filter(l => selectedLocations.includes(l.location))
        : location;

      // Filter attending data by divisions and specific attending selection
      let filteredAttending = effectiveDivisions.length > 0
        ? attending.filter(a => effectiveDivisions.includes(a.division))
        : attending;

      // Further filter by specific attending if selected
      if (selectedAttendings.length > 0) {
        filteredAttending = filteredAttending.filter(a =>
          selectedAttendings.includes(a.attending_id)
        );
      }

      setDivisionData(filteredDivision);
      setLocationData(filteredLocation);
      setAttendingData(filteredAttending);
    } catch (err) {
      if (stillCurrent()) {
        setError('Failed to load data. Please try again.');
        console.error('Failed to fetch data:', err);
      }
    } finally {
      if (stillCurrent()) setLoading(false);
    }
  }, [user, isIndividual, startDate, endDate, dateField, selectedCategories, selectedDivisions, selectedLocations, selectedAttendings, selectedPatientTypes, showLocations, activeTab]);

  // Cancellation guard: when filters change fast enough to fire two fetchData runs before
  // the first finishes, the earlier run's setState calls would overwrite the later one's
  // and leave the charts sourced from a mixture of filter sets. We can't AbortController
  // 30+ API helpers cheaply, so we just tag each run with an id and drop late writes.
  const fetchGeneration = useRef(0);
  useEffect(() => {
    const myGen = ++fetchGeneration.current;
    // Wrap fetchData to gate its state updates on still-being-current. Cheaper than
    // AbortController and correct for our case: the stale response's setters are no-ops.
    let cancelled = false;
    (async () => {
      try { await fetchData({ isCurrent: () => !cancelled && fetchGeneration.current === myGen }); }
      catch (e) { /* fetchData handles its own errors */ }
    })();
    return () => { cancelled = true; };
  }, [fetchData]);

  // Auth gates. While we're checking the session, render nothing. If no session, show login.
  if (authLoading) {
    return <div className="min-h-screen bg-gray-50" />;
  }
  if (!user) {
    return <LoginPage onLogin={setUser} />;
  }

  return (
    <div className="min-h-screen bg-gray-100">
      <header className="bg-white shadow">
        <div className="max-w-[1700px] mx-auto px-4 py-6 sm:px-6 lg:px-8 flex items-center justify-between">
          <h1 className="text-3xl font-bold text-gray-900">RVU Dashboard</h1>
          <div className="text-sm text-gray-600 flex items-center gap-3">
            <span>
              Signed in as <span className="font-medium">{user.attending_name || user.username}</span>
              {' '}<span className="text-gray-400">({user.role})</span>
            </span>
            <button
              onClick={() => setShowChangePassword(true)}
              className="text-blue-600 hover:text-blue-700 hover:underline"
            >
              Change password
            </button>
            <button
              onClick={handleLogout}
              className="text-blue-600 hover:text-blue-700 hover:underline"
            >
              Log out
            </button>
          </div>
        </div>
      </header>
      {pwToast && (
        <div className="fixed top-4 right-4 z-50 bg-green-50 border border-green-300 text-green-800 px-4 py-2 rounded shadow text-sm">
          {pwToast}
        </div>
      )}
      {showChangePassword && (
        <ChangePasswordModal
          onClose={() => setShowChangePassword(false)}
          onSuccess={() => {
            setPwToast('Password updated.');
            // Clear the "still using default" flag so the banner disappears immediately.
            setUser(prev => prev ? { ...prev, password_is_default: false } : prev);
          }}
        />
      )}
      {user.password_is_default && (
        <div className="bg-amber-50 border-b border-amber-200">
          <div className="max-w-[1700px] mx-auto px-4 py-2 sm:px-6 lg:px-8 text-sm text-amber-900 flex items-center justify-between">
            <span>
              You're still using the initial password. Please{' '}
              <button
                onClick={() => setShowChangePassword(true)}
                className="font-medium text-amber-900 underline hover:no-underline"
              >
                change it now
              </button>.
            </span>
          </div>
        </div>
      )}

      {/* Tab Navigation - Department tab disabled */}

      <main className="max-w-[1700px] mx-auto px-4 py-8 sm:px-6 lg:px-8 space-y-6">
        {activeTab === 'neuro' ? (
          <>
            {/* Error Message */}
            {error && (
              <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">
                {error}
              </div>
            )}

            {/* Loading indicator — stays inline so the date pickers underneath don't unmount on every refetch */}
            {loading && (
              <div className="flex items-center gap-3 text-sm text-gray-500">
                <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-blue-500"></div>
                Loading…
              </div>
            )}

            {/* Dashboard Content — always mounted so the date pickers keep focus through refetches */}
            {!error && (
              <>
                {/* Sub-tab Navigation */}
                <NeuroSubTabs
                  activeTab={neuroSubTab}
                  onTabChange={setNeuroSubTab}
                  role={user.role}
                  featureFlags={featureFlags}
                />

                {/* Sub-tab Content */}
                {isAdmin && neuroSubTab === 'rvu' && (
                  <div className="mt-6 space-y-4">
                    <div className="bg-white rounded-lg shadow p-6 space-y-4">
                      <DateRangePicker
                        startDate={startDate} endDate={endDate}
                        onStartDateChange={setStartDate} onEndDateChange={setEndDate}
                        dateField={dateField} onDateFieldChange={setDateField}
                        maxDate={maxDataDate}
                      />
                      <Filters
                        divisions={divisions} locations={locations} attendings={attendings}
                        selectedDivisions={selectedDivisions} selectedLocations={selectedLocations}
                        selectedAttendings={selectedAttendings} selectedPatientTypes={selectedPatientTypes}
                        selectedCategories={selectedCategories}
                        showLocations={showLocations} showAttendings={showAttendings}
                        onDivisionChange={setSelectedDivisions} onLocationChange={setSelectedLocations}
                        onAttendingChange={setSelectedAttendings} onPatientTypeChange={setSelectedPatientTypes}
                        onCategoryChange={setSelectedCategories}
                        onShowLocationsChange={setShowLocations} onShowAttendingsChange={setShowAttendings}
                        hideDivisions={true} activeTab={activeTab}
                      />
                    </div>
                    <div className="flex items-center justify-between mb-4">
                      <h2 className="text-xl font-bold text-gray-900">RVU Metrics by Attending</h2>
                      {/* Benchmark selector relocated here from the (removed) Staffing Metrics
                          table — it still drives this table's Expected / Multiplier columns. */}
                      <label className="flex items-center gap-2 text-sm text-gray-600">
                        Benchmark
                        <select
                          value={benchmark}
                          onChange={(e) => setBenchmark(e.target.value)}
                          className="border border-gray-300 rounded-md px-2 py-1 text-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500"
                        >
                          <option value="65th">65th percentile</option>
                          <option value="70th">70th percentile</option>
                        </select>
                      </label>
                    </div>
                    <NeuroRvuTab
                      shiftByAttendingData={shiftByAttendingData}
                      neuroStaffingData={neuroStaffingData}
                      neuroConfig={neuroConfig}
                      benchmark={benchmark}
                    />

                    <h2 className="text-xl font-bold text-gray-900 mt-8 mb-4">Shift Distribution</h2>
                    <NeuroShiftDistributionTab
                      scheduleShiftCounts={scheduleShiftCounts}
                      shiftByAttendingData={shiftByAttendingData}
                      neuroConfig={neuroConfig}
                      neuroStaffingData={neuroStaffingData}
                    />
                  </div>
                )}


                {isAdmin && neuroSubTab === 'compensation' && (
                  <div className="mt-6 space-y-4">
                    <div className="bg-white rounded-lg shadow p-4">
                      <DateRangePicker
                        startDate={startDate} endDate={endDate}
                        onStartDateChange={setStartDate} onEndDateChange={setEndDate}
                        dateField={dateField} onDateFieldChange={setDateField}
                        maxDate={maxDataDate}
                      />
                    </div>
                    <h2 className="text-xl font-bold text-gray-900 mb-4">Proposed Compensation Model</h2>
                    <NeuroCompensationTab
                      proposedCompData={proposedCompData}
                      neuroConfig={neuroConfig}
                      scheduleShiftCounts={scheduleShiftCounts}
                      redactMoney={isDemoStack}
                    />
                  </div>
                )}

                {(isAdmin || isDemo) && neuroSubTab === 'schedule-past' && (
                  <div className="mt-6">
                    <h2 className="text-xl font-bold text-gray-900 mb-4">Compensation Review</h2>
                    <NeuroScheduleTab
                      mode="past"
                      startDate={startDate}
                      endDate={endDate}
                      onStartDateChange={setStartDate}
                      onEndDateChange={setEndDate}
                      neuroConfig={neuroConfig}
                      proposedCompData={proposedCompData}
                      scheduleShiftCounts={scheduleShiftCounts}
                      hideMoney={isDemo}
                      redactMoney={isDemoStack}
                      isDemo={isDemoStack}
                      readOnly={isDemo}
                    />
                  </div>
                )}

                {(isAdmin || isDemo) && neuroSubTab === 'schedule-future' && (
                  <div className="mt-6">
                    <h2 className="text-xl font-bold text-gray-900 mb-4">Schedule Planning</h2>
                    <NeuroScheduleTab
                      mode="future"
                      startDate={startDate}
                      endDate={endDate}
                      neuroConfig={neuroConfig}
                      proposedCompData={proposedCompData}
                      scheduleShiftCounts={scheduleShiftCounts}
                      hideMoney={isDemo}
                      redactMoney={isDemoStack}
                      readOnly={isDemo}
                      isDemo={isDemoStack}
                    />
                  </div>
                )}

                {neuroSubTab === 'my-stats' && (isAdmin || isIndividual || isDemo) && (
                  <div className="mt-6">
                    <h2 className="text-xl font-bold text-gray-900 mb-4">My Stats</h2>
                    <NeuroMyStatsTab
                      neuroConfig={neuroConfig}
                      user={user}
                      adminAttendingId={adminAttendingId}
                      onAdminAttendingIdChange={setAdminAttendingId}
                      hideMoney={isDemo}
                      redactMoney={isDemoStack}
                      startDate={startDate}
                      endDate={endDate}
                      onStartDateChange={setStartDate}
                      onEndDateChange={setEndDate}
                      maxDataDate={maxDataDate}
                    />
                  </div>
                )}

                {neuroSubTab === 'pay-projection' && (isAdmin || isIndividual) && (
                  <div className="mt-6">
                    <h2 className="text-xl font-bold text-gray-900 mb-4">Pay Projection</h2>
                    <NeuroPayProjectionTab
                      neuroConfig={neuroConfig}
                      user={user}
                      adminAttendingId={adminAttendingId}
                      onAdminAttendingIdChange={setAdminAttendingId}
                      fyStartYear={payProjectionFy}
                      onFyStartYearChange={setPayProjectionFy}
                      redactMoney={isDemoStack}
                    />
                  </div>
                )}

                {isAdmin && neuroSubTab === 'settings' && (
                  <div className="mt-6">
                    <h2 className="text-xl font-bold text-gray-900 mb-4">Settings</h2>
                    <NeuroSettingsTab
                      featureFlags={featureFlags}
                      onFeatureFlagsChange={refreshFeatureFlags}
                    />
                  </div>
                )}

                {/* Staffing Metrics is shared across sub-tabs; only show on Productivity since
                    the schedule and comp tabs already break this down. */}
              </>
            )}
          </>
        ) : (
          <>
            {/* Date Range and Filters */}
            <div className="bg-white rounded-lg shadow p-6 space-y-4">
              <DateRangePicker
                startDate={startDate}
                endDate={endDate}
                onStartDateChange={setStartDate}
                onEndDateChange={setEndDate}
                dateField={dateField}
                onDateFieldChange={setDateField}
                maxDate={maxDataDate}
              />

              <Filters
                divisions={divisions}
                locations={locations}
                attendings={attendings}
                selectedDivisions={selectedDivisions}
                selectedLocations={selectedLocations}
                selectedAttendings={selectedAttendings}
                selectedPatientTypes={selectedPatientTypes}
                selectedCategories={selectedCategories}
                showLocations={showLocations}
                showAttendings={showAttendings}
                onDivisionChange={setSelectedDivisions}
                onLocationChange={setSelectedLocations}
                onAttendingChange={setSelectedAttendings}
                onPatientTypeChange={setSelectedPatientTypes}
                onCategoryChange={setSelectedCategories}
                onShowLocationsChange={setShowLocations}
                onShowAttendingsChange={setShowAttendings}
                activeTab={activeTab}
              />
            </div>

            {/* Error Message */}
            {error && (
              <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">
                {error}
              </div>
            )}

            {/* Loading State */}
            {loading && (
              <div className="flex justify-center py-8">
                <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-500"></div>
              </div>
            )}

            {/* Dashboard Content */}
            {!loading && !error && (
              <>
                <SummaryCards data={{ division: divisionData }} />

                <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                  <DivisionChart data={divisionData} />
                  <LocationChart data={locationData} />
                </div>

                <AttendingTable data={attendingData} />
              </>
            )}
          </>
        )}
      </main>
    </div>
  );
}

export default App;
