#!/usr/bin/env python3
"""Peer isolation: a scoped friend reaches only its sessions, a blocked friend nothing; tracer pairing scopes itself.

    python3 tests/dropship/test_isolation.py
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HOME = tempfile.mkdtemp(prefix='dropship-iso-')
os.environ['DROPSHIP_HOME'] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins' / 'hypernym' / 'skills' / 'hypernym' / 'scripts'))
import dropship as d  # noqa: E402

PUB, PRIV, TEAM = 'pub-publishing-sid', 'priv-transport-sid', 'teammate0000001'


class Fake:
    """The parts of the Courier that deliver() uses."""
    me = {'name': 'chris', 'id': 'self0000'}


IDS = {'teammate': TEAM, 'benito': 'benito00'}


def deliver(name, body, to_s='', pid=None):
    """As the transports do: with the sender's AUTHENTICATED machine id."""
    d.Courier.deliver(Fake(), name, body, 'mailbox', time.time(), to_s, 'theirs', IDS[name] if pid is None else pid)


def delivered():
    return [(m['from'], m['owner'], m['body']) for m in d.inbox() if m['dir'] == 'in']


class IsolationTests(unittest.TestCase):
    def setUp(self):
        for f in (d.INBOX, d.FRIENDS):
            if f.exists():
                f.unlink()
        for sid, name in ((PUB, 'final-night-7-publishing'), (PRIV, 'transportsession')):
            d.save(d.SESSIONS / f'{sid}.json', {'n': 0, 'name': name, 'title': name, 'cwd': '/tmp', 'seen': time.time(),
                                                 'scan': 0, 'transcript': '', 'exposed': True, 'pid': 0, 'tty': '',
                                                 'app': '', 'borg': True})
        d.save(d.FRIENDS, {TEAM: {'name': 'teammate', 'pub': 'aa', 'box': 'bb', 'how': 'paired'},
                           'benito00': {'name': 'benito', 'pub': 'cc', 'box': 'dd', 'how': 'paired'}})

    def test_existing_friends_keep_full_access(self):
        deliver('benito', 'hi transport', 'transportsession')
        self.assertEqual(delivered()[-1][1], PRIV)
        self.assertIn('transportsession', d.sessions_for('benito00'))

    def test_scoped_friend_cannot_reach_other_sessions(self):
        print(d.set_policy('teammate', scope=['final-night-7-publishing']))
        deliver('teammate', 'sneaky', 'transportsession')            # another session by name → dropped
        deliver('teammate', 'alias trick', 'priv-transport-alias')   # unknown alias → dropped
        self.assertEqual(delivered(), [])
        deliver('teammate', 'bare machine mail')                     # no session → forced into scope
        deliver('teammate', 'to publishing', 'final-night-7-publishing')
        self.assertEqual([o for _f, o, _b in delivered()], [PUB, PUB])
        self.assertEqual(d.sessions_for(TEAM), ['final-night-7-publishing'], 'only the scoped name is advertised')

    def test_blocked_friend_gets_nothing(self):
        d.set_policy('teammate', blocked=True)
        deliver('teammate', 'anything', 'final-night-7-publishing')
        deliver('teammate', 'bare')
        self.assertEqual(delivered(), [])
        self.assertEqual(d.sessions_for(TEAM), [])
        d.set_policy('teammate', blocked=False)
        deliver('teammate', 'unblocked')
        self.assertEqual(len(delivered()), 1)

    def test_trust_lifts_the_scope(self):
        d.set_policy('teammate', scope=['final-night-7-publishing'])
        d.set_policy('teammate', trust=True)
        deliver('teammate', 'now trusted', 'transportsession')
        self.assertEqual(delivered()[-1][1], PRIV)

    def test_tracer_pairing_scopes_atomically(self):
        import threading
        friends = d.load(d.FRIENDS, {})
        del friends[TEAM]
        d.save(d.FRIENDS, friends)
        d.save(d.HOME / 'tracer-invitation.json', {'code': 'c0de', 'sid': PUB, 'session': 'final-night-7-publ-x'})
        d.save(d.PAIRING, {'code': 'c0de', 'status': 'waiting', 'started': time.time()})

        class C:
            lock, friends, peers = threading.Lock(), d.load(d.FRIENDS, {}), {}
            refresh_names = d.Courier.refresh_names
            save_friends = d.Courier.save_friends
        c = C()
        d.Courier.add_friend(c, {'id': TEAM, 'name': 'teammate', 'pub': 'aa', 'box': 'bb'}, 'paired')
        self.assertEqual(d.load(d.FRIENDS, {})[TEAM]['scope'], [PUB], 'scope written in the SAME save as the friendship')
        deliver('teammate', 'sneaky', 'transportsession')
        self.assertEqual(delivered(), [])

    def test_unknown_or_unauthenticated_peer_fails_closed(self):
        deliver('teammate', 'no id', 'final-night-7-publishing', pid='')
        deliver('teammate', 'stranger id', 'final-night-7-publishing', pid='ffffffffffffffff')
        self.assertEqual(delivered(), [])

    def test_renamed_label_cannot_borrow_trust(self):
        # the teammate (scoped) claims Benito's display name: policy follows its authenticated id, not the name
        d.set_policy('teammate', scope=['final-night-7-publishing'])
        d.Courier.deliver(Fake(), 'benito', 'pretending', 'mailbox', time.time(), 'transportsession', 'x', TEAM)
        self.assertEqual(delivered(), [])

    def test_cli_policy_survives_courier_save(self):
        import threading
        class C:
            lock, peers = threading.Lock(), {}
            save_friends = d.Courier.save_friends
        c = C()
        c.friends = d.load(d.FRIENDS, {})               # courier's in-memory copy, loaded BEFORE the CLI change
        d.set_policy('teammate', blocked=True)          # CLI
        c.save_friends()                                # courier writes its (stale) copy
        self.assertTrue(d.friend_policy(TEAM)['blocked'])
        d.set_policy('teammate', trust=True)
        c.save_friends()
        self.assertFalse(d.friend_policy(TEAM)['blocked'])

    def test_revocation_holds_unread_mail(self):
        deliver('teammate', 'before revocation', 'transportsession')     # full trust at the time
        self.assertEqual(delivered()[-1][1], PRIV)
        d.set_policy('teammate', scope=['final-night-7-publishing'])
        self.assertEqual(d.unread_for(PRIV), [], 'revoked: the already-delivered mail is no longer consumable')

    def test_legacy_unattributed_remote_mail_fails_closed(self):
        d.append_inbox({'dir': 'in', 'via': 'mailbox', 'from': 'someone-gone', 'to': 'chris', 'owner': PRIV,
                        'ts': time.time(), 'body': 'old mail without from_id'})
        d.append_inbox({'dir': 'in', 'via': 'local', 'from': 'chris', 'to': 'chris', 'owner': PRIV,
                        'ts': time.time(), 'body': 'a local session'})
        self.assertEqual([m['body'] for m in d.unread_for(PRIV)], ['a local session'])

    def test_delivery_then_revocation_interleaving(self):
        # mail passes the policy at delivery, then the friend is blocked before the session reads it
        deliver('teammate', 'race', 'final-night-7-publishing')
        d.set_policy('teammate', blocked=True)
        self.assertEqual(d.unread_for(PUB), [])
        d.set_policy('teammate', blocked=False)          # unblocking restores what is still unread

    def test_unknown_friend_name_refused(self):
        self.assertIn('no friend called', d.set_policy('nobody', blocked=True))


if __name__ == '__main__':
    unittest.main()
