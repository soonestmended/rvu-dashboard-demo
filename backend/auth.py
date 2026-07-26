"""Username/password auth via signed session cookies.

Users are defined in `config/users.yaml`. Sample shape:

    - username: dave
      password_hash: "$2b$12$..."   # generate via `python -m backend.auth hash-password <pw>`
      role: admin
    - username: floriolli
      password_hash: "$2b$12$..."
      role: individual
      attending_id: ATT001132

Roles:
  - "admin"      : sees all tabs / all data.
  - "individual" : sees only their own stats; backend enforces filtering by `attending_id`.

Helpers exported:
  hash_password(pt)     → bcrypt hash (CLI: `python -m backend.auth hash-password <pw>`)
  verify_password(pt, h)→ bool
  find_user(username)   → user dict or None
  current_user()        → {username, role, attending_id?} from session, or None
  require_auth(fn)      → decorator returning 401 if not logged in
  require_admin(fn)     → decorator returning 403 if logged-in user isn't admin
"""

import os
from functools import wraps
from pathlib import Path

import bcrypt
import yaml
from flask import jsonify, session

USERS_YAML_PATH = Path(os.environ.get('USERS_YAML', '/app/config/users.yaml'))


def hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plaintext: str, hash_str: str) -> bool:
    if not plaintext or not hash_str:
        return False
    try:
        return bcrypt.checkpw(plaintext.encode(), hash_str.encode())
    except (ValueError, TypeError):
        return False


def load_users() -> list[dict]:
    if not USERS_YAML_PATH.exists():
        return []
    with open(USERS_YAML_PATH) as f:
        data = yaml.safe_load(f) or []
    return data if isinstance(data, list) else []


def update_user_password(username: str, new_hash: str) -> None:
    """Replace just the `password_hash` line for `username` in users.yaml, atomically.

    Uses line-based substitution (not yaml.dump) so comments, ordering, and the rest of
    the file remain byte-identical. Writes to a temp file in the same directory then
    `os.replace`s it into place — safe against partial writes if the process crashes.
    """
    if not USERS_YAML_PATH.exists():
        raise FileNotFoundError(f'users.yaml not found at {USERS_YAML_PATH}')
    raw = USERS_YAML_PATH.read_text()
    lines = raw.split('\n')

    target_idx = None
    for i, line in enumerate(lines):
        if line.strip() == f'- username: {username}':
            for j in range(i + 1, len(lines)):
                stripped = lines[j].strip()
                if stripped.startswith('- username:'):
                    break  # entered next user block without finding the field
                if stripped.startswith('password_hash:'):
                    target_idx = j
                    break
            break
    if target_idx is None:
        raise RuntimeError(f'No password_hash line found in users.yaml for {username!r}')

    raw_line = lines[target_idx]
    indent = raw_line[:len(raw_line) - len(raw_line.lstrip())]
    lines[target_idx] = f'{indent}password_hash: "{new_hash}"'

    import tempfile
    fd, tmp = tempfile.mkstemp(
        dir=str(USERS_YAML_PATH.parent), prefix='.users.', suffix='.tmp',
    )
    try:
        with os.fdopen(fd, 'w') as f:
            f.write('\n'.join(lines))
        os.replace(tmp, str(USERS_YAML_PATH))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def find_user(username: str) -> dict | None:
    if not username:
        return None
    for u in load_users():
        if u.get('username') == username:
            return u
    return None


def current_user() -> dict | None:
    """Resolve the logged-in user from the Flask session. Returns None if not logged in or
    the username no longer exists in users.yaml (e.g., user was removed)."""
    username = session.get('username')
    if not username:
        return None
    u = find_user(username)
    if not u:
        return None
    return {
        'username': u['username'],
        'role': u.get('role', 'individual'),
        'attending_id': u.get('attending_id'),
    }


def require_auth(fn):
    """Decorator: any endpoint with this wrapper returns 401 unless a session exists."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return jsonify({'error': 'authentication required'}), 401
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    """Decorator: 401 if not logged in, 403 if logged in but not admin role."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u:
            return jsonify({'error': 'authentication required'}), 401
        if u.get('role') != 'admin':
            return jsonify({'error': 'admin access required'}), 403
        return fn(*args, **kwargs)
    return wrapper


def require_admin_or_demo(fn):
    """Decorator: 401 if not logged in, 403 if role isn't admin or demo. Used on read-only
    endpoints that the demo user needs to render Productivity / Schedule / My Stats — dollar
    figures on those views are hidden client-side. Mutating endpoints (schedule generate,
    publish, refresh, users, feature flags) stay `require_admin` only."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u:
            return jsonify({'error': 'authentication required'}), 401
        if u.get('role') not in ('admin', 'demo'):
            return jsonify({'error': 'admin access required'}), 403
        return fn(*args, **kwargs)
    return wrapper


def _generate_users(specs):
    """Print ready-to-paste users.yaml entries for a list of user specs. Each spec is either:
        username           — admin user (no attending_id)
        username:ATT_ID    — individual user bound to that attending
    The username doubles as the initial password (intended for VPN-only deployments where
    account security is layered on the network, not the password). Recommend rotating once
    the user logs in.
    """
    import sys
    from .config import load_neuro_config

    valid_att_ids = set((load_neuro_config().get('attendings') or {}).keys())

    for spec in specs:
        if ':' in spec:
            username, att_id = spec.split(':', 1)
            username = username.strip()
            att_id = att_id.strip()
            if att_id and att_id not in valid_att_ids:
                print(f"# WARNING: {att_id} not in neuro_config attendings — emitting anyway",
                      file=sys.stderr)
        else:
            username = spec.strip()
            att_id = None

        if not username:
            print(f"# Skipping empty spec '{spec}'", file=sys.stderr)
            continue

        password = username  # initial password = username
        hsh = hash_password(password)
        role = 'individual' if att_id else 'admin'
        print(f"- username: {username}")
        print(f'  password_hash: "{hsh}"')
        print(f"  role: {role}")
        if att_id:
            print(f"  attending_id: {att_id}")
        print(f"  # initial password: {password}")
        print()


def _list_attendings():
    """Print attending_id → name pairs from neuro_config so you can find the ATT IDs to use
    with `generate-users`."""
    from .config import load_neuro_config
    attendings = (load_neuro_config().get('attendings') or {})
    rows = sorted(attendings.items(), key=lambda kv: (kv[1].get('name') or ''))
    for att_id, info in rows:
        name = info.get('name') or '(no name)'
        fte = info.get('fte', '?')
        print(f"{att_id}  FTE {fte:>4}  {name}")


def _main():
    import argparse
    import getpass
    parser = argparse.ArgumentParser(description='Auth utilities')
    sub = parser.add_subparsers(dest='cmd', required=True)

    h = sub.add_parser('hash-password', help='Generate a single bcrypt hash')
    h.add_argument('password', nargs='?', help='Password (omit to prompt securely)')

    g = sub.add_parser(
        'generate-users',
        help='Print users.yaml entries. Spec is `username` (admin) or `username:ATT_ID` '
             '(individual). Initial password = username.',
    )
    g.add_argument('specs', nargs='+', metavar='SPEC',
                   help='username or username:ATT_ID (e.g. dfloriolli:ATT001132 admin1 admin2)')

    sub.add_parser(
        'list-attendings',
        help='Print attending_id → name pairs from neuro_config, for finding ATT IDs.',
    )

    args = parser.parse_args()
    if args.cmd == 'hash-password':
        pw = args.password or getpass.getpass('Password: ')
        if not pw:
            raise SystemExit('Empty password')
        print(hash_password(pw))
    elif args.cmd == 'generate-users':
        _generate_users(args.specs)
    elif args.cmd == 'list-attendings':
        _list_attendings()


if __name__ == '__main__':
    _main()
