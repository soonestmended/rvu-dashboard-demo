import React, { useState, useEffect, useMemo, useRef } from 'react';
import Redacted from './Redacted';

const CHOW_ID = 'ATT000772';
const CHANG_ID = 'ATT000767';
const SADIGH_ID = 'ATT003605';
const FLORIOLLI_ID = 'ATT001132';
const KEVIN_ID = 'PHAM';
const KEVIN_FTE = 0.9;
const KEVIN_MONTHLY_TNS = 440000 / 12;
const WEEKDAY_ER_PAY = 1840;
const WEEKEND_ER_PAY = 2080;
// Pham's expected ER load when added to the rotation
const PHAM_WEEKDAY_ER_PER_MONTH = 2;
const PHAM_WEEKEND_ER_PER_MONTH = 1;
// Attendings who don't work weekday evening ER (Sadigh, Chang)
const WEEKDAY_ER_INELIGIBLE = new Set([CHANG_ID, SADIGH_ID]);
// Attendings who don't work weekend evening ER (above + Floriolli)
const WEEKEND_ER_INELIGIBLE = new Set([CHANG_ID, SADIGH_ID, FLORIOLLI_ID]);

// Distribute `total` integer items among N slots as evenly as possible (largest-remainder).
// Returns array of length N summing to `total`.
function evenIntegerSplit(total, n) {
  if (n <= 0) return [];
  const base = Math.floor(total / n);
  const remainder = total - base * n;
  return Array.from({ length: n }, (_, i) => base + (i < remainder ? 1 : 0));
}

// Reduce per-id integer counts by `take`, proportional to existing counts (largest-remainder).
// Mutates `counts[id][key]`. Returns shifts actually taken.
function takeFromPool(counts, ids, key, take) {
  const eligible = ids.filter(id => counts[id][key] > 0);
  const total = eligible.reduce((s, id) => s + counts[id][key], 0);
  if (total === 0) return 0;
  if (take >= total) {
    eligible.forEach(id => { counts[id][key] = 0; });
    return total;
  }
  const targetTotal = total - take;
  const fair = eligible.map(id => {
    const exact = counts[id][key] * targetTotal / total;
    const floor = Math.floor(exact);
    return { id, floor, frac: exact - floor };
  });
  let remainder = targetTotal - fair.reduce((s, x) => s + x.floor, 0);
  fair.sort((a, b) => b.frac - a.frac);
  for (const x of fair) {
    counts[x.id][key] = x.floor + (remainder > 0 ? 1 : 0);
    if (remainder > 0) remainder--;
  }
  return take;
}

export default function NeuroCompensationTab({ proposedCompData, neuroConfig, scheduleShiftCounts, redactMoney = false }) {
  const [dollarsPerRvu, setDollarsPerRvu] = useState(51);
  const [sectionBonusPct, setSectionBonusPct] = useState(4);
  const [tnsIncreasePct, setTnsIncreasePct] = useState(2.2);
  const [growthPct, setGrowthPct] = useState(0);
  const [withKevin, setWithKevin] = useState(false);
  const [projectFullYear, setProjectFullYear] = useState(false);

  // Per-attending RVU values driven by sliders. Source of truth once user starts adjusting.
  const [rvuValues, setRvuValues] = useState({});
  const [lockedIds, setLockedIds] = useState(() => new Set());

  // Per-attending ER shift counts ({id: {weekday, weekend}}). Editable.
  const [erShiftCounts, setErShiftCounts] = useState({});
  const [erPanelExpanded, setErPanelExpanded] = useState(false);

  // No early return here — moved to the end of the hook block (just before the JSX) so the
  // number of hooks called doesn't change when proposedCompData transitions empty→populated.
  // (React error #310 if hook counts differ between renders.)
  const hasData = !!(proposedCompData && Array.isArray(proposedCompData) && proposedCompData.length > 0);

  const annual70 = neuroConfig?.annual_rvu_expectation_70th || 10509;

  const periodMonths = useMemo(() => {
    if (!proposedCompData || proposedCompData.length === 0) return 3;
    const first = proposedCompData[0];
    if (first.fte > 0 && annual70 > 0) {
      const periodFrac = first.benchmark_70th / (annual70 * first.fte);
      return Math.round(periodFrac * 12);
    }
    return 3;
  }, [proposedCompData, annual70]);
  const periodFrac = periodMonths / 12;

  // When "Project to one year" is on, scale data values from the selected period up to a full year.
  const projectionMult = projectFullYear && periodMonths > 0 ? 12 / periodMonths : 1;
  const effectivePeriodMonths = projectFullYear ? 12 : periodMonths;
  const effectivePeriodFrac = projectFullYear ? 1 : periodFrac;

  // Baseline = post-growth, post-Kevin, post-projection starting point. Sliders adjust away from this.
  // Chow is excluded as a row but his historical RVUs stay in the section pool — his work
  // gets redistributed across the remaining attendings proportional to FTE so the projection
  // reflects what the section actually produced.
  const baseline = useMemo(() => {
    const growthMult = 1 + growthPct / 100;
    const dataExChow = proposedCompData.filter(a => a.attending_id !== CHOW_ID);
    const chow = proposedCompData.find(a => a.attending_id === CHOW_ID);
    const totalNonChowFte = dataExChow.reduce((s, a) => s + a.fte, 0);

    // Section pool INCLUDING Chow's contribution, post-scaling
    const totalRvusInclChow = proposedCompData
      .reduce((sum, a) => sum + a.total_rvus * growthMult * projectionMult, 0);
    const kevinBenchmark = annual70 * KEVIN_FTE * effectivePeriodFrac;
    const kevinReduction = withKevin && totalRvusInclChow > 0
      ? (totalRvusInclChow - kevinBenchmark) / totalRvusInclChow
      : 1;

    // Distribute Chow's per-component RVUs to each non-Chow attending by FTE share
    const chowShareFor = (att, key) => {
      if (!chow || totalNonChowFte === 0) return 0;
      return (chow[key] || 0) * (att.fte / totalNonChowFte);
    };

    const rows = dataExChow.map(att => {
      const reduction = growthMult * kevinReduction * projectionMult;
      return {
        attending_id: att.attending_id,
        attending_name: att.attending_name,
        fte: att.fte,
        monthly_tns: att.monthly_tns || 38000,
        // 70th benchmark scales with effective period (annualized when projecting)
        benchmark_70th: att.benchmark_70th * projectionMult,
        exam_count: att.exam_count,
        // Qualification status from backend (proposed-model endpoint)
        qualified_for_rvu_bonus: att.qualified_for_rvu_bonus ?? true,
        qualifying_rvus: att.qualifying_rvus ?? 0,
        qualifying_benchmark: att.qualifying_benchmark ?? 0,
        is_new_hire: att.is_new_hire ?? false,
        total_rvus: (att.total_rvus + chowShareFor(att, 'total_rvus')) * reduction,
        daytime_rvus: (att.daytime_rvus + chowShareFor(att, 'daytime_rvus')) * reduction,
        call_rvus: (att.call_rvus + chowShareFor(att, 'call_rvus')) * reduction,
        evening_er_rvus: (att.evening_er_rvus + chowShareFor(att, 'evening_er_rvus')) * reduction,
        after_hours_rvus: ((att.after_hours_rvus || 0) + chowShareFor(att, 'after_hours_rvus')) * reduction,
      };
    });

    if (withKevin) {
      const tDay = rows.reduce((s, r) => s + r.daytime_rvus, 0);
      const tCall = rows.reduce((s, r) => s + r.call_rvus, 0);
      const tEve = rows.reduce((s, r) => s + r.evening_er_rvus, 0);
      const tAh = rows.reduce((s, r) => s + r.after_hours_rvus, 0);
      const cat = tDay + tCall + tEve + tAh;
      rows.push({
        attending_id: KEVIN_ID,
        attending_name: 'Pham, Kevin',
        fte: KEVIN_FTE,
        monthly_tns: KEVIN_MONTHLY_TNS,
        benchmark_70th: kevinBenchmark,
        exam_count: 0,
        // Pham auto-qualifies as a new hire
        qualified_for_rvu_bonus: true,
        qualifying_rvus: 0,
        qualifying_benchmark: 0,
        is_new_hire: true,
        total_rvus: kevinBenchmark,
        daytime_rvus: cat > 0 ? kevinBenchmark * tDay / cat : kevinBenchmark,
        call_rvus: cat > 0 ? kevinBenchmark * tCall / cat : 0,
        evening_er_rvus: cat > 0 ? kevinBenchmark * tEve / cat : 0,
        after_hours_rvus: cat > 0 ? kevinBenchmark * tAh / cat : 0,
      });
    }

    return rows;
  }, [proposedCompData, growthPct, withKevin, effectivePeriodFrac, projectionMult, annual70]);

  const baseMap = useMemo(() => {
    const m = {};
    baseline.forEach(r => { m[r.attending_id] = r; });
    return m;
  }, [baseline]);

  // Fixed section RVU target — held constant as sliders move
  const sectionTarget = useMemo(
    () => baseline.reduce((s, r) => s + r.total_rvus, 0),
    [baseline]
  );

  // Distinguish "structural" changes (new data, Kevin toggle, period) from a growth-only change.
  // Structural → full reset of slider state; growth-only → preserve user values + locks and
  // redistribute the section delta across unlocked attendings (so locked rows stay put).
  // Chow is auto-locked: excluded from comp modeling but his RVUs still count toward the section total.
  const prevSnapshot = useRef({ data: null, withKevin: null, projectFullYear: null });
  useEffect(() => {
    const isStructural =
      prevSnapshot.current.data !== proposedCompData ||
      prevSnapshot.current.withKevin !== withKevin ||
      prevSnapshot.current.projectFullYear !== projectFullYear;

    if (isStructural) {
      const init = {};
      baseline.forEach(r => { init[r.attending_id] = r.total_rvus; });
      setRvuValues(init);
      setLockedIds(new Set());
    } else {
      setRvuValues(prev => {
        if (Object.keys(prev).length === 0) {
          const init = {};
          baseline.forEach(r => { init[r.attending_id] = r.total_rvus; });
          return init;
        }
        const newTarget = baseline.reduce((s, r) => s + r.total_rvus, 0);
        const oldSum = Object.values(prev).reduce((s, v) => s + v, 0);
        const delta = newTarget - oldSum;
        if (Math.abs(delta) < 0.01) return prev;

        const unlocked = baseline
          .filter(r => !lockedIds.has(r.attending_id))
          .map(r => ({ id: r.attending_id, fte: r.fte }));
        const totalUnlockedFte = unlocked.reduce((s, x) => s + x.fte, 0);
        if (totalUnlockedFte === 0) return prev;

        const next = { ...prev };
        for (const { id, fte } of unlocked) {
          next[id] = Math.max(0, (prev[id] || 0) + delta * (fte / totalUnlockedFte));
        }
        return next;
      });
    }
    prevSnapshot.current = { data: proposedCompData, withKevin, projectFullYear };
  }, [baseline, proposedCompData, withKevin, projectFullYear, lockedIds]);

  // Slider handler: enforce section total constancy by redistributing delta across unlocked others by FTE
  const handleRvuChange = (id, newValue) => {
    setRvuValues(prev => {
      if (!(id in prev)) return prev;
      const oldValue = prev[id];
      const others = Object.keys(prev).filter(k => k !== id && !lockedIds.has(k));
      const totalOtherFte = others.reduce((s, k) => s + (baseMap[k]?.fte || 0), 0);
      if (totalOtherFte === 0) return prev; // nothing to redistribute against

      // Hard ceiling: can't take more from others than they currently have
      const availableFromOthers = others.reduce((s, k) => s + prev[k], 0);
      const clamped = Math.max(0, Math.min(newValue, oldValue + availableFromOthers));
      const delta = clamped - oldValue;

      const next = { ...prev, [id]: clamped };
      for (const k of others) {
        const share = delta * (baseMap[k].fte / totalOtherFte);
        next[k] = Math.max(0, prev[k] - share);
      }

      // Correct any drift from clamping by topping up / drawing from the largest unlocked others
      const sum = Object.values(next).reduce((s, v) => s + v, 0);
      const drift = sectionTarget - sum;
      if (Math.abs(drift) > 0.01) {
        const eligible = others.filter(k => drift > 0 || next[k] > 0);
        const ftSum = eligible.reduce((s, k) => s + baseMap[k].fte, 0);
        if (ftSum > 0) {
          for (const k of eligible) {
            next[k] = Math.max(0, next[k] + drift * baseMap[k].fte / ftSum);
          }
        }
      }
      return next;
    });
  };

  const toggleLock = (id) => {
    setLockedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const distributeByFte = () => {
    const lockedTotal = baseline
      .filter(r => lockedIds.has(r.attending_id))
      .reduce((s, r) => s + (rvuValues[r.attending_id] || 0), 0);
    const remaining = sectionTarget - lockedTotal;
    const unlocked = baseline.filter(r => !lockedIds.has(r.attending_id));
    const totalFte = unlocked.reduce((s, r) => s + r.fte, 0);
    if (totalFte === 0) return;

    setRvuValues(prev => {
      const next = { ...prev };
      for (const r of unlocked) {
        next[r.attending_id] = Math.max(0, remaining * r.fte / totalFte);
      }
      return next;
    });
  };

  const resetToBaseline = () => {
    const init = {};
    baseline.forEach(r => { init[r.attending_id] = r.total_rvus; });
    setRvuValues(init);
    setLockedIds(new Set());
  };

  // Raw ER shift counts from the schedule (weekday Flex/Nights + empirical weekend ER).
  // Scaled by projectionMult when "Project to one year" is on; per-attending counts are rounded to integers.
  const actualScheduleErCounts = useMemo(() => {
    const counts = {};
    baseline.forEach(b => { counts[b.attending_id] = { weekday: 0, weekend: 0 }; });
    if (Array.isArray(scheduleShiftCounts)) {
      for (const row of scheduleShiftCounts) {
        if (!counts[row.attending_id]) counts[row.attending_id] = { weekday: 0, weekend: 0 };
        if (row.shift_name === 'Flex/Nights') counts[row.attending_id].weekday += row.count;
        else if (row.shift_name === 'Weekend Evening ER') counts[row.attending_id].weekend += row.count;
      }
    }
    if (projectionMult !== 1) {
      for (const id of Object.keys(counts)) {
        counts[id].weekday = Math.round(counts[id].weekday * projectionMult);
        counts[id].weekend = Math.round(counts[id].weekend * projectionMult);
      }
    }
    return counts;
  }, [scheduleShiftCounts, baseline, projectionMult]);

  // Per-shift-type eligibility (everyone except the per-shift ineligible set).
  // Chow is already excluded via baseline.
  const eligibleFor = (key) => {
    const blocked = key === 'weekend' ? WEEKEND_ER_INELIGIBLE : WEEKDAY_ER_INELIGIBLE;
    return baseline
      .filter(b => !blocked.has(b.attending_id))
      .map(b => b.attending_id);
  };
  const weekdayEligibleIds = useMemo(() => eligibleFor('weekday'), [baseline]);
  const weekendEligibleIds = useMemo(() => eligibleFor('weekend'), [baseline]);

  // Build the default ER shift counts:
  // - Weekday: split current weekday total evenly across eligible (incl. Pham if added)
  // - Weekend: use actual schedule values (Pham, if added, gets fixed allocation; subtract from pool)
  const buildDefaultErCounts = () => {
    const init = {};
    baseline.forEach(b => { init[b.attending_id] = { weekday: 0, weekend: 0 }; });
    if (withKevin && !init[KEVIN_ID]) init[KEVIN_ID] = { weekday: 0, weekend: 0 };

    const totalWeekday = Object.values(actualScheduleErCounts).reduce((s, c) => s + c.weekday, 0);
    // weekdayEligibleIds already includes Pham when withKevin (he's in baseline), so don't re-append.
    const wdRecipients = weekdayEligibleIds;
    const wdSplit = evenIntegerSplit(totalWeekday, wdRecipients.length);
    wdRecipients.forEach((id, i) => { init[id].weekday = wdSplit[i]; });

    // Weekend: start from actuals, but route shifts from non-eligible attendings (Floriolli, Chow,
    // anyone else not in baseline) into a surplus pool that gets redistributed to the eligible set.
    const weekendEligibleSet = new Set(weekendEligibleIds);
    let weekendSurplus = 0;
    for (const id of Object.keys(actualScheduleErCounts)) {
      const actual = actualScheduleErCounts[id].weekend || 0;
      if (weekendEligibleSet.has(id)) {
        init[id].weekend = actual;
      } else {
        weekendSurplus += actual;
        if (init[id]) init[id].weekend = 0;
      }
    }
    if (weekendSurplus > 0 && weekendEligibleIds.length > 0) {
      const bonus = evenIntegerSplit(weekendSurplus, weekendEligibleIds.length);
      weekendEligibleIds.forEach((id, i) => { init[id].weekend += bonus[i]; });
    }

    if (withKevin) {
      const phamWd = PHAM_WEEKDAY_ER_PER_MONTH * effectivePeriodMonths;
      const phamWe = PHAM_WEEKEND_ER_PER_MONTH * effectivePeriodMonths;
      // Override Pham's weekday from even split with explicit 2/mo, give remainder back to eligible
      const wdDelta = init[KEVIN_ID].weekday - phamWd;
      init[KEVIN_ID].weekday = phamWd;
      if (wdDelta !== 0 && weekdayEligibleIds.length > 0) {
        const adj = evenIntegerSplit(wdDelta, weekdayEligibleIds.length);
        weekdayEligibleIds.forEach((id, i) => { init[id].weekday += adj[i]; });
      }
      init[KEVIN_ID].weekend = phamWe;
      takeFromPool(init, weekendEligibleIds, 'weekend', phamWe);
    }
    return init;
  };

  // Reset ER counts on any baseline / Kevin / period change
  useEffect(() => {
    setErShiftCounts(buildDefaultErCounts());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseline, withKevin, periodMonths, actualScheduleErCounts]);

  const erCounts = erShiftCounts;

  // Total ER shifts that need coverage in this period (from actual schedule).
  // This is a fixed constraint: sum of per-attending counts + unassigned pool = totalFixed.
  const totalFixedErShifts = useMemo(() => ({
    weekday: Object.values(actualScheduleErCounts).reduce((s, c) => s + c.weekday, 0),
    weekend: Object.values(actualScheduleErCounts).reduce((s, c) => s + c.weekend, 0),
  }), [actualScheduleErCounts]);

  const erAssigned = useMemo(() => ({
    weekday: Object.values(erShiftCounts).reduce((s, c) => s + (c?.weekday || 0), 0),
    weekend: Object.values(erShiftCounts).reduce((s, c) => s + (c?.weekend || 0), 0),
  }), [erShiftCounts]);

  const erUnassigned = {
    weekday: Math.max(0, totalFixedErShifts.weekday - erAssigned.weekday),
    weekend: Math.max(0, totalFixedErShifts.weekend - erAssigned.weekend),
  };

  // Decreases freely add to the unassigned pool; increases are clamped to what's in the pool.
  const setErForAttending = (id, key, value) => {
    setErShiftCounts(prev => {
      const oldVal = prev[id]?.[key] || 0;
      const newVal = Math.max(0, Math.round(value));
      const delta = newVal - oldVal;
      if (delta > 0) {
        const totalAssigned = Object.values(prev).reduce((s, c) => s + (c?.[key] || 0), 0);
        const available = Math.max(0, totalFixedErShifts[key] - totalAssigned);
        const clampedDelta = Math.min(delta, available);
        return { ...prev, [id]: { ...prev[id], [key]: oldVal + clampedDelta } };
      }
      return { ...prev, [id]: { ...prev[id], [key]: newVal } };
    });
  };

  // Distribute the fixed total (incl. anything in the pool) evenly across eligible attendings.
  const distributeErEvenly = (key) => {
    // Eligible lists already include Pham (he's in baseline when withKevin); don't re-append.
    const recipients = key === 'weekend' ? weekendEligibleIds : weekdayEligibleIds;
    if (recipients.length === 0) return;
    const total = totalFixedErShifts[key];
    const split = evenIntegerSplit(total, recipients.length);
    setErShiftCounts(prev => {
      const next = { ...prev };
      Object.keys(next).forEach(id => { next[id] = { ...next[id], [key]: 0 }; });
      recipients.forEach((id, i) => {
        next[id] = { ...next[id], [key]: split[i] };
      });
      return next;
    });
  };

  const resetErToActual = () => {
    setErShiftCounts(buildDefaultErCounts());
  };

  const tableData = useMemo(() => {
    return baseline.map(b => {
      const cur = rvuValues[b.attending_id] ?? b.total_rvus;
      const ratio = b.total_rvus > 0 ? cur / b.total_rvus : 0;
      const daytime = b.daytime_rvus * ratio;
      const call = b.call_rvus * ratio;
      const evening = b.evening_er_rvus * ratio;
      // Use after_hours_rvus (time-based, matches the comp API's bonus bucket) rather
      // than moonlight_rvus (tag-based, narrower). Previously this tab paid the bonus
      // only on Moonlight-tagged rows while the Schedule tab paid on all after-hours —
      // same formula, different inputs, dollars-off between tabs. Now they agree.
      const afterHours = (b.after_hours_rvus || 0) * ratio;

      const tns = b.monthly_tns * (1 + tnsIncreasePct / 100) * effectivePeriodMonths;
      const tat = tns * 0.05;
      const sectionBonus = tns * sectionBonusPct / 100;

      // $/RVU paid on qualifying after-hours wRVU, gated by qualification (must clear the
      // FTE-prorated 65th-pctile over the 3-month qualification window — M3 of the
      // quarter-before-previous plus M1+M2 of the previous quarter — or be a new hire).
      const bonusRvus = b.qualified_for_rvu_bonus ? afterHours : 0;
      const rvuBonus = bonusRvus * dollarsPerRvu;

      const erC = erCounts[b.attending_id] || { weekday: 0, weekend: 0 };
      const erPay = erC.weekday * WEEKDAY_ER_PAY + erC.weekend * WEEKEND_ER_PAY;

      const total = tns + tat + sectionBonus + rvuBonus + erPay;

      return {
        ...b,
        total_rvus: cur,
        daytime_rvus: daytime,
        call_rvus: call,
        evening_er_rvus: evening,
        after_hours_rvus: afterHours,
        bonus_eligible_rvus: bonusRvus,
        tns,
        rvuBonus,
        sectionBonus,
        weekday_er_count: erC.weekday,
        weekend_er_count: erC.weekend,
        erPay,
        total,
      };
    }).sort((a, b) => a.attending_name.localeCompare(b.attending_name));
  }, [baseline, rvuValues, tnsIncreasePct, effectivePeriodMonths, sectionBonusPct, dollarsPerRvu, erCounts]);

  const totals = useMemo(() => {
    return tableData.reduce((acc, row) => ({
      total_rvus: acc.total_rvus + row.total_rvus,
      daytime_rvus: acc.daytime_rvus + row.daytime_rvus,
      call_rvus: acc.call_rvus + row.call_rvus,
      evening_er_rvus: acc.evening_er_rvus + row.evening_er_rvus,
      after_hours_rvus: acc.after_hours_rvus + row.after_hours_rvus,
      bonus_eligible_rvus: acc.bonus_eligible_rvus + row.bonus_eligible_rvus,
      rvuBonus: acc.rvuBonus + row.rvuBonus,
      sectionBonus: acc.sectionBonus + row.sectionBonus,
      tns: acc.tns + row.tns,
      weekday_er_count: acc.weekday_er_count + row.weekday_er_count,
      weekend_er_count: acc.weekend_er_count + row.weekend_er_count,
      erPay: acc.erPay + row.erPay,
      total: acc.total + row.total,
      exam_count: acc.exam_count + row.exam_count,
    }), {
      total_rvus: 0, daytime_rvus: 0, call_rvus: 0, evening_er_rvus: 0,
      after_hours_rvus: 0, bonus_eligible_rvus: 0, rvuBonus: 0, sectionBonus: 0,
      tns: 0, weekday_er_count: 0, weekend_er_count: 0, erPay: 0,
      total: 0, exam_count: 0,
    });
  }, [tableData]);

  const fmtRvu = (v) => v.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 });
  const fmtDollar = (v) => '$' + v.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 });

  // Section RVUs include Chow (excluded from comp modeling, but his work still counts toward section total).
  const sectionAllocated = useMemo(
    () => Object.values(rvuValues).reduce((s, v) => s + v, 0),
    [rvuValues]
  );
  const sectionDrift = sectionAllocated - sectionTarget;
  const totalFte = baseline.reduce((s, r) => s + r.fte, 0);

  // All hooks are now declared above this guard, so React sees the same hook count
  // on every render regardless of whether data is present.
  if (!hasData) {
    return (
      <div className="bg-white rounded-lg shadow p-6 text-gray-500">
        No compensation data available for the selected period.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Controls */}
      <div className="bg-white rounded-lg shadow p-6 space-y-4">
        <div className="flex items-center gap-4">
          <label className="font-medium text-gray-700 w-40">RVU growth:</label>
          <input
            type="range" min="0" max="20" step="0.5"
            value={growthPct}
            onChange={(e) => setGrowthPct(Number(e.target.value))}
            className="w-64"
          />
          <div className="flex items-center gap-1">
            <input
              type="number" min="0" max="20" step="0.5"
              value={growthPct}
              onChange={(e) => setGrowthPct(Number(e.target.value))}
              className="w-20 border border-gray-300 rounded px-2 py-1 text-right"
            />
            <span className="text-gray-500">%</span>
          </div>
          <span className="text-xs text-gray-500 ml-2">Adjusting growth resets per-attending sliders</span>
        </div>
        <div className="flex items-center gap-4">
          <label className="font-medium text-gray-700 w-40">TNS increase:</label>
          <input
            type="range" min="0" max="10" step="0.1"
            value={tnsIncreasePct}
            onChange={(e) => setTnsIncreasePct(Number(e.target.value))}
            className="w-64"
          />
          <div className="flex items-center gap-1">
            <input
              type="number" min="0" max="50" step="0.1"
              value={tnsIncreasePct}
              onChange={(e) => setTnsIncreasePct(Number(e.target.value))}
              className="w-20 border border-gray-300 rounded px-2 py-1 text-right"
            />
            <span className="text-gray-500">%</span>
          </div>
        </div>
        <div className="flex items-center gap-4">
          <label className="font-medium text-gray-700 w-40">$/RVU on moonlight:</label>
          <input
            type="range" min="40" max="70" step="1"
            value={dollarsPerRvu}
            onChange={(e) => setDollarsPerRvu(Number(e.target.value))}
            className="w-64"
          />
          <div className="flex items-center gap-1">
            <span className="text-gray-500">$</span>
            <input
              type="number" min="40" max="70" step="1"
              value={dollarsPerRvu}
              onChange={(e) => setDollarsPerRvu(Number(e.target.value))}
              className="w-20 border border-gray-300 rounded px-2 py-1 text-right"
            />
          </div>
        </div>
        <div className="flex items-center gap-4">
          <label className="font-medium text-gray-700 w-40">Section bonus:</label>
          <input
            type="range" min="0" max="4" step="1"
            value={sectionBonusPct}
            onChange={(e) => setSectionBonusPct(Number(e.target.value))}
            className="w-64"
          />
          <span className="text-lg font-medium text-gray-900 w-12">{sectionBonusPct}%</span>
        </div>
        <div className="flex items-center gap-4 pt-2 border-t">
          {/* The "Add Kevin" what-if models a specific real incoming hire by name — hidden on
              the demo (redactMoney is the demo-stack signal) so no real name is shown. */}
          {!redactMoney && (
          <button
            onClick={() => setWithKevin(!withKevin)}
            className={`px-4 py-2 rounded-md font-medium text-sm ${
              withKevin
                ? 'bg-blue-600 text-white hover:bg-blue-700'
                : 'bg-gray-200 text-gray-700 hover:bg-gray-300'
            }`}
          >
            {withKevin ? '✓ With Kevin (0.9 FTE)' : 'Add Kevin (0.9 FTE)'}
          </button>
          )}
          <label className="flex items-center gap-2 text-sm font-medium text-gray-700 cursor-pointer">
            <input
              type="checkbox"
              checked={projectFullYear}
              onChange={(e) => setProjectFullYear(e.target.checked)}
              className="w-4 h-4"
            />
            Project to one year
            {projectFullYear && periodMonths !== 12 && (
              <span className="text-xs text-gray-500 font-normal">
                (×{(12 / periodMonths).toFixed(2)} from {periodMonths}-month period)
              </span>
            )}
          </label>
          <button
            onClick={distributeByFte}
            className="px-4 py-2 rounded-md font-medium text-sm bg-purple-600 text-white hover:bg-purple-700"
          >
            Distribute evenly by FTE
          </button>
          <button
            onClick={resetToBaseline}
            className="px-4 py-2 rounded-md font-medium text-sm bg-gray-200 text-gray-700 hover:bg-gray-300"
          >
            Reset to actual
          </button>
        </div>

        {/* Summary cards */}
        <div className="grid grid-cols-4 gap-4 pt-2">
          <div className="bg-gray-50 rounded-lg p-3">
            <div className="text-sm text-gray-500">Section RVU Target</div>
            <div className="text-xl font-bold text-gray-900">{fmtRvu(sectionTarget)}</div>
          </div>
          <div className={`rounded-lg p-3 ${Math.abs(sectionDrift) > 1 ? 'bg-amber-50' : 'bg-gray-50'}`}>
            <div className="text-sm text-gray-500">Currently Allocated</div>
            <div className="text-xl font-bold text-gray-900">
              {fmtRvu(sectionAllocated)}
              {Math.abs(sectionDrift) > 1 && (
                <span className="text-sm font-normal text-amber-700 ml-2">
                  ({sectionDrift > 0 ? '+' : ''}{fmtRvu(sectionDrift)})
                </span>
              )}
            </div>
          </div>
          <div className="bg-gray-50 rounded-lg p-3">
            <div className="text-sm text-gray-500">Total Bonus-Eligible RVUs</div>
            <div className="text-xl font-bold text-green-600">{fmtRvu(totals.bonus_eligible_rvus)}</div>
          </div>
          <div className="bg-gray-50 rounded-lg p-3">
            <div className="text-sm text-gray-500">Total $/RVU Payout</div>
            <div className="text-xl font-bold text-blue-600"><Redacted on={redactMoney}>{fmtDollar(totals.rvuBonus)}</Redacted></div>
          </div>
        </div>
      </div>

      {/* ER shift planning panel */}
      <div className="bg-white rounded-lg shadow">
        <div className="flex items-center justify-between px-6 py-3 border-b">
          <button
            onClick={() => setErPanelExpanded(!erPanelExpanded)}
            className="flex items-center gap-2 text-left font-medium text-gray-700 hover:text-gray-900"
          >
            <span className="text-gray-400">{erPanelExpanded ? '▼' : '▶'}</span>
            <span>ER Shift Allocation</span>
            <span className="text-sm font-normal text-gray-500 ml-2">
              Weekday {erAssigned.weekday}/{totalFixedErShifts.weekday}
              {erUnassigned.weekday > 0 && <span className="text-amber-700"> ({erUnassigned.weekday} unassigned)</span>}
              {' · '}
              Weekend {erAssigned.weekend}/{totalFixedErShifts.weekend}
              {erUnassigned.weekend > 0 && <span className="text-amber-700"> ({erUnassigned.weekend} unassigned)</span>}
              {' · '}
              <Redacted on={redactMoney}>{fmtDollar(totals.erPay)}</Redacted>
            </span>
          </button>
          <div className="flex gap-2">
            <button
              onClick={() => distributeErEvenly('weekday')}
              className="px-3 py-1.5 rounded-md text-sm bg-purple-600 text-white hover:bg-purple-700"
            >
              Distribute weekday evenly
            </button>
            <button
              onClick={() => distributeErEvenly('weekend')}
              className="px-3 py-1.5 rounded-md text-sm bg-purple-600 text-white hover:bg-purple-700"
            >
              Distribute weekend evenly
            </button>
            <button
              onClick={resetErToActual}
              className="px-3 py-1.5 rounded-md text-sm bg-gray-200 text-gray-700 hover:bg-gray-300"
            >
              Reset
            </button>
          </div>
        </div>
        {erPanelExpanded && (
          <div className="px-6 py-4">
            <div className="text-xs text-gray-500 mb-2">
              Sadigh and Chang excluded — no ER shifts. Floriolli excluded from weekend ER.
              Total fixed at {totalFixedErShifts.weekday} weekday + {totalFixedErShifts.weekend} weekend; decreases go to the unassigned pool.
            </div>
            <div className="grid grid-cols-[1fr_auto_auto] gap-x-6 gap-y-1 items-center max-w-2xl">
              <div className="text-xs font-medium text-gray-500 uppercase">Attending</div>
              <div className="text-xs font-medium text-gray-500 uppercase text-center">Weekday</div>
              <div className="text-xs font-medium text-gray-500 uppercase text-center">Weekend</div>
              {[...baseline]
                .filter(b => !WEEKDAY_ER_INELIGIBLE.has(b.attending_id) || !WEEKEND_ER_INELIGIBLE.has(b.attending_id))
                .concat(withKevin && !baseline.some(b => b.attending_id === KEVIN_ID)
                  ? [{ attending_id: KEVIN_ID, attending_name: 'Pham, Kevin' }] : [])
                .sort((a, b) => a.attending_name.localeCompare(b.attending_name))
                .map(b => {
                  const c = erShiftCounts[b.attending_id] || { weekday: 0, weekend: 0 };
                  const wdAllowed = !WEEKDAY_ER_INELIGIBLE.has(b.attending_id);
                  const weAllowed = !WEEKEND_ER_INELIGIBLE.has(b.attending_id);
                  const StepInput = ({ value, onChange, disabled, atCap }) => (
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => onChange(value - 1)}
                        disabled={disabled || value <= 0}
                        className="w-7 h-7 rounded bg-gray-200 hover:bg-gray-300 text-gray-700 disabled:opacity-30 disabled:cursor-not-allowed"
                      >−</button>
                      <input
                        type="number" min="0" value={value} disabled={disabled}
                        onChange={(e) => onChange(Number(e.target.value))}
                        className="w-14 border border-gray-300 rounded px-2 py-1 text-right text-sm disabled:bg-gray-100 disabled:text-gray-400"
                      />
                      <button
                        onClick={() => onChange(value + 1)}
                        disabled={disabled || atCap}
                        title={atCap && !disabled ? 'Pool is empty' : undefined}
                        className="w-7 h-7 rounded bg-gray-200 hover:bg-gray-300 text-gray-700 disabled:opacity-30 disabled:cursor-not-allowed"
                      >+</button>
                    </div>
                  );
                  return (
                    <React.Fragment key={b.attending_id}>
                      <div className="text-sm text-gray-900">{b.attending_name}</div>
                      <StepInput value={c.weekday} disabled={!wdAllowed}
                                 atCap={erUnassigned.weekday <= 0}
                                 onChange={(v) => setErForAttending(b.attending_id, 'weekday', v)} />
                      <StepInput value={c.weekend} disabled={!weAllowed}
                                 atCap={erUnassigned.weekend <= 0}
                                 onChange={(v) => setErForAttending(b.attending_id, 'weekend', v)} />
                    </React.Fragment>
                  );
                })}
            </div>
          </div>
        )}
      </div>

      {/* Per-attending table */}
      <div className="bg-white rounded-lg shadow overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Attending</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">FTE</th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase" style={{ minWidth: '260px' }}>Total RVUs</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">70th Bench</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Daytime+Call</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">After-Hours</th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase" title="Qualified for $/RVU bonus: cleared FTE-prorated 65th-percentile over the 3-month qualification window (M3 of the quarter-before-previous + M1+M2 of the previous quarter), OR is a new hire">Qual?</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Bonus RVUs</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">$/RVU Payout</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase" title="Weekday ER × $1,840 + Weekend ER × $2,080">ER Pay</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">TNS</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">TAT (5%)</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Sect Bonus</th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Total Comp</th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
              {tableData.map((row) => {
                const daytimeCall = row.daytime_rvus + row.call_rvus;
                const afterHours = row.evening_er_rvus + row.after_hours_rvus;
                const isKevin = row.attending_id === KEVIN_ID;
                const isLocked = lockedIds.has(row.attending_id);
                // Slider max: 2× this row's FTE share of section, with a floor so even tiny FTE rows have room
                const fteShare = totalFte > 0 ? sectionTarget * row.fte / totalFte : sectionTarget / baseline.length;
                const sliderMax = Math.max(fteShare * 2, row.total_rvus * 1.5, 100);
                return (
                  <tr key={row.attending_id} className={`hover:bg-gray-50 ${isKevin ? 'bg-blue-50' : ''}`}>
                    <td className="px-4 py-3 whitespace-nowrap text-sm font-medium text-gray-900">
                      {row.attending_name}
                      {isKevin && <span className="text-xs text-blue-500 ml-1">(new)</span>}
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-500 text-right">
                      {row.fte.toFixed(2)}
                    </td>
                    <td className="px-2 py-3 whitespace-nowrap">
                      <div className="flex items-center gap-2 justify-center">
                        <input
                          type="range"
                          min="0"
                          max={sliderMax}
                          step="25"
                          value={Math.round(row.total_rvus)}
                          disabled={isLocked}
                          onChange={(e) => handleRvuChange(row.attending_id, Number(e.target.value))}
                          className="w-32"
                        />
                        <input
                          type="number"
                          min="0"
                          step="25"
                          value={Math.round(row.total_rvus)}
                          disabled={isLocked}
                          onChange={(e) => handleRvuChange(row.attending_id, Number(e.target.value))}
                          className="w-20 border border-gray-300 rounded px-2 py-1 text-right text-sm disabled:bg-gray-100"
                        />
                        <button
                          onClick={() => toggleLock(row.attending_id)}
                          title={isLocked ? 'Unlock' : 'Lock'}
                          className={`text-base ${isLocked ? 'opacity-100' : 'opacity-40 hover:opacity-80'}`}
                        >
                          {isLocked ? '🔒' : '🔓'}
                        </button>
                      </div>
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-500 text-right">
                      {fmtRvu(row.benchmark_70th)}
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-500 text-right">
                      {fmtRvu(daytimeCall)}
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-500 text-right">
                      {fmtRvu(afterHours)}
                    </td>
                    <td className="px-2 py-3 whitespace-nowrap text-sm text-center"
                        title={row.is_new_hire
                          ? 'New hire — auto-qualified for first quarter'
                          : `${fmtRvu(row.qualifying_rvus)} / ${fmtRvu(row.qualifying_benchmark)} over the qualification window — M3 of quarter-before-previous + M1+M2 of previous quarter (65th × FTE × 3/12)`}>
                      {row.qualified_for_rvu_bonus
                        ? <span className="text-green-600 font-bold">{row.is_new_hire ? '✓*' : '✓'}</span>
                        : <span className="text-gray-400">✗</span>}
                    </td>
                    <td className={`px-4 py-3 whitespace-nowrap text-sm text-right font-medium ${row.bonus_eligible_rvus > 0 ? 'text-green-600' : 'text-gray-400'}`}>
                      {fmtRvu(row.bonus_eligible_rvus)}
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-sm text-right font-medium text-blue-600">
                      <Redacted on={redactMoney}>{fmtDollar(row.rvuBonus)}</Redacted>
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-sm text-right text-gray-700"
                        title={`${row.weekday_er_count} weekday + ${row.weekend_er_count} weekend ER shifts`}>
                      <Redacted on={redactMoney}>{fmtDollar(row.erPay)}</Redacted>
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-sm text-right text-gray-500">
                      <Redacted on={redactMoney}>{fmtDollar(row.tns)}</Redacted>
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-sm text-right text-gray-500">
                      <Redacted on={redactMoney}>{fmtDollar(row.tns * 0.05)}</Redacted>
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-sm text-right text-gray-500">
                      <Redacted on={redactMoney}>{fmtDollar(row.sectionBonus)}</Redacted>
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap text-sm text-right font-medium text-gray-900">
                      <Redacted on={redactMoney}>{fmtDollar(row.total)}</Redacted>
                    </td>
                  </tr>
                );
              })}
              {/* Totals row */}
              <tr className="bg-gray-100 font-bold">
                <td className="px-4 py-3 text-sm text-gray-900">Total</td>
                <td className="px-4 py-3 text-sm text-right text-gray-500">{totalFte.toFixed(2)}</td>
                <td className="px-4 py-3 text-sm text-center text-gray-900">{fmtRvu(totals.total_rvus)}</td>
                <td className="px-4 py-3 text-sm text-right text-gray-500"></td>
                <td className="px-4 py-3 text-sm text-right text-gray-500"></td>
                <td className="px-4 py-3 text-sm text-right text-gray-500"></td>
                <td className="px-4 py-3 text-sm text-center text-gray-500"></td>
                <td className="px-4 py-3 text-sm text-right font-medium text-green-600">{fmtRvu(totals.bonus_eligible_rvus)}</td>
                <td className="px-4 py-3 text-sm text-right font-medium text-blue-600"><Redacted on={redactMoney}>{fmtDollar(totals.rvuBonus)}</Redacted></td>
                <td className="px-4 py-3 text-sm text-right text-gray-900"
                    title={`${totals.weekday_er_count} weekday + ${totals.weekend_er_count} weekend ER shifts`}>
                  <Redacted on={redactMoney}>{fmtDollar(totals.erPay)}</Redacted>
                </td>
                <td className="px-4 py-3 text-sm text-right text-gray-900"><Redacted on={redactMoney}>{fmtDollar(totals.tns)}</Redacted></td>
                <td className="px-4 py-3 text-sm text-right text-gray-900"><Redacted on={redactMoney}>{fmtDollar(totals.tns * 0.05)}</Redacted></td>
                <td className="px-4 py-3 text-sm text-right text-gray-900"><Redacted on={redactMoney}>{fmtDollar(totals.sectionBonus)}</Redacted></td>
                <td className="px-4 py-3 text-sm text-right font-medium text-gray-900"><Redacted on={redactMoney}>{fmtDollar(totals.total)}</Redacted></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
