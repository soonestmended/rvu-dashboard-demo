import { useEffect, useMemo, useState } from 'react';
import { fetchMyStats, fetchDateRange } from '../api';
import DateRangePicker from './DateRangePicker';
import Redacted from './Redacted';

// "2026-03-01" -> "Mar 2026, Apr 2026, May 2026" — the 3 months of a qualification window.
const windowMonthsLabel = (startIso) => {
  if (!startIso) return '';
  const [y, m] = startIso.split('-').map(Number);
  return [0, 1, 2]
    .map(k => new Date(y, (m - 1) + k, 1).toLocaleDateString('en-US', { month: 'short', year: 'numeric' }))
    .join(', ');
};

// Each attending sees this view. Headline = "X% of your FTE-prorated 65th percentile benchmark."
// Plus per-month breakdown + current-pace projection to end of fiscal year.
//
// While auth is being built, a dropdown lets you switch which attending you're viewing as.
// Once auth lands, the dropdown disappears and attending_id comes from the session.
export default function NeuroMyStatsTab({
  neuroConfig, user, adminAttendingId, onAdminAttendingIdChange, hideMoney = false, redactMoney = false,
  // Shared date range (owned by App.jsx). For individual users the parent doesn't wire these
  // in, so this component falls back to fetching its own date range and defaulting to FY-YTD.
  startDate: sharedStartDate, endDate: sharedEndDate,
  onStartDateChange, onEndDateChange, maxDataDate: sharedMaxDataDate,
}) {
  // For individual users, the attending is locked to their session attending_id — no picker.
  // For admins (impersonating to validate the view), the picked id is owned by App.jsx so it
  // survives navigation across tabs.
  const isAdmin = user?.role === 'admin';
  const sessionAttendingId = user?.attending_id || '';

  const attendingOptions = useMemo(() => {
    if (!neuroConfig?.attendings) return [];
    return Object.entries(neuroConfig.attendings)
      .filter(([id]) => id !== 'ATT000772') // exclude Chow
      .map(([id, info]) => ({ id, name: info.name, fte: info.fte }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [neuroConfig]);

  // Admins use the shared (lifted) state; individuals use their session attending_id.
  const attendingId = isAdmin ? (adminAttendingId || '') : sessionAttendingId;
  const setAttendingId = (id) => { if (isAdmin) onAdminAttendingIdChange?.(id); };
  // Seed the lifted state once if it's empty.
  useEffect(() => {
    if (isAdmin && !adminAttendingId && attendingOptions.length > 0) {
      onAdminAttendingIdChange?.(sessionAttendingId || attendingOptions[0].id);
    }
  }, [isAdmin, adminAttendingId, attendingOptions, sessionAttendingId, onAdminAttendingIdChange]);

  // Date range: prefer the shared range from App.jsx (admin/demo path). Individual users
  // don't get shared state wired in — fall back to fetching the data range and defaulting to
  // FY-YTD, then owning the state locally.
  const sharedDatesWired = typeof onStartDateChange === 'function' && typeof onEndDateChange === 'function';
  const [localStartDate, setLocalStartDate] = useState('');
  const [localEndDate, setLocalEndDate] = useState('');
  const [localMaxDataDate, setLocalMaxDataDate] = useState('');
  useEffect(() => {
    if (sharedDatesWired) return;  // individual users: bootstrap FY-YTD locally
    fetchDateRange({ date_field: 'report_finalized_date', division: 'NEURO' })
      .then(r => {
        if (!r || !r.max_date) return;
        const maxIso = String(r.max_date).slice(0, 10);
        setLocalMaxDataDate(maxIso);
        const [y, m] = maxIso.split('-').map(Number);
        // FY runs Jul–Jun. If we're in Jul–Dec, FY started July of this year; else July of previous year.
        const fyStartYear = m >= 7 ? y : y - 1;
        setLocalStartDate(`${fyStartYear}-07-01`);
        setLocalEndDate(maxIso);
      })
      .catch(() => {});
  }, [sharedDatesWired]);
  const startDate = sharedDatesWired ? sharedStartDate : localStartDate;
  const endDate = sharedDatesWired ? sharedEndDate : localEndDate;
  const setStartDate = sharedDatesWired ? onStartDateChange : setLocalStartDate;
  const setEndDate = sharedDatesWired ? onEndDateChange : setLocalEndDate;
  const maxDataDate = sharedDatesWired ? (sharedMaxDataDate || '') : localMaxDataDate;

  // Fetch stats whenever attending or date range changes.
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => {
    if (!attendingId || !startDate || !endDate) return;
    setLoading(true);
    setError(null);
    // Cancellation guard — see NeuroPayProjectionTab. Attending A→B before A resolves
    // would otherwise show B's name over A's numbers, and the CSV-download URL (built
    // from current attendingId) would point at yet a third combination.
    let cancelled = false;
    fetchMyStats({ attending_id: attendingId, start_date: startDate, end_date: endDate })
      .then(d => {
        if (cancelled) return;
        if (d.error) { setError(d.error); setStats(null); }
        else setStats(d);
      })
      .catch(e => { if (!cancelled) setError(String(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [attendingId, startDate, endDate]);

  // The forward projection / projected-month rows only make sense when the user is viewing
  // the *current* fiscal year (start = some Jul 1, end within the same FY). Other ranges
  // (Last 3 months, YTD, custom) suppress projections entirely and show actual-only.
  const isCurrentFY = (() => {
    if (!startDate || !endDate) return false;
    if (!startDate.endsWith('-07-01')) return false;
    const [sy] = startDate.split('-').map(Number);
    const [ey, em] = endDate.split('-').map(Number);
    if (ey === sy && em >= 7) return true;        // Jul–Dec of sy
    if (ey === sy + 1 && em <= 6) return true;     // Jan–Jun of sy+1
    return false;
  })();

  const fmt0 = (n) => (n == null ? '—' : Math.round(n).toLocaleString());

  // Category color scheme — applied consistently to daytime vs after-hours throughout the page.
  // Sky for daytime (the "day" connotation), violet for after-hours (the "evening" connotation).
  // Used on column headers, % sub-text, and the projection-box breakdown lines.
  const DAYTIME_COLOR = 'text-sky-900';
  const AFTER_HOURS_COLOR = 'text-emerald-700';
  const fmt1 = (n) => (n == null ? '—' : n.toFixed(1));
  const pctClass = (p) => {
    if (p == null) return 'text-gray-500';
    if (p >= 100) return 'text-green-700';
    if (p >= 80) return 'text-yellow-700';
    return 'text-red-700';
  };

  return (
    <div className="space-y-4">
      {/* Admin-only impersonation picker — admins can preview each attending's view. Individual
          users have their attending_id locked to their session and see no picker. */}
      {isAdmin && (
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 text-base">
          <span className="font-medium text-amber-900">Admin view:</span>{' '}
          <span className="text-amber-800">previewing as</span>{' '}
          <select
            value={attendingId}
            onChange={e => setAttendingId(e.target.value)}
            className="ml-1 border border-amber-300 rounded px-2 py-0.5 text-base bg-white"
          >
            {attendingOptions.map(a => (
              <option key={a.id} value={a.id}>{a.name} (FTE {a.fte})</option>
            ))}
          </select>
          <span className="ml-2 text-amber-700 text-sm">(individual users only see their own view)</span>
        </div>
      )}

      <div className="bg-white rounded-lg shadow p-4">
        <DateRangePicker
          startDate={startDate} endDate={endDate}
          onStartDateChange={setStartDate} onEndDateChange={setEndDate}
          maxDate={maxDataDate}
        />
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">{error}</div>
      )}

      {loading && !stats && (
        <div className="bg-white rounded-lg shadow p-6 text-gray-500">Loading stats…</div>
      )}

      {stats && (
        <>
          {/* Headline */}
          <div className="bg-white rounded-lg shadow p-6">
            <div className="text-base uppercase tracking-wide text-gray-500 mb-1">
              {stats.attending_name} · FTE {stats.fte}
            </div>
            <div className="text-base text-gray-600">
              <span className="font-medium">{stats.period_start}</span>{' → '}
              <span className="font-medium">{stats.period_end}</span>
              <span className="text-gray-500"> ({stats.period_months} months)</span>
            </div>
            <div className="mt-3">
              <span className={`text-5xl font-bold ${pctClass(stats.pct_of_target)}`}>
                {stats.pct_of_target != null ? `${stats.pct_of_target}%` : '—'}
              </span>
              <span className="ml-3 text-gray-600">of your FTE-prorated 65th percentile benchmark</span>
            </div>
            <div className="mt-2 text-base text-gray-600">
              <strong>{fmt0(stats.total_rvus)} wRVU</strong> produced
              {' '}· target{' '}<strong>{fmt0(stats.target_rvus)} wRVU</strong>
              {' '}<span className="text-gray-500">({stats.annual_65th_rvu} annual × FTE {stats.fte} × {stats.period_months}/12)</span>
            </div>
            {/* Daytime vs after-hours as % of the (period) benchmark — these add up to the
                headline percentage above, giving a quick read on what's driving it. */}
            {stats.target_rvus > 0 && (
              <div className="mt-1 text-base text-gray-700">
                <span className={DAYTIME_COLOR}>Daytime</span>:{' '}
                <strong>{fmt0(stats.total_daytime_rvus)} wRVU</strong>
                {!hideMoney && (
                  <>{' '}<span className={`${DAYTIME_COLOR} font-medium`}>
                    ({Math.round(stats.total_daytime_rvus / stats.target_rvus * 1000) / 10}% of benchmark)
                  </span></>
                )}
                {' '}· <span className={AFTER_HOURS_COLOR}>After-hours</span>:{' '}
                <strong>{fmt0(stats.total_after_hours_rvus)} wRVU</strong>
                {!hideMoney && (
                  <>{' '}<span className={`${AFTER_HOURS_COLOR} font-medium`}>
                    ({Math.round(stats.total_after_hours_rvus / stats.target_rvus * 1000) / 10}% of benchmark)
                  </span></>
                )}
              </div>
            )}
            {!hideMoney && stats.total_after_hours_pay > 0 && (
              <div className="mt-2 text-base text-gray-700">
                <span className={AFTER_HOURS_COLOR}>After-hours pay (so far)</span>:{' '}
                <strong className={AFTER_HOURS_COLOR}>
                  <Redacted on={redactMoney}>${stats.total_after_hours_pay.toLocaleString(undefined, {maximumFractionDigits: 0})}</Redacted>
                </strong>
                {' '}<span className="text-gray-500">
                  ({fmt0(stats.total_after_hours_rvus)} wRVU × ${stats.dollars_per_rvu}/RVU)
                </span>
              </div>
            )}
          </div>

          {/* After-hours bonus qualification — current quarter (final) + next quarter (running). */}
          {stats.qualifying_benchmark != null && (() => {
            const currPct = stats.qualifying_benchmark > 0 ? (stats.qualifying_rvus / stats.qualifying_benchmark) * 100 : null;
            const nextPct = stats.qualifying_benchmark_next > 0 ? (stats.qualifying_rvus_next / stats.qualifying_benchmark_next) * 100 : null;
            const fmtPct = (p) => (p == null ? '—' : `${Math.round(p)}%`);
            return (
              <div className="bg-white rounded-lg shadow p-6">
                <h3 className="text-lg font-semibold text-gray-900 mb-1">After-hours bonus qualification</h3>
                <p className="text-sm text-gray-500 mb-4">
                  The $/RVU after-hours bonus is paid when your production over a quarter's 3-month
                  qualification window clears the FTE-prorated 65th percentile (65th × FTE × 3/12).
                </p>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <div className="border border-gray-200 rounded-md p-4">
                    <div className="text-xs uppercase tracking-wide text-gray-500">This quarter</div>
                    <div className="mt-1 flex items-baseline gap-3">
                      <span className={`text-3xl font-bold ${pctClass(currPct)}`}>{fmtPct(currPct)}</span>
                      <span className={`text-sm font-medium ${stats.qualified_for_rvu_bonus ? 'text-green-700' : 'text-red-700'}`}>
                        {stats.qualified_for_rvu_bonus
                          ? (stats.is_new_hire ? 'Qualified (new hire)' : 'Qualified ✓')
                          : 'Not qualified'}
                      </span>
                    </div>
                    <div className="mt-1 text-sm text-gray-600">
                      {fmt0(stats.qualifying_rvus)} / {fmt0(stats.qualifying_benchmark)} wRVU
                    </div>
                    <div className="mt-1 text-xs text-gray-500">Counting {windowMonthsLabel(stats.qualifying_window_start)}</div>
                  </div>
                  <div className="border border-gray-200 rounded-md p-4">
                    <div className="text-xs uppercase tracking-wide text-gray-500">Next quarter — on track?</div>
                    <div className="mt-1 flex items-baseline gap-3">
                      <span className={`text-3xl font-bold ${pctClass(nextPct)}`}>{fmtPct(nextPct)}</span>
                      <span className="text-sm text-gray-500">running</span>
                    </div>
                    <div className="mt-1 text-sm text-gray-600">
                      {fmt0(stats.qualifying_rvus_next)} / {fmt0(stats.qualifying_benchmark_next)} wRVU
                    </div>
                    <div className="mt-1 text-xs text-gray-500">
                      Counting {windowMonthsLabel(stats.qualifying_window_next_start)} · window may still be in progress
                    </div>
                  </div>
                </div>
              </div>
            );
          })()}

          {/* Year-end projection — shown only in Current FY view, where it makes sense. */}
          {isCurrentFY && stats.projection && (
            <div className="bg-white rounded-lg shadow p-6">
              <h3 className="text-lg font-semibold text-gray-900 mb-2">
                Fiscal year projection
                <span className="ml-2 text-base font-normal text-gray-500">
                  through {stats.projection.fy_end}
                </span>
              </h3>
              <div className="text-base text-gray-600 mb-3">
                Based on the section average wRVU per shift type, multiplied by the shifts you're
                scheduled for between now and the end of the fiscal year. Days not yet on the
                schedule fall back to your historical pace ({fmt0(stats.projection.daily_pace)} wRVU/day).
              </div>
              <div className="mb-3">
                <span className={`text-3xl font-bold ${pctClass(stats.projection.pct_of_annual)}`}>
                  {fmt0(stats.projection.projected_year_end)}
                </span>
                <span className="ml-2 text-gray-600">
                  wRVU · {' '}
                  <span className={`font-bold ${pctClass(stats.projection.pct_of_annual)}`}>
                    {stats.projection.pct_of_annual}%
                  </span>{' '}
                  of annual target {fmt0(stats.projection.annual_target)} wRVU
                </span>
              </div>
              {(() => {
                // Pct-of-annual-benchmark for daytime/after-hours splits of past production.
                // Helps an attending see the share each category contributes toward their
                // annual target.
                const annual = stats.projection.annual_target || 0;
                const dayPct = annual > 0 ? Math.round(stats.total_daytime_rvus / annual * 1000) / 10 : null;
                const ahPct = annual > 0 ? Math.round(stats.total_after_hours_rvus / annual * 1000) / 10 : null;
                return (
              <table className="text-base text-gray-700 border-collapse">
                <tbody>
                  <tr>
                    <td className="pr-4 py-0.5 text-gray-500">Past production (through {stats.period_end})</td>
                    <td className="text-right py-0.5">{fmt0(stats.projection.past_rvus)} wRVU</td>
                  </tr>
                  <tr>
                    <td className={`pr-4 py-0.5 pl-4 ${DAYTIME_COLOR}`}>— daytime</td>
                    <td className="text-right py-0.5 text-gray-500">
                      {fmt0(stats.total_daytime_rvus)} wRVU{' '}
                      {!hideMoney && dayPct != null && <span className={DAYTIME_COLOR}>({dayPct}% of annual)</span>}
                    </td>
                  </tr>
                  <tr>
                    <td className={`pr-4 py-0.5 pl-4 ${AFTER_HOURS_COLOR}`}>— after-hours</td>
                    <td className="text-right py-0.5 text-gray-500">
                      {fmt0(stats.total_after_hours_rvus)} wRVU{' '}
                      {!hideMoney && ahPct != null && <span className={AFTER_HOURS_COLOR}>({ahPct}% of annual)</span>}
                    </td>
                  </tr>
                  <tr>
                    <td className="pr-4 py-0.5 text-gray-500">
                      Scheduled future ({stats.projection.scheduled_workdays_remaining} work day{stats.projection.scheduled_workdays_remaining !== 1 ? 's' : ''})
                    </td>
                    <td className="text-right py-0.5">{fmt0(stats.projection.scheduled_future_rvus)} wRVU</td>
                  </tr>
                  {stats.projection.projected_after_hours_rvus > 0 && (
                    <tr>
                      <td className="pr-4 py-0.5 text-gray-500">
                        Expected after-hours (at recent rate of {stats.projection.after_hours_rate_per_day} wRVU/day)
                      </td>
                      <td className="text-right py-0.5">{fmt0(stats.projection.projected_after_hours_rvus)} wRVU</td>
                    </tr>
                  )}
                  {stats.projection.projected_weekend_er_rvus > 0 && (
                    <tr>
                      <td className="pr-4 py-0.5 text-gray-500">
                        Expected weekend ER (at recent rate of {stats.projection.weekend_er_rate_per_day} wRVU/day)
                      </td>
                      <td className="text-right py-0.5">{fmt0(stats.projection.projected_weekend_er_rvus)} wRVU</td>
                    </tr>
                  )}
                  {stats.projection.unscheduled_days_remaining > 0 && (
                    <tr>
                      <td className="pr-4 py-0.5 text-gray-500">
                        Unscheduled future ({stats.projection.unscheduled_days_remaining} day{stats.projection.unscheduled_days_remaining !== 1 ? 's' : ''}, at historical pace)
                      </td>
                      <td className="text-right py-0.5">{fmt0(stats.projection.unscheduled_future_rvus)} wRVU</td>
                    </tr>
                  )}
                  <tr className="font-medium border-t border-gray-200">
                    <td className="pr-4 pt-1">Projected total</td>
                    <td className="text-right pt-1">{fmt0(stats.projection.projected_year_end)} wRVU</td>
                  </tr>
                </tbody>
              </table>
                );
              })()}
            </div>
          )}

          {/* Per-month table. Each row labeled actual / projected / mix to make clear which
              months are already in the bank vs forecast from the schedule. */}
          <div className="bg-white rounded-lg shadow p-6">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-lg font-semibold text-gray-900">Per-month breakdown</h3>
              {/* Direct-download link — browser handles via Content-Disposition. Cookie
                  travels same-origin so auth just works. */}
              <a
                href={`/api/me/after-hours-cases.csv?attending_id=${encodeURIComponent(attendingId)}&start_date=${encodeURIComponent(startDate)}&end_date=${encodeURIComponent(endDate)}`}
                className="px-3 py-1.5 rounded text-base font-medium bg-blue-100 text-blue-700 hover:bg-blue-200"
                title={`After-hours cases between ${startDate} and ${endDate}`}
              >
                ⬇ Download after-hours CSV
              </a>
            </div>
            {(() => {
              // Show FTE column only when FTE actually varies across the displayed months —
              // otherwise it's noise. Computed inline so it's just derived from `stats.by_month`.
              const ftes = (stats.by_month || []).map(m => m.fte).filter(v => v != null);
              const showFteCol = ftes.length > 0 && Math.min(...ftes) !== Math.max(...ftes);
              return (
            <table className="min-w-full text-base">
              <thead>
                <tr className="border-b border-gray-200 text-gray-600">
                  <th className="text-left py-2 pr-4 font-medium">Month</th>
                  <th className="text-left py-2 pr-4 font-medium">Source</th>
                  {showFteCol && <th className="text-right py-2 pr-4 font-medium">FTE</th>}
                  {!hideMoney && (
                    <th className={`text-right py-2 pr-4 font-medium ${DAYTIME_COLOR}`}>
                      Daytime <span className="font-normal text-sm">(% bench)</span>
                    </th>
                  )}
                  {hideMoney && <th className="text-right py-2 pr-4 font-medium">wRVU</th>}
                  <th className={`text-right py-2 pr-4 font-medium ${AFTER_HOURS_COLOR}`}>
                    After-hrs {!hideMoney && <span className="font-normal text-sm">(% bench)</span>}
                  </th>
                  {!hideMoney && (
                    <th className={`text-right py-2 pr-4 font-medium ${AFTER_HOURS_COLOR}`}
                        title={`After-hours wRVU × $${stats.dollars_per_rvu}/RVU`}>
                      A-H pay
                    </th>
                  )}
                  {!hideMoney && <th className="text-right py-2 pr-4 font-medium">wRVU</th>}
                  <th className="text-right py-2 pr-4 font-medium">Target</th>
                  <th className="text-right py-2 font-medium">% of target</th>
                </tr>
              </thead>
              <tbody>
                {(isCurrentFY
                  ? stats.by_month
                  : stats.by_month.filter(m => {
                      // Outside Current-FY mode, restrict to months in the selected range and
                      // show only the actual portion (no projections).
                      const [sy, sm] = startDate.split('-').map(Number);
                      const [ey, em] = endDate.split('-').map(Number);
                      const [my, mm] = m.month.split('-').map(Number);
                      const after = my > sy || (my === sy && mm >= sm);
                      const before = my < ey || (my === ey && mm <= em);
                      return after && before;
                    }).map(m => ({
                      ...m,
                      kind: 'actual',
                      rvus: m.actual_rvus,
                      pct_of_target: m.target_rvus > 0 ? Math.round((m.actual_rvus / m.target_rvus) * 1000) / 10 : null,
                    }))
                ).map(m => {
                  const kindBadge = m.kind === 'actual'
                    ? <span className="px-1.5 py-0.5 rounded text-sm bg-gray-100 text-gray-700">Actual</span>
                    : m.kind === 'projected'
                    ? <span className="px-1.5 py-0.5 rounded text-sm bg-blue-100 text-blue-800">Projected</span>
                    : <span className="px-1.5 py-0.5 rounded text-sm bg-amber-100 text-amber-800">Mix · {fmt0(m.actual_rvus)} actual + {fmt0(m.projected_rvus)} projected</span>;
                  // Projected-future months have no daytime/after-hours breakdown (we don't
                  // know what mix the schedule will produce). Show "—" for those.
                  const showSplit = m.kind !== 'projected';
                  return (
                    <tr key={m.month} className="border-b border-gray-100">
                      <td className="py-2 pr-4 text-gray-700">{m.month}</td>
                      <td className="py-2 pr-4">{kindBadge}</td>
                      {showFteCol && (
                        <td className="py-2 pr-4 text-right text-gray-500">{m.fte != null ? m.fte.toFixed(2) : '—'}</td>
                      )}
                      {!hideMoney && (
                        <td className="py-2 pr-4 text-right text-gray-500">
                          {showSplit ? (
                            <>
                              {fmt0(m.actual_daytime_rvus)}
                              {m.target_rvus > 0 && (
                                <span className={`text-sm ml-1 ${DAYTIME_COLOR}`}>
                                  ({Math.round(m.actual_daytime_rvus / m.target_rvus * 100)}%)
                                </span>
                              )}
                            </>
                          ) : '—'}
                        </td>
                      )}
                      {hideMoney && (
                        <td className={`py-2 pr-4 text-right ${m.kind === 'projected' ? 'text-blue-700 italic' : 'text-gray-900'}`}>
                          {fmt0(m.rvus)}
                        </td>
                      )}
                      <td className="py-2 pr-4 text-right text-gray-500">
                        {showSplit ? (
                          <>
                            {fmt0(m.actual_after_hours_rvus)}
                            {!hideMoney && m.target_rvus > 0 && (
                              <span className={`text-sm ml-1 ${AFTER_HOURS_COLOR}`}>
                                ({Math.round(m.actual_after_hours_rvus / m.target_rvus * 100)}%)
                              </span>
                            )}
                          </>
                        ) : '—'}
                      </td>
                      {!hideMoney && (
                        <td className={`py-2 pr-4 text-right ${AFTER_HOURS_COLOR}`}>
                          <Redacted on={redactMoney}>{showSplit
                            ? `$${Math.round(m.after_hours_pay || 0).toLocaleString()}`
                            : '—'}</Redacted>
                        </td>
                      )}
                      {!hideMoney && (
                        <td className={`py-2 pr-4 text-right ${m.kind === 'projected' ? 'text-blue-700 italic' : 'text-gray-900'}`}>
                          {fmt0(m.rvus)}
                        </td>
                      )}
                      <td className="py-2 pr-4 text-right text-gray-500">{fmt0(m.target_rvus)}</td>
                      <td className={`py-2 text-right font-medium ${pctClass(m.pct_of_target)} ${m.kind === 'projected' ? 'italic' : ''}`}>
                        {m.pct_of_target != null ? `${m.pct_of_target}%` : '—'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
              );
            })()}
          </div>
        </>
      )}
    </div>
  );
}
