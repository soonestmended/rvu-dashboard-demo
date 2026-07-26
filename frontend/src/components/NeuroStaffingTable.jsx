import { useState } from 'react';

export default function NeuroStaffingTable({ data, benchmark = '65th', onBenchmarkChange }) {
  const [sortKey, setSortKey] = useState('attending_name');
  const [sortOrder, setSortOrder] = useState('asc');

  if (!data || data.length === 0) {
    return (
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-semibold text-gray-900 mb-4">Neuro Division Staffing</h3>
        <p className="text-gray-500 text-center py-8">No data available</p>
      </div>
    );
  }

  const has70th = data.some(row => row.expected_rvu_70th != null);
  const use70th = benchmark === '70th' && has70th;

  const handleSort = (key) => {
    if (sortKey === key) {
      setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc');
    } else {
      setSortKey(key);
      setSortOrder('asc');
    }
  };

  const sortedData = [...data].sort((a, b) => {
    let aVal = a[sortKey];
    let bVal = b[sortKey];

    // Handle numeric values
    if (typeof aVal === 'number') {
      return sortOrder === 'asc' ? aVal - bVal : bVal - aVal;
    }

    // Handle string values
    aVal = aVal || '';
    bVal = bVal || '';
    return sortOrder === 'asc'
      ? aVal.localeCompare(bVal)
      : bVal.localeCompare(aVal);
  });

  const SortHeader = ({ label, sortKey: key }) => (
    <th
      className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100"
      onClick={() => handleSort(key)}
    >
      <div className="flex items-center gap-1">
        {label}
        {sortKey === key && (
          <span className="text-gray-400">{sortOrder === 'asc' ? '↑' : '↓'}</span>
        )}
      </div>
    </th>
  );

  const getExpected = (row) => use70th ? row.expected_rvu_70th : row.expected_rvu;
  const getVariance = (row) => use70th ? row.variance_70th : row.variance;
  const getVariancePct = (row) => use70th ? row.variance_pct_70th : row.variance_pct;

  // Calculate totals
  const totals = data.reduce((acc, row) => ({
    fte: acc.fte + row.fte,
    exam_count: acc.exam_count + row.exam_count,
    actual_rvu: acc.actual_rvu + row.actual_rvu,
    expected_rvu: acc.expected_rvu + (getExpected(row) || 0),
    variance: acc.variance + (getVariance(row) || 0),
  }), { fte: 0, exam_count: 0, actual_rvu: 0, expected_rvu: 0, variance: 0 });

  totals.variance_pct = totals.expected_rvu > 0
    ? ((totals.actual_rvu / totals.expected_rvu - 1) * 100)
    : 0;

  const getVarianceColor = (variancePct) => {
    if (variancePct >= 0) return 'text-green-600';
    if (variancePct >= -5) return 'text-yellow-600';
    return 'text-red-600';
  };

  return (
    <div className="bg-white rounded-lg shadow overflow-hidden">
      <div className="flex items-center justify-between p-6 pb-0">
        <h3 className="text-lg font-semibold text-gray-900">Neuro Division Staffing</h3>
        {has70th && (
          <div className="flex items-center gap-2">
            <label className="text-sm font-medium text-gray-700">Benchmark:</label>
            <select
              value={benchmark}
              onChange={(e) => onBenchmarkChange(e.target.value)}
              className="px-3 py-1.5 border border-gray-300 rounded-md shadow-sm text-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500"
            >
              <option value="65th">65th percentile</option>
              <option value="70th">70th percentile</option>
            </select>
          </div>
        )}
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 p-6">
        <div className="bg-gray-50 rounded-lg p-4">
          <div className="text-sm text-gray-500">Total FTE</div>
          <div className="text-2xl font-bold text-gray-900">{totals.fte.toFixed(2)}</div>
        </div>
        <div className="bg-gray-50 rounded-lg p-4">
          <div className="text-sm text-gray-500">Expected RVUs ({benchmark})</div>
          <div className="text-2xl font-bold text-gray-900">{totals.expected_rvu.toFixed(0)}</div>
        </div>
        <div className="bg-gray-50 rounded-lg p-4">
          <div className="text-sm text-gray-500">Actual RVUs</div>
          <div className="text-2xl font-bold text-gray-900">{totals.actual_rvu.toFixed(0)}</div>
        </div>
        <div className={`bg-gray-50 rounded-lg p-4 ${getVarianceColor(totals.variance_pct)}`}>
          <div className="text-sm text-gray-500">Variance</div>
          <div className="text-2xl font-bold">
            {totals.variance >= 0 ? '+' : ''}{totals.variance.toFixed(0)}
            <span className="text-sm ml-1">({totals.variance_pct >= 0 ? '+' : ''}{totals.variance_pct.toFixed(1)}%)</span>
          </div>
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto px-6 pb-6">
        <table className="min-w-full divide-y divide-gray-200">
          <thead className="bg-gray-50">
            <tr>
              <SortHeader label="Attending" sortKey="attending_name" />
              <SortHeader label="FTE" sortKey="fte" />
              <SortHeader label="Exams" sortKey="exam_count" />
              <SortHeader label={`Expected (${benchmark})`} sortKey={use70th ? "expected_rvu_70th" : "expected_rvu"} />
              <SortHeader label="Actual RVU" sortKey="actual_rvu" />
              <SortHeader label="Variance" sortKey={use70th ? "variance_70th" : "variance"} />
            </tr>
          </thead>
          <tbody className="bg-white divide-y divide-gray-200">
            {sortedData.map((row) => (
              <tr key={row.attending_code} className="hover:bg-gray-50">
                <td className="px-4 py-3 whitespace-nowrap text-sm font-medium text-gray-900">
                  {row.attending_name}
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-500">
                  {row.fte.toFixed(2)}
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-500">
                  {row.exam_count?.toLocaleString()}
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-500">
                  {getExpected(row)?.toFixed(0)}
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-900 font-medium">
                  {row.actual_rvu?.toFixed(0)}
                </td>
                <td className={`px-4 py-3 whitespace-nowrap text-sm font-medium ${getVarianceColor(getVariancePct(row))}`}>
                  {getVariance(row) >= 0 ? '+' : ''}{getVariance(row)?.toFixed(0)}
                  <span className="text-xs ml-1">({getVariancePct(row) >= 0 ? '+' : ''}{getVariancePct(row)?.toFixed(1)}%)</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
