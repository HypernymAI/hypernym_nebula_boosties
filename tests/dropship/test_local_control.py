"""Identity, naming, routing, and startup regressions with isolated local state."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins/hypernym/skills/hypernym/scripts'))
import dropship as d
import dropship_tracer as t
import hypernym_mcp_server as mcp

class LocalControlTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        home = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for key, filename in {'HOME': '.', 'IDENTITY': 'identity.json', 'CONFIG': 'config.json',
                              'FRIENDS': 'friends.json', 'STATE': 'state.json', 'TALKS': 'talks.jsonl',
                              'SESSIONS': 'sessions', 'OUTBOX': 'outbox'}.items():
            stack.enter_context(patch.object(d, key, home / filename))
        stack.enter_context(patch.object(d, 'running', return_value=False))
        stack.enter_context(patch.object(d, 'network', return_value={}))
        stack.enter_context(patch.object(d, 'log'))
        d.save(d.IDENTITY, {'id': 'local-id', 'name': 'local', 'priv': '2', 'pub': '4', 'box_priv': 'secret'})

    def friend(self, name='remote', key='8'):
        return dict(name=name, pub=key, box='box', how='test', since=1)

    def test_first_identity_and_rename_preserve_trust_and_keys(self):
        before = d.identity()
        friends = {'remote-id': self.friend()}
        d.save(d.FRIENDS, friends)
        d.local_control('name', 'fieldriser')
        self.assertEqual(d.identity(), dict(before, name='fieldriser'))
        self.assertEqual(d.load(d.FRIENDS, {}), friends)
        self.assertEqual(d.config()['homes'], {})
        d.IDENTITY.unlink()
        first = d.identity()
        self.assertEqual(d.identity(), first)
        self.assertTrue(d.NAME_RE.fullmatch(first['name']))
        self.assertEqual(d.config()['homes'], {})

    def test_collisions_are_order_independent_and_cover_own_name(self):
        f = {'b': self.friend('same', '8'), 'a': self.friend('same', '10'), 'c': self.friend('local', '12')}
        labels = d.friend_names(f)
        self.assertEqual(labels, d.friend_names(dict(reversed(list(f.items())))))
        self.assertEqual(len(set(labels.values())), 3)
        self.assertNotIn('same', labels.values())
        self.assertNotIn('local', labels.values())

    def test_nickname_survives_peer_rename_and_thread_follows_id(self):
        d.save(d.FRIENDS, {'remote-id': self.friend()})
        d.save(d.SESSIONS / 'a.json', {'name': 'work', 'seen': 2, 'exposed': True, 'pid': 0})
        d.log_talk('a', 'remote/work', 'out')
        d.local_control('nickname', 'brother', 'remote')
        c = d.Courier()
        c.peer_renamed('remote-id', 'renamed')
        self.assertEqual(c.friends['remote-id']['name'], 'brother')
        self.assertEqual(d.owner_of('', 'brother'), 'a')
        d.local_control('clear-nickname', peer='brother')
        self.assertEqual(d.friend_names()['remote-id'], 'renamed')

    def test_lan_and_pairing_use_same_collision_rules_and_keep_keys(self):
        c = d.Courier()
        c.befriend('a', 'same', '8', '10.0.0.1', 123)
        c.add_friend(dict(id='b', **self.friend('same', '10')), 'pair')
        self.assertEqual(c.peers['a']['name'], c.friends['a']['name'])
        self.assertNotEqual(c.friends['a']['name'], c.friends['b']['name'])
        c.befriend('a', 'changed', '8', '10.0.0.1', 123)
        self.assertEqual(c.friends['a']['name'], 'changed')
        with self.assertRaises(ValueError):
            c.add_friend(dict(id='a', **self.friend('bad', '12')), 'pair')
        self.assertEqual(c.friends['a']['pub'], '8')

    def test_reused_name_cannot_take_over_an_existing_thread(self):
        d.save(d.FRIENDS, {'first': self.friend()})
        d.save(d.SESSIONS / 'a.json', {'name': 'work', 'seen': 1, 'exposed': True, 'pid': 0})
        d.save(d.SESSIONS / 'b.json', {'name': 'default', 'seen': 2, 'exposed': True, 'pid': 0})
        d.log_talk('a', 'remote/work', 'out')
        d.save(d.FRIENDS, {'first': self.friend('renamed'), 'second': self.friend('remote', '10')})
        self.assertEqual(d.owner_of('', 'renamed'), 'a')
        self.assertEqual(d.owner_of('', 'remote'), 'b')

    def test_mail_queue_keeps_identity_during_rename(self):
        d.save(d.FRIENDS, {'remote-id': self.friend()})
        def complete(_):
            for path in d.OUTBOX.glob('*.json'):
                self.assertEqual(d.load(path, {})['to_id'], 'remote-id')
                d.save(path.with_suffix('.done'), {'result': 'ok'})
        with patch.object(d.time, 'sleep', side_effect=complete):
            self.assertEqual(d.send_mail('remote', 'hello'), 'ok')

    def test_mcp_control_and_old_courier_restart(self):
        with patch.object(mcp, 'run_dropship', return_value=('ok', False)) as run:
            mcp.call_tool('dropship_local', {'action': 'nickname', 'peer': '#remote', 'name': 'brother'})
            run.assert_called_once_with(['nickname', '#remote', 'brother'])
        with patch.object(d, 'running', return_value=True), patch.object(d, 'stop_daemon') as stop, patch.object(d, 'start_daemon') as start, patch.object(d, 'state', side_effect=[{'tracer_version': 0}, {'local_control_version': 1, 'mail': {'ok': True}}]):
            t.ready()
            stop.assert_called_once()
            start.assert_called_once()

if __name__ == '__main__':
    unittest.main()
