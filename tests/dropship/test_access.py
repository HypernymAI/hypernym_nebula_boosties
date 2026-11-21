#!/usr/bin/env python3
"""Session access: a window can only act as itself, nothing works until a session is added, no reading other
sessions' mail, and sends to restricted friends need an approval only the person can grant (typed at a prompt).

    python3 tests/dropship/test_access.py
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

HOME = tempfile.mkdtemp(prefix='dropship-access-')
os.environ['DROPSHIP_HOME'] = HOME
for k in ('DROPSHIP_SESSION', 'DROPSHIP_AS', 'DROPSHIP_MCP_PID', 'DROPSHIP_UNDER', 'CODEX_THREAD_ID'):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins' / 'hypernym' / 'skills' / 'hypernym' / 'scripts'))
import dropship as d  # noqa: E402

NICK = 'f' * 16


STARTED = 1000.0


def session(sid, name, host, exposed=True, title=None, seen=None, app='claude', started=STARTED):
    d.save(d.SESSIONS / f'{sid}.json', {'n': 0, 'name': name, 'title': name if title is None else title, 'cwd': '/w',
                                         'seen': seen or time.time(), 'scan': 0, 'transcript': '', 'exposed': exposed,
                                         'pid': host, 'host': host, 'host_start': started, 'host_app': app,
                                         'tty': '', 'app': '', 'borg': False})
    d.ACTIVE.mkdir(parents=True, exist_ok=True)                # what that window's hooks declare as its current session
    d.save(d.ACTIVE / f'{host}-{int(started)}.json', {'sid': sid, 'at': time.time()})


def run_cli(*argv, env=None):
    out = io.StringIO()
    with patch.object(sys, 'argv', ['dropship.py', *argv]), patch.dict(os.environ, env or {}), redirect_stdout(out):
        try:
            code = d.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            out.write(str(e.code) if not isinstance(e.code, int) else '')
    return code, out.getvalue()


class AccessTests(unittest.TestCase):
    def setUp(self):
        for f in list(d.SESSIONS.glob('*.json') if d.SESSIONS.exists() else []) + \
                list(d.ACTIVE.glob('*.json') if d.ACTIVE.exists() else []):
            f.unlink()
        d._private_dir()
        session('alpha-sid', 'alpha', 100)
        session('beta-sid', 'beta', 200)
        d.save(d.FRIENDS, {NICK: {'name': 'nick', 'pub': 'x', 'box': 'y', 'scope': ['alpha-sid'], 'scoped_by': 'tracer'}})
        d.SENT_LOG.unlink(missing_ok=True)

    def as_window(self, host, app='claude', started=STARTED):
        stack = __import__('contextlib').ExitStack()
        stack.enter_context(patch.object(d, 'host_pid', lambda start: host))
        stack.enter_context(patch.object(d, 'proc_start', lambda pid: started))
        stack.enter_context(patch.object(d, 'host_app', lambda pid: app))
        return stack

    def test_a_window_can_only_act_as_itself(self):
        with self.as_window(100), patch.dict(os.environ, {'DROPSHIP_MCP_PID': '4242', 'DROPSHIP_AS': 'beta'}):
            with self.assertRaises(SystemExit) as e:
                d.calling_session()
            self.assertIn('not this window', str(e.exception.code))
        with self.as_window(100), patch.dict(os.environ, {'DROPSHIP_MCP_PID': '4242', 'DROPSHIP_AS': ''}):
            self.assertEqual(d.calling_session(), 'alpha-sid')
        with self.as_window(100), patch.dict(os.environ, {'DROPSHIP_MCP_PID': '4242', 'DROPSHIP_SESSION': 'beta-sid'}):
            self.assertEqual(d.calling_session(), 'alpha-sid', 'DROPSHIP_SESSION cannot override the real window')

    def test_unverifiable_window_fails_closed(self):          # e.g. Windows lookup failed, or not under claude/codex
        with self.as_window(0), patch.dict(os.environ, {'DROPSHIP_MCP_PID': '4242', 'DROPSHIP_AS': 'alpha'}):
            with self.assertRaises(SystemExit) as e:
                d.calling_session()
            self.assertIn('cannot verify', str(e.exception.code))

    def test_reused_pid_is_not_the_old_window(self):
        with self.as_window(100, started=STARTED + 500), patch.dict(os.environ, {'DROPSHIP_MCP_PID': '4242'}):
            with self.assertRaises(SystemExit) as e:
                d.calling_session()
            self.assertIn('not registered', str(e.exception.code))

    def test_codex_threads_sharing_one_app_are_ambiguous(self):
        session('c1-sid', 'codex-one', 500, app='codex')
        session('c2-sid', 'codex-two', 500, app='codex')
        with self.as_window(500, app='codex'), patch.dict(os.environ, {'DROPSHIP_MCP_PID': '4242', 'DROPSHIP_AS': 'codex-one'}):
            with self.assertRaises(SystemExit) as e:
                d.calling_session()
            self.assertIn('several Codex threads', str(e.exception.code))

    def test_shared_host_uses_request_thread(self):
        session('c1-sid', 'codex-one', 500, app='codex')
        session('c2-sid', 'codex-two', 500, app='codex')
        with self.as_window(500, app='codex'), patch.dict(os.environ, {
            'DROPSHIP_MCP_PID': '4242', 'DROPSHIP_CALL_THREAD': 'c1-sid'
        }):
            self.assertEqual(d.calling_session(), 'c1-sid')
            with patch.dict(os.environ, {'DROPSHIP_AS': 'codex-two'}), self.assertRaises(SystemExit):
                d.calling_session()
            with patch.dict(os.environ, {'DROPSHIP_CALL_THREAD': 'alpha-sid'}), self.assertRaises(SystemExit):
                d.calling_session()

    def test_local_send_never_reaches_a_session_that_is_not_added(self):
        session('gamma-sid', 'gamma', 300, exposed=False)
        out = d.send(f"{d.identity()['name']}/gamma", 'hello', 'alpha-sid')
        self.assertIn('no added session', out)
        self.assertFalse([m for m in d.inbox() if m['owner'] == 'gamma-sid'])

    def test_restricted_friend_cannot_be_reached_through_a_route_without_its_id(self):
        with patch.object(d, 'directory', lambda: {'nick': {'kind': 'relay', 'relay': {}, 'label': 'r'}}), \
                patch.object(d, 'state', lambda: {'home': False, 'peers': [], 'relays': {}, 'warnings': []}), \
                patch.dict(os.environ, {'DROPSHIP_UNDER': '1'}):
            self.assertIn('own authenticated route', d.send('nick', 'x', 'alpha-sid'))

    def test_a_fresher_old_record_does_not_take_over_the_window(self):
        session('alpha-sid', 'alpha', 100)                                   # declared current
        d.save(d.SESSIONS / 'stale-sid.json', dict(d.session_record('alpha-sid'), name='stale', seen=time.time() + 999))
        with self.as_window(100), patch.dict(os.environ, {'DROPSHIP_MCP_PID': '4242'}):
            self.assertEqual(d.calling_session(), 'alpha-sid', 'only the declared session, never the freshest record')

    def test_record_without_a_start_time_is_not_accepted(self):
        r = d.session_record('alpha-sid')
        r.pop('host_start')
        d.save(d.SESSIONS / 'alpha-sid.json', r)
        with self.as_window(100), patch.dict(os.environ, {'DROPSHIP_MCP_PID': '4242'}):
            with self.assertRaises(SystemExit):
                d.calling_session()

    def test_unregistered_window_is_refused(self):
        with self.as_window(999), patch.dict(os.environ, {'DROPSHIP_MCP_PID': '4242', 'DROPSHIP_AS': 'alpha'}):
            with self.assertRaises(SystemExit) as e:
                d.calling_session()
            self.assertIn('not registered', str(e.exception.code))

    def test_reading_the_whole_computer_is_refused_to_agents(self):
        code, out = run_cli('read', '--all', env={'DROPSHIP_MCP_PID': '4242'})
        self.assertEqual(code, 1)
        self.assertIn('not available to agents', out)

    def test_not_added_session_cannot_send_read_or_wait(self):
        session('gamma-sid', 'gamma', 300, exposed=False)
        self.assertEqual(d.send('chris/alpha', 'hi', 'gamma-sid'), d.NOT_ADDED)
        self.assertFalse(d.session_record('gamma-sid')['exposed'], 'sending no longer adds a session')
        with self.as_window(300):
            for argv in (['read'], ['wait', '1']):
                code, out = run_cli(*argv, env={'DROPSHIP_MCP_PID': '4242'})
                self.assertEqual(code, 1, argv)
                self.assertIn('not added', out)

    def test_mail_and_routing_only_reach_added_sessions(self):
        session('gamma-sid', 'gamma', 300, exposed=False, seen=time.time() + 100)    # most recent, but not added
        reach = d.reachable()
        self.assertNotIn('gamma-sid', reach)
        self.assertTrue(any('default' in r for r in reach.values()), 'the default is an added session')
        self.assertNotIn('gamma', d.advertised_sessions())

    def test_prompt_hook_injects_nothing_into_a_window_that_is_not_added(self):
        session('gamma-sid', 'gamma', 300, exposed=False)
        d.append_inbox({'dir': 'in', 'via': 'local', 'from': d.identity()['name'], 'from_s': 'alpha', 'to': 'x',
                        'to_s': 'gamma', 'owner': 'gamma-sid', 'ts': time.time(), 'body': 'SECRET-ISH'})
        out = d.hook_prompt(json.dumps({'prompt': 'hello', 'session_id': 'gamma-sid', 'transcript_path': '', 'cwd': '/w'}))
        self.assertNotIn('SECRET-ISH', out)

    def test_register_needs_a_real_name_and_lists_the_session(self):
        session('delta-sid', 'delta-si', 400, exposed=False, title='')       # unnamed: id fallback
        with self.as_window(400):
            code, out = run_cli('register', env={'DROPSHIP_MCP_PID': '4242'})
        self.assertEqual(code, 1)
        self.assertIn('/rename', out)
        session('delta-sid', 'billing-api', 400, exposed=False)
        with self.as_window(400):
            code, out = run_cli('register', env={'DROPSHIP_MCP_PID': '4242'})
        self.assertEqual(code, 0, out)
        self.assertIn('billing-api', out)
        self.assertTrue(d.session_record('delta-sid')['exposed'])
        self.assertIn('billing-api', d.advertised_sessions())

    def test_approvals_only_by_typed_prompt_and_every_send_names_one(self):
        code, _ = run_cli('approve', 'nick', 'anything')
        self.assertNotEqual(code, 0, 'there is no CLI for approvals')
        prompt = lambda text: d.hook_prompt(json.dumps({'prompt': text, 'session_id': 'alpha-sid',
                                                        'transcript_path': '', 'cwd': '/w'}))
        out = json.loads(prompt('hypernym dropship approve nick the Granite write-up'))
        ctx = out['hookSpecificOutput']['additionalContext']
        self.assertNotIn('decision', out, 'not blocked: a blocked prompt is put back in the input box')
        self.assertIn('ALREADY handled', ctx)
        self.assertIn('Granite', ctx)
        f = d.load(d.FRIENDS, {})[NICK]
        self.assertEqual(d.approval_for(f, 'the granite write-up'), 'the Granite write-up')
        self.assertEqual(d.approval_for(f, '1'), 'the Granite write-up')
        self.assertEqual(d.approval_for(f, 'something else'), '')

        with patch.object(d, 'directory', lambda: {'nick': {'kind': 'mail', 'id': NICK}}), \
                patch.object(d, 'state', lambda: {'home': False, 'peers': [], 'relays': {}, 'warnings': []}), \
                patch.object(d, 'send_mail', lambda *a, **k: 'ok'):
            out = d.send('nick', 'numbers', 'alpha-sid')
            self.assertIn('Not sent', out)
            self.assertIn('hypernym dropship approve nick', out)
            with patch.dict(os.environ, {'DROPSHIP_UNDER': '1'}):
                self.assertTrue(d.send('nick', 'numbers', 'alpha-sid').startswith('sent'))
            self.assertIn('restricted', d.send('all', 'x', 'alpha-sid'))
        log = [json.loads(x) for x in d.SENT_LOG.read_text().splitlines()]
        self.assertEqual([(e['to_id'], e['item'], e['session']) for e in log], [(NICK, 'the Granite write-up', 'alpha')])
        self.assertIn('Granite', json.loads(prompt('hypernym dropship sent nick'))['hookSpecificOutput']['additionalContext'])
        json.loads(prompt('hypernym dropship revoke nick 1'))
        self.assertEqual(d.approvals_of(d.load(d.FRIENDS, {})[NICK]), [])

    def test_pasted_batch_is_one_command_per_line(self):
        prompt = lambda text: json.loads(d.hook_prompt(json.dumps({'prompt': text, 'session_id': 'alpha-sid',
                                                                   'transcript_path': '', 'cwd': '/w'})))
        out = prompt('hypernym dropship approve nick general advice --followups\n'
                     'hypernym dropship approve nick Granite results\n'
                     'hypernym dropship approve nick RunPod demand data\n'
                     'hypernym dropship revoke nick 3')
        self.assertNotIn('decision', out)
        items = d.approvals_of(d.load(d.FRIENDS, {})[NICK])
        self.assertEqual([a['item'] for a in items], ['general advice', 'Granite results'])
        self.assertTrue(items[0]['followups'])
        self.assertFalse(items[1]['followups'])
        out = prompt('hypernym dropship approve nick x --followupshypernym dropship approve nick y')   # leftover text
        self.assertIn('run together', out['hookSpecificOutput']['additionalContext'])
        self.assertEqual(len(d.approvals_of(d.load(d.FRIENDS, {})[NICK])), 2, 'nothing changed')

    def test_rename_and_nicknames_are_typed_only(self):
        for argv in (['name', 'evil'], ['nickname', 'nick', 'boss'], ['clear-nickname', 'nick']):
            code, out = run_cli(*argv, env={'DROPSHIP_MCP_PID': '4242'})
            self.assertEqual(code, 1, argv)
            self.assertIn('only your user', out)
        before = d.identity()['name']
        with patch.object(d, 'running', lambda: False):
            out = json.loads(d.hook_prompt(json.dumps({'prompt': 'hypernym dropship name typedname',
                                                       'session_id': 'alpha-sid', 'transcript_path': '', 'cwd': '/w'})))
        self.assertNotIn('decision', out)
        self.assertEqual(d.identity()['name'], 'typedname')
        d.local_control('name', before) if d.NAME_RE.match(before) else None

    def tracer(self, nonce, sid, session, expires=None, claimed=None):
        d.TRACERS.mkdir(exist_ok=True)
        inv = {'id': 'me', 'session': session, 'code': 'c' + nonce[:8], 'nonce': nonce, 'sid': sid,
               'expires': expires or time.time() + 600}
        if claimed:
            inv['claimed_by_id'] = claimed
        d.save(d.TRACERS / f'{nonce}.json', inv)

    def test_accepting_a_tracer_adds_that_session_to_an_existing_friends_reach(self):
        import dropship_tracer as t
        self.tracer('1' * 32, 'beta-sid', 'beta-1234')
        hello = t.CONTROL + json.dumps({'kind': 'hello', 'nonce': '1' * 32})
        self.assertEqual(t.admit(NICK, hello, 'beta-1234'), 'beta-sid')
        self.assertEqual(t.admit(NICK, hello, 'other-alias'), '', 'only for the session the tracer names')
        self.assertEqual(t.admit(NICK, 'plain text', 'beta-1234'), '')
        self.tracer('2' * 32, 'beta-sid', 'beta-1234', expires=time.time() - 1)
        self.assertEqual(t.admit(NICK, t.CONTROL + json.dumps({'kind': 'hello', 'nonce': '2' * 32}), 'beta-1234'), '',
                         'expired tracers admit nobody')
        self.tracer('3' * 32, 'beta-sid', 'beta-1234', claimed='a' * 16)
        self.assertEqual(t.admit(NICK, t.CONTROL + json.dumps({'kind': 'hello', 'nonce': '3' * 32}), 'beta-1234'), '',
                         'a claimed tracer admits only its claimer')
        c = d.Courier.__new__(d.Courier)
        c.me = {'name': 'me', 'id': 'me'}
        with patch('dropship_tracer.receive', lambda *a: True):
            c.deliver('nick', hello, 'mailbox', time.time(), 'beta-1234', '', NICK)
        self.assertIn('beta-sid', d.friend_policy(NICK)['scope'])
        self.assertIn('alpha-sid', d.friend_policy(NICK)['scope'], 'existing reach kept')

    def test_several_open_tracers_all_stay_valid(self):
        import dropship_tracer as t
        self.tracer('4' * 32, 'alpha-sid', 'alpha-1')
        self.tracer('5' * 32, 'beta-sid', 'beta-1')
        self.assertEqual(t._invitation('4' * 32)[1]['sid'], 'alpha-sid')
        self.assertEqual(t._invitation('5' * 32)[1]['sid'], 'beta-sid')
        self.assertEqual(d.tracer_by_code('c' + '4' * 8)['sid'], 'alpha-sid')
        self.assertEqual(d.tracer_by_code('c' + '5' * 8)['sid'], 'beta-sid')

    def test_parallel_tracer_pairings_scope_each_friend_to_its_own_session(self):
        import threading
        self.tracer('6' * 32, 'alpha-sid', 'alpha-1')
        self.tracer('7' * 32, 'beta-sid', 'beta-1')
        c = d.Courier.__new__(d.Courier)
        c.friends, c.peers, c.lock, c.stop = d.load(d.FRIENDS, {}), {}, threading.Lock(), threading.Event()
        c._tl, c.tracer_jobs = threading.local(), set()
        c.me = {'name': 'me', 'id': 'me'}
        barrier = threading.Barrier(2)

        class FakeMail:
            def pair(self, code, done, wanted, until):
                barrier.wait(5)                                       # both pairings in flight at once
                pid = ('8' if code == 'c' + '6' * 8 else '9') * 16
                done('done', c.add_friend({'id': pid, 'name': 'guest' + pid[0], 'pub': 'p' + pid, 'box': 'b'}, 'paired'))
        c.mail = FakeMail()
        d.PAIRINGS.mkdir(exist_ok=True)
        jobs = []
        for code in ('c' + '6' * 8, 'c' + '7' * 8):
            path = d.PAIRINGS / f'{code}.json'
            d.save(path, {'code': code, 'status': 'waiting', 'started': time.time(), 'until': time.time() + 600})
            jobs.append(threading.Thread(target=c.run_tracer_pairing, args=(path, d.load(path, {}))))
        [j.start() for j in jobs]
        [j.join(10) for j in jobs]
        f = d.load(d.FRIENDS, {})
        self.assertEqual(f['8' * 16]['scope'], ['alpha-sid'])
        self.assertEqual(f['9' * 16]['scope'], ['beta-sid'])

    def test_a_session_outside_a_friends_reach_cannot_message_it(self):
        with patch.object(d, 'directory', lambda: {'nick': {'kind': 'mail', 'id': NICK}}), \
                patch.object(d, 'state', lambda: {'home': False, 'peers': [], 'relays': {}, 'warnings': []}), \
                patch.object(d, 'send_mail', lambda *a, **k: 'ok'), patch.dict(os.environ, {'DROPSHIP_UNDER': '1'}):
            out = d.send('nick', 'hi', 'beta-sid')
        self.assertIn('can only talk with', out)

    def test_undeliverable_mail_is_held_never_thrown_away(self):
        d.save(d.HELD_STATE, {})
        c = d.Courier.__new__(d.Courier)
        c.me = {'name': 'me', 'id': 'me'}
        c.deliver('nick', 'to the wrong window', 'mailbox', time.time(), 'beta', '', NICK)     # outside nick's reach
        c.deliver('nick', 'to nowhere', 'mailbox', time.time(), 'nosuchsession', '', NICK)
        c.deliver('nick', 'bad address', 'mailbox', time.time(), 'BAD NAME!', '', NICK)
        c.deliver('stranger', 'forged', 'mailbox', time.time(), 'alpha', '', 'e' * 16)        # not a friend: dropped
        held = d.held_mail()
        self.assertEqual([m['body'] for m in held], ['to the wrong window', 'to nowhere', 'bad address'])
        self.assertFalse(any(m['owner'] for m in held), 'held mail is filed under no session')
        with self.as_window(200):                                              # beta never sees it on its own
            self.assertEqual(d.unread_for('beta-sid'), [])
        prompt = lambda text, sid='alpha-sid': json.loads(d.hook_prompt(json.dumps(
            {'prompt': text, 'session_id': sid, 'transcript_path': '', 'cwd': '/w'})))['hookSpecificOutput']['additionalContext']
        self.assertIn('to the wrong window', prompt('hypernym dropship held'))
        n = str(held[0]['n'])
        self.assertIn('released', prompt(f'hypernym dropship release {n} beta'))
        got = d.unread_for('beta-sid')
        self.assertEqual([m['body'] for m in got], ['to the wrong window'], 'released into beta, past the reach check')
        self.assertIn('deleted 2', prompt('hypernym dropship delete-held all'))
        self.assertEqual(d.held_mail(), [])

    def test_courier_save_keeps_approvals(self):
        f = d.load(d.FRIENDS, {})
        f[NICK]['approvals'] = [{'item': 'general advice', 'at': 1, 'by': 'typed', 'followups': False}]
        d.save(d.FRIENDS, f)
        c = d.Courier.__new__(d.Courier)
        c.friends = {NICK: {'name': 'nick', 'pub': 'x', 'box': 'y'}}               # courier's stale copy
        c.save_friends()
        self.assertEqual(d.approvals_of(d.load(d.FRIENDS, {})[NICK])[0]['item'], 'general advice')


if __name__ == '__main__':
    unittest.main()
