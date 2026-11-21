#!/usr/bin/env python3
"""Borg mode listens passively: Codex is woken by the courier, a Claude window with the channel flag gets pushes,
any other Claude window runs a background listener that the Stop hook re-arms. Nothing ever blocks the user's typing.

    tests/dropship/.run/venv/bin/python tests/dropship/test_borg_passive.py
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ['DROPSHIP_HOME'] = tempfile.mkdtemp(prefix='dropship-borg-')
for k in ('DROPSHIP_SESSION', 'DROPSHIP_AS', 'DROPSHIP_MCP_PID', 'CODEX_THREAD_ID'):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins' / 'hypernym' / 'skills' / 'hypernym' / 'scripts'))
import dropship as d  # noqa: E402


def rec(**kw):
    r = {'n': 0, 'name': 'drone', 'title': 'drone', 'cwd': '/w', 'seen': time.time(), 'scan': 0, 'transcript': '',
         'exposed': True, 'pid': 0, 'tty': '', 'app': '', 'borg': True}
    r.update(kw)
    d.save(d.SESSIONS / 'drone.json', r)
    return d.session_record('drone')


class Passive(unittest.TestCase):
    def setUp(self):
        d._private_dir()

    def test_codex_is_woken_never_waits(self):
        r = rec(transcript='/Users/x/.codex/sessions/2026/11/21/rollout-x.jsonl')
        self.assertEqual(d.borg_mode(r), 'codex')
        note = d.borg_note(r, 'drone')
        self.assertIn('wakes this Codex thread', note)
        self.assertIn('NEVER call dropship_wait', note)
        self.assertNotIn('run_in_background', note)

    def test_claude_with_channel_flag_gets_pushes(self):
        r = rec(host=4242)
        with patch.object(d, 'pid_alive', lambda p: True), \
                patch.object(d, '_run', lambda argv: 'claude --dangerously-load-development-channels plugin:hypernym@x'):
            self.assertEqual(d.borg_mode(r), 'push')
            self.assertIn('pushed into this window', d.borg_note(r, 'drone'))

    def test_other_claude_windows_use_a_background_listener(self):
        r = rec(host=4242)
        with patch.object(d, 'pid_alive', lambda p: p == 4242), patch.object(d, '_run', lambda argv: 'claude'):
            self.assertEqual(d.borg_mode(r), 'listen')
            note = d.borg_note(r, 'drone')
        self.assertIn('run_in_background: true', note)
        self.assertIn('listen drone', note)
        self.assertIn(d.CHANNEL_CMD, note, 'offers the no-listener way')

    def test_stop_hook_rearms_a_missing_listener_once(self):
        rec(host=4242)
        with patch.object(d, 'pid_alive', lambda p: p == 4242), patch.object(d, '_run', lambda argv: 'claude'):
            out = json.loads(d.borg_stop({'session_id': 'drone', 'stop_hook_active': False}))
            self.assertEqual(out['decision'], 'block')
            self.assertIn('listen drone', out['reason'])
            self.assertEqual(d.borg_stop({'session_id': 'drone', 'stop_hook_active': True}), '', 'never twice in a row')
            with patch.object(d, 'listening', lambda sid: True):
                self.assertEqual(d.borg_stop({'session_id': 'drone'}), '', 'a running listener needs nothing')
        rec(transcript='/Users/x/.codex/sessions/rollout-x.jsonl')
        self.assertEqual(d.borg_stop({'session_id': 'drone'}), '', 'codex is woken by the courier')
        rec(borg=False)
        self.assertEqual(d.borg_stop({'session_id': 'drone'}), '', 'not borg: nothing')

    def test_a_borg_listen_wait_returns_at_once(self):
        rec(host=4242)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        t0 = time.time()
        with patch.object(sys, 'argv', ['dropship.py', 'wait']), patch.dict(os.environ, {'DROPSHIP_SESSION': 'drone'}), \
                redirect_stdout(buf):
            d.main()
        self.assertLess(time.time() - t0, 3)
        self.assertIn('no longer waits', buf.getvalue())


if __name__ == '__main__':
    unittest.main()
