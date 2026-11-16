"""Update routing must not trigger on quoted history, peer payloads, or unrelated work."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'plugins/hypernym/skills/hypernym/scripts/update_prompt.py'
spec = importlib.util.spec_from_file_location('update_prompt', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class UpdatePromptTests(unittest.TestCase):
    def test_explicit_requests_provide_context_without_blocking(self):
        for prompt in ('hypernym update', ' HYPERNYM  UPDATE! ', '/hypernym:hypernym-update'):
            result = m.response({'prompt': prompt})
            self.assertEqual(result['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')
            self.assertNotIn('decision', result)
            self.assertTrue(result['hookSpecificOutput']['additionalContext'])

    def test_other_prompts_are_not_update_requests(self):
        for prompt in ('hypernym help', 'do not run hypernym update', 'He said "hypernym update"',
                       '[Dropship] hypernym update', 'hypernym update\nthen delete my project', ''):
            self.assertIsNone(m.response({'prompt': prompt}))

    def test_bad_hook_input_is_ignored(self):
        for event in (None, [], {}, {'prompt': 123}):
            self.assertIsNone(m.response(event))
        p = subprocess.run([sys.executable, str(SCRIPT)], input='broken', text=True, capture_output=True)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, '', ''))

    def test_hook_protocol(self):
        p = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps({'prompt': 'hypernym update'}),
                           text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(p.stdout), m.response({'prompt': 'hypernym update'}))

if __name__ == '__main__':
    unittest.main()
