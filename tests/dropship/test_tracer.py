"""Offline checks of invitations, session isolation, and authenticated delivery handling."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins/hypernym/skills/hypernym/scripts'))
import dropship as d
import dropship_tracer as t
import codex_status as status
import hypernym_mcp_server as mcp


class TracerTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for key, filename in {'HOME': '.', 'SESSIONS': 'sessions', 'IDENTITY': 'identity.json',
                              'CONFIG': 'config.json', 'STATE': 'state.json', 'FRIENDS': 'friends.json',
                              'INBOX': 'inbox.jsonl', 'TALKS': 'talks.jsonl', 'OUTBOX': 'outbox',
                              'PAIRING': 'pairing.json'}.items():
            self.stack.enter_context(patch.object(d, key, self.home / filename))
        self.stack.enter_context(patch.dict(os.environ, {'DROPSHIP_SESSION': 'a', 'CODEX_HOME': str(self.home / 'codex')}))
        self.stack.enter_context(patch.object(d, 'running', return_value=True))
        self.stack.enter_context(patch.object(t, 'ready'))
        self.stack.enter_context(patch.object(d, 'terminal_of_session', return_value={'pid': 0, 'tty': '', 'app': ''}))
        d.save(d.IDENTITY, {'id': 'machine-a', 'name': 'alice', 'priv': '2', 'pub': '4'})
        d.save(d.STATE, {'mail': {'ok': True}})
        d.register('a', '', '/work')
        d.register('b', '', '/work')
        self.invite = t.create('a').split('hypernym dropship add ')[1].splitlines()[0]

    def test_expiry_and_malformed(self):
        value = t.decode(self.invite)
        self.assertEqual(t.decode(self.invite[:5] + '\n  ' + self.invite[5:]), value)
        self.assertTrue(value['session'].startswith('a-'))
        with patch.object(t.time, 'time', return_value=value['expires'] + 1):
            with self.assertRaises(ValueError):
                t.decode(self.invite)
        for bad in ('oops', 'dsp1-@@', 'dsp1-' + 'a' * 3000):
            with self.assertRaises(ValueError):
                t.decode(bad)

    def test_codex_rename_and_claude_title(self):
        root = self.home / 'codex'
        (root / 'sessions').mkdir(parents=True)
        transcript = root / 'sessions/rollout-a.jsonl'
        transcript.write_text('')
        (root / 'session_index.jsonl').write_text(json.dumps({'id': 'a', 'thread_name': 'old'}) + '\n' +
             json.dumps({'id': 'a', 'thread_name': 'New Name'}) + '\n{partial')
        r = d.register('a', str(transcript), '/work')
        self.assertEqual(r['name'], 'new-name')
        self.assertEqual(d.owner_of(t.decode(self.invite)['session'], 'bob'), 'a')
        claude = self.home / 'claude.jsonl'
        claude.write_text(json.dumps({'type': 'custom-title', 'customTitle': 'Claude Name'}) + '\n')
        self.assertEqual(d.register('b', str(claude), '/work')['name'], 'claude-name')

    def test_ambiguous_or_unknown_session_refused(self):
        with patch.dict(os.environ, {'DROPSHIP_SESSION': '', 'CODEX_THREAD_ID': ''}), patch.object(os, 'getcwd', return_value='/work'):
            with self.assertRaises(ValueError):
                t.current_session()
        with patch.dict(os.environ, {'DROPSHIP_SESSION': 'missing'}):
            with self.assertRaises(ValueError):
                t.current_session('a')

    def test_receipt_only_for_invited_session_and_peer(self):
        value = t.decode(self.invite)
        body = t.CONTROL + json.dumps({'kind': 'hello', 'nonce': value['nonce']})
        t.receive(None, 'bob', body, 'b', 'sender')
        self.assertFalse(d.OUTBOX.exists())
        t.receive(None, 'bob', body, 'a', 'sender')
        jobs = list(d.OUTBOX.glob('*.json'))
        self.assertEqual(len(jobs), 1)
        ack = d.load(jobs[0], {})
        self.assertEqual((ack['to'], ack['to_s'], ack['sid']), ('bob', 'sender', 'a'))
        t.receive(None, 'mallory', body, 'a', 'sender')
        self.assertEqual(len(list(d.OUTBOX.glob('*.json'))), 1)

    def test_existing_friend_connect_uses_mailbox(self):
        value = t.decode(self.invite)
        value['id'] = 'other-machine'
        import base64
        token = t.PREFIX + base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
        d.save(d.FRIENDS, {'other-machine': {'name': 'bob', 'box': 'key'}})
        def transport(name, body, to_s, from_s, sid, peer_id):
            self.assertEqual(peer_id, 'other-machine')
            self.assertEqual((name, to_s, sid), ('bob', value['session'], 'a'))
            t.receive(None, 'bob', t.CONTROL + json.dumps({'kind': 'ack', 'nonce': value['nonce']}), from_s, to_s)
            return 'ok'
        with patch.object(d, 'send_mail', side_effect=transport), patch.object(d, 'pair') as pairing:
            self.assertIn('round trip confirmed', t.connect(token, 'a'))
            pairing.assert_not_called()

    def test_mcp_and_prompt_dispatch(self):
        with patch.object(mcp, 'run_dropship', return_value=('ok', False)) as run:
            mcp.call_tool('dropship_tracer', {'tracer': self.invite, 'as_session': 'a'})
            self.assertEqual(run.call_args.args[0], ['tracer', self.invite])
        event = {'prompt': 'hypernym dropship add ' + self.invite, 'session_id': 'a', 'cwd': '/work'}
        out = json.loads(d.hook_prompt(json.dumps(event)))
        self.assertIn('dropship_tracer', out['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('decision', out)

    def test_codex_status_without_remote_and_change_suppression(self):
        event = json.dumps({'session_id': 'a', 'cwd': '/work', 'hook_event_name': 'Stop'})
        with patch.object(status, 'SHOWN', self.home / 'status'), patch.object(status, 'row', return_value='no github remote'), \
                patch.object(d, 'status_text', return_value='courier running'), patch.dict(os.environ, {'PLUGIN_ROOT': '/plugin'}):
            first = io.StringIO()
            with patch.object(sys, 'stdin', io.StringIO(event)), contextlib.redirect_stdout(first):
                status.main()
            self.assertIn('Dropship', json.loads(first.getvalue())['systemMessage'])
            second = io.StringIO()
            with patch.object(sys, 'stdin', io.StringIO(event)), contextlib.redirect_stdout(second):
                status.main()
            self.assertEqual(second.getvalue(), '')
            self.assertEqual(d.session_record('a')['n'], 0)


if __name__ == '__main__':
    unittest.main()
