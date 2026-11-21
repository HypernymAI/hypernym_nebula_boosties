#!/usr/bin/env python3
"""Large-file transfer core (needs PyNaCl: run with the test venv's python).

    tests/dropship/.run/venv/bin/python tests/dropship/test_files.py
"""
import base64
import hashlib
import importlib
import json
import os
import random
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

RUN = Path(__file__).resolve().parent / '.run'
RUN.mkdir(exist_ok=True)
HOME = tempfile.mkdtemp(prefix='files-home-', dir=RUN)
os.environ['DROPSHIP_HOME'] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins' / 'hypernym' / 'skills' / 'hypernym' / 'scripts'))
import dropship_files as df  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix='files-work-', dir=RUN))
SENDER_ID, RECEIVER_ID, MALLORY = 'a' * 16, 'b' * 16, 'c' * 16
SMALL = 1024


def setup_module_paths():
    """Point the landing folder at a temp dir and use small chunks."""
    df.DOWNLOADS = WORK / 'Downloads'
    df.CHUNK = SMALL


def make_file(name: str, size: int, seed: int = 1) -> Path:
    p = WORK / 'src' / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(random.Random(seed).randbytes(size))
    return p


def transfer(o: dict, tid: str, order=None):
    """Move every chunk sender → receiver (same process plays both)."""
    idx = list(range(o['n'])) if order is None else order
    for i in idx:
        df.store_chunk(tid, SENDER_ID, i, df.serve_chunk(tid, i, RECEIVER_ID))


def clear_incoming():
    """Each test starts with no incoming records (the open-offer caps count them)."""
    for folder in (df.IN, df.PARTIAL):
        if folder.exists():
            for f in folder.iterdir():
                f.unlink()


def case_insensitive(p: Path) -> bool:
    flipped = Path(str(p).swapcase())
    return str(flipped) != str(p) and flipped.exists() and os.path.samefile(flipped, p)


class Base(unittest.TestCase):
    def setUp(self):
        setup_module_paths()
        clear_incoming()


class CryptoTests(Base):
    def test_round_trip_and_binding(self):
        import nacl.utils
        K, K2 = nacl.utils.random(32), nacl.utils.random(32)
        tid, tid2 = 'fx_' + '1' * 16, 'fx_' + '2' * 16
        ct = df.encrypt_chunk(K, tid, 3, b'hello world')
        self.assertNotIn(b'hello', ct)
        self.assertEqual(len(ct), len(b'hello world') + 16)
        self.assertEqual(df.decrypt_chunk(K, tid, 3, ct), b'hello world')
        for args in ((K, tid, 4, ct), (K, tid2, 3, ct), (K2, tid, 3, ct), (K, tid, 3, ct[:-1] + bytes([ct[-1] ^ 1]))):
            with self.assertRaises(ValueError):
                df.decrypt_chunk(*args)
        self.assertEqual(df.decrypt_chunk(K, tid, 0, df.encrypt_chunk(K, tid, 0, b'')), b'')

    def test_nonce_layout(self):
        tid = 'fx_' + 'a' * 16
        n = df._nonce(tid, 5)
        self.assertEqual(len(n), 24)
        self.assertEqual(n[:16], hashlib.sha256(b'dsp-file-nonce|' + tid.encode()).digest()[:16])
        self.assertEqual(n[16:], (5).to_bytes(8, 'big'))

    def test_chunk_count(self):
        self.assertEqual(df.chunk_count(0, 10), 1)
        self.assertEqual(df.chunk_count(10, 10), 1)
        self.assertEqual(df.chunk_count(11, 10), 2)
        self.assertEqual(df.chunk_count(1, 0), 1)
        df.CHUNK = 8 * 1024 * 1024
        self.assertEqual(df.chunk_count(8 * 1024 * 1024 + 1), 2)

    def test_read_plain_chunk(self):
        p = make_file('r.bin', 2500)
        data = p.read_bytes()
        self.assertEqual(df.read_plain_chunk(p, 2, 1000), data[2000:])
        self.assertEqual(df.read_plain_chunk(p, 0, 1000), data[:1000])


class TransferTests(Base):
    def test_multi_chunk_out_of_order_with_restart(self):
        src = make_file('big.bin', SMALL * 5 + 123, seed=7)
        tid, o = df.offer(str(src), 'alice', RECEIVER_ID, 'work', label='big one')
        self.assertTrue(df.ID_RE.match(tid))
        self.assertEqual(o['n'], 6)
        self.assertEqual(o['chunk'], SMALL)
        self.assertEqual(o['sha256'], hashlib.sha256(src.read_bytes()).hexdigest())
        self.assertEqual(len(base64.b64decode(o['key'])), 32)
        out = json.loads((df.OUT / f'{tid}.json').read_text())
        self.assertEqual(out['to_id'], RECEIVER_ID)
        self.assertEqual(stat.S_IMODE((df.OUT / f'{tid}.json').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(df.OUT.stat().st_mode), 0o700)

        self.assertEqual(df.offer_in(SENDER_ID, 'bob', json.loads(json.dumps(o)), auto=False), tid)
        self.assertEqual(df.in_record(tid, SENDER_ID)['status'], 'pending')
        with self.assertRaises(ValueError):
            df.store_chunk(tid, SENDER_ID, 0, df.serve_chunk(tid, 0, RECEIVER_ID))   # not accepted yet
        df.accept(tid, SENDER_ID)
        part = df.PARTIAL / f'{SENDER_ID}-{tid}.part'
        self.assertEqual(part.stat().st_size, src.stat().st_size)
        self.assertEqual(stat.S_IMODE(part.stat().st_mode), 0o600)

        for i in (4, 1, 5):
            self.assertTrue(df.store_chunk(tid, SENDER_ID, i, df.serve_chunk(tid, i, RECEIVER_ID)))
        self.assertFalse(df.store_chunk(tid, SENDER_ID, 4, df.serve_chunk(tid, 4, RECEIVER_ID)))   # idempotent
        self.assertEqual(df.missing(tid, SENDER_ID), [0, 2, 3])

        # simulated restart: fresh module state, everything resumes from disk
        importlib.reload(df)
        setup_module_paths()
        self.assertEqual(df.missing(tid, SENDER_ID), [0, 2, 3])
        self.assertEqual(df.in_record(tid, SENDER_ID)['status'], 'transferring')
        threads = [threading.Thread(target=lambda i=i: df.store_chunk(tid, SENDER_ID, i,
                                                                       df.serve_chunk(tid, i, RECEIVER_ID)))
                   for i in (3, 0, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(df.missing(tid, SENDER_ID), [])
        path = Path(df.finish(tid, SENDER_ID))
        self.assertEqual(path, WORK / 'Downloads' / 'Dropship' / 'bob' / 'big.bin')
        self.assertEqual(path.read_bytes(), src.read_bytes())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse(part.exists())
        self.assertEqual(df.finish(tid, SENDER_ID), str(path))           # idempotent
        r = df.receipt(tid, SENDER_ID)
        self.assertEqual(r, {'id': tid, 'status': 'saved', 'sha256': o['sha256']})
        self.assertEqual(df.receipt_in(MALLORY, r), '')                   # receipts only from the recipient
        self.assertEqual(df.receipt_in(RECEIVER_ID, dict(r, sha256='0' * 64)), '')
        self.assertEqual(df.receipt_in(RECEIVER_ID, r), 'saved')
        self.assertEqual(df.out_record(tid)['status'], 'saved')
        with self.assertRaises(PermissionError):
            df.serve_chunk(tid, 0, RECEIVER_ID)                           # closed after the receipt

    def test_empty_file(self):
        src = make_file('empty.txt', 0)
        tid, o = df.offer(str(src), 'alice', RECEIVER_ID, '')
        self.assertEqual(o['n'], 1)
        df.offer_in(SENDER_ID, 'bob', o, auto=True)
        self.assertEqual(df.in_record(tid, SENDER_ID)['status'], 'accepted')
        transfer(o, tid)
        self.assertEqual(Path(df.finish(tid, SENDER_ID)).read_bytes(), b'')

    def test_sha_mismatch_refused(self):
        src = make_file('s.bin', SMALL * 2 + 5)
        tid, o = df.offer(str(src), 'alice', RECEIVER_ID, '')
        o = dict(o, sha256=hashlib.sha256(b'something else').hexdigest())
        df.offer_in(SENDER_ID, 'bob', o, auto=True)
        transfer(o, tid)
        with self.assertRaises(ValueError) as cm:
            df.finish(tid, SENDER_ID)
        self.assertNotIsInstance(cm.exception, df.FinishError)
        self.assertEqual(str(cm.exception), 'fingerprint mismatch')
        rec = df.in_record(tid, SENDER_ID)
        self.assertEqual((rec['status'], rec['reason']), ('failed', 'fingerprint mismatch'))
        self.assertFalse((df.PARTIAL / f'{SENDER_ID}-{tid}.part').exists())
        self.assertFalse((WORK / 'Downloads' / 'Dropship' / 'bob' / 's.bin').exists())
        self.assertEqual(df.receipt(tid, SENDER_ID), {'id': tid, 'status': 'failed', 'sha256': ''})

    def test_chunk_from_other_transfer_rejected(self):
        a = make_file('a.bin', SMALL + 1, seed=1)
        tid1, o1 = df.offer(str(a), 'alice', RECEIVER_ID, '')
        tid2, o2 = df.offer(str(a), 'alice', RECEIVER_ID, '')
        df.offer_in(SENDER_ID, 'bob', o1, auto=True)
        with self.assertRaises(ValueError):
            df.store_chunk(tid1, SENDER_ID, 0, df.serve_chunk(tid2, 0, RECEIVER_ID))
        with self.assertRaises(ValueError):
            df.store_chunk(tid1, SENDER_ID, 1, df.serve_chunk(tid1, 0, RECEIVER_ID))
        with self.assertRaises(ValueError):
            df.store_chunk(tid1, SENDER_ID, 2, b'x' * 40)                 # out of range
        self.assertEqual(df.missing(tid1, SENDER_ID), [0, 1])

    def test_serve_chunk_guards(self):
        src = make_file('g.bin', SMALL * 2)
        tid, o = df.offer(str(src), 'alice', RECEIVER_ID, '')
        with self.assertRaises(PermissionError):
            df.serve_chunk(tid, 0, MALLORY)
        with self.assertRaises(ValueError):
            df.serve_chunk(tid, 2, RECEIVER_ID)
        df.serve_chunk(tid, 1, RECEIVER_ID)
        src.write_bytes(b'z' * (SMALL * 2 + 1))                          # size changes
        with self.assertRaises(ValueError):
            df.serve_chunk(tid, 0, RECEIVER_ID)
        src2 = make_file('g2.bin', SMALL)
        tid2, _ = df.offer(str(src2), 'alice', RECEIVER_ID, '')
        st = src2.stat()
        src2.write_bytes(b'q' * SMALL)                                    # same size, new mtime
        os.utime(src2, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        with self.assertRaises(ValueError):
            df.serve_chunk(tid2, 0, RECEIVER_ID)

    def test_offer_source_safety(self):
        (WORK / 'adir').mkdir(exist_ok=True)
        with self.assertRaises(ValueError):
            df.offer(str(WORK / 'adir'), 'alice', RECEIVER_ID, '')
        real = make_file('real.bin', 10)
        link = WORK / 'src' / 'link.bin'
        if not link.exists():
            link.symlink_to(real)
        with self.assertRaises(ValueError):
            df.offer(str(link), 'alice', RECEIVER_ID, '')
        with self.assertRaises(ValueError):
            df.offer(str(Path(HOME) / 'identity.json'), 'alice', RECEIVER_ID, '')
        with self.assertRaises(ValueError):
            df.offer(str(real), 'alice', 'NOT-AN-ID', '')
        with self.assertRaises(ValueError):
            df.offer(str(real), 'alice', RECEIVER_ID, '', kind='folder')

    def test_never_overwrite(self):
        land = WORK / 'Downloads' / 'Dropship' / 'carol'
        land.mkdir(parents=True, exist_ok=True)
        (land / 'notes.txt').write_bytes(b'mine')
        (land / 'notes (2).txt').write_bytes(b'mine too')
        src = make_file('notes.txt', 30, seed=3)
        paths = []
        for _ in range(2):
            tid, o = df.offer(str(src), 'alice', RECEIVER_ID, '')
            df.offer_in(SENDER_ID, 'carol', o, auto=True)
            transfer(o, tid)
            paths.append(Path(df.finish(tid, SENDER_ID)))
        self.assertEqual([p.name for p in paths], ['notes (3).txt', 'notes (4).txt'])
        self.assertEqual((land / 'notes.txt').read_bytes(), b'mine')
        self.assertEqual((land / 'notes (2).txt').read_bytes(), b'mine too')

    def test_dest_dir_and_symlinks(self):
        src = make_file('d.bin', 50)
        real = WORK / 'realdest'
        real.mkdir(exist_ok=True)
        link = WORK / 'linkdest'
        if not link.exists():
            link.symlink_to(real, target_is_directory=True)
        tid, o = df.offer(str(src), 'alice', RECEIVER_ID, '')
        df.offer_in(SENDER_ID, 'bob', o, auto=False)
        with self.assertRaises(ValueError):
            df.accept(tid, SENDER_ID, str(link))
        with self.assertRaises(ValueError):
            df.accept(tid, SENDER_ID, str(link / 'sub'))
        self.assertEqual(df.in_record(tid, SENDER_ID)['status'], 'pending')
        df.accept(tid, SENDER_ID, str(real))
        transfer(o, tid)
        self.assertEqual(Path(df.finish(tid, SENDER_ID)), real / 'd.bin')

    def test_default_landing_through_symlink_refused(self):
        other = WORK / 'elsewhere'
        other.mkdir(exist_ok=True)
        (WORK / 'Downloads' / 'Dropship').mkdir(parents=True, exist_ok=True)
        trap = WORK / 'Downloads' / 'Dropship' / 'eve'
        if not trap.exists():
            trap.symlink_to(other, target_is_directory=True)
        src = make_file('e.bin', 20)
        tid, o = df.offer(str(src), 'alice', RECEIVER_ID, '')
        df.offer_in(SENDER_ID, 'eve', o, auto=True)
        transfer(o, tid)
        with self.assertRaises(df.FinishError):
            df.finish(tid, SENDER_ID)
        self.assertEqual(list(other.iterdir()), [])
        self.assertEqual(df.in_record(tid, SENDER_ID)['status'], 'failed')
        self.assertIn('symlink', df.in_record(tid, SENDER_ID)['reason'])
        self.assertEqual(df.receipt(tid, SENDER_ID)['status'], 'failed')

    def test_refuse(self):
        src = make_file('f.bin', 20)
        tid, o = df.offer(str(src), 'alice', RECEIVER_ID, '')
        df.offer_in(SENDER_ID, 'bob', o, auto=True)
        df.refuse(tid, SENDER_ID)
        self.assertEqual(df.in_record(tid, SENDER_ID)['status'], 'refused')
        self.assertFalse((df.PARTIAL / f'{SENDER_ID}-{tid}.part').exists())
        with self.assertRaises(ValueError):
            df.store_chunk(tid, SENDER_ID, 0, df.serve_chunk(tid, 0, RECEIVER_ID))
        with self.assertRaises(ValueError):
            df.accept(tid, SENDER_ID)
        self.assertEqual(df.receipt(tid, SENDER_ID), {'id': tid, 'status': 'refused', 'sha256': ''})

    def test_repeated_offer_does_not_overwrite(self):
        src = make_file('h.bin', 20)
        tid, o = df.offer(str(src), 'alice', RECEIVER_ID, '')
        df.offer_in(SENDER_ID, 'bob', o, auto=False)
        df.refuse(tid, SENDER_ID)
        with self.assertRaises(df.Duplicate) as cm:
            df.offer_in(SENDER_ID, 'bob', o, auto=True)
        self.assertEqual(cm.exception.tid, tid)
        self.assertIs(df.DUPLICATE, df.Duplicate)
        self.assertEqual(df.in_record(tid, SENDER_ID)['status'], 'refused')

    def test_message_kind(self):
        src = make_file('long.md', SMALL * 3 + 7, seed=9)
        tid, o = df.offer(str(src), 'alice', RECEIVER_ID, 'work', kind='message')
        self.assertEqual(o['kind'], 'message')
        df.offer_in(SENDER_ID, 'bob', o, auto=True)
        transfer(o, tid, order=[3, 2, 1, 0])
        p = Path(df.finish(tid, SENDER_ID))
        self.assertEqual(p, Path(HOME) / 'files' / 'messages' / f'bob-{tid}.md')
        self.assertEqual(p.read_bytes(), src.read_bytes())
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_contents_never_logged(self):
        src = WORK / 'src' / 'secretish.txt'
        src.write_bytes(b'CANARY-CONTENTS-9f8e7d')
        tid, o = df.offer(str(src), 'alice', RECEIVER_ID, '')
        df.offer_in(SENDER_ID, 'bob', o, auto=True)
        transfer(o, tid)
        df.finish(tid, SENDER_ID)
        for p in Path(HOME).rglob('*'):
            if p.is_file() and p.suffix != '.md':
                self.assertNotIn(b'CANARY-CONTENTS', p.read_bytes(), str(p))

    def test_active_progress(self):
        df._active_at[0] = 0.0
        src = make_file('p.bin', SMALL * 3)
        tid, o = df.offer(str(src), 'alice', RECEIVER_ID, '')
        df.offer_in(SENDER_ID, 'bob', o, auto=True)
        df.store_chunk(tid, SENDER_ID, 0, df.serve_chunk(tid, 0, RECEIVER_ID))
        rows = {(r['dir'], r['id']): r for r in df.active()}
        self.assertIn(('out', tid), rows)
        mine = [r for r in json.loads(df.ACTIVE_FILE.read_text()) if r['id'] == tid]
        self.assertTrue(mine)
        df.store_chunk(tid, SENDER_ID, 1, df.serve_chunk(tid, 1, RECEIVER_ID))
        before = df.ACTIVE_FILE.stat().st_mtime_ns
        df._flush()                                                       # throttled: within a second, no rewrite
        self.assertEqual(df.ACTIVE_FILE.stat().st_mtime_ns, before)
        ct = df.serve_chunk(tid, 2, RECEIVER_ID)
        df._active_at[0] = 0.0
        df.store_chunk(tid, SENDER_ID, 2, ct)
        rows = {(r['dir'], r['id']): r for r in df.active()}
        self.assertEqual(rows[('in', tid)]['done_bytes'], SMALL * 3)
        self.assertEqual(rows[('in', tid)]['size'], SMALL * 3)
        self.assertEqual(rows[('in', tid)]['peer'], 'bob')
        self.assertEqual(set(rows[('in', tid)]), {'id', 'name', 'dir', 'done_bytes', 'size', 'peer'})
        df.finish(tid, SENDER_ID)
        self.assertNotIn(('in', tid), {(r['dir'], r['id']) for r in df.active()})


class OfferValidationTests(Base):
    def good(self, **kw):
        size = kw.pop('size', 10)
        o = {'t': 'dsp-file/1', 'id': 'fx_' + os.urandom(8).hex(), 'name': 'report.pdf', 'size': size,
             'sha256': 'ab' * 32, 'chunk': SMALL, 'n': df.chunk_count(size, SMALL),
             'key': base64.b64encode(os.urandom(32)).decode(), 'label': 'report', 'kind': 'file',
             'direct': [{'host': '192.168.1.20', 'port': 47779}, {'host': '100.101.102.103', 'port': 47779}],
             'relay': True, 'to_s': 'work'}
        o.update(kw)
        return o

    def test_good_offer(self):
        o = self.good()
        tid = df.offer_in(SENDER_ID, 'bob', o, auto=False)
        r = df.in_record(tid, SENDER_ID)
        self.assertEqual((r['status'], r['from_id'], r['have'], r['dest']), ('pending', SENDER_ID, [], ''))
        self.assertEqual(len(r['direct']), 2)

    def test_bad_offers(self):
        bad = [
            {'name': '../x'}, {'name': '.hidden'}, {'name': 'a/b'}, {'name': 'a\\b'}, {'name': ''}, {'name': '..'},
            {'name': 'x' * 201}, {'name': 'nul\x00byte'}, {'name': ' padded'}, {'name': 5},
            {'size': -1}, {'size': True}, {'size': '10'}, {'size': 1 << 41, 'n': df.chunk_count(1 << 41, SMALL)},
            {'n': 2}, {'n': 0}, {'size': SMALL * 3, 'n': 2},
            {'id': 'fx_123'}, {'id': '../fx_0000000000000000'}, {'t': 'dsp-file/2'},
            {'sha256': 'zz' * 32}, {'sha256': 'ab' * 31}, {'chunk': SMALL * 2},
            {'key': base64.b64encode(b'short').decode()}, {'key': 'not base64!!'}, {'key': None},
            {'label': 'l' * 61}, {'kind': 'exe'}, {'relay': 'yes'}, {'to_s': 's' * 65},
            {'direct': [{'host': '8.8.8.8', 'port': 1}]}, {'direct': [{'host': 'example.com', 'port': 1}]},
            {'direct': [{'host': '10.0.0.1', 'port': 70000}]}, {'direct': [{'host': '10.0.0.1'}]},
            {'direct': [{'host': '10.0.0.1', 'port': 1}] * 9}, {'direct': 'x'},
            {'kind': 'message', 'size': df.MESSAGE_MAX + 1, 'n': df.chunk_count(df.MESSAGE_MAX + 1, SMALL)},
        ]
        for kw in bad:
            with self.subTest(kw=str(kw)[:60]):
                with self.assertRaises(ValueError):
                    df.offer_in(SENDER_ID, 'bob', self.good(**kw), auto=False)
        o = self.good()
        del o['sha256']
        with self.assertRaises(ValueError):
            df.offer_in(SENDER_ID, 'bob', o, auto=False)
        with self.assertRaises(ValueError):
            df.offer_in(SENDER_ID, 'bob', 'not a dict', auto=False)
        with self.assertRaises(ValueError):
            df.offer_in('../evil', 'bob', self.good(), auto=False)

    def test_peer_label_sanitised(self):
        tid = df.offer_in(SENDER_ID, '../../etc', self.good(), auto=False)
        self.assertNotIn('/', df.in_record(tid, SENDER_ID)['peer'])
        self.assertFalse(df.in_record(tid, SENDER_ID)['peer'].startswith('.'))

    def test_safe_name(self):
        self.assertEqual(df.safe_name('..evil'), 'evil')
        self.assertEqual(df.safe_name('a/b\\c:d'), 'a_b_c_d')
        self.assertEqual(df.safe_name('CON.txt'), '_CON.txt')
        self.assertEqual(df.safe_name('...'), 'file')
        self.assertEqual(len(df.safe_name('y' * 500)), 200)


class PolicyTests(unittest.TestCase):
    def test_auto_accept(self):
        G = 1024 ** 3
        trusted = {'name': 'alice', 'pub': 'x', 'files': 'auto'}
        self.assertFalse(df.auto_accept({'name': 'alice', 'pub': 'x'}, 10, {}))       # default: ask
        self.assertFalse(df.auto_accept(dict(trusted, files='ask'), 10, {}))
        self.assertFalse(df.auto_accept(dict(trusted, files=True), 10, {}))
        self.assertFalse(df.auto_accept(dict(trusted, files='AUTO'), 10, {}))
        self.assertTrue(df.auto_accept(trusted, 5 * G, {}))
        self.assertTrue(df.auto_accept(trusted, 10 * G, {}))
        self.assertFalse(df.auto_accept(trusted, 10 * G + 1, {}))                      # over the default cap
        self.assertFalse(df.auto_accept(trusted, 200, {'files_auto_accept_max': 100}))   # over a configured cap
        self.assertTrue(df.auto_accept(trusted, 100, {'files_auto_accept_max': 100}))
        self.assertFalse(df.auto_accept(dict(trusted, scope=['s1']), 10, {}))
        self.assertFalse(df.auto_accept(dict(trusted, scope=[]), 10, {}))
        self.assertFalse(df.auto_accept(dict(trusted, blocked=True), 10, {}))
        self.assertFalse(df.auto_accept(dict(trusted, approvals=[]), 10, {}))
        self.assertFalse(df.auto_accept(dict(trusted, scoped_by='tracer'), 10, {}))
        self.assertTrue(df.auto_accept(dict(trusted, blocked=False), 10, {}))
        self.assertFalse(df.auto_accept({}, 10, {}))
        self.assertFalse(df.auto_accept({'files': 'auto'}, 10 * G + 1, {}))
        self.assertFalse(df.auto_accept(dict(trusted, scope=['s1'], blocked=False), 10, {}))
        self.assertFalse(df.auto_accept(trusted, -1, {}))

    def test_relay_allowed(self):
        G = 1024 ** 3
        self.assertTrue(df.relay_allowed(2 * G, {}))
        self.assertFalse(df.relay_allowed(2 * G + 1, {}))
        self.assertTrue(df.relay_allowed(5 * G, {'files_relay_max': 5 * G}))
        self.assertFalse(df.relay_allowed(11, {'files_relay_max': 10}))


class FixRoundTests(Base):
    """Regression tests for the security audit (fix round 1)."""

    def accepted(self, name='z.bin', size=SMALL * 2 + 3, peer='bob', seed=5, owner=''):
        src = make_file(name, size, seed=seed)
        tid, o = df.offer(str(src), 'alice', RECEIVER_ID, '')
        df.offer_in(SENDER_ID, peer, o, auto=True, owner=owner)
        return tid, o, src

    # -- finish: never stuck in 'transferring'
    def test_poc_symlinked_landing_fails_cleanly(self):
        """poc_finish.py: Downloads/Dropship is a symlink (e.g. to another disk) → failed + receipt, not stuck."""
        dl = WORK / 'poc-dl'
        (dl / 'old').mkdir(parents=True, exist_ok=True)
        ext = WORK / 'poc-ext'
        ext.mkdir(exist_ok=True)
        if not (dl / 'Dropship').is_symlink():
            os.symlink(ext, dl / 'Dropship')
        df.DOWNLOADS = dl
        tid, o, _ = self.accepted('ok.txt')
        transfer(o, tid)
        with self.assertRaises(df.FinishError) as cm:
            df.finish(tid, SENDER_ID)
        self.assertTrue(cm.exception.reason)
        rec = df.in_record(tid, SENDER_ID)
        self.assertEqual(rec['status'], 'failed')
        self.assertEqual(rec['reason'], cm.exception.reason)
        self.assertEqual(df.receipt(tid, SENDER_ID), {'id': tid, 'status': 'failed', 'sha256': ''})
        self.assertFalse((df.PARTIAL / f'{SENDER_ID}-{tid}.part').exists())
        self.assertEqual(list(ext.iterdir()), [])
        self.assertNotIn(tid, [r['id'] for r in df.pending()])
        with self.assertRaises(df.FinishError):                          # a second call: closed, left as it is
            df.finish(tid, SENDER_ID)
        self.assertEqual(df.in_record(tid, SENDER_ID)['reason'], rec['reason'])

    def test_poc_long_multibyte_name_saves(self):
        """poc_finish.py: 100 x 'あ' is 104 characters but 304 bytes: landing name is shortened to fit, not stuck."""
        name = 'あ' * 100 + '.txt'
        self.assertTrue(df._valid_name(name))
        tid, o, src = self.accepted()
        o = dict(o, name=name)
        clear_incoming()
        df.offer_in(SENDER_ID, 'bob', o, auto=True)
        transfer(o, tid)
        path = Path(df.finish(tid, SENDER_ID))
        self.assertLessEqual(len(path.name.encode('utf-8')), df.NAME_MAX)
        self.assertTrue(path.name.endswith('.txt'))
        self.assertTrue(path.name.startswith('あ'))
        self.assertEqual(path.read_bytes(), src.read_bytes())
        self.assertEqual(df.receipt(tid, SENDER_ID)['status'], 'saved')

    def test_safe_name_byte_cap(self):
        n = df.safe_name('é' * 150 + '.jpeg')
        self.assertLessEqual(len(n.encode('utf-8')), 200)
        self.assertTrue(n.endswith('.jpeg'))
        self.assertEqual(df.safe_name('plain.txt'), 'plain.txt')
        self.assertLessEqual(len(df.safe_name('😀' * 199).encode('utf-8')), 200)

    def test_finish_any_failure_is_failed_with_receipt(self):
        tid, o, _ = self.accepted()
        df.store_chunk(tid, SENDER_ID, 0, df.serve_chunk(tid, 0, RECEIVER_ID))
        with self.assertRaises(df.FinishError) as cm:                     # chunks missing
            df.finish(tid, SENDER_ID)
        self.assertIn('missing', cm.exception.reason)
        self.assertEqual(df.receipt(tid, SENDER_ID)['status'], 'failed')
        # a landing that cannot be written (folder is a file) also ends as failed
        tid2, o2, _ = self.accepted('w.bin')
        transfer(o2, tid2)
        with mock.patch.object(df, '_reserve', side_effect=OSError(36, 'File name too long')):
            with self.assertRaises(df.FinishError):
                df.finish(tid2, SENDER_ID)
        self.assertEqual(df.in_record(tid2, SENDER_ID)['status'], 'failed')
        self.assertEqual(df.receipt(tid2, SENDER_ID)['status'], 'failed')

    def test_finish_on_refused_leaves_refused(self):
        tid, o, _ = self.accepted()
        transfer(o, tid)
        df.refuse(tid, SENDER_ID)
        with self.assertRaises(df.FinishError):
            df.finish(tid, SENDER_ID)
        self.assertEqual(df.receipt(tid, SENDER_ID)['status'], 'refused')

    def test_hash_runs_outside_the_lock(self):
        tid, o, _ = self.accepted()
        transfer(o, tid)
        real, seen = df._stream_sha, []

        def probe(path, size):
            got = []

            def try_lock():                                    # another thread can take the lock meanwhile
                ok = df._lock.acquire(timeout=2)
                got.append(ok)
                if ok:
                    df._lock.release()
            t = threading.Thread(target=try_lock)
            t.start()
            t.join()
            seen.append(got[0])
            return real(path, size)
        with mock.patch.object(df, '_stream_sha', probe):
            df.finish(tid, SENDER_ID)
        self.assertEqual(seen, [True])

    def test_refused_while_hashing_is_not_landed(self):
        tid, o, _ = self.accepted('race.bin', peer='race')
        transfer(o, tid)
        real = df._stream_sha

        def refuse_midway(path, size):
            h = real(path, size)
            df.refuse(tid, SENDER_ID)
            return h
        with mock.patch.object(df, '_stream_sha', refuse_midway):
            with self.assertRaises(df.FinishError):
                df.finish(tid, SENDER_ID)
        self.assertEqual(df.in_record(tid, SENDER_ID)['status'], 'refused')
        land = WORK / 'Downloads' / 'Dropship' / 'race'
        self.assertFalse(land.exists() and any(land.iterdir()))

    # -- fail
    def test_fail_reason_and_never_over_refused(self):
        tid, o, _ = self.accepted()
        df.fail(tid, SENDER_ID, 'no route')
        rec = df.in_record(tid, SENDER_ID)
        self.assertEqual((rec['status'], rec['reason']), ('failed', 'no route'))
        self.assertEqual(df.receipt(tid, SENDER_ID), {'id': tid, 'status': 'failed', 'sha256': ''})
        tid2, _, _ = self.accepted('r.bin')
        df.refuse(tid2, SENDER_ID)
        df.fail(tid2, SENDER_ID, 'deadline')
        df.fail(tid2, SENDER_ID)
        self.assertEqual(df.in_record(tid2, SENDER_ID)['status'], 'refused')
        self.assertEqual(df.receipt(tid2, SENDER_ID)['status'], 'refused')

    # -- owner / session / duplicates
    def test_owner_and_session_stored(self):
        src = make_file('own.bin', 40)
        tid, o = df.offer(str(src), 'alice', RECEIVER_ID, 'work', session='sess-123')
        self.assertEqual(df.out_record(tid)['session'], 'sess-123')
        df.offer_in(SENDER_ID, 'bob', o, auto=False, owner='mysession')
        self.assertEqual(df.in_record(tid, SENDER_ID)['owner'], 'mysession')
        mine = [r for r in df.pending() if r['id'] == tid]
        self.assertEqual(mine[0]['owner'], 'mysession')
        self.assertNotIn('key', mine[0])
        with self.assertRaises(df.Duplicate):                              # repeat: owner and status unchanged
            df.offer_in(SENDER_ID, 'bob', o, auto=True, owner='attacker-session')
        rec = df.in_record(tid, SENDER_ID)
        self.assertEqual((rec['owner'], rec['status']), ('mysession', 'pending'))
        self.assertFalse((df.PARTIAL / f'{SENDER_ID}-{tid}.part').exists())
        # older records without owner/reason still read with both fields
        meta = df.IN / f'{SENDER_ID}-{tid}.json'
        raw = json.loads(meta.read_text())
        del raw['owner'], raw['reason']
        meta.write_text(json.dumps(raw))
        self.assertEqual((df.in_record(tid, SENDER_ID)['owner'], df.in_record(tid, SENDER_ID)['reason']), ('', ''))

    # -- limits
    def test_pending_caps(self):
        good = OfferValidationTests.good
        for _ in range(df.MAX_PENDING_PER_PEER):
            df.offer_in(SENDER_ID, 'bob', good(None), auto=False)
        with self.assertRaises(ValueError):
            df.offer_in(SENDER_ID, 'bob', good(None), auto=False)
        # closing one frees a slot
        df.refuse(df.pending()[0]['id'], SENDER_ID)
        df.offer_in(SENDER_ID, 'bob', good(None), auto=False)
        # a repeat at the cap is still a Duplicate (not a cap error)
        o = good(None)
        clear_incoming()
        df.offer_in(SENDER_ID, 'bob', o, auto=False)
        with self.assertRaises(df.Duplicate):
            df.offer_in(SENDER_ID, 'bob', o, auto=False)
        clear_incoming()
        peers = [f'{k:x}' * 16 for k in range(1, 6)]                     # 5 peers x 20 = the total cap
        for pid in peers:
            for _ in range(df.MAX_PENDING_PER_PEER):
                df.offer_in(pid, 'p', good(None), auto=False)
        self.assertEqual(len(df.pending()), df.MAX_PENDING_TOTAL)
        with self.assertRaises(ValueError):
            df.offer_in('6' * 16, 'p', good(None), auto=False)
        clear_incoming()

    def test_reserved_bytes_counted(self):
        size = SMALL * 10
        Usage = type('Usage', (), {})
        u = Usage()
        u.total, u.used, u.free = 10 ** 9, 0, int(size * 2.5)
        with mock.patch.object(df.shutil, 'disk_usage', return_value=u):
            t1, o1, _ = self.accepted('r1.bin', size=size, seed=1)
            t2, o2, _ = self.accepted('r2.bin', size=size, seed=2)
            t3, o3, _ = self.accepted('r3.bin', size=size, seed=3)
            self.assertEqual([df.in_record(t, SENDER_ID)['status'] for t in (t1, t2, t3)],
                             ['accepted', 'accepted', 'pending'])          # 2 reserved: no room for a third
            with self.assertRaises(ValueError):
                df.accept(t3, SENDER_ID)
            transfer(o1, t1)                                                # written bytes no longer reserved
            df.finish(t1, SENDER_ID)
            df.accept(t3, SENDER_ID)
            self.assertEqual(df.in_record(t3, SENDER_ID)['status'], 'accepted')
            # a partly written transfer only reserves what is still missing
            self.assertEqual(df._reserved(Path('-')), size + size)
            df.store_chunk(t2, SENDER_ID, 0, df.serve_chunk(t2, 0, RECEIVER_ID))
            self.assertEqual(df._reserved(Path('-')), size + size - SMALL)

    # -- landing folders
    def test_dest_in_home_or_denylist_refused(self):
        tid, o, _ = self.accepted(peer='bob')
        clear_incoming()
        df.offer_in(SENDER_ID, 'bob', o, auto=False)
        home = Path(HOME)
        bad = [home, home / 'files', home / 'files' / 'new-sub', home / 'inbox-new',
               Path.home() / '.ssh', Path.home() / '.ssh' / 'keys-new',
               Path.home() / 'Library' / 'LaunchAgents', Path('/Library/LaunchDaemons'),
               Path.home() / '.config' / 'autostart', Path.home() / '.config' / 'autostart' / 'x',
               Path.home() / 'AppData' / 'Roaming' / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup']
        if case_insensitive(home):
            bad += [Path(str(home).swapcase()), Path(str(home / 'files').upper()) / 'new']
        for p in bad:
            with self.subTest(dest=str(p)):
                with self.assertRaises(ValueError):
                    df.accept(tid, SENDER_ID, str(p))
        self.assertFalse((home / 'files' / 'new-sub').exists())
        self.assertFalse((Path.home() / '.ssh' / 'keys-new').exists())
        self.assertEqual(df.in_record(tid, SENDER_ID)['status'], 'pending')
        ok = WORK / 'fine-dest'
        df.accept(tid, SENDER_ID, str(ok))
        transfer(o, tid)
        self.assertEqual(Path(df.finish(tid, SENDER_ID)).parent, ok)

    def test_dest_rechecked_in_finish(self):
        tid, o, _ = self.accepted()
        meta = df.IN / f'{SENDER_ID}-{tid}.json'
        rec = json.loads(meta.read_text())
        rec['dest'] = str(Path(HOME) / 'files' / 'planted')                # e.g. written by an older version
        meta.write_text(json.dumps(rec))
        transfer(o, tid)
        with self.assertRaises(df.FinishError):
            df.finish(tid, SENDER_ID)
        self.assertEqual(df.in_record(tid, SENDER_ID)['status'], 'failed')
        self.assertFalse((Path(HOME) / 'files' / 'planted').exists())

    # -- sending: Dropship's own state never leaves
    def test_poc_case_changed_home_refused(self):
        """poc_case.py: on a case-insensitive disk a differently-cased spelling of HOME is still HOME."""
        ident = Path(HOME) / 'identity.json'
        ident.write_text('{"box_priv":"SECRET"}')
        with self.assertRaises(ValueError):
            df._safe_source(str(ident))
        if not case_insensitive(Path(HOME)):
            self.skipTest('case-sensitive filesystem')
        for spelled in (str(ident).swapcase(), str(Path(HOME)).upper() + os.sep + 'identity.json'):
            with self.assertRaises(ValueError):
                df._safe_source(spelled)
            with self.assertRaises(ValueError):
                df.offer(spelled, 'alice', RECEIVER_ID, '')

    def test_hardlink_into_home_refused(self):
        ident = Path(HOME) / 'identity.json'
        ident.write_text('{"box_priv":"SECRET"}')
        link = WORK / 'innocent.txt'
        link.unlink(missing_ok=True)
        try:
            os.link(ident, link)
        except OSError:
            self.skipTest('no hardlinks here')
        with self.assertRaises(ValueError):
            df.offer(str(link), 'alice', RECEIVER_ID, '')
        # an ordinary hardlinked file outside HOME is fine
        a = make_file('hl-a.bin', 10)
        b = WORK / 'src' / 'hl-b.bin'
        b.unlink(missing_ok=True)
        os.link(a, b)
        df.offer(str(b), 'alice', RECEIVER_ID, '')

    def test_outmsg_exception_only_for_messages(self):
        outmsg = df.FILES / 'outmsg'
        outmsg.mkdir(parents=True, exist_ok=True)
        m = outmsg / 'm1.md'
        m.write_text('long message')
        df.offer(str(m), 'alice', RECEIVER_ID, '', kind='message')
        with self.assertRaises(ValueError):
            df.offer(str(m), 'alice', RECEIVER_ID, '', kind='file')
        with self.assertRaises(ValueError):                                # other HOME files: never, even as message
            df.offer(str(Path(HOME) / 'identity.json'), 'alice', RECEIVER_ID, '', kind='message')
        h = outmsg / 'm2.md'
        h.unlink(missing_ok=True)
        os.link(m, h)                                                      # hardlinked staging file
        with self.assertRaises(ValueError):
            df.offer(str(h), 'alice', RECEIVER_ID, '', kind='message')

    # -- routes
    def test_offer_routes_only_private_ipv4(self):
        good = OfferValidationTests.good
        for host in ('127.0.0.1', '169.254.10.1', '::1', 'fe80::1', 'fd00::1', '0.0.0.5', '0.1.2.3', '240.0.0.1',
                     '255.255.255.255', '224.0.0.1', '8.8.8.8', '::ffff:127.0.0.1', '::ffff:8.8.8.8', '198.18.0.1',
                     '100.63.255.255', '100.128.0.1', '172.32.0.1', 'localhost'):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    df.offer_in(SENDER_ID, 'bob', good(None, direct=[{'host': host, 'port': 47779}]), auto=False)
        tid = df.offer_in(SENDER_ID, 'bob', good(None, direct=[{'host': '::ffff:192.168.1.5', 'port': 1},
                                                               {'host': '100.64.0.1', 'port': 2},
                                                               {'host': '172.16.0.9', 'port': 3},
                                                               {'host': '10.1.2.3', 'port': 4}]), auto=False)
        self.assertEqual([r['host'] for r in df.in_record(tid, SENDER_ID)['direct']],
                         ['192.168.1.5', '100.64.0.1', '172.16.0.9', '10.1.2.3'])

    # -- sender expiry
    def test_serve_never_extends_expiry(self):
        src = make_file('exp.bin', SMALL * 2)
        tid, _ = df.offer(str(src), 'alice', RECEIVER_ID, '')
        rec = df.out_record(tid)
        self.assertAlmostEqual(rec['expires'] - rec['offered_at'], 48 * 3600, delta=1)
        p = df.OUT / f'{tid}.json'
        raw = json.loads(p.read_text())
        raw['expires'] = time.time() + 60                                 # nearly 48 h in
        p.write_text(json.dumps(raw))
        df.serve_chunk(tid, 0, RECEIVER_ID)
        self.assertEqual(json.loads(p.read_text())['expires'], raw['expires'])
        raw['expires'] = time.time() - 1
        p.write_text(json.dumps(raw))
        with self.assertRaises(PermissionError):
            df.serve_chunk(tid, 1, RECEIVER_ID)

    # -- sweep vs live progress
    def test_sweep_concurrent_with_progress(self):
        stop, errors = threading.Event(), []

        def churn():
            k = 0
            while not stop.is_set():
                tid = f'fx_{k % 50:016x}'
                df._set_live('in', SENDER_ID, tid, 'n', 1, 2, 'bob')
                df._drop_live('in', SENDER_ID, tid) if k % 3 == 0 else None
                k += 1

        def sweeper():
            try:
                for _ in range(200):
                    df.sweep()
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=churn) for _ in range(2)]
        for t in ts:
            t.start()
        sw = threading.Thread(target=sweeper)
        sw.start()
        sw.join()
        stop.set()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        with df._lock:
            df._live.clear()

    # -- Windows replace retry
    def test_replace_retries_on_windows(self):
        a, b = WORK / 'rep-a', WORK / 'rep-b'
        a.write_bytes(b'1')
        real, calls = os.replace, []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(13, 'in use')
            return real(src, dst)
        with mock.patch.object(df.d, 'IS_WIN', True), mock.patch.object(df.os, 'replace', flaky):
            df._replace(a, b)
        self.assertEqual((len(calls), b.read_bytes()), (3, b'1'))
        calls.clear()
        a.write_bytes(b'2')
        with mock.patch.object(df.d, 'IS_WIN', False), mock.patch.object(df.os, 'replace', flaky):
            with self.assertRaises(PermissionError):
                df._replace(a, b)
        self.assertEqual(len(calls), 1)
        calls.clear()
        with mock.patch.object(df.d, 'IS_WIN', True), mock.patch.object(df.os, 'replace',
                                                                        side_effect=PermissionError(13, 'x')), \
                mock.patch.object(df.time, 'sleep'):
            with self.assertRaises(PermissionError):
                df._replace(a, b)


class SweepTests(Base):
    def test_expiry_sweep(self):
        src = make_file('x.bin', SMALL * 2)
        tid_old, o_old = df.offer(str(src), 'alice', RECEIVER_ID, '')
        tid_new, o_new = df.offer(str(src), 'alice', RECEIVER_ID, '')
        df.offer_in(SENDER_ID, 'bob', o_old, auto=True)
        df.offer_in(SENDER_ID, 'bob', o_new, auto=False)
        df.store_chunk(tid_old, SENDER_ID, 0, df.serve_chunk(tid_old, 0, RECEIVER_ID))
        meta = df.IN / f'{SENDER_ID}-{tid_old}.json'
        rec = json.loads(meta.read_text())
        rec['expires'] = time.time() - 1                                  # stalled for > 24 h
        meta.write_text(json.dumps(rec))
        orphan = df.PARTIAL / f'{SENDER_ID}-fx_{"9" * 16}.part'
        orphan.write_bytes(b'')
        out_meta = df.OUT / f'{tid_new}.json'
        orec = json.loads(out_meta.read_text())
        orec['expires'] = time.time() - 1
        out_meta.write_text(json.dumps(orec))

        gone = df.sweep()
        self.assertIn(tid_old, gone)
        self.assertNotIn(tid_new, gone)
        self.assertFalse(meta.exists())
        self.assertFalse((df.PARTIAL / f'{SENDER_ID}-{tid_old}.part').exists())
        self.assertFalse(orphan.exists())
        self.assertEqual(df.in_record(tid_new, SENDER_ID)['status'], 'pending')
        self.assertFalse(out_meta.exists())
        self.assertTrue((df.OUT / f'{tid_old}.json').exists())

    def test_pending_expires_after_24h(self):
        o = OfferValidationTests.good(None)
        tid = df.offer_in(SENDER_ID, 'bob', o, auto=False)
        rec = df.in_record(tid, SENDER_ID)
        self.assertAlmostEqual(rec['expires'] - rec['at'], 24 * 3600, delta=1)
        meta = df.IN / f'{SENDER_ID}-{tid}.json'
        rec['expires'] = time.time() - 1
        meta.write_text(json.dumps(rec))
        with self.assertRaises(ValueError):
            df.accept(tid, SENDER_ID)
        self.assertIn(tid, df.sweep())
        with self.assertRaises(ValueError):
            df.in_record(tid, SENDER_ID)


if __name__ == '__main__':
    unittest.main()
