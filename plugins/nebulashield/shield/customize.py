#!/usr/bin/env python3
"""
nebulashield look-and-feel customization (writes ~/.nebulashield/settings.json; validated; updates never touch it).

    customize.py wings            show how to choose, the current wings, and where the contact sheet is
    customize.py wings <N>        use contact-sheet wings #N (0–159) beside HYPERNYM.ai
    customize.py icons on|off     the badge icons (shield, wings) — off = the plain 🛡 backup
NO .get(). Imports at top.
"""
import noegress                                   # noqa: F401 — FIRST: the no-network guarantee (noegress.py)
import json
import sys

from brand import wings_icon
from settings import PLUGIN_ROOT, USER_DIR, USER_SETTINGS, _validate_user, resolve_config, user_settings, wings_count

SHEET = PLUGIN_ROOT / 'shield' / 'brand_assets' / 'wings_contact_sheet.png'


def save(u: dict) -> None:
    _validate_user(u)
    USER_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USER_SETTINGS.with_suffix('.tmp')
    tmp.write_text(json.dumps(u, indent=1, sort_keys=True) + '\n')
    tmp.replace(USER_SETTINGS)


def glyph(n: int) -> str:
    return ''.join(ch for ch, _ in wings_icon(n)[0])


def main() -> None:
    a = sys.argv[1:]
    if a == ['wings']:
        cur = json.loads(resolve_config(None).read_text())['brand']['wings']
        print(f"""Wings beside HYPERNYM.ai — current: #{cur}  {glyph(cur)}

How to choose:
  1. The contact sheet shows all {wings_count()} candidates, numbered, large and at badge size beside HYPERNYM.ai:
       {SHEET}
     (/nebulashield:shield-wings opens it for you.)
  2. Pick a number, then run   /nebulashield:shield-wings <number>     e.g. /nebulashield:shield-wings 91
  3. The badge updates within a second. Change your mind any time; nothing else is affected.
Even numbers: the two wings touch (3 characters). Odd numbers: a one-dot gap between them (4 characters).
Turn all badge icons off/on:  /nebulashield:shield-wings off   ·   /nebulashield:shield-wings on""")
    elif len(a) == 2 and a[0] == 'wings' and a[1].isdigit():
        n = int(a[1])
        wings_icon(n)                                       # raises with the valid range if out of bounds
        u = user_settings()
        u['brand'] = {**(u['brand'] if 'brand' in u else {}), 'wings': n}
        save(u)
        print(f"wings → #{n}  {glyph(n)}   (saved in {USER_SETTINGS})")
    elif len(a) == 2 and a[0] == 'icons' and a[1] in ('on', 'off'):
        u = user_settings()
        u['brand'] = {**(u['brand'] if 'brand' in u else {}), 'icons': a[1] == 'on'}
        save(u)
        print(f"badge icons {a[1]}")
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
