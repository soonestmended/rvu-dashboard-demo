export default function Filters({
  divisions,
  locations,
  attendings,
  selectedDivisions,
  selectedLocations,
  selectedAttendings,
  selectedPatientTypes,
  selectedCategories,
  showLocations,
  showAttendings,
  onDivisionChange,
  onLocationChange,
  onAttendingChange,
  onPatientTypeChange,
  onCategoryChange,
  onShowLocationsChange,
  onShowAttendingsChange,
  hideDivisions = false,
  activeTab = 'department'
}) {
  const patientTypes = ['ER', 'INPATIENT', 'OUTPATIENT'];
  const examCategories = [
    { key: 'WEEKDAY', label: 'Weekday' },
    { key: 'CALL', label: 'Call' },
    { key: 'WEEKDAY_EVENING_ER', label: 'Weekday Evening ER' },
    { key: 'WEEKEND_EVENING_ER', label: 'Weekend Evening ER' },
    { key: 'AFTER_HOURS', label: 'After-Hours' },
  ];

  const allDivisionsSelected = selectedDivisions.length === divisions.length;

  const handleDivisionAllClick = () => {
    onDivisionChange(divisions);
  };

  const handleDivisionClick = (division) => {
    if (allDivisionsSelected) {
      onDivisionChange([division]);
    } else {
      const newSelection = selectedDivisions.includes(division)
        ? selectedDivisions.filter(d => d !== division)
        : [...selectedDivisions, division];
      onDivisionChange(newSelection);
    }
  };

  const handleLocationToggle = (location) => {
    const newSelection = selectedLocations.includes(location)
      ? selectedLocations.filter(l => l !== location)
      : [...selectedLocations, location];
    onLocationChange(newSelection);
  };

  // Filter attendings based on active tab: Neuro tab shows only Neuro attendings
  const effectiveDivisions = activeTab === 'neuro' ? ['NEURO'] : selectedDivisions;
  const filteredAttendings = attendings.filter(
    a => effectiveDivisions.includes(a.division)
  );

  const allAttendingsSelected = selectedAttendings.length === 0 ||
    selectedAttendings.length === filteredAttendings.length;

  const handleAttendingAllClick = () => {
    onAttendingChange([]);
  };

  const handleAttendingClick = (attendingId) => {
    if (allAttendingsSelected) {
      onAttendingChange([attendingId]);
    } else {
      const newSelection = selectedAttendings.includes(attendingId)
        ? selectedAttendings.filter(a => a !== attendingId)
        : [...selectedAttendings, attendingId];
      onAttendingChange(newSelection);
    }
  };

  // Patient-type selection — same smart multi-select as Case Type / Divisions:
  //   All active by default; click one while All is on -> only that; click more -> add;
  //   deselect the last -> revert to All.
  const allPatientTypesSelected = selectedPatientTypes.length === patientTypes.length;

  const handlePatientTypeClick = (pt) => {
    if (allPatientTypesSelected) {
      onPatientTypeChange([pt]);
      return;
    }
    if (selectedPatientTypes.includes(pt)) {
      const remaining = selectedPatientTypes.filter(t => t !== pt);
      onPatientTypeChange(remaining.length === 0 ? [...patientTypes] : remaining);
    } else {
      onPatientTypeChange([...selectedPatientTypes, pt]);
    }
  };

  const handlePatientTypeAllClick = () => {
    onPatientTypeChange(patientTypes);
  };

  // Case-type selection — mirrors the Divisions buttons above for consistency:
  //   - "All" active by default (everything shown, no filter).
  //   - Click a type while All is active  -> narrow to ONLY that type.
  //   - Click more types                  -> add them to the selection.
  //   - Deselect the last remaining type  -> revert to All (not "show nothing").
  //   - Click All                         -> back to everything.
  const allCategoriesSelected = selectedCategories.length === examCategories.length;

  const handleCategoryClick = (cat) => {
    if (allCategoriesSelected) {
      // Coming from "everything" — a click means "show only this one".
      onCategoryChange([cat]);
      return;
    }
    if (selectedCategories.includes(cat)) {
      const remaining = selectedCategories.filter(c => c !== cat);
      // Turning off the last active type reverts to All rather than an empty (show-nothing)
      // selection, which is what a user expects from "I'm done filtering".
      onCategoryChange(remaining.length === 0 ? examCategories.map(c => c.key) : remaining);
    } else {
      onCategoryChange([...selectedCategories, cat]);
    }
  };

  const handleCategoryAllClick = () => {
    onCategoryChange(examCategories.map(c => c.key));
  };

  return (
    <div className="bg-white p-4 rounded-lg shadow space-y-4">
      {!hideDivisions && (
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">Divisions</label>
          <div className="flex flex-wrap gap-2">
            <button
              onClick={handleDivisionAllClick}
              className={`px-3 py-1 rounded-full text-sm font-medium ${
                allDivisionsSelected
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-200 text-gray-700 hover:bg-gray-300'
              }`}
            >
              All
            </button>
            {divisions.map(division => (
              <button
                key={division}
                onClick={() => handleDivisionClick(division)}
                className={`px-3 py-1 rounded-full text-sm font-medium ${
                  !allDivisionsSelected && selectedDivisions.includes(division)
                    ? 'bg-blue-600 text-white'
                    : 'bg-gray-200 text-gray-700 hover:bg-gray-300'
                }`}
              >
                {division}
              </button>
            ))}
          </div>
        </div>
      )}

      <div>
        <label className="block text-sm font-medium text-gray-700 mb-2">Patient Type</label>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={handlePatientTypeAllClick}
            className={`px-3 py-1 rounded-full text-sm font-medium ${
              allPatientTypesSelected
                ? 'bg-blue-600 text-white'
                : 'bg-gray-200 text-gray-700 hover:bg-gray-300'
            }`}
          >
            All
          </button>
          {patientTypes.map(pt => (
            <button
              key={pt}
              onClick={() => handlePatientTypeClick(pt)}
              className={`px-3 py-1 rounded-full text-sm font-medium ${
                !allPatientTypesSelected && selectedPatientTypes.includes(pt)
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-200 text-gray-700 hover:bg-gray-300'
              }`}
            >
              {pt}
            </button>
          ))}
        </div>
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700 mb-2">Case Type</label>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={handleCategoryAllClick}
            className={`px-3 py-1 rounded-full text-sm font-medium ${
              allCategoriesSelected
                ? 'bg-blue-600 text-white'
                : 'bg-gray-200 text-gray-700 hover:bg-gray-300'
            }`}
          >
            All
          </button>
          {examCategories.map(cat => (
            <button
              key={cat.key}
              onClick={() => handleCategoryClick(cat.key)}
              className={`px-3 py-1 rounded-full text-sm font-medium ${
                !allCategoriesSelected && selectedCategories.includes(cat.key)
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-200 text-gray-700 hover:bg-gray-300'
              }`}
            >
              {cat.label}
            </button>
          ))}
        </div>
      </div>

      <div className="flex gap-6 items-center flex-wrap">
        {!hideDivisions && (
          <>
            <button
              onClick={() => onShowAttendingsChange(!showAttendings)}
              className="text-sm text-blue-600 hover:text-blue-800 underline"
            >
              {showAttendings ? 'Hide Attendings' : 'Show Attendings'}
            </button>

            <button
              onClick={() => onShowLocationsChange(!showLocations)}
              className="text-sm text-blue-600 hover:text-blue-800 underline"
            >
              {showLocations ? 'Hide Locations' : 'Show Locations'}
            </button>
          </>
        )}
      </div>

      {showAttendings && !hideDivisions && (
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">
            Attendings {!hideDivisions && selectedDivisions.length > 0 && selectedDivisions.length < divisions.length && '(filtered by selected divisions)'}
          </label>
          <div className="flex flex-wrap gap-2">
            <button
              onClick={handleAttendingAllClick}
              className={`px-3 py-1 rounded-full text-sm font-medium ${
                allAttendingsSelected
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-200 text-gray-700 hover:bg-gray-300'
              }`}
            >
              All
            </button>
            {filteredAttendings.map(attending => (
              <button
                key={attending.attending_id}
                onClick={() => handleAttendingClick(attending.attending_id)}
                className={`px-3 py-1 rounded-full text-sm font-medium ${
                  !allAttendingsSelected && selectedAttendings.includes(attending.attending_id)
                    ? 'bg-blue-600 text-white'
                    : 'bg-gray-200 text-gray-700 hover:bg-gray-300'
                }`}
              >
                {attending.attending_id}
                <span className="text-gray-300 ml-1">({attending.division})</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {showLocations && (
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">Locations</label>
          <div className="flex flex-wrap gap-2">
            {locations.map(location => (
              <button
                key={location}
                onClick={() => handleLocationToggle(location)}
                className={`px-3 py-1 rounded-full text-sm font-medium ${
                  selectedLocations.includes(location)
                    ? 'bg-blue-600 text-white'
                    : 'bg-gray-200 text-gray-700 hover:bg-gray-300'
                }`}
              >
                {location}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
