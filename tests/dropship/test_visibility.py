#!/usr/bin/env python3
"""A friend that is not one of your own computers sees and reaches only the sessions it is connected to (a
conversation or your tracer). Own computers (made on the home Wi-Fi, or "hypernym dropship trust <friend>") see all.

    tests/dropship/.run/venv/bin/python tests/dropship/test_visibility.py
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ['DROPSHIP_HOME'] = tempfile.mkdtemp(prefix='dropship-vis-')
for k in ('DROPSHIP_SESSION', 'DROPSHIP_AS', 'DROPSHIP_MCP_PID', 'CODEX_THREAD_ID'):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins' / 'hypernym' / 'skills' / 'hypernym' / 'scripts'))
import dropship as d  # noqa: E402

GUEST, HOMEPC = 'c1' * 8, 'b3' * 8


class Fake:
    me = {'name': 'chris', 'id': 'self0000'}
    hold = d.Courier.hold


def deliver(name, pid, body, to_s=''):
    d.Courier.deliver(Fake(), name, body, 'mailbox', time.time(), to_s, 'theirs', pid)


def delivered():
    return [(m['from'], m['owner'], m['body']) for m in d.inbox() if m['dir'] == 'in' and 'held' not in m]


class Visibility(unittest.TestCase):
    def setUp(self):
        for f in (d.INBOX, d.FRIENDS, d.TALKS):
            f.unlink(missing_ok=True)
        d._private_dir()
        for sid, name in (('s-bridge', 'joe-bridge'), ('s-private', 'transportsession'), ('s-other', 'final-night-7')):
            d.save(d.SESSIONS / f'{sid}.json', {'n': 0, 'name': name, 'title': name, 'cwd': '/w', 'seen': time.time(),
                                                 'scan': 0, 'transcript': '', 'exposed': True, 'pid': 0, 'tty': '',
                                                 'app': '', 'borg': False})
        d.save(d.FRIENDS, {GUEST: {'name': 'devagent-mac', 'pub': 'aa', 'box': 'bb', 'how': 'paired'},
                           HOMEPC: {'name': 'chris-biggun', 'pub': 'cc', 'box': 'dd', 'how': 'home wi-fi'}})

    def test_a_code_paired_friend_sees_nothing_until_connected(self):
        self.assertEqual(d.sessions_for(GUEST), [])
        d.log_talk('s-bridge', 'devagent-mac', 'out')            # one of our sessions talks with it
        self.assertEqual(d.sessions_for(GUEST), ['joe-bridge'])
        self.assertEqual(len(d.sessions_for(HOMEPC)), 3, 'own computers see every added session')

    def test_it_reaches_only_connected_sessions_and_the_rest_is_held(self):
        deliver('devagent-mac', GUEST, 'hello nobody')            # no connection yet: held, not delivered
        self.assertEqual(delivered(), [])
        d.log_talk('s-bridge', 'devagent-mac', 'out')
        deliver('devagent-mac', GUEST, 'to the bridge', 'joe-bridge')
        deliver('devagent-mac', GUEST, 'sneaky', 'transportsession')
        deliver('devagent-mac', GUEST, 'bare')
        self.assertEqual([(o, b) for _f, o, b in delivered()], [('s-bridge', 'to the bridge'), ('s-bridge', 'bare')])
        self.assertIn('sneaky', [m['body'] for m in d.held_mail()])

    def test_typed_trust_makes_it_an_own_computer(self):
        out = json.loads(d.hook_prompt(json.dumps({'prompt': 'hypernym dropship trust devagent-mac',
                                                   'session_id': 's-private', 'transcript_path': '', 'cwd': '/w'})))
        self.assertIn('ALREADY handled', out['hookSpecificOutput']['additionalContext'])
        self.assertTrue(d.load(d.FRIENDS, {})[GUEST]['own'])
        self.assertEqual(len(d.sessions_for(GUEST)), 3)

    def test_friends_listing_explains_the_limit(self):
        self.assertIn('connected to', d.friends_text())
        self.assertIn('your own computer', d.friends_text())


if __name__ == '__main__':
    unittest.main()
