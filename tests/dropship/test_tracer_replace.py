#!/usr/bin/env python3
"""A second tracer replacing a pending one must not be killed when the old pairing records its cancellation."""
import json, os, sys, tempfile, threading, time
os.environ['DROPSHIP_HOME'] = tempfile.mkdtemp()
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins' / 'hypernym' / 'skills' / 'hypernym' / 'scripts'))
import dropship as d
d.save(d.PAIRING, {'code': 'old-code', 'status': 'new', 'started': time.time()})
c = d.Courier.__new__(d.Courier)
c.stop, c.pairing = threading.Event(), None
class FakeMail:
    def pair(self, code, done, wanted, until):
        d.save(d.PAIRING, {'code': 'new-code', 'status': 'new', 'started': time.time() + 1})   # a second tracer
        done('cancelled', 'replaced by a newer pairing')
        c.stop.set()
c.mail = FakeMail()
c.pairing_loop()
now = d.load(d.PAIRING, {})
assert now['code'] == 'new-code' and now['status'] == 'new', now
print('OK  regression: the newer pairing survives the old one cancelling')
