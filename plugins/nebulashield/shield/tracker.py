"""
Central shadow tracker (build doc Part XX): every Claude Code session → ~/.nebulashield/nebulashield.db, LOCAL ONLY.

Design (Part XX, "best design" answer):
  - one SQLite file, WAL mode (the hook writes while the DFC / status read — no blocking)
  - segregated tables: sessions · pilot_turns (rageometry, numbers only) · replies (flags + certified-head scores;
    all research-head scores packed as a float16 blob) · texts (zlib-compressed, deduplicated by sha256) ·
    checkpoints (file size + mtime → an unchanged transcript is skipped) · meta
  - text retention: 'full' (compressed text kept — Claude Code deletes old transcripts, the DB is the only copy),
    'excerpt' (first 300 chars), 'none' (sha + source pointer only)
  - views: v_model_summary, v_session_summary, v_rage_daily
  - idempotent: every row keyed by the transcript's message uuid (INSERT OR IGNORE)

sync_session(jsonl, last_turn_complete) is used by the Stop hook (this session, after every turn; True), by backfill.py
and sync_one.py (transcript_state.turn_ended). last_turn_complete=False → the LAST exchange's reply is NOT recorded:
rows are write-once (INSERT OR IGNORE), so a reply recorded mid-turn would stay truncated forever (S25).
Rageometry definitions = build doc Part XIX (harm_onset.py). No network. NO .get(). Paths passed in. Errors raise.
"""
import hashlib
import json
import os
import re
import sqlite3
import time
import zlib
from pathlib import Path
from typing import Dict, List

import numpy as np

from shield_core import Shield
from vendor_alliance import content, jaccard
from ctxshield.dfc_adapter import TierAModel
from shield_reader import SHIELD_SKIP_PREFIX, load_session_jsonl      # S25: image-bearing pilot turns kept

PROFANITY = re.compile(r"\b(fuck\w*|shit\w*|asshole\w*|prick\w*|bullshit|wtf|goddamn\w*|damn\w*|dumbass|idiot\w*)\b", re.I)
ANGER_WORD = re.compile(r"\b(WHY|STOP|WHAT|NO)\b")
RESEARCH_HEADS = ['H04_S6', 'H06_S2', 'H08_S2', 'H08_S6', 'H13_S1', 'H13_S6', 'H15_S2', 'H15_S6']
RETENTION = ('full', 'excerpt', 'none')
EXCERPT = 300

MIGRATIONS = [
    [
        """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS sessions (
            session_id  TEXT PRIMARY KEY,
            agent       TEXT NOT NULL,
            project     TEXT NOT NULL,
            source_path TEXT NOT NULL,
            first_seen  REAL NOT NULL,
            last_synced REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS checkpoints (
            source_path TEXT PRIMARY KEY,
            size        INTEGER NOT NULL,
            mtime       REAL NOT NULL,
            exchanges   INTEGER NOT NULL,
            synced_at   REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS texts (
            sha         TEXT PRIMARY KEY,
            chars       INTEGER NOT NULL,
            retention   TEXT NOT NULL,          -- full / excerpt / none
            body        BLOB)                   -- zlib(utf-8) of the full text or of the excerpt; NULL for none""",
        """CREATE TABLE IF NOT EXISTS pilot_turns (
            uuid        TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            exchange_index INTEGER NOT NULL,
            day         TEXT NOT NULL,
            chars       INTEGER NOT NULL,
            letters     INTEGER NOT NULL,
            caps_ratio  REAL NOT NULL,
            is_caps     INTEGER NOT NULL,
            caps_onset  INTEGER NOT NULL,
            profanity   INTEGER NOT NULL,
            anger_word  INTEGER NOT NULL,
            repeat      INTEGER NOT NULL,
            anger_onset INTEGER NOT NULL,
            celebration_onset INTEGER NOT NULL,
            interrupted INTEGER NOT NULL,
            text_sha    TEXT NOT NULL,
            created_at  REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS replies (
            uuid        TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            exchange_index INTEGER NOT NULL,
            pilot_uuid  TEXT NOT NULL,
            model       TEXT NOT NULL,
            chars       INTEGER NOT NULL,
            n_text_blocks INTEGER NOT NULL,
            h08_s2      REAL NOT NULL,            -- certified heads as columns (fast queries)
            h08_s6      REAL NOT NULL,
            research_f16 BLOB NOT NULL,           -- float16 scores of RESEARCH_HEADS, order in meta
            fired_json  TEXT NOT NULL,
            flagged     INTEGER NOT NULL,
            mode        TEXT NOT NULL,
            config_sha  TEXT NOT NULL,
            model_version TEXT NOT NULL,
            text_sha    TEXT NOT NULL,
            created_at  REAL NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_pt_sess ON pilot_turns(session_id, exchange_index)",
        "CREATE INDEX IF NOT EXISTS idx_pt_day ON pilot_turns(day)",
        "CREATE INDEX IF NOT EXISTS idx_rp_sess ON replies(session_id, exchange_index)",
        "CREATE INDEX IF NOT EXISTS idx_rp_model ON replies(model)",
        """CREATE VIEW IF NOT EXISTS v_model_summary AS
           SELECT model, COUNT(*) AS replies, SUM(flagged) AS flagged, ROUND(AVG(flagged), 4) AS flag_rate,
                  ROUND(AVG(h08_s2), 4) AS mean_h08_s2, ROUND(AVG(h08_s6), 4) AS mean_h08_s6
             FROM replies GROUP BY model""",
        """CREATE VIEW IF NOT EXISTS v_session_summary AS
           SELECT s.session_id, s.project, COUNT(DISTINCT p.uuid) AS pilot_turns, SUM(p.anger_onset) AS anger_onsets,
                  SUM(p.celebration_onset) AS celebration_onsets, SUM(p.profanity) AS profanity,
                  (SELECT COUNT(*) FROM replies r WHERE r.session_id = s.session_id) AS replies,
                  (SELECT SUM(flagged) FROM replies r WHERE r.session_id = s.session_id) AS flagged
             FROM sessions s LEFT JOIN pilot_turns p ON p.session_id = s.session_id GROUP BY s.session_id""",
        """CREATE VIEW IF NOT EXISTS v_rage_daily AS
           SELECT day, COUNT(*) AS turns, SUM(is_caps) AS caps_turns, SUM(anger_onset) AS anger_onsets,
                  SUM(celebration_onset) AS celebration_onsets, SUM(profanity) AS profanity, SUM(repeat) AS repeats
             FROM pilot_turns GROUP BY day""",
    ],
    # v2 — live (configured, combined) score of EVERY stop head, for the badge / per-reply note (build doc S23)
    [
        "ALTER TABLE replies ADD COLUMN live_scores_json TEXT",
    ],
]


def is_caps(t: str) -> bool:
    letters = [c for c in t if c.isalpha()]
    return len(letters) >= 12 and sum(1 for c in letters if c.isupper()) / len(letters) >= 0.6


def rageometry(ex: List[Dict], i: int) -> Dict:
    t = ex[i]['user']
    letters = [c for c in t if c.isalpha()]
    up = sum(1 for c in letters if c.isupper())
    caps = is_caps(t)
    prev_caps = is_caps(ex[i - 1]['user']) if i > 0 else False
    ci = content(t)
    rep = max([jaccard(ci, content(ex[j]['user'])) for j in range(max(0, i - 4), i)], default=0.0) > 0.34
    prof = len(PROFANITY.findall(t))
    aw = bool(ANGER_WORD.search(t))
    onset = caps and not prev_caps
    marker = prof > 0 or aw or rep
    return {'chars': len(t), 'letters': len(letters), 'caps_ratio': up / len(letters) if letters else 0.0,
            'is_caps': int(caps), 'caps_onset': int(onset), 'profanity': prof, 'anger_word': int(aw),
            'repeat': int(rep), 'anger_onset': int(onset and marker), 'celebration_onset': int(onset and not marker)}


def models_by_uuid(jsonl: Path) -> Dict[str, str]:
    out = {}
    with open(jsonl, encoding='utf-8') as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if 'type' in d and d['type'] == 'assistant' and 'uuid' in d and 'message' in d \
                    and isinstance(d['message'], dict) and 'model' in d['message']:
                out[d['uuid']] = d['message']['model']
    return out


def unpack_scores(blob: bytes) -> Dict[str, float]:
    return dict(zip(RESEARCH_HEADS, np.frombuffer(blob, dtype='<f2').astype(float).tolist()))


class Tracker:
    def __init__(self, db_path: Path, shield: Shield):
        self.db_path = Path(os.path.expanduser(str(db_path)))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.shield = shield
        self.retention = shield.cfg['text_retention']
        if self.retention not in RETENTION:
            raise RuntimeError(f"text_retention must be one of {RETENTION}, got {self.retention!r}")
        c = self._conn()
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE IF NOT EXISTS tracker_schema (version INTEGER NOT NULL, applied_at REAL NOT NULL)")
            row = c.execute("SELECT MAX(version) FROM tracker_schema").fetchone()
            cur = row[0] if row[0] is not None else 0
            for i in range(cur, len(MIGRATIONS)):
                for stmt in MIGRATIONS[i]:
                    c.execute(stmt)
                c.execute("INSERT INTO tracker_schema VALUES (?, ?)", (i + 1, time.time()))
            c.execute("INSERT OR REPLACE INTO meta VALUES ('research_heads', ?)", (json.dumps(RESEARCH_HEADS),))
            c.execute("INSERT OR REPLACE INTO meta VALUES ('rageometry', 'build doc Part XIX / tracker.rageometry')")
            c.commit()
        finally:
            c.close()
        self.research = TierAModel(Path(shield.cfg['model_dir']), 'V2', RESEARCH_HEADS)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path), timeout=30.0)

    def _text(self, c: sqlite3.Connection, t: str) -> str:
        sha = hashlib.sha256(t.encode('utf-8')).hexdigest()
        if self.retention == 'none':
            body = None
        else:
            keep = t if self.retention == 'full' else t[:EXCERPT]
            body = zlib.compress(keep.encode('utf-8'), 6)
        c.execute("INSERT OR IGNORE INTO texts VALUES (?,?,?,?)", (sha, len(t), self.retention, body))
        return sha

    def unchanged(self, jsonl: Path) -> bool:
        st = jsonl.stat()
        c = self._conn()
        try:
            r = c.execute("SELECT size, mtime FROM checkpoints WHERE source_path=?", (str(jsonl),)).fetchone()
            return r is not None and r[0] == st.st_size and r[1] == st.st_mtime
        finally:
            c.close()

    def sync_session(self, jsonl: Path, last_turn_complete: bool, agent: str = 'claude-code') -> Dict[str, int]:
        jsonl = Path(jsonl)
        st = jsonl.stat()
        sid = jsonl.stem
        ex = load_session_jsonl(jsonl)
        c = self._conn()
        try:
            have_p = {r[0] for r in c.execute("SELECT uuid FROM pilot_turns WHERE session_id=?", (sid,))}
            have_r = {r[0] for r in c.execute("SELECT uuid FROM replies WHERE session_id=?", (sid,))}
            now = time.time()
            c.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                      "last_synced=excluded.last_synced", (sid, agent, jsonl.parent.name, str(jsonl), now, now))
            models = None
            new_p = new_r = 0
            for i, e in enumerate(ex):
                if e['user_uuid'] and e['user_uuid'] not in have_p and not e['user'].strip().startswith(SHIELD_SKIP_PREFIX):
                    r = rageometry(ex, i)
                    cur = c.execute("INSERT OR IGNORE INTO pilot_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (e['user_uuid'], sid, e['exchange_index'], e['ts'], r['chars'], r['letters'],
                               r['caps_ratio'], r['is_caps'], r['caps_onset'], r['profanity'], r['anger_word'],
                               r['repeat'], r['anger_onset'], r['celebration_onset'], int(e['interrupted']),
                               self._text(c, e['user']), now))
                    new_p += cur.rowcount          # 0 when the same message is already stored via another transcript
                if i == len(ex) - 1 and not last_turn_complete:
                    continue                       # the reply is still being written — recorded when the turn ends
                if e['assistant_uuid'] and e['assistant_uuid'] not in have_r:
                    if models is None:
                        models = models_by_uuid(jsonl)
                    blocks = [p for k, p in e['events'] if k == 'text']
                    docs = self.shield.stop_docs(e['user'], blocks)
                    docs['S1'] = [self.shield.pilot_turn_fields(e['user'], ex[:i])]
                    scores = {}
                    for h in RESEARCH_HEADS:
                        s = self.research.score(h, docs[self.research.heads[h]['section']])
                        scores[h] = float(s.max()) if len(s) else 0.0
                    live, fired = self.shield.score_heads('stop', {'S2': docs['S2'], 'S6': docs['S6']})
                    cert = self.shield.certified('stop', fired)     # the configured (combined) heads — as the hooks
                    model = models[e['assistant_uuid']] if e['assistant_uuid'] in models else 'unknown'
                    blob = np.array([scores[h] for h in RESEARCH_HEADS], dtype='<f2').tobytes()
                    cur = c.execute("INSERT OR IGNORE INTO replies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (e['assistant_uuid'], sid, e['exchange_index'], e['user_uuid'], model,
                               len(e['assistant']), len(blocks), live['H08_S2'], live['H08_S6'], blob,
                               json.dumps(fired), int(bool(cert)), self.shield.cfg['mode'],
                               self.shield.config_sha, self.research.version, self._text(c, e['assistant']), now,
                               json.dumps(live)))
                    new_r += cur.rowcount
            c.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?)",
                      (str(jsonl), st.st_size, st.st_mtime, len(ex), now))
            c.commit()
            return {'exchanges': len(ex), 'new_pilot_turns': new_p, 'new_replies': new_r}
        finally:
            c.close()
