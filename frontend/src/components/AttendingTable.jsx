import { useState } from 'react';

export default function AttendingTable({ data }) {
  const [sortKey, setSortKey] = useState('total_rvu');
  const [sortOrder, setSortOrder] = useState('desc');

  if (!data || data.length === 0) {
    return (
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-semibold text-gray-900 mb-4">RVUs by Attending</h3>
        <p className="text-gray-500 text-center py-8">No data available</p>
      </div>
    );
  }

  const handleSort = (key) => {
    if (sortKey === key) {
      setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc');
    } else {
      setSortKey(key);
      setSortOrder('desc');
    }
  };

  const sortedData = [...data].sort((a, b) => {
    const aVal = a[sortKey] || 0;
    const bVal = b[sortKey] || 0;
    return sortOrder === 'asc' ? aVal - bVal : bVal - aVal;
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

  return (
    <div className="bg-white rounded-lg shadow overflow-hidden">
      <h3 className="text-lg font-semibold text-gray-900 p-6 pb-0">RVUs by Attending</h3>
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-gray-200">
          <thead className="bg-gray-50">
            <tr>
              <SortHeader label="Attending" sortKey="attending_id" />
              <SortHeader label="Division" sortKey="division" />
              <SortHeader label="Exams" sortKey="exam_count" />
              <SortHeader label="Total RVU" sortKey="total_rvu" />
              <SortHeader label="Evening ER RVU" sortKey="evening_er_rvu" />
              <SortHeader label="Flex RVU" sortKey="flex_rvu" />
            </tr>
          </thead>
          <tbody className="bg-white divide-y divide-gray-200">
            {sortedData.map((row, index) => (
              <tr key={index} className="hover:bg-gray-50">
                <td className="px-4 py-3 whitespace-nowrap text-sm font-medium text-gray-900">
                  {row.attending_id}
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-500">
                  {row.division || '-'}
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-500">
                  {row.exam_count?.toLocaleString()}
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-900 font-medium">
                  {row.total_rvu?.toFixed(2)}
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-500">
                  {row.evening_er_rvu?.toFixed(2)}
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-500">
                  {row.flex_rvu?.toFixed(2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
