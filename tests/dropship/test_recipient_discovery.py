#!/usr/bin/env python3
"""Explicit destinations cannot become default-session or unowned mail."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory(prefix='dropship-recipient-')
os.environ['DROPSHIP_HOME'] = _tmp.name
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins/hypernym/skills/hypernym/scripts'))
import dropship as d
import dropship_tracer


class RecipientTests(unittest.TestCase):
    def deliver(self, target):
        c = d.Courier.__new__(d.Courier)
        c.me = {'name': 'receiver'}
        sessions = {'main-id': {'name': 'codex-main', 'aliases': ['stable-main'], 'seen': 2, 'default': True}}
        with patch.object(d, 'load', return_value={'peer-id': {}}), \
             patch.object(d, 'friend_policy', return_value={'blocked': False, 'scope': None}), \
             patch.object(d, 'reachable', return_value=sessions), \
             patch.object(d, 'talks', return_value=[]), \
             patch.object(d, 'peer_identity', return_value='peer-id'), \
             patch.object(dropship_tracer, 'receive', return_value=False), \
             patch.object(d, 'append_inbox') as inbox, \
             patch.object(d, 'log_talk') as talk, patch.object(d, 'log'):
            c.deliver('peer', 'hello', 'lan', 1, to_s=target, pid='peer-id')
            return inbox.call_args_list, talk.call_args_list

    def assert_held_not_delivered(self, target):
        """Since 0.7.52 undeliverable mail is HELD for the person (never thrown away): filed under no session, so it
        is neither delivered, nor defaulted, nor logged as a conversation."""
        inbox, talk = self.deliver(target)
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0].args[0]['owner'], '', 'not delivered to any session, not the default')
        self.assertIn('held', inbox[0].args[0])
        self.assertEqual(talk, [], 'not logged as a conversation')

    def test_typo_is_not_delivered_or_logged_as_conversation(self):
        self.assert_held_not_delivered('cdex-main')

    def test_malformed_target_does_not_become_default(self):
        self.assert_held_not_delivered('codex-main/other')

    def test_exact_and_stable_alias_reach_same_session(self):
        for target in ('codex-main', 'stable-main'):
            inbox, talk = self.deliver(target)
            self.assertEqual(len(inbox), 1)
            self.assertEqual(inbox[0].args[0]['owner'], 'main-id')
            self.assertEqual(len(talk), 1)

    def test_intentional_bare_machine_still_uses_default(self):
        inbox, _ = self.deliver('')
        self.assertEqual(inbox[0].args[0]['owner'], 'main-id')


if __name__ == '__main__':
    unittest.main()
