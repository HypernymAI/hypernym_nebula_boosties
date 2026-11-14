#!/usr/bin/env python3
"""Dropship finds its mailbox through the published pointer (mailbox-dropship.vercel.app), with cache and fallbacks.

    python3 tests/dropship/test_mailbox_pointer.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOME = tempfile.mkdtemp(prefix='dropship-pointer-')
os.environ['DROPSHIP_HOME'] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins' / 'hypernym' / 'skills' / 'hypernym' / 'scripts'))
import dropship as d  # noqa: E402

ANSWER = {'body': b'{"mailbox": "https://example-mailbox.proxy.runpod.net/"}', 'code': 200, 'hits': 0}


class Pointer(BaseHTTPRequestHandler):
    def do_GET(self):
        ANSWER['hits'] += 1
        self.send_response(ANSWER['code'])
        self.end_headers()
        self.wfile.write(ANSWER['body'])

    def log_message(self, *_a):
        pass


class MailboxPointerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(('127.0.0.1', 0), Pointer)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        d.POINTER = f'http://127.0.0.1:{cls.srv.server_port}/dropship/mailbox.json'

    def setUp(self):
        for f in (d.MAILBOX_CACHE, d.CONFIG):
            if f.exists():
                f.unlink()
        ANSWER.update(body=b'{"mailbox": "https://example-mailbox.proxy.runpod.net/"}', code=200, hits=0)

    def test_default_reads_the_pointer_and_caches_it(self):
        self.assertEqual(d.mailbox_url(), 'https://example-mailbox.proxy.runpod.net')
        self.assertEqual(d.mailbox_url(), 'https://example-mailbox.proxy.runpod.net')
        self.assertEqual(ANSWER['hits'], 1, 'second call served from the cache')

    def test_refresh_follows_a_moved_mailbox(self):
        d.mailbox_url()
        ANSWER['body'] = b'{"mailbox": "https://moved.proxy.runpod.net"}'
        self.assertEqual(d.mailbox_url(refresh=True), 'https://moved.proxy.runpod.net')

    def test_config_override_wins(self):
        d.save(d.CONFIG, {'mailbox': 'https://ntfy.example.org/'})
        self.assertEqual(d.mailbox_url(), 'https://ntfy.example.org')
        self.assertEqual(ANSWER['hits'], 0)

    def test_old_saved_default_means_auto(self):
        d.save(d.CONFIG, {'mailbox': 'https://ntfy.sh'})      # written by <=0.7.37 on any config save
        self.assertEqual(d.mailbox_url(), 'https://example-mailbox.proxy.runpod.net')

    def test_pointer_down_keeps_last_known(self):
        d.mailbox_url()
        ANSWER['code'], ANSWER['body'] = 500, b'oops'
        d.save(d.MAILBOX_CACHE, dict(d.load(d.MAILBOX_CACHE, {}), fetched=time.time() - 7 * 3600))   # stale
        self.assertEqual(d.mailbox_url(), 'https://example-mailbox.proxy.runpod.net')

    def test_nothing_known_falls_back_to_public_ntfy(self):
        ANSWER['code'], ANSWER['body'] = 500, b'oops'
        self.assertEqual(d.mailbox_url(), d.FALLBACK_MAILBOX)

    def test_non_https_pointer_refused(self):
        ANSWER['body'] = b'{"mailbox": "http://evil.example"}'
        self.assertEqual(d.mailbox_url(), d.FALLBACK_MAILBOX)


if __name__ == '__main__':
    unittest.main()
