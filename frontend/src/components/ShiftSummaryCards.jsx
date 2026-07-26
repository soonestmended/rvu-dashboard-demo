import { useMemo } from 'react';

export default function ShiftSummaryCards({ shiftData, totalRvu }) {
  // Create a summary map by shift name
  const shiftSummary = useMemo(() => {
    const summary = {};
    shiftData?.forEach(item => {
      if (!summary[item.shift_name]) {
        summary[item.shift_name] = { rvu: 0, exams: 0 };
      }
      summary[item.shift_name].rvu += item.total_rvu || 0;
      summary[item.shift_name].exams += item.exam_count || 0;
    });
    return summary;
  }, [shiftData]);

  const shiftNames = Object.keys(shiftSummary).sort();

  // Color mapping for different shifts
  const getShiftColor = (shiftName) => {
    const colors = {
      'Inpatient A': 'bg-blue-500',
      'Inpatient B': 'bg-green-500',
      'Outpatient A': 'bg-purple-500',
      'Outpatient B': 'bg-purple-500',
      'Flex': 'bg-yellow-500',
      'Flex/Nights': 'bg-orange-500',
      'Call': 'bg-red-500',
      'Moonlight': 'bg-gray-500',
      'Weekend Call': 'bg-red-600',
      'Unassigned': 'bg-gray-400',
    };
    return colors[shiftName] || 'bg-gray-500';
  };

  const getShiftLabel = (shiftName) => {
    const labels = {
      'Inpatient A': 'Inpt A',
      'Inpatient B': 'Inpt B',
      'Outpatient A': 'Outpt A',
      'Outpatient B': 'Outpt B',
      'Flex/Nights': 'Flex/Nts',
      'Weekend Call': 'Wknd Call',
    };
    return labels[shiftName] || shiftName;
  };

  const getPercentage = (rvu) => {
    if (!totalRvu || totalRvu === 0) return 0;
    return ((rvu / totalRvu) * 100).toFixed(1);
  };

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
      {shiftNames.map(shiftName => (
        <div key={shiftName} className="bg-white rounded-lg shadow p-4">
          <div className={`w-10 h-10 ${getShiftColor(shiftName)} rounded-lg flex items-center justify-center mb-2`}>
            <svg className="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
            </svg>
          </div>
          <p className="text-lg font-bold text-gray-900">{shiftSummary[shiftName].rvu.toFixed(0)}</p>
          <p className="text-xs text-gray-500">{getShiftLabel(shiftName)}</p>
          <p className="text-xs text-gray-400 mt-1">{getPercentage(shiftSummary[shiftName].rvu)}%</p>
        </div>
      ))}
    </div>
  );
}
