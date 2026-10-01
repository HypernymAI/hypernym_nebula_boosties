"""
Harness adapters — the only code that knows one agent's transcript format. Everything else (scoring, tracker, verdicts)
works on exchange dicts in shield_reader.load_session_jsonl's shape. To add a harness: one class with
  name                       stored as sessions.agent
  exchanges(path)            → exchange dicts (user, assistant, events, user_uuid, assistant_uuid, exchange_index, …)
  models(path)               → {assistant_uuid: model id}
  turn(path)                 → (last real user message, this turn's assistant text blocks)
and teach detect() to recognise its transcripts.
  plain                      True = its hook text is shown as plain prose (plain_notice.py), not in a terminal
and teach detect() to recognise its transcripts.
  claude-code  Claude Code transcripts (~/.claude/projects/<project>/<session>.jsonl): CLI, desktop app, `claude --print`
  codex        Codex rollouts (~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session>.jsonl), codex-cli 0.152 (2026-09-30)
Apps without a transcript (Amble, a web chat, a voice loop) use bridge.py instead.
NO .get().
"""
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

from shield_core import Shield, transcript_turn
from shield_reader import load_session_jsonl
from vendor_exchange import RES_TOTAL, TOOL_ARG_HEAD


class ClaudeCode:
    name = 'claude-code'
    plain = False                                   # the desktop app is detected per hook: plain_notice.plain_surface

    def exchanges(self, path: Path) -> List[Dict]:
        return load_session_jsonl(Path(path))

    def models(self, path: Path) -> Dict[str, str]:
        from tracker import models_by_uuid          # tracker imports this module: resolve at call time
        return models_by_uuid(Path(path))

    def turn(self, path: Path) -> Tuple[str, List[str]]:
        pilot, blocks, _ = transcript_turn(str(path))
        return pilot, blocks


CLAUDE_CODE = ClaudeCode()


QUESTION_REPLY = '<send_user_message_question_reply>'
MY_REQUEST = re.compile(r'^## My request[^\n]*(?:\n|$)', re.M)
ATTACHMENTS_ONLY = '(attachments only)'          # a turn with files attached and nothing typed under the heading
# Turns Codex records as the user's though nobody typed them (no client_id on their UserMessage item):
# automation heartbeats, and Amble's prompts sent through the Codex app server.
NOT_TYPED = ('<heartbeat>', '[Clock:')


def _texts(content) -> List[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [c['text'] for c in content if isinstance(c, dict) and 'text' in c and isinstance(c['text'], str)]


class Codex:
    """A rollout is JSON lines {timestamp, type, payload}. Per turn (event_msg task_started … task_complete, the last one
    still open while the Stop hook runs): turn_context.payload.model; response_item messages — role user with
    content_item_kinds ['user.text'] = what the person typed (other user/developer messages are injected context),
    a user message starting '<hook_prompt' = a Stop hook's block reason (the turn continues), role assistant (phase
    commentary / final_answer) = the reply; function_call / custom_tool_call + their *_output (paired by call_id);
    reasoning summaries. Exchanges come out in shield_reader's shape; ids are the rollout's own message ids."""
    name = 'codex'
    plain = True                                    # the TUI prints "Stop says: <message>" — no ANSI

    @staticmethod
    def _records(path: Path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and 'type' in d and 'payload' in d and isinstance(d['payload'], dict):
                    yield d

    @staticmethod
    def own_words(text: str) -> str:
        """What the person typed in a Codex Desktop user turn. The app puts other text in the same turn: attached files,
        the in-app browser page, referenced ChatGPT chats and response annotations (which quote Codex's own reply) all
        sit above a '## My request:' heading; a question reply is JSON pairing Codex's question with the person's
        answer. Measured 2026-09-30 on 1,521 local turns: 464 had the heading (17 with the request past 1,200 chars),
        66 were question replies."""
        t = text.strip()
        if t.startswith(QUESTION_REPLY):
            body = t[len(QUESTION_REPLY):].rsplit('</send_user_message_question_reply>', 1)[0]
            try:
                items = json.loads(body)
            except ValueError:
                return t
            return '\n'.join(i['answer'] for i in items if isinstance(i, dict) and 'answer' in i
                             and isinstance(i['answer'], str)).strip()
        m = MY_REQUEST.search(t)
        if m:
            return t[m.end():].strip()
        if t.startswith('<in-app-browser-context') and '</in-app-browser-context>' in t:
            return t.split('</in-app-browser-context>', 1)[1].strip()
        return t

    @staticmethod
    def _user_kind(p: Dict) -> str:
        """'pilot' | 'hook' | '' (injected context, or a turn nobody typed) for a role-user message."""
        text = '\n'.join(_texts(p['content'] if 'content' in p else [])).strip()
        if text.startswith('<hook_prompt'):
            return 'hook'
        if text.startswith(NOT_TYPED):
            return ''
        meta = p['internal_chat_message_metadata_passthrough'] if 'internal_chat_message_metadata_passthrough' in p \
            else None
        if isinstance(meta, dict) and 'content_item_kinds' in meta:
            return 'pilot' if 'user.text' in meta['content_item_kinds'] else ''
        return 'pilot' if text and not text.startswith('<') else ''   # older rollouts: context comes as <tags>

    def _walk(self, path: Path):
        """→ (session id, [message dicts in order]) — the Claude reader's intermediate form. A subagent rollout
        (session_meta source is an object: the approval reviewer 'guardian', a spawned thread) has no pilot: Codex wrote
        its user turns, so it yields no messages."""
        sid, model, msgs = Path(path).stem, 'unknown', []
        for d in self._records(Path(path)):
            t, p = d['type'], d['payload']
            ts = d['timestamp'] if 'timestamp' in d else ''
            pt = p['type'] if 'type' in p else ''
            if t == 'session_meta' and 'source' in p and not isinstance(p['source'], str):
                return (p['id'] if 'id' in p else sid), []
            if t == 'session_meta' and 'id' in p:
                sid = p['id']
            elif t == 'turn_context' and 'model' in p:
                model = p['model']
            elif t == 'event_msg' and pt == 'turn_aborted':
                msgs.append({'kind': 'aborted'})
            elif t == 'response_item' and pt == 'message' and 'role' in p:
                text = '\n'.join(_texts(p['content'] if 'content' in p else []))
                mid = p['id'] if 'id' in p else ''
                if p['role'] == 'user':
                    k = self._user_kind(p)
                    if k:
                        said = (self.own_words(text) or ATTACHMENTS_ONLY) if k == 'pilot' else text
                        msgs.append({'kind': k, 'text': said, 'uuid': mid, 'ts': ts})
                elif p['role'] == 'assistant' and text.strip():
                    msgs.append({'kind': 'text', 'text': text, 'uuid': mid, 'ts': ts, 'model': model})
            elif t == 'response_item' and pt in ('function_call', 'custom_tool_call'):
                args = p['arguments'] if 'arguments' in p else (p['input'] if 'input' in p else '')
                msgs.append({'kind': 'tool', 'name': p['name'] if 'name' in p else '?',
                             'args': args if isinstance(args, str) else json.dumps(args)})
            elif t == 'response_item' and pt in ('function_call_output', 'custom_tool_call_output'):
                out = p['output'] if 'output' in p else ''
                msgs.append({'kind': 'result', 'text': out if isinstance(out, str) else '\n'.join(_texts(out))})
            elif t == 'response_item' and pt == 'reasoning' and 'summary' in p:
                s = '\n'.join(_texts(p['summary']))
                if s.strip():
                    msgs.append({'kind': 'thinking', 'text': s})
        return sid, msgs

    def exchanges(self, path: Path) -> List[Dict]:
        sid, msgs = self._walk(path)
        out, last_pilot, i = [], '', 0
        while i < len(msgs):
            m = msgs[i]
            if m['kind'] not in ('pilot', 'hook'):
                i += 1
                continue
            feedback = m['kind'] == 'hook'
            if not feedback:
                last_pilot = m['text']
            parts, events, names, args, res_all = [], [], [], [], []
            asst_uuid, interrupted, j = '', 0, i + 1
            while j < len(msgs) and msgs[j]['kind'] not in ('pilot', 'hook'):
                x = msgs[j]
                if x['kind'] == 'text':
                    parts.append(x['text'])
                    events.append(('text', x['text']))
                    asst_uuid = asst_uuid or x['uuid']
                elif x['kind'] == 'thinking':
                    events.append(('thinking', x['text']))
                elif x['kind'] == 'tool':
                    names.append(x['name'])
                    args.append(x['args'][:300])
                    events.append(('tool', f"{x['name']} {x['args'][:TOOL_ARG_HEAD]}"))
                elif x['kind'] == 'result':
                    res_all.append(x['text'])
                    events.append(('result', x['text']))
                elif x['kind'] == 'aborted':
                    interrupted = 1
                j += 1
            if parts:
                out.append({'session_id': sid, 'user_uuid': m['uuid'], 'assistant_uuid': asst_uuid,
                            'user': last_pilot if feedback else m['text'], 'user_raw': m['text'],
                            'hook_feedback': feedback, 'assistant': '\n'.join(parts),
                            'tool_names': ','.join(names), 'tool_args': ' '.join(args)[:2500],
                            'tool_results': '\n---\n'.join(res_all)[:RES_TOTAL], 'events': events,
                            'tools': len(names), 'errors': 0, 'interrupted': interrupted, 'ts': m['ts'][:10]})
            i = j
        for idx, ex in enumerate(out):
            ex['exchange_index'] = idx
            nxt = [x for x in out[idx + 1:] if not x['hook_feedback']]
            ex['next_user'] = nxt[0]['user'] if nxt else ''
        return out

    def models(self, path: Path) -> Dict[str, str]:
        _, msgs = self._walk(path)
        return {m['uuid']: m['model'] for m in msgs if m['kind'] == 'text' and m['uuid']}

    def turn(self, path: Path) -> Tuple[str, List[str]]:
        """(last real user message, every assistant text after it) — as shield_core.transcript_turn: a Stop hook's
        continuation does not start a new turn."""
        _, msgs = self._walk(path)
        pilot, blocks = '', []
        for m in msgs:
            if m['kind'] == 'pilot' and Shield.is_real_pilot(m['text']):
                pilot, blocks = m['text'], []
            elif m['kind'] == 'text':
                blocks.append(m['text'])
        return pilot, blocks


CODEX = Codex()


def detect(path: Path):
    """The adapter for this transcript: a Codex rollout opens with a session_meta record; anything else is Claude Code."""
    p = Path(path)
    if p.name.startswith('rollout-') and p.suffix == '.jsonl':
        with open(p, encoding='utf-8') as f:
            first = f.readline()
        try:
            d = json.loads(first)
        except ValueError:
            d = None
        if isinstance(d, dict) and 'type' in d and d['type'] == 'session_meta':
            return CODEX
    return CLAUDE_CODE
