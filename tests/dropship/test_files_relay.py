#!/usr/bin/env python3
"""Dropship file relay through ntfy attachments, against a local fake ntfy (no production mailbox).

    tests/dropship/.run/venv/bin/python tests/dropship/test_files_relay.py
"""
import hashlib
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RUN = Path(__file__).resolve().parent / '.run'
RUN.mkdir(exist_ok=True)
os.environ['DROPSHIP_HOME'] = tempfile.mkdtemp(prefix='relay-home-', dir=RUN)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins' / 'hypernym' / 'skills' / 'hypernym' / 'scripts'))
import dropship_files_relay as rl  # noqa: E402

rl.BACKOFF_S, rl.BACKOFF_MAX_S = 0.05, 0.2
rl.POLL_EVERY_S, rl.POLL_MAX_S = 0.05, 0.1


class FakeNtfy:
    """Just enough of ntfy: PUT {topic} (attachment), GET {topic}/json?poll=1, GET /file/<id>.bin.
    actions[kind] (kind = put | poll | file) is a list of faults applied to the next requests of that kind:
    '429' (Retry-After: 1), '500', '413', 'drop' (close the connection without an answer), 'redirect'."""

    def __init__(self, attach_base=None):
        self.topics, self.files, self.log = {}, {}, []
        self.actions = {'put': [], 'poll': [], 'file': []}
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def fault(self, kind):
                with fake.lock:
                    fake.log.append((time.time(), kind, self.path))
                    act = fake.actions[kind].pop(0) if fake.actions[kind] else ''
                if act == 'drop':
                    self.close_connection = True
                    return True
                if act == '429':
                    body = json.dumps({'code': 42901, 'http': 429, 'error': 'limit reached'}).encode()
                    self.send_response(429)
                    self.send_header('Retry-After', '1')
                elif act in ('500', '413'):
                    body = b'{"code":50001,"http":500,"error":"boom"}'
                    self.send_response(int(act))
                elif act == 'redirect':
                    body = b''
                    self.send_response(302)
                    self.send_header('Location', 'http://198.51.100.7/file/x.bin')
                else:
                    return False
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return True

            def do_PUT(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                if self.fault('put'):
                    return
                topic = self.path.strip('/')
                mid = secrets.token_hex(6)
                now = int(time.time())
                base = fake.attach_base or fake.base
                msg = {'id': mid, 'time': now, 'expires': now + 86400, 'event': 'message', 'topic': topic,
                       'message': self.headers['X-Message'] or '',
                       'attachment': {'name': self.headers['Filename'], 'type': 'application/octet-stream',
                                      'size': len(body), 'expires': now + 86400, 'url': f'{base}/file/{mid}.bin'}}
                with fake.lock:
                    fake.files[mid] = body
                    fake.topics.setdefault(topic, []).append(msg)
                out = json.dumps(msg).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                if u.path.startswith('/file/'):
                    if self.fault('file'):
                        return
                    mid = u.path[len('/file/'):].split('.')[0]
                    with fake.lock:
                        data = fake.files[mid] if mid in fake.files else None
                    if data is None:
                        self.send_response(404)
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if u.path.endswith('/json'):
                    if self.fault('poll'):
                        return
                    q = urllib.parse.parse_qs(u.query)
                    assert q['poll'] == ['1'] and q['since'] == ['all'], q
                    topic = u.path[1:-len('/json')]
                    with fake.lock:
                        msgs = list(fake.topics[topic]) if topic in fake.topics else []
                    out = ''.join(json.dumps(m) + '\n' for m in msgs).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
                self.send_response(404)
                self.send_header('Content-Length', '0')
                self.end_headers()

        self.attach_base = attach_base
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.base = f'http://127.0.0.1:{self.srv.server_address[1]}'
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def count(self, kind):
        with self.lock:
            return sum(1 for e in self.log if e[1] == kind)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def make_chunks(n):
    return [secrets.token_bytes(1000 + 37 * i) for i in range(n)]


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeNtfy()
        self.K = secrets.token_bytes(32)
        self.tid = 'fx_' + secrets.token_hex(8)

    def tearDown(self):
        self.fake.close()

    def up(self, chunks, should_stop=lambda: False, progress=None, calls=None):
        def chunk_for(i):
            if calls is not None:
                calls.append(i)
            return chunks[i]
        return rl.upload_all(self.fake.base + '/', self.K, self.tid, len(chunks), chunk_for, progress, should_stop)

    def down(self, n, missing=None, deadline=None, should_stop=lambda: False, store=None, K=None):
        got, prog = {}, []

        def default_store(i, ct):
            got[i] = ct
        ok = rl.download_all(self.fake.base, K or self.K, self.tid, n, list(range(n)) if missing is None else missing,
                             store or default_store, lambda d, t: prog.append((d, t)), should_stop,
                             deadline if deadline is not None else time.time() + 10)
        return ok, got, prog

    # -- topic
    def test_topic_derived_from_key(self):
        want = 'dsp-f-' + hashlib.sha256(b'dsp-file-topic|' + self.K).hexdigest()[:24]
        self.assertEqual(rl.topic_for(self.K), want)
        self.assertNotEqual(rl.topic_for(secrets.token_bytes(32)), want)
        self.assertTrue(self.up(make_chunks(2)))
        self.assertEqual(list(self.fake.topics), [want])
        with self.assertRaises(ValueError):
            rl.topic_for(b'short')
        # someone without K can't even find the chunks
        ok, got, _ = self.down(2, deadline=time.time() + 0.4, K=secrets.token_bytes(32))
        self.assertFalse(ok)
        self.assertEqual(got, {})
        self.assertEqual(self.fake.count('file'), 0)

    # -- round trip
    def test_round_trip(self):
        chunks = make_chunks(6)
        prog = []
        self.assertTrue(self.up(chunks, progress=lambda d, t: prog.append((d, t))))
        self.assertEqual(prog[-1], (6, 6))
        msgs = self.fake.topics[rl.topic_for(self.K)]
        self.assertEqual(sorted(m['message'] for m in msgs), sorted(f'c{i}' for i in range(6)))
        self.assertEqual({m['attachment']['name'] for m in msgs}, {f'{self.tid}-{i}.bin' for i in range(6)})
        ok, got, dprog = self.down(6)
        self.assertTrue(ok)
        self.assertEqual([got[i] for i in range(6)], chunks)
        self.assertEqual(dprog[-1], (6, 6))

    def test_download_waits_for_slow_uploader(self):
        chunks = make_chunks(4)
        res = {}
        t = threading.Thread(target=lambda: res.update(zip(('ok', 'got', 'p'), self.down(4))))
        t.start()
        time.sleep(0.3)
        self.assertTrue(self.up(chunks))
        t.join(10)
        self.assertTrue(res['ok'])
        self.assertEqual([res['got'][i] for i in range(4)], chunks)

    # -- resume
    def test_upload_resume_skips_published(self):
        chunks = make_chunks(5)
        topic = rl.topic_for(self.K)
        for i in (0, 2):
            rl._put(self.fake.base, topic, self.tid, i, chunks[i])
        calls = []
        self.assertTrue(self.up(chunks, calls=calls))
        self.assertEqual(sorted(calls), [1, 3, 4])
        self.assertEqual(self.fake.count('put'), 5)       # 2 before + 3 now

    def test_upload_reuploads_expiring_chunk(self):
        chunks = make_chunks(2)
        self.assertTrue(self.up(chunks))
        first = [m for m in self.fake.topics[rl.topic_for(self.K)] if m['attachment']['name'] == f'{self.tid}-0.bin']
        self.assertEqual(len(first), 1)                   # uploads run in parallel: find chunk 0 by name, not position
        first[0]['attachment']['expires'] = int(time.time()) + 30
        calls = []
        self.assertTrue(self.up(chunks, calls=calls))
        self.assertEqual(calls, [0])

    def test_download_only_fetches_missing(self):
        chunks = make_chunks(5)
        self.assertTrue(self.up(chunks))
        ok, got, prog = self.down(5, missing=[1, 3])
        self.assertTrue(ok)
        self.assertEqual(sorted(got), [1, 3])
        self.assertEqual(got[1], chunks[1])
        self.assertEqual(self.fake.count('file'), 2)
        self.assertEqual(prog[0], (3, 5))
        self.assertEqual(prog[-1], (5, 5))
        ok, got, _ = self.down(5, missing=[])
        self.assertTrue(ok)
        self.assertEqual(self.fake.count('file'), 2)

    def test_bad_chunk_rejected_then_good_copy_used(self):
        chunks = make_chunks(2)
        self.assertTrue(self.up(chunks))
        rl._put(self.fake.base, rl.topic_for(self.K), self.tid, 0, b'garbage-from-server')
        self.fake.topics[rl.topic_for(self.K)][-1]['time'] += 5           # newest copy is the bad one

        def store(i, ct):
            if ct not in chunks:
                raise ValueError('decrypt failed')
            got[i] = ct
        got = {}
        ok, _, _ = self.down(2, store=store)
        self.assertTrue(ok)
        self.assertEqual(got, {0: chunks[0], 1: chunks[1]})

    # -- limits and faults
    def test_429_backoff_upload(self):
        self.fake.actions['put'] = ['429']
        t0 = time.time()
        self.assertTrue(self.up(make_chunks(3)))
        self.assertGreaterEqual(time.time() - t0, 0.9)
        log = self.fake.log
        hit = next(k for k, e in enumerate(log) if e[1] == 'put')
        t429 = log[hit][0]
        # the other worker's request may already be in flight; after that, nothing is sent during the cooldown
        quiet = [e for e in log if t429 + 0.1 < e[0] < t429 + 0.9]
        self.assertEqual(quiet, [])
        self.assertTrue(any(e[0] >= t429 + 0.9 for e in log))
        self.assertEqual(rl.last_error(self.tid), '')                   # cleared once the upload succeeded

    def test_429_backoff_download_and_poll(self):
        chunks = make_chunks(3)
        self.assertTrue(self.up(chunks))
        self.fake.actions['poll'] = ['429']
        self.fake.actions['file'] = ['429']
        t0 = time.time()
        ok, got, _ = self.down(3)
        self.assertTrue(ok)
        self.assertGreaterEqual(time.time() - t0, 1.8)
        self.assertEqual([got[i] for i in range(3)], chunks)

    def test_429_past_deadline_gives_up(self):
        self.fake.actions['poll'] = ['429']
        t0 = time.time()
        ok, _, _ = self.down(2, deadline=time.time() + 0.3)
        self.assertFalse(ok)
        self.assertLess(time.time() - t0, 0.9)

    def test_5xx_and_dropped_connections_retried(self):
        chunks = make_chunks(4)
        self.fake.actions['put'] = ['500', 'drop', '500']
        self.fake.actions['poll'] = ['drop']
        self.assertTrue(self.up(chunks))
        self.fake.actions['file'] = ['drop', '500']
        self.fake.actions['poll'] = ['500']
        ok, got, _ = self.down(4)
        self.assertTrue(ok)
        self.assertEqual([got[i] for i in range(4)], chunks)

    def test_client_error_is_fatal(self):
        self.fake.actions['put'] = ['413']
        t0 = time.time()
        self.assertFalse(self.up(make_chunks(3)))
        self.assertLess(time.time() - t0, 2)
        self.assertIn('413', rl.last_error(self.tid))

    # -- foreign hosts
    def test_foreign_attachment_url_refused(self):
        other = FakeNtfy()
        try:
            chunks = make_chunks(2)
            self.assertTrue(self.up(chunks))
            for m in self.fake.topics[rl.topic_for(self.K)]:      # a hostile/misconfigured server points elsewhere
                m['attachment']['url'] = m['attachment']['url'].replace(self.fake.base, other.base)
            other.files.update(self.fake.files)
            ok, got, _ = self.down(2, deadline=time.time() + 0.5)
            self.assertFalse(ok)
            self.assertEqual(got, {})
            self.assertEqual(other.count('file'), 0)
            self.assertEqual(self.fake.count('file'), 0)
            self.assertIn('another host', rl.last_error(self.tid))
            self.assertNotIn('http', rl.last_error(self.tid))
        finally:
            other.close()

    def test_upload_refuses_server_with_foreign_base_url(self):
        self.fake.attach_base = 'http://localhost:9'
        self.assertFalse(self.up(make_chunks(2)))
        self.assertIn('another host', rl.last_error(self.tid))

    def test_redirect_not_followed(self):
        chunks = make_chunks(1)
        self.assertTrue(self.up(chunks))
        self.fake.actions['file'] = ['redirect']
        ok, got, _ = self.down(1, deadline=time.time() + 0.5)
        self.assertFalse(ok)                              # the only copy is now marked bad; nothing fetched elsewhere

    def test_same_host_rules(self):
        b = 'https://abc-8080.proxy.runpod.net'
        ok = rl.same_host
        self.assertTrue(ok(b, b + '/file/Xy12.bin'))
        self.assertTrue(ok(b + '/', 'https://ABC-8080.proxy.runpod.net:443/file/Xy12.bin'))
        for bad in ('http://abc-8080.proxy.runpod.net/file/Xy12.bin', 'https://evil.net/file/Xy12.bin',
                    'https://abc-8080.proxy.runpod.net.evil.net/file/a.bin', 'https://abc-8080.proxy.runpod.net:8443/file/a',
                    'https://u:p@abc-8080.proxy.runpod.net/file/a.bin', b + '/_admin/access.log', b + '/file/',
                    b + '/file/../_admin/x', b + '/file/a.bin?x=1', b + '/file/a/b.bin', b + '/file/%2e%2e',
                    'file:///etc/passwd', '', 'nonsense'):
            self.assertFalse(ok(b, bad), bad)
        self.assertTrue(ok('https://h.example/ntfy', 'https://h.example/ntfy/file/a.bin'))
        self.assertFalse(ok('https://h.example/ntfy', 'https://h.example/file/a.bin'))

    # -- stop and deadline
    def test_upload_should_stop(self):
        chunks = make_chunks(10)
        calls = []
        self.assertFalse(self.up(chunks, should_stop=lambda: len(calls) >= 2, calls=calls))
        self.assertLess(self.fake.count('put'), 10)
        # and a later run resumes from what made it to the topic
        calls2 = []
        self.assertTrue(self.up(chunks, calls=calls2))
        self.assertEqual(sorted(set(calls) | set(calls2)), list(range(10)))
        self.assertLess(len(calls2), 10)

    def test_download_should_stop_and_deadline(self):
        stop = {'v': False}
        threading.Timer(0.3, lambda: stop.update(v=True)).start()
        t0 = time.time()
        ok, _, _ = self.down(3, deadline=time.time() + 30, should_stop=lambda: stop['v'])
        self.assertFalse(ok)
        self.assertLess(time.time() - t0, 1.5)
        t0 = time.time()
        ok, _, _ = self.down(3, deadline=time.time() + 0.3)
        self.assertFalse(ok)
        self.assertLess(time.time() - t0, 1.5)

    def test_no_payload_in_errors(self):
        chunks = [b'SECRET-PAYLOAD-' + secrets.token_bytes(8)]
        self.fake.actions['put'] = ['413']
        self.assertFalse(self.up(chunks))
        err = rl.last_error(self.tid)
        self.assertNotIn('SECRET', err)
        self.assertNotIn(rl.topic_for(self.K), err)
        self.assertNotIn(self.K.hex(), err)


if __name__ == '__main__':
    unittest.main(verbosity=2)
