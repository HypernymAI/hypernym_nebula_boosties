#!/usr/bin/env python3
"""
Hypernym MCP server — standard library only, stdio transport (newline-delimited JSON-RPC 2.0).

One core: every tool runs the SAME `hypernym.py` command the skill runs (subprocess), so Claude Code, Codex and
any other MCP host get identical behavior — the dry-run rule for analyze, signed requests, packages on disk.

    python3 hypernym_mcp_server.py        (the host launches it; cwd = the project the session is in)

Tools take `dir` (the git repo; default: the server's working directory). NO .get().
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE / 'hypernym.py'
LOGIN_LOG = Path.home() / '.hypernym' / 'login.log'
PLUGIN_MANIFEST = HERE.parent.parent.parent / '.claude-plugin' / 'plugin.json'
SERVER_INFO = {'name': 'hypernym', 'version': json.loads(PLUGIN_MANIFEST.read_text())['version']}
SUPPORTED_PROTOCOLS = ('2025-06-18', '2025-03-26', '2024-11-05')
RUN_TIMEOUT_S = 900

DIR_PROP = {'type': 'string', 'description': 'Path of the git repo (default: the working directory).'}
TOOLS = [
    {'name': 'hypernym_about',
     'description': "What the two plugins are (Nebula, the local shield; Hypernym, the Zephyr connector), plus the "
                    "install and update lines. Use for \"what does this do?\".",
     'inputSchema': {'type': 'object', 'properties': {}}},
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
                    "would be sent (repo URL, commit, question — never source code). Set execute=true ONLY after "
                    "the user explicitly approves this submission: a real run costs a float per new file.",
     'inputSchema': {'type': 'object', 'required': ['question'], 'properties': {
         'dir': DIR_PROP,
         'question': {'type': 'string', 'description': 'The bug or area the analysis should cover.'},
         'top_n': {'type': 'integer', 'description': 'Files to float (default 40).'},
         'budget': {'type': 'integer', 'description': 'Per-float budget (default 3077).'},
         'commit': {'type': 'string', 'description': 'Commit to analyze (default: newest pushed).'},
         'execute': {'type': 'boolean', 'description': 'Submit for real. Only with explicit user approval.'}}}},
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


def call_tool(name: str, arguments: dict) -> tuple:
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
        args = ['--dir', _dir(arguments), 'analyze', '--question', arguments['question']]
        if 'top_n' in arguments:
            args += ['--top-n', str(arguments['top_n'])]
        if 'budget' in arguments:
            args += ['--budget', str(arguments['budget'])]
        if 'commit' in arguments:
            args += ['--commit', arguments['commit']]
        if 'execute' in arguments:
            if arguments['execute'] is True:
                args.append('--execute')
        return run_cli(args)
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


def respond(message_id, result=None, error=None) -> None:
    out = {'jsonrpc': '2.0', 'id': message_id}
    if error is not None:
        out['error'] = error
    else:
        out['result'] = result
    sys.stdout.write(json.dumps(out) + '\n')
    sys.stdout.flush()


def handle(message: dict) -> None:
    method = message['method']
    if 'id' not in message:
        return  # notification (notifications/initialized, cancellations): nothing to answer
    message_id = message['id']
    params = {}
    if 'params' in message:
        params = message['params']
    if method == 'initialize':
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
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as e:
            respond(None, error={'code': -32700, 'message': f"parse error: {e}"})
            continue
        handle(message)


if __name__ == '__main__':
    main()
