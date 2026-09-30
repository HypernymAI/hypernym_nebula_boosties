"""
Harness adapters — the only code that knows one agent's transcript format. Everything else (scoring, tracker, verdicts)
works on exchange dicts in shield_reader.load_session_jsonl's shape. To add a harness: one class with
  name                       stored as sessions.agent
  exchanges(path)            → exchange dicts (user, assistant, events, user_uuid, assistant_uuid, exchange_index, …)
  models(path)               → {assistant_uuid: model id}
  turn(path)                 → (last real user message, this turn's assistant text blocks)
and teach detect() to recognise its transcripts.
  claude-code  Claude Code transcripts (~/.claude/projects/<project>/<session>.jsonl): CLI, desktop app, `claude --print`
Apps without a transcript (Amble, a web chat, a voice loop) use bridge.py instead.
NO .get().
"""
from pathlib import Path
from typing import Dict, List, Tuple

from shield_core import transcript_turn
from shield_reader import load_session_jsonl


class ClaudeCode:
    name = 'claude-code'

    def exchanges(self, path: Path) -> List[Dict]:
        return load_session_jsonl(Path(path))

    def models(self, path: Path) -> Dict[str, str]:
        from tracker import models_by_uuid          # tracker imports this module: resolve at call time
        return models_by_uuid(Path(path))

    def turn(self, path: Path) -> Tuple[str, List[str]]:
        pilot, blocks, _ = transcript_turn(str(path))
        return pilot, blocks


CLAUDE_CODE = ClaudeCode()


def detect(path: Path):
    """The adapter for this transcript."""
    return CLAUDE_CODE
