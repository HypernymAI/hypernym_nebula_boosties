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

Configuration (first match wins):
  1. HYPERNYM_API_KEY (+ optional HYPERNYM_API_URL, default https://zephyr-b-gpu.hypernym.ai)
  2. ~/.hypernym/config.json (mode 600): {"servers": [{"name": "...", "url": "...", "api_key": "..."}]}
Packages: ~/.hypernym/packages/<owner>__<repo>/<completed UTC>_<commit12>_<job_id>/
NO .get().
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import nebula_statusline

HOME = Path.home() / '.hypernym'
CONFIG = HOME / 'config.json'
PACKAGES = HOME / 'packages'
SUBMISSIONS = HOME / 'submissions'
DEFAULT_URL = 'https://zephyr-b-gpu.hypernym.ai'
PAGE = 100
# GitHub login goes through the ClaudeStorm app, which finds or creates the Zephyr user for a GitHub account.
LOGIN_APP = {'app_id': 'claudestorm',
             'app_secret_hash': '0cabf53047051a5636da91c1be7dcd123d72dafa08f0600e02aeeca8dfbc462f'}
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
    headers = {'X-API-Key': server['api_key']}
    if body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(f"{server['url'].rstrip('/')}{path}", data=data, headers=headers)
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

def cmd_help(args, servers):
    info = repo_info(args.dir)
    print(f"repo: {info['key']}  (local HEAD {info['head'][:12]})")
    print(f"config: {len(servers)} server(s)")
    for server in servers:
        print(f"\n[{server['name']}] {server['url']}")
        try:
            q = call(server, "/api/me/quota")
            print(f"  auth ok: tier {q['tier']}, active floats {q['my_active_floats']}/{q['my_max_concurrent']}, "
                  f"can submit now: {q['can_submit_now']}")
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
    if not args.execute:
        print("DRY RUN — nothing submitted. Would POST /api/repo/analyze to "
              f"{server['name']} ({server['url']}):")
        print(f"  repo:      {info['url']}")
        print(f"  commit:    {commit}")
        print(f"  top_n:     {args.top_n}   per_float_budget: {args.budget}")
        print(f"  question:  {question}")
        print("A real run creates a float for every ranked file the server has not compressed before.")
        print("Re-run with --execute only when the user has explicitly approved this submission.")
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


def load_app_credentials() -> dict:
    """App used for GitHub login: HYPERNYM_APP_ID + HYPERNYM_APP_SECRET_HASH, "app" in the config, or LOGIN_APP."""
    if 'HYPERNYM_APP_ID' in os.environ and 'HYPERNYM_APP_SECRET_HASH' in os.environ:
        return {'app_id': os.environ['HYPERNYM_APP_ID'], 'app_secret_hash': os.environ['HYPERNYM_APP_SECRET_HASH']}
    if CONFIG.exists():
        config = json.loads(CONFIG.read_text())
        if 'app' in config:
            return config['app']
    return LOGIN_APP


def app_call(url: str, app: dict, path: str, body: dict = None) -> dict:
    headers = {'X-App-Id': app['app_id'], 'X-App-Secret-Hash': app['app_secret_hash']}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(f"{url}{path}", data=data, headers=headers)
    return json.load(urllib.request.urlopen(req, timeout=30))


def browser_login(url: str, minutes: float) -> dict:
    """GitHub login in the browser (the same flow as ClaudeStorm's login_with_github). Returns the active status."""
    app = load_app_credentials()
    try:
        init = app_call(url, app, "/api/github/login/init", {})
    except urllib.error.HTTPError as e:
        sys.exit(f"could not start login on {url}: {http_error_text(e)}")
    print("Opening your browser to sign in with GitHub. If it does not open, visit:")
    print(f"  {init['auth_url']}")
    webbrowser.open(init['auth_url'])
    deadline = min(time.time() + minutes * 60, init['expires_at'])
    while time.time() < deadline:
        time.sleep(2)
        try:
            s = app_call(url, app, f"/api/github/login/status/{init['id']}")
        except (urllib.error.URLError, OSError) as e:
            print(f"  (status check failed: {e}; retrying)")
            continue
        if s['status'] == 'active':
            return s
        if s['status'] != 'pending':
            sys.exit(f"login ended as {s['status']}: {s['error_message']}")
    sys.exit("login was not approved in time; run login again")


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
    """Log in: browser (GitHub) by default, or --key KEY / --key - for a key you were given. Saves to config (600)."""
    url = args.url.rstrip('/')
    server = {'name': args.name, 'url': url, 'api_key': ''}
    who = ''
    if args.key:
        key = args.key
        if key == '-':
            key = sys.stdin.readline().strip()
        if not key:
            sys.exit("empty key")
        server['api_key'] = key
    else:
        s = browser_login(url, args.minutes)
        server['api_key'] = s['session_token']
        server['expires_at'] = time.time() + 30 * 86400
        who = f"GitHub @{s['github_username']} ({s['github_email']}), "
    try:
        q = call(server, "/api/me/quota")
    except urllib.error.HTTPError as e:
        sys.exit(f"key rejected by {url}: {http_error_text(e)}")
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"{url} unreachable: {e}")
    save_server(server)
    print(f"logged in to {args.name} ({url}): {who}tier {q['tier']}, "
          f"max concurrent floats {q['my_max_concurrent']}. Saved to {CONFIG} (mode 600).")
    if 'expires_at' in server:
        print(f"This login lasts 30 days (until {time.strftime('%Y-%m-%d', time.localtime(server['expires_at']))}).")
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
    print("Open this URL in a browser and approve access:")
    print(f"  {r['auth_url']}")
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
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('help')
    p = sub.add_parser('floats')
    p.add_argument('--files', action='store_true')
    p = sub.add_parser('analyze')
    p.add_argument('--question', default='')
    p.add_argument('--question-file', default='')
    p.add_argument('--top-n', type=int, default=40)
    p.add_argument('--budget', type=int, default=3077)
    p.add_argument('--commit', default='')
    p.add_argument('--execute', action='store_true')
    p = sub.add_parser('wait')
    p.add_argument('job_id')
    p.add_argument('--minutes', type=float, default=10)
    p = sub.add_parser('cancel')
    p.add_argument('job_id')
    p = sub.add_parser('login')
    p.add_argument('--key', default='')
    p.add_argument('--url', default=DEFAULT_URL)
    p.add_argument('--name', default='zephyr')
    p.add_argument('--minutes', type=float, default=10)
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
    if not servers and args.cmd != 'login':
        print("No Hypernym API key configured. Run: hypernym.py login --key YOUR_KEY [--url SERVER_URL], "
              f"or set HYPERNYM_API_KEY (and HYPERNYM_API_URL if not {DEFAULT_URL}). See SKILL.md.")
        sys.exit(2)
    commands = {'help': cmd_help, 'floats': cmd_floats, 'analyze': cmd_analyze, 'wait': cmd_wait,
                'sync': cmd_sync, 'packages': cmd_packages, 'load': cmd_load, 'statuscache': cmd_statuscache,
                'cancel': cmd_cancel, 'login': cmd_login, 'github': cmd_github}
    commands[args.cmd](args, servers)


if __name__ == '__main__':
    main()
