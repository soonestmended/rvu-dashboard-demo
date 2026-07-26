import { useState } from 'react';
import { setFeatureFlags } from '../api';
import NeuroUsersTab from './NeuroUsersTab';

// Admin-only Settings tab. Houses feature flag toggles and the existing Users management.
// As more admin-controlled settings come along, add them as additional sections here.
export default function NeuroSettingsTab({ featureFlags, onFeatureFlagsChange }) {
  const [saving, setSaving] = useState(null);   // key currently saving
  const [error, setError] = useState(null);

  const toggle = async (key, value) => {
    setSaving(key);
    setError(null);
    const r = await setFeatureFlags({ [key]: value });
    setSaving(null);
    if (r.error) {
      setError(r.error);
      return;
    }
    onFeatureFlagsChange?.();
  };

  return (
    <div className="space-y-6">
      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">{error}</div>
      )}

      <div className="bg-white rounded-lg shadow overflow-hidden">
        <div className="px-6 py-4 border-b">
          <h3 className="text-lg font-semibold text-gray-900">Feature flags</h3>
          <p className="text-sm text-gray-500 mt-1">
            Toggle dashboard features for non-admin users. Admins always see everything.
          </p>
        </div>
        <div className="px-6 py-4 space-y-3">
          <label className="flex items-start gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={!!featureFlags.pay_projection_visible_to_individuals}
              disabled={saving === 'pay_projection_visible_to_individuals'}
              onChange={e => toggle('pay_projection_visible_to_individuals', e.target.checked)}
              className="mt-1 rounded border-gray-300"
            />
            <span className="text-sm text-gray-900">
              <strong>Pay Projection visible to individual users</strong>
              <span className="block text-gray-500 mt-0.5">
                When off, only admins see the Pay Projection tab. Backend also rejects
                /api/me/pay-projection requests from individuals.
              </span>
            </span>
          </label>
        </div>
      </div>

      <NeuroUsersTab />
    </div>
  );
}
