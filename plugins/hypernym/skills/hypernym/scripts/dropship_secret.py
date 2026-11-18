#!/usr/bin/env python3
"""
DROPSHIP SECRETS — move a secret file (an API key, a .env value, a credential) between friends' computers WITHOUT its
bytes entering either agent's context: no message body, tool output, inbox line, log or notice carries them.

    sender:   dropship.py secret-offer <file> <#peer/session> [--label NAME] [--move]
    receiver: dropship.py secret-allow <friend> [--session NAME] [--dest PATH]   standing, scoped grant (default: none)
              dropship.py secret-deny <friend>
              dropship.py secret-list
              dropship.py secret-install <id> <dest> [--key NAME] [--overwrite]
              dropship.py secret-discard <id>

AUTHORIZATION, receiver side, checked every time it matters (arrival, list, install, discard):
  the friend is known, not blocked, inside its session scope; it holds a grant for the target session and the
  destination folder; the staged item hasn't expired; and the session running the command is the one it was sent to.
Arrival is authorized BEFORE a byte is buffered (dropship_mail keeps secret chunks in memory only, never journaled).
Staged items are namespaced by the sender's machine id and never overwrite each other; they are encrypted at rest.
RECEIPTS are bound to the pinned recipient machine id, the transfer id and the content hash, with a fixed status
vocabulary; --move deletes the sender's source only for a matching 'installed' receipt AND an unchanged file (size,
mtime, hash snapshot).
FILES are written through exclusive, unpredictable temp files (mkstemp), fsync'd, renamed into place, 0600 (on
Windows an owner-only ACL is applied with icacls — untested here), and cleaned up on failure. Paths through symlinks
are refused.
.ENV installs set one KEY with a double-quoted literal value (\\ " $ ` and newlines escaped). Files with multi-line
quoted values are refused rather than risk corrupting them. This is a dotenv value, not a promise that sourcing the
file in a shell is safe.
LIMITS: contents stay out of model context, transport and the mailbox server; an agent with unrestricted shell access
as the same OS user can still read installed files. Deletion is ordinary deletion, not secure erase.
NO .get().
"""
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

import dropship as d

SECRETS = d.HOME / 'secrets'
SENT = SECRETS / 'sent'
MAX_BYTES = 48 * 1024
TTL_S = 3600
ID_RE = re.compile(r'^sx_[0-9a-f]{16}$')
PID_RE = re.compile(r'^[0-9a-f]{8,32}$')
KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')
STATUSES = ('installed', 'refused', 'expired')


# ------------------------------------------------------------------------------------------------ private files
def _owner_only(p: Path) -> None:
    """Owner-only access, or an exception — never a silent success. Windows: drop inheritance, remove the broad
    groups (Everyone, Users, Authenticated Users), grant the current user only, and check every icacls result
    (implemented to spec; not yet exercised on a real Windows machine)."""
    if d.IS_WIN:
        user = os.environ['USERNAME'] if 'USERNAME' in os.environ else ''
        if not user:
            raise PermissionError('cannot protect a secret: USERNAME is not set')
        steps = [['icacls', str(p), '/inheritance:r'],
                 ['icacls', str(p), '/remove:g', '*S-1-1-0', '*S-1-5-32-545', '*S-1-5-11'],
                 ['icacls', str(p), '/grant:r', f'{user}:F']]
        for argv in steps:
            r = subprocess.run(argv, capture_output=True, creationflags=d.NO_WINDOW)
            if r.returncode != 0:
                raise PermissionError(f'cannot protect a secret file (icacls exit {r.returncode})')
    else:
        p.chmod(0o700 if p.is_dir() else 0o600)


def _dirs() -> None:
    for p in (SECRETS, SENT):
        p.mkdir(parents=True, exist_ok=True)
        _owner_only(p)


def _write_private(p: Path, data: bytes) -> None:
    """Exclusive unpredictable temp file in the destination folder → fsync → rename; removed on any failure."""
    fd, tmp = tempfile.mkstemp(prefix=f'.{p.name}.', suffix='.tmp', dir=str(p.parent))
    try:
        _owner_only(Path(tmp))                         # protected BEFORE a single plaintext byte is written
        with os.fdopen(fd, 'wb') as f:
            fd = None
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _no_symlinks(p: Path) -> None:
    for part in [p] + list(p.parents):
        if part.is_symlink():
            raise ValueError(f'refusing a path through a symlink ({part})')


def _snapshot(path: Path) -> dict:
    st = path.stat()
    return {'size': st.st_size, 'mtime': st.st_mtime_ns, 'sha': hashlib.sha256(path.read_bytes()).hexdigest()}


def _safe_source(path: str) -> Path:
    p = Path(path).expanduser()
    p = p if p.is_absolute() else Path.cwd() / p
    _no_symlinks(p)
    p = p.resolve()
    if not p.is_file():
        raise ValueError(f'{path}: not a file')
    if p.stat().st_size > MAX_BYTES:
        raise ValueError(f'{path}: over {MAX_BYTES // 1024} KB — not a secret-sized file')
    return p


# ------------------------------------------------------------------------------------------------ grants
def grant(friend: str, on: bool, session: str = '', dest: str = '') -> str:
    """secret-allow: a standing grant scoped to (peer × session × destination folder); secret-deny removes all.
    Both must be named: a session (or 'any') and a destination folder (or 'any') — no implicit wildcards."""
    if on and (not session or not dest):
        return ('Dropship: say which session and which folder, e.g. secret-allow alice --session work '
                '--dest ~/proj  (use --session any / --dest any to allow all, deliberately).')
    session = '' if session == 'any' else session
    dest = '' if dest == 'any' else dest
    with d.friends_lock():
        friends = d.load(d.FRIENDS, {})
        pid = friend if friend in friends else d.friend_id(friend.lstrip('#'))
        if not pid:
            return f'Dropship: no friend called {friend}.'
        if not on:
            friends[pid].pop('secrets', None)
            d.save(d.FRIENDS, friends)
            return f"Dropship: refusing secret files from #{friend.lstrip('#')}."
        sid = '*'
        if session:
            hits = [(r['seen'], k) for k, r in d.all_sessions().items()
                    if r['name'] == session.lstrip('#').split('/')[-1]]
            if not hits:
                return f'Dropship: no local session called {session}.'
            sid = max(hits)[1]
        folder = str(Path(dest).expanduser().resolve()) if dest else '*'
        cur = friends[pid]['secrets'] if 'secrets' in friends[pid] and isinstance(friends[pid]['secrets'], list) else []
        cur = [g for g in cur if not (g['session'] == sid and g['dest'] == folder)] + [{'session': sid, 'dest': folder}]
        friends[pid]['secrets'] = cur
        d.save(d.FRIENDS, friends)
    where = (f' into session {session}' if session else ' (any session it may reach)') + \
        (f', destinations under {folder}' if dest else ', any allowed destination')
    return f"Dropship: accepting secret files from #{friend.lstrip('#')}{where}."


def _grants(pid: str) -> list:
    friends = d.load(d.FRIENDS, {})
    g = friends[pid]['secrets'] if pid in friends and 'secrets' in friends[pid] else None
    if g is True:
        return [{'session': '*', 'dest': '*'}]          # pre-0.7.48 boolean grant
    return g if isinstance(g, list) else []


def authorized(pid: str, owner: str, dest: str = '') -> str:
    """'' when allowed now, else the reason. The single check used at arrival, list, install and discard."""
    friends = d.load(d.FRIENDS, {})
    if pid not in friends:
        return 'unknown peer'
    pol = d.friend_policy(pid)
    if pol['blocked']:
        return 'peer is blocked'
    if pol['scope'] is not None and owner not in pol['scope']:
        return 'outside its session scope'
    dest_s = str(Path(dest).resolve()) if dest else ''
    ok = [g for g in _grants(pid) if g['session'] in ('*', owner)
          and (not dest_s or g['dest'] == '*' or dest_s.startswith(g['dest'].rstrip(os.sep) + os.sep))]
    return '' if ok else 'no secret grant for this session/destination'


def target_owner(pid: str, name: str, to_s: str) -> str:
    pol = d.friend_policy(pid)
    if pol['scope'] is not None:
        hits = [s for s in pol['scope'] if not to_s or d.session_record(s)['name'] == to_s]
        return hits[0] if hits else ''
    return d.owner_of(to_s, name)


# ------------------------------------------------------------------------------------------------ sender (CLI side)
def offer(path: str, to: str, label: str = '', move: bool = False, sid: str = '') -> str:
    """Queue an offer: only the PATH goes into the outbox; the courier reads the file when it sends."""
    src = _safe_source(path)
    target = d.resolve_target(to)
    machine, _, to_s = target.partition('/')
    if machine == d.identity()['name']:
        raise ValueError('secret files go to another computer; for this one, just copy the file')
    to_id = d.friend_id(machine)
    friends = d.load(d.FRIENDS, {})
    if not to_id or to_id not in friends or 'box' not in friends[to_id]:
        raise ValueError(f'#{machine} is not a mailbox friend')
    _dirs()
    tid = 'sx_' + secrets.token_hex(8)
    label = d.clean(label or src.name, 60)
    _write_private(SENT / f'{tid}.json', json.dumps({'id': tid, 'path': str(src), 'to': machine, 'to_id': to_id,
                                                       'to_s': to_s, 'label': label, 'move': move, 'snap': _snapshot(src),
                                                       'status': 'queued', 'at': time.time()}).encode())
    d.OUTBOX.mkdir(parents=True, exist_ok=True)
    d.save(d.OUTBOX / f'{time.time():.6f}-{tid}.json', {'to': machine, 'to_id': to_id, 'to_s': to_s, 'kind': 'secret',
                                                         'secret_id': tid, 'sid': sid,
                                                         'from_s': d.session_record(sid)['name'] if sid else ''})
    return f'Dropship: secret {tid} ("{label}") queued for #{target}. Its contents never appear here.'


def build_payload(tid: str) -> str:
    rec = json.loads((SENT / f'{tid}.json').read_text())
    src = _safe_source(rec['path'])
    data = src.read_bytes()
    if hashlib.sha256(data).hexdigest() != rec['snap']['sha']:
        raise ValueError('source changed since the offer; offer it again')
    return json.dumps({'t': 'dsp-secret/1', 'id': tid, 'label': rec['label'], 'size': len(data),
                       'sha': rec['snap']['sha'], 'data': base64.b64encode(data).decode()})


def sent_status(tid: str, status: str) -> None:
    p = SENT / f'{tid}.json'
    rec = json.loads(p.read_text())
    rec['status'], rec['at'] = status, time.time()
    _write_private(p, json.dumps(rec).encode())


def receipt_in(courier, pid: str, body: str) -> None:
    """An authenticated receipt: must come from the PINNED recipient id, for that transfer, that content."""
    try:
        r = json.loads(body)
        tid, status = str(r['id']), str(r['status'])
        if not ID_RE.match(tid) or status not in STATUSES:
            return
        rec = json.loads((SENT / f'{tid}.json').read_text())
    except (ValueError, KeyError, TypeError, OSError):
        return
    if 'to_id' not in rec or pid != rec['to_id']:
        d.log(f'secret {tid}: receipt from an unexpected peer ignored')
        return
    if status == 'installed' and ('sha' not in r or r['sha'] != rec['snap']['sha']):
        d.log(f'secret {tid}: receipt does not match the offered content; ignored')
        return
    if status == 'installed' and rec['to_s'] and ('session' not in r or r['session'] != rec['to_s']):
        d.log(f'secret {tid}: receipt is from a different session than the one it was sent to; ignored')
        return
    sent_status(tid, status)
    d.log(f'secret {tid} receipt: {status}')
    if status == 'installed' and rec['move']:
        src = Path(rec['path'])
        try:
            if _snapshot(src) == rec['snap']:
                src.unlink()
                sent_status(tid, 'installed+moved')
                d.log(f'secret {tid}: source deleted after a verified receipt')
            else:
                d.log(f'secret {tid}: source changed since the offer; NOT deleted')
        except OSError as e:
            d.log(f'secret {tid}: could not delete source ({e})')


# ------------------------------------------------------------------------------------------------ receiver (courier)
PARTIAL = SECRETS / 'partial'
MID_RE = re.compile(r'^[0-9a-f]{8,64}$')


def buffer_chunk(pid: str, mid: str, i: int, n: int, to_s: str, chunk: str):
    """Store one authorized chunk encrypted at rest; return the whole body once all n are present, else None."""
    import nacl.secret
    if not PID_RE.match(pid) or not MID_RE.match(mid) or not 0 <= i < n <= 64:
        return None
    _dirs()
    PARTIAL.mkdir(exist_ok=True)
    _owner_only(PARTIAL)
    box = nacl.secret.SecretBox(hashlib.sha256(b'dropship-secret-partial|' + bytes.fromhex(d.identity()['box_priv'])
                                               + f'{pid}|{mid}'.encode()).digest())
    head = PARTIAL / f'{pid}-{mid}.json'
    meta = json.loads(head.read_text()) if head.exists() else {'n': n, 'to_s': to_s, 'at': time.time()}
    if meta['n'] != n or meta['to_s'] != to_s:
        return None                                    # chunks must agree on shape and target
    if not head.exists():
        _write_private(head, json.dumps(meta).encode())
    _write_private(PARTIAL / f'{pid}-{mid}.{i}', box.encrypt(chunk.encode()))
    parts = [PARTIAL / f'{pid}-{mid}.{k}' for k in range(n)]
    if not all(p.exists() for p in parts):
        return None
    whole = ''.join(box.decrypt(p.read_bytes()).decode() for p in parts)
    for p in parts + [head]:
        p.unlink(missing_ok=True)
    return whole



def _local_key(pid: str, tid: str) -> bytes:
    me = d.identity()
    return hashlib.sha256(b'dropship-secret-at-rest|' + bytes.fromhex(me['box_priv']) + pid.encode() + b'|'
                          + tid.encode()).digest()


def _staged(pid: str, tid: str) -> tuple:
    if not PID_RE.match(pid) or not ID_RE.match(tid):
        raise ValueError('bad id')
    return SECRETS / f'{pid}-{tid}.json', SECRETS / f'{pid}-{tid}.enc'


def secret_in(courier, pid: str, name: str, body: str, to_s: str) -> None:
    """A complete, already pre-authorized 'secret' mail. Re-check, verify, store encrypted at rest."""
    import nacl.secret
    try:
        p = json.loads(body)
        tid = str(p['id'])
        if p['t'] != 'dsp-secret/1' or not ID_RE.match(tid):
            return
    except (ValueError, KeyError, TypeError):
        return
    owner = target_owner(pid, name, to_s)
    scoped = d.friend_policy(pid)['scope'] is not None
    why = authorized(pid, owner) if (owner or not scoped) else 'no target session in its scope'
    if why:
        d.log(f'secret {tid} from {name} ({pid}) refused: {why}')
        return _receipt(courier, pid, tid, 'refused', '')
    data = base64.b64decode(p['data'])
    if len(data) != int(p['size']) or len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != p['sha']:
        d.log(f'secret {tid} from {name} ({pid}) refused: integrity check failed')
        return _receipt(courier, pid, tid, 'refused', '')
    _dirs()
    meta_p, enc_p = _staged(pid, tid)
    if meta_p.exists() or enc_p.exists():
        d.log(f'secret {tid} from {name}: already staged; duplicate ignored (no overwrite)')
        return
    _write_private(enc_p, nacl.secret.SecretBox(_local_key(pid, tid)).encrypt(data))
    label = d.clean(str(p['label']), 60)
    _write_private(meta_p, json.dumps({'id': tid, 'from': name, 'from_id': pid, 'label': label, 'size': len(data),
                                       'sha': p['sha'], 'owner': owner, 'to_s': to_s,
                                       'expires': time.time() + TTL_S}).encode())
    d.append_inbox({'dir': 'in', 'via': 'mailbox', 'from': name, 'from_id': pid, 'from_s': '', 'to': d.identity()['name'],
                    'to_s': to_s, 'owner': owner, 'ts': time.time(),
                    'body': f'[secret file] {tid} "{label}" ({len(data)} bytes) is waiting, encrypted, for {TTL_S // 60} '
                            f'min. Its contents are not shown. Ask your user where it goes, then: hypernym dropship '
                            f'secret install {tid} <destination> [--key NAME]'})
    d.log(f'secret {tid} from {name} staged ({len(data)} bytes)')


def _receipt(courier, pid, tid, status, sha):
    friends = d.load(d.FRIENDS, {})
    if pid not in friends or 'box' not in friends[pid] or courier is None or courier.mail is None:
        return
    try:
        courier.mail.send(dict(friends[pid], id=pid), json.dumps({'id': tid, 'status': status, 'sha': sha}),
                          'secret-receipt')
    except (OSError, ValueError, KeyError) as e:
        d.log(f'secret {tid}: receipt not sent ({e})')


def sweep() -> None:
    if not SECRETS.exists():
        return
    for head in PARTIAL.glob('*.json') if PARTIAL.exists() else []:   # abandoned partial transfers
        try:
            if json.loads(head.read_text())['at'] < time.time() - TTL_S:
                for p in PARTIAL.glob(head.stem + '.*'):
                    p.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError):
            pass
    for meta in SECRETS.glob('*-sx_*.json'):
        try:
            if json.loads(meta.read_text())['expires'] < time.time():
                for p in (meta, meta.with_suffix('.enc')):
                    p.unlink(missing_ok=True)
                d.log(f'secret {meta.stem} expired and was deleted')
        except (OSError, ValueError, KeyError):
            pass


# ------------------------------------------------------------------------------------------------ receiver (CLI side)
def _items() -> list:
    out = []
    for meta in sorted(SECRETS.glob('*-sx_*.json')) if SECRETS.exists() else []:
        try:
            out.append(json.loads(meta.read_text()))
        except (OSError, ValueError):
            pass
    return out


def _find(tid: str, sid: str) -> dict:
    if not ID_RE.match(tid):
        raise ValueError('not a secret id')
    if not sid:
        raise ValueError('which session is this for? (run it from that session, or pass --session NAME)')
    hits = [m for m in _items() if m['id'] == tid and (m['owner'] in ('', sid))]
    if not hits:
        raise ValueError(f'no pending secret {tid} for this session (expired, discarded, installed or sent elsewhere)')
    if len(hits) > 1:
        raise ValueError(f'{tid} is ambiguous (several senders used it); discard and ask for a new one')
    m = hits[0]
    if m['expires'] < time.time():
        raise ValueError(f'{tid} has expired; ask the sender to offer it again')
    return m


def pending(sid: str = '') -> str:
    if not sid:
        return 'Pending secret files: say which session (run it from that session, or pass --session NAME).'
    rows = []
    for m in _items():
        if m['expires'] < time.time() or m['owner'] not in ('', sid) or authorized(m['from_id'], m['owner']):
            continue
        rows.append(f"{m['id']}  \"{m['label']}\"  {m['size']} bytes  from #{m['from']}  "
                    f"expires {time.strftime('%H:%M', time.localtime(m['expires']))}")
    return 'Pending secret files:\n' + ('\n'.join(rows) if rows else '(none)')


def _safe_dest(dest: str) -> Path:
    p = Path(dest).expanduser()
    p = p if p.is_absolute() else Path.cwd() / p
    _no_symlinks(p)
    p = p.resolve()
    home, cwd = Path.home().resolve(), Path.cwd().resolve()
    if not (str(p).startswith(str(home) + os.sep) or str(p).startswith(str(cwd) + os.sep)):
        raise ValueError('destination must be inside your home folder or the current project')
    if not p.parent.is_dir():
        raise ValueError(f'{p.parent} does not exist')
    return p


def _env_quote(value: str) -> str:
    return '"' + (value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('`', '\\`')
                  .replace('\n', '\\n')) + '"'


def _env_lines(text: str) -> list:
    """Line-oriented .env, refusing anything this simple writer could corrupt (multi-line quoted values)."""
    lines = text.splitlines()
    for ln in lines:
        m = re.match(r'^\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*(["\'])(.*)$', ln)
        if m:
            q, rest = m.group(1), m.group(2)
            body = re.sub(r'\\.', '', rest) if q == '"' else rest
            if q not in body:
                raise ValueError('this .env has a multi-line quoted value; edit it by hand or install as a file')
    return lines


def install(tid: str, dest: str, key: str = '', overwrite: bool = False, sid: str = '') -> str:
    import nacl.exceptions
    import nacl.secret
    m = _find(tid, sid)
    target = _safe_dest(dest)
    why = authorized(m['from_id'], m['owner'], str(target))
    if why:
        raise ValueError(f'not allowed now: {why}')
    meta_p, enc_p = _staged(m['from_id'], tid)
    try:
        data = nacl.secret.SecretBox(_local_key(m['from_id'], tid)).decrypt(enc_p.read_bytes())
    except (nacl.exceptions.CryptoError, OSError):
        raise ValueError('staged secret failed its integrity check; discard it and ask for a new one')
    if hashlib.sha256(data).hexdigest() != m['sha']:
        raise ValueError('staged secret failed its integrity check')
    if key:
        if not KEY_RE.match(key):
            raise ValueError('a .env key is letters, digits and _ (not starting with a digit)')
        try:
            value = data.decode('utf-8').rstrip('\r\n')
        except UnicodeDecodeError:
            raise ValueError('this secret is binary; install it as a file instead of a .env key')
        lines = _env_lines(target.read_text()) if target.exists() else []
        out, done = [], False
        for ln in lines:
            mm = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', ln)
            if mm and mm.group(1) == key:
                if not overwrite:
                    raise ValueError(f'{key} is already set in {target}; add --overwrite to replace it')
                if not done:
                    out.append(f'{key}={_env_quote(value)}')
                    done = True
                continue
            out.append(ln)
        if not done:
            out.append(f'{key}={_env_quote(value)}')
        payload = ('\n'.join(out) + '\n').encode()
    else:
        if target.exists() and not overwrite:
            raise ValueError(f'{target} exists; add --overwrite to replace it')
        payload = data
    _write_private(target, payload)
    # the receipt is durably queued BEFORE the staged copy is removed
    d.OUTBOX.mkdir(parents=True, exist_ok=True)
    d.save(d.OUTBOX / f'{time.time():.6f}-{tid}-receipt.json', {'to': m['from'], 'to_id': m['from_id'],
                                                                  'kind': 'secret-receipt', 'secret_id': tid,
                                                                  'receipt': 'installed', 'sha': m['sha'],
                                                                  'session': m['to_s']})
    for p in (meta_p, enc_p):
        p.unlink(missing_ok=True)
    return f'Dropship: installed {tid} → {target}' + (f' ({key})' if key else '') + '. Receipt queued.'


def discard(tid: str, sid: str = '') -> str:
    m = _find(tid, sid)
    meta_p, enc_p = _staged(m['from_id'], tid)
    for p in (meta_p, enc_p):
        p.unlink(missing_ok=True)
    return f'Dropship: discarded {tid}.'
