"""Session invitations using Dropship's existing authenticated pairing and transports."""
import base64
import hashlib
import json
import os
import secrets
import sys
import time

import dropship as d

PREFIX = 'dsp1-'
CONTROL = 'dropship-tracer-v1:'


def current_session(name=''):
    ss = d.all_sessions()
    if os.environ.get('DROPSHIP_MCP_PID'):              # through a tool: only this window's own session
        sid = d.calling_session()
        r = d.register(sid, ss[sid]['transcript'], ss[sid]['cwd'])
        d.set_exposed(sid, True)
        if r['n'] is None:
            d.unread_for(sid)
        return sid, r
    sid = os.environ.get('DROPSHIP_SESSION') or os.environ.get('CODEX_THREAD_ID', '')
    if sid and sid not in ss:
        raise ValueError('This session is not registered yet. Resume the agent with the Hypernym hooks enabled.')
    if sid and sid in ss:
        if name and ss[sid]['name'] != name:
            raise ValueError('The supplied name does not match this session. Run Dropship status again.')
    elif name:
        hits = [k for k, r in ss.items() if r['name'] == name]
        if len(hits) != 1:
            raise ValueError('Session name is missing or ambiguous; supply the exact session ID.')
        sid = hits[0]
    else:
        hits = [k for k, r in ss.items() if r['cwd'] == os.getcwd()]
        if len(hits) != 1:
            raise ValueError('Several or no sessions use this folder. Supply as_session from Dropship status.')
        sid = hits[0]
    r = ss[sid]
    r = d.register(sid, r['transcript'], r['cwd'])
    d.set_exposed(sid, True)
    # Establish the cursor before the first message arrives.
    if r['n'] is None:
        d.unread_for(sid)
    return sid, r


def decode(token):
    if isinstance(token, str):
        token = ''.join(token.split())
    if not isinstance(token, str) or not token.startswith(PREFIX) or len(token) > 2048:
        raise ValueError('Invalid tracer; paste the dsp1- invitation exactly.')
    try:
        raw = token[len(PREFIX):]
        value = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
        if set(value) != {'id', 'session', 'code', 'nonce', 'expires'}:
            raise ValueError()
        for key in ('id', 'code', 'nonce'):
            if not isinstance(value[key], str) or not value[key] or len(value[key]) > 128:
                raise ValueError()
        if not isinstance(value['session'], str) or not d.SESSION_RE.fullmatch(value['session']):
            raise ValueError()
        if not isinstance(value['expires'], int) or not time.time() < value['expires'] <= time.time() + MAX_MINUTES * 60 + 60:
            raise ValueError('Tracer expired or has an invalid expiry. Ask for a new tracer.')
        return value
    except (TypeError, KeyError, UnicodeError, ValueError) as e:
        raise ValueError('Invalid or expired tracer. Ask the other session for a new tracer.') from e


def ready():
    """Use the installed courier implementation before offering a working invitation."""
    if not d.config()['enabled']:
        raise ValueError('Dropship is off. Turn it on before using a tracer.')
    if d.running() and d.state().get('local_control_version') != 1:
        d.stop_daemon()
    d.start_daemon()
    deadline = time.time() + 15
    while time.time() < deadline:
        state = d.state()
        if d.running() and state.get('local_control_version') == 1 and state.get('mail', {}).get('ok'):
            return
        time.sleep(.25)
    raise ValueError('Courier/mailbox not ready after startup. Run dropship status for the reason.')


MAX_MINUTES = 7 * 24 * 60


def describe(secs: int) -> str:
    m = max(1, round(secs / 60))
    return f'{m} minutes' if m < 120 else f'{m / 60:.1f} hours' if m < 2880 else f'{m / 1440:.1f} days'


def create(name='', minutes=10):
    ready()
    sid, r = current_session(name)
    if not d.config()['enabled']:
        raise ValueError('Dropship is off. Turn it on before creating a tracer.')
    state = d.state()
    if not d.running() or not state.get('mail', {}).get('ok'):
        raise ValueError('Mailbox is not ready. Run dropship status before creating a tracer.')
    code, nonce = secrets.token_hex(16), secrets.token_hex(16)
    route = r['name'][:18] + '-' + hashlib.sha256(sid.encode()).hexdigest()[:12]
    value = {'id': d.identity()['id'], 'session': route, 'code': code,
             'nonce': nonce, 'expires': int(time.time()) + 60 * max(1, min(int(minutes), MAX_MINUTES))}
    # Keep the invitation's route valid if the user renames the session.
    r = d.session_record(sid)
    r.setdefault('aliases', []).append(value['session'])
    r['aliases'] = list(dict.fromkeys(r['aliases']))[-10:]
    d.save(d.SESSIONS / f'{sid}.json', r)
    d.TRACERS.mkdir(exist_ok=True)
    d.PAIRINGS.mkdir(exist_ok=True)
    d.save(d.TRACERS / f'{nonce}.json', dict(value, sid=sid))            # one file per open tracer: none replaces another
    d.save(d.HOME / 'tracer-invitation.json', dict(value, sid=sid))       # the latest one (kept for older tools)
    d.save(d.PAIRINGS / f'{code}.json', {'code': code, 'status': 'new', 'started': time.time(), 'until': value['expires']})
    token = PREFIX + base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).decode().rstrip('=')
    return (f"Your address: {d.addr(d.identity()['name'], r['name'])}\n"
            f'Tracer expires in {describe(value["expires"] - int(time.time()))}. Paste this into the other Codex or Claude agent:\n'
            f'hypernym dropship add {token}\nTreat the tracer as a temporary invitation; share it only with the intended person.')


def connect(token, name=''):
    value = decode(token)
    ready()
    sid, r = current_session(name)
    if value['id'] == d.identity()['id']:
        raise ValueError('This tracer belongs to this computer. Create it on the other computer.')
    if not d.config()['enabled']:
        raise ValueError('Dropship is off. Turn it on before accepting a tracer.')
    if not d.running() or not d.state().get('mail', {}).get('ok'):
        raise ValueError('The courier/mailbox is not ready. Run dropship status first.')
    friends = d.load(d.FRIENDS, {})
    friend = friends.get(value['id'])
    if not friend or 'box' not in friend:
        job = d.load(d.PAIRING, {})
        if job.get('code') != value['code']:
            d.pair(value['code'])
        friends = d.load(d.FRIENDS, {})
        friend = friends.get(value['id'])
        if not friend or 'box' not in friend:
            return 'Pairing is pending or failed. Run dropship status, then retry this same invitation before expiry.'
    target = d.addr(friend['name'], value['session'])
    body = CONTROL + json.dumps({'kind': 'hello', 'nonce': value['nonce']})
    # Use the encrypted mailbox even on LAN: this checks the long-range path.
    result = d.send_mail(friend['name'], body, value['session'], r['name'], sid, value['id'])
    if result != 'ok':
        return f'{target}: {result}. Receipt is not confirmed; retry before the tracer expires.'
    receipt = d.HOME / ('tracer-receipt-' + value['nonce'] + '.json')
    deadline = time.time() + 60
    while time.time() < deadline:
        ack = d.load(receipt, {})
        if (ack.get('from_id') == value['id'] or ack.get('from') == friend['name']) and ack.get('owner') == sid:
            target = d.addr(d.friend_names().get(value['id'], friend['name']), value['session'])
            return f'Connected to {target}. Encrypted mailbox round trip confirmed to the invited session inbox. Agent reply not yet checked.'
        time.sleep(.25)
    return f'Invitation sent to {target}; no session receipt yet. Retry before expiry. This does not prove an agent is awake.'


def _invitation(nonce):
    """The open tracer with this nonce (path, invitation), else (None, {})."""
    if not isinstance(nonce, str) or len(nonce) != 32 or any(c not in '0123456789abcdef' for c in nonce):
        return None, {}
    p = d.TRACERS / f'{nonce}.json'
    inv = d.load(p, {})
    if not inv:                                         # made before 0.7.52
        p = d.HOME / 'tracer-invitation.json'
        inv = d.load(p, {})
    ok = isinstance(inv, dict) and 'nonce' in inv and inv['nonce'] == nonce and 'expires' in inv \
        and inv['expires'] > time.time() and 'sid' in inv
    return (p, inv) if ok else (None, {})


def admit(pid, body, to_s):
    """A tracer hello from an already-paired friend, for an open tracer of the addressed session: that session's id
    (accepting your tracer is your consent to that session), else ''. Claimed tracers admit only their claimer."""
    if not isinstance(body, str) or not body.startswith(CONTROL):
        return ''
    try:
        msg = json.loads(body[len(CONTROL):])
        if msg['kind'] != 'hello':
            return ''
        _p, inv = _invitation(msg['nonce'])
    except (ValueError, KeyError, TypeError):
        return ''
    if not inv or inv['session'] != to_s:
        return ''
    if 'claimed_by_id' in inv and inv['claimed_by_id'] != pid:
        return ''
    return inv['sid']


def receive(courier, name, body, to_s, from_s):
    """Only acknowledge an unexpired invitation; never interpret peer text as commands."""
    if not body.startswith(CONTROL):
        return False
    try:
        msg = json.loads(body[len(CONTROL):])
        nonce = msg['nonce']
        if not isinstance(nonce, str) or len(nonce) != 32 or any(c not in '0123456789abcdef' for c in nonce):
            return True
        owner = d.owner_of(to_s, name)
        if not owner:
            return True
        if msg['kind'] == 'ack':
            d.save(d.HOME / ('tracer-receipt-' + nonce + '.json'), {'from': name, 'from_id': d.peer_identity(name), 'owner': owner, 'at': time.time()})
        elif msg['kind'] == 'hello':
            inv_path, invitation = _invitation(nonce)
            if not invitation or invitation['sid'] != owner:
                return True
            peer_id = d.peer_identity(name)
            if invitation.get('claimed_by_id', peer_id) != peer_id or ('claimed_by_id' not in invitation and invitation.get('claimed_by', name) != name):
                return True
            invitation['claimed_by'] = name
            invitation['claimed_by_id'] = peer_id
            d.save(inv_path, invitation)
            d.OUTBOX.mkdir(exist_ok=True)
            d.save(d.OUTBOX / ('tracer-' + secrets.token_hex(8) + '.json'),
                   {'to': name, 'to_id': '' if peer_id.startswith('name:') else peer_id, 'body': CONTROL + json.dumps({'kind': 'ack', 'nonce': nonce}),
                    'to_s': from_s, 'from_s': to_s, 'sid': owner})
        return True
    except (ValueError, KeyError, TypeError):
        return True


if __name__ == '__main__':
    try:
        args = sys.argv[1:]
        name = os.environ.get('DROPSHIP_AS', '')
        print(connect(args[0], name) if args else create(name))
    except (ValueError, OSError) as e:
        print(f'Dropship: {e}')
        sys.exit(1)
