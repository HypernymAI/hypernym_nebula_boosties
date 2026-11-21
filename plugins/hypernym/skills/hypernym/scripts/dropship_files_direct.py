"""Dropship large files: the direct transport (same Wi-Fi, or Tailscale).

The sender runs a small HTTP server bound ONLY to its LAN address (when on a home network) and its Tailscale address
(when present). The receiver fetches `GET /f/<tid>/<i>` chunk by chunk, in parallel, resuming where it left off.

Chunks served here are already encrypted by dropship_files (SecretBox per chunk, nonce bound to tid + index), so this
module only adds: private-address-only sources, a per-request HMAC from the friends' pair key (±120 s timestamp),
per-peer rate limits, and a fast resumable client. Python stdlib only. Never logs payloads.

    X-Dsp-From: <peer machine id>   X-Dsp-Ts: <unix seconds>
    X-Dsp-Mac:  hex(HMAC-SHA256(key, f"{tid}|{i}|{ts}|{from}"))
"""
import hashlib
import hmac
import http.client
import ipaddress
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FILE_PORT = int(os.environ['DROPSHIP_FILE_PORT']) if 'DROPSHIP_FILE_PORT' in os.environ else 47779
CHUNK = 8 * 1024 * 1024
MAX_CT = CHUNK + 64                    # SecretBox ciphertext = plain + 16-byte MAC; a little slack, never more
SKEW_S = 120.0
RATE_N, RATE_S = 9000, 60.0            # chunk requests per peer per minute (8 MiB each: ~1.2 GB/s ceiling)
MAX_CONC = 8                           # simultaneous requests per peer
FAIL_N, FAIL_S = 30, 60.0              # failed auth attempts per source address per minute
TS_CACHE_S = 60.0
TS_TIMEOUT = 2
_PLATFORM = sys.platform
NO_WINDOW = 0x08000000 if _PLATFORM == 'win32' else 0
TID_RE = re.compile(r'^fx_[0-9a-f]{16}$')
PATH_RE = re.compile(r'^/f/(fx_[0-9a-f]{16})/(\d{1,7})$')
ID_RE = re.compile(r'^[0-9A-Za-z_-]{1,64}$')
_ALLOWED_NETS = [ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10')]
_LOOPBACK = ipaddress.ip_network('127.0.0.0/8')
# Tests only: let 127.0.0.1 count as an allowed source / bind address. Production never sets it.
_ALLOW_LOOPBACK = False


class FetchError(Exception):
    """A chunk request that did not return the chunk. `code` is the HTTP status (0 = network/protocol failure)."""

    def __init__(self, code: int, detail: str = ''):
        super().__init__(f'{code} {detail}'.strip())
        self.code = code


# ---------------------------------------------------------------------------------------------------- addresses
def _addr(a: str):
    try:
        ip = ipaddress.ip_address(a)
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def allowed_source(addr: str) -> bool:
    """RFC 1918 or Tailscale CGNAT (100.64.0.0/10) IPv4 only. Loopback only under the test hook."""
    ip = _addr(addr)
    if ip is None or ip.version != 4:
        return False
    if ip in _LOOPBACK:
        return _ALLOW_LOOPBACK
    return any(ip in n for n in _ALLOWED_NETS)


def _cli_candidates() -> list:
    c = ['tailscale']
    if _PLATFORM == 'darwin':
        c.append('/Applications/Tailscale.app/Contents/MacOS/Tailscale')
    elif _PLATFORM == 'win32':
        c.append(os.path.join(os.environ['ProgramFiles'] if 'ProgramFiles' in os.environ else r'C:\Program Files',
                              'Tailscale', 'tailscale.exe'))
    return c


_ts_cache = {'ip': '', 'at': 0.0, 'valid': False}
_ts_lock = threading.Lock()


def tailscale_ip() -> str:
    """This machine's Tailscale IPv4, '' if none. Tries the CLI on PATH, then the platform app path. Cached 60 s."""
    with _ts_lock:
        if _ts_cache['valid'] and time.time() - _ts_cache['at'] < TS_CACHE_S:
            return _ts_cache['ip']
    found = ''
    for exe in _cli_candidates():
        try:
            r = subprocess.run([exe, 'ip', '-4'], capture_output=True, text=True, timeout=TS_TIMEOUT,
                               creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            continue
        for ln in (r.stdout or '').splitlines():
            ip = _addr(ln.strip())
            if ip is not None and ip.version == 4 and ip in _ALLOWED_NETS[3]:
                found = str(ip)
                break
        if found or r.returncode == 0:
            break                       # the CLI answered: no address means Tailscale is down, not a missing CLI
    with _ts_lock:
        _ts_cache.update(ip=found, at=time.time(), valid=True)
    return found


def my_routes(lan_ip: str, home: bool, port=None) -> list:
    """Routes to put in a file-offer: LAN address when on a home network, plus the Tailscale address when present."""
    p = FILE_PORT if port is None else int(port)
    out = []
    if home and lan_ip and allowed_source(lan_ip):
        out.append({'host': lan_ip, 'port': p})
    ts = tailscale_ip()
    if ts and ts != lan_ip:
        out.append({'host': ts, 'port': p})
    return out


# ---------------------------------------------------------------------------------------------------- auth
def mac_for(key: bytes, tid: str, i: int, ts: int, me_id: str) -> str:
    return hmac.new(key, f'{tid}|{i}|{ts}|{me_id}'.encode(), hashlib.sha256).hexdigest()


def auth_headers(key: bytes, tid: str, i: int, me_id: str) -> dict:
    ts = int(time.time())
    return {'X-Dsp-From': me_id, 'X-Dsp-Ts': str(ts), 'X-Dsp-Mac': mac_for(key, tid, i, ts, me_id)}


# ---------------------------------------------------------------------------------------------------- server
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    if _PLATFORM == 'win32':            # SO_REUSEADDR on Windows lets another process steal the port: be exclusive
        allow_reuse_address = False

        def server_bind(self):
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()


class FileServer:
    """Serves encrypted chunks to friends. One ThreadingHTTPServer per bound address (never 0.0.0.0).

    key_for(peer_id) -> bytes|None       the pair key with that friend (None/raise: unknown peer)
    chunk_for(tid, i, peer_id) -> bytes  the ciphertext; raise PermissionError → 403, anything else → 404
    """

    def __init__(self, key_for, chunk_for, port=None, log=None):
        self.key_for, self.chunk_for, self.log = key_for, chunk_for, log
        self.port = FILE_PORT if port is None else int(port)   # 0: ephemeral (tests); see .bound
        self.servers = {}                              # host -> ThreadingHTTPServer
        self.lock = threading.Lock()
        self.rates, self.conc, self.fails = {}, {}, {}

    # -- lifecycle
    def start(self, addrs) -> list:
        """Bind to exactly these local addresses (others are closed). Returns [(host, port)] actually bound."""
        return self.rebind(addrs)

    def rebind(self, addrs) -> list:
        want = []
        for a in addrs or []:
            a = str(a or '').strip()
            if a and a not in want and self._bindable(a):
                want.append(a)
        with self.lock:
            gone = [h for h in self.servers if h not in want]
            drop = [self.servers.pop(h) for h in gone]
        for s in drop:
            self._close(s)
        for a in want:
            with self.lock:
                if a in self.servers:
                    continue
            try:
                s = _Server((a, self.port), _handler(self))
            except OSError as e:
                self._log(f'file server: cannot bind {a}: {e.strerror or e}')
                continue
            threading.Thread(target=s.serve_forever, kwargs={'poll_interval': 0.5}, daemon=True).start()
            with self.lock:
                self.servers[a] = s
            self._log(f'file server listening on {a}:{s.server_address[1]}')
        return self.bound

    def stop(self):
        with self.lock:
            drop = list(self.servers.values())
            self.servers = {}
        for s in drop:
            self._close(s)

    @property
    def bound(self) -> list:
        with self.lock:
            return [(h, s.server_address[1]) for h, s in self.servers.items()]

    @staticmethod
    def _bindable(a: str) -> bool:
        ip = _addr(a)
        return ip is not None and ip.version == 4 and not ip.is_unspecified and allowed_source(a)

    @staticmethod
    def _close(s):
        try:
            s.shutdown()
        finally:
            s.server_close()

    def _log(self, text: str):
        if self.log:
            try:
                self.log(text)
            except Exception:  # noqa: BLE001 - a broken logger must never break serving
                pass

    # -- request checks
    def _failed(self, addr: str, now: float) -> bool:
        """Record a failed auth from addr; True once it should be throttled."""
        with self.lock:
            q = [t for t in (self.fails[addr] if addr in self.fails else []) if now - t < FAIL_S]
            q.append(now)
            self.fails[addr] = q[-FAIL_N - 1:]
            return len(q) > FAIL_N

    def _throttled(self, addr: str, now: float) -> bool:
        with self.lock:
            q = [t for t in (self.fails[addr] if addr in self.fails else []) if now - t < FAIL_S]
            return len(q) >= FAIL_N

    def check(self, path: str, headers, addr: str):
        """-> (code, detail, tid, i, peer_id). Code 200 means the peer passed every check and holds a slot."""
        now = time.time()
        if not allowed_source(addr):
            return 403, 'not a private address', '', 0, ''
        if self._throttled(addr, now):
            return 429, 'too many failures', '', 0, ''
        m = PATH_RE.match(path)
        if not m:
            return 404, 'no', '', 0, ''
        tid, i = m.group(1), int(m.group(2))
        try:
            peer, ts_s, mac = headers['X-Dsp-From'], headers['X-Dsp-Ts'], headers['X-Dsp-Mac']
        except KeyError:
            peer = ts_s = mac = None
        if not (peer and ts_s and mac) or not ID_RE.match(peer) or not re.match(r'^\d{1,12}$', ts_s) \
                or not re.match(r'^[0-9a-fA-F]{64}$', mac.strip()):
            self._failed(addr, now)
            return 403, 'missing auth', '', 0, ''
        try:
            key = self.key_for(peer)
        except Exception:  # noqa: BLE001 - e.g. pair_key ValueError on a bad stored pub
            key = None
        if not key:
            self._failed(addr, now)
            return 403, 'unknown peer', '', 0, ''
        if not hmac.compare_digest(mac_for(key, tid, i, int(ts_s), peer), mac.strip().lower()):
            self._failed(addr, now)
            return 403, 'bad signature', '', 0, ''
        if abs(now - int(ts_s)) > SKEW_S:
            self._failed(addr, now)
            return 403, 'stale', '', 0, ''
        with self.lock:
            q = [t for t in (self.rates[peer] if peer in self.rates else []) if now - t < RATE_S]
            if len(q) >= RATE_N:
                return 429, 'rate limited', '', 0, ''
            c = self.conc[peer] if peer in self.conc else 0
            if c >= MAX_CONC:
                return 429, 'too many at once', '', 0, ''
            q.append(now)
            self.rates[peer] = q
            self.conc[peer] = c + 1
        return 200, 'ok', tid, i, peer

    def release(self, peer: str):
        with self.lock:
            if peer in self.conc:
                self.conc[peer] -= 1
                if self.conc[peer] <= 0:
                    del self.conc[peer]


def _handler(fs: FileServer):
    class H(BaseHTTPRequestHandler):
        server_version = 'dropship'
        sys_version = ''
        protocol_version = 'HTTP/1.1'          # keep-alive: the client reuses one connection per worker
        timeout = 60                           # an idle or stalled client cannot hold a thread forever

        def log_message(self, *_a):
            pass

        def do_GET(self):
            code, detail, tid, i, peer = fs.check(self.path, self.headers, self.client_address[0])
            if code != 200:
                return self._err(code, detail)
            try:
                try:
                    data = fs.chunk_for(tid, i, peer)
                except PermissionError:
                    return self._err(403, 'refused')
                except Exception:  # noqa: BLE001 - unknown transfer, index out of range, source changed...
                    return self._err(404, 'no such chunk')
                if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) > MAX_CT:
                    return self._err(404, 'no such chunk')
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                mv = memoryview(data)
                for off in range(0, len(mv), 1 << 20):
                    self.wfile.write(mv[off:off + (1 << 20)])
            except OSError:
                self.close_connection = True       # client went away mid-chunk; it will resume
            finally:
                fs.release(peer)

        def do_POST(self):
            self._err(405, 'no')

        do_PUT = do_DELETE = do_POST

        def _err(self, code, text):
            b = text.encode()
            try:
                self.send_response(code)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', str(len(b)))
                if code != 200:
                    self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(b)
            except OSError:
                pass
            self.close_connection = True
    return H


# ---------------------------------------------------------------------------------------------------- client
def _get(conn: http.client.HTTPConnection, tid: str, i: int, me_id: str, key: bytes) -> bytes:
    conn.request('GET', f'/f/{tid}/{int(i)}', headers=auth_headers(key, tid, i, me_id))
    r = conn.getresponse()
    if r.status != 200:
        r.read(4096)
        raise FetchError(r.status, r.reason or '')
    cl = r.getheader('Content-Length')
    if cl is None or not cl.isdigit() or int(cl) > MAX_CT:
        raise FetchError(0, 'bad length')
    want = int(cl)
    body = r.read(want)
    if len(body) != want:
        raise FetchError(0, 'short read')
    return body


def fetch_chunk(host: str, port: int, tid: str, i: int, me_id: str, key: bytes, timeout=30) -> bytes:
    """One chunk's ciphertext from a friend's FileServer. Raises FetchError (with .code) or OSError."""
    if not TID_RE.match(tid):
        raise ValueError('bad transfer id')
    conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
    try:
        return _get(conn, tid, i, me_id, key)
    except (http.client.HTTPException, socket.timeout) as e:
        raise FetchError(0, type(e).__name__) from None
    finally:
        conn.close()


def _fatal(e) -> bool:
    """Errors that retrying on the same route will not fix."""
    return isinstance(e, FetchError) and e.code in (403, 404)


def fetch_all(routes, tid, n, missing, me_id, key, store, progress, parallel=4, timeout=30,
              should_stop=None) -> bool:
    """Download every index in `missing` (resume-safe: pass only what is still missing) over the first route that
    answers; fall through to the next route if one dies mid-way. Routes whose host is not allowed_source() (loopback,
    link-local, public, IPv6, 0.0.0.0/8, 240/4...) are skipped without a connection. store(i, ct) is called once per chunk (serialised);
    progress(done, total) after each one. Returns False if some chunk could not be fetched over any route."""
    if not TID_RE.match(tid):
        return False
    todo = sorted({int(i) for i in missing if 0 <= int(i) < int(n)})
    total = len(todo)
    if not todo:
        return True
    done = [0]
    store_lock = threading.Lock()

    def deliver(i, ct) -> bool:
        with store_lock:
            try:
                store(i, ct)
            except Exception:  # noqa: BLE001 - e.g. decrypt failed: treat as a bad chunk, retry/fall through
                return False
            done[0] += 1
            if progress:
                try:
                    progress(done[0], total)
                except Exception:  # noqa: BLE001
                    pass
        return True

    for r in routes or []:
        try:
            host, port = str(r['host']), int(r['port'])
        except (KeyError, TypeError, ValueError):
            continue
        ip = _addr(host)                 # re-filter: only IPv4 RFC 1918 / Tailscale 100.64/10, whatever the caller passed
        if ip is None or not allowed_source(str(ip)) or not 0 < port < 65536:
            continue
        host = str(ip)
        if should_stop and should_stop():
            return False
        # probe: the first chunk decides whether this route answers at all (short connect budget)
        try:
            ct = fetch_chunk(host, port, tid, todo[0], me_id, key, timeout=min(timeout, 5))
        except (FetchError, OSError, ValueError):
            continue
        if deliver(todo[0], ct):
            todo = todo[1:]
        todo = _pool(host, port, tid, todo, me_id, key, deliver, parallel, timeout, should_stop)
        if not todo:
            return True
    return False


def _pool(host, port, tid, todo, me_id, key, deliver, parallel, timeout, should_stop) -> list:
    """Fetch `todo` over one route with `parallel` keep-alive workers. Returns the indices still missing."""
    q = queue.Queue()
    for i in todo:
        q.put(i)
    left = set(todo)
    left_lock = threading.Lock()
    dead = threading.Event()             # the route stopped answering: leave the rest for the next route
    tries = {}

    def worker():
        conn = None
        while not dead.is_set():
            if should_stop and should_stop():
                dead.set()
                break
            try:
                i = q.get_nowait()
            except queue.Empty:
                break
            try:
                if conn is None:
                    conn = http.client.HTTPConnection(host, port, timeout=timeout)
                ct = _get(conn, tid, i, me_id, key)
                ok = deliver(i, ct)
                err = None if ok else FetchError(0, 'store failed')
            except (FetchError, OSError, http.client.HTTPException) as e:
                err = e if isinstance(e, FetchError) else FetchError(0, type(e).__name__)
                if conn is not None:
                    conn.close()
                    conn = None
            if err is None:
                with left_lock:
                    left.discard(i)
                continue
            with left_lock:
                tries[i] = k = (tries[i] if i in tries else 0) + 1
            if err.code == 429 and k <= 6:
                time.sleep(min(2.0, 0.25 * k))
                q.put(i)
            elif _fatal(err) or k > 2:
                dead.set()
            else:
                time.sleep(0.2 * k)
                q.put(i)
        if conn is not None:
            conn.close()

    ts = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, min(int(parallel), MAX_CONC, len(todo) or 1)))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return sorted(left)
