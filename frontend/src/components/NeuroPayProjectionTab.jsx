import { useEffect, useMemo, useRef, useState } from 'react';
import { fetchMyPayProjection } from '../api';
import Redacted from './Redacted';

// Pay Projection tab — individual + admin. Shows annualized salary for the current FY based
// on the schedule (past actual + future scheduled) plus historical after-hours rate. Includes
// a weekend-ER slider so an attending can model how many extra weekend shifts they'd take on.
//
// Schedule view (per-day): hover a past day to see what they actually read; hover a future
// day to see the projection breakdown.
export default function NeuroPayProjectionTab({ user, neuroConfig, adminAttendingId, onAdminAttendingIdChange, fyStartYear, onFyStartYearChange, redactMoney = false }) {
  const isAdmin = user?.role === 'admin';
  const sessionAttendingId = user?.attending_id || '';

  // For admin impersonation
  const attendingOptions = useMemo(() => {
    if (!neuroConfig?.attendings) return [];
    return Object.entries(neuroConfig.attendings)
      .filter(([id]) => id !== 'ATT000772')
      .map(([id, info]) => ({ id, name: info.name }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [neuroConfig]);
  // Admins use the lifted selection (preserved across tabs); individuals are locked.
  const attendingId = isAdmin ? (adminAttendingId || '') : sessionAttendingId;
  const setAttendingId = (id) => { if (isAdmin) onAdminAttendingIdChange?.(id); };
  useEffect(() => {
    if (isAdmin && !adminAttendingId && attendingOptions.length > 0) {
      onAdminAttendingIdChange?.(sessionAttendingId || attendingOptions[0].id);
    }
  }, [isAdmin, adminAttendingId, attendingOptions, sessionAttendingId, onAdminAttendingIdChange]);

  // Weekend ER slider — represents TOTAL annual WE ER shifts (past actual + planned future).
  // Initial value gets set from the server's past-count when data lands so the projection
  // starts at the attending's actual current pace. Once the user touches it, we stop syncing.
  const [weekendErCount, setWeekendErCount] = useState(0);
  const userTouchedSlider = useRef(false);

  // Fiscal-year selector: lifted to App.jsx so the picked value persists across tabs and
  // reloads. Pass null on first render → backend picks the smart default; once user picks,
  // we lock to that.
  const setFyStartYear = onFyStartYearChange;

  // Custom hover popup with a configurable delay — native title= has a ~500ms browser delay
  // we can't override.
  const [hoverCell, setHoverCell] = useState(null);  // {day, x, y}
  const hoverTimer = useRef(null);
  const HOVER_DELAY_MS = 200;
  const scheduleHover = (day, x, y) => {
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
    hoverTimer.current = setTimeout(() => setHoverCell({ day, x, y }), HOVER_DELAY_MS);
  };
  const clearHover = () => {
    if (hoverTimer.current) { clearTimeout(hoverTimer.current); hoverTimer.current = null; }
    setHoverCell(null);
  };

  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  // Refetch when attending or FY selection changes. Slider is recomputed locally so
  // moving it doesn't round-trip.
  useEffect(() => {
    if (!attendingId) return;
    setLoading(true);
    setError(null);
    // Switching attendings or FYs resets the "user touched" guard so the new past
    // count seeds the slider.
    userTouchedSlider.current = false;
    // Cancellation guard: pick attending A, then B before A resolves — without this,
    // A's late setData would clobber B and the salary displayed under B's name would
    // actually be A's numbers. Dropping stale writes prevents that.
    let cancelled = false;
    fetchMyPayProjection({ attending_id: attendingId, weekend_er_count: 0, fy: fyStartYear })
      .then(d => {
        if (cancelled) return;
        if (d.error) { setError(d.error); setData(null); }
        else {
          setData(d);
          if (!userTouchedSlider.current) {
            setWeekendErCount(d.components?.weekend_er_pay?.shifts_past || 0);
          }
        }
      })
      .catch(e => { if (!cancelled) setError(String(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [attendingId, fyStartYear]);

  // Derived values that depend on the slider — compute locally so the slider feels instant.
  const weekendErRate = data?.components?.weekend_er_pay?.rate_per_shift || 2080;
  const weekendErAnnual = weekendErCount * weekendErRate;
  // Server's salary_total was computed with weekend_er_count=0, so we just add the local value.
  const salaryTotal = data ? data.salary_total + weekendErAnnual : null;

  // FY-to-date call summary: how many call days the attending has done so far, and the
  // wRVUs they read on those days. Past only — projection of future call days is part
  // of the schedule grid, not summarized here.
  const callSummary = useMemo(() => {
    if (!data?.per_day) return { days: 0, rvu: 0 };
    let days = 0, rvu = 0;
    for (const d of data.per_day) {
      if (d.on_call && d.is_past) {
        days += 1;
        rvu += d.actual_rvu || 0;
      }
    }
    return { days, rvu };
  }, [data]);

  const fmt$ = (n) => (n == null ? '—' : `$${Math.round(n).toLocaleString()}`);
  const fmt0 = (n) => (n == null ? '—' : Math.round(n).toLocaleString());

  // Color scheme — consistent with My Stats. Daytime sky-blue, after-hours emerald-green.
  const DAYTIME_COLOR = 'text-sky-900';
  const AFTER_HOURS_COLOR = 'text-emerald-700';

  // Group per_day entries by month, then for each month build a calendar layout:
  // 6 rows × 7 cols (Sun..Sat) with leading blanks before the 1st of the month.
  const months = useMemo(() => {
    if (!data?.per_day) return [];
    const byMonth = {};
    for (const d of data.per_day) {
      const mk = d.date.slice(0, 7);
      if (!byMonth[mk]) byMonth[mk] = [];
      byMonth[mk].push(d);
    }
    return Object.entries(byMonth)
      // Most recent months first — easier to glance at the current state of the FY.
      .sort(([a], [b]) => b.localeCompare(a))
      .map(([monthKey, days]) => {
        const [yy, mm] = monthKey.split('-').map(Number);
        const firstDow = new Date(yy, mm - 1, 1).getDay();  // 0 = Sun
        const label = new Date(yy, mm - 1, 1).toLocaleDateString('en-US', {
          month: 'long', year: 'numeric',
        });
        const cells = Array(firstDow).fill(null).concat(days);
        while (cells.length % 7 !== 0) cells.push(null);
        return { monthKey, label, cells };
      });
  }, [data]);

  const dollarsPerRvu = data?.components?.after_hours_pay?.rate_per_rvu || 51;
  const weekdayErRate = data?.components?.weekday_er_pay?.rate_per_shift || 1840;
  // Shift-color palette — mirrors NeuroScheduleTab's LABEL_COLORS exactly so the individual
  // calendar reads the same way as the admin schedule grid.
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
  const renderHoverPopup = (d) => {
    if (!d) return null;
    const shiftLabel = d.shift || (d.on_call ? 'On call' : '—');
    // Call line: shown for any day the attending is on call, even when there's also a shift.
    const callLine = d.on_call ? (
      <div className="text-yellow-300 font-semibold">On call</div>
    ) : null;
    if (d.is_past) {
      const ahPay = (d.actual_after_hours_rvu || 0) * dollarsPerRvu;
      const isWeekdayErPast = d.shift === 'Flex/Nights' && !d.is_weekend && !d.is_holiday;
      const weekdayErPay = isWeekdayErPast ? weekdayErRate : 0;
      const weekendErPay = d.is_weekend_er_coverer ? weekendErRate : 0;
      return (
        <>
          <div className="font-semibold">{d.date} ({d.weekday})</div>
          <div className="text-gray-300">
            {d.is_weekend_er_coverer ? 'Weekend evening ER' : shiftLabel}
          </div>
          {callLine}
          <div className="mt-1">
            <span className="text-green-300">Actual:</span> {d.actual_rvu || 0} wRVU · {d.actual_exam_count || 0} exams
          </div>
          <div className="text-gray-400 text-base">
            daytime {d.actual_daytime_rvu || 0} · after-hours {d.actual_after_hours_rvu || 0}
          </div>
          <div className="mt-1 text-emerald-300">
            after-hours pay: {redactMoney ? '$•••' : '$' + Math.round(ahPay).toLocaleString()}
          </div>
          {weekdayErPay > 0 && (
            <div className="text-emerald-300">
              weekday ER shift: {redactMoney ? '$•••' : '$' + Math.round(weekdayErPay).toLocaleString()}
            </div>
          )}
          {weekendErPay > 0 && (
            <div className="text-emerald-300">
              weekend ER shift: {redactMoney ? '$•••' : '$' + Math.round(weekendErPay).toLocaleString()}
            </div>
          )}
        </>
      );
    }
    const isWeekdayErFut = d.shift === 'Flex/Nights' && !d.is_weekend && !d.is_holiday;
    const erPay = isWeekdayErFut ? weekdayErRate : 0;
    return (
      <>
        <div className="font-semibold">{d.date} ({d.weekday})</div>
        <div className="text-gray-300">{shiftLabel}</div>
        {callLine}
        <div className="mt-1">
          <span className="text-blue-300">Projected:</span> {d.projected_rvu || 0} wRVU
        </div>
        {erPay > 0 ? (
          <div className="mt-1 text-emerald-300">weekday ER shift: {redactMoney ? '$•••' : '$' + Math.round(erPay).toLocaleString()}</div>
        ) : (
          <div className="mt-1 text-gray-400 text-base">after-hours bonus rolled into the FY projection</div>
        )}
      </>
    );
  };

  return (
    <div className="space-y-4">
      {/* Cell hover popup — rendered at top so it isn't clipped by any container overflow. */}
      {hoverCell && (
        <div className="fixed z-50 pointer-events-none bg-gray-900 text-white text-lg rounded shadow-lg px-5 py-4 min-w-[340px] leading-snug"
             style={{ left: hoverCell.x, top: hoverCell.y, transform: 'translateX(-50%)' }}>
          {renderHoverPopup(hoverCell.day)}
        </div>
      )}

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
              <option key={a.id} value={a.id}>{a.name}</option>
            ))}
          </select>
        </div>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">{error}</div>
      )}

      {loading && !data && (
        <div className="bg-white rounded-lg shadow p-6 text-gray-500">Loading projection…</div>
      )}

      {data && (
        <>
          {/* Top-line projected annual salary */}
          <div className="bg-white rounded-lg shadow p-6">
            <div className="flex items-center justify-between mb-1">
              <div className="text-base uppercase tracking-wide text-gray-500">
                {data.attending_name} · FY {data.fy_start.slice(0, 4)}–{data.fy_end.slice(2, 4)}
              </div>
              {(data.available_fys || []).length > 1 && (
                <select
                  value={data.fy_start_year}
                  onChange={e => setFyStartYear(Number(e.target.value))}
                  className="text-sm border border-gray-300 rounded px-2 py-1"
                  title="Pick which fiscal year to project"
                >
                  {data.available_fys.map(y => (
                    <option key={y} value={y}>FY {y}–{String(y + 1).slice(2)}</option>
                  ))}
                </select>
              )}
            </div>
            <div className="mt-2">
              <span className="text-5xl font-bold text-gray-900"><Redacted on={redactMoney}>{fmt$(salaryTotal)}</Redacted></span>
              <span className="ml-3 text-gray-600 text-base">projected FY total</span>
            </div>
            <div className="mt-1 text-base text-gray-500">
              Past: through {data.past_end} · Future: schedule + projected after-hours
            </div>
          </div>

          {/* Weekend ER slider — seeded from past actuals so the projection starts realistic */}
          <div className="bg-white rounded-lg shadow p-6">
            <h3 className="text-lg font-semibold text-gray-900 mb-2">Weekend evening ER shifts</h3>
            <p className="text-base text-gray-600 mb-3">
              Total annual weekend ER shifts (past + planned future). Slider starts at the{' '}
              <strong>{data.components.weekend_er_pay.shifts_past || 0}</strong> shift
              {(data.components.weekend_er_pay.shifts_past || 0) === 1 ? '' : 's'} you've already done
              this FY; drag higher to model more. Each pays{' '}
              <strong><Redacted on={redactMoney}>{fmt$(data.components.weekend_er_pay.rate_per_shift)}</Redacted></strong>.
            </p>
            <div className="flex items-center gap-4">
              <input
                type="range"
                min={data.components.weekend_er_pay.shifts_past || 0}
                max="52"
                step="1"
                value={weekendErCount}
                onChange={e => { userTouchedSlider.current = true; setWeekendErCount(Number(e.target.value)); }}
                className="flex-1"
              />
              <div className="text-2xl font-bold text-gray-900 w-20 text-right">
                {weekendErCount}
              </div>
              <div className={`text-xl font-semibold ${AFTER_HOURS_COLOR} w-32 text-right`}>
                <Redacted on={redactMoney}>{fmt$(weekendErAnnual)}</Redacted>
              </div>
            </div>
          </div>

          {/* Salary breakdown table */}
          <div className="bg-white rounded-lg shadow p-6">
            <h3 className="text-lg font-semibold text-gray-900 mb-3">Salary components</h3>
            <table className="min-w-full text-base">
              <thead>
                <tr className="border-b border-gray-200 text-gray-600">
                  <th className="text-left py-2 pr-4 font-medium">Component</th>
                  <th className="text-left py-2 pr-4 font-medium">Detail</th>
                  <th className="text-right py-2 font-medium">Annual</th>
                </tr>
              </thead>
              <tbody>
                <tr className="border-b border-gray-100">
                  <td className="py-2 pr-4">TNS</td>
                  <td className="py-2 pr-4 text-gray-500">
                    <Redacted on={redactMoney}>{fmt$(data.components.tns.monthly_rate)}</Redacted>/month × 12 months
                  </td>
                  <td className="py-2 text-right"><Redacted on={redactMoney}>{fmt$(data.components.tns.annual)}</Redacted></td>
                </tr>
                <tr className="border-b border-gray-100">
                  <td className="py-2 pr-4">TAT</td>
                  <td className="py-2 pr-4 text-gray-500">{data.components.tat.rate_pct}% of TNS</td>
                  <td className="py-2 text-right"><Redacted on={redactMoney}>{fmt$(data.components.tat.annual)}</Redacted></td>
                </tr>
                <tr className="border-b border-gray-100">
                  <td className="py-2 pr-4">Section bonus</td>
                  <td className="py-2 pr-4 text-gray-500">
                    {data.components.section_bonus.rate_pct}% of TNS · {data.components.section_bonus.note}
                  </td>
                  <td className="py-2 text-right"><Redacted on={redactMoney}>{fmt$(data.components.section_bonus.annual)}</Redacted></td>
                </tr>
                <tr className={`border-b border-gray-100 ${AFTER_HOURS_COLOR}`}>
                  <td className="py-2 pr-4">After-hours $/RVU</td>
                  <td className="py-2 pr-4 text-gray-500">
                    {fmt0(data.components.after_hours_pay.past_rvu)} past
                    {' + '}{fmt0(data.components.after_hours_pay.projected_future_rvu)} projected{' '}
                    ({data.components.after_hours_pay.historical_rate_per_day}/day rate)
                    {' = '}{fmt0(data.components.after_hours_pay.total_rvu)} wRVU
                    {' × '}${data.components.after_hours_pay.rate_per_rvu}/RVU
                  </td>
                  <td className="py-2 text-right"><Redacted on={redactMoney}>{fmt$(data.components.after_hours_pay.annual)}</Redacted></td>
                </tr>
                <tr className={`border-b border-gray-100 ${AFTER_HOURS_COLOR}`}>
                  <td className="py-2 pr-4">Weekday evening ER</td>
                  <td className="py-2 pr-4 text-gray-500">
                    {data.components.weekday_er_pay.shifts_past} past + {data.components.weekday_er_pay.shifts_future} scheduled
                    {data.components.weekday_er_pay.shifts_estimated > 0
                      && ` + ${data.components.weekday_er_pay.shifts_estimated} estimated`}
                    {' = '}{data.components.weekday_er_pay.shifts_total} shifts × <Redacted on={redactMoney}>{fmt$(data.components.weekday_er_pay.rate_per_shift)}</Redacted>
                    {data.components.weekday_er_pay.shifts_estimated > 0
                      && ` · estimate based on ${data.components.weekday_er_pay.historical_annual_count}/yr historical pace`}
                  </td>
                  <td className="py-2 text-right"><Redacted on={redactMoney}>{fmt$(data.components.weekday_er_pay.annual)}</Redacted></td>
                </tr>
                <tr className={`border-b border-gray-100 ${AFTER_HOURS_COLOR}`}>
                  <td className="py-2 pr-4">Weekend evening ER</td>
                  <td className="py-2 pr-4 text-gray-500">
                    {data.components.weekend_er_pay.shifts_past || 0} past
                    {' + '}{Math.max(0, weekendErCount - (data.components.weekend_er_pay.shifts_past || 0))} future
                    {' = '}{weekendErCount} shifts × <Redacted on={redactMoney}>{fmt$(weekendErRate)}</Redacted>
                  </td>
                  <td className="py-2 text-right"><Redacted on={redactMoney}>{fmt$(weekendErAnnual)}</Redacted></td>
                </tr>
                {data.components.flex_star_pay && data.components.flex_star_pay.shifts_total > 0 && (
                  <tr className="border-b border-gray-100">
                    <td className="py-2 pr-4">Peter coverage</td>
                    <td className="py-2 pr-4 text-gray-500">
                      {data.components.flex_star_pay.shifts_past} past + {data.components.flex_star_pay.shifts_future} future
                      {' = '}{data.components.flex_star_pay.shifts_total} shifts × <Redacted on={redactMoney}>{fmt$(data.components.flex_star_pay.rate_per_shift)}</Redacted>
                    </td>
                    <td className="py-2 text-right"><Redacted on={redactMoney}>{fmt$(data.components.flex_star_pay.annual)}</Redacted></td>
                  </tr>
                )}
                <tr className="font-semibold text-lg">
                  <td className="pt-3">TOTAL</td>
                  <td></td>
                  <td className="pt-3 text-right"><Redacted on={redactMoney}>{fmt$(salaryTotal)}</Redacted></td>
                </tr>
              </tbody>
            </table>
          </div>

          {/* FY calendar — one month grid per month, Sun–Sat columns. Each cell shows the day
              number and the shift label (color-coded). Hover for actual reads / pay detail. */}
          <div className="bg-white rounded-lg shadow p-6">
            <h3 className="text-lg font-semibold text-gray-900 mb-3">
              FY schedule (past + scheduled future)
            </h3>
            <p className="text-sm text-gray-500 mb-2">
              Hover any day for that day's pay components and actual reads (past) or projection (future).
              Future days are shown in italics. Days you're on call are flagged with a yellow{' '}
              <span className="inline-block bg-yellow-400 text-gray-900 text-[10px] font-bold rounded-sm px-1 py-0.5 not-italic align-middle">C</span>
              {' '}badge in the corner.
            </p>
            <div className="text-sm text-gray-700 mb-4">
              Call days so far this FY:{' '}
              <strong>{callSummary.days}</strong>
              {' · '}wRVU read on those days:{' '}
              <strong>{fmt0(callSummary.rvu)}</strong>
            </div>
            {/* dir="rtl" makes the grid fill right-to-left while months are already in
                descending order, so the most-recent month lands in the top-right corner.
                Each child is forced back to dir="ltr" so its calendar reads normally. */}
            <div dir="rtl" className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6">
              {months.map(({ monthKey, label, cells }) => (
                <div dir="ltr" key={monthKey}>
                  <div className="text-base font-semibold text-gray-800 mb-2">{label}</div>
                  <div className="grid grid-cols-7 gap-px bg-gray-200 border border-gray-200 rounded overflow-hidden">
                    {['S', 'M', 'T', 'W', 'T', 'F', 'S'].map((d, i) => (
                      <div key={`h-${i}`} className="bg-gray-50 text-xs font-medium text-gray-500 text-center py-1">
                        {d}
                      </div>
                    ))}
                    {cells.map((d, i) => {
                      if (!d) return <div key={`b-${i}`} className="bg-gray-50 h-16" />;
                      const shiftLabel = d.shift || (d.on_call ? 'On call' : '');
                      const bg = SHIFT_COLORS[d.shift] || (d.on_call ? '#fff7c0' : '#ffffff');
                      const fg = DARK_BG_COLORS.has(bg) ? '#ffffff' : '#1f2937';
                      return (
                        <div
                          key={d.date}
                          onMouseEnter={(e) => {
                            const r = e.currentTarget.getBoundingClientRect();
                            scheduleHover(d, r.left + r.width / 2, r.bottom + 6);
                          }}
                          onMouseLeave={clearHover}
                          style={{ backgroundColor: bg, color: fg }}
                          className={`relative h-16 p-1 ${d.is_past ? '' : 'italic'} cursor-help hover:ring-2 hover:ring-blue-400 hover:ring-inset`}
                        >
                          <div className="text-xs font-semibold leading-tight">
                            {Number(d.date.slice(8, 10))}
                          </div>
                          {shiftLabel && (
                            <div className="text-[10px] leading-tight mt-0.5 truncate">
                              {shiftLabel}
                            </div>
                          )}
                          {/* Call badge — pinned to the top-right corner so it's visible even
                              when the day also has a shift. */}
                          {d.on_call && (
                            <div
                              title="On call"
                              className="absolute top-0.5 right-0.5 bg-yellow-400 text-gray-900 text-[10px] font-bold leading-none rounded-sm px-1 py-0.5 not-italic"
                            >
                              C
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
