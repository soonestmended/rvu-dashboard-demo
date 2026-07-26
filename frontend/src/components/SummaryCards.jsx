export default function SummaryCards({ data }) {
  const totalRvu = data.division?.reduce((sum, d) => sum + (d.total_rvu || 0), 0) || 0;
  const totalExams = data.division?.reduce((sum, d) => sum + (d.exam_count || 0), 0) || 0;
  const avgRvuPerExam = totalExams > 0 ? totalRvu / totalExams : 0;

  const cards = [
    { label: 'Total RVUs', value: totalRvu.toFixed(2), color: 'bg-blue-500' },
    { label: 'Total Exams', value: totalExams.toLocaleString(), color: 'bg-green-500' },
    { label: 'Avg RVU/Exam', value: avgRvuPerExam.toFixed(2), color: 'bg-purple-500' },
    { label: 'Divisions', value: data.division?.length || 0, color: 'bg-orange-500' },
  ];

  return (
    <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
      {cards.map(card => (
        <div key={card.label} className="bg-white rounded-lg shadow p-4">
          <div className={`w-12 h-12 ${card.color} rounded-lg flex items-center justify-center mb-3`}>
            <svg className="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
            </svg>
          </div>
          <p className="text-2xl font-bold text-gray-900">{card.value}</p>
          <p className="text-sm text-gray-500">{card.label}</p>
        </div>
      ))}
    </div>
  );
}
