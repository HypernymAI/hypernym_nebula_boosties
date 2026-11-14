#!/usr/bin/env python3
"""
DROPSHIP MAIL — friends anywhere: end-to-end encrypted messages through a public mailbox (ntfy, https://ntfy.sh by
default; a self-hosted ntfy is one config line: config.json "mailbox"), plus pairing with a short spoken code.

Loaded ONLY by the Dropship daemon, from its private environment (~/.hypernym/dropship/venv, built with uv):
PyNaCl 1.5.0 (libsodium: Curve25519 + XSalsa20-Poly1305) and spake2 0.9 (SPAKE2, the PAKE Magic Wormhole uses).
The status line and hooks never import this (they stay standard library). NO .get().

KEYS. Each machine has a Curve25519 key pair (identity.json "box_priv"/"box_pub"). Two friends' Box (ECDH + Poly1305)
both encrypts and authenticates every message: the mailbox sees only random topic names and ciphertext — it can't
read, forge or tamper with anything. Friends made on the home Wi-Fi exchange box keys in their beacons, so they keep
talking through the mailbox when they're apart; anyone else pairs once with a code.

TOPICS. Per ordered pair: "dsp-" + BLAKE2b(shared key ‖ from id ‖ to id)[:20 bytes] — unguessable without the key.
Each daemon holds ONE streaming subscription to all of its inbound topics (ntfy JSON stream), resumed from the time
of the last message it saw, so up to the mailbox's cache window (12 h on ntfy.sh) of offline time is caught up.

PAIRING ("hypernym pair" → 7-neptune-googly-blimp). Rendezvous topic = "dsp-pair-" + SHA-256(code)[:24 hex]; both
sides post their SPAKE2 message, derive the same 32-byte key only if they used the same code (one guess per attempt
for an attacker; nothing to brute-force offline), then exchange {id, name, box_pub, lan_pub} inside a SecretBox under
that key. A wrong code fails to decrypt → "pairing failed". Codes expire after PAIR_S.

MESSAGES. Plaintext {k: "msg"|"ping", mid, from, to, ts, i, n, body} in a Box, base64, ≤ CHUNK characters of body
per mailbox message (long messages are split and reassembled by mid). Timestamps outside ±12 h and repeated mids are
dropped. Presence: a tiny encrypted "ping" to each distant friend every PING_S.
"""
import base64
import hashlib
import email.utils
import dropship as storage
import json
import math
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import nacl.exceptions
import nacl.public
import nacl.secret
from spake2 import SPAKE2_Symmetric

DEFAULT_MAILBOX = 'https://ntfy.sh'
PAIR_S, PING_S, CHUNK, SKEW_S = 600.0, 3600.0, 1500, 12 * 3600
UA = 'dropship/1 (Nebula Pro)'
WORDS = ('anchor arrow aurora badge beacon blimp bloom breeze bubble cactus candle canyon cedar cherry cinder clover '
         'comet copper coral cosmos crane crystal dawn delta dune echo ember fable falcon feather fern fjord flare '
         'fossil galaxy garnet geyser glacier glimmer googly grove harbor hazel helix heron honey horizon iris ivory '
         'jade jasper jelly kayak kelp kite lagoon lantern lark lemon lilac lotus lunar maple marble meadow meteor '
         'mint mist moss nebula nectar neptune nimbus nova oasis ocean olive onyx orbit otter pebble pepper petal '
         'pixel planet plum polar prism pulsar quartz quill radar raven reef ripple river rocket ruby saffron sage '
         'sail shell sierra silver sky slate sonar spark spruce star summit swift tango thistle tide topaz trident '
         'tulip tundra umber valley velvet vortex wave willow wisp zephyr zinnia').split()


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip('=')


def unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def new_code() -> str:
    return f"{secrets.randbelow(98) + 2}-" + '-'.join(secrets.choice(WORDS) for _ in range(3))


def normalize(code: str) -> str:
    return '-'.join(code.strip().lower().replace(' ', '-').split('-'))


def new_box_keys() -> tuple:
    k = nacl.public.PrivateKey.generate()
    return bytes(k).hex(), bytes(k.public_key).hex()


class MailboxDeferred(OSError):
    def __init__(self, reason, retry_at):
        self.retry_at = retry_at
        super().__init__(reason)



class Mail:
    """The mailbox side of the daemon. `c` is the dropship Courier (friends, inbox, logging, locks)."""

    def __init__(self, c, base: str):
        self.c = c
        self.base = base.rstrip('/')
        self.priv = nacl.public.PrivateKey(bytes.fromhex(c.me['box_priv']))
        self.journal_path = storage.HOME / 'mailbox-receive.json'
        journal = storage.load(self.journal_path, {})
        self.done = journal.get('done', {})
        self.parts = journal.get('parts', {})
        self.limit_path = storage.HOME / 'mailbox-limits.json'
        self.limits = storage.load(self.limit_path, {})
        self.presence_path = storage.HOME / 'mailbox-presence.json'
        self.presence = storage.load(self.presence_path, {})
        self.send_lock = threading.Lock()
        self.limit_lock = threading.Lock()
        self.last_seen = {}                # friend id → last time we heard from them through the mailbox
        self.since = journal.get('since', int(time.time()) - 12 * 3600)
        self.stream = None
        self.topics_now = ()
        self.error = ''

    # -- keys and topics
    def box(self, friend: dict):
        return nacl.public.Box(self.priv, nacl.public.PublicKey(bytes.fromhex(friend['box'])))

    def topic(self, friend: dict, outbound: bool) -> str:
        a, b = (self.c.me['id'], friend['id']) if outbound else (friend['id'], self.c.me['id'])
        shared = self.box(friend).shared_key()
        return 'dsp-' + hashlib.blake2b(shared + a.encode() + b'|' + b.encode(), digest_size=20).hexdigest()

    def mail_friends(self) -> list:
        with self.c.lock:
            return [dict(v, id=k) for k, v in self.c.friends.items() if 'box' in v]

    # -- HTTP
    def checkpoint(self):
        now = time.time()
        self.done = {k: v for k, v in self.done.items() if now - v < 2 * SKEW_S}
        self.parts = {k: v for k, v in self.parts.items() if now - v['ts'] < 2 * SKEW_S}
        storage.save(self.journal_path, {'since': self.since, 'done': self.done, 'parts': self.parts})

    def retry_at(self, sending=True):
        with self.limit_lock:
            kinds = ('requests', 'messages') if sending else ('requests',)
            return max((self.limits.get(k, {}).get('until', 0) for k in kinds), default=0)

    def limit_status(self):
        with self.limit_lock:
            active = [v for v in self.limits.values() if v.get('until', 0) > time.time()]
            return dict(max(active, key=lambda v: v['until'])) if active else {}

    def request(self, req, timeout, sending=False):
        until = self.retry_at(sending)
        if until > time.time():
            raise MailboxDeferred('mailbox cooldown; request queued', until)
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            raw = e.read(4096)
            try:
                info = json.loads(raw)
            except (ValueError, UnicodeError):
                info = {}
            if not isinstance(info, dict):
                info = {}
            now = time.time()
            retry = e.headers.get('Retry-After', '')
            try:
                delay = float(retry)
            except ValueError:
                try:
                    delay = email.utils.parsedate_to_datetime(retry).timestamp() - now
                except (ValueError, TypeError, OverflowError):
                    delay = 60
            if not math.isfinite(delay):
                delay = 60
            until = now + max(1, delay)
            kind = 'messages' if info.get('code') == 42908 or '"messages"' in e.headers.get('RateLimit-Policy', '') else 'requests'
            reason = str(info.get('error', 'provider rate limit (HTTP 429)'))[:240]
            with self.limit_lock:
                self.limits[kind] = {'until': until, 'reason': reason, 'code': info.get('code', 429),
                                     'policy': e.headers.get('RateLimit-Policy', '')[:160]}
                storage.save(self.limit_path, self.limits)
            raise MailboxDeferred(reason, until) from e

    def post(self, topic: str, text: str) -> None:
        req = urllib.request.Request(f'{self.base}/{topic}', data=text.encode(), method='POST',
                                     headers={'User-Agent': UA, 'Content-Type': 'text/plain'})
        with self.request(req, timeout=10, sending=True) as r:
            r.read()

    def poll(self, topic: str, since: str) -> list:
        url = f'{self.base}/{topic}/json?poll=1&since={urllib.parse.quote(since)}'
        with self.request(urllib.request.Request(url, headers={'User-Agent': UA}), timeout=15) as r:
            out = []
            for ln in r.read().decode().splitlines():
                m = json.loads(ln)
                if m['event'] == 'message':
                    out.append(m)
            return out

    # -- sending
    def send(self, friend: dict, body: str, kind: str = 'msg', to_s: str = '', from_s: str = '', job=None) -> None:
        # Keep the same message ID and chunk progress across queued retries.
        with self.send_lock:
            box, topic = self.box(friend), self.topic(friend, True)
            mid = job['mid'] if job is not None else secrets.token_hex(8)
            chunks = [body[i:i + CHUNK] for i in range(0, len(body), CHUNK)] or ['']
            start = job.get('next_chunk', 0) if job is not None else 0
            for i in range(start, len(chunks)):
                env = {'k': kind, 'mid': mid, 'from': self.c.me['id'], 'to': friend['id'], 'ts': time.time(),
                       'i': i, 'n': len(chunks), 'body': chunks[i], 'to_s': to_s, 'from_s': from_s,
                       'name': self.c.me['name'], 'sessions': self.c.sessions_now()}
                self.post(topic, 'dsp1.' + b64(box.encrypt(json.dumps(env).encode())))
                if job is not None:
                    job['next_chunk'] = i + 1
                    storage.save(storage.OUTBOX / job['filename'], job)
            self.presence[friend['id']] = time.time()
            storage.save(self.presence_path, self.presence)

    def pinger(self):
        while not self.c.stop.is_set():
            for f in self.mail_friends():
                if self.c.on_lan(f['id']) or time.time() - self.presence.get(f['id'], 0) < PING_S:
                    continue
                # User messages get priority; never spend quota on pings during cooldown.
                if self.retry_at() > time.time() or any(storage.OUTBOX.glob('*.json')):
                    continue
                try:
                    self.send(f, json.dumps({'sessions': self.c.sessions_now(), 'name': self.c.me['name']}), 'ping')
                except (OSError, ValueError):
                    pass  # Receiver availability is independent of publishing quotas.
            self.c.stop.wait(60)

    # -- receiving: one streaming subscription to every inbound topic
    def listen(self):
        failures = 0
        while not self.c.stop.is_set():
            friends = {self.topic(f, False): f for f in self.mail_friends()}
            if not friends:
                self.c.stop.wait(5)
                continue
            self.topics_now = tuple(sorted(friends))
            url = f"{self.base}/{','.join(self.topics_now)}/json?since={self.since}"
            try:
                with self.request(urllib.request.Request(url, headers={'User-Agent': UA}), timeout=90) as r:
                    self.stream, self.error = r, ''
                    failures = 0
                    for ln in r:
                        if self.c.stop.is_set():
                            break
                        m = json.loads(ln)
                        if m['event'] == 'message' and m['topic'] in friends:
                            self.since = max(self.since, int(m['time']) - 1)
                            self.receive(friends[m['topic']], m['message'])
                            self.checkpoint()
                        if tuple(sorted(self.topic(f, False) for f in self.mail_friends())) != self.topics_now:
                            break                                 # a new friend: resubscribe with their topic
            except (OSError, ValueError) as e:
                self.error = f'mailbox: {e}'[:240]
                failures += 1
                delay = max(1, e.retry_at - time.time()) if isinstance(e, MailboxDeferred) else min(300, 10 * 2 ** min(failures - 1, 5))
                self.c.stop.wait(delay)
            finally:
                self.stream = None

    def receive(self, friend: dict, text: str) -> None:
        if not text.startswith('dsp1.'):
            return
        try:
            env = json.loads(self.box(friend).decrypt(unb64(text[5:])))
            kind, mid, i, n = env['k'], str(env['mid']), int(env['i']), int(env['n'])
            if env['from'] != friend['id'] or env['to'] != self.c.me['id'] or not 0 <= i < n <= 64:
                return
            if abs(time.time() - float(env['ts'])) > SKEW_S:
                return
        except (nacl.exceptions.CryptoError, ValueError, KeyError, TypeError):
            return                                                # not from this friend, tampered, or garbage
        self.last_seen[friend['id']] = time.time()
        if 'name' in env:
            self.c.peer_renamed(friend['id'], env['name'])
        if isinstance(env.get('sessions'), list):
            self.c.peer_sessions[friend['id']] = [str(x)[:32] for x in env['sessions'][:20]]
        if kind == 'ping':
            try:
                presence = json.loads(env['body']) if env['body'] else {}
                self.c.peer_renamed(friend['id'], presence.get('name'))
                self.c.peer_sessions[friend['id']] = [str(x)[:32] for x in presence.get('sessions', [])[:20]]
            except (ValueError, KeyError, TypeError):
                pass
            return
        if kind != 'msg':
            return
        key = friend['id'] + ':' + mid
        if key in self.done:
            return
        got = self.parts.setdefault(key, {'ts': time.time(), 'n': n, 'chunks': {},
                                         'to_s': str(env.get('to_s', '')), 'from_s': str(env.get('from_s', ''))})
        if got['n'] != n or got['to_s'] != str(env.get('to_s', '')) or got['from_s'] != str(env.get('from_s', '')):
            return
        got['chunks'][str(i)] = str(env['body'])
        if len(got['chunks']) == n:
            self.c.deliver(self.c.friends[friend['id']]['name'], ''.join(got['chunks'][str(k)] for k in range(n)),
                           'mailbox', float(env['ts']), got['to_s'], got['from_s'])
            self.done[key] = time.time()
            del self.parts[key]
        self.checkpoint()

    # -- pairing
    def pair(self, code: str, done, wanted=lambda: True) -> None:
        """Run one pairing attempt for `code` (both sides run the same thing); done(status, detail). Stops early when
        wanted() turns False (the person started another pairing)."""
        code = normalize(code)
        topic = 'dsp-pair-' + hashlib.sha256(f'dropship-pair-v1|{code}'.encode()).hexdigest()[:24]
        side = secrets.token_hex(6)
        s = SPAKE2_Symmetric(code.encode(), idSymmetric=b'dropship-v1')
        started = time.time()
        try:
            self.post(topic, json.dumps({'t': 'pake', 'side': side, 'm': b64(s.start())}))
            key, sent_id = None, False
            while time.time() - started < PAIR_S and not self.c.stop.is_set():
                if not wanted():
                    return done('cancelled', 'replaced by a newer pairing')
                for m in self.poll(topic, '15m'):
                    try:
                        msg = json.loads(m['message'])
                    except ValueError:
                        continue
                    if msg['side'] == side:
                        continue
                    if msg['t'] == 'pake' and key is None:
                        key = s.finish(unb64(msg['m']))
                    if key is not None and not sent_id:
                        me = self.c.me
                        card = json.dumps({'id': me['id'], 'name': me['name'], 'box': me['box_pub'], 'pub': me['pub']})
                        self.post(topic, json.dumps({'t': 'id', 'side': side,
                                                     'c': b64(nacl.secret.SecretBox(key).encrypt(card.encode()))}))
                        sent_id = True
                    if msg['t'] == 'id' and key is not None:
                        try:
                            card = json.loads(nacl.secret.SecretBox(key).decrypt(unb64(msg['c'])))
                        except nacl.exceptions.CryptoError:
                            return done('failed', 'the code did not match — check it and try again')
                        name = self.c.add_friend(card, 'paired')
                        return done('done', name)
                self.c.stop.wait(4)
            done('failed', 'nobody joined with that code in time')
        except (OSError, ValueError, KeyError, TypeError) as e:
            done('failed', f'mailbox: {e}'[:160])
