#!/usr/bin/env python3
"""
Dropship end-to-end tests. Real daemons, real sockets, two (or three) machines simulated on this one.

    python3 tests/dropship/test_dropship.py              # everything local (home Wi-Fi, sessions, security, bar)
    python3 tests/dropship/test_dropship.py --network    # + the public mailbox: pairing and messages via ntfy.sh
    python3 tests/dropship/test_dropship.py -k sessions  # only tests whose name contains "sessions"

Each run works in tests/dropship/.run/<time>/ (gitignored): one Dropship home per simulated machine, their logs and
inboxes, kept after the run for inspection. The crypto environment is built once in tests/dropship/.run/venv.
Test machines use their own discovery port (DROPSHIP_UDP), so they never befriend the real Dropship on this Mac.
"""
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / 'plugins' / 'hypernym' / 'skills' / 'hypernym' / 'scripts'
DROPSHIP = SCRIPTS / 'dropship.py'
RUN_ROOT = Path(__file__).resolve().parent / '.run'
RUN = RUN_ROOT / time.strftime('%Y%m%d-%H%M%S')
VENV = RUN_ROOT / 'venv'
UDP = '47977'                                  # never the real 47777
ANSI = re.compile(r'\x1b\[[0-9;]*m')


class Machine:
    """One simulated computer: its own Dropship home, TCP port and name."""
    count = 0

    def __init__(self, name: str, home_wifi: bool = True):
        Machine.count += 1
        self.name, self.home = name, RUN / name
        self.port = str(47900 + Machine.count)
        self.home.mkdir(parents=True)
        (self.home / 'transcripts').mkdir()
        self.env = dict(os.environ, DROPSHIP_HOME=str(self.home), DROPSHIP_PORT=self.port, DROPSHIP_UDP=UDP,
                        DROPSHIP_VENV=str(VENV))
        self.py(f"""
c = d.config(); c['homes'] = {{d.network()['fingerprint']: 'test'}} if {home_wifi} else {{}}; d.save(d.CONFIG, c)
me = d.identity(); me['name'] = {name!r}; d.save(d.IDENTITY, me)""")

    def cli(self, *args, stdin: str = '', extra_env=None) -> str:
        r = subprocess.run([sys.executable, str(DROPSHIP), *args], input=stdin, capture_output=True, text=True,
                           env=dict(self.env, **(extra_env or {})), timeout=210 if args[:1] == ('tracer',) else 150)
        if r.returncode != 0:
            raise AssertionError(f'{self.name}: dropship {" ".join(args)} failed:\n{r.stderr}')
        return r.stdout.strip()

    def py(self, code: str) -> str:
        r = subprocess.run([sys.executable, '-c', f'import sys; sys.path.insert(0, {str(SCRIPTS)!r}); '
                            f'import dropship as d\n{code}'], capture_output=True, text=True, env=self.env, timeout=60)
        if r.returncode != 0:
            raise AssertionError(f'{self.name}: python failed:\n{r.stderr}')
        return r.stdout.strip()

    def start(self):
        self.cli('daemon')
        wait(lambda: 'daemon running' in self.cli('status'), 30, f'{self.name} daemon up')

    def stop(self):
        self.cli('off')

    def session(self, sid: str, title: str = '') -> 'Session':
        t = self.home / 'transcripts' / f'{sid}.jsonl'
        t.write_text(json.dumps({'type': 'custom-title', 'customTitle': title, 'sessionId': sid}) + '\n' if title else '')
        s = Session(self, sid, t)
        s.prompt('hello')                       # a session registers on its first prompt
        return s

    def inbox(self) -> list:
        return [json.loads(ln) for ln in (self.home / 'inbox.jsonl').read_text().splitlines()] \
            if (self.home / 'inbox.jsonl').exists() else []


class Session:
    def __init__(self, m: Machine, sid: str, transcript: Path):
        self.m, self.sid, self.transcript = m, sid, transcript

    def prompt(self, text: str) -> dict:
        out = self.m.cli('hook-prompt', stdin=json.dumps({'prompt': text, 'session_id': self.sid,
                                                          'transcript_path': str(self.transcript),
                                                          'cwd': f'/tmp/{self.sid}'}))
        return json.loads(out) if out else {}

    def got(self) -> str:
        """What Dropship adds to this session's next prompt ('' = nothing)."""
        out = self.prompt('anything new?')
        return out['hookSpecificOutput']['additionalContext'] if out else ''

    def send(self, line: str) -> str:
        """'#to text' → dropship_send exactly as Claude calls it from this session; 'hypernym dropship …' → the
        instant prompt command."""
        if line.startswith('#'):
            to, _, body = line.partition(' ')
            return self.m.cli('send', to, body, extra_env={'DROPSHIP_SESSION': self.sid})
        out = self.prompt(line)
        assert out and 'decision' in out, f'{line!r} was not answered by Dropship'
        return out['reason']

    def wait(self, secs: int) -> str:
        return self.m.cli('wait', str(secs), extra_env={'DROPSHIP_SESSION': self.sid})


def wait(cond, secs: float, what: str):
    deadline = time.time() + secs
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.5)
    raise AssertionError(f'timed out waiting for: {what}')


def check(ok: bool, what: str):
    if not ok:
        raise AssertionError(what)


# ------------------------------------------------------------------------------------------------------------ tests
def test_home_wifi_discovery_and_message(a, b, c, d):
    wait(lambda: '#bob' in a.cli('status'), 20, 'alice sees bob')
    wait(lambda: '#alice' in b.cli('status'), 20, 'bob sees alice')
    out = a.cli('send', '#bob', 'hello over the home wi-fi')
    check('sent to #bob' in out, out)
    wait(lambda: any(m['body'] == 'hello over the home wi-fi' for m in b.inbox()), 10, 'bob receives')


def test_sessions_targeted_routing(a, b, c, d):
    transport, other = a.session('a-transport-1', 'TransportSession'), a.session('a-other-2', 'Other Window')
    build, chat = b.session('b-build-1', 'Build'), b.session('b-chat-2', 'Chat')
    check('reachable as #bob/build' in build.send('hypernym dropship add'), 'build joins')
    check('reachable as #alice/transportsession' in transport.send('hypernym dropship add'), 'transport joins')
    chat.prompt('I am the most recent bob session')          # → chat is bob's default
    wait(lambda: 'build' in a.cli('status'), 20, "alice learns bob's session names")
    r = transport.send('#bob/build can you run the FN7 test?')
    check('sent to #bob/build' in r, r)
    wait(lambda: any(m['body'].startswith('can you run') for m in b.inbox()), 10, 'bob receives')
    got = build.got()
    check('can you run the FN7 test?' in got, f'build did not get it: {got!r}')
    check('#alice/transportsession' in got, f'sender session missing: {got!r}')
    check('You are #bob/build' in got, f'build not told its own address: {got!r}')
    check(chat.got() == '', 'chat (even as the default) must NOT get a message addressed to build')
    # bare reply follows the thread back to transport only
    r = build.send('#alice all green')
    wait(lambda: any(m['body'] == 'all green' for m in a.inbox()), 10, 'alice receives reply')
    check('all green' in transport.got(), 'transport (the thread) must get the reply')
    check(other.got() == '', 'other window must NOT get it')


def test_sessions_unknown_name_refused(a, b, c, d):
    s = a.session('a-x-3', 'Xray')
    y = b.session('b-y-3', 'Yankee')                          # most recent → bob's default
    hidden = b.session('b-hidden-4', 'Hidden')                # newer still → the default now; yankee is not added
    y.prompt('x')                                             # yankee most recent again → default; hidden unreachable
    wait(lambda: 'yankee' in a.cli('status') and 'hidden' not in a.cli('status'), 20, 'alice learns the defaults')
    check('has no session called nope' in s.send('#bob/nope hi'), 'unknown name refused')
    check('has no session called hidden' in s.send('#bob/hidden hi'), 'a session that was not added is not reachable')


def test_unthreaded_message_goes_to_default_only(a, b, c, d):
    wait(lambda: '#alice' in d.cli('status'), 20, 'dave sees alice')
    newest = a.session('a-newest-6', 'Newest')               # alice's most recent session = her default
    out = d.cli('send', '#alice', 'hello from dave, no thread')
    check('sent to #alice' in out, out)
    wait(lambda: any(m['from'] == 'dave' for m in a.inbox()), 10, 'alice receives from dave')
    msg = [m for m in a.inbox() if m['dir'] == 'in' and m['from'] == 'dave'][-1]
    check(msg['owner'] == 'a-newest-6', f'no thread → the default session only, got owner {msg["owner"]!r}')
    for sid in ('a-transport-1', 'a-other-2', 'a-x-3'):
        got = Session(a, sid, a.home / 'transcripts' / f'{sid}.jsonl').got()
        check('dave' not in got, f'{sid} must not get it')
    check('hello from dave' in newest.got(), 'the default session gets it')


def test_same_computer_sessions(a, b, c, d):
    one, two = a.session('a-local-7', 'LocalOne'), a.session('a-local-8', 'LocalTwo')
    check('reachable' in two.send('hypernym dropship add'), 'localtwo joins')
    r = one.send('#alice/localtwo ping from the next tab')
    check('(this computer)' in r, r)
    got = two.got()
    check('ping from the next tab' in got and '#alice/localone' in got, got)
    three = a.session('a-local-10', 'LocalThree')            # never added: on your OWN computer, reachable by name
    r = one.send('#alice/localthree hi from the same computer')
    check('(this computer)' in r, r)
    check('hi from the same computer' in three.got(), 'own sessions reach each other by name without add')


def test_sessions_report_waiting(a, b, c, d):
    w = a.session('a-wait-9', 'Waiter')
    with open(w.transcript, 'a') as f:                      # Claude answered last → the person's turn
        f.write(json.dumps({'type': 'user', 'message': {'content': 'do the thing'}}) + '\n')
        f.write(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'done!'}]}}) + '\n')
    out = a.cli('sessions')
    first = out.splitlines()[0]
    check('#alice/waiter' in first and 'WAITING FOR YOU' in first, f'waiter first and waiting:\n{out}')
    with open(w.transcript, 'a') as f:
        f.write(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'x'}]}}) + '\n')
    check('working' in a.cli('sessions').splitlines()[0], 'tool running → working')
    check('hypernym dropship awake waiter' in out, out)


def test_awake_targets_the_tab(a, b, c, d):
    script = a.cli('awake', 'waiter', '--dry-run')
    rec = json.loads((a.home / 'sessions' / 'a-wait-9.json').read_text())
    check(rec['tty'].startswith('/dev/tty') and f'tty of' in script and rec['tty'] in script, script)


def test_sender_is_auto_added_and_alias(a, b, c, d):
    quiet = a.session('a-quiet-11', 'Quiet')                 # never said "add"
    newest = a.session('a-newest-12', 'Newer')               # so quiet is not the default either
    check(not json.loads((a.home / 'sessions' / 'a-quiet-11.json').read_text())['exposed'], 'starts unreachable')
    r = quiet.send('#bob-build alias form works')             # #machine-session
    check('sent to #bob/build' in r and 'dropship_wait' in r, r)
    check(json.loads((a.home / 'sessions' / 'a-quiet-11.json').read_text())['exposed'], 'sending auto-adds the sender')
    wait(lambda: 'quiet' in b.cli('status'), 20, 'bob learns #alice/quiet')


def test_listener_wakes_with_the_reply(a, b, c, d):
    quiet = Session(a, 'a-quiet-11', a.home / 'transcripts' / 'a-quiet-11.jsonl')
    build = Session(b, 'b-build-1', b.home / 'transcripts' / 'b-build-1.jsonl')
    got = build.got()
    check('alias form works' in got and '#alice/quiet' in got and 'Answer it yourself' in got, got)
    p = subprocess.Popen([sys.executable, str(DROPSHIP), 'listen', 'a-quiet-11'], env=a.env, stdout=subprocess.PIPE,
                         text=True)
    time.sleep(1.5)
    check(p.poll() is None, 'listener waits while nothing has arrived')
    again = subprocess.run([sys.executable, str(DROPSHIP), 'listen', 'a-quiet-11'], env=a.env, capture_output=True,
                           text=True, timeout=10).stdout
    check('already running' in again, 'one listener per session')
    build.send('#alice/quiet reply from build')
    out, _ = p.communicate(timeout=20)
    check(p.returncode == 0 and 'reply from build' in out and 'start the listener again' in out, out)
    check(quiet.got() == '', 'the listener consumed it: not shown twice')


def test_typed_text_is_never_sent(a, b, c, d):
    s = a.session('a-typed-13', 'Typed')
    before = len(b.inbox())
    out = s.prompt('#bob/build this is just a prompt, not a message')
    check(out == {} or 'decision' not in out, f'a typed #… prompt must reach Claude untouched: {out}')
    time.sleep(1.5)
    check(len(b.inbox()) == before, 'nothing may be sent because of typed text')


def test_wait_works_like_wait_for_reply(a, b, c, d):
    asker = a.session('a-asker-14', 'Asker')
    build = Session(b, 'b-build-1', b.home / 'transcripts' / 'b-build-1.jsonl')
    t0 = time.time()
    check('timed out' in asker.wait(2) and time.time() - t0 >= 1.5, 'wait blocks, then times out quietly')
    asker.send('#bob/build what is 2+2?')
    replier = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'], env=b.env)
    replier.wait()
    build.send('#alice/asker 4')                              # arrives while asker is waiting
    out = asker.wait(20)
    check('#bob/build' in out and ': 4' in out and 'You are #alice/asker' in out, out)
    check('timed out' in asker.wait(1), 'marked read: not returned twice')
    build.send('#alice/asker already here')                   # arrives BEFORE wait is called
    time.sleep(1)
    check('already here' in asker.wait(5), 'an unread message is returned at once')


def test_borg_mode(a, b, c, d):
    drone = a.session('a-drone-15', 'Drone')
    out = drone.prompt('hypernym dropship borg')
    ctx = out['hookSpecificOutput']['additionalContext'] if out else ''
    check('BORG MODE is on' in ctx and 'dropship_wait' in ctx and 'call dropship_wait' in ctx, f'borg starts the loop:\n{ctx}')
    rec = json.loads((a.home / 'sessions' / 'a-drone-15.json').read_text())
    check(rec['borg'] and rec['exposed'], 'borg makes the session reachable and remembers the mode')
    later = drone.prompt('unrelated question')['hookSpecificOutput']['additionalContext']
    check('go back to the dropship_wait loop' in later, 'every later prompt reminds a borg session to resume')
    other = Session(b, 'b-build-1', b.home / 'transcripts' / 'b-build-1.jsonl')
    wait(lambda: 'drone' in b.cli('status'), 20, 'bob learns #alice/drone')
    sent = other.send('#alice/drone are you there?')
    check('sent to #alice/drone' in sent, sent)
    w = drone.wait(10)
    check('are you there?' in w and 'Borg mode: after answering, call dropship_wait' in w, w)
    check('borg: answers on its own' in a.cli('sessions'), 'sessions shows borg')
    off = drone.send('hypernym dropship borg off')
    check('borg off' in off, off)
    check(not json.loads((a.home / 'sessions' / 'a-drone-15.json').read_text())['borg'], 'borg off is remembered')
    check(drone.prompt('normal again') == {}, 'no more borg reminders after borg off')


def test_mcp_wait_never_blocks_a_send(a, b, c, d):
    """The real MCP server: a long dropship_wait must not hold up dropship_send, and cancelling it stops it."""
    a.session('a-mcp-16', 'McpBox')
    srv = subprocess.Popen([sys.executable, str(SCRIPTS / 'hypernym_mcp_server.py')], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, text=True, env=a.env, bufsize=1)
    def rpc(msg):
        srv.stdin.write(json.dumps(msg) + '\n')
        srv.stdin.flush()
    def read_until(rid, secs):
        deadline = time.time() + secs
        while time.time() < deadline:
            m = json.loads(srv.stdout.readline())
            if 'id' in m and m['id'] == rid:
                return m
        raise AssertionError(f'no answer to {rid}')
    try:
        rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18',
             'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}}})
        read_until(1, 10)
        rpc({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'dropship_wait', 'arguments': {
            'as_session': 'mcpbox', 'timeout_seconds': 300}}})
        time.sleep(1)
        t0 = time.time()
        rpc({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'dropship_send', 'arguments': {
            'to': '#bob/build', 'body': 'sent while a wait is pending', 'as_session': 'mcpbox'}}})
        r3 = read_until(3, 20)
        check('sent to #bob/build' in r3['result']['content'][0]['text'] and time.time() - t0 < 10,
              f'send must not queue behind the wait: {r3}')
        rpc({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 2}})
        r2 = read_until(2, 10)
        check('cancelled' in r2['result']['content'][0]['text'], f'cancel stops the wait: {r2}')
    finally:
        srv.kill()


def test_sessions_history_order(a, b, c, d):
    time.sleep(1.1)                                   # distinct timestamps
    other = Session(a, 'a-other-2', a.home / 'transcripts' / 'a-other-2.jsonl')
    chat = Session(b, 'b-chat-2', b.home / 'transcripts' / 'b-chat-2.jsonl')
    chat.send('hypernym dropship add')
    wait(lambda: 'chat' in a.cli('status'), 20, 'alice sees bob/chat')
    check('sent to #bob/chat' in other.send('#bob/chat quick question'), 'other → bob/chat')
    out = a.cli('sessions')
    ids = re.findall(r'\]  (\S+)$', out, re.M)
    check(ids[0] == 'a-other-2', f'last used first: expected a-other-2 first, got {ids}\n{out}')
    check('talked with #bob/chat' in out, out)


def test_lan_attacks_refused(a, b, c, d):
    out = a.py(f"""
import json, time, secrets, urllib.request, urllib.error
me = d.identity(); f = d.load(d.FRIENDS, {{}}); bid = [k for k, v in f.items() if v['name'] == 'bob'][0]
key = d.pair_key(me, f[bid]['pub'])
def post(m):
    r = urllib.request.Request('http://' + d.network()['ip'] + ':{b.port}/m', data=json.dumps(m).encode(), method='POST')
    try: return urllib.request.urlopen(r, timeout=3).read().decode()
    except urllib.error.HTTPError as e: return str(e.code) + ' ' + e.read().decode()
m = {{'from': me['id'], 'to': 'bob', 'ts': time.time(), 'nonce': secrets.token_hex(12), 'body': 'x \\x1b[31mRED',
     'to_s': 'build', 'from_s': 'transportsession'}}
m['mac'] = d.sign(key, m)
print('good', post(m)); print('replay', post(m))
t = dict(m, nonce=secrets.token_hex(12), to_s='chat'); print('session-swap', post(t))
print('stale', post(dict(m, nonce=secrets.token_hex(12), ts=time.time() - 600)))
print('unknown', post(dict(m, **{{'from': 'ffffffffffffffff'}}, nonce=secrets.token_hex(12))))""")
    check('good {"ok": true' in out, out)
    for word in ('replay 403', 'session-swap 403', 'stale 403', 'unknown 403'):
        check(word in out, f'{word!r} expected in:\n{out}')
    check(not any('\x1b' in m['body'] for m in b.inbox()), 'escape codes must be stripped')


def test_bar_width(a, b, c, d):
    row = a.cli('row', stdin=json.dumps({'session_id': 'a-transport-1'}))
    width = len(ANSI.sub('', row))
    check(row.startswith('\x1b[') and width <= 40, f'bar row {width} columns: {ANSI.sub("", row)!r}')


def test_network_pairing_and_mail(a, b, c, d):
    """--network: pair carol (not on the home wi-fi) with alice through ntfy.sh, then message both ways."""
    out = a.cli('pair')
    code = re.search(r'code: (\S+)', out).group(1)
    joined = c.cli('pair', code)
    check('paired with #alice' in joined, joined)
    wait(lambda: '#carol (mailbox)' in a.cli('status'), 30, 'alice reaches carol by mailbox')
    t = a.session('a-mail-5', 'Mailroom')
    r = t.send('#carol long-haul hello ' + 'x' * 3000)
    check('sent to #carol' in r, r)
    wait(lambda: any(m['body'].startswith('long-haul hello') and len(m['body']) > 3000 for m in c.inbox()), 40,
         'carol receives the chunked message')
    c.cli('send', '#alice/mailroom', 'right back at you')
    wait(lambda: any(m['body'] == 'right back at you' for m in a.inbox()), 40, 'alice receives over mailbox')
    check('right back at you' in t.got(), 'mailroom session gets the mailbox reply')


# ------------------------------------------------------------------------------------------------------------ runner
def test_network_tracer(a, b, c, d):
    origin = a.session('tracer-origin', 'Codex Origin')
    receiver = c.session('tracer-receiver', 'Claude Receiver')
    invitation = a.cli('tracer', extra_env={'DROPSHIP_SESSION': origin.sid})
    token = invitation.split('hypernym dropship add ')[1].splitlines()[0]
    result = c.cli('tracer', token, extra_env={'DROPSHIP_SESSION': receiver.sid})
    if 'round trip confirmed' not in result:
        time.sleep(5)
        result = c.cli('tracer', token, extra_env={'DROPSHIP_SESSION': receiver.sid})
    check('round trip confirmed' in result, result)
    # Exercise an ordinary message after pairing; carol has no home LAN.
    result = receiver.send('#alice/codex-origin tracer mailbox message')
    check('sent' in result, result)
    wait(lambda: any(m['body'] == 'tracer mailbox message' for m in a.inbox()), 30, 'mailbox message arrives')
    check('tracer mailbox message' in origin.got(), 'invited session reads the message')


def test_local_control_rename_and_nickname(a, b, c, d):
    origin = a.session('control-origin', 'Control Origin')
    dest = b.session('control-dest', 'Control Dest')
    dest.send('hypernym dropship add')
    bid = b.py("print(d.identity()['id'])")
    before = b.py("print(d.identity()['pub'])")
    try:
        b.cli('name', 'bob-renamed')
        wait(lambda: 'bob-renamed' in a.cli('peers'), 20, 'LAN rename propagates')
        a.cli('nickname', bid, 'brother')
        wait(lambda: json.loads(a.cli('peers')).get('brother') == 'home wi-fi', 20, 'nickname LAN route rediscovered')
        result = origin.send('#brother/control-dest rename-safe-message')
        check('sent' in result, result)
        wait(lambda: any(m['body'] == 'rename-safe-message' for m in b.inbox()), 10, 'nicknamed LAN delivery')
        check('rename-safe-message' in dest.got(), 'correct session receives renamed peer message')
        check(before == b.py("print(d.identity()['pub'])"), 'rename preserved key')
        check(bid == b.py("print(d.identity()['id'])"), 'rename preserved identity')
    finally:
        a.cli('clear-nickname', bid)
        b.cli('name', 'bob')


def main() -> int:
    network = '--network' in sys.argv
    only = sys.argv[sys.argv.index('-k') + 1] if '-k' in sys.argv else ''
    RUN.mkdir(parents=True)
    print(f'run dir: {RUN}')
    a, b = Machine('alice'), Machine('bob')
    c = Machine('carol', home_wifi=False)               # carol is "at her house": mailbox only
    d = Machine('dave')                                 # dave is on the home wi-fi but never talked to alice
    machines = (a, b, c, d)
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith('test_')
             and (network or 'network' not in n) and only in n]
    order = ['test_home_wifi_discovery_and_message', 'test_sessions_targeted_routing',
             'test_sessions_unknown_name_refused', 'test_unthreaded_message_goes_to_default_only',
             'test_same_computer_sessions', 'test_sessions_report_waiting', 'test_awake_targets_the_tab',
             'test_sender_is_auto_added_and_alias', 'test_listener_wakes_with_the_reply', 'test_typed_text_is_never_sent',
             'test_wait_works_like_wait_for_reply', 'test_borg_mode', 'test_mcp_wait_never_blocks_a_send',
             'test_sessions_history_order',
             'test_lan_attacks_refused', 'test_bar_width', 'test_network_pairing_and_mail']
    tests.sort(key=lambda t: order.index(t[0]) if t[0] in order else 99)
    failed = 0
    try:
        for m in machines:
            m.start()
        for name, fn in tests:
            t0 = time.time()
            try:
                fn(a, b, c, d)
                print(f'  PASS  {name}  ({time.time() - t0:.1f}s)')
            except Exception as e:                          # noqa: BLE001 — report every failure, keep going
                failed += 1
                print(f'  FAIL  {name}: {e}')
                if not isinstance(e, AssertionError):
                    traceback.print_exc()
    finally:
        for m in machines:
            try:
                m.stop()
            except AssertionError:
                pass
    print(f'{len(tests) - failed}/{len(tests)} passed — logs and inboxes in {RUN}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
