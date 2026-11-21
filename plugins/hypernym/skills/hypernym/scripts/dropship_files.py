#!/usr/bin/env python3
"""
DROPSHIP FILES (core) — large files between friends' computers, end-to-end encrypted, resumable, chunked.

The offer (which carries the per-transfer key K) travels only inside the friends' existing end-to-end encrypted Dropship
channel. The bytes then move over a transport (dropship_files_direct: same Wi-Fi / Tailscale; dropship_files_relay:
mailbox attachments) as independently authenticated chunks:

    chunk i   = plaintext[i*CHUNK : (i+1)*CHUNK]           n = ceil(size / CHUNK), n = 1 for an empty file
    nonce_i   = sha256(b'dsp-file-nonce|' + tid)[:16] + i (8 bytes, big endian)
    cipher_i  = SecretBox(K).encrypt(chunk_i, nonce_i).ciphertext     (binds each chunk to its index AND transfer)

The whole-file sha256 of the plaintext is in the offer and checked after reassembly.

STATE (all private: dirs 0700, files 0600, written via temp + os.replace) under HOME/files/:
    out/<tid>.json            sender: path, size, sha256, key, to_id, to_s, status, snap {size, mtime}
    in/<pid>-<tid>.json       receiver: offer fields, from_id, status, dest, have [chunk indices], route
    partial/<pid>-<tid>.part  receiver: preallocated plaintext, chunks written at their offsets
    messages/<peer>-<tid>.md  receiver: kind 'message' landings
    active.json               progress for the status bar: [{id, name, dir, done_bytes, size, peer, ts}]

File CONTENTS never go into a message body, log line or inbox entry: only ids, names and sizes.
Stdlib + PyNaCl (imported lazily). NO .get().
"""
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path

import dropship as d
import dropship_files_direct as dfd
from dropship_secret import PID_RE, _no_symlinks, _owner_only

CHUNK = 8 * 1024 * 1024
PROTO = 'dsp-file/1'
FILES = d.HOME / 'files'
OUT = FILES / 'out'
IN = FILES / 'in'
PARTIAL = FILES / 'partial'
MESSAGES = FILES / 'messages'
ACTIVE_FILE = FILES / 'active.json'
DOWNLOADS = Path(os.environ['DROPSHIP_DOWNLOADS']) if 'DROPSHIP_DOWNLOADS' in os.environ else \
    Path.home() / 'Downloads'                    # landing anchor (may itself be a symlink, e.g. to another disk)
LANDING = 'Dropship'                            # default landing: DOWNLOADS / LANDING / <peer> / <name>

ID_RE = re.compile(r'^fx_[0-9a-f]{16}$')
SHA_RE = re.compile(r'^[0-9a-f]{64}$')
KINDS = ('file', 'message')
RECEIPTS = ('saved', 'refused', 'failed')
PENDING_TTL_S = 24 * 3600                       # pending (or stalled) incoming offers expire after this
DONE_TTL_S = 24 * 3600                          # finished/refused records are kept this long (for receipts)
OUT_TTL_S = 48 * 3600                           # sender records: served at most this long after the offer (fixed)
ACTIVE_EVERY_S = 1.0                            # active.json is rewritten at most this often
ACTIVE_STALE_S = 120.0                          # entries not updated for this long are not shown
NAME_MAX, LABEL_MAX, SESSION_MAX, DIRECT_MAX = 200, 60, 64, 8
MAX_SIZE = 1 << 40                              # 1 TiB: anything bigger in an offer is refused as malformed
MESSAGE_MAX = 64 * 1024 * 1024                  # kind 'message' is text: refused above this
AUTO_ACCEPT_MAX = 10 * 1024 ** 3
MAX_PENDING_PER_PEER = 20                       # open incoming records (pending/accepted/transferring) per friend
MAX_PENDING_TOTAL = 100                         # ... and in all
OPEN = ('pending', 'accepted', 'transferring')
REASON_MAX = 200
REPLACE_TRIES = 10                              # Windows: os.replace retries on PermissionError (AV / indexer locks)
RELAY_MAX = 2 * 1024 ** 3

_lock = threading.RLock()
_finishing = set()                              # 'pid-tid' being finished in this process (hash/copy run unlocked)
_live = {}                                      # 'dir:pid:tid' -> active entry (this process)
_served = {}                                    # tid -> set of chunk indices served (sender progress)
_active_at = [0.0]
_BAD_NAME = re.compile(r'[\x00-\x1f\x7f/\\]')
_WIN_BAD = re.compile(r'[\x00-\x1f\x7f/\\<>:"|?*]')
_WIN_RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {f'COM{k}' for k in range(1, 10)} | {f'LPT{k}' for k in range(1, 10)}
_RW = os.O_RDWR | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)




class Duplicate(ValueError):
    """offer_in: this offer (same peer, same tid) is already recorded; nothing changed, send no second notice."""

    def __init__(self, tid: str):
        super().__init__(f'file {tid}: already offered')
        self.tid = tid


DUPLICATE = Duplicate


class FinishError(ValueError):
    """finish() failed for a reason other than a fingerprint mismatch. The record is 'failed' (reason stored) unless it
    was already closed (refused/failed), which is left as it was. receipt() works either way."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ------------------------------------------------------------------------------------------------ crypto
def _nonce(tid: str, i: int) -> bytes:
    return hashlib.sha256(b'dsp-file-nonce|' + tid.encode()).digest()[:16] + int(i).to_bytes(8, 'big')


def encrypt_chunk(K: bytes, tid: str, i: int, plain: bytes) -> bytes:
    import nacl.secret
    return nacl.secret.SecretBox(K).encrypt(bytes(plain), _nonce(tid, i)).ciphertext


def decrypt_chunk(K: bytes, tid: str, i: int, ct: bytes) -> bytes:
    """Raises ValueError if the chunk is not chunk i of transfer tid under K (or was tampered with)."""
    import nacl.exceptions
    import nacl.secret
    try:
        return nacl.secret.SecretBox(K).decrypt(bytes(ct), _nonce(tid, i))
    except (nacl.exceptions.CryptoError, TypeError, ValueError):
        raise ValueError(f'chunk {i} of {tid} failed authentication')


def chunk_count(size: int, chunk: int = 0) -> int:
    chunk = chunk or CHUNK
    return max(1, -(-int(size) // chunk))


def _chunk_len(size: int, chunk: int, i: int) -> int:
    return max(0, min(chunk, size - i * chunk))


def read_plain_chunk(path, i: int, chunk: int = 0) -> bytes:
    chunk = chunk or CHUNK
    with open(path, 'rb') as f:
        f.seek(i * chunk)
        return f.read(chunk)


# ------------------------------------------------------------------------------------------------ helpers
def _dirs() -> None:
    for p in (FILES, OUT, IN, PARTIAL):
        p.mkdir(parents=True, exist_ok=True)
        _owner_only(p)


def _replace(src, dst) -> None:
    """os.replace, retried briefly on Windows where another process (antivirus, indexer) can hold the file."""
    for k in range(REPLACE_TRIES):
        try:
            os.replace(str(src), str(dst))
            return
        except PermissionError:
            if not d.IS_WIN or k == REPLACE_TRIES - 1:
                raise
            time.sleep(0.05 * (k + 1))


def _write_private(p: Path, data: bytes) -> None:
    """Exclusive unpredictable temp file in the destination folder (0600) → fsync → replace; removed on failure."""
    fd, tmp = tempfile.mkstemp(prefix=f'.{p.name}.', suffix='.tmp', dir=str(p.parent))
    try:
        _owner_only(Path(tmp))
        with os.fdopen(fd, 'wb') as f:
            fd = None
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, p)
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


def _read(p: Path) -> dict:
    try:
        r = json.loads(p.read_text())
    except (OSError, ValueError):
        raise ValueError('no such transfer')
    if not isinstance(r, dict):
        raise ValueError('no such transfer')
    return r


def _put(p: Path, rec: dict) -> None:
    _write_private(p, json.dumps(rec).encode())


def _in_view(rec: dict) -> dict:
    """An incoming record with the fields every caller may index (older records lack owner/reason)."""
    for k in ('owner', 'reason'):
        if k not in rec:
            rec[k] = ''
    return rec


def _in_paths(tid: str, pid: str) -> tuple:
    if not ID_RE.match(str(tid)) or not PID_RE.match(str(pid)):
        raise ValueError('bad transfer or peer id')
    return IN / f'{pid}-{tid}.json', PARTIAL / f'{pid}-{tid}.part'


def _out_path(tid: str) -> Path:
    if not ID_RE.match(str(tid)):
        raise ValueError('bad transfer id')
    return OUT / f'{tid}.json'


def _valid_name(name) -> bool:
    return (isinstance(name, str) and 0 < len(name) <= NAME_MAX and not _BAD_NAME.search(name)
            and not d._UNSAFE.search(name) and not name.startswith('.') and name == name.strip()
            and name not in ('.', '..'))


def safe_name(name: str, fallback: str = 'file') -> str:
    """A landing name on every OS: no path separators or reserved characters, no leading dots, <= 200 chars."""
    s = d._UNSAFE.sub('', _WIN_BAD.sub('_', str(name))).strip().lstrip('.').strip().rstrip('. ')
    s = s[:NAME_MAX].rstrip('. ')
    if not s:
        return fallback
    if s.split('.')[0].upper() in _WIN_RESERVED:
        s = '_' + s
    return _fit_bytes(s[:NAME_MAX]) or fallback


def _fit_bytes(s: str, limit: int = NAME_MAX) -> str:
    """Shorten s to <= limit UTF-8 bytes (filesystems cap names at 255 bytes; leave room for ' (2)'), keeping a short
    extension. Never splits a character."""
    if len(s.encode('utf-8')) <= limit:
        return s
    suffix = Path(s).suffix
    suffix = suffix if 0 < len(suffix.encode('utf-8')) <= 16 and suffix != s else ''
    stem = s[:-len(suffix)] if suffix else s
    room = limit - len(suffix.encode('utf-8'))
    stem = stem.encode('utf-8')[:room].decode('utf-8', 'ignore').rstrip('. ')
    return (stem + suffix) if stem else ''


def _stream_sha(path, size: int) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        left = size
        while left > 0:
            b = f.read(min(1 << 20, left))
            if not b:
                break
            h.update(b)
            left -= len(b)
    return h.hexdigest()


def _snap(path: Path) -> dict:
    st = path.stat()
    return {'size': st.st_size, 'mtime': st.st_mtime_ns}


# ------------------------------------------------------------------------------------------------ path identity
# Paths are compared by what they ARE on disk (st_dev, st_ino), never by string prefix: a case-changed spelling on a
# case-insensitive disk, a hardlink, or a second mount point all name the same file. For a path that does not exist
# yet, its identity is (identity of its deepest existing ancestor, casefolded remaining names).
def _ident(p: Path):
    try:
        st = os.stat(str(p))
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def _canon(p: Path) -> tuple:
    tail = []
    cur = Path(os.path.abspath(str(p)))
    while True:
        i = _ident(cur)
        if i is not None:
            return i, tuple(reversed(tail))
        if cur.parent == cur:
            return None, tuple(reversed(tail))
        tail.append(cur.name.casefold())
        cur = cur.parent


def _at_or_under(p: Path, root: Path) -> bool:
    """True if p is root, or anything below it — by on-disk identity of p and each of its parents."""
    want = _canon(root)
    if want[0] is None:
        return False
    for q in [p] + list(Path(os.path.abspath(str(p))).parents):
        if _canon(q) == want:
            return True
    return False


def _home_inodes() -> set:
    """(st_dev, st_ino) of every file under Dropship HOME (no symlink following)."""
    got = set()
    for top, dirs, files in os.walk(str(d.HOME), followlinks=False):
        for name in files:
            try:
                st = os.lstat(os.path.join(top, name))
            except OSError:
                continue
            got.add((st.st_dev, st.st_ino))
    return got


def _safe_source(path: str, kind: str = 'file') -> Path:
    p = Path(path).expanduser()
    p = p if p.is_absolute() else Path.cwd() / p
    _no_symlinks(p)
    p = p.resolve()
    if not p.is_file():
        raise ValueError(f'{path}: not a file')
    st = p.stat()
    if kind == 'message' and p.parent.name == 'outmsg' and _at_or_under(p.parent, FILES / 'outmsg'):
        if st.st_nlink > 1:                              # our own staging files are never hardlinked
            raise ValueError(f'{path}: refusing a hardlinked message file')
        return p                                         # long messages wait in FILES/outmsg to be sent as one file
    if _at_or_under(p, d.HOME):
        raise ValueError(f'{path}: refusing to send Dropship\'s own state')
    if st.st_nlink > 1 and (st.st_dev, st.st_ino) in _home_inodes():
        raise ValueError(f'{path}: refusing to send Dropship\'s own state (a hardlink to it)')
    return p


def _no_symlinks_below(anchor: Path, p: Path) -> None:
    """Refuse symlinks in p and every component of p strictly below anchor (the anchor itself may be a link)."""
    rel = p.relative_to(anchor)
    cur = anchor
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f'refusing a path through a symlink ({cur})')


def _private_mkdirs(anchor: Path, p: Path) -> None:
    _no_symlinks_below(anchor, p)
    cur = anchor
    for part in p.relative_to(anchor).parts:
        cur = cur / part
        if not cur.exists():
            cur.mkdir()
            _owner_only(cur)
    _no_symlinks_below(anchor, p)
    if not p.is_dir():
        raise ValueError(f'{p} is not a folder')


# ------------------------------------------------------------------------------------------------ progress
def _set_live(direction: str, pid: str, tid: str, name: str, done: int, size: int, peer: str) -> None:
    with _lock:
        _live[f'{direction}:{pid}:{tid}'] = {'id': tid, 'name': name, 'dir': direction, 'done_bytes': int(done),
                                             'size': int(size), 'peer': peer, 'ts': time.time()}
    _flush()


def _drop_live(direction: str, pid: str, tid: str) -> None:
    with _lock:
        _live.pop(f'{direction}:{pid}:{tid}', None)
        if direction == 'out':
            _served.pop(tid, None)
    _flush(force=True)


def _flush(force: bool = False) -> None:
    with _lock:
        now = time.time()
        if not force and now - _active_at[0] < ACTIVE_EVERY_S:
            return
        _active_at[0] = now
        rows = [dict(v) for v in _live.values()]
    try:
        FILES.mkdir(parents=True, exist_ok=True)
        _write_private(ACTIVE_FILE, json.dumps(rows).encode())
    except OSError:
        pass


def active() -> list:
    """Transfers in flight, for the status bar: [{id, name, dir: in|out, done_bytes, size, peer}]."""
    if _live:
        _flush()
    try:
        rows = json.loads(ACTIVE_FILE.read_text())
    except (OSError, ValueError):
        return []
    now = time.time()
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and 'ts' in r and now - float(r['ts']) <= ACTIVE_STALE_S:
            out.append({k: r[k] for k in ('id', 'name', 'dir', 'done_bytes', 'size', 'peer') if k in r})
    return out


# ------------------------------------------------------------------------------------------------ sender
def offer(path: str, to_label: str, to_id: str, to_s: str, label: str = '', kind: str = 'file',
          session: str = '') -> tuple:
    """Record an outgoing transfer and build its file-offer. Returns (tid, offer_dict). The integration fills in
    offer_dict['direct'] / ['relay'] and sends it inside the friend channel. The file itself is read later, per chunk.
    session: the sending session's id, stored in the out record (notices about this transfer go to it)."""
    if kind not in KINDS:
        raise ValueError('kind is file or message')
    if not PID_RE.match(str(to_id)):
        raise ValueError('bad recipient id')
    src = _safe_source(path, kind)
    snap = _snap(src)
    if kind == 'message' and snap['size'] > MESSAGE_MAX:
        raise ValueError(f'{path}: too long to send as a message; send it as a file')
    sha = _stream_sha(src, snap['size'])
    if _snap(src) != snap:
        raise ValueError(f'{path}: changed while it was being read; try again')
    _dirs()
    tid = 'fx_' + secrets.token_hex(8)
    key = secrets.token_bytes(32)
    name = safe_name(src.name)
    label = d.clean(label or name, LABEL_MAX)
    to_s = d.clean(to_s or '', SESSION_MAX)
    n = chunk_count(snap['size'], CHUNK)
    now = time.time()
    _put(_out_path(tid), {'id': tid, 'path': str(src), 'name': name, 'size': snap['size'], 'sha256': sha,
                          'key': base64.b64encode(key).decode(), 'chunk': CHUNK, 'n': n, 'to': str(to_label),
                          'to_id': to_id, 'to_s': to_s, 'label': label, 'kind': kind, 'snap': snap,
                          'session': d.clean(str(session or ''), SESSION_MAX), 'status': 'offered', 'at': now,
                          'offered_at': now, 'expires': now + OUT_TTL_S})
    d.log(f'file {tid} offered to {to_label} ({snap["size"]} bytes, {n} chunks)')
    return tid, {'t': PROTO, 'id': tid, 'name': name, 'size': snap['size'], 'sha256': sha, 'chunk': CHUNK, 'n': n,
                 'key': base64.b64encode(key).decode(), 'label': label, 'kind': kind, 'direct': [], 'relay': False,
                 'to_s': to_s}


def out_record(tid: str) -> dict:
    rec = _read(_out_path(tid))
    if 'session' not in rec:
        rec['session'] = ''
    return rec


def sent_status(tid: str, status: str) -> None:
    with _lock:
        p = _out_path(tid)
        rec = _read(p)
        rec['status'], rec['at'] = status, time.time()
        _put(p, rec)
    if status in RECEIPTS or status == 'cancelled':
        _drop_live('out', rec['to_id'], tid)


def serve_chunk(tid: str, i: int, requester_id: str) -> bytes:
    """Ciphertext of chunk i, only for the transfer's own recipient, only while the source is unchanged.
    Raises PermissionError (wrong requester / closed transfer) or ValueError (bad index / source changed)."""
    rec = _read(_out_path(tid))
    if not isinstance(requester_id, str) or requester_id != rec['to_id']:
        raise PermissionError(f'{tid}: not this peer\'s transfer')
    if rec['status'] in ('refused', 'failed', 'cancelled', 'saved'):
        raise PermissionError(f'{tid}: transfer is {rec["status"]}')
    if time.time() > rec['expires']:                     # fixed at offer + 48 h: never kept alive by activity
        raise PermissionError(f'{tid}: transfer has expired')
    if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < rec['n']:
        raise ValueError(f'{tid}: no chunk {i}')
    src = Path(rec['path'])
    _no_symlinks(src)
    if _snap(src) != rec['snap']:
        raise ValueError(f'{tid}: the source file changed since the offer; offer it again')
    chunk = rec['chunk']
    with open(src, 'rb') as f:
        f.seek(i * chunk)
        plain = f.read(chunk)
        st = os.fstat(f.fileno())
    if st.st_size != rec['snap']['size'] or st.st_mtime_ns != rec['snap']['mtime'] or \
            len(plain) != _chunk_len(rec['size'], chunk, i):
        raise ValueError(f'{tid}: the source file changed since the offer; offer it again')
    ct = encrypt_chunk(base64.b64decode(rec['key']), tid, i, plain)
    with _lock:
        got = _served.setdefault(tid, set())
        got.add(i)
        done = sum(_chunk_len(rec['size'], chunk, k) for k in got)
    _set_live('out', rec['to_id'], tid, rec['name'], done, rec['size'], rec['to'])
    return ct


def receipt_in(pid: str, receipt: dict) -> str:
    """A file-receipt from the friend channel: must come from the pinned recipient and (for 'saved') match the content.
    Returns the status recorded, or '' if ignored."""
    try:
        tid, status = str(receipt['id']), str(receipt['status'])
        if not ID_RE.match(tid) or status not in RECEIPTS:
            return ''
        rec = _read(_out_path(tid))
    except (ValueError, KeyError, TypeError):
        return ''
    if pid != rec['to_id']:
        d.log(f'file {tid}: receipt from an unexpected peer ignored')
        return ''
    if status == 'saved' and ('sha256' not in receipt or receipt['sha256'] != rec['sha256']):
        d.log(f'file {tid}: receipt does not match the offered content; ignored')
        return ''
    sent_status(tid, status)
    d.log(f'file {tid} receipt: {status}')
    return status


# ------------------------------------------------------------------------------------------------ receiver
def _check_offer(o) -> dict:
    """Strict validation of an incoming file-offer. Returns the cleaned fields, raises ValueError."""
    def bad(why):
        raise ValueError(f'malformed file offer: {why}')

    def is_int(v):
        return isinstance(v, int) and not isinstance(v, bool)

    if not isinstance(o, dict):
        bad('not an object')
    for k in ('t', 'id', 'name', 'size', 'sha256', 'chunk', 'n', 'key', 'label', 'kind', 'direct', 'relay', 'to_s'):
        if k not in o:
            bad(f'missing {k}')
    if o['t'] != PROTO:
        bad('unknown protocol')
    if not isinstance(o['id'], str) or not ID_RE.match(o['id']):
        bad('id')
    if not _valid_name(o['name']):
        bad('name')
    if not is_int(o['size']) or not 0 <= o['size'] <= MAX_SIZE:
        bad('size')
    if not isinstance(o['sha256'], str) or not SHA_RE.match(o['sha256']):
        bad('sha256')
    if not is_int(o['chunk']) or o['chunk'] != CHUNK:
        bad('chunk size')
    if not is_int(o['n']) or o['n'] != chunk_count(o['size'], o['chunk']):
        bad('chunk count')
    try:
        key = base64.b64decode(o['key'], validate=True) if isinstance(o['key'], str) else b''
    except ValueError:
        key = b''
    if len(key) != 32:
        bad('key')
    if not isinstance(o['label'], str) or len(o['label']) > LABEL_MAX:
        bad('label')
    if o['kind'] not in KINDS:
        bad('kind')
    if o['kind'] == 'message' and o['size'] > MESSAGE_MAX:
        bad('message too long')
    if not isinstance(o['direct'], list) or len(o['direct']) > DIRECT_MAX:
        bad('direct routes')
    routes = []
    for r in o['direct']:
        if not isinstance(r, dict) or 'host' not in r or 'port' not in r or not isinstance(r['host'], str) \
                or not is_int(r['port']) or not 0 < r['port'] < 65536:
            bad('direct route')
        try:
            ip = ipaddress.ip_address(r['host'])
        except ValueError:
            bad('direct route host')
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not dfd.allowed_source(str(ip)):              # IPv4 RFC 1918 or Tailscale 100.64/10 only
            bad('direct route host is not on a private network')
        routes.append({'host': str(ip), 'port': r['port']})
    if not isinstance(o['relay'], bool):
        bad('relay')
    if not isinstance(o['to_s'], str) or len(o['to_s']) > SESSION_MAX:
        bad('to_s')
    return {'id': o['id'], 'name': o['name'], 'size': o['size'], 'sha256': o['sha256'], 'chunk': o['chunk'],
            'n': o['n'], 'key': o['key'], 'label': d.clean(o['label'], LABEL_MAX), 'kind': o['kind'],
            'direct': routes, 'relay': o['relay'], 'to_s': d.clean(o['to_s'], SESSION_MAX)}


def _reserved(skip: Path) -> int:
    """Bytes other open transfers still need: their partials are preallocated but (sparse) not yet written."""
    total = 0
    for meta in IN.glob('*-fx_*.json') if IN.exists() else []:
        if meta == skip:
            continue
        try:
            r = _read(meta)
            if r['status'] not in ('accepted', 'transferring') or \
                    not (PARTIAL / (meta.stem + '.part')).exists():
                continue
            done = sum(_chunk_len(r['size'], r['chunk'], k) for k in set(r['have']))
            total += max(0, int(r['size']) - done)
        except (ValueError, KeyError, TypeError):
            continue
    return total


def _prealloc(part: Path, size: int, meta: Path) -> None:
    """Caller holds _lock (so reservations are counted consistently)."""
    if part.is_symlink():
        raise ValueError('refusing a partial file that is a symlink')
    if part.exists():
        return
    free = shutil.disk_usage(str(PARTIAL)).free
    held = _reserved(meta)
    if size + held > free:
        raise ValueError(f'not enough disk space ({size} bytes needed, {free} free, {held} reserved by other '
                         f'transfers)')
    fd = os.open(str(part), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    try:
        _owner_only(part)
        os.ftruncate(fd, size)
    finally:
        os.close(fd)


def _open_counts(pid: str) -> tuple:
    """(open records from pid, open records in all). Open = pending/accepted/transferring and not expired."""
    mine = every = 0
    now = time.time()
    for meta in IN.glob('*-fx_*.json') if IN.exists() else []:
        try:
            r = _read(meta)
            if r['status'] not in OPEN or r['expires'] < now:
                continue
        except (ValueError, KeyError, TypeError):
            continue
        every += 1
        if meta.name.startswith(pid + '-'):
            mine += 1
    return mine, every


def offer_in(pid: str, peer_label: str, offer_dict: dict, auto: bool, owner: str = '') -> str:
    """Validate and store an incoming file-offer from friend pid. Status 'accepted' when auto (policy already checked by
    the caller with auto_accept), else 'pending' until accept()/refuse(). owner: the local session the offer is for
    (stored in the record). Returns tid. A repeated offer (same peer, same tid) changes nothing and raises Duplicate.
    ValueError beyond MAX_PENDING_PER_PEER open records from this peer or MAX_PENDING_TOTAL in all."""
    if not PID_RE.match(str(pid)):
        raise ValueError('bad peer id')
    o = _check_offer(offer_dict)
    tid = o['id']
    _dirs()
    meta, part = _in_paths(tid, pid)
    with _lock:
        if meta.exists():
            d.log(f'file {tid} from {pid}: already known; repeated offer ignored')
            raise Duplicate(tid)
        mine, every = _open_counts(pid)
        if mine >= MAX_PENDING_PER_PEER:
            raise ValueError(f'too many open file offers from this friend ({mine}); refuse or finish some first')
        if every >= MAX_PENDING_TOTAL:
            raise ValueError(f'too many open file offers ({every}); refuse or finish some first')
        now = time.time()
        rec = dict(o, from_id=pid, peer=safe_name(peer_label, 'peer'), status='pending', dest='', have=[], route='',
                   at=now, expires=now + PENDING_TTL_S, path='', owner=d.clean(str(owner or ''), SESSION_MAX),
                   reason='')
        if auto:
            try:
                _prealloc(part, o['size'], meta)
                rec['status'] = 'accepted'
            except (OSError, ValueError) as e:
                d.log(f'file {tid}: not auto-accepted ({e}); left pending')
        _put(meta, rec)
    d.log(f'file {tid} offered by {rec["peer"]} ({o["size"]} bytes): {rec["status"]}')
    return tid


def in_record(tid: str, pid: str) -> dict:
    return _in_view(_read(_in_paths(tid, pid)[0]))


def pending() -> list:
    """Incoming records not yet finished (pending/accepted/transferring), without keys."""
    out = []
    for p in sorted(IN.glob('*-fx_*.json')) if IN.exists() else []:
        try:
            r = _read(p)
            if r['status'] not in OPEN or r['expires'] < time.time():
                continue
        except (ValueError, KeyError, TypeError):
            continue
        out.append({k: v for k, v in _in_view(r).items() if k != 'key'})
    return out


def _denied_dirs() -> list:
    """Folders a received file must never land in (or under): Dropship's own state, agent/plugin config, and the
    places an OS runs things from at login."""
    h = Path.home()
    out = [d.HOME, h / '.hypernym', h / '.ssh', h / '.gnupg', h / '.claude', h / '.codex',
           h / 'Library' / 'LaunchAgents', Path('/Library/LaunchAgents'), Path('/Library/LaunchDaemons'),
           h / '.config' / 'autostart', h / '.config' / 'systemd', Path('/etc')]
    startup = Path('Microsoft') / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'
    out.append(h / 'AppData' / 'Roaming' / startup)
    for env in ('APPDATA', 'ProgramData', 'ALLUSERSPROFILE'):
        if env in os.environ and os.environ[env]:
            out.append(Path(os.environ[env]) / startup)
    return out


def _check_dest(p: Path, shown: str = '') -> None:
    """Refuse a landing folder at/under Dropship HOME or a denied folder (by on-disk identity, not by spelling)."""
    for root in _denied_dirs():
        if _at_or_under(p, root):
            raise ValueError(f'{shown or p}: refusing to save received files there')


def _dest_dir(dest_dir: str) -> str:
    if not dest_dir:
        return ''
    p = Path(dest_dir).expanduser()
    p = p if p.is_absolute() else Path.cwd() / p
    _no_symlinks(p)
    p = p.resolve()
    if p.exists() and not p.is_dir():
        raise ValueError(f'{dest_dir} is not a folder')
    _check_dest(p, dest_dir)
    return str(p)


def accept(tid: str, pid: str, dest_dir: str = '') -> dict:
    """Accept a pending offer (optionally into dest_dir). Returns the record (without its key)."""
    meta, part = _in_paths(tid, pid)
    with _lock:
        rec = _read(meta)
        if rec['status'] not in ('pending', 'accepted'):
            raise ValueError(f'{tid} is {rec["status"]}')
        if rec['expires'] < time.time():
            raise ValueError(f'{tid} has expired; ask the sender to offer it again')
        dest = _dest_dir(dest_dir)
        _dirs()
        _prealloc(part, rec['size'], meta)
        rec['status'] = 'accepted'
        rec['dest'] = dest or rec['dest']
        rec['expires'] = time.time() + PENDING_TTL_S
        _put(meta, rec)
    d.log(f'file {tid} accepted')
    return {k: v for k, v in rec.items() if k != 'key'}


def set_route(tid: str, pid: str, route: str) -> None:
    meta, _ = _in_paths(tid, pid)
    with _lock:
        rec = _read(meta)
        rec['route'] = str(route)[:16]
        _put(meta, rec)


def _close(tid: str, pid: str, status: str, reason: str = '') -> bool:
    """Close a record as refused/failed and remove its partial. 'saved' is final (ValueError); 'refused' is never
    turned into 'failed' (returns False, nothing changed)."""
    meta, part = _in_paths(tid, pid)
    with _lock:
        rec = _read(meta)
        if rec['status'] == 'saved':
            raise ValueError(f'{tid} is already saved')
        if status == 'failed' and rec['status'] == 'refused':
            return False
        rec['status'] = status
        rec['reason'] = d.clean(str(reason or ''), REASON_MAX)
        rec['expires'] = time.time() + DONE_TTL_S
        _put(meta, rec)
        try:
            part.unlink(missing_ok=True)
        except OSError:                                 # Windows: still held open; sweep removes orphans later
            pass
    _drop_live('in', pid, tid)
    return True


def refuse(tid: str, pid: str) -> None:
    _close(tid, pid, 'refused')
    d.log(f'file {tid} refused')


def fail(tid: str, pid: str, reason: str = '') -> None:
    """Give up on a transfer (no route, deadline passed...): the partial is removed, status 'failed' with reason.
    Never overwrites 'refused' (left as it is)."""
    if _close(tid, pid, 'failed', reason):
        d.log(f'file {tid} failed')


def store_chunk(tid: str, pid: str, i: int, ciphertext: bytes) -> bool:
    """Decrypt chunk i, write it at its offset, record it. Idempotent: False if it was already stored.
    Safe to call from several threads at once. Raises ValueError for a bad chunk or a closed transfer."""
    meta, part = _in_paths(tid, pid)
    rec = _read(meta)
    if rec['status'] not in ('accepted', 'transferring'):
        raise ValueError(f'{tid} is {rec["status"]}')
    if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < rec['n']:
        raise ValueError(f'{tid}: no chunk {i}')
    if i in rec['have']:
        return False
    plain = decrypt_chunk(base64.b64decode(rec['key']), tid, i, ciphertext)
    chunk = rec['chunk']
    if len(plain) != _chunk_len(rec['size'], chunk, i):
        raise ValueError(f'{tid}: chunk {i} has the wrong length')
    if part.is_symlink() or not part.is_file():
        raise ValueError(f'{tid}: partial file missing; accept it again')
    fd = os.open(str(part), _RW)
    try:
        os.lseek(fd, i * chunk, os.SEEK_SET)
        view = memoryview(plain)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    with _lock:
        rec = _read(meta)
        if rec['status'] not in ('accepted', 'transferring'):
            raise ValueError(f'{tid} is {rec["status"]}')
        if i in rec['have']:
            return False
        rec['have'] = sorted(set(rec['have']) | {i})
        rec['status'] = 'transferring'
        rec['expires'] = time.time() + PENDING_TTL_S
        _put(meta, rec)
        done = sum(_chunk_len(rec['size'], chunk, k) for k in rec['have'])
    _set_live('in', pid, tid, rec['name'], done, rec['size'], rec['peer'])
    return True


def missing(tid: str, pid: str) -> list:
    rec = _read(_in_paths(tid, pid)[0])
    have = set(rec['have'])
    return [k for k in range(rec['n']) if k not in have]


def _reserve(folder: Path, name: str) -> Path:
    """Create (exclusively) a new empty 0600 file named name, 'name (2)', 'name (3)'... — never overwrites."""
    p = Path(name)
    suffix = p.suffix if 0 < len(p.suffix) <= 16 and p.suffix != name else ''
    stem = name[:-len(suffix)] if suffix else name
    for k in range(1, 10000):
        cand = folder / (name if k == 1 else f'{stem} ({k}){suffix}')
        try:
            fd = os.open(str(cand), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return cand
    raise ValueError(f'too many files called {name} in {folder}')


def _land(part: Path, target: Path) -> None:
    """Move the verified partial onto the reserved target (copy through a temp file across filesystems)."""
    try:
        _replace(part, target)
        return
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix='.dsp-', suffix='.tmp', dir=str(target.parent))
    try:
        _owner_only(Path(tmp))
        with os.fdopen(fd, 'wb') as dst, open(part, 'rb') as srcf:
            fd = None
            shutil.copyfileobj(srcf, dst, 1 << 20)
            dst.flush()
            os.fsync(dst.fileno())
        _replace(tmp, target)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    part.unlink(missing_ok=True)


class _Mismatch(Exception):
    pass


def _landing(tid: str, rec: dict) -> tuple:
    """(anchor, folder, name) for a finished transfer, folder created privately and checked (no symlinks below the
    anchor; not at/under Dropship HOME or a denied folder)."""
    if rec['kind'] == 'message':
        anchor, folder, name = d.HOME, MESSAGES, f'{rec["peer"]}-{tid}.md'
    elif rec['dest']:
        folder = Path(rec['dest'])
        _no_symlinks(folder)
        _check_dest(folder)
        anchor, name = folder, safe_name(rec['name'])
        folder.mkdir(parents=True, exist_ok=True)
        _no_symlinks(folder)
    else:
        anchor, folder, name = DOWNLOADS, DOWNLOADS / LANDING / rec['peer'], safe_name(rec['name'])
        _check_dest(folder)
        anchor.mkdir(parents=True, exist_ok=True)
    _private_mkdirs(anchor, folder)
    if rec['kind'] != 'message':
        _check_dest(folder)                                  # again, now that every folder exists (by identity)
    return anchor, folder, name


def _finish_work(tid: str, pid: str, rec: dict, part: Path) -> Path:
    """Runs WITHOUT the module lock: checks, hashes, lands. Raises _Mismatch on a fingerprint mismatch."""
    if rec['status'] not in ('accepted', 'transferring'):
        raise ValueError(f'{tid} is {rec["status"]}, not accepted')
    if len(set(rec['have'])) != rec['n']:
        raise ValueError(f'{rec["n"] - len(set(rec["have"]))} chunks still missing')
    if part.is_symlink() or not part.is_file() or part.stat().st_size != rec['size']:
        raise ValueError('partial file is missing or the wrong size')
    if _stream_sha(part, rec['size']) != rec['sha256']:
        raise _Mismatch()
    _, folder, name = _landing(tid, rec)
    with _lock:                                              # refused/failed while we were hashing? do not land
        now_status = _read(_in_paths(tid, pid)[0])['status']
    if now_status not in ('accepted', 'transferring'):
        raise ValueError(f'{tid} became {now_status}')
    target = _reserve(folder, name)
    try:
        _land(part, target)
        _owner_only(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def finish(tid: str, pid: str) -> str:
    """All chunks present → verify sha256 → move to its landing (unique name, 0600) → 'saved'. Returns the path.
    A sha256 mismatch: status 'failed', partial removed, ValueError('fingerprint mismatch'). Any other failure on an
    open record: status 'failed' with the reason stored, partial removed, FinishError(reason). A record already
    refused/failed raises FinishError and is left as it is. receipt() works after every raise.
    Hashing and copying run outside the module lock."""
    meta, part = _in_paths(tid, pid)
    key = f'{pid}-{tid}'
    with _lock:
        rec = _read(meta)
        if rec['status'] == 'saved':
            return rec['path']
        if rec['status'] in RECEIPTS:
            raise FinishError(f'{tid} is {rec["status"]}')
        if key in _finishing:
            raise FinishError(f'{tid} is already being finished')
        _finishing.add(key)
    try:
        try:
            target = _finish_work(tid, pid, rec, part)
        except _Mismatch:
            fail(tid, pid, 'fingerprint mismatch')
            d.log(f'file {tid}: sha256 mismatch after reassembly; discarded')
            raise ValueError('fingerprint mismatch') from None
        except Exception as e:  # noqa: BLE001 - every other failure closes the record as failed
            reason = d.clean(f'{type(e).__name__}: {e}', REASON_MAX)
            try:
                fail(tid, pid, reason)
            except ValueError:
                pass
            d.log(f'file {tid}: could not be saved ({type(e).__name__})')
            raise FinishError(reason) from e
        with _lock:
            fresh = _read(meta)
            if fresh['status'] not in ('accepted', 'transferring'):      # closed during the copy: undo the landing
                target.unlink(missing_ok=True)
                raise FinishError(f'{tid} became {fresh["status"]}')
            fresh['status'], fresh['path'] = 'saved', str(target)
            fresh['expires'] = time.time() + DONE_TTL_S
            _put(meta, fresh)
    finally:
        with _lock:
            _finishing.discard(key)
    _drop_live('in', pid, tid)
    d.log(f'file {tid} saved ({rec["size"]} bytes)')
    return str(target)


def receipt(tid: str, pid: str) -> dict:
    """The file-receipt body for a closed transfer: {id, status, sha256} (sha256 only when saved)."""
    rec = in_record(tid, pid)
    if rec['status'] not in RECEIPTS:
        raise ValueError(f'{tid} is still {rec["status"]}')
    return {'id': tid, 'status': rec['status'], 'sha256': rec['sha256'] if rec['status'] == 'saved' else ''}


# ------------------------------------------------------------------------------------------------ housekeeping
def sweep() -> list:
    """Remove expired incoming offers (pending, or stalled for 24 h) with their partials, old finished records,
    orphan partials and expired sender records. Returns the removed incoming tids."""
    now, gone = time.time(), []
    with _lock:
        for meta in IN.glob('*-fx_*.json') if IN.exists() else []:
            try:
                rec = _read(meta)
                expired = rec['expires'] < now
            except (ValueError, KeyError, TypeError):
                expired, rec = True, {}
            if expired:
                (PARTIAL / (meta.stem + '.part')).unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                gone.append(meta.stem.split('-', 1)[-1])
                if 'status' in rec and rec['status'] in ('pending', 'accepted', 'transferring'):
                    d.log(f'file {meta.stem.split("-", 1)[-1]}: offer expired; removed')
        for part in PARTIAL.glob('*.part') if PARTIAL.exists() else []:
            if not (IN / (part.stem + '.json')).exists():
                part.unlink(missing_ok=True)
        for meta in OUT.glob('fx_*.json') if OUT.exists() else []:
            try:
                expired = _read(meta)['expires'] < now
            except (ValueError, KeyError, TypeError):
                expired = True
            if expired:
                meta.unlink(missing_ok=True)
        for tid in gone:
            for k in [k for k in list(_live) if k.endswith(':' + tid)]:
                _live.pop(k, None)
    if gone:
        _flush(force=True)
    return gone


# ------------------------------------------------------------------------------------------------ policy (pure)
def _cap(cfg: dict, key: str, default: int) -> int:
    v = cfg[key] if isinstance(cfg, dict) and key in cfg else default
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else default


def auto_accept(friend: dict, size: int, cfg: dict) -> bool:
    """True only when the person set this friend's files flag to 'auto' (friends.json[pid]['files'] == 'auto'), AND the
    friend is unscoped, unblocked, unrestricted, AND size <= files_auto_accept_max (10 GiB). Default: ask."""
    if not isinstance(friend, dict) or not friend:
        return False
    if 'files' not in friend or friend['files'] != 'auto':
        return False
    if 'scope' in friend and friend['scope'] is not None:
        return False
    if 'blocked' in friend and friend['blocked']:
        return False
    if d.restricted(friend):
        return False
    return isinstance(size, int) and not isinstance(size, bool) and \
        0 <= size <= _cap(cfg, 'files_auto_accept_max', AUTO_ACCEPT_MAX)


def relay_allowed(size: int, cfg: dict) -> bool:
    return isinstance(size, int) and not isinstance(size, bool) and 0 <= size <= _cap(cfg, 'files_relay_max', RELAY_MAX)
