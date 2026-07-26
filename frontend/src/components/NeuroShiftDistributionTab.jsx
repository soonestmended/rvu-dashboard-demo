import { useMemo, useState } from 'react';

export default function NeuroShiftDistributionTab({ scheduleShiftCounts, shiftByAttendingData, neuroConfig, neuroStaffingData }) {
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

  // Get all unique shift types from schedule
  const allShiftTypes = useMemo(() => {
    const shiftSet = new Set();
    scheduleShiftCounts?.forEach(item => {
      shiftSet.add(item.shift_name);
    });
    return Array.from(shiftSet).sort();
  }, [scheduleShiftCounts]);

  // Create pivot data: attending -> shift -> count
  const { pivotData, attendings, columnTotals } = useMemo(() => {
    const attendingSet = new Set();
    const pivotData = {};
    const columnTotals = {};

    // Initialize column totals
    allShiftTypes.forEach(shift => {
      columnTotals[shift] = 0;
    });

    // Process schedule shift counts
    scheduleShiftCounts?.forEach(item => {
      const attId = item.attending_id;
      attendingSet.add(attId);

      if (!pivotData[attId]) {
        pivotData[attId] = { total_shifts: 0 };
        allShiftTypes.forEach(shift => {
          pivotData[attId][shift] = 0;
        });
      }

      pivotData[attId][item.shift_name] = item.count;
      pivotData[attId].total_shifts += item.count;
      columnTotals[item.shift_name] += item.count;
    });

    const attendings = Array.from(attendingSet);

    return { pivotData, attendings, columnTotals };
  }, [scheduleShiftCounts, allShiftTypes]);

  // Create exam counts by shift type from shiftByAttendingData
  const examData = useMemo(() => {
    const byAttendingAndShift = {};
    const byShiftTotal = {};

    shiftByAttendingData?.forEach(item => {
      const key = `${item.attending_id}:${item.shift_name}`;
      byAttendingAndShift[key] = {
        exam_count: item.exam_count || 0,
        rvu: item.total_rvu || 0,
      };

      // Aggregate by shift type for averages
      if (!byShiftTotal[item.shift_name]) {
        byShiftTotal[item.shift_name] = { exams: 0, shifts: 0 };
      }
      byShiftTotal[item.shift_name].exams += item.exam_count || 0;
    });

    return { byAttendingAndShift, byShiftTotal };
  }, [shiftByAttendingData]);

  // Sort attendings
  const sortedAttendings = useMemo(() => {
    return [...attendings].sort((a, b) => {
      let aVal, bVal;

      if (sortField === 'attending_name') {
        aVal = attendingNameMap[a] || a;
        bVal = attendingNameMap[b] || b;
        if (sortDirection === 'asc') {
          return aVal.localeCompare(bVal);
        } else {
          return bVal.localeCompare(aVal);
        }
      } else if (sortField === 'total_shifts') {
        aVal = pivotData[a]?.total_shifts || 0;
        bVal = pivotData[b]?.total_shifts || 0;
      } else {
        // Sort by shift column
        aVal = pivotData[a]?.[sortField] || 0;
        bVal = pivotData[b]?.[sortField] || 0;
      }

      if (sortDirection === 'asc') {
        return aVal - bVal;
      } else {
        return bVal - aVal;
      }
    });
  }, [attendings, pivotData, sortField, sortDirection, attendingNameMap]);

  // Calculate grand total shifts
  const grandTotalShifts = useMemo(() => {
    return Object.values(columnTotals).reduce((sum, count) => sum + count, 0);
  }, [columnTotals]);

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
      'Inpatient A': 'bg-blue-50',
      'Inpatient B': 'bg-green-50',
      'Outpatient A': 'bg-purple-50',
      'Outpatient B': 'bg-indigo-50',
      'Flex': 'bg-yellow-50',
      'Flex/Nights': 'bg-orange-50',
    };
    return colors[shiftName] || 'bg-gray-50';
  };

  const getAttendingName = (attendingId) => {
    const name = attendingNameMap[attendingId] || attendingId;
    const fte = attendingFteMap[attendingId];
    if (fte) {
      return { name, fte };
    }
    return { name, fte: null };
  };

  if (!scheduleShiftCounts || scheduleShiftCounts.length === 0) {
    return (
      <div className="bg-white rounded-lg shadow p-6">
        <p className="text-gray-500 text-center py-8">No schedule data available</p>
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
                className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100 sticky left-0 bg-gray-50 z-10"
              >
                Attending{getSortIndicator('attending_name')}
              </th>
              {allShiftTypes.map(shift => (
                <th
                  key={shift}
                  onClick={() => handleSort(shift)}
                  className={`px-3 py-3 text-center text-xs font-medium uppercase tracking-wider cursor-pointer hover:bg-gray-100 ${getShiftColor(shift)}`}
                >
                  {shift}{getSortIndicator(shift)}
                </th>
              ))}
              <th
                onClick={() => handleSort('total_shifts')}
                className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100"
              >
                Total Shifts{getSortIndicator('total_shifts')}
              </th>
            </tr>
          </thead>
          <tbody className="bg-white divide-y divide-gray-200">
            {/* Totals row at top */}
            <tr className="bg-gray-100 font-medium">
              <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-900 sticky left-0 bg-gray-100 z-10">
                Total
              </td>
              {allShiftTypes.map(shift => {
                const count = columnTotals[shift] || 0;
                const percentage = grandTotalShifts > 0 ? (count / grandTotalShifts * 100) : 0;
                const totalExams = examData.byShiftTotal[shift]?.exams || 0;
                const avgExamsPerShift = count > 0 ? (totalExams / count) : 0;

                return (
                  <td key={shift} className={`px-3 py-3 whitespace-nowrap text-sm text-center ${getShiftColor(shift)}`}>
                    {count > 0 ? (
                      <div>
                        <div className="font-medium text-gray-900">{count}</div>
                        <div className="text-xs text-gray-500">{percentage.toFixed(0)}%</div>
                        <div className="text-xs text-gray-400">{avgExamsPerShift.toFixed(1)} ex/sh</div>
                      </div>
                    ) : (
                      <span className="text-gray-300">-</span>
                    )}
                  </td>
                );
              })}
              <td className="px-4 py-3 whitespace-nowrap text-sm text-right text-gray-900">
                {grandTotalShifts}
              </td>
            </tr>
            {/* Individual attending rows */}
            {sortedAttendings.map(attending => {
              const attendingTotal = pivotData[attending]?.total_shifts || 0;
              const { name: attendingName, fte } = getAttendingName(attending);

              return (
                <tr key={attending} className="hover:bg-gray-50">
                  <td className="px-4 py-3 whitespace-nowrap text-sm font-medium text-gray-900 sticky left-0 bg-white z-10">
                    {attendingName}
                    {fte && <span className="text-gray-400 font-normal ml-1">({fte} FTE)</span>}
                  </td>
                  {allShiftTypes.map(shift => {
                    const count = pivotData[attending]?.[shift] || 0;
                    const examKey = `${attending}:${shift}`;
                    const examInfo = examData.byAttendingAndShift[examKey] || { exam_count: 0 };
                    const percentage = attendingTotal > 0 ? (count / attendingTotal * 100) : 0;
                    const examsPerShift = count > 0 ? (examInfo.exam_count / count) : 0;

                    return (
                      <td key={shift} className={`px-3 py-3 whitespace-nowrap text-sm text-center ${getShiftColor(shift)}`}>
                        {count > 0 ? (
                          <div>
                            <div className="font-medium text-gray-900">{count}</div>
                            <div className="text-xs text-gray-500">{percentage.toFixed(0)}%</div>
                            <div className="text-xs text-gray-400">{examsPerShift.toFixed(1)} ex/sh</div>
                          </div>
                        ) : (
                          <span className="text-gray-300">-</span>
                        )}
                      </td>
                    );
                  })}
                  <td className="px-4 py-3 whitespace-nowrap text-sm text-right font-medium text-gray-900">
                    {attendingTotal}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
