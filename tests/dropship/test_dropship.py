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
        self.file_port = str(48900 + Machine.count)
        self.home.mkdir(parents=True)
        self.downloads = RUN / f'{name}-downloads'      # like ~/Downloads: never inside Dropship's own folder
        (self.home / 'transcripts').mkdir()
        self.env = dict(os.environ, DROPSHIP_SESSION='', CODEX_THREAD_ID='', DROPSHIP_HOME=str(self.home), DROPSHIP_PORT=self.port, DROPSHIP_UDP=UDP,
                        DROPSHIP_FILE_PORT=self.file_port, DROPSHIP_DOWNLOADS=str(self.downloads),
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

    def session(self, sid: str, title: str = '', added: bool = True) -> 'Session':
        t = self.home / 'transcripts' / f'{sid}.jsonl'
        t.write_text(json.dumps({'type': 'custom-title', 'customTitle': title, 'sessionId': sid}) + '\n' if title else '')
        s = Session(self, sid, t)
        s.prompt('hello')                       # a session registers on its first prompt
        if added:                               # only added sessions may use Dropship
            s.prompt('hypernym dropship add')
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
        out = self.prompt(line)                  # handled by the hook first, then passed through with the result
        ctx = out['hookSpecificOutput']['additionalContext'] if out and 'hookSpecificOutput' in out else ''
        assert 'ALREADY handled' in ctx and 'decision' not in out, f'{line!r} was not answered by Dropship: {out}'
        return ctx

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
    hidden = b.session('b-hidden-4', 'Hidden', added=False)   # newer still, but never added: never reachable
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


def test_sender_must_be_added_and_alias(a, b, c, d):
    quiet = a.session('a-quiet-11', 'Quiet', added=False)    # never said "add"
    newest = a.session('a-newest-12', 'Newer')               # so quiet is not the default either
    check(not json.loads((a.home / 'sessions' / 'a-quiet-11.json').read_text())['exposed'], 'starts unreachable')
    r = quiet.send('#bob-build should not go')
    check('not added' in r, f'a session that was never added cannot send: {r}')
    check(not json.loads((a.home / 'sessions' / 'a-quiet-11.json').read_text())['exposed'], 'sending does not add it')
    quiet.prompt('hypernym dropship add')
    r = quiet.send('#bob-build alias form works')             # #machine-session
    check('sent to #bob/build' in r and 'dropship_wait' in r, r)
    wait(lambda: 'quiet' in b.cli('status'), 20, 'bob learns #alice/quiet')


def test_typed_text_is_never_sent(a, b, c, d):
    s = a.session('a-typed-13', 'Typed')
    before = len(b.inbox())
    out = s.prompt('#bob/build this is just a prompt, not a message')
    check(out == {} or 'decision' not in out, f'a typed #… prompt must reach Claude untouched: {out}')
    time.sleep(1.5)
    check(len(b.inbox()) == before, 'nothing may be sent because of typed text')


def test_wait_works_like_wait_for_reply(a, b, c, d):
    asker = a.session('a-asker-14', 'Asker')
    build = b.session('b-build-1', 'Build')
    t0 = time.time()
    check('do not retry automatically' in asker.wait(2) and time.time() - t0 >= 1.5, 'wait blocks, then times out quietly')
    asker.send('#bob/build what is 2+2?')
    replier = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'], env=b.env)
    replier.wait()
    build.send('#alice/asker 4')                              # arrives while asker is waiting
    out = asker.wait(20)
    check('#bob/build' in out and ': 4' in out and 'You are #alice/asker' in out, out)
    check('do not retry automatically' in asker.wait(1), 'marked read: not returned twice')
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
    check('stay silent while it is pending' in later, 'borg resumes with a single silent wait')
    other = b.session('b-build-1', 'Build')
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


def test_borg_silent_wait_and_cancel(a, b, c, d):
    import queue
    import threading
    drone = a.session('silent-drone', 'Silent Drone')
    drone.prompt('hypernym dropship borg')
    srv = subprocess.Popen([sys.executable, str(SCRIPTS / 'hypernym_mcp_server.py')], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, text=True, env=a.env, bufsize=1)
    replies = queue.Queue()
    def consume():
        for line in srv.stdout:
            replies.put(json.loads(line))
    threading.Thread(target=consume, daemon=True).start()
    def rpc(message):
        srv.stdin.write(json.dumps(dict(jsonrpc='2.0', **message)) + '\n')
        srv.stdin.flush()
    try:
        rpc({'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
             'clientInfo': {'name': 'codex-mcp-client', 'version': 'test'}}})
        check(replies.get(timeout=5)['id'] == 1, 'initialized')
        rpc({'id': 2, 'method': 'tools/call', 'params': {'name': 'dropship_wait',
             'arguments': {'as_session': 'silent-drone', 'timeout_seconds': 1}}})
        try:
            unexpected = replies.get(timeout=float(os.environ.get('DROPSHIP_IDLE_TEST_SECONDS', '3')))
            raise AssertionError(f'Borg must stay silent beyond a requested timeout: {unexpected}')
        except queue.Empty:
            pass
        sender = a.session('silent-sender', 'Silent Sender')
        result = sender.send('#alice/silent-drone wake-on-message')
        check(result.startswith('sent'), result)
        response = replies.get(timeout=5)
        check(response['id'] == 2 and 'wake-on-message' in response['result']['content'][0]['text'], 'one response on actual mail')
        # Every test session runs under the test runner's one app process, so that "window" is on whichever session
        # last had a prompt: put it back on the drone, as a real drone window would be, before its next waits.
        drone.prompt('back to the drone')
        rpc({'id': 3, 'method': 'tools/call', 'params': {'name': 'dropship_wait', 'arguments': {'as_session': 'silent-drone'}}})
        time.sleep(.5)
        rpc({'method': 'notifications/cancelled', 'params': {'requestId': 3}})
        response = replies.get(timeout=5)
        check(response['id'] == 3 and response['result']['isError'], 'pending silent wait can be cancelled')
        # Turning Borg off from another invocation wakes the waiter once, without a retry instruction.
        rpc({'id': 4, 'method': 'tools/call', 'params': {'name': 'dropship_wait', 'arguments': {'as_session': 'silent-drone'}}})
        time.sleep(.5)
        drone.prompt('hypernym dropship borg off')
        response = replies.get(timeout=5)
        check(response['id'] == 4 and 'do not retry automatically' in response['result']['content'][0]['text'], 'Borg off stops waiting')
    finally:
        srv.stdin.close()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()
            srv.wait()


def test_borg_wait_heartbeat_keeps_claude_code_alive(a, b, c, d):
    """Claude Code cancels a plugin tool that is silent for 30 minutes. With a progress token the server sends a
    progress heartbeat while dropship_wait sleeps (no model turns), then returns only when mail arrives."""
    hive = a.session('a-hive-17', 'Hive')
    hive.prompt('hypernym dropship borg')
    env = dict(a.env, DROPSHIP_HEARTBEAT_S='1')
    srv = subprocess.Popen([sys.executable, str(SCRIPTS / 'hypernym_mcp_server.py')], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, text=True, env=env, bufsize=1)
    def rpc(msg):
        srv.stdin.write(json.dumps(msg) + '\n')
        srv.stdin.flush()
    try:
        rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18',
             'capabilities': {}, 'clientInfo': {'name': 'claude-code', 'version': 't'}}})
        json.loads(srv.stdout.readline())
        rpc({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'dropship_wait', '_meta': {
            'progressToken': 'tok-hive'}, 'arguments': {'as_session': 'hive', 'timeout_seconds': 0}}})
        beats, t0 = 0, time.time()
        while beats < 3 and time.time() - t0 < 15:
            m = json.loads(srv.stdout.readline())
            check('id' not in m, f'a borg wait must not return without mail: {m}')
            if m['method'] == 'notifications/progress' and m['params']['progressToken'] == 'tok-hive':
                beats += 1
        check(beats >= 3, f'expected progress heartbeats, got {beats}')
        log = (a.home / 'mcp.log').read_text()
        check('as=hive' in log and 'progress_token=yes' in log, log)
        build = Session(b, 'b-build-1', b.home / 'transcripts' / 'b-build-1.jsonl')
        wait(lambda: 'hive' in b.cli('status'), 20, 'bob learns #alice/hive')
        build.send('#alice/hive wake up, hive')
        deadline = time.time() + 20
        while time.time() < deadline:
            m = json.loads(srv.stdout.readline())
            if 'id' in m and m['id'] == 2:
                check('wake up, hive' in m['result']['content'][0]['text'], m)
                break
        else:
            raise AssertionError('the borg wait never returned the mail')
        # without a progress token the server asks for the 25-minute renewal instead (logged)
        rpc({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'dropship_wait', 'arguments': {
            'as_session': 'hive', 'timeout_seconds': 0}}})
        time.sleep(1.5)
        check('progress_token=no' in (a.home / 'mcp.log').read_text(), 'no token → logged, short renewal')
        rpc({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 3}})
    finally:
        srv.kill()
        Session(a, 'a-hive-17', a.home / 'transcripts' / 'a-hive-17.jsonl').send('hypernym dropship borg off')


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


def test_network_secret_file(a, b, c, d):
    """--network: a secret file goes alice → carol without its contents ever appearing in output, inbox or staging."""
    SECRET = 'sk-test-' + 'Q' * 20 + '-do-not-print'
    src = a.home / 'api.secret'
    src.write_text(SECRET + '\n')
    t = a.session('a-secret-20', 'Vault')
    # carol has NOT granted alice yet: refused, nothing staged
    out = a.cli('secret-offer', str(src), '#carol', '--label', 'API key', extra_env={'DROPSHIP_SESSION': t.sid})
    check(SECRET not in out and 'queued' in out, out)
    time.sleep(12)
    check(not list((c.home / 'secrets').glob('*-sx_*')) if (c.home / 'secrets').exists() else True,
          'ungranted secret must not be staged')
    keeper = c.session('c-keeper-21', 'Keeper')              # carol's session that receives it
    check('say which session' in c.cli('secret-allow', 'alice'), 'no implicit wildcard grants')
    check('accepting' in c.cli('secret-allow', 'alice', '--session', 'keeper', '--dest', str(c.home)), 'scoped grant')
    out = a.cli('secret-offer', str(src), '#carol', '--label', 'API key', '--move', extra_env={'DROPSHIP_SESSION': t.sid})
    tid = re.search(r'(sx_[0-9a-f]{16})', out).group(1)
    wait(lambda: tid in c.cli('secret-list', '--session', 'keeper'), 60, 'carol has the secret staged')
    listing = c.cli('secret-list', '--session', 'keeper')
    check(SECRET not in listing and 'API key' in listing, listing)
    staged = next((c.home / 'secrets').glob(f'*-{tid}.enc')).read_bytes()      # namespaced by sender id
    check(SECRET.encode() not in staged, 'staged secret is encrypted at rest')
    for m in (a.inbox() + c.inbox()):
        check(SECRET not in m['body'], f'secret leaked into an inbox line: {m}')
    proj = c.home / 'proj'
    proj.mkdir(exist_ok=True)
    (proj / '.env').write_text('OTHER=1\n')
    try:
        refused = c.cli('secret-install', tid, str(proj / '.env'), '--key', 'API_KEY')
    except AssertionError as e:                       # it fails closed with a non-zero exit
        refused = str(e)
    check(SECRET not in refused and 'API_KEY' not in (proj / '.env').read_text()
          and list((c.home / 'secrets').glob(f'*-{tid}.enc')), 'no session context: fails closed, nothing installed')
    out = c.cli('secret-install', tid, str(proj / '.env'), '--key', 'API_KEY', '--session', 'keeper')
    check(SECRET not in out and 'installed' in out, out)
    env = (proj / '.env').read_text()
    check(f'API_KEY="{SECRET}"' in env and 'OTHER=1' in env, 'installed into .env as one quoted key')
    check(oct((proj / '.env').stat().st_mode & 0o777) == '0o600', 'installed file is private')
    check(not list((c.home / 'secrets').glob(f'*-{tid}.*')), 'staging removed after install')
    wait(lambda: not src.exists(), 60, 'alice deletes the source after the verified receipt (--move)')
    for f in (a.home / 'daemon.log', c.home / 'daemon.log', a.home / 'mcp.log'):
        if f.exists():
            check(SECRET not in f.read_text(), f'secret leaked into {f.name}')


# ------------------------------------------------------------------------------------------------------------ runner
def test_network_file_direct(a, b, c, d):
    """--network: a 20 MB file alice → carol over the direct route (carol's person typed "hypernym dropship files alice
    auto", so it is accepted without asking); the bytes never appear in any output or inbox; carol gets one notice,
    alice gets one receipt."""
    import hashlib
    blob = os.urandom(20 * 1024 * 1024)
    src = a.home.parent / 'alice-project' / 'big.bin'     # a normal folder, not Dropship's own state
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(blob)
    sender = a.session('a-files-22', 'Files')
    inbox_s = c.session('c-files-23', 'Inbox')
    flag = inbox_s.send('hypernym dropship files alice auto')       # typed by carol's person: the only way to set it
    check('files: auto' in flag, flag)
    check('files: auto' in c.cli('friends'), 'the flag shows in the friends listing')
    out = a.cli('file-offer', str(src), '#carol', extra_env={'DROPSHIP_SESSION': sender.sid})
    m = re.search(r'(fx_[0-9a-f]{16})', out)
    check(m and 'offered' in out, out)
    tid = m.group(1)
    landed = c.downloads / 'Dropship' / 'alice' / 'big.bin'
    wait(lambda: landed.exists(), 120, 'carol has the file')
    check(hashlib.sha256(landed.read_bytes()).digest() == hashlib.sha256(blob).digest(), 'identical bytes')
    aid = a.py("print(d.identity()['id'])")
    route = c.py(f"import json; print(json.loads((d.HOME / 'files' / 'in' / '{aid}-{tid}.json').read_text())['route'])")
    check(route == 'direct', f'the transfer used the direct route, not {route!r}')
    wait(lambda: any(tid in x['body'] and 'saved' in x['body'] for x in c.inbox()), 20, 'carol notified')
    wait(lambda: any(tid in x['body'] and 'saved by' in x['body'] for x in a.inbox()), 60, 'alice gets the receipt')
    notes = [x for x in a.inbox() + c.inbox() if tid in x['body']]
    check(notes and all(len(x['body']) < 600 for x in notes), 'only short one-line notices about the file')


def test_network_long_message_as_file(a, b, c, d):
    """--network: a message longer than one message is delivered as ONE file plus a one-line notice."""
    sender = a.session('a-files-22', 'Files')
    text = 'burndown line\n' * 2000                        # ~28 KB
    out = a.cli('send', '#carol', text, extra_env={'DROPSHIP_SESSION': sender.sid})
    check('long message' in out and 'offered' in out, out)
    wait(lambda: any('[long message' in x['body'] for x in c.inbox()), 120, 'carol gets the long message notice')
    note = [x for x in c.inbox() if '[long message' in x['body']][-1]['body']
    path = note.split('saved to ', 1)[1].split(' . Friend', 1)[0]
    check(Path(path).read_text() == text, 'the whole message arrived as one file')


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
             'test_sender_is_auto_added_and_alias', 'test_typed_text_is_never_sent',
             'test_wait_works_like_wait_for_reply', 'test_borg_mode', 'test_mcp_wait_never_blocks_a_send', 'test_borg_wait_heartbeat_keeps_claude_code_alive',
             'test_sessions_history_order',
             'test_lan_attacks_refused', 'test_bar_width', 'test_network_pairing_and_mail', 'test_network_secret_file']
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
