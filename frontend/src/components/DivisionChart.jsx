import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from 'recharts';

export default function DivisionChart({ data }) {
  if (!data || data.length === 0) {
    return (
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-semibold text-gray-900 mb-4">RVUs by Division</h3>
        <p className="text-gray-500 text-center py-8">No data available</p>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-lg shadow p-6">
      <h3 className="text-lg font-semibold text-gray-900 mb-4">RVUs by Division</h3>
      <ResponsiveContainer width="100%" height={300}>
        <BarChart data={data} margin={{ top: 20, right: 30, left: 20, bottom: 60 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis
            dataKey="division"
            angle={-45}
            textAnchor="end"
            height={80}
            interval={0}
            tick={{ fontSize: 12 }}
          />
          <YAxis tick={{ fontSize: 12 }} />
          <Tooltip
            formatter={(value) => [value?.toFixed(2), '']}
            labelFormatter={(label) => `Division: ${label}`}
          />
          <Legend />
          <Bar dataKey="total_rvu" name="Total RVU" fill="#3b82f6" />
          <Bar dataKey="evening_er_rvu" name="Evening ER RVU" fill="#f59e0b" />
          <Bar dataKey="flex_rvu" name="Flex RVU" fill="#10b981" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
