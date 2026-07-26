import { useState } from 'react';
import { changePassword } from '../api';

// Small modal that lets a logged-in user change their own password. On success, closes itself
// and surfaces a brief success message via the onSuccess callback (parent can show a toast).
export default function ChangePasswordModal({ onClose, onSuccess }) {
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  const validate = () => {
    if (!oldPassword) return 'Enter your current password.';
    if (newPassword.length < 8) return 'New password must be at least 8 characters.';
    if (newPassword !== confirmPassword) return 'New passwords do not match.';
    if (newPassword === oldPassword) return 'New password must differ from current.';
    return null;
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    const v = validate();
    if (v) { setError(v); return; }
    setError(null);
    setSubmitting(true);
    const res = await changePassword({ old_password: oldPassword, new_password: newPassword });
    setSubmitting(false);
    if (res.ok) {
      onSuccess?.();
      onClose?.();
    } else {
      setError(res.error || 'Failed to change password');
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <form onSubmit={handleSubmit} className="bg-white rounded-lg shadow-xl p-6 w-[420px]">
        <h2 className="text-lg font-semibold text-gray-900 mb-4">Change password</h2>

        <label className="block mb-3">
          <span className="text-sm font-medium text-gray-700">Current password</span>
          <input
            type="password"
            value={oldPassword}
            onChange={(e) => setOldPassword(e.target.value)}
            autoFocus
            autoComplete="current-password"
            disabled={submitting}
            className="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 disabled:bg-gray-100"
          />
        </label>

        <label className="block mb-3">
          <span className="text-sm font-medium text-gray-700">New password</span>
          <input
            type="password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            autoComplete="new-password"
            disabled={submitting}
            className="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 disabled:bg-gray-100"
          />
          <span className="text-xs text-gray-500">At least 8 characters.</span>
        </label>

        <label className="block mb-4">
          <span className="text-sm font-medium text-gray-700">Confirm new password</span>
          <input
            type="password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            autoComplete="new-password"
            disabled={submitting}
            className="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 disabled:bg-gray-100"
          />
        </label>

        {error && (
          <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2 mb-3">
            {error}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-3 py-1.5 rounded-md text-sm font-medium bg-gray-100 text-gray-700 hover:bg-gray-200 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={submitting}
            className={`px-3 py-1.5 rounded-md text-sm font-medium ${
              submitting
                ? 'bg-gray-200 text-gray-500 cursor-not-allowed'
                : 'bg-blue-600 text-white hover:bg-blue-700'
            }`}
          >
            {submitting ? 'Updating…' : 'Update password'}
          </button>
        </div>
      </form>
    </div>
  );
}
