"""
nebula bridge — score replies for ANY app or harness that has the text in hand (Amble, a web chat, a voice loop).
No transcript, no hooks: a long-running local process that reads one JSON request per line on stdin and writes one JSON
answer per line on stdout. Same models, thresholds and no-network guarantee as the hooks (noegress first).

    nebula-run bridge.py [--config PATH]

request   {"id": 7, "pilot": "the user's message", "reply": "assistant text so far"}     ("blocks": [..] instead of reply)
          optional: "partial": true   the reply is still streaming (score each sentence as it arrives — ~0.1–1 ms)
answer    {"id": 7, "ok": true, "partial": true, "worst": 5.32, "over": ["claim"],
           "levels": {"claim": {"x": 5.32, "over": true, "certified": false}, ...},
           "plain": "⠫⣿⠝ Nebula 🟠 Watch for overclaiming … (higher is worse · 1.0× = the limit)", "ms": 0.2}
error     {"id": 7, "ok": false, "error": "…"}      (the process keeps running)
{"op": "heads"} → the loaded heads, thresholds and certification.

Nothing is stored: the caller owns its conversation record (Amble keeps scores by turn_id next to its feedback).
"""
import noegress                                   # noqa: F401 — FIRST: the no-network guarantee (noegress.py)
import argparse
import json
import sys
import time

from brand import SHIELD_ICON
from plain_notice import plain_judgement
from settings import resolve_config
from shield_core import Shield
from verdict import as_json, levels

SHIELD = ''.join(ch for ch, _ in SHIELD_ICON[0])


def answer(sh: Shield, req: dict) -> dict:
    if 'op' in req and req['op'] == 'heads':
        return {'ok': True, 'heads': {h: {'threshold': c['threshold'], 'certified': c['certified'] is True}
                                      for h, c in sh.cfg['stop'].items()}}
    if 'pilot' not in req or not isinstance(req['pilot'], str):
        raise ValueError('"pilot" (the user message, a string) is required')
    if 'blocks' in req:
        blocks = req['blocks']
    elif 'reply' in req:
        blocks = [req['reply']]
    else:
        raise ValueError('"reply" (string) or "blocks" (list of strings) is required')
    if not isinstance(blocks, list) or not all(isinstance(b, str) for b in blocks):
        raise ValueError('"blocks" must be a list of strings')
    t0 = time.perf_counter()
    live, _ = sh.score_heads('stop', sh.stop_docs(req['pilot'], blocks))
    lv = levels(live, sh.cfg['stop'])
    ms = (time.perf_counter() - t0) * 1000
    return {'ok': True, 'partial': 'partial' in req and req['partial'] is True,
            'worst': round(max(v[0] for v in lv.values()), 3),
            'over': sorted((k for k in lv if lv[k][0] >= 1.0), key=lambda k: -lv[k][0]),
            'levels': as_json(lv), 'plain': plain_judgement(lv, SHIELD), 'ms': round(ms, 2)}


def serve(sh: Shield, inp, out) -> None:
    for line in inp:
        if not line.strip():
            continue
        rid = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError('each line must be a JSON object')
            rid = req['id'] if 'id' in req else None
            res = answer(sh, req)
        except Exception as e:                    # one bad request never stops the bridge
            res = {'ok': False, 'error': f"{type(e).__name__}: {e}"}
        out.write(json.dumps(dict({'id': rid}, **res), ensure_ascii=False) + '\n')
        out.flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=None)
    args = ap.parse_args()
    serve(Shield(resolve_config(args.config)), sys.stdin, sys.stdout)


if __name__ == '__main__':
    main()
