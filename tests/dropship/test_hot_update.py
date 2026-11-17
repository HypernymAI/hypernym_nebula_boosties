#!/usr/bin/env python3
"""hot-update brings every older cached plugin copy (Claude Code + Codex) onto the newest scripts, no restart.

    python3 tests/dropship/test_hot_update.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HOME = tempfile.mkdtemp(prefix='dropship-hot-')
CACHES = Path(tempfile.mkdtemp(prefix='caches-'))
os.environ['DROPSHIP_HOME'] = HOME
os.environ['DROPSHIP_PLUGIN_CACHES'] = str(CACHES)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins' / 'hypernym' / 'skills' / 'hypernym' / 'scripts'))
import dropship as d  # noqa: E402


def make(version, code):
    root = CACHES / 'hypernym-nebula-pro' / 'hypernym' / version
    (root / 'skills' / 'hypernym' / 'scripts').mkdir(parents=True, exist_ok=True)
    (root / 'skills' / 'dropship').mkdir(parents=True, exist_ok=True)
    (root / '.claude-plugin').mkdir(exist_ok=True)
    (root / '.claude-plugin' / 'plugin.json').write_text(json.dumps({'version': version}))
    (root / 'skills' / 'hypernym' / 'scripts' / 'dropship.py').write_text(code)
    (root / 'skills' / 'dropship' / 'SKILL.md').write_text('skill ' + code)
    return root


class HotUpdateTests(unittest.TestCase):
    def test_older_copies_get_the_newest_scripts(self):
        old, mid, new = make('0.7.9', 'old'), make('0.7.36', 'mid'), make('0.7.45', 'NEW')
        (new / 'skills' / 'hypernym' / 'scripts' / 'dropship_tracer.py').write_text('tracer')
        d.running = lambda: False
        d.config = lambda: {'enabled': False}
        out = d.hot_update(managers=False)
        self.assertIn('newest 0.7.45', out)
        for c in (old, mid):
            self.assertEqual((c / 'skills' / 'hypernym' / 'scripts' / 'dropship.py').read_text(), 'NEW')
            self.assertEqual((c / 'skills' / 'hypernym' / 'scripts' / 'dropship_tracer.py').read_text(), 'tracer')
            self.assertEqual((c / 'skills' / 'dropship' / 'SKILL.md').read_text(), 'skill NEW')
        self.assertIn('brought 2 older cached copies', out)

    def test_copy_deleted_by_a_plugin_manager_is_restored(self):
        old, new = make('0.7.47', 'old'), make('0.7.48', 'NEW')
        d.running = lambda: False
        d.config = lambda: {'enabled': False}
        import shutil
        real_which = shutil.which
        d.shutil.which = lambda name: None                  # no real claude
        d.codex_bin = lambda: ''                            # no real codex
        orig = d.plugin_caches
        calls = []

        def caches():                                       # the manager step runs between the first two listings:
            if len(calls) == 1:                             # simulate Codex deleting the old version folder
                shutil.rmtree(old, ignore_errors=True)
            calls.append(1)
            return orig()
        d.plugin_caches = caches
        try:
            out = d.hot_update(managers=True)
        finally:
            d.plugin_caches, d.shutil.which = orig, real_which
        try:
            self.assertIn('restored 1 cached copies', out)
            self.assertEqual((old / 'skills' / 'hypernym' / 'scripts' / 'dropship.py').read_text(), 'NEW')
        finally:
            shutil.rmtree(old, ignore_errors=True)
            shutil.rmtree(new, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
