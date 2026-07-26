import { useState } from 'react';
import { login as loginApi } from '../api';

// Full-screen login. Calls back to the parent (App) on success with the user info, so the
// parent can swap to the main app view. No password reset / signup — Dave manages users
// directly via config/users.yaml.
export default function LoginPage({ onLogin }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    const res = await loginApi({ username: username.trim(), password });
    setSubmitting(false);
    if (res.ok) {
      onLogin(res.user);
    } else {
      setError(res.error || 'Login failed');
    }
  };

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center px-4">
      <form onSubmit={handleSubmit} className="bg-white rounded-lg shadow-md p-8 w-full max-w-sm space-y-4">
        <h1 className="text-2xl font-bold text-gray-900">RVU Dashboard</h1>
        <p className="text-sm text-gray-600">Sign in to continue.</p>

        <label className="block">
          <span className="text-sm font-medium text-gray-700">Username</span>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            autoComplete="username"
            disabled={submitting}
            className="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 disabled:bg-gray-100"
          />
        </label>

        <label className="block">
          <span className="text-sm font-medium text-gray-700">Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            disabled={submitting}
            className="mt-1 block w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 disabled:bg-gray-100"
          />
        </label>

        {error && (
          <div className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">
            {error}
          </div>
        )}

        <button
          type="submit"
          disabled={submitting || !username || !password}
          className={`w-full py-2 rounded-md text-sm font-medium ${
            submitting || !username || !password
              ? 'bg-gray-200 text-gray-500 cursor-not-allowed'
              : 'bg-blue-600 text-white hover:bg-blue-700'
          }`}
        >
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  );
}
