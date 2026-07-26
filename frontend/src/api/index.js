const API_BASE = '/api';

// Auth ---------------------------------------------------------------------------------
// Every auth-related fetch sets credentials: 'include' so the session cookie travels in
// both directions. The other fetches in this file default to same-origin which still
// sends cookies but is more permissive about CORS; using 'include' here matches the
// backend's `supports_credentials=True` configuration.

export async function login({ username, password }) {
  const response = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    return { ok: false, error: body.error || `login failed (HTTP ${response.status})` };
  }
  return { ok: true, user: body };
}

export async function logout() {
  await fetch(`${API_BASE}/auth/logout`, { method: 'POST', credentials: 'include' });
}

export async function fetchUsers() {
  const r = await fetch(`${API_BASE}/auth/users`, { credentials: 'include' });
  if (!r.ok) throw new Error(`fetchUsers HTTP ${r.status}`);
  return r.json();
}

export async function resetPassword(username) {
  const r = await fetch(`${API_BASE}/auth/reset-password`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) return { ok: false, error: body.error || `HTTP ${r.status}` };
  return { ok: true, ...body };
}

export async function changePassword({ old_password, new_password }) {
  const response = await fetch(`${API_BASE}/auth/change-password`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ old_password, new_password }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    return { ok: false, error: body.error || `HTTP ${response.status}` };
  }
  return { ok: true };
}

export async function fetchMe() {
  const response = await fetch(`${API_BASE}/auth/me`, { credentials: 'include' });
  if (response.status === 401) return null;
  if (!response.ok) throw new Error(`me failed: HTTP ${response.status}`);
  return response.json();
}


export async function fetchHealth() {
  const response = await fetch(`${API_BASE}/health`);
  return response.json();
}

export async function fetchDivisions() {
  const response = await fetch(`${API_BASE}/divisions`);
  return response.json();
}

export async function fetchLocations() {
  const response = await fetch(`${API_BASE}/locations`);
  return response.json();
}

export async function fetchAttendings() {
  const response = await fetch(`${API_BASE}/attendings`);
  return response.json();
}

export async function fetchDateRange(params = {}) {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}/date-range?${query}`);
  return response.json();
}

export async function fetchRvusByDivision(params) {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}/rvus/by-division?${query}`);
  return response.json();
}

export async function fetchRvusByLocation(params) {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}/rvus/by-location?${query}`);
  return response.json();
}

export async function fetchRvusByAttending(params) {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}/rvus/by-attending?${query}`);
  return response.json();
}

export async function fetchNeuroConfig() {
  const response = await fetch(`${API_BASE}/staffing/neuro-config`);
  return response.json();
}

export async function fetchNeuroStaffingMetrics(params) {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}/staffing/neuro-metrics?${query}`);
  return response.json();
}

export async function fetchNeuroSummary(params) {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}/staffing/neuro-summary?${query}`);
  return response.json();
}

export async function fetchShiftsByShift(params) {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}/shifts/by-shift?${query}`);
  return response.json();
}

export async function fetchShiftsByAttending(params) {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}/shifts/by-attending?${query}`);
  return response.json();
}

export async function fetchScheduleShiftCounts(params) {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}/schedule/shift-counts?${query}`);
  return response.json();
}

export async function fetchMoonlightCompensation(params) {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}/compensation/moonlight-monthly?${query}`);
  return response.json();
}

export async function fetchProposedCompensation(params) {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}/compensation/proposed-model?${query}`);
  return response.json();
}

export async function fetchShiftRvuAverages(params) {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}/schedule/shift-rvu-averages?${query}`);
  return response.json();
}

export async function fetchFullSchedule(params) {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}/schedule/full?${query}`);
  return response.json();
}

export async function fetchScheduleDateRange() {
  const response = await fetch(`${API_BASE}/schedule/date-range`);
  return response.json();
}

export async function fetchFeatureFlags() {
  const response = await fetch(`${API_BASE}/settings/feature-flags`, { credentials: 'include' });
  if (!response.ok) return {};
  return response.json();
}

export async function setFeatureFlags(updates) {
  const r = await fetch(`${API_BASE}/settings/feature-flags`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
  if (!r.ok) return { error: `HTTP ${r.status}` };
  return r.json();
}

export async function fetchActualProduction(params) {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}/schedule/actual-production?${query}`);
  return response.json();
}

export async function fetchMyStats(params) {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}/me/stats?${query}`);
  return response.json();
}

export async function fetchMyPayProjection(params) {
  // Drop undefined/null/'' values so we don't send literal "undefined" in the URL.
  const clean = Object.fromEntries(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
  );
  const query = new URLSearchParams(clean);
  const response = await fetch(`${API_BASE}/me/pay-projection?${query}`, { credentials: 'include' });
  return response.json();
}

export async function refreshScheduleFromSheets() {
  const response = await fetch(`${API_BASE}/schedule/refresh-from-sheets`, { method: 'POST' });
  return response.json();
}

// Streams NDJSON events from /api/schedule/generate. `onEvent` is called for each parsed line.
// Returns a promise resolving to {result?, error?} once the stream ends.
export async function publishCandidateToSheet({ candidate, tab_name, dry_run }) {
  const response = await fetch(`${API_BASE}/schedule/publish`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ candidate, tab_name, dry_run }),
  });
  return response.json();
}

export async function generateScheduleStream({ start_date, end_date }, onEvent) {
  const response = await fetch(`${API_BASE}/schedule/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/x-ndjson' },
    body: JSON.stringify({ start_date, end_date }),
  });
  if (!response.ok || !response.body) {
    throw new Error(`generate failed: HTTP ${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let final = {};
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (!line) continue;
      try {
        const evt = JSON.parse(line);
        onEvent(evt);
        if (evt.type === 'result') final.result = evt;
        else if (evt.type === 'error') final.error = evt;
      } catch (e) {
        // Skip malformed line
      }
    }
  }
  return final;
}
