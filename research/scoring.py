#!/usr/bin/env python3
"""scoring.py: shared §2C score aggregation for the Solana and EVM scanners (pure functions, unit-tested).

Why this module exists (2026-09-25, see SCORING_NOTES.md):
  Before today no coin could reach the 0.75 bar, for three separate reasons:
  (1) S4 social was hard-coded UNKNOWN = 0 (so COMPOSITE max was 0.85, or 0.65 with S3 unknown);
  (2) C2, C3 and C5 confidences were CONSTANTS (0.65/0.65/0.60), so the prompt's DECISION CONFIDENCE (the minimum)
      could never exceed 0.60 whatever the evidence;
  (3) any missing factor sat at confidence 0.5 inside that minimum.
  Fixes, all honest (no points added, bar unchanged):
  * S4 now comes from real keyless sources (social_data.py).
  * C1/C2/C3/C5 confidences are derived from evidence: two independent price views (DexScreener/Jupiter % changes vs
    our own hourly candles) that AGREE raise confidence; disagreement lowers it; C5 is a deterministic function of
    S1, S2 and C2, so its confidence can't exceed theirs.
  * The prompt's rules stay the gate: COMPOSITE uses the fixed prompt weights with UNKNOWN = 0, and
    DECISION CONFIDENCE = min(S1-S4, C1, C2, C3, C5) (prompt §2C.5). The prompt says weights may change ONLY to make
    buying stricter, and renormalizing over known factors makes buying LOOSER, so the scanner can't adopt it on its own.
  * The "missing = missing, renormalize + explicit uncertainty haircut" method is computed alongside ("adjusted"
    numbers, shown on every card as a SHADOW). It becomes the gate only if scan_config "scoring_mode" = "renormalized",
    which should be set only after the owner approves the matching §2C wording (draft in SCORING_NOTES.md).
  None of these probabilities are calibrated until 20+ closed, logged trades; after that the prompt's §2C.9 audit
  compares outcomes with the bar and may raise it.
"""
from __future__ import annotations

import math

WEIGHTS = {"S1": 0.30, "S2": 0.25, "S3": 0.20, "S4": 0.15}
E_WEIGHT = 0.10
HAIRCUT_PER_MISSING_WEIGHT = 0.5   # adjusted = renormalized x (1 - 0.5 x missing weight): S4 missing -> x0.925,
                                   # S3+S4 missing -> x0.825. Missing data always costs something, never blocks alone.
C2_VETO = ("BLOW-OFF", "DISTRIBUTION", "COLLAPSE")


def candle_changes(candles: list) -> dict | None:
    """h1/h6/h24 % changes from our own hourly candles [(ts,o,h,l,c)] (independent of the pair source's numbers)."""
    if len(candles) < 25:
        return None
    c = [x[4] for x in candles]
    ch = lambda n: (c[-1] / c[-1 - n] - 1) * 100 if c[-1 - n] > 0 else 0.0  # noqa: E731
    out = {"h1": ch(1), "h6": ch(6), "h24": ch(24)}
    if len(c) >= 73:
        out["h72"] = ch(72)
    return out


def price_action_confidence(classify, pc: dict, candles: list, c2: str, c3: str) -> tuple[float, float, str]:
    """C2/C3 confidence. One view only (pair % changes) = 0.65 (the old constant). A second, independent view (our
    candles) that yields the SAME class = 0.80; a different class = 0.55 (conflicting evidence)."""
    cc = candle_changes(candles)
    if not cc:
        return 0.65, 0.65, "one price view only (pair % changes)"
    c2b, c3b, _ = classify(cc)
    return (0.80 if c2b == c2 else 0.55), (0.80 if c3b == c3 else 0.55), f"candle view C2 {c2b} / C3 {c3b}"


def trend_confidence(stage: str, base_conf: float, candles: list) -> tuple[float, str]:
    """C1 confidence: the heuristic stage (0.55-0.60) is raised to 0.75 only when 72 h of candles agree with its
    direction (EMERGING/EXPANDING up, DECLINING/CROWDED flat-or-down), and cut to 0.45 when they contradict it."""
    cc = candle_changes(candles)
    if not cc or "h72" not in cc:
        return base_conf, "no 72 h candle check"
    up = cc["h72"] > 0
    agree = (stage in ("EARLY", "EMERGING", "EXPANDING") and up) or (stage in ("DECLINING", "CROWDED", "SATURATED") and not up)
    return (0.75 if agree else 0.45), f"72 h change {cc['h72']:.1f}% {'agrees' if agree else 'contradicts'}"


def c5_confidence(confs: dict) -> float:
    """C5 = BUY/WATCH is computed from S1, S2 and C2, so it can't be more certain than they are."""
    return round(min(confs["S1"], confs["S2"], confs["C2"]), 2)


def aggregate(levels: dict, confs: dict, known: dict, e: float, bar: float = 0.75, mode: str = "prompt") -> dict:
    """levels S1-S4 (0-4), confs for S1-S4/C1/C2/C3/C5, known[S3]/known[S4] = was the factor actually measured.
    Returns the prompt numbers (the gate by default) and the adjusted shadow numbers, plus which one gates."""
    comp = sum(WEIGHTS[k] * levels[k] / 4 for k in WEIGHTS) + E_WEIGHT * e
    dconf = min(confs.values())
    setter = min(confs, key=confs.get)
    missing = [k for k in ("S3", "S4") if not known.get(k)]
    miss_w = sum(WEIGHTS[k] for k in missing)
    known_sum = sum(WEIGHTS[k] * levels[k] / 4 for k in WEIGHTS if k not in missing) + E_WEIGHT * e
    renorm = known_sum / (1.0 - miss_w) if miss_w < 1 else 0.0
    hair = 1.0 - HAIRCUT_PER_MISSING_WEIGHT * miss_w
    known_confs = {k: v for k, v in confs.items() if k not in missing}
    adj_comp = renorm * hair
    adj_conf = min(known_confs.values()) * hair if known_confs else 0.0
    use_adj = mode == "renormalized"
    return {"composite": round(comp, 4), "decision_confidence": round(dconf, 3), "confidence_set_by": setter,
            "missing_factors": missing, "missing_weight": round(miss_w, 3), "haircut": round(hair, 3),
            "composite_adjusted": round(adj_comp, 4), "confidence_adjusted": round(adj_conf, 3),
            "gate_mode": "renormalized (owner-approved setting)" if use_adj else "prompt (fixed weights, UNKNOWN = 0, min confidence)",
            "gate_composite": round(adj_comp if use_adj else comp, 4),
            "gate_confidence": round(adj_conf if use_adj else dconf, 3),
            "max_composite_if_unknown_stay_0": round(sum(WEIGHTS[k] for k in WEIGHTS if k not in missing) + E_WEIGHT, 3)}


def selftest() -> list[tuple[str, bool, str]]:
    rows = []
    lv = {"S1": 4, "S2": 3, "S3": 0, "S4": 3}
    cf = {"S1": 0.9, "S2": 0.8, "S3": 0.5, "S4": 0.8, "C1": 0.75, "C2": 0.8, "C3": 0.8, "C5": 0.8}
    a = aggregate(lv, cf, {"S3": False, "S4": True}, 0.85)
    rows.append(("prompt composite = fixed weights, unknown S3 = 0", abs(a["composite"] - (0.30 + 0.1875 + 0 + 0.1125 + 0.085)) < 1e-9, a["composite"]))
    rows.append(("prompt confidence = min (unknown S3 at 0.5 blocks)", a["decision_confidence"] == 0.5 and a["confidence_set_by"] == "S3", a["decision_confidence"]))
    rows.append(("adjusted composite: renormalized over known x (1 - 0.5 x 0.20)", abs(a["composite_adjusted"] - (0.685 / 0.8) * 0.9) < 1e-3, a["composite_adjusted"]))
    rows.append(("adjusted confidence: min over known x haircut (0.75 x 0.9)", abs(a["confidence_adjusted"] - 0.675) < 1e-3, a["confidence_adjusted"]))
    rows.append(("default gate is the prompt rule", a["gate_composite"] == a["composite"] and a["gate_confidence"] == a["decision_confidence"], a["gate_mode"]))
    weak = aggregate({"S1": 2, "S2": 2, "S3": 0, "S4": 0}, cf, {"S3": False, "S4": False}, 0.5)
    rows.append(("missing data never lifts a mediocre coin to 0.75 (adjusted)", weak["composite_adjusted"] < 0.75, weak["composite_adjusted"]))
    full = aggregate({"S1": 3, "S2": 3, "S3": 1, "S4": 2}, cf, {"S3": True, "S4": True}, 0.5)
    rows.append(("no missing factors -> adjusted == prompt", abs(full["composite_adjusted"] - full["composite"]) < 1e-9, full["composite"]))
    rows.append(("haircut: missing data always costs (adjusted < renormalized)", a["haircut"] < 1.0, a["haircut"]))
    return rows


if __name__ == "__main__":
    import sys
    if any(x in sys.argv for x in ("-h", "--help")):
        print(__doc__)
        raise SystemExit(0)
    for n, ok, d in selftest():
        print("PASS" if ok else "FAIL", n, "::", d)
