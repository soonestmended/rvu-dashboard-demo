import { useMemo, useState } from 'react';

export default function ShiftDistributionTable({ attendingShiftData, neuroConfig, neuroStaffingData }) {
  const [sortField, setSortField] = useState('attending_id');
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

  // Helper to get attending display name with FTE
  const getAttendingName = (attendingId) => {
    const name = attendingNameMap[attendingId] || attendingId;
    const fte = attendingFteMap[attendingId];
    if (fte) {
      return { name, fte };
    }
    return { name, fte: null };
  };

  // Pivot the data: create a matrix of attendings x shifts
  const { attendings, allShifts, pivotData, columnTotals } = useMemo(() => {
    // Get unique attendings and shifts
    const attendingSet = new Set();
    const shiftSet = new Set();

    attendingShiftData?.forEach(item => {
      attendingSet.add(item.attending_id);
      shiftSet.add(item.shift_name);
    });

    const attendings = Array.from(attendingSet).sort();
    const allShifts = Array.from(shiftSet).sort();

    // Create pivot data
    const pivotData = {};
    const columnTotals = {};

    // Initialize
    attendings.forEach(att => {
      pivotData[att] = { total_rvu: 0, exam_count: 0 };
      allShifts.forEach(shift => {
        pivotData[att][shift] = { rvu: 0, exams: 0 };
      });
    });

    allShifts.forEach(shift => {
      columnTotals[shift] = { rvu: 0, exams: 0 };
    });

    // Fill in data
    attendingShiftData?.forEach(item => {
      const att = item.attending_id;
      const shift = item.shift_name;
      const rvu = item.total_rvu || 0;
      const exams = item.exam_count || 0;

      if (pivotData[att]) {
        pivotData[att][shift] = { rvu, exams };
        pivotData[att].total_rvu += rvu;
        pivotData[att].exam_count += exams;
      }

      if (columnTotals[shift] !== undefined) {
        columnTotals[shift].rvu += rvu;
        columnTotals[shift].exams += exams;
      }
    });

    return { attendings, allShifts, pivotData, columnTotals };
  }, [attendingShiftData]);

  // Sort attendings
  const sortedAttendings = useMemo(() => {
    return [...attendings].sort((a, b) => {
      let aVal, bVal;

      if (sortField === 'attending_id') {
        aVal = a;
        bVal = b;
      } else if (sortField === 'total_rvu') {
        aVal = pivotData[a]?.total_rvu || 0;
        bVal = pivotData[b]?.total_rvu || 0;
      } else if (sortField === 'exam_count') {
        aVal = pivotData[a]?.exam_count || 0;
        bVal = pivotData[b]?.exam_count || 0;
      } else {
        // Sort by shift column
        aVal = pivotData[a]?.[sortField]?.rvu || 0;
        bVal = pivotData[b]?.[sortField]?.rvu || 0;
      }

      if (sortDirection === 'asc') {
        return aVal > bVal ? 1 : aVal < bVal ? -1 : 0;
      } else {
        return aVal < bVal ? 1 : aVal > bVal ? -1 : 0;
      }
    });
  }, [attendings, pivotData, sortField, sortDirection]);

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

  const getShiftColor = (shiftName) => {
    const colors = {
      'Inpatient A': 'bg-blue-100 text-blue-800',
      'Inpatient B': 'bg-green-100 text-green-800',
      'Outpatient A': 'bg-purple-100 text-purple-800',
      'Outpatient B': 'bg-purple-100 text-purple-800',
      'Flex': 'bg-yellow-100 text-yellow-800',
      'Flex/Nights': 'bg-orange-100 text-orange-800',
      'Call': 'bg-red-100 text-red-800',
      'Moonlight': 'bg-gray-100 text-gray-800',
      'Weekend Call': 'bg-red-100 text-red-800',
      'Unassigned': 'bg-gray-50 text-gray-500',
    };
    return colors[shiftName] || 'bg-gray-100 text-gray-800';
  };

  return (
    <div className="bg-white rounded-lg shadow overflow-hidden">
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-gray-200">
          <thead className="bg-gray-50">
            <tr>
              <th
                onClick={() => handleSort('attending_id')}
                className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100 sticky left-0 bg-gray-50 z-10"
              >
                Attending{getSortIndicator('attending_id')}
              </th>
              {allShifts.map(shift => (
                <th
                  key={shift}
                  className={`px-3 py-3 text-center text-xs font-medium uppercase tracking-wider ${getShiftColor(shift).split(' ')[0]}`}
                >
                  {shift}
                </th>
              ))}
              <th
                onClick={() => handleSort('total_rvu')}
                className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100"
              >
                Total RVU{getSortIndicator('total_rvu')}
              </th>
              <th
                onClick={() => handleSort('exam_count')}
                className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100"
              >
                Exams{getSortIndicator('exam_count')}
              </th>
            </tr>
          </thead>
          <tbody className="bg-white divide-y divide-gray-200">
            {sortedAttendings.map(attending => {
              const { name: attendingName, fte } = getAttendingName(attending);
              return (
                <tr key={attending} className="hover:bg-gray-50">
                  <td className="px-4 py-3 whitespace-nowrap text-sm font-medium text-gray-900 sticky left-0 bg-white z-10">
                    {attendingName}
                    {fte && <span className="text-gray-400 font-normal ml-1">({fte} FTE)</span>}
                  </td>
                {allShifts.map(shift => {
                  const cell = pivotData[attending]?.[shift] || { rvu: 0, exams: 0 };
                  return (
                    <td key={shift} className="px-3 py-3 whitespace-nowrap text-sm text-center">
                      {cell.rvu > 0 ? (
                        <div>
                          <div className="font-medium text-gray-900">{cell.rvu.toFixed(0)}</div>
                          <div className="text-xs text-gray-500">{cell.exams} exams</div>
                        </div>
                      ) : (
                        <span className="text-gray-300">-</span>
                      )}
                    </td>
                  );
                })}
                <td className="px-4 py-3 whitespace-nowrap text-sm text-right font-medium text-gray-900">
                  {(pivotData[attending]?.total_rvu || 0).toFixed(0)}
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-sm text-right text-gray-500">
                  {pivotData[attending]?.exam_count || 0}
                </td>
              </tr>
            );
            })}
            {/* Totals row */}
            <tr className="bg-gray-50 font-medium">
              <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-900 sticky left-0 bg-gray-50 z-10">
                Total
              </td>
              {allShifts.map(shift => {
                const total = columnTotals[shift] || { rvu: 0, exams: 0 };
                return (
                  <td key={shift} className="px-3 py-3 whitespace-nowrap text-sm text-center text-gray-900">
                    {total.rvu > 0 ? (
                      <div>
                        <div>{total.rvu.toFixed(0)}</div>
                        <div className="text-xs text-gray-500">{total.exams} exams</div>
                      </div>
                    ) : (
                      <span className="text-gray-300">-</span>
                    )}
                  </td>
                );
              })}
              <td className="px-4 py-3 whitespace-nowrap text-sm text-right text-gray-900">
                {Object.values(columnTotals).reduce((sum, col) => sum + col.rvu, 0).toFixed(0)}
              </td>
              <td className="px-4 py-3 whitespace-nowrap text-sm text-right text-gray-900">
                {Object.values(columnTotals).reduce((sum, col) => sum + col.exams, 0)}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}
