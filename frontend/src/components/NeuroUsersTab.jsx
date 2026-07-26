import { useEffect, useState } from 'react';
import { fetchUsers, resetPassword } from '../api';

// Admin-only user management. Lists everyone from users.yaml and lets an admin reset any
// password back to the username (the initial value). The reset user then sees the standard
// "rotate-me" banner on next login.
export default function NeuroUsersTab() {
  const [users, setUsers] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);          // username currently being reset
  const [toast, setToast] = useState(null);        // {username, password}

  const load = () => {
    fetchUsers()
      .then(setUsers)
      .catch(e => setError(String(e)));
  };
  useEffect(() => { load(); }, []);

  // Auto-dismiss toast after 8 seconds.
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 8000);
    return () => clearTimeout(t);
  }, [toast]);

  const handleReset = async (username) => {
    if (!window.confirm(
      `Reset password for "${username}" back to "${username}"?\n\n`
      + `The user will be able to log in with their username as the password, and will be `
      + `prompted to change it.`
    )) return;
    setBusy(username);
    setError(null);
    const res = await resetPassword(username);
    setBusy(null);
    if (!res.ok) {
      setError(res.error || 'Reset failed');
      return;
    }
    setToast({ username: res.username, password: res.temp_password });
    load();  // refresh password_is_default flags
  };

  if (error) {
    return <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">{error}</div>;
  }
  if (!users) {
    return <div className="bg-white rounded-lg shadow p-6 text-gray-500">Loading users…</div>;
  }

  return (
    <div className="space-y-4">
      {toast && (
        <div className="bg-green-50 border border-green-300 text-green-900 px-4 py-3 rounded-lg flex items-center justify-between">
          <span>
            Password reset for <strong>{toast.username}</strong>. Tell them their temporary
            password is <code className="bg-white px-1.5 py-0.5 rounded border border-green-200 font-mono">{toast.password}</code>{' '}
            — they'll be prompted to change it on next login.
          </span>
          <button onClick={() => setToast(null)} className="ml-3 text-green-700 hover:text-green-900">✕</button>
        </div>
      )}

      <div className="bg-white rounded-lg shadow overflow-hidden">
        <div className="px-6 py-4 border-b">
          <h3 className="text-lg font-semibold text-gray-900">Users</h3>
          <p className="text-sm text-gray-500 mt-1">
            {users.length} accounts. Reset any password back to its initial value (= the
            username) so a user who forgot their password can log in again.
          </p>
        </div>
        <table className="min-w-full divide-y divide-gray-200">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Username</th>
              <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Role</th>
              <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Attending</th>
              <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Password</th>
              <th className="px-4 py-2"></th>
            </tr>
          </thead>
          <tbody className="bg-white divide-y divide-gray-100">
            {users.map(u => (
              <tr key={u.username}>
                <td className="px-4 py-2 text-sm font-medium text-gray-900">{u.username}</td>
                <td className="px-4 py-2 text-sm text-gray-600">{u.role}</td>
                <td className="px-4 py-2 text-sm text-gray-600">{u.attending_id || <span className="text-gray-400">—</span>}</td>
                <td className="px-4 py-2 text-sm">
                  {u.password_is_default
                    ? <span className="px-1.5 py-0.5 rounded text-xs bg-amber-100 text-amber-800">Initial (= username)</span>
                    : <span className="px-1.5 py-0.5 rounded text-xs bg-gray-100 text-gray-700">Changed</span>}
                </td>
                <td className="px-4 py-2 text-right">
                  <button
                    onClick={() => handleReset(u.username)}
                    disabled={busy === u.username}
                    className={`px-3 py-1 rounded text-xs font-medium ${
                      busy === u.username
                        ? 'bg-gray-100 text-gray-400'
                        : 'bg-blue-100 text-blue-700 hover:bg-blue-200'
                    }`}
                  >
                    {busy === u.username ? 'Resetting…' : 'Reset password'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
