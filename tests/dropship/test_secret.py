#!/usr/bin/env python3
"""Secret transfer hardening (needs PyNaCl: run with the test venv's python).

    tests/dropship/.run/venv/bin/python tests/dropship/test_secret.py
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

RUN = Path(__file__).resolve().parent / '.run'
RUN.mkdir(exist_ok=True)
HOME = tempfile.mkdtemp(prefix='secret-home-', dir=RUN)
os.environ['DROPSHIP_HOME'] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins' / 'hypernym' / 'skills' / 'hypernym' / 'scripts'))
import dropship as d  # noqa: E402
import dropship_mail as dm  # noqa: E402
import dropship_secret as ds  # noqa: E402
import nacl.secret  # noqa: E402

me = d.identity()
me['box_priv'], me['box_pub'] = dm.new_box_keys()
d.save(d.IDENTITY, me)
WORK = Path(tempfile.mkdtemp(prefix='secret-proj-', dir=RUN))
ALICE, MALLORY, S1, S2 = 'a' * 16, 'b' * 16, 'sess-one', 'sess-two'


def reset_friends(grant=True):
    f = {ALICE: {'name': 'alice', 'pub': 'x', 'box': 'y'}, MALLORY: {'name': 'mallory', 'pub': 'x', 'box': 'y'}}
    if grant:
        f[ALICE]['secrets'] = [{'session': '*', 'dest': '*'}]
    d.save(d.FRIENDS, f)
    for sid, name in ((S1, 'one'), (S2, 'two')):
        d.save(d.SESSIONS / f'{sid}.json', {'n': 0, 'name': name, 'title': name, 'cwd': str(WORK), 'seen': time.time(),
                                             'scan': 0, 'transcript': '', 'exposed': True, 'pid': 0, 'tty': '', 'app': '',
                                             'borg': False})


def stage(tid, data: bytes, pid=ALICE, owner=S1, expires=None):
    ds._dirs()
    meta_p, enc_p = ds._staged(pid, tid)
    ds._write_private(enc_p, nacl.secret.SecretBox(ds._local_key(pid, tid)).encrypt(data))
    ds._write_private(meta_p, json.dumps({
        'id': tid, 'from': 'alice', 'from_id': pid, 'label': 'k', 'size': len(data),
        'sha': hashlib.sha256(data).hexdigest(), 'owner': owner, 'to_s': '',
        'expires': expires or time.time() + 600}).encode())


class SecretTests(unittest.TestCase):
    def setUp(self):
        os.chdir(WORK)
        reset_friends()

    def test_env_value_is_quoted_literally(self):
        tricky = 'a"b$HOME\\c `rm -rf` x'
        stage('sx_0000000000000001', tricky.encode())
        out = ds.install('sx_0000000000000001', str(WORK / '.env'), 'TOKEN', sid=S1)
        self.assertNotIn(tricky, out)
        self.assertIn('Receipt queued', out)
        self.assertEqual((WORK / '.env').read_text().strip(), 'TOKEN="a\\"b\\$HOME\\\\c \\`rm -rf\\` x"')

    def test_existing_key_needs_overwrite(self):
        (WORK / 'b.env').write_text('TOKEN=old\n')
        stage('sx_0000000000000002', b'new')
        with self.assertRaises(ValueError):
            ds.install('sx_0000000000000002', str(WORK / 'b.env'), 'TOKEN', sid=S1)
        ds.install('sx_0000000000000002', str(WORK / 'b.env'), 'TOKEN', overwrite=True, sid=S1)
        self.assertEqual((WORK / 'b.env').read_text(), 'TOKEN="new"\n')

    def test_multiline_env_refused(self):
        (WORK / 'm.env').write_text('CERT="-----BEGIN\nabc\n-----END"\n')
        stage('sx_0000000000000003', b'v')
        with self.assertRaises(ValueError):
            ds.install('sx_0000000000000003', str(WORK / 'm.env'), 'K', sid=S1)

    def test_symlink_anywhere_in_path_refused(self):
        real = WORK / 'realdir'
        real.mkdir(exist_ok=True)
        (WORK / 'linkdir').symlink_to(real)
        stage('sx_0000000000000004', b'secret')
        with self.assertRaises(ValueError):
            ds.install('sx_0000000000000004', str(WORK / 'linkdir' / 'k.key'), sid=S1)
        src = WORK / 'linkdir' / 'src.txt'
        (real / 'src.txt').write_text('x')
        with self.assertRaises(ValueError):
            ds._safe_source(str(src))

    def test_outside_home_and_project_refused(self):
        stage('sx_0000000000000005', b'secret')
        with self.assertRaises(ValueError):
            ds.install('sx_0000000000000005', '/tmp/stolen.key', sid=S1)

    def test_install_rechecks_everything(self):
        stage('sx_0000000000000006', b's', expires=time.time() - 1)
        with self.assertRaises(ValueError):                          # expired
            ds.install('sx_0000000000000006', str(WORK / 'e.key'), sid=S1)
        stage('sx_0000000000000007', b's')
        with self.assertRaises(ValueError):                          # wrong calling session
            ds.install('sx_0000000000000007', str(WORK / 'w.key'), sid=S2)
        d.set_policy('alice', blocked=True)
        with self.assertRaises(ValueError):                          # sender blocked since arrival
            ds.install('sx_0000000000000007', str(WORK / 'w.key'), sid=S1)
        d.set_policy('alice', blocked=False)
        ds.grant('alice', False)
        with self.assertRaises(ValueError):                          # grant revoked since arrival
            ds.install('sx_0000000000000007', str(WORK / 'w.key'), sid=S1)
        self.assertIn('(none)', ds.pending(S1), 'revoked items are not listed either')

    def test_grant_is_scoped_to_session_and_destination(self):
        reset_friends(grant=False)
        allowed = WORK / 'keys'
        allowed.mkdir(exist_ok=True)
        print(ds.grant('alice', True, session='one', dest=str(allowed)))
        self.assertEqual(ds.authorized(ALICE, S1, str(allowed / 'x')), '')
        self.assertNotEqual(ds.authorized(ALICE, S2, str(allowed / 'x')), '', 'other session')
        self.assertNotEqual(ds.authorized(ALICE, S1, str(WORK / 'elsewhere')), '', 'other folder')

    def test_staging_is_per_peer_and_never_overwritten(self):
        stage('sx_0000000000000008', b'first')
        meta_p, enc_p = ds._staged(ALICE, 'sx_0000000000000008')
        before = enc_p.read_bytes()

        class C:
            mail = None
        payload = json.dumps({'t': 'dsp-secret/1', 'id': 'sx_0000000000000008', 'label': 'l', 'size': 6,
                              'sha': hashlib.sha256(b'second').hexdigest(), 'data': 'c2Vjb25k'})
        ds.secret_in(C(), ALICE, 'alice', payload, '')
        self.assertEqual(enc_p.read_bytes(), before, 'a duplicate id must not overwrite a staged secret')

    def test_receipts_are_pinned_to_recipient_and_content(self):
        src = WORK / 'src.secret'
        src.write_text('s')
        ds._dirs()
        snap = ds._snapshot(src)
        ds._write_private(ds.SENT / 'sx_0000000000000009.json', json.dumps({
            'id': 'sx_0000000000000009', 'path': str(src), 'to': 'alice', 'to_id': ALICE, 'to_s': '', 'label': 'l',
            'move': True, 'snap': snap, 'status': 'sent', 'at': time.time()}).encode())
        ds.receipt_in(None, MALLORY, json.dumps({'id': 'sx_0000000000000009', 'status': 'installed', 'sha': snap['sha']}))
        self.assertTrue(src.exists(), 'receipt from the wrong peer ignored')
        ds.receipt_in(None, ALICE, json.dumps({'id': 'sx_0000000000000009', 'status': 'installed', 'sha': 'f' * 64}))
        self.assertTrue(src.exists(), 'receipt for other content ignored')
        ds.receipt_in(None, ALICE, json.dumps({'id': 'sx_0000000000000009', 'status': 'pwned\x1b[31m'}))
        self.assertEqual(json.loads((ds.SENT / 'sx_0000000000000009.json').read_text())['status'], 'sent')
        ds.receipt_in(None, ALICE, json.dumps({'id': 'sx_0000000000000009', 'status': 'installed', 'sha': snap['sha']}))
        self.assertFalse(src.exists())

    def test_unauthorized_secret_is_never_buffered_or_journaled(self):
        class Courier:
            stop = threading.Event()
            friends = {MALLORY: {'name': 'mallory'}}
            me = {'id': d.identity()['id'], 'name': 'chris', 'box_priv': me['box_priv']}
            refused = []
            secret_allowed = lambda self, pid, to_s: ds.authorized(pid, ds.target_owner(pid, '', to_s) or S1) == ''
            secret_mail = lambda self, kind, pid, name, body, to_s: self.refused.append(kind)
        m = dm.Mail.__new__(dm.Mail)
        m.c, m.done, m.parts, m.since = Courier(), {}, {}, 0
        m.journal_path = Path(HOME) / 'mailbox-receive.json'
        friend = {'id': MALLORY}
        m.box = lambda f: type('B', (), {'decrypt': lambda self, b: json.dumps({
            'k': 'secret', 'mid': 'm1', 'from': MALLORY, 'to': m.c.me['id'], 'ts': time.time(), 'i': 0, 'n': 2,
            'body': 'SECRET-CHUNK-0', 'to_s': ''}).encode()})()
        m.last_seen, m.presence, m.presence_path = {}, {}, Path(HOME) / 'presence.json'
        m.c.peer_renamed = lambda *a: None
        m.c.peer_sessions = {}
        m.receive(friend, 'dsp1.' + dm.b64(b'x'))
        self.assertEqual(m.c.refused, ['secret-refused'])
        self.assertFalse(getattr(m, 'secret_parts', {}), 'nothing buffered')
        journal = m.journal_path.read_text() if m.journal_path.exists() else ''
        self.assertNotIn('SECRET-CHUNK', journal)

    def test_partial_transfer_survives_a_restart_encrypted(self):
        mid = 'ab' * 8
        self.assertIsNone(ds.buffer_chunk(ALICE, mid, 0, 2, '', 'PLAINTEXT-PART-ONE'))
        # a courier restart loses all memory; only what is on disk remains
        on_disk = b''.join(p.read_bytes() for p in ds.PARTIAL.glob(f'{ALICE}-{mid}.*'))
        self.assertNotIn(b'PLAINTEXT-PART-ONE', on_disk, 'partial chunks are encrypted at rest')
        self.assertEqual(ds.buffer_chunk(ALICE, mid, 1, 2, '', '-AND-TWO'), 'PLAINTEXT-PART-ONE-AND-TWO')
        self.assertFalse(list(ds.PARTIAL.glob(f'{ALICE}-{mid}.*')), 'partials removed once whole')
        self.assertIsNone(ds.buffer_chunk(ALICE, 'cd' * 8, 0, 2, 'one', 'x'))
        self.assertIsNone(ds.buffer_chunk(ALICE, 'cd' * 8, 1, 2, 'two', 'y'), 'chunks must agree on the target')

    def test_receipt_must_come_from_the_receiving_session(self):
        src = WORK / 'src2.secret'
        src.write_text('s2')
        ds._dirs()
        snap = ds._snapshot(src)
        ds._write_private(ds.SENT / 'sx_000000000000000b.json', json.dumps({
            'id': 'sx_000000000000000b', 'path': str(src), 'to': 'alice', 'to_id': ALICE, 'to_s': 'keeper', 'label': 'l',
            'move': True, 'snap': snap, 'status': 'sent', 'at': time.time()}).encode())
        ds.receipt_in(None, ALICE, json.dumps({'id': 'sx_000000000000000b', 'status': 'installed', 'sha': snap['sha'],
                                               'session': 'other'}))
        self.assertTrue(src.exists(), 'receipt from another session ignored')
        ds.receipt_in(None, ALICE, json.dumps({'id': 'sx_000000000000000b', 'status': 'installed', 'sha': snap['sha'],
                                               'session': 'keeper'}))
        self.assertFalse(src.exists())

    def test_tampered_staging_refused(self):
        stage('sx_000000000000000a', b'secret')
        _meta, enc = ds._staged(ALICE, 'sx_000000000000000a')
        raw = bytearray(enc.read_bytes())
        raw[-1] ^= 1
        enc.write_bytes(bytes(raw))
        with self.assertRaises(ValueError):
            ds.install('sx_000000000000000a', str(WORK / 'c.key'), sid=S1)


if __name__ == '__main__':
    unittest.main()
