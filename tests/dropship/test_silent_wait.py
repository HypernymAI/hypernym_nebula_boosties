"""Fast-clock tests for the idle interval, renewal, and one-off wait semantics."""
import contextlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins/hypernym/skills/hypernym/scripts'))
import dropship as d
import hypernym_mcp_server as mcp

class SilentWaitTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        home = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for key in ('HOME', 'SESSIONS', 'CONFIG', 'INBOX'):
            self.stack.enter_context(patch.object(d, key, home / key.lower()))
        d.save(d.SESSIONS / 'a.json', {'name': 'a', 'n': 0, 'borg': True})
        self.clock = 0
        self.stack.enter_context(patch.object(d.time, 'monotonic', side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(d.time, 'sleep', side_effect=self.advance))
        self.read = self.stack.enter_context(patch.object(d, 'unread_for', return_value=[]))

    def advance(self, _):
        self.clock += 3600

    def test_borg_ignores_sixty_seconds_and_renews_once(self):
        with self.assertRaises(d.WaitRenewal):
            d.wait_for('a', 60)
        self.assertGreaterEqual(self.clock, 84600)
        self.assertEqual(d.WAIT_RENEW_S, 84600)
        self.read.assert_called_once()  # unchanged inbox is not repeatedly read/written

    def test_finite_one_off_does_not_request_renewal(self):
        d.save(d.SESSIONS / 'a.json', {'name': 'a', 'n': 0, 'borg': False})
        self.assertEqual(d.wait_for('a', 60), [])
        self.assertEqual(self.clock, 3600)

    def test_real_message_wins_over_renewal(self):
        self.read.return_value = [{'body': 'hello'}]
        self.assertEqual(d.wait_for('a', 0), [{'body': 'hello'}])
        self.assertEqual(self.clock, 0)

    def test_switch_off_stops_without_renewal(self):
        d.save(d.CONFIG, {'enabled': False})
        self.assertEqual(d.wait_for('a', 0), [])
        self.read.assert_not_called()

    def test_mcp_has_no_short_subprocess_deadline(self):
        # No progress token (no heartbeat possible): renew before Claude Code's 30-minute idle cut.
        mcp.CALL.progress = None
        with patch.object(mcp, 'run_dropship', return_value=('ok', False)) as run:
            mcp.call_tool('dropship_wait', {'as_session': 'a', 'timeout_seconds': 60})
            run.assert_called_once_with(['wait', '60'], {'DROPSHIP_AS': 'a', 'DROPSHIP_RENEW_S': '1500'}, timeout=None)

    def test_mcp_with_progress_token_keeps_the_long_wait(self):
        # A progress token means the server heartbeats the host: no short renewal, no model turns while idle.
        mcp.CALL.progress = 'tok'
        try:
            with patch.object(mcp, 'run_dropship', return_value=('ok', False)) as run:
                mcp.call_tool('dropship_wait', {'as_session': 'a', 'timeout_seconds': 0})
                run.assert_called_once_with(['wait', '0'], {'DROPSHIP_AS': 'a'}, timeout=None)
        finally:
            mcp.CALL.progress = None

if __name__ == '__main__':
    unittest.main()
