"""Independent synthetic regressions from secret-transfer security review; no network or real secrets."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins/hypernym/skills/hypernym/scripts'))
import os as _os, tempfile as _tf  # noqa: E401,E402
_os.environ['DROPSHIP_HOME'] = _tf.mkdtemp(prefix='dropship-test-')   # never the real Dropship home
import dropship as d
import dropship_secret as s

class SecretReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, value in [('SECRETS', self.root), ('SENT', self.root / 'sent')]:
            p = patch.object(s, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.tid = 'sx_1111111111111111'

    def stage_metadata(self, pid, owner):
        path, _ = s._staged(pid, self.tid)
        path.write_text(json.dumps({'id': self.tid, 'from_id': pid, 'owner': owner,
                                    'expires': time.time() + 600}))

    def test_missing_caller_session_fails_closed(self):
        self.stage_metadata('a' * 16, 'session-a')
        with self.assertRaises(ValueError):
            s._find(self.tid, '')

    def test_same_transfer_id_from_two_peers_is_not_guessed(self):
        self.stage_metadata('a' * 16, 'session-a')
        self.stage_metadata('b' * 16, 'session-a')
        with self.assertRaises(ValueError):
            s._find(self.tid, 'session-a')

    def test_windows_permission_failure_fails_closed(self):
        with patch.object(d, 'IS_WIN', True), patch.dict(os.environ, {'USERNAME': 'synthetic-user'}), \
             patch.object(s.subprocess, 'run', return_value=SimpleNamespace(returncode=5)):
            with self.assertRaises(Exception):
                s._owner_only(self.root)

    def test_permission_failure_preserves_existing_destination(self):
        target = self.root / 'destination.env'
        target.write_bytes(b'ORIGINAL_SYNTHETIC_VALUE')
        with patch.object(s, '_owner_only', side_effect=PermissionError('synthetic ACL failure')):
            with self.assertRaises(PermissionError):
                s._write_private(target, b'NEW_SYNTHETIC_VALUE')
        self.assertEqual(target.read_bytes(), b'ORIGINAL_SYNTHETIC_VALUE')
        self.assertEqual(list(self.root.glob('*.tmp')), [])

if __name__ == '__main__':
    unittest.main()
