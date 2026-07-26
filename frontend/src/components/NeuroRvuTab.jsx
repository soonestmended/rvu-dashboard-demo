import { useMemo, useState } from 'react';

export default function NeuroRvuTab({ shiftByAttendingData, neuroStaffingData, neuroConfig, benchmark = '65th' }) {
  const [sortField, setSortField] = useState('attending_name');
  const [sortDirection, setSortDirection] = useState('asc');

  // Create a mapping from attending ID to name
  const attendingNameMap = useMemo(() => {
    if (!neuroConfig?.attendings) return {};
    const map = {};
    Object.entries(neuroConfig.attendings).forEach(([id, info]) => {
      map[id] = info.name;
    });
    return map;
  }, [neuroConfig]);

  // Create a mapping from attending ID to FTE
  const attendingFteMap = useMemo(() => {
    if (!neuroStaffingData) return {};
    const map = {};
    neuroStaffingData.forEach(item => {
      map[item.attending_code] = item.fte;
    });
    return map;
  }, [neuroStaffingData]);

  // Aggregate shiftByAttendingData by attending_id. Previously tracked moonlight separately
  // so we could show "with moonlight" vs "ex moonlight" multipliers, but under the new comp
  // model (after-hours reads earn $/RVU, not a separate shift stipend) there's no reason to
  // split — a rad's productivity is total wRVU / expected regardless of when they read.
  const aggregatedData = useMemo(() => {
    const byAttending = {};
    shiftByAttendingData?.forEach(item => {
      const attId = item.attending_id;
      if (!byAttending[attId]) {
        byAttending[attId] = {
          attending_id: attId,
          attending_name: attendingNameMap[attId] || attId,
          total_exams: 0,
          total_rvu: 0,
        };
      }
      byAttending[attId].total_exams += item.exam_count || 0;
      byAttending[attId].total_rvu += item.total_rvu || 0;
    });
    return byAttending;
  }, [shiftByAttendingData, attendingNameMap]);

  // Combine with neuroStaffingData to get expected RVUs
  const tableData = useMemo(() => {
    const results = [];
    const expectedRvuMap = {};
    neuroStaffingData?.forEach(item => {
      const use70th = benchmark === '70th' && item.expected_rvu_70th != null;
      expectedRvuMap[item.attending_code] = use70th ? item.expected_rvu_70th : (item.expected_rvu || 0);
    });

    Object.values(aggregatedData).forEach(item => {
      const expectedRvu = expectedRvuMap[item.attending_id] || 0;
      const multiplier = expectedRvu > 0 ? item.total_rvu / expectedRvu : 0;
      const fte = attendingFteMap[item.attending_id];
      results.push({
        ...item,
        expected_rvu: expectedRvu,
        multiplier,
        fte,
      });
    });
    return results;
  }, [aggregatedData, neuroStaffingData, attendingFteMap, benchmark]);

  // Sort data
  const sortedData = useMemo(() => {
    return [...tableData].sort((a, b) => {
      let aVal, bVal;
      switch (sortField) {
        case 'attending_name': aVal = a.attending_name; bVal = b.attending_name; break;
        case 'total_exams':    aVal = a.total_exams;    bVal = b.total_exams;    break;
        case 'total_rvu':      aVal = a.total_rvu;      bVal = b.total_rvu;      break;
        case 'expected_rvu':   aVal = a.expected_rvu;   bVal = b.expected_rvu;   break;
        case 'multiplier':     aVal = a.multiplier;     bVal = b.multiplier;     break;
        default:               aVal = a.attending_name; bVal = b.attending_name;
      }
      if (typeof aVal === 'string') {
        return sortDirection === 'asc' ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
      }
      return sortDirection === 'asc' ? aVal - bVal : bVal - aVal;
    });
  }, [tableData, sortField, sortDirection]);

  // Calculate totals
  const totals = useMemo(() => {
    return tableData.reduce((acc, item) => ({
      total_exams: acc.total_exams + item.total_exams,
      total_rvu: acc.total_rvu + item.total_rvu,
      expected_rvu: acc.expected_rvu + item.expected_rvu,
    }), { total_exams: 0, total_rvu: 0, expected_rvu: 0 });
  }, [tableData]);

  totals.multiplier = totals.expected_rvu > 0 ? totals.total_rvu / totals.expected_rvu : 0;

  const handleSort = (field) => {
    if (sortField === field) {
      setSortDirection(sortDirection === 'asc' ? 'desc' : 'asc');
    } else {
      setSortField(field);
      setSortDirection('desc');
    }
  };

  const getSortIndicator = (field) => {
    if (sortField !== field) return '';
    return sortDirection === 'asc' ? ' ↑' : ' ↓';
  };

  const getMultiplierColor = (multiplier) => {
    if (multiplier >= 1.0) return 'text-green-600';
    if (multiplier >= 0.9) return 'text-yellow-600';
    return 'text-red-600';
  };

  if (!tableData || tableData.length === 0) {
    return (
      <div className="bg-white rounded-lg shadow p-6">
        <p className="text-gray-500 text-center py-8">No data available</p>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-lg shadow overflow-hidden">
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-gray-200">
          <thead className="bg-gray-50">
            <tr>
              <th
                onClick={() => handleSort('attending_name')}
                className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100"
              >
                Attending{getSortIndicator('attending_name')}
              </th>
              <th
                onClick={() => handleSort('total_exams')}
                className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100"
              >
                Total Exams{getSortIndicator('total_exams')}
              </th>
              <th
                onClick={() => handleSort('total_rvu')}
                className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100"
              >
                Total RVUs{getSortIndicator('total_rvu')}
              </th>
              <th
                onClick={() => handleSort('expected_rvu')}
                className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100"
              >
                Expected RVUs{getSortIndicator('expected_rvu')}
              </th>
              <th
                onClick={() => handleSort('multiplier')}
                className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100"
              >
                Multiplier{getSortIndicator('multiplier')}
              </th>
            </tr>
          </thead>
          <tbody className="bg-white divide-y divide-gray-200">
            {/* Totals row at top */}
            <tr className="bg-gray-100 font-medium">
              <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-900">Total</td>
              <td className="px-4 py-3 whitespace-nowrap text-sm text-right text-gray-900">
                {totals.total_exams.toLocaleString()}
              </td>
              <td className="px-4 py-3 whitespace-nowrap text-sm text-right text-gray-900">
                {totals.total_rvu.toFixed(0)}
              </td>
              <td className="px-4 py-3 whitespace-nowrap text-sm text-right text-gray-900">
                {totals.expected_rvu.toFixed(0)}
              </td>
              <td className={`px-4 py-3 whitespace-nowrap text-sm text-right ${getMultiplierColor(totals.multiplier)}`}>
                {totals.multiplier.toFixed(2)}x
              </td>
            </tr>
            {/* Individual attending rows */}
            {sortedData.map((row) => (
              <tr key={row.attending_id} className="hover:bg-gray-50">
                <td className="px-4 py-3 whitespace-nowrap text-sm font-medium text-gray-900">
                  {row.attending_name}
                  {row.fte && <span className="text-gray-400 font-normal ml-1">({row.fte} FTE)</span>}
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-sm text-right text-gray-500">
                  {row.total_exams.toLocaleString()}
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-sm text-right font-medium text-gray-900">
                  {row.total_rvu.toFixed(0)}
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-sm text-right text-gray-500">
                  {row.expected_rvu.toFixed(0)}
                </td>
                <td className={`px-4 py-3 whitespace-nowrap text-sm text-right font-medium ${getMultiplierColor(row.multiplier)}`}>
                  {row.multiplier.toFixed(2)}x
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
