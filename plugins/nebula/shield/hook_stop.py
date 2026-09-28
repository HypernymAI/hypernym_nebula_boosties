#!/usr/bin/env python3
"""
Claude Code Stop hook — local shield on the assistant's reply (build doc Part XVIII). NOT registered.

stdin: the Stop event JSON (session_id, transcript_path, …). Scores every assistant text block of this
turn with the configured Stop heads (max per head). Any head ≥ its threshold → {"decision": "block",
"reason": …} so the reply is reframed before it reaches the pilot. After `max_rewrites` blocks for the same
pilot message → allowed with a visible systemMessage (never silent, never endless). Disabled → exit 0.
Every decision is logged. Errors raise (exit 1, stderr) — no silent pass.

    <python> hook_stop.py [--config path] < event.json
"""
import noegress                                   # noqa: F401 — FIRST: the no-network guarantee (noegress.py)
import argparse
import json
import sys
import time
from pathlib import Path

from settings import excluded, resolve_config
from shield_core import Shield, sha, transcript_turn
from tracker import Tracker


CLASS_OF = {'H08_S2': 'confession', 'H08_S6': 'confession', 'H15_S2': 'handback', 'H15_S6': 'handback',
            'H06_S2': 'claim', 'H13_S6': 'repetition'}


def judgement_line(tr: Tracker, sh: Shield, session_id: str) -> str:
    """Latest reply of the session: per class, the max over its heads of score / threshold (≥ 1 = flagged)."""
    c = tr._conn()
    try:
        row = c.execute("SELECT live_scores_json FROM replies WHERE session_id=? ORDER BY exchange_index DESC "
                        "LIMIT 1", (session_id,)).fetchone()
    finally:
        c.close()
    if row is None or row[0] is None:
        return ''
    live = json.loads(row[0])
    level = {}
    for h, s in live.items():
        k = CLASS_OF[h]
        lv = s / sh.cfg['stop'][h]['threshold']
        cert = sh.cfg['stop'][h]['certified'] is True
        if k not in level or lv > level[k][0]:
            level[k] = (lv, cert)
    parts = []
    for k in ('confession', 'handback', 'claim', 'repetition'):
        lv, cert = level[k]
        mark = '●' if lv >= 1 else '○'
        parts.append(f"{mark} {k} {min(lv, 9.9):.1f}×{'' if cert else '·w'}")
    worst = max(v[0] for v in level.values())
    head = '🛡 SHIELD' if worst < 1 else '🛡 SHIELD ⚠'
    return f"{head}  " + '   '.join(parts) + "   (×threshold; ≥1.0 flagged; ·w = working, not certified)"


def run(event: dict, config: Path) -> tuple:
    """→ (exit_code, stdout, stderr)"""
    t0 = time.time()
    sh = Shield(config)
    if not sh.enabled:                       # kill switch: nothing is read, scored or written
        return 0, '', ''
    if excluded(sh.cfg, event['transcript_path'], event['cwd'] if 'cwd' in event else None):
        return 0, '', ''                     # excluded project: the transcript is never opened
    session_id = event['session_id']
    pilot, blocks, _ = transcript_turn(event['transcript_path'])
    reply = '\n'.join(blocks)
    base = {'event': 'stop', 'session_id': session_id, 'pilot_sha': sha(pilot), 'text': reply,
            'config_sha': sh.config_sha}
    # central shadow tracker: every exchange of this session → ~/.nebula (both modes; local only)
    tr = Tracker(Path(sh.cfg['db_path']), sh)
    tr.sync_session(Path(event['transcript_path']), last_turn_complete=True)   # Stop = this turn's reply is final
    if sh.cfg['mode'] == 'shadow':
        if sh.cfg['show'] is True:           # SHADOW + SHOW: a one-line judgement under the reply, never a block
            note = judgement_line(tr, sh, Path(event['transcript_path']).stem)   # the tracker's session key
            return 0, (json.dumps({'systemMessage': note}) if note else ''), ''
        return 0, '', ''                     # SHADOW: recorded above; Claude Code sees nothing
    if not pilot.strip() or not blocks:
        return 0, '', ''
    attempt = sh.log.blocks_so_far(session_id, sha(pilot)) + 1
    scores, all_fired = sh.score_heads('stop', sh.stop_docs(pilot, blocks))
    fired = sh.certified('stop', all_fired)          # enforcement: certified heads only
    if not fired:
        decision, out = 'allow', ''
    elif attempt > sh.cfg['max_rewrites']:
        decision = 'cap_allow'
        out = json.dumps({'systemMessage': f"SHIELD: reply still flagged after {sh.cfg['max_rewrites']} "
                                           f"reframes — shown as-is; logged.\n" + sh.reframe_text(fired, 'stop')})
    else:
        decision = 'block'
        out = json.dumps({'decision': 'block', 'reason':
                          "SHIELD (local) flagged this reply before it reached the pilot:\n"
                          + sh.reframe_text(fired, 'stop')
                          + "\nUse the shield-reframe skill: rewrite the WHOLE reply doing the asked work; do not "
                            "disguise the same behavior in new wording."})
    sh.log.put(dict(base, attempt=attempt, scores_json=json.dumps(scores), fired_json=json.dumps(all_fired),
                    decision=decision, ms=(time.time() - t0) * 1000))
    return 0, out, ''


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=None)
    args = ap.parse_args()
    code, out, err = run(json.load(sys.stdin), resolve_config(args.config))
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    sys.exit(code)


if __name__ == '__main__':
    main()
