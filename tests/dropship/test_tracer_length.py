#!/usr/bin/env python3
"""Tracers can live longer than 10 minutes (up to 7 days); the accepting side checks the new limit.

    python3 tests/dropship/test_tracer_length.py
"""
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ['DROPSHIP_HOME'] = tempfile.mkdtemp(prefix='dropship-tl-')
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins' / 'hypernym' / 'skills' / 'hypernym' / 'scripts'))
import dropship_tracer as t  # noqa: E402


def token(expires):
    v = {'id': 'ab' * 8, 'session': 'pub-0123456789ab', 'code': 'c' * 32, 'nonce': 'n' * 32, 'expires': int(expires)}
    return t.PREFIX + base64.urlsafe_b64encode(json.dumps(v, separators=(',', ':')).encode()).decode().rstrip('=')


class TracerLengthTests(unittest.TestCase):
    def test_two_day_tracer_accepted(self):
        self.assertEqual(t.decode(token(time.time() + 2 * 86400))['session'], 'pub-0123456789ab')

    def test_beyond_seven_days_refused(self):
        with self.assertRaises(ValueError):
            t.decode(token(time.time() + 8 * 86400))

    def test_expired_refused(self):
        with self.assertRaises(ValueError):
            t.decode(token(time.time() - 5))

    def test_describe(self):
        self.assertEqual(t.describe(600), '10 minutes')
        self.assertEqual(t.describe(3 * 3600), '3.0 hours')
        self.assertEqual(t.describe(2 * 86400), '2.0 days')


if __name__ == '__main__':
    unittest.main()
