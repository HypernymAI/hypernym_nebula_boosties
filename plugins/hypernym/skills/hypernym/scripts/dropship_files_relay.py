#!/usr/bin/env python3
"""
DROPSHIP FILES — RELAY transport: big files through the Dropship mailbox (our own ntfy) when two computers can't reach
each other directly. Standard library only (chunks arrive here already encrypted by dropship_files). NO .get().

HOW. Every encrypted chunk is one ntfy attachment on a per-transfer topic:

    PUT {base}/{topic}   body = chunk ciphertext, Filename: <tid>-<i>.bin, X-Message: c<i>, Cache: yes
      → message JSON with attachment {name, size, expires, url}
    GET {base}/{topic}/json?poll=1&since=all     newline-delimited JSON, one line per cached message
    GET {attachment.url}                          the ciphertext (ntfy serves it at {base-url}/file/<id>.bin)

    topic = 'dsp-f-' + sha256(b'dsp-file-topic|' + K).hexdigest()[:24]   (only holders of K can name it)

The mailbox only ever sees the topic name and ciphertext. Each chunk is sealed with a nonce bound to (tid, i), so a
chunk that the server swaps, replays or corrupts fails to decrypt in store() and is simply fetched again or skipped.

RESUME. The topic itself is the progress record: an uploader first polls it and skips every index that is already
published (and not about to expire); a downloader fetches only the indices it is still missing.

SAFETY. Attachment URLs are fetched ONLY when they are on the mailbox's own scheme + host + port and under its /file/
path; anything else is refused (a hostile message could otherwise make us fetch arbitrary URLs). Redirects are never
followed. Nothing here logs or returns payloads, keys, topics or URLs: last_error(tid) is a short reason only.

LIMITS (server: nebula_dropship_mailbox/start.sh). attachment-file-size-limit 16M (a chunk is 8 MiB + 16 bytes),
attachments expire after 24 h, per-IP attachment storage / daily bandwidth caps. RunPod's proxy (Cloudflare) closes a
request after ~100 s, so a chunk must upload within that (8 MiB needs ~85 KB/s). HTTP 429 → wait Retry-After
(shared by every request of the transfer), exactly as dropship_mail.Mail.request does for chat mail.
"""
import email.utils
import hashlib
import http.client
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:                                    # the courier's venv: share the mailbox's own types and user agent
    from dropship_mail import UA, MailboxDeferred
except ImportError:                     # no PyNaCl/spake2 here: same shapes, standard library only
    UA = 'dropship/1 (Nebula Pro)'

    class MailboxDeferred(OSError):
        def __init__(self, reason, retry_at):
            self.retry_at = retry_at
            super().__init__(reason)

MAX_CT = 16 * 1024 * 1024               # server attachment-file-size-limit; never read more than this per chunk
MAX_POLL = 32 * 1024 * 1024             # a poll of 256 small messages is ~100 KB; anything huge is refused
TIMEOUT_S = 95.0                        # just under the RunPod/Cloudflare proxy's ~100 s request limit
POLL_TIMEOUT_S = 30.0
RETRIES = 6                             # transient failures (5xx, dropped connection) per request before giving up
BACKOFF_S, BACKOFF_MAX_S = 2.0, 60.0
POLL_EVERY_S, POLL_MAX_S = 5.0, 20.0    # downloader: wait between polls while the sender is still uploading
FRESH_S = 600.0                         # a published chunk is skipped only if it stays downloadable this long
PARALLEL = 2
ROUNDS = 3                              # upload passes (each re-reads the topic and fills gaps)

_ERRORS = {}
_ERR_LOCK = threading.Lock()


class _Stopped(Exception):
    pass


class _Fatal(Exception):
    pass


def last_error(tid: str) -> str:
    """Short reason for the last failure of a transfer ('' if none). Never contains payloads, keys, topics or URLs."""
    with _ERR_LOCK:
        return _ERRORS[tid] if tid in _ERRORS else ''


def _err(tid: str, reason: str) -> None:
    with _ERR_LOCK:
        _ERRORS[tid] = reason[:200]


def topic_for(K: bytes) -> str:
    if not isinstance(K, (bytes, bytearray)) or len(K) != 32:
        raise ValueError('transfer key must be 32 bytes')
    return 'dsp-f-' + hashlib.sha256(b'dsp-file-topic|' + bytes(K)).hexdigest()[:24]


def _norm_base(base: str) -> str:
    b = str(base).strip().rstrip('/')
    u = urllib.parse.urlsplit(b)
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError('mailbox base must be a plain http(s) URL')
    return b


def _port(u) -> int:
    try:
        p = u.port
    except ValueError:
        return -1
    return p if p is not None else (443 if u.scheme == 'https' else 80)


def same_host(base: str, url: str) -> bool:
    """True only for an ntfy attachment URL on the mailbox itself: same scheme, host and port, under {base}/file/."""
    try:
        b, u = urllib.parse.urlsplit(_norm_base(base)), urllib.parse.urlsplit(str(url))
    except ValueError:
        return False
    if u.scheme != b.scheme or not u.hostname or u.username or u.password or u.query or u.fragment:
        return False
    if u.hostname.lower() != b.hostname.lower() or _port(u) != _port(b) or _port(u) < 0:
        return False
    prefix = b.path.rstrip('/') + '/file/'
    path = u.path
    return path.startswith(prefix) and len(path) > len(prefix) and '/' not in path[len(prefix):] \
        and '..' not in path and '%' not in path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                     # → HTTPError(3xx): never follow a redirect anywhere


_OPENER = urllib.request.build_opener(_NoRedirect)


def _retry_after(headers) -> float:
    now = time.time()
    retry = headers['Retry-After'] if headers is not None and 'Retry-After' in headers else ''
    try:
        delay = float(retry)
    except ValueError:
        try:
            delay = email.utils.parsedate_to_datetime(retry).timestamp() - now
        except (ValueError, TypeError, OverflowError):
            delay = 60
    if not math.isfinite(delay):
        delay = 60
    return now + max(1, delay)


def _open(req, timeout):
    """urlopen without redirects; HTTP 429 → MailboxDeferred(reason, retry_at) like dropship_mail.Mail.request."""
    try:
        return _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code != 429:
            raise
        try:
            info = json.loads(e.read(4096))
        except (ValueError, UnicodeError, OSError, http.client.HTTPException):
            info = {}
        if not isinstance(info, dict):
            info = {}
        reason = str(info['error'] if 'error' in info else 'mailbox rate limit (HTTP 429)')[:160]
        raise MailboxDeferred(reason, _retry_after(e.headers)) from e


class _Ctl:
    """One transfer's shared state: stop/deadline, a cooldown every request honours, and progress counting."""

    def __init__(self, tid, n, progress, should_stop, deadline=0.0):
        self.tid, self.n, self.progress, self.should_stop = tid, n, progress, should_stop
        self.deadline = float(deadline) if deadline else 0.0
        self.until = 0.0                # server asked us to wait (429) until this time
        self.abort = threading.Event()  # a worker hit a fatal error
        self.lock = threading.Lock()
        self.done = 0

    def stopped(self) -> bool:
        if self.abort.is_set() or (self.deadline and time.time() >= self.deadline):
            return True
        try:
            return bool(self.should_stop()) if self.should_stop is not None else False
        except Exception:
            return True

    def wait(self, seconds: float) -> None:
        end = time.time() + max(0.0, seconds)
        if self.deadline and end > self.deadline:
            end = self.deadline
        while True:
            if self.stopped():
                raise _Stopped()
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(0.2, left))

    def tick(self, count: int = 1) -> None:
        with self.lock:
            self.done = min(self.n, self.done + count)
            done = self.done
        if self.progress is not None:
            try:
                self.progress(done, self.n)
            except Exception:
                pass                    # a broken progress bar never breaks a transfer

    def call(self, fn, what: str):
        """fn() with the transfer's retry policy. Raises _Stopped, or _Fatal for a non-retryable / exhausted error."""
        tries = 0
        while True:
            if self.stopped():
                raise _Stopped()
            if self.until > time.time():
                if self.deadline and self.until > self.deadline:
                    _err(self.tid, f'{what}: mailbox asked to wait past the deadline')
                    raise _Stopped()
                self.wait(self.until - time.time())
                continue
            try:
                return fn()
            except MailboxDeferred as e:
                with self.lock:
                    self.until = max(self.until, float(e.retry_at))
                _err(self.tid, f'{what}: {e}')
                continue                # wait for the cooldown at the top of the loop; not a failed try
            except urllib.error.HTTPError as e:
                if e.code < 500 and e.code != 408:
                    _err(self.tid, f'{what}: HTTP {e.code}')
                    raise _Fatal(f'HTTP {e.code}') from None
                tries += 1
                _err(self.tid, f'{what}: HTTP {e.code}')
            except (OSError, http.client.HTTPException, ValueError) as e:
                tries += 1
                _err(self.tid, f'{what}: {type(e).__name__}')
            if tries > RETRIES:
                raise _Fatal(f'{what}: gave up after {RETRIES} retries')
            self.wait(min(BACKOFF_MAX_S, BACKOFF_S * 2 ** (tries - 1)))


def _parallel(items, worker, ctl: _Ctl) -> None:
    items = list(items)
    lock = threading.Lock()

    def run():
        while not ctl.abort.is_set():
            with lock:
                if not items:
                    return
                item = items.pop(0)
            try:
                worker(item)
            except _Stopped:
                ctl.abort.set()
                return
            except _Fatal as e:
                _err(ctl.tid, str(e))
                ctl.abort.set()
                return
    threads = [threading.Thread(target=run, daemon=True) for _ in range(min(PARALLEL, len(items)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def _listing(base: str, topic: str, tid: str, n: int) -> dict:
    """Index → attachments published for it on the topic, newest first: [{'url', 'size', 'expires'}]."""
    req = urllib.request.Request(f'{base}/{topic}/json?poll=1&since=all', headers={'User-Agent': UA})
    with _open(req, POLL_TIMEOUT_S) as r:
        raw = r.read(MAX_POLL + 1)
    if len(raw) > MAX_POLL:
        raise ValueError('poll too large')
    found = {}
    for ln in raw.decode('utf-8', 'replace').splitlines():
        if not ln.strip():
            continue
        try:
            m = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(m, dict) or 'event' not in m or m['event'] != 'message' or 'attachment' not in m:
            continue
        att, text = m['attachment'], m['message'] if 'message' in m else ''
        if not isinstance(att, dict) or 'url' not in att or 'name' not in att or not isinstance(text, str):
            continue
        hit = re.fullmatch(r'c(\d{1,9})', text)
        if not hit:
            continue
        i = int(hit.group(1))
        if not 0 <= i < n or att['name'] != f'{tid}-{i}.bin' or not isinstance(att['url'], str):
            continue
        exp = att['expires'] if 'expires' in att else (m['expires'] if 'expires' in m else 0)
        size = att['size'] if 'size' in att else 0
        when = m['time'] if 'time' in m else 0
        try:
            entry = {'url': att['url'], 'size': int(size), 'expires': float(exp or 0), 'time': float(when)}
        except (TypeError, ValueError):
            continue
        found.setdefault(i, []).append(entry)
    for v in found.values():
        v.sort(key=lambda e: -e['time'])
    return found


def _alive(entry: dict, margin: float) -> bool:
    return not entry['expires'] or entry['expires'] > time.time() + margin


def _put(base: str, topic: str, tid: str, i: int, ct: bytes) -> dict:
    req = urllib.request.Request(f'{base}/{topic}', data=ct, method='PUT', headers={
        'User-Agent': UA, 'Content-Type': 'application/octet-stream', 'Filename': f'{tid}-{i}.bin',
        'X-Message': f'c{i}', 'Cache': 'yes'})
    with _open(req, TIMEOUT_S) as r:
        m = json.loads(r.read(1024 * 1024))
    if not isinstance(m, dict) or 'attachment' not in m or not isinstance(m['attachment'], dict) \
            or 'url' not in m['attachment']:
        raise _Fatal('mailbox did not store the chunk as an attachment (attachments disabled on the server?)')
    if not same_host(base, m['attachment']['url']):
        raise _Fatal('mailbox attachment URL is on another host (server base-url mismatch)')
    if 'size' in m['attachment'] and int(m['attachment']['size']) != len(ct):
        raise ValueError('stored size mismatch')
    return m['attachment']


def _get(url: str) -> bytes:
    with _open(urllib.request.Request(url, headers={'User-Agent': UA}), TIMEOUT_S) as r:
        data = r.read(MAX_CT + 1)
    if len(data) > MAX_CT:
        raise _Fatal('attachment larger than the chunk limit')
    return data


def upload_all(base, K, tid, n, chunk_for, progress, should_stop) -> bool:
    """Publish chunks 0..n-1 (chunk_for(i) → ciphertext bytes) as attachments on the transfer's topic. Resumable:
    indices already on the topic (and not about to expire) are skipped. progress(done, n) after each chunk.
    True once every index is published; False on stop, a fatal server answer, or retries exhausted."""
    base, topic, n = _norm_base(base), topic_for(K), int(n)
    ctl = _Ctl(tid, n, progress, should_stop)
    try:
        for _ in range(ROUNDS):
            have = ctl.call(lambda: _listing(base, topic, tid, n), 'poll')
            todo = [i for i in range(n) if i not in have or not any(_alive(e, FRESH_S) for e in have[i])]
            ctl.done = 0
            ctl.tick(n - len(todo))
            if not todo:
                with _ERR_LOCK:
                    _ERRORS.pop(tid, None)
                return True

            def one(i):
                try:
                    ct = chunk_for(i)
                except Exception as e:      # the source changed or vanished: not something a retry fixes
                    raise _Fatal(f'chunk {i}: source unavailable ({type(e).__name__})') from None
                if not isinstance(ct, (bytes, bytearray)) or len(ct) > MAX_CT:
                    raise _Fatal(f'chunk {i}: bad ciphertext size')
                ctl.call(lambda: _put(base, topic, tid, i, bytes(ct)), f'upload {i}')
                ctl.tick()
            _parallel(todo, one, ctl)
            if ctl.abort.is_set() or ctl.stopped():
                return False
        have = ctl.call(lambda: _listing(base, topic, tid, n), 'poll')
        return all(i in have and any(_alive(e, 0) for e in have[i]) for i in range(n))
    except (_Stopped, _Fatal) as e:
        if isinstance(e, _Fatal):
            _err(tid, str(e))
        return False
    except ValueError as e:
        _err(tid, f'upload: {e}')
        return False


def download_all(base, K, tid, n, missing, store, progress, should_stop, deadline) -> bool:
    """Fetch the chunks in `missing` from the transfer's topic as the sender publishes them; store(i, ct) for each
    (store raising = bad chunk: that attachment is skipped and another copy awaited). Only attachment URLs on the
    mailbox's own host are fetched. progress(done, n). deadline: unix time to give up (0/None = only should_stop).
    True when nothing is missing any more."""
    base, topic, n = _norm_base(base), topic_for(K), int(n)
    want = set()
    for i in missing:
        if 0 <= int(i) < n:
            want.add(int(i))
    ctl = _Ctl(tid, n, progress, should_stop, deadline or 0.0)
    ctl.tick(n - len(want))
    bad, refused = set(), set()
    store_lock, want_lock = threading.Lock(), threading.Lock()
    delay = POLL_EVERY_S
    try:
        while True:
            with want_lock:
                if not want:
                    with _ERR_LOCK:
                        _ERRORS.pop(tid, None)
                    return True
                need = sorted(want)
            if ctl.stopped():
                return False
            try:
                have = ctl.call(lambda: _listing(base, topic, tid, n), 'poll')
            except _Fatal:
                have = {}
            jobs = []
            for i in need:
                for e in have[i] if i in have else ():
                    if e['url'] in bad or not _alive(e, 5) or e['size'] > MAX_CT:
                        continue
                    if not same_host(base, e['url']):
                        if e['url'] not in refused:
                            refused.add(e['url'])
                            _err(tid, f'chunk {i}: attachment on another host refused')
                        continue
                    jobs.append((i, e['url']))
                    break
            if not jobs:
                ctl.wait(delay)
                delay = min(POLL_MAX_S, delay * 1.5)
                continue
            delay = POLL_EVERY_S

            def one(job):
                i, url = job
                try:
                    ct = ctl.call(lambda: _get(url), f'download {i}')
                except _Fatal:
                    bad.add(url)            # expired/removed/oversized: wait for (or ask for) another copy
                    return
                with store_lock:
                    try:
                        store(i, ct)
                    except Exception as e:
                        bad.add(url)
                        _err(tid, f'chunk {i}: rejected ({type(e).__name__})')
                        return
                with want_lock:
                    want.discard(i)
                ctl.tick()
            _parallel(jobs, one, ctl)
            if ctl.abort.is_set():
                return False
    except _Stopped:
        return False
