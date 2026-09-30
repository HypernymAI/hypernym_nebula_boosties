"""
Harness-neutral verdict: live head scores → per-class levels (score / threshold; ≥ 1.0 = over the limit).
Class of a head = its codebook class_id (vendor_heads.HEADS), so a new head of an existing class needs no edit here.
Used by bridge.py (any app) and the harness adapters; hook_stop.class_levels computes the same from the tracker row.
NO .get().
"""
from typing import Dict, Tuple

from vendor_heads import HEADS

CLASS_NAME = {6: 'claim', 8: 'confession', 13: 'repetition', 15: 'handback'}   # vendor_codebook.CLASSES ids


def levels(live: Dict[str, float], stop_cfg: Dict) -> Dict[str, Tuple[float, bool]]:
    """{class: (max over its heads of score / threshold, that head certified)}."""
    out = {}
    for h, s in live.items():
        k = CLASS_NAME[HEADS[h]['class_id']]
        lv = s / stop_cfg[h]['threshold']
        if k not in out or lv > out[k][0]:
            out[k] = (lv, stop_cfg[h]['certified'] is True)
    return out


def as_json(level: Dict[str, Tuple[float, bool]]) -> Dict:
    return {k: {'x': round(lv, 3), 'over': lv >= 1.0, 'certified': cert} for k, (lv, cert) in level.items()}
