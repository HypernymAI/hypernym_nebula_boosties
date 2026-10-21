#!/usr/bin/env python3
"""
Hypernym MCP server — standard library only, stdio transport (newline-delimited JSON-RPC 2.0).

One core: every tool runs the SAME `hypernym.py` command the skill runs (subprocess), so Claude Code, Codex and
any other MCP host get identical behavior — the dry-run rule for analyze, signed requests, packages on disk.

    python3 hypernym_mcp_server.py        (the host launches it; cwd = the project the session is in)

Tools take `dir` (the git repo; default: the server's working directory). NO .get().

THE APPROVAL GATE (hypernym_analyze with execute / auto): the TOOL asks, not the agent. The server runs the dry run,
then sends `elicitation/create` to the client — the preview (repo, commit, what is not on GitHub, question, cost, who
pays) and a one-field form (files). The dialog's own buttons are the decision: Accept runs the CLI with --execute (or
--auto) and the chosen file count; Decline, closing it, or a failed dialog → "not submitted", nothing sent. (A separate
submit/cancel choice inside the form defaulted to cancel, and people pressed Accept without changing it.) The answer comes from the
person's dialog, so the agent cannot approve on their behalf. A client that did not advertise the `elicitation`
capability at initialize gets execute REFUSED with the dry run and the instruction to get an explicit yes; `auto`
there keeps its old meaning (the person's portal Sending setting is their consent, and the CLI enforces its limit).
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE / 'hypernym.py'
STATUSLINE = HERE / 'statusline.py'
SHARED_BAR = HERE / 'nebula_statusline.py'
BAR_ACTIONS = {'install': ['install'], 'force': ['install', '--force'], 'remove': ['remove']}
ANSI = re.compile(r'\x1b\[[0-9;]*m')
LOGIN_LOG = Path.home() / '.hypernym' / 'login.log'
# Every approval dialog's raw answer (local only), so what the client actually returned can always be checked.
APPROVAL_LOG = Path.home() / '.hypernym' / 'approval_answers.jsonl'
PLUGIN_MANIFEST = HERE.parent.parent.parent / '.claude-plugin' / 'plugin.json'
SERVER_INFO = {'name': 'hypernym', 'version': json.loads(PLUGIN_MANIFEST.read_text())['version']}
SUPPORTED_PROTOCOLS = ('2025-06-18', '2025-03-26', '2024-11-05')
RUN_TIMEOUT_S = 900
CLIENT = {'elicitation': False, 'codex': False}   # set from the client's capabilities / clientInfo at initialize
BACKLOG = []                                      # messages that arrived while waiting for a dialog answer
ELICIT_SEQ = [0]

DIR_PROP = {'type': 'string', 'description': 'Path of the git repo (default: the working directory).'}
TOOLS = [
    {'name': 'hypernym_about',
     'description': "What the two plugins are (Nebula, the local shield; Hypernym, the Zephyr connector), plus the "
                    "install and update lines. Use for \"what does this do?\".",
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'hypernym_status',
     'description': "The Zephyr status row for this repo — the same line Claude Code's status bar shows (signed in, "
                    "analysis running with its progress, analysis complete with its file count, top up, Zephyr "
                    "reconnecting). Use for \"hypernym status\". Show it as-is.",
     'inputSchema': {'type': 'object', 'properties': {'dir': DIR_PROP}}},
    {'name': 'hypernym_statusline',
     'description': "The shared hypernym-nebula status line in Claude Code: install it, force it over someone else's "
                    "(backed up first), or remove it. Show the output as-is.",
     'inputSchema': {'type': 'object', 'required': ['action'],
                     'properties': {'action': {'type': 'string', 'enum': list(BAR_ACTIONS)}}}},
    {'name': 'hypernym_help',
     'description': "Setup check and overview for this repo: sign-in on each server, this repo's floats and "
                    "repo-analyze jobs (downloads finished packages), and local packages with their drift.",
     'inputSchema': {'type': 'object', 'properties': {'dir': DIR_PROP}}},
    {'name': 'hypernym_floats',
     'description': "This repo's floats on each server.",
     'inputSchema': {'type': 'object', 'properties': {'dir': DIR_PROP,
                                                      'files': {'type': 'boolean', 'description': 'List files too.'}}}},
    {'name': 'hypernym_analyze',
     'description': "Repo analyze for this repo's newest commit on GitHub. DRY RUN by default: shows exactly what "
                    "would be sent (repo URL, commit, question — never source code), what this computer has that is "
                    "not on GitHub, the cost and who pays. execute=true shows the person an approval dialog with that "
                    "preview; only their Submit sends anything. This is the normal way to submit.",
     'inputSchema': {'type': 'object', 'properties': {
         'dir': DIR_PROP,
         'question': {'type': 'string', 'description': "What to focus on: the user's current task if they described "
                                                      "one; omit it to analyze the repo as a whole."},
         'top_n': {'type': 'integer', 'description': 'Files to float (default 40).'},
         'budget': {'type': 'integer', 'description': 'Per-float budget (default 3077).'},
         'commit': {'type': 'string', 'description': 'Commit to analyze (default: newest pushed).'},
         'execute': {'type': 'boolean', 'description': "Submit for real. The person approves in Hypernym's own "
                                                        "dialog (shown by this tool); declining sends nothing."},
         'auto': {'type': 'boolean', 'description': "Submit without asking. Only allowed when the person's sending "
                                                     "setting for this repo is auto; refused over their daily limit."}}}},
    {'name': 'hypernym_wait',
     'description': 'Poll a repo-analyze job; downloads the package when it completes.',
     'inputSchema': {'type': 'object', 'required': ['job_id'], 'properties': {
         'dir': DIR_PROP, 'job_id': {'type': 'string'},
         'minutes': {'type': 'number', 'description': 'How long to wait (default 10).'}}}},
    {'name': 'hypernym_cancel',
     'description': 'Cancel a running repo-analyze job and its in-flight floats.',
     'inputSchema': {'type': 'object', 'required': ['job_id'], 'properties': {'job_id': {'type': 'string'}}}},
    {'name': 'hypernym_sync',
     'description': "Download every completed job for this repo not yet on disk.",
     'inputSchema': {'type': 'object', 'properties': {'dir': DIR_PROP}}},
    {'name': 'hypernym_packages',
     'description': "This repo's packages with drift against the working tree.",
     'inputSchema': {'type': 'object', 'properties': {'dir': DIR_PROP}}},
    {'name': 'hypernym_load',
     'description': 'Print the newest (or given) package so the session can read it. Mention its drift.',
     'inputSchema': {'type': 'object', 'properties': {'dir': DIR_PROP,
                                                      'package': {'type': 'string', 'description': 'Package dir.'}}}},
    {'name': 'hypernym_github_status',
     'description': 'Active GitHub links (private repos need one).',
     'inputSchema': {'type': 'object', 'properties': {'dir': DIR_PROP}}},
    {'name': 'hypernym_account_list',
     'description': "The user's accounts on the Zephyr (one identity can hold several, e.g. two emails) and which "
                    "one this machine acts as.",
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'hypernym_account_use',
     'description': "Make one of the user's accounts (by email) this machine's default on the Zephyr.",
     'inputSchema': {'type': 'object', 'required': ['email'], 'properties': {'email': {'type': 'string'}}}},
    {'name': 'hypernym_login_start',
     'description': "Start sign-in: GitHub in the browser, invite-only, nothing to copy or remember. Returns the URL "
                    "to approve. Then call hypernym_login_status until it reports logged in.",
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'hypernym_login_status',
     'description': 'Progress of the sign-in started by hypernym_login_start.',
     'inputSchema': {'type': 'object', 'properties': {}}},
]


def _dir(arguments: dict) -> str:
    if 'dir' in arguments:
        return arguments['dir']
    return os.getcwd()


def run_cli(args: list) -> tuple:
    r = subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, timeout=RUN_TIMEOUT_S,
                       stdin=subprocess.DEVNULL)
    text = r.stdout
    if r.stderr.strip():
        text += '\n' + r.stderr
    return text.strip(), r.returncode != 0


def login_start(arguments: dict) -> tuple:
    LOGIN_LOG.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(CLI), 'login', '--minutes', '10']
    log = open(LOGIN_LOG, 'w')
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    log.close()
    deadline = time.time() + 20
    while time.time() < deadline:
        time.sleep(0.5)
        text = LOGIN_LOG.read_text()
        if 'https://github.com/login/oauth/authorize' in text:
            return text + '\nApprove in the browser, then call hypernym_login_status.', False
        if 'could not start' in text:
            return text, True
    return LOGIN_LOG.read_text() + '\n(still starting; call hypernym_login_status)', False


def login_status() -> tuple:
    if not LOGIN_LOG.exists():
        return 'no sign-in started; call hypernym_login_start', True
    return LOGIN_LOG.read_text().strip(), False


def status_row(arguments: dict) -> tuple:
    """statusline.py's row for this repo (it reads the local status cache and refreshes it in the background), plain."""
    r = subprocess.run([sys.executable, str(STATUSLINE)], input=json.dumps({'session_id': 'mcp', 'cwd': _dir(arguments)}),
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return f"status line failed: {r.stderr.strip()[-300:]}", True
    return ANSI.sub('', r.stdout).strip(), False


def call_tool(name: str, arguments: dict) -> tuple:
    if name == 'hypernym_status':
        return status_row(arguments)
    if name == 'hypernym_statusline':
        if arguments['action'] not in BAR_ACTIONS:
            return f"action must be one of {list(BAR_ACTIONS)}", True
        r = subprocess.run([sys.executable, str(SHARED_BAR), *BAR_ACTIONS[arguments['action']]], capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
        return (r.stdout + r.stderr).strip(), r.returncode != 0
    if name == 'hypernym_about':
        return run_cli(['about'])
    if name == 'hypernym_help':
        return run_cli(['--dir', _dir(arguments), 'help'])
    if name == 'hypernym_floats':
        args = ['--dir', _dir(arguments), 'floats']
        if 'files' in arguments:
            if arguments['files']:
                args.append('--files')
        return run_cli(args)
    if name == 'hypernym_analyze':
        return analyze(arguments)
    if name == 'hypernym_wait':
        args = ['--dir', _dir(arguments), 'wait', arguments['job_id']]
        if 'minutes' in arguments:
            args += ['--minutes', str(arguments['minutes'])]
        return run_cli(args)
    if name == 'hypernym_cancel':
        return run_cli(['cancel', arguments['job_id']])
    if name == 'hypernym_sync':
        return run_cli(['--dir', _dir(arguments), 'sync'])
    if name == 'hypernym_packages':
        return run_cli(['--dir', _dir(arguments), 'packages'])
    if name == 'hypernym_load':
        args = ['--dir', _dir(arguments), 'load']
        if 'package' in arguments:
            args += ['--package', arguments['package']]
        return run_cli(args)
    if name == 'hypernym_github_status':
        return run_cli(['--dir', _dir(arguments), 'github', 'status'])
    if name == 'hypernym_account_list':
        return run_cli(['account', 'list'])
    if name == 'hypernym_account_use':
        return run_cli(['account', 'use', arguments['email']])
    if name == 'hypernym_login_start':
        return login_start(arguments)
    if name == 'hypernym_login_status':
        return login_status()
    raise KeyError(name)


def analyze_args(arguments: dict, top_n: int) -> list:
    args = ['--dir', _dir(arguments), 'analyze', '--top-n', str(top_n)]
    if 'question' in arguments:
        if arguments['question'].strip():
            args += ['--question', arguments['question']]
    if 'budget' in arguments:
        args += ['--budget', str(arguments['budget'])]
    if 'commit' in arguments:
        args += ['--commit', arguments['commit']]
    return args


def dialog_text(dry: str, top_n: int) -> str:
    """The approval dialog's words, built from the dry run's facts (what is analyzed, what is sent, cost, who pays,
    the GitHub gap). Raises ValueError if the dry run no longer carries a fact — then nothing is submitted."""
    facts = {}
    pays = None
    gap = None
    for line in dry.splitlines():
        stripped = line.strip()
        for key in ('repo:', 'commit:', 'focus:'):
            if stripped.startswith(key):
                facts[key[:-1]] = stripped[len(key):].strip()
        if stripped.startswith('pays from'):
            pays = stripped
        if stripped.startswith('The server reads GitHub commit'):
            gap = stripped
    for key in ('repo', 'commit', 'focus'):
        if key not in facts:
            raise ValueError(f"the dry run has no {key} line")
    repo = facts['repo'].removeprefix('https://github.com/')
    lines = [f"Analyze {repo} with Hypernym",
             "",
             f"Cost: up to {top_n} credits (files Hypernym has already analyzed are free)"]
    if pays is not None:
        lines.append(f"Paid from: {pays.removeprefix('pays from ')}")
    sent = f"Sent: the repo's GitHub address and commit {facts['commit'][:12]} — never your code."
    if facts['focus'] != 'auto':
        sent = f"Sent: the repo's GitHub address, commit {facts['commit'][:12]} and your question — never your code."
    agent = 'Claude'
    if CLIENT['codex']:
        agent = 'Codex'
    lines += [sent, f"{agent} keeps working and uses it automatically when it's ready."]
    if gap is not None:
        lines.append("Note: " + gap.replace("; these files differ and the package will describe their GitHub version.",
                                            "; the analysis describes the GitHub version."))
    if CLIENT['codex']:
        # Codex shows the message as one paragraph (line breaks dropped, measured 2026-10-19) under its own buttons
        # (Yes / No / Cancel): full sentences, joined by spaces, naming those buttons.
        lines += [f"Yes analyzes (up to {top_n} credits); No or Cancel sends nothing."]
        sentences = []
        for line in lines:
            if line:
                if not line.endswith(('.', '!', '?')):
                    line += '.'
                sentences.append(line)
        return ' '.join(sentences)
    lines += ["", f"Accept to analyze (up to {top_n} credits). Decline to send nothing."]
    return "\n".join(lines)


def analyze(arguments: dict) -> tuple:
    top_n = arguments['top_n'] if 'top_n' in arguments else 40
    flag = None
    if 'execute' in arguments and arguments['execute'] is True:
        flag = '--execute'
    elif 'auto' in arguments and arguments['auto'] is True:
        flag = '--auto'
    if flag is None:
        return run_cli(analyze_args(arguments, top_n))                       # the dry run
    if not CLIENT['elicitation']:
        if flag == '--auto':
            return run_cli(analyze_args(arguments, top_n) + ['--auto'])
        dry, failed = run_cli(analyze_args(arguments, top_n))
        return ("NOT SUBMITTED — this client cannot show Hypernym's approval dialog (it did not offer MCP "
                "elicitation), so this tool does not submit. Show the person the dry run below and ask for an explicit "
                "yes; nothing was sent.\n\n" + dry), True
    dry, failed = run_cli(analyze_args(arguments, top_n))
    if failed:
        return dry, True
    try:
        message = dialog_text(dry, top_n)
    except ValueError as e:
        return f"not submitted: the approval dialog could not be prepared ({e}); nothing was sent.", True
    try:
        answer = elicit(message, {
            'type': 'object',
            'properties': {
                'top_n': {'type': 'integer', 'title': 'Files to analyze (the most credits it can use)',
                          'minimum': 1, 'maximum': 200, 'default': top_n}}})
    except EOFError:
        return "not submitted: the client closed the connection before answering; nothing was sent.", True
    if 'error' in answer:
        return (f"not submitted: the approval dialog failed ({answer['error']['message']}); nothing was sent."), True
    APPROVAL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with APPROVAL_LOG.open('a') as log:
        log.write(json.dumps({'at': time.time(), 'answer': answer}) + '\n')
    result = answer['result']
    content = result['content'] if 'content' in result else {}
    if result['action'] != 'accept':
        how = {'decline': 'declined in the approval dialog', 'cancel': 'closed the approval dialog'}[result['action']]
        return f"not submitted: the person {how}; nothing was sent.", False
    chosen = content['top_n'] if 'top_n' in content else top_n
    return run_cli(analyze_args(arguments, chosen) + [flag])


def next_message():
    """The next client message: first any that arrived while a dialog was open, then stdin. None at end of input."""
    if BACKLOG:
        return BACKLOG.pop(0)
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        if line.strip():
            try:
                return json.loads(line)
            except json.JSONDecodeError as e:
                respond(None, error={'code': -32700, 'message': f"parse error: {e}"})


def elicit(message: str, schema: dict) -> dict:
    """Send elicitation/create and wait for THE answer (the whole JSON-RPC response). Pings are answered meanwhile;
    anything else is kept for the main loop."""
    ELICIT_SEQ[0] += 1
    rid = f"hypernym-approval-{ELICIT_SEQ[0]}"
    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': 'elicitation/create',
                                 'params': {'message': message, 'requestedSchema': schema}}) + '\n')
    sys.stdout.flush()
    while True:
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        if not line.strip():
            continue
        msg = json.loads(line)
        if 'method' not in msg and 'id' in msg and msg['id'] == rid:
            return msg
        if 'method' in msg and msg['method'] == 'ping' and 'id' in msg:
            respond(msg['id'], {})
            continue
        BACKLOG.append(msg)


def respond(message_id, result=None, error=None) -> None:
    out = {'jsonrpc': '2.0', 'id': message_id}
    if error is not None:
        out['error'] = error
    else:
        out['result'] = result
    sys.stdout.write(json.dumps(out) + '\n')
    sys.stdout.flush()


def handle(message: dict) -> None:
    if 'method' not in message:
        return  # a stray response (e.g. to a dialog that already ended): nothing to do
    method = message['method']
    if 'id' not in message:
        return  # notification (notifications/initialized, cancellations): nothing to answer
    message_id = message['id']
    params = {}
    if 'params' in message:
        params = message['params']
    if method == 'initialize':
        CLIENT['elicitation'] = 'capabilities' in params and 'elicitation' in params['capabilities']
        if 'clientInfo' in params:
            if 'name' in params['clientInfo']:
                CLIENT['codex'] = params['clientInfo']['name'] == 'codex-mcp-client'
        version = SUPPORTED_PROTOCOLS[0]
        if 'protocolVersion' in params:
            if params['protocolVersion'] in SUPPORTED_PROTOCOLS:
                version = params['protocolVersion']
        respond(message_id, {'protocolVersion': version, 'capabilities': {'tools': {}}, 'serverInfo': SERVER_INFO})
        return
    if method == 'ping':
        respond(message_id, {})
        return
    if method == 'tools/list':
        respond(message_id, {'tools': TOOLS})
        return
    if method == 'tools/call':
        arguments = {}
        if 'arguments' in params:
            arguments = params['arguments']
        try:
            text, is_error = call_tool(params['name'], arguments)
        except KeyError as e:
            respond(message_id, error={'code': -32602, 'message': f"unknown tool or missing argument: {e}"})
            return
        except subprocess.TimeoutExpired:
            text, is_error = f"timed out after {RUN_TIMEOUT_S}s", True
        respond(message_id, {'content': [{'type': 'text', 'text': text}], 'isError': is_error})
        return
    respond(message_id, error={'code': -32601, 'message': f"method not found: {method}"})


def main() -> None:
    while True:
        message = next_message()
        if message is None:
            return
        handle(message)


if __name__ == '__main__':
    main()
