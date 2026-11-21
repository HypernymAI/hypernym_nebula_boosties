#!/usr/bin/env python3
"""Dropship large files: direct transport (stdlib only; local 127.0.0.1 servers, mocked Tailscale CLI).

    python3 tests/dropship/test_files_direct.py
"""
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

RUN = Path(__file__).resolve().parent / '.run'
RUN.mkdir(exist_ok=True)
os.environ['DROPSHIP_HOME'] = tempfile.mkdtemp(prefix='files-direct-home-', dir=RUN)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins' / 'hypernym' / 'skills' / 'hypernym' / 'scripts'))
import dropship_files_direct as fd  # noqa: E402

TID = 'fx_' + '0123456789abcdef'
ALICE, BOB, EVE = 'a' * 16, 'b' * 16, 'e' * 16
KEY_A = b'k' * 32
LO = '127.0.0.1'


def free_port() -> int:
    with socket.socket() as s:
        s.bind((LO, 0))
        return s.getsockname()[1]


class Sender:
    """A FileServer over in-memory 'ciphertext' chunks, recording what was asked for."""

    def __init__(self, chunks, delay=0.0):
        self.chunks, self.delay = chunks, delay
        self.asked, self.live, self.peak = [], 0, 0
        self.lk = threading.Lock()
        self.fs = fd.FileServer(self.key_for, self.chunk_for, port=0)

    @staticmethod
    def key_for(peer):
        if peer == ALICE:
            return KEY_A
        if peer == EVE:
            raise ValueError('bad public key')
        return None

    def chunk_for(self, tid, i, peer):
        with self.lk:
            self.asked.append(i)
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            if self.delay:
                time.sleep(self.delay)
            if tid != TID:
                raise KeyError(tid)
            if i == 666:
                raise PermissionError('not yours')
            return self.chunks[i]
        finally:
            with self.lk:
                self.live -= 1

    def start(self):
        (host, port), = self.fs.start([LO])
        return host, port


class Base(unittest.TestCase):
    def setUp(self):
        fd._ALLOW_LOOPBACK = True
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.fs.stop()
        fd._ALLOW_LOOPBACK = False

    def sender(self, chunks, delay=0.0):
        s = Sender(chunks, delay)
        self.servers.append(s)
        return s, s.start()


class ServerTests(Base):
    def test_good_fetch(self):
        s, (h, p) = self.sender({0: b'hello', 1: b''})
        self.assertEqual(fd.fetch_chunk(h, p, TID, 0, ALICE, KEY_A), b'hello')
        self.assertEqual(fd.fetch_chunk(h, p, TID, 1, ALICE, KEY_A), b'')

    def test_wrong_mac(self):
        s, (h, p) = self.sender({0: b'x'})
        with self.assertRaises(fd.FetchError) as cm:
            fd.fetch_chunk(h, p, TID, 0, ALICE, b'wrong' * 7)
        self.assertEqual(cm.exception.code, 403)
        self.assertEqual(s.asked, [])

    def test_unknown_peer_and_bad_pub(self):
        s, (h, p) = self.sender({0: b'x'})
        for who in (BOB, EVE):
            with self.assertRaises(fd.FetchError) as cm:
                fd.fetch_chunk(h, p, TID, 0, who, KEY_A)
            self.assertEqual(cm.exception.code, 403)
        self.assertEqual(s.asked, [])

    def test_stale_timestamp(self):
        s, (h, p) = self.sender({0: b'x'})
        for skew in (-121, 121):
            with mock.patch.object(fd.time, 'time', return_value=time.time() + skew):
                hdr = fd.auth_headers(KEY_A, TID, 0, ALICE)
            code, detail = s.fs.check(f'/f/{TID}/0', hdr, LO)[:2]
            self.assertEqual((code, detail), (403, 'stale'))
        hdr = fd.auth_headers(KEY_A, TID, 0, ALICE)
        self.assertEqual(s.fs.check(f'/f/{TID}/0', hdr, LO)[0], 200)
        s.fs.release(ALICE)

    def test_missing_or_malformed_headers(self):
        s, _ = self.sender({0: b'x'})
        good = fd.auth_headers(KEY_A, TID, 0, ALICE)
        for drop in ('X-Dsp-From', 'X-Dsp-Ts', 'X-Dsp-Mac'):
            hdr = {k: v for k, v in good.items() if k != drop}
            self.assertEqual(s.fs.check(f'/f/{TID}/0', hdr, LO)[0], 403)
        hdr = dict(good, **{'X-Dsp-Mac': 'é' * 64})
        self.assertEqual(s.fs.check(f'/f/{TID}/0', hdr, LO)[0], 403)
        for bad in ('/f/../etc/0', f'/f/{TID}/-1', f'/f/{TID}/x', '/f/fx_ZZ/0', f'/f/{TID}/0?x=1'):
            self.assertEqual(s.fs.check(bad, good, LO)[0], 404)

    def test_non_private_source_refused(self):
        s, (h, p) = self.sender({0: b'x'})
        hdr = fd.auth_headers(KEY_A, TID, 0, ALICE)
        for addr in ('8.8.8.8', '169.254.1.1', '::1', '2001:db8::1', 'garbage'):
            self.assertEqual(s.fs.check(f'/f/{TID}/0', hdr, addr)[:2], (403, 'not a private address'))
        for addr in ('10.1.2.3', '172.20.0.1', '192.168.1.9', '100.100.1.1', '::ffff:192.168.1.9'):
            self.assertEqual(s.fs.check(f'/f/{TID}/0', hdr, addr)[0], 200, addr)
            s.fs.release(ALICE)
        fd._ALLOW_LOOPBACK = False          # production: loopback is NOT an allowed source
        self.assertFalse(fd.allowed_source(LO))
        with self.assertRaises(fd.FetchError) as cm:
            fd.fetch_chunk(h, p, TID, 0, ALICE, KEY_A)
        self.assertEqual(cm.exception.code, 403)
        self.assertEqual(s.asked, [])

    def test_chunk_for_errors(self):
        s, (h, p) = self.sender({0: b'x'})
        with self.assertRaises(fd.FetchError) as cm:
            fd.fetch_chunk(h, p, TID, 666, ALICE, KEY_A)
        self.assertEqual(cm.exception.code, 403)
        with self.assertRaises(fd.FetchError) as cm:
            fd.fetch_chunk(h, p, TID, 5, ALICE, KEY_A)
        self.assertEqual(cm.exception.code, 404)
        with self.assertRaises(fd.FetchError) as cm:
            fd.fetch_chunk(h, p, 'fx_ffffffffffffffff', 0, ALICE, KEY_A)
        self.assertEqual(cm.exception.code, 404)
        self.assertEqual(s.fs.conc, {})     # slots released after errors

    def test_rate_limit(self):
        s, (h, p) = self.sender({0: b'x'})
        with mock.patch.object(fd, 'RATE_N', 3):
            for _ in range(3):
                fd.fetch_chunk(h, p, TID, 0, ALICE, KEY_A)
            with self.assertRaises(fd.FetchError) as cm:
                fd.fetch_chunk(h, p, TID, 0, ALICE, KEY_A)
            self.assertEqual(cm.exception.code, 429)

    def test_failed_auth_throttle(self):
        s, (h, p) = self.sender({0: b'x'})
        with mock.patch.object(fd, 'FAIL_N', 3):
            for _ in range(3):
                with self.assertRaises(fd.FetchError):
                    fd.fetch_chunk(h, p, TID, 0, ALICE, b'nope')
            with self.assertRaises(fd.FetchError) as cm:
                fd.fetch_chunk(h, p, TID, 0, ALICE, KEY_A)     # even a good request, from that address, for a while
            self.assertEqual(cm.exception.code, 429)

    def test_rebind(self):
        s = Sender({0: b'x'})
        self.servers.append(s)
        fs = s.fs
        self.assertEqual(fs.start(['0.0.0.0', '8.8.8.8', '', None]), [])     # never wildcard / public
        (h, p), = fs.start([LO])
        first = fs.servers[LO]
        self.assertEqual(fs.rebind([LO, LO]), [(h, p)])
        self.assertIs(fs.servers[LO], first)                                 # unchanged address: not restarted
        self.assertEqual(fd.fetch_chunk(h, p, TID, 0, ALICE, KEY_A), b'x')
        self.assertEqual(fs.rebind(['10.255.255.254']), [])                  # not a local address: skipped, old closed
        with self.assertRaises(OSError):
            fd.fetch_chunk(h, p, TID, 0, ALICE, KEY_A, timeout=2)
        (h2, p2), = fs.rebind([LO])
        self.assertEqual(fd.fetch_chunk(h2, p2, TID, 0, ALICE, KEY_A), b'x')
        fs.stop()
        self.assertEqual(fs.bound, [])

    def test_fixed_port_from_env_default(self):
        self.assertEqual(fd.FileServer(None, None).port, fd.FILE_PORT)


class FetchAllTests(Base):
    def chunks(self, n=12, size=50_000):
        return {i: os.urandom(size) for i in range(n)}

    def run_fetch(self, routes, n, missing, parallel=4, key=KEY_A):
        got, prog = {}, []
        ok = fd.fetch_all(routes, TID, n, missing, ALICE, key, lambda i, ct: got.__setitem__(i, ct),
                          lambda d, t: prog.append((d, t)), parallel=parallel, timeout=5)
        return ok, got, prog

    def test_parallel(self):
        ch = self.chunks()
        s, (h, p) = self.sender(ch, delay=0.05)
        ok, got, prog = self.run_fetch([{'host': h, 'port': p}], 12, range(12))
        self.assertTrue(ok)
        self.assertEqual(got, ch)
        self.assertGreaterEqual(s.peak, 2)
        self.assertLessEqual(s.peak, 4)
        self.assertEqual(prog[-1], (12, 12))
        self.assertEqual(sorted(s.asked), list(range(12)))

    def test_resume_only_missing(self):
        ch = self.chunks()
        s, (h, p) = self.sender(ch)
        ok, got, _ = self.run_fetch([{'host': h, 'port': p}], 12, [2, 5, 11, 99, -1])
        self.assertTrue(ok)
        self.assertEqual(sorted(s.asked), [2, 5, 11])
        self.assertEqual(set(got), {2, 5, 11})
        ok, got, _ = self.run_fetch([{'host': h, 'port': p}], 12, [])
        self.assertTrue(ok)
        self.assertEqual(got, {})

    def test_dead_route_falls_through(self):
        ch = self.chunks(6)
        s, (h, p) = self.sender(ch)
        dead = {'host': LO, 'port': free_port()}
        refusing = {'host': h, 'port': p}
        ok, got, _ = self.run_fetch([dead, {'nonsense': 1}, refusing], 6, range(6))
        self.assertTrue(ok)
        self.assertEqual(got, ch)

    def test_wrong_key_route_then_good(self):
        ch = self.chunks(3)
        bad_s, (bh, bp) = self.sender(ch)
        bad_s.key_for = None
        bad_s.fs.key_for = lambda peer: b'other' * 7
        s, (h, p) = self.sender(ch)
        ok, got, _ = self.run_fetch([{'host': bh, 'port': bp}, {'host': h, 'port': p}], 3, range(3))
        self.assertTrue(ok)
        self.assertEqual(got, ch)
        self.assertEqual(bad_s.asked, [])

    def test_no_route_works(self):
        ok, got, _ = self.run_fetch([{'host': LO, 'port': free_port()}, {'host': LO, 'port': free_port()}], 3, [0, 1])
        self.assertFalse(ok)
        self.assertFalse(self.run_fetch([], 3, [0])[0])

    def test_partial_then_resume(self):
        ch = self.chunks(5)
        s, (h, p) = self.sender({i: ch[i] for i in (0, 1, 2)})     # chunks 3, 4 404 on this route
        ok, got, _ = self.run_fetch([{'host': h, 'port': p}], 5, range(5), parallel=1)
        self.assertFalse(ok)
        missing = [i for i in range(5) if i not in got]
        self.assertTrue(set(missing) >= {3, 4})
        s2, (h2, p2) = self.sender(ch)
        ok, got2, _ = self.run_fetch([{'host': h2, 'port': p2}], 5, missing)
        self.assertTrue(ok)
        self.assertEqual(sorted(s2.asked), missing)
        got.update(got2)
        self.assertEqual(got, ch)

    def test_forbidden_routes_never_contacted(self):
        """fetch_all re-filters whatever routes it is given: only IPv4 RFC 1918 / 100.64/10 are dialled."""
        ch = self.chunks(2)
        s, (h, p) = self.sender(ch)
        fd._ALLOW_LOOPBACK = False                       # production: loopback is not a route
        tried = []
        real_fetch, real_conn = fd.fetch_chunk, fd.http.client.HTTPConnection

        def spy_fetch(host, *a, **kw):
            tried.append(host)
            raise fd.FetchError(0, 'spy')

        def spy_conn(host, *a, **kw):
            tried.append(host)
            raise OSError('spy')
        bad = ['127.0.0.1', LO, 'localhost', '::1', 'fe80::1', 'fd00::1', '169.254.1.1', '0.0.0.5', '0.1.2.3',
               '240.0.0.1', '255.255.255.255', '224.0.0.1', '8.8.8.8', '::ffff:127.0.0.1', '198.18.0.1', '100.128.0.1']
        try:
            fd.fetch_chunk = spy_fetch
            fd.http.client.HTTPConnection = spy_conn
            ok, got, _ = self.run_fetch([{'host': b, 'port': p} for b in bad], 2, [0, 1])
        finally:
            fd.fetch_chunk, fd.http.client.HTTPConnection = real_fetch, real_conn
        self.assertFalse(ok)
        self.assertEqual(tried, [])
        self.assertEqual(s.asked, [])
        # an allowed private route IS dialled (normalised from its IPv4-mapped form)
        tried.clear()
        try:
            fd.fetch_chunk = spy_fetch
            self.run_fetch([{'host': '::ffff:192.168.77.1', 'port': p}, {'host': '10.9.9.9', 'port': 0}], 2, [0])
        finally:
            fd.fetch_chunk = real_fetch
        self.assertEqual(tried, ['192.168.77.1'])

    def test_store_failure_retried_then_gives_up(self):
        ch = self.chunks(2)
        s, (h, p) = self.sender(ch)
        calls = []

        def store(i, ct):
            calls.append(i)
            raise ValueError('decrypt failed')
        ok = fd.fetch_all([{'host': h, 'port': p}], TID, 2, [0, 1], ALICE, KEY_A, store, None, timeout=5)
        self.assertFalse(ok)
        self.assertLess(len(calls), 12)


class TailscaleTests(unittest.TestCase):
    def setUp(self):
        fd._ts_cache.update(ip='', at=0.0, valid=False)

    @staticmethod
    def done(out, rc=0):
        return subprocess.CompletedProcess([], rc, stdout=out, stderr='')

    def test_present(self):
        with mock.patch.object(fd.subprocess, 'run', return_value=self.done('100.101.102.103\n')) as run:
            self.assertEqual(fd.tailscale_ip(), '100.101.102.103')
        self.assertEqual(run.call_args[0][0], ['tailscale', 'ip', '-4'])
        self.assertEqual(run.call_args[1]['timeout'], 2)

    def test_absent_cli_answers_without_address(self):
        with mock.patch.object(fd, '_PLATFORM', 'darwin'), \
                mock.patch.object(fd.subprocess, 'run', return_value=self.done('', 1)) as run:
            self.assertEqual(fd.tailscale_ip(), '')
        self.assertEqual(run.call_count, 2)          # rc != 0 → also asks the app CLI

    def test_not_a_tailscale_address_ignored(self):
        with mock.patch.object(fd.subprocess, 'run', return_value=self.done('192.168.1.5\nnot an ip\n')):
            self.assertEqual(fd.tailscale_ip(), '')

    def test_cli_missing(self):
        with mock.patch.object(fd, '_PLATFORM', 'linux'), \
                mock.patch.object(fd.subprocess, 'run', side_effect=FileNotFoundError):
            self.assertEqual(fd.tailscale_ip(), '')

    def test_timeout(self):
        with mock.patch.object(fd, '_PLATFORM', 'linux'), \
                mock.patch.object(fd.subprocess, 'run', side_effect=subprocess.TimeoutExpired('tailscale', 2)):
            self.assertEqual(fd.tailscale_ip(), '')

    def test_app_path_fallback(self):
        def run(argv, **_kw):
            if argv[0] == 'tailscale':
                raise FileNotFoundError
            self.assertEqual(argv, ['/Applications/Tailscale.app/Contents/MacOS/Tailscale', 'ip', '-4'])
            return self.done('100.64.0.7\n')
        with mock.patch.object(fd, '_PLATFORM', 'darwin'), mock.patch.object(fd.subprocess, 'run', side_effect=run):
            self.assertEqual(fd.tailscale_ip(), '100.64.0.7')

    def test_windows_candidate(self):
        with mock.patch.object(fd, '_PLATFORM', 'win32'):
            c = fd._cli_candidates()
        self.assertEqual(c[0], 'tailscale')
        self.assertTrue(c[1].lower().endswith('tailscale.exe'))

    def test_caching(self):
        with mock.patch.object(fd.subprocess, 'run', return_value=self.done('100.70.0.1\n')) as run:
            self.assertEqual(fd.tailscale_ip(), '100.70.0.1')
            self.assertEqual(fd.tailscale_ip(), '100.70.0.1')
            self.assertEqual(run.call_count, 1)
            fd._ts_cache['at'] -= fd.TS_CACHE_S + 1
            fd.tailscale_ip()
            self.assertEqual(run.call_count, 2)
        with mock.patch.object(fd.subprocess, 'run', side_effect=FileNotFoundError) as run:   # '' is cached too
            fd._ts_cache.update(valid=False)
            fd.tailscale_ip()
            fd.tailscale_ip()
            self.assertLessEqual(run.call_count, 2)

    def test_my_routes(self):
        with mock.patch.object(fd, 'tailscale_ip', return_value='100.90.0.2'):
            self.assertEqual(fd.my_routes('192.168.1.4', True, port=5000),
                             [{'host': '192.168.1.4', 'port': 5000}, {'host': '100.90.0.2', 'port': 5000}])
            self.assertEqual(fd.my_routes('192.168.1.4', False), [{'host': '100.90.0.2', 'port': fd.FILE_PORT}])
        with mock.patch.object(fd, 'tailscale_ip', return_value=''):
            self.assertEqual(fd.my_routes('192.168.1.4', True), [{'host': '192.168.1.4', 'port': fd.FILE_PORT}])
            self.assertEqual(fd.my_routes('', True), [])
            self.assertEqual(fd.my_routes('8.8.8.8', True), [])


if __name__ == '__main__':
    unittest.main()
