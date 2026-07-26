export default function NeuroSubTabs({ activeTab, onTabChange, role, featureFlags = {} }) {
  // Individual users see a subset; admins see everything. Pay Projection for individuals is
  // gated by feature flag (admins can toggle in Settings → individuals never see it unless
  // pay_projection_visible_to_individuals is true). Demo role sees the same tabs as admin
  // minus anything that reveals salary/$: Comp What-If, Pay Projection, Settings.
  // Order groups the three accountability views together (Productivity, Compensation
  // Review, Comp — What-If), then the planning tool, then per-attending + settings.
  const allTabs = [
    { id: 'rvu', label: 'Productivity', adminOnly: true },
    { id: 'schedule-past', label: 'Compensation Review', adminOnly: true, demoAllowed: true },
    { id: 'compensation', label: 'Comp — What-If', adminOnly: true },
    { id: 'schedule-future', label: 'Schedule Planning', adminOnly: true, demoAllowed: true },
    { id: 'my-stats', label: 'My Stats', adminOnly: false, demoAllowed: true },
    {
      id: 'pay-projection',
      label: 'Pay Projection',
      adminOnly: false,
      individualHidden: !featureFlags.pay_projection_visible_to_individuals,
    },
    { id: 'settings', label: 'Settings', adminOnly: true },
  ];
  let tabs;
  if (role === 'admin') {
    tabs = allTabs;
  } else if (role === 'demo') {
    tabs = allTabs.filter(t => t.demoAllowed);
  } else {
    tabs = allTabs.filter(t => !t.adminOnly && !t.individualHidden);
  }

  return (
    <div className="bg-white border-b border-gray-200">
      <div className="flex space-x-4">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => onTabChange(tab.id)}
            className={`
              whitespace-nowrap py-3 px-4 border-b-2 font-medium text-sm
              ${activeTab === tab.id
                ? 'border-blue-500 text-blue-600'
                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'}
            `}
          >
            {tab.label}
          </button>
        ))}
      </div>
    </div>
  );
}
