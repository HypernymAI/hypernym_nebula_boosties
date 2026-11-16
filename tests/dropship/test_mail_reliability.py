"""Offline tests for provider quota handling, durable delivery and restart replay."""
import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins/hypernym/skills/hypernym/scripts'))
import dropship as d
import dropship_mail as mail
import nacl.public

class StopAfterPass:
    def __init__(self): self.stopped = False
    def is_set(self): return self.stopped
    def wait(self, _): self.stopped = True; return True

class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack(); self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for key in ('HOME', 'OUTBOX', 'INBOX'):
            stack.enter_context(patch.object(d, key, root if key == 'HOME' else root / key.lower()))
        d.OUTBOX.mkdir()
        self.local = nacl.public.PrivateKey.generate(); self.remote = nacl.public.PrivateKey.generate()
        self.friend = {'id': 'remote', 'name': 'friend', 'box': bytes(self.remote.public_key).hex()}
        self.c = types.SimpleNamespace(me={'id': 'local', 'name': 'local', 'box_priv': bytes(self.local).hex()},
            friends={'remote': dict(self.friend)}, lock=threading.Lock(), stop=StopAfterPass(),
            sessions_now=lambda pid="": ['work'], on_lan=lambda _: False, peer_renamed=Mock(), peer_sessions={}, deliver=Mock())
        self.m = mail.Mail(self.c, 'https://example.invalid')

    def encrypted(self, mid, i=0, n=1, body='hello'):
        env = dict(k='msg', mid=mid, i=i, n=n, body=body, ts=time.time(), to='local', **{'from': 'remote'}, to_s='work', from_s='sender')
        box = nacl.public.Box(self.remote, self.local.public_key)
        return 'dsp1.' + mail.b64(box.encrypt(json.dumps(env).encode()))

    def test_daily_quota_persists_and_does_not_disable_receive(self):
        error = urllib.error.HTTPError('https://example.invalid', 429, 'limit',
            {'Retry-After': '21269', 'RateLimit-Policy': '"messages";q=250;w=86400'},
            io.BytesIO(b'{"code":42908,"error":"daily message quota reached"}'))
        with patch.object(mail.urllib.request, 'urlopen', side_effect=error) as request:
            with self.assertRaises(mail.MailboxDeferred) as caught: self.m.post('topic', 'data')
            self.assertGreater(caught.exception.retry_at, time.time() + 21000)
            restarted = mail.Mail(self.c, 'https://example.invalid')
            with self.assertRaises(mail.MailboxDeferred): restarted.post('topic', 'data')
            self.assertEqual(request.call_count, 1)
            self.assertEqual(restarted.retry_at(sending=False), 0)
        self.assertIn('daily', restarted.limit_status()['reason'])

    def test_http_date_retry_after(self):
        future = mail.email.utils.formatdate(time.time() + 7200, usegmt=True)
        error = urllib.error.HTTPError('https://example.invalid', 429, 'limit', {'Retry-After': future}, io.BytesIO(b'{}'))
        with patch.object(mail.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(mail.MailboxDeferred): self.m.post('topic', 'data')
        self.assertGreater(self.m.retry_at(False), time.time() + 7100)

    def test_completed_messages_not_replayed_after_restart(self):
        text = self.encrypted('same')
        self.m.receive(self.friend, text)
        self.assertEqual(self.c.deliver.call_count, 1)
        restarted = mail.Mail(self.c, 'https://example.invalid')
        restarted.receive(self.friend, text)
        self.assertEqual(self.c.deliver.call_count, 1)

    def test_partial_message_survives_restart(self):
        self.m.receive(self.friend, self.encrypted('partial', 0, 2, 'one'))
        self.c.deliver.assert_not_called()
        restarted = mail.Mail(self.c, 'https://example.invalid')
        restarted.receive(self.friend, self.encrypted('partial', 1, 2, 'two'))
        self.assertEqual(self.c.deliver.call_args.args[1], 'onetwo')
        restarted.receive(self.friend, self.encrypted('partial', 0, 2, 'one'))
        self.assertEqual(self.c.deliver.call_count, 1)

    def test_chunk_progress_is_durable(self):
        job = {'mid': 'stable', 'filename': 'job.json'}
        with patch.object(self.m, 'post', side_effect=[None, mail.MailboxDeferred('quota', time.time()+60)]):
            with self.assertRaises(mail.MailboxDeferred): self.m.send(self.friend, 'x'*(mail.CHUNK+1), job=job)
        saved = d.load(d.OUTBOX/'job.json', {})
        self.assertEqual(saved['next_chunk'], 1)
        with patch.object(self.m, 'post') as send:
            self.m.send(self.friend, 'x'*(mail.CHUNK+1), job=saved)
            self.assertEqual(send.call_count, 1)
            box = nacl.public.Box(self.remote, self.local.public_key)
            env = json.loads(box.decrypt(mail.unb64(send.call_args.args[1][5:])))
            self.assertEqual((env['mid'],env['i']), ('stable',1))

    def test_idle_presence_survives_restart(self):
        with patch.object(self.m, 'post') as post:
            self.m.pinger()
            self.assertEqual(post.call_count, 1)
        self.c.stop = StopAfterPass()
        restarted = mail.Mail(self.c, 'https://example.invalid')
        with patch.object(restarted, 'post') as post:
            restarted.pinger()
            post.assert_not_called()
        self.assertEqual(mail.PING_S, 3600)

    def test_outbox_keeps_deferred_job_and_retries_once_due(self):
        req = d.OUTBOX/'queued.json'
        d.save(req, {'to':'friend','body':'test','to_id':'remote','sid':'work'})
        self.c.mail = types.SimpleNamespace(send=Mock(side_effect=mail.MailboxDeferred('quota', time.time()+60)))
        d.Courier.outbox_loop(self.c)
        self.assertTrue(req.exists())
        self.assertTrue(d.load(req.with_suffix('.done'),{})['result'].startswith('queued'))
        mid = d.load(req,{})['mid']
        self.c.stop = StopAfterPass()
        d.Courier.outbox_loop(self.c)
        self.assertEqual(self.c.mail.send.call_count, 1)
        job=d.load(req,{});job['retry_at']=0;d.save(req,job)
        self.c.stop=StopAfterPass();self.c.mail.send.side_effect=None
        d.Courier.outbox_loop(self.c)
        self.assertFalse(req.exists())
        self.assertEqual(self.c.mail.send.call_args.kwargs['job']['mid'],mid)
        self.assertEqual(d.load(req.with_suffix('.done'),{})['result'],'ok')

if __name__ == '__main__': unittest.main()
