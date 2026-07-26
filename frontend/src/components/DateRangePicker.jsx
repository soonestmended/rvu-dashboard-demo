import { useEffect, useRef, useState } from 'react';
import { DayPicker } from 'react-day-picker';
import 'react-day-picker/dist/style.css';

// ISO ↔ Date helpers. Treat ISO as calendar dates (no time), and build Date objects in local
// time so DayPicker highlights the right cell regardless of the user's timezone. Using
// `new Date('2026-07-15')` would parse as UTC midnight and shift back a day west of UTC.
function isoToDate(iso) {
  if (!iso) return undefined;
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d);
}
function dateToIso(dt) {
  if (!dt) return '';
  const y = dt.getFullYear();
  const m = String(dt.getMonth() + 1).padStart(2, '0');
  const d = String(dt.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}
function formatDisplay(iso) {
  if (!iso) return 'Pick a date';
  const dt = isoToDate(iso);
  return dt.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}

// Preset helpers (unchanged from the native-picker version — the preset buttons still work
// the same way regardless of which picker we use for manual selection).
function startOfNthMonthBack(iso, n) {
  const [y, m] = iso.split('-').map(Number);
  let newY = y;
  let newM = m - n;
  while (newM <= 0) { newM += 12; newY -= 1; }
  return `${String(newY).padStart(4, '0')}-${String(newM).padStart(2, '0')}-01`;
}
function fiscalYearStart(iso) {
  const [y, m] = iso.split('-').map(Number);
  const fyYear = m >= 7 ? y : y - 1;
  return `${fyYear}-07-01`;
}
function calendarYearStart(iso) {
  return `${iso.slice(0, 4)}-01-01`;
}


function DateField({ label, value, onCommit, buttonRef, isOpen, onToggle }) {
  return (
    <div className="relative">
      <label className="block text-sm font-medium text-gray-700 mb-1">{label}</label>
      <button
        ref={buttonRef}
        type="button"
        onClick={onToggle}
        className={
          "block w-full px-3 py-2 border rounded-md shadow-sm text-left text-sm " +
          "focus:outline-none focus:ring-blue-500 focus:border-blue-500 " +
          (isOpen ? "border-blue-500 ring-1 ring-blue-500 " : "border-gray-300 ") +
          "bg-white hover:border-gray-400"
        }
      >
        {formatDisplay(value)}
      </button>
    </div>
  );
}


export default function DateRangePicker({
  startDate, endDate,
  onStartDateChange, onEndDateChange,
  dateField, onDateFieldChange,
  maxDate,
}) {
  // Which field's popover is open: 'start' | 'end' | null. Clicking the same button again closes.
  const [openField, setOpenField] = useState(null);
  const startBtn = useRef(null);
  const endBtn = useRef(null);
  const popoverRef = useRef(null);

  // Close on outside click. We check the click target isn't in either the popover or the
  // button that opened it — otherwise clicking the same button again would open-and-immediately-
  // close instead of toggling as expected.
  useEffect(() => {
    if (!openField) return;
    function onDocClick(e) {
      if (popoverRef.current?.contains(e.target)) return;
      if (startBtn.current?.contains(e.target)) return;
      if (endBtn.current?.contains(e.target)) return;
      setOpenField(null);
    }
    function onEsc(e) { if (e.key === 'Escape') setOpenField(null); }
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onEsc);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onEsc);
    };
  }, [openField]);

  // Preset range buttons. Anchor at maxDate (most recent data) or today as fallback.
  const anchor = maxDate || dateToIso(new Date());
  const applyRange = (start, end) => {
    if (start !== startDate) onStartDateChange(start);
    if (end !== endDate) onEndDateChange(end);
    setOpenField(null);
  };
  const presets = [
    { label: 'Current FY', start: fiscalYearStart(anchor), end: anchor },
    { label: 'YTD', start: calendarYearStart(anchor), end: anchor },
    { label: 'Last 3 months', start: startOfNthMonthBack(anchor, 3), end: anchor },
  ];

  // Handle a day being clicked in the popover. mode='single' fires onSelect with the picked
  // Date (or undefined if the user clicks the currently-selected day again, which react-day-
  // picker treats as a de-select — we ignore that so the field always has a value).
  const handleSelect = (which) => (picked) => {
    if (!picked) return;
    const iso = dateToIso(picked);
    if (which === 'start' && iso !== startDate) onStartDateChange(iso);
    if (which === 'end' && iso !== endDate) onEndDateChange(iso);
    setOpenField(null);
  };

  const activeIso = openField === 'start' ? startDate : openField === 'end' ? endDate : '';
  const activeSelected = activeIso ? isoToDate(activeIso) : undefined;

  // Disable invalid dates in the picker so start ≤ end is enforced visually. Also cap at
  // maxDate (the ingest cutoff) so users can't pick past the last-loaded data date, which
  // would just return empty results.
  const disabledMatchers = [];
  if (openField === 'start' && endDate) {
    disabledMatchers.push({ after: isoToDate(endDate) });
  }
  if (openField === 'end' && startDate) {
    disabledMatchers.push({ before: isoToDate(startDate) });
  }
  if (maxDate) {
    disabledMatchers.push({ after: isoToDate(maxDate) });
  }

  return (
    <div className="flex gap-4 items-end flex-wrap relative">
      <DateField
        label="Start Date"
        value={startDate}
        buttonRef={startBtn}
        isOpen={openField === 'start'}
        onToggle={() => setOpenField(openField === 'start' ? null : 'start')}
      />
      <DateField
        label="End Date"
        value={endDate}
        buttonRef={endBtn}
        isOpen={openField === 'end'}
        onToggle={() => setOpenField(openField === 'end' ? null : 'end')}
      />
      {onDateFieldChange && (
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Date Based On</label>
          <select
            value={dateField || 'exam_completed_date'}
            onChange={(e) => onDateFieldChange(e.target.value)}
            className="block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm text-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500"
          >
            <option value="exam_completed_date">Exam Completed</option>
            <option value="report_finalized_date">Report Finalized</option>
          </select>
        </div>
      )}
      <div className="flex gap-1.5 pb-0.5">
        {presets.map(p => (
          <button
            key={p.label}
            type="button"
            onClick={() => applyRange(p.start, p.end)}
            className="px-2.5 py-1.5 text-xs font-medium rounded border border-gray-300 bg-gray-50 text-gray-700 hover:bg-gray-100 hover:border-gray-400"
            title={`${p.start} → ${p.end}`}
          >
            {p.label}
          </button>
        ))}
      </div>

      {openField && (
        <div
          ref={popoverRef}
          className="absolute z-50 top-full left-0 mt-1 bg-white border border-gray-300 rounded-md shadow-lg p-2"
        >
          <DayPicker
            mode="single"
            selected={activeSelected}
            defaultMonth={activeSelected || (maxDate ? isoToDate(maxDate) : new Date())}
            onSelect={handleSelect(openField)}
            disabled={disabledMatchers}
            showOutsideDays
            weekStartsOn={0}
          />
        </div>
      )}
    </div>
  );
}
