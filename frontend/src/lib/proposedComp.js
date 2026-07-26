// Shared compensation engine for the proposed model.
// Consumers: NeuroCompensationTab (current/projection), NeuroScheduleTab (live projection on candidates),
// later NeuroCompHistoricalTab.

export const WEEKDAY_ER_PAY = 1840;
export const WEEKEND_ER_PAY = 2080;
// Peter coverage: per-shift bonus paid to whoever covers Chang's Flex* bookkeeping slot.
export const PETER_COVERAGE_PAY = 2000;

// Compute per-attending comp under the proposed model.
//
// Inputs:
//   attendings: [{
//     attending_id, attending_name, fte, monthly_tns,
//     daytime_rvus, call_rvus, evening_er_rvus, moonlight_rvus, total_rvus,
//     benchmark_70th,                  // displayed only; not used for bonus calc
//     qualified_for_rvu_bonus,         // bool — gate on $/RVU bonus
//     is_new_hire?, qualifying_rvus?, qualifying_benchmark?,  // optional, surfaced for UI
//     ...other fields preserved on output
//   }]
//   erShiftCounts: {attending_id: {weekday, weekend}} — drives ER pay
//   modelParams: {dollarsPerRvu, sectionBonusPct, tnsIncreasePct, periodMonths}
//
// Bonus rule (current model):
//   - $/RVU paid on after_hours_rvus (broader than legacy moonlight), when qualified_for_rvu_bonus
//     Falls back to moonlight_rvus if the caller hasn't supplied after_hours_rvus, for
//     backward compatibility during the migration.
//   - Section bonus: tns * sectionBonusPct/100 (caller passes the right pct)
//   - ER pay: weekday × $1,840 + weekend × $2,080
export function computeProposedComp({ attendings, erShiftCounts = {}, modelParams }) {
  const { dollarsPerRvu, sectionBonusPct, tnsIncreasePct, periodMonths } = modelParams;
  const rows = attendings.map(a => {
    const tns = (a.monthly_tns || 38000) * (1 + tnsIncreasePct / 100) * periodMonths;
    const tat = tns * 0.05;
    const sectionBonus = tns * sectionBonusPct / 100;

    const afterHours = a.after_hours_rvus != null ? a.after_hours_rvus : (a.moonlight_rvus || 0);
    const bonusRvus = a.qualified_for_rvu_bonus ? afterHours : 0;
    const rvuBonus = bonusRvus * dollarsPerRvu;

    const erC = erShiftCounts[a.attending_id] || { weekday: 0, weekend: 0 };
    const erPay = (erC.weekday || 0) * WEEKDAY_ER_PAY + (erC.weekend || 0) * WEEKEND_ER_PAY;

    // Peter coverage pay — per-attending count flows through on the row.
    const peterCoverageCount = a.peter_coverage_count || 0;
    const peterCoveragePay = peterCoverageCount * PETER_COVERAGE_PAY;

    const total = tns + tat + sectionBonus + rvuBonus + erPay + peterCoveragePay;

    return {
      ...a,
      bonus_eligible_rvus: bonusRvus,
      tns,
      tat,
      sectionBonus,
      rvuBonus,
      weekday_er_count: erC.weekday || 0,
      weekend_er_count: erC.weekend || 0,
      erPay,
      peter_coverage_count: peterCoverageCount,
      peterCoveragePay,
      total,
    };
  });

  const sumKeys = ['tns', 'tat', 'sectionBonus', 'rvuBonus', 'erPay', 'peterCoveragePay', 'total',
                   'bonus_eligible_rvus', 'total_rvus', 'daytime_rvus', 'call_rvus',
                   'evening_er_rvus', 'moonlight_rvus', 'weekday_er_count',
                   'weekend_er_count', 'peter_coverage_count', 'exam_count'];
  const totals = rows.reduce((acc, r) => {
    for (const k of sumKeys) acc[k] = (acc[k] || 0) + (r[k] || 0);
    return acc;
  }, {});

  return { rows, totals };
}

// Distribute `total` integer items among N slots as evenly as possible (largest-remainder).
// Returns array of length N summing to `total`.
export function evenIntegerSplit(total, n) {
  if (n <= 0) return [];
  const base = Math.floor(total / n);
  const remainder = total - base * n;
  return Array.from({ length: n }, (_, i) => base + (i < remainder ? 1 : 0));
}

// Reduce per-id integer counts by `take`, proportional to existing counts (largest-remainder).
// Mutates `counts[id][key]`. Returns shifts actually taken.
export function takeFromPool(counts, ids, key, take) {
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
