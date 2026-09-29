#!/usr/bin/env python3
"""
Hypernym CLI for Claude Code / Codex skills. Standard library only.

    hypernym.py help                         setup check, this repo's floats + jobs, sync packages, list packages
    hypernym.py floats [--files]             floats for this repo on each server
    hypernym.py analyze --question TEXT      submit repo analyze for the newest commit GitHub has
             [--question-file F] [--top-n N] [--budget B] [--server NAME] [--commit SHA]
    hypernym.py wait JOB_ID [--minutes M]    poll a job; download the package when it completes
    hypernym.py sync                         download every completed job for this repo not yet on disk
    hypernym.py packages                     list this repo's packages with drift against the working tree
    hypernym.py load [--package DIR]         print the newest (or given) package for a session to read

Common: --dir PATH (repo; default cwd).

Sign in: `hypernym.py login` — GitHub in the browser (like `claude login`), invite-only, nothing to copy or
remember and no Hypernym secret in this plugin. This machine gets its own key (~/.hypernym/identity/device.json, 600)
that signs every request; RESTORIA looks after the person's identity. `login --key KEY` uses a key instead.

Configuration (first match wins):
  1. HYPERNYM_API_KEY (+ optional HYPERNYM_API_URL, default https://zephyr-b-gpu.hypernym.ai)
  2. ~/.hypernym/config.json (mode 600): {"servers": [{"name", "url", "auth": "restoria", "device",
     "node_address"} | {"name", "url", "api_key"}]}
Packages: ~/.hypernym/packages/<owner>__<repo>/<completed UTC>_<commit12>_<job_id>/
NO .get().
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import nebula_statusline
import restoria_identity

HOME = Path.home() / '.hypernym'
CONFIG = HOME / 'config.json'
PACKAGES = HOME / 'packages'
SUBMISSIONS = HOME / 'submissions'
DEFAULT_URL = 'https://zephyr-b-gpu.hypernym.ai'
PAGE = 100
# What this plugin's device key may do on a Zephyr (RESTORIA device scopes). Nothing else is reachable —
# never /register, /login, benefact, other users' status, or admin.
DEVICE_SCOPES = ['account.read', 'float.read', 'github.link', 'repo_analyze.read', 'repo_analyze.submit']
DEVICE_AUTHORIZATION_DAYS = 365
POLL_S = 30


# ---------------------------------------------------------------- config / http

def load_servers() -> list:
    if 'HYPERNYM_API_KEY' in os.environ:
        url = DEFAULT_URL
        if 'HYPERNYM_API_URL' in os.environ:
            url = os.environ['HYPERNYM_API_URL']
        return [{'name': 'env', 'url': url.rstrip('/'), 'api_key': os.environ['HYPERNYM_API_KEY']}]
    if CONFIG.exists():
        return json.loads(CONFIG.read_text())['servers']
    return []


def pick_server(servers: list, name: str) -> dict:
    if not name:
        return servers[0]
    for s in servers:
        if s['name'] == name:
            return s
    sys.exit(f"no configured server named {name!r}; configured: {[s['name'] for s in servers]}")


def call(server: dict, path: str, body: dict = None) -> dict:
    data = None
    headers = {}
    method = 'GET'
    if body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
        method = 'POST'
    if 'auth' in server and server['auth'] == 'restoria':
        device_key, device = restoria_identity.load_device()
        signed_body = b''
        if data is not None:
            signed_body = data
        headers['X-API-Key'] = restoria_identity.request_credential(
            device_key, device, server['node_address'], method, path, signed_body)
    else:
        headers['X-API-Key'] = server['api_key']
    if 'account' in server:
        if server['account']:
            headers['X-Restoria-Account'] = server['account']
    req = urllib.request.Request(f"{server['url'].rstrip('/')}{path}", data=data, headers=headers, method=method)
    return json.load(urllib.request.urlopen(req, timeout=60))


def http_error_text(e: urllib.error.HTTPError) -> str:
    return f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"


# ---------------------------------------------------------------- git

def git(repo: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True)


def repo_key(url: str) -> str:
    """https://user@github.com/Owner/Repo.git | git@github.com:Owner/Repo → owner/repo"""
    m = re.search(r'github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$', url.strip())
    if m is None:
        return ''
    return f"{m.group(1)}/{m.group(2)}"


def repo_info(dirpath: str) -> dict:
    top = git(Path(dirpath), 'rev-parse', '--show-toplevel')
    if top.returncode != 0:
        sys.exit(f"not a git repository: {Path(dirpath).resolve()}")
    root = Path(top.stdout.strip())
    remote = git(root, 'remote', 'get-url', 'origin')
    if remote.returncode != 0:
        sys.exit(f"{root} has no 'origin' remote")
    key = repo_key(remote.stdout)
    if not key:
        sys.exit(f"origin is not a GitHub remote: {remote.stdout.strip()}")
    return {'root': root, 'key': key, 'key_lower': key.lower(), 'url': f"https://github.com/{key}",
            'head': git(root, 'rev-parse', 'HEAD').stdout.strip()}


def newest_pushed_commit(root: Path) -> str:
    """Newest ancestor of HEAD that some remote-tracking branch contains (the server clones from GitHub)."""
    git(root, 'fetch', '--quiet', 'origin')
    for commit in git(root, 'rev-list', '--first-parent', '--max-count=500', 'HEAD').stdout.split():
        if git(root, 'branch', '-r', '--contains', commit, '--list', 'origin/*').stdout.strip():
            return commit
    sys.exit("none of the last 500 commits on this branch exist on origin; push first")


# What the server will NOT see: it clones GitHub, so everything this computer has beyond the analyzed commit is read
# from GitHub's version instead. Files always change — this is information, never an error. Read-only git.
NOT_ON_GITHUB_LIST = 25
SECRET_NAME = re.compile(r'(^\.env|\.pem$|\.key$|\.p12$|\.pfx$|\.keystore$|\.jks$|^id_rsa|^id_ed25519|^id_ecdsa|secret|'
                         r'credential)', re.I)


def secret_looking(path: str) -> bool:
    """Named only as a count, never printed: .env*, keys, certificates, anything called secret/credential."""
    return SECRET_NAME.search(Path(path).name) is not None


def name_status(root: Path, *args) -> dict:
    """path → (status letter, shown text) from `git diff --name-status …`; renames shown as old → new."""
    out = {}
    for line in git(root, 'diff', '--name-status', *args).stdout.splitlines():
        parts = line.split('\t')
        letter = parts[0][:1]
        if letter in ('R', 'C') and len(parts) == 3:
            out[parts[2]] = (letter, f"{parts[1]} → {parts[2]}", (parts[1], parts[2]))
        elif len(parts) == 2:
            out[parts[1]] = (letter, parts[1], (parts[1],))
    return out


def not_on_github(root: Path, base: str) -> list:
    """Lines for the dry run / approval dialog: what differs between GitHub commit `base` and this computer."""
    ahead = int(git(root, 'rev-list', '--count', f"{base}..HEAD").stdout.strip())
    committed = name_status(root, base, 'HEAD')
    uncommitted = name_status(root, 'HEAD')
    untracked = [p for p in git(root, 'ls-files', '--others', '--exclude-standard').stdout.splitlines() if p]
    tracked = dict(committed)
    tracked.update(uncommitted)                       # a file changed in both: its working-tree state is what differs
    if not ahead and not tracked and not untracked:
        return [f"  This computer matches GitHub commit {base[:12]}: nothing here is missing from what the server reads."]
    if ahead:
        head = f"This computer is {ahead} commit{'s' if ahead != 1 else ''} ahead"
    else:
        head = "This computer has no unpushed commits"
    lines = [f"  The server reads GitHub commit {base[:12]}. {head}; these files differ and the package will describe "
             "their GitHub version.",
             f"  {ahead} commit{'s' if ahead != 1 else ''} not pushed ({len(committed)} files) · {len(uncommitted)} "
             f"uncommitted · {len(untracked)} untracked (not listed; the server never sees them)"]
    hidden = sum(1 for _, _, paths in tracked.values() if any(secret_looking(p) for p in paths))
    hidden += sum(1 for p in untracked if secret_looking(p))
    shown = [(letter, text) for letter, text, paths in sorted(tracked.values(), key=lambda v: v[1])
             if not any(secret_looking(p) for p in paths)]
    for letter, text in shown[:NOT_ON_GITHUB_LIST]:
        lines.append(f"    {letter}  {text}")
    if len(shown) > NOT_ON_GITHUB_LIST:
        lines.append(f"    …and {len(shown) - NOT_ON_GITHUB_LIST} more")
    if hidden:
        lines.append(f"    ({hidden} secret-looking file{'s' if hidden != 1 else ''} (.env, keys, credentials) counted, "
                     "not named)")
    return lines


def sha256_at(root: Path, commit: str, path: str):
    r = subprocess.run(['git', '-C', str(root), 'show', f"{commit}:{path}"], capture_output=True)
    if r.returncode != 0:
        return None
    return hashlib.sha256(r.stdout).hexdigest()


# ---------------------------------------------------------------- server queries

def repo_floats(server: dict, key_lower: str) -> dict:
    floats = {}
    offset = 0
    while True:
        page = call(server, f"/api/floats?limit={PAGE}&offset={offset}")
        for f in page['floats']:
            meta = f['metadata']
            if meta and 'git_remote' in meta and repo_key(meta['git_remote']).lower() == key_lower:
                path = ''
                if 'short_path' in meta:
                    path = meta['short_path']
                elif 'file_path' in meta:
                    path = meta['file_path']
                floats[f['float_id']] = (f['status'], path, 'submitted')
        offset += len(page['floats'])
        if not page['floats'] or offset >= page['total_count']:
            break
    return floats


def repo_jobs(server: dict, key_lower: str) -> list:
    jobs = call(server, "/api/repo/analyze?limit=100")['jobs']
    return [j for j in jobs if repo_key(j['repo_url']).lower() == key_lower]


# ---------------------------------------------------------------- packages

def package_root(info: dict) -> Path:
    return PACKAGES / info['key_lower'].replace('/', '__')


def existing_package(info: dict, job_id: str):
    root = package_root(info)
    if not root.exists():
        return None
    for d in root.iterdir():
        if d.is_dir() and d.name.endswith(f"_{job_id}"):
            return d
    return None


def fetch(server: dict, info: dict, job_id: str) -> Path:
    found = existing_package(info, job_id)
    if found is not None:
        return found
    job = call(server, f"/api/repo/analyze/{job_id}")
    if job['status'] != 'COMPLETED':
        sys.exit(f"{job_id} is {job['status']} at {job['stage']}, not COMPLETED")
    commit = job['commit_hash']
    completed = datetime.datetime.fromtimestamp(job['completed_at'], datetime.timezone.utc)
    pkg = package_root(info) / f"{completed.strftime('%Y%m%dT%H%M%SZ')}_{commit[:12]}_{job_id}"
    pkg.mkdir(parents=True)
    (pkg / 'treatment_prompt.txt').write_text(job['result']['treatment_prompt'])
    (pkg / 'job.json').write_text(json.dumps(job, indent=1))

    have_commit = git(info['root'], 'cat-file', '-e', f"{commit}^{{commit}}").returncode == 0
    files = []
    for f in job['files']:
        digest = None
        if have_commit:
            digest = sha256_at(info['root'], commit, f['file_path'])
        files.append({'path': f['file_path'], 'float_id': f['float_id'], 'sha256_at_commit': digest})

    question = None
    top_n = None
    budget = None
    sub = SUBMISSIONS / f"{job_id}.json"
    if sub.exists():
        s = json.loads(sub.read_text())
        question = s['question']
        top_n = s['top_n']
        budget = s['per_float_budget']
    manifest = {
        'repo': info['key'], 'server': server['name'], 'server_url': server['url'], 'job_id': job_id,
        'commit': commit, 'submitted_at_utc': iso(job['submitted_at']), 'completed_at_utc': iso(job['completed_at']),
        'fetched_at_utc': iso(time.time()), 'question': question, 'top_n': top_n, 'per_float_budget': budget,
        'metadata': job['result']['metadata'], 'files': files,
    }
    (pkg / 'manifest.json').write_text(json.dumps(manifest, indent=1))
    return pkg


def iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def drift(info: dict, manifest: dict) -> dict:
    changed = []
    missing = []
    unknown = 0
    for f in manifest['files']:
        if f['sha256_at_commit'] is None:
            unknown += 1
            continue
        p = info['root'] / f['path']
        if not p.exists():
            missing.append(f['path'])
        elif hashlib.sha256(p.read_bytes()).hexdigest() != f['sha256_at_commit']:
            changed.append(f['path'])
    behind = None
    r = git(info['root'], 'rev-list', '--count', f"{manifest['commit']}..HEAD")
    if r.returncode == 0:
        behind = int(r.stdout.strip())
    return {'files': len(manifest['files']), 'changed': changed, 'missing': missing, 'unknown': unknown,
            'commits_behind': behind}


def list_packages(info: dict) -> list:
    root = package_root(info)
    if not root.exists():
        return []
    pkgs = [d for d in root.iterdir() if d.is_dir() and (d / 'manifest.json').exists()]
    return sorted(pkgs, key=lambda d: d.name, reverse=True)


def describe(info: dict, pkg: Path) -> list:
    m = json.loads((pkg / 'manifest.json').read_text())
    d = drift(info, m)
    age_h = (time.time() - datetime.datetime.strptime(m['completed_at_utc'], '%Y-%m-%dT%H:%M:%SZ')
             .replace(tzinfo=datetime.timezone.utc).timestamp()) / 3600
    hamming = len(d['changed']) + len(d['missing'])
    behind = 'unknown (commit not in local clone)'
    if d['commits_behind'] is not None:
        behind = str(d['commits_behind'])
    lines = [f"  {pkg}",
             f"    job {m['job_id']} on {m['server']} | commit {m['commit'][:12]} | completed {m['completed_at_utc']} "
             f"({age_h:.1f} h ago) | {m['metadata']['prompt_tokens']} tokens, {m['metadata']['files_selected']} files",
             f"    drift: {hamming}/{d['files']} files differ from the working tree, commits since package: {behind}"]
    if d['unknown']:
        lines.append(f"    {d['unknown']} files have no baseline hash (commit missing locally when fetched)")
    for p in d['changed'][:10]:
        lines.append(f"      changed: {p}")
    for p in d['missing'][:10]:
        lines.append(f"      missing: {p}")
    if m['question']:
        lines.append(f"    question: {m['question'][:160]}")
    return lines


# ---------------------------------------------------------------- commands

# What the two plugins are, in the Pilot's words (Hypernym and Nebula capitalized in prose, lowercase in commands).
ABOUT = (
    "Nebula — the local shield: flags fabricated confession, decision hand-back, claims beyond evidence and forced "
    "repetition in Claude's replies; sends nothing.",
    "Hypernym — the Zephyr connector: analyzes the repo you're in on Hypernym's server and loads the result into "
    "the session as a context package; never sends source code.",
)
INSTALL_LINES = (
    "Install both: command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh; claude plugin "
    "marketplace add HypernymAI/hypernym-nebula && claude plugin install nebula@hypernym-nebula && claude plugin "
    "install hypernym@hypernym-nebula, then restart Claude Code once",
    "Update: claude plugin marketplace update hypernym-nebula && claude plugin update nebula@hypernym-nebula && "
    "claude plugin update hypernym@hypernym-nebula",
)


def cmd_about(args, servers):
    for line in ABOUT:
        print(line)
    print()
    for line in INSTALL_LINES:
        print(line)


def acting_account_line(server: dict) -> str:
    """Which of the person's accounts this machine acts as on `server`, and why."""
    mine = call(server, "/api/me/accounts")
    current = [a['email'] for a in mine['accounts'] if a['current']]
    others = [a['email'] for a in mine['accounts'] if not a['current']]
    why = "your identity's default account on this server"
    if 'account' in server:
        why = "chosen on this machine with: account use"
    line = f"acting as {', '.join(current)} ({why})"
    if others:
        line += f"; your other accounts here: {', '.join(others)} (switch: account use EMAIL)"
    return line


def credits(v: float) -> str:
    """1001539.24 → '1,001,539.24'; 500.0 → '500'. User-facing text says "credits", never the internal unit name."""
    return f"{v:,.2f}".rstrip('0').rstrip('.')


def payment_line(q: dict) -> str:
    """Where a submission's credits come from, as the Zephyr decides it (GET /api/me/quota → payment_source; Zephyrs
    without that field → '')."""
    if 'payment_source' not in q:
        return ''
    ps = q['payment_source']
    if ps['pays_from'] == 'hub':
        return f"pays from {ps['reason']} — {credits(ps['hub_available_fl0wt'])} credits available"
    if ps['pays_from'] == 'zephyr':
        return f"pays from this Zephyr's balance — {credits(ps['zephyr_balance_fl0wt'])} credits ({ps['reason']})"
    return f"payment source unknown right now ({ps['reason']})"


def cmd_help(args, servers):
    for line in ABOUT:
        print(line)
    print()
    info = repo_info(args.dir)
    print(f"repo: {info['key']}  (local HEAD {info['head'][:12]})")
    print(f"config: {len(servers)} server(s)")
    if portal_of(servers) is not None:
        try:
            policy = fetch_policy(servers)
        except (urllib.error.URLError, OSError) as e:
            policy = cached_policy()
            print(f"(portal unreachable: {e}; using the last sending setting read)")
        level = sending_level(policy, info['key_lower'])
        print(f"sending for this repo: {level} (change it on the portal's Sending tab: "
              f"{portal_of(servers)['portal']}/account?tab=sending)")
        if level == 'off':
            print("nothing is sent for this repo; local packages still load")
            return
    for server in servers:
        print(f"\n[{server['name']}] {server['url']}")
        try:
            q = call(server, "/api/me/quota")
            print(f"  auth ok: tier {q['tier']}, active floats {q['my_active_floats']}/{q['my_max_concurrent']}, "
                  f"can submit now: {q['can_submit_now']}")
            print(f"  {acting_account_line(server)}")
            fl = repo_floats(server, info['key_lower'])
            jobs = repo_jobs(server, info['key_lower'])
        except urllib.error.HTTPError as e:
            print(f"  {http_error_text(e)}")
            if e.code == 401:
                print("  your login is not valid (expired or revoked) — run: login")
            continue
        except (urllib.error.URLError, OSError) as e:
            print(f"  unreachable: {e}")
            continue
        if 'expires_at' in server and server['expires_at'] - time.time() < 3 * 86400:
            print(f"  login expires {time.strftime('%Y-%m-%d', time.localtime(server['expires_at']))} — run: login")
        done = sum(1 for s, _, _ in fl.values() if s == 'COMPLETED')
        print(f"  floats for this repo (direct submissions): {len(fl)} ({done} completed)")
        for j in jobs:
            print(f"  repo-analyze {j['job_id']}: {j['status']} at {j['current_stage']} (commit {j['commit_hash'][:12]})")
            if j['status'] == 'COMPLETED' and existing_package(info, j['job_id']) is None:
                print(f"    downloaded → {fetch(server, info, j['job_id'])}")
        if not jobs:
            print("  no repo-analyze jobs for this repo")
    pkgs = list_packages(info)
    print(f"\nlocal packages ({PACKAGES}):")
    if not pkgs:
        print("  none — run: hypernym.py analyze --question \"...\"")
    for p in pkgs:
        print('\n'.join(describe(info, p)))
    print(f"\n{nebula_statusline.peers('hypernym')}")
    print("\ncommands: floats [--files] | analyze --question TEXT [--top-n N] [--budget B] | wait JOB | sync | packages | load")


def cmd_floats(args, servers):
    info = repo_info(args.dir)
    print(f"repo: {info['key']}")
    for server in servers:
        fl = repo_floats(server, info['key_lower'])
        done = sum(1 for s, _, _ in fl.values() if s == 'COMPLETED')
        print(f"[{server['name']}] {server['url']}: {len(fl)} floats ({done} completed)")
        if args.files:
            for fid, (status, path, source) in sorted(fl.items(), key=lambda kv: kv[1][1]):
                print(f"    {status:10s} {path}  [{fid[:12]}]")


def cmd_analyze(args, servers):
    info = repo_info(args.dir)
    server = pick_server(servers, args.server)
    execute = args.execute
    if args.auto:
        execute = True
    policy = None
    if portal_of(servers) is not None:
        try:
            policy = fetch_policy(servers)
        except (urllib.error.URLError, OSError) as e:
            # Fail closed: without the person's current setting, nothing is sent.
            if execute:
                sys.exit(f"could not read your sending setting from the portal ({e}); nothing was sent")
            policy = cached_policy()
    level = sending_level(policy, info['key_lower'])
    if level == 'off':
        sys.exit(f"sending is off for {info['key']} (your portal's Sending tab): nothing was sent")
    if args.auto:
        if level != 'auto':
            sys.exit(f"sending for {info['key']} is '{level}', not auto: show the dry run and ask the person")
        spent = auto_spent_today()
        limit = policy['auto_daily_credits']
        if spent + args.top_n > limit:
            sys.exit(f"this could use up to {args.top_n} credits and {spent} of today's {limit} auto credits are "
                     "used: show the dry run and ask the person")
    question = args.question
    if args.question_file:
        question = Path(args.question_file).read_text()
    if not question or not question.strip():
        sys.exit("a question is required: --question TEXT or --question-file FILE")
    commit = args.commit
    if not commit:
        commit = newest_pushed_commit(info['root'])
    if commit != info['head']:
        print(f"note: analyzing {commit[:12]} (newest commit on GitHub); local HEAD {info['head'][:12]} is ahead")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    seed = f"{info['key_lower'].replace('/', '__')}-{commit[:12]}-{stamp}"
    body = {'repo_url': info['url'], 'commit': commit, 'problem_statement': question,
            'top_n': args.top_n, 'per_float_budget': args.budget, 'session_seed': seed}
    if not execute:
        try:
            payer = acting_account_line(server)
            pays = payment_line(call(server, "/api/me/quota"))
            if pays:
                payer += f"\n             {pays}"
        except urllib.error.HTTPError as e:
            payer = f"could not read which account ({http_error_text(e)})"
        except (urllib.error.URLError, OSError) as e:
            payer = f"could not read which account ({server['url']} unreachable: {e})"
        print("DRY RUN — nothing submitted. Would POST /api/repo/analyze to "
              f"{server['name']} ({server['url']}):")
        print(f"  repo:      {info['url']}")
        print(f"  commit:    {commit}")
        print(f"  top_n:     {args.top_n}   per_float_budget: {args.budget}")
        print(f"  question:  {question}")
        print(f"  cost:      up to {args.top_n} new floats = up to {args.top_n} credits (files the server has "
              "compressed before cost nothing)")
        print(f"  paid by:   {payer}")
        print("Not on GitHub:")
        print('\n'.join(not_on_github(info['root'], commit)))
        print("A real run creates a float for every ranked file the server has not compressed before.")
        print("To submit: the hypernym_analyze tool (execute) asks the person in Hypernym's approval dialog; from a "
              "shell, --execute only after their explicit yes.")
        if level == 'auto':
            print(f"(sending is auto for this repo: --auto may submit without asking, up to "
                  f"{policy['auto_daily_credits']} credits a day)")
        return
    try:
        r = call(server, "/api/repo/analyze", body)
    except urllib.error.HTTPError as e:
        sys.exit(f"submit failed: {http_error_text(e)}")
    SUBMISSIONS.mkdir(parents=True, exist_ok=True)
    (SUBMISSIONS / f"{r['job_id']}.json").write_text(json.dumps(
        {'job_id': r['job_id'], 'server': server['name'], 'repo': info['key'], 'commit': commit, 'question': question,
         'top_n': args.top_n, 'per_float_budget': args.budget, 'session_seed': seed, 'submitted_at_utc': iso(time.time())},
        indent=1))
    if args.auto:
        record_auto_spend(args.top_n)
    print(f"submitted {r['job_id']} to {server['name']}: {r['status']} at {r['stage']}")
    print(f"it runs on the server; check with: hypernym.py wait {r['job_id']}  (or hypernym.py help later)")


def cmd_wait(args, servers):
    info = repo_info(args.dir)
    server = pick_server(servers, args.server)
    deadline = time.time() + args.minutes * 60
    last = ''
    while True:
        job = call(server, f"/api/repo/analyze/{args.job_id}")
        line = f"{job['status']} at {job['stage']} ({job['stage_number']}/{job['total_stages']})"
        if 'float_progress' in job and job['float_progress']:
            fp = job['float_progress']
            line += f" floats {fp['completed']}/{fp['total']}"
        if line != last:
            print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)
            last = line
        if job['status'] == 'COMPLETED':
            pkg = fetch(server, info, args.job_id)
            print(f"package → {pkg}")
            print('\n'.join(describe(info, pkg)))
            return
        if job['status'].startswith('DRY_RUN_GATE'):
            print(f"server dry-run gate: stopped at {job['stage']}; no floats past this point were created")
            if 'files' in job:
                for i, f in enumerate(job['files'], 1):
                    score = ''
                    if 'combined_score' in f:
                        score = f"  score {f['combined_score']:.4f}"
                    print(f"  {i:>2}. {f['file_path']}{score}")
            return
        if job['status'] == 'FAILED':
            err = ''
            if 'error' in job:
                err = job['error']
            sys.exit(f"job failed: {err}")
        if time.time() >= deadline:
            print(f"still running after {args.minutes} min; run wait again later")
            return
        time.sleep(POLL_S)


def raw_json(url: str, method: str, path: str, body: bytes = b'', headers: dict = None) -> dict:
    """An unauthenticated (or proof-carrying) JSON call — enrollment only."""
    all_headers = {}
    if headers is not None:
        all_headers.update(headers)
    data = None
    if body:
        data = body
        all_headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(f"{url}{path}", data=data, headers=all_headers, method=method)
    return json.load(urllib.request.urlopen(req, timeout=30))


def enroll(url: str, minutes: float) -> dict:
    """Sign in like any web login: GitHub in the browser, invite-only, nothing to copy or remember. This machine gets
    its own key (kept in ~/.hypernym/identity, mode 600) that signs every request; RESTORIA looks after the person's
    identity. No app credential, token, password or Hypernym secret is ever held here."""
    device_key, device = restoria_identity.load_or_create_device()
    try:
        node = raw_json(url, 'GET', '/api/github/login/enroll/node')['node_address']
    except urllib.error.HTTPError as e:
        sys.exit(f"{url} does not offer sign-in: {http_error_text(e)}")
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"{url} unreachable: {e}")
    body = json.dumps({'device': device, 'label': f"hypernym plugin on {socket.gethostname()}",
                       'scopes': ','.join(sorted(DEVICE_SCOPES))}).encode()
    start_path = '/api/github/login/enroll/start'
    proof = restoria_identity.request_credential(device_key, device, node, 'POST', start_path, body)
    try:
        start = raw_json(url, 'POST', start_path, body, headers={'X-Restoria-Proof': proof})
    except urllib.error.HTTPError as e:
        sys.exit(f"could not start sign-in on {url}: {http_error_text(e)}")
    # flush: the link must be on screen (and in a log a background run writes) BEFORE the wait, not after it
    print("Opening your browser to sign in with GitHub. If it does not open, visit:", flush=True)
    print(f"  {start['auth_url']}", flush=True)
    webbrowser.open(start['auth_url'])
    status_path = f"/api/github/login/enroll/status/{start['enrollment_id']}"
    deadline = min(time.time() + minutes * 60, start['expires_at'] + 60)
    while time.time() < deadline:
        time.sleep(2)
        proof = restoria_identity.request_credential(device_key, device, node, 'GET', status_path, b'')
        try:
            st = raw_json(url, 'GET', status_path, headers={'X-Restoria-Proof': proof})
        except urllib.error.HTTPError as e:
            sys.exit(f"sign-in status failed: {http_error_text(e)}")
        except (urllib.error.URLError, OSError) as e:
            print(f"  (status check failed: {e}; retrying)")
            continue
        if st['status'] == 'pending':
            continue
        if st['status'] == 'not_invited':
            if 'waitlisted' in st:
                print(f"GitHub @{st['github_username']} is on the Hypernym waitlist — it's invite-only for now. "
                      "When you're invited, run login again and you're in.")
                sys.exit(3)
            sys.exit(f"GitHub @{st['github_username']} is not on this Zephyr's invite list yet. "
                     "Ask the operator for an invitation, then run login again.")
        if st['status'] == 'failed':
            sys.exit(f"sign-in could not finish: {st['error']}")
        if st['status'] == 'enrolled':
            return {'github_username': st['github_username'], 'node_address': node,
                    'device': device, 'expires_at': time.time() + DEVICE_AUTHORIZATION_DAYS * 86400}
    sys.exit("sign-in was not completed in time; run login again")


def pair_with_portal(portal: str, minutes: float) -> dict:
    """Sign in through the Hypernym account portal (sign-up, terms, credit and computers live there): this machine's
    key proves itself to the portal, the person approves the short code in the browser, and the portal vouches for
    this computer on RESTORIA. Nothing to copy or remember; no token or password is stored here."""
    device_key, device = restoria_identity.load_or_create_device()
    label = f"hypernym plugin on {socket.gethostname()}"
    issued_at = str(int(time.time() * 1_000_000))
    text = restoria_identity.canonical_text('HYPERNYM-PORTAL-DEVICE-PAIRING-v1',
                                            [('device', device), ('label', label), ('issued_at', issued_at)])
    body = json.dumps({'device': device, 'label': label, 'issued_at': issued_at,
                       'signature': restoria_identity.sign_text(device_key, text)}).encode()
    try:
        start = raw_json(portal, 'POST', '/api/device/start', body)
    except urllib.error.HTTPError as e:
        sys.exit(f"the portal refused this computer: {http_error_text(e)}")
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"{portal} unreachable: {e}")
    # flush: the code and link must be on screen (and in a background run's log) BEFORE the wait
    print(f"Your code: {start['user_code']}", flush=True)
    print("Opening your browser to approve this computer. If it does not open, visit:", flush=True)
    print(f"  {start['verification_uri']}", flush=True)
    webbrowser.open(start['verification_uri'])
    deadline = min(time.time() + minutes * 60, start['expires_at'] + 60)
    while time.time() < deadline:
        time.sleep(2)
        try:
            st = raw_json(portal, 'GET', f"/api/device/status/{start['enrollment_id']}")
        except urllib.error.HTTPError as e:
            sys.exit(f"sign-in status failed: {http_error_text(e)}")
        except (urllib.error.URLError, OSError) as e:
            print(f"  (status check failed: {e}; retrying)", flush=True)
            continue
        if st['status'] == 'pending':
            continue
        if st['status'] == 'approved':
            return {'device': device, 'principal': st['principal'],
                    'expires_at': time.time() + DEVICE_AUTHORIZATION_DAYS * 86400}
        if st['status'] == 'denied':
            sys.exit("this computer was denied on the portal")
        sys.exit(f"the code {st['status']}; run login again")
    sys.exit("sign-in was not completed in time; run login again")


# ---------------------------------------------------------------- sending policy
# What this person's computers may send (set on the portal's Sending tab; read with a device signature, cached here).
#   off  — nothing leaves this computer for that repo: no analysis, and the status line stops asking the server
#   ask  — every analysis is a dry run first and needs the person's explicit yes (the default; also without a portal)
#   auto — the agent may submit on its own (analyze --auto), up to the daily credit limit; beyond it, it asks
POLICY_FILE = HOME / 'sending_policy.json'
AUTO_SPEND_FILE = HOME / 'auto_spend.json'
POLICY_FRESH_SECONDS = 600


def portal_of(servers: list):
    for server in servers:
        if 'portal' in server:
            return server
    return None


def fetch_policy(servers: list):
    """The person's policy from the portal (refreshes the cache), or None when no server was signed in via a portal."""
    server = portal_of(servers)
    if server is None:
        return None
    device_key, device = restoria_identity.load_device()
    issued_at = str(int(time.time() * 1_000_000))
    text = f"HYPERNYM-PORTAL-SENDING-POLICY-v1\ndevice={device}\nissued_at={issued_at}"
    body = json.dumps({'device': device, 'issued_at': issued_at,
                       'signature': restoria_identity.sign_text(device_key, text)}).encode()
    policy = raw_json(server['portal'], 'POST', '/api/device/policy', body)
    policy['fetched_at'] = time.time()
    HOME.mkdir(parents=True, exist_ok=True)
    POLICY_FILE.write_text(json.dumps(policy))
    return policy


def cached_policy():
    if not POLICY_FILE.exists():
        return None
    return json.loads(POLICY_FILE.read_text())


def sending_level(policy, repo_key_lower: str) -> str:
    if policy is None:
        return 'ask'
    if repo_key_lower in policy['repos']:
        return policy['repos'][repo_key_lower]
    return policy['level']


def auto_spent_today() -> int:
    today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
    if not AUTO_SPEND_FILE.exists():
        return 0
    spend = json.loads(AUTO_SPEND_FILE.read_text())
    if spend['date'] != today:
        return 0
    return spend['credits']


def record_auto_spend(credits: int) -> None:
    today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
    AUTO_SPEND_FILE.write_text(json.dumps({'date': today, 'credits': auto_spent_today() + credits}))


def save_server(server: dict) -> None:
    config = {'servers': []}
    if CONFIG.exists():
        config = json.loads(CONFIG.read_text())
    config['servers'] = [s for s in config['servers'] if s['name'] != server['name']] + [server]
    HOME.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077)
    try:
        CONFIG.write_text(json.dumps(config, indent=1))
    finally:
        os.umask(old_umask)
    os.chmod(CONFIG, 0o600)


def cmd_login(args, servers):
    """Sign in: GitHub in the browser (default, invite-only, nothing to remember),
    or --key KEY / --key - for a key you were given. Saves to config (600)."""
    url = args.url.rstrip('/')
    server = {'name': args.name, 'url': url}
    who = ''
    if args.key:
        key = args.key
        if key == '-':
            key = sys.stdin.readline().strip()
        if not key:
            sys.exit("empty key")
        server['api_key'] = key
    elif args.portal:
        paired = pair_with_portal(args.portal.rstrip('/'), args.minutes)
        try:
            node = raw_json(url, 'GET', '/api/github/login/enroll/node')['node_address']
        except urllib.error.HTTPError as e:
            sys.exit(f"{url} does not offer sign-in: {http_error_text(e)}")
        except (urllib.error.URLError, OSError) as e:
            sys.exit(f"{url} unreachable: {e}")
        server['auth'] = 'restoria'
        server['device'] = paired['device']
        server['node_address'] = node
        server['expires_at'] = paired['expires_at']
        server['portal'] = args.portal.rstrip('/')
        who = f"identity {paired['principal']}, "
    else:
        e = enroll(url, args.minutes)
        server['auth'] = 'restoria'
        server['device'] = e['device']
        server['node_address'] = e['node_address']
        server['expires_at'] = e['expires_at']
        who = f"GitHub @{e['github_username']}, "
    try:
        q = call(server, "/api/me/quota")
    except urllib.error.HTTPError as e:
        if args.portal:
            # e.g. the account isn't set up yet on the portal: the Zephyr's message says what to finish, and where
            save_server(server)
            sys.exit(f"this computer is signed in, but {url} refused it: {http_error_text(e)}")
        sys.exit(f"key rejected by {url}: {http_error_text(e)}")
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"{url} unreachable: {e}")
    save_server(server)
    print(f"logged in to {args.name} ({url}): {who}tier {q['tier']}, "
          f"max concurrent floats {q['my_max_concurrent']}. Saved to {CONFIG} (mode 600).")
    if 'auth' in server:
        print(acting_account_line(server))
    if 'expires_at' in server:
        print(f"This device is authorized until {time.strftime('%Y-%m-%d', time.localtime(server['expires_at']))}; "
              "every request is signed by its key — no token is stored.")
    if 'HYPERNYM_API_KEY' in os.environ:
        print("note: HYPERNYM_API_KEY is set in this environment and takes precedence over the config file.")


def cmd_github(args, servers):
    """GitHub access for private repos: `github status` lists links; `github link` starts OAuth for this repo."""
    server = pick_server(servers, args.server)
    if args.action == 'status':
        links = call(server, "/api/github/links")['links']
        active = [l for l in links if l['status'] == 'active']
        if not active:
            print(f"no active GitHub link on {server['name']}; private repos cannot be analyzed yet. Run: github link")
        for l in active:
            print(f"  active: GitHub user {l['github_username']} (link {l['id']}, linked for {l['repo_url']})")
        return
    info = repo_info(args.dir)
    try:
        r = call(server, "/api/github/init-link", {'repo_url': info['url']})
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')
        if e.code == 409 and 'already_linked' in body:
            d = json.loads(body)
            print(f"already linked: GitHub user {d['github_username']} (link {d['link_id']}, {d['status']})")
            return
        sys.exit(f"github link failed: HTTP {e.code}: {body[:300]}")
    print("Open this URL in a browser and approve access:", flush=True)
    print(f"  {r['auth_url']}", flush=True)
    print("The approval redirects to the server's GitHub callback; if that callback is not reachable from your "
          "browser, the link cannot complete (see README).")
    deadline = time.time() + args.minutes * 60
    while time.time() < deadline:
        time.sleep(5)
        s = call(server, f"/api/github/status/{r['id']}")
        if s['status'] == 'active':
            print(f"linked: GitHub user {s['github_username']}. Private repos that account can read can now be analyzed.")
            return
        if s['status'] not in ('pending',):
            sys.exit(f"link ended as {s['status']}: {s['error_message']}")
    print(f"still pending after {args.minutes} min; run `github status` later")


def cmd_account(args, servers):
    """`account list` — your accounts on the server (one identity can hold several, e.g. two emails);
    `account use EMAIL` — make EMAIL this machine's default for that server."""
    server = pick_server(servers, args.server)
    mine = call(server, "/api/me/accounts")
    if args.action == 'list':
        print(f"[{server['name']}] your accounts:")
        for a in mine['accounts']:
            tags = []
            if a['default']:
                tags.append('server default')
            if a['current']:
                tags.append('acting as')
            suffix = ''
            if tags:
                suffix = f"  ({', '.join(tags)})"
            print(f"  {a['email']}{suffix}")
        return
    if not args.email:
        sys.exit("account use needs an EMAIL")
    if args.email not in [a['email'] for a in mine['accounts']]:
        sys.exit(f"{args.email} is not one of your accounts on {server['name']}: "
                 f"{', '.join(a['email'] for a in mine['accounts'])}")
    server['account'] = args.email
    save_server(server)
    print(f"{server['name']}: this machine now acts as {args.email} by default (override per command with --account).")


def cmd_cancel(args, servers):
    server = pick_server(servers, args.server)
    try:
        r = call(server, f"/api/repo/analyze/{args.job_id}/cancel", {})
    except urllib.error.HTTPError as e:
        sys.exit(f"cancel failed: {http_error_text(e)}")
    if not r['cancelled']:
        print(f"{args.job_id} not cancelled: {r['message']}")
        return
    f = r['floats']
    print(f"{args.job_id} cancelled at {r['stage']}: {f['total_cancelled']} in-flight float(s) cancelled, "
          f"{len(f['not_active'])} already finished, {len(f['not_owned'])} shared from other users (left alone)")


def cmd_sync(args, servers):
    info = repo_info(args.dir)
    for server in servers:
        for j in repo_jobs(server, info['key_lower']):
            if j['status'] == 'COMPLETED':
                print(f"{j['job_id']} → {fetch(server, info, j['job_id'])}")


def cmd_packages(args, servers):
    info = repo_info(args.dir)
    pkgs = list_packages(info)
    if not pkgs:
        print(f"no packages for {info['key']} in {package_root(info)}")
    for p in pkgs:
        print('\n'.join(describe(info, p)))


def cmd_load(args, servers):
    info = repo_info(args.dir)
    if args.package:
        pkg = Path(args.package)
    else:
        pkgs = list_packages(info)
        if not pkgs:
            sys.exit(f"no packages for {info['key']}; run analyze first")
        pkg = pkgs[0]
    print('\n'.join(describe(info, pkg)))
    print(f"\n===== {pkg / 'treatment_prompt.txt'} =====\n")
    print((pkg / 'treatment_prompt.txt').read_text())


def cmd_statuscache(args, servers):
    """Write ~/.hypernym/status/<owner__repo>.json for the status line (refreshed in the background)."""
    info = repo_info(args.dir)
    out = {'repo': info['key'], 'refreshed_at': time.time(), 'servers': []}
    policy = cached_policy()
    if portal_of(servers) is not None:
        if policy is None or time.time() - policy['fetched_at'] > POLICY_FRESH_SECONDS:
            try:
                policy = fetch_policy(servers)
            except (urllib.error.URLError, OSError):
                pass  # keep the cached setting; a later refresh retries
    if sending_level(policy, info['key_lower']) == 'off':
        # Sending is off for this repo: the status line asks the server nothing.
        servers = []
        out['sending_off'] = True
    for server in servers:
        entry = {'name': server['name'], 'error': None, 'floats': 0, 'floats_done': 0, 'jobs': []}
        try:
            call(server, "/api/me/quota")
            fl = repo_floats(server, info['key_lower'])
            entry['floats'] = len(fl)
            entry['floats_done'] = sum(1 for s, _, _ in fl.values() if s == 'COMPLETED')
            for j in repo_jobs(server, info['key_lower']):
                job = {'job_id': j['job_id'], 'status': j['status'], 'stage': j['current_stage'],
                       'stage_number': j['current_stage_number'], 'floats_total': None, 'floats_completed': None}
                if j['status'] == 'PROCESSING':
                    detail = call(server, f"/api/repo/analyze/{j['job_id']}")
                    if 'float_progress' in detail and detail['float_progress']:
                        job['floats_total'] = detail['float_progress']['total']
                        job['floats_completed'] = detail['float_progress']['completed']
                if j['status'] == 'COMPLETED' and existing_package(info, j['job_id']) is None:
                    fetch(server, info, j['job_id'])
                entry['jobs'].append(job)
        except urllib.error.HTTPError as e:
            entry['error'] = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            entry['error'] = 'unreachable'
        out['servers'].append(entry)
    pkgs = list_packages(info)
    out['packages'] = len(pkgs)
    out['newest_package'] = None
    if pkgs:
        m = json.loads((pkgs[0] / 'manifest.json').read_text())
        d = drift(info, m)
        out['newest_package'] = {'job_id': m['job_id'], 'changed': len(d['changed']) + len(d['missing']),
                                 'files': d['files'], 'commits_behind': d['commits_behind']}
    target = HOME / 'status' / f"{info['key_lower'].replace('/', '__')}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix('.tmp')
    tmp.write_text(json.dumps(out))
    tmp.replace(target)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default='.')
    ap.add_argument('--server', default='')
    ap.add_argument('--account', default='', help='act as this one of your accounts (email) for this command')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('help')
    sub.add_parser('about')
    p = sub.add_parser('floats')
    p.add_argument('--files', action='store_true')
    p = sub.add_parser('analyze')
    p.add_argument('--question', default='')
    p.add_argument('--question-file', default='')
    p.add_argument('--top-n', type=int, default=40)
    p.add_argument('--budget', type=int, default=3077)
    p.add_argument('--commit', default='')
    p.add_argument('--execute', action='store_true')
    p.add_argument('--auto', action='store_true', help='submit without asking, only when sending is auto and under the daily limit')
    p = sub.add_parser('wait')
    p.add_argument('job_id')
    p.add_argument('--minutes', type=float, default=10)
    p = sub.add_parser('cancel')
    p.add_argument('job_id')
    p = sub.add_parser('login')
    p.add_argument('--key', default='')
    p.add_argument('--url', default=DEFAULT_URL)
    p.add_argument('--portal', default='', help='sign in through the account portal, e.g. https://portal.hypernym.ai')
    p.add_argument('--name', default='zephyr')
    p.add_argument('--minutes', type=float, default=10)
    p = sub.add_parser('account')
    p.add_argument('action', choices=['list', 'use'])
    p.add_argument('email', nargs='?', default='')
    p = sub.add_parser('github')
    p.add_argument('action', choices=['status', 'link'])
    p.add_argument('--minutes', type=float, default=5)
    sub.add_parser('sync')
    sub.add_parser('packages')
    sub.add_parser('statuscache')
    p = sub.add_parser('load')
    p.add_argument('--package', default='')
    args = ap.parse_args()

    servers = load_servers()
    if args.account:
        for server in servers:
            server['account'] = args.account
    if args.cmd == 'about':
        cmd_about(args, servers)
        return
    if not servers and args.cmd != 'login':
        print("Not signed in. Say \"hypernym login\" (GitHub in the browser, invite-only), or hypernym login "
              f"--key YOUR_KEY, or set HYPERNYM_API_KEY (and HYPERNYM_API_URL if not {DEFAULT_URL}).")
        sys.exit(2)
    commands = {'help': cmd_help, 'floats': cmd_floats, 'analyze': cmd_analyze, 'wait': cmd_wait,
                'sync': cmd_sync, 'packages': cmd_packages, 'load': cmd_load, 'statuscache': cmd_statuscache,
                'cancel': cmd_cancel, 'login': cmd_login, 'github': cmd_github, 'account': cmd_account}
    commands[args.cmd](args, servers)


if __name__ == '__main__':
    main()
