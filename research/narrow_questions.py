#!/usr/bin/env python3
"""narrow_questions.py: SHADOW-ONLY "narrow question" probability for scanner candidates (2026-09-25).

Idea (from the 'System One' decision-model write-up the owner shared): instead of one vague judgment, ask ~20 narrow,
mechanically answerable yes/no or pick-one questions whose answers come straight from data the scanner already
collects, then turn the answers into a probability with a logistic model whose weights are FITTED on labeled
outcomes (shadow_tracker.py resolutions).

Rules (hard):
  * SHADOW ONLY. rec["narrow"] is informational. It never changes PASS/WATCH/REJECT, the 0.75 bar, sorting, or any
    gate. The prompt's rules stay the gate (see SCORING_NOTES.md for draft wording if the owner ever wants it as a gate).
  * Unknown answers contribute 0 (neutral), never a guess.
  * Until a fit on >= 50 resolved labels exists AND is saved on purpose (fit --save), p comes from hand-set neutral
    priors (model_version "narrow-prior-v1"): tiny weights, sign from the prompt's own logic, uncalibrated.
  * RSI(14) / EMA(12/26) features use CLOSED candles only (the still-forming candle is dropped).

Commands
  python3 narrow_questions.py explain                # list questions + current weights
  python3 narrow_questions.py fit [--save] [--l2 1.0] [--test-frac 0.3]
        fits on resolved shadow entries (data/shadow_log.jsonl). Refuses (prints 'too few labels') below 50 labels.
  python3 narrow_questions.py selftest               # offline tests
Pure stdlib; no network; nothing here trades.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths as _paths  # noqa: E402
DATA_DIR = _paths.DATA_DIR
MODEL_PATH = os.environ.get("NARROW_MODEL_PATH", os.path.join(DATA_DIR, "narrow_model.json"))
MIN_LABELS = 50
PRIOR_VERSION = "narrow-prior-v1"

# ---------------------------------------------------------------- TA on CLOSED candles (shared with rsi_ema_backtest.py)


def closed_only(candles: list, interval_s: int, now: float | None = None) -> list:
    """Drop the last candle if it is still forming (open_ts + interval > now). candles = [(ts,o,h,l,c), ...] ascending."""
    now = time.time() if now is None else now
    if candles and candles[-1][0] + interval_s > now:
        return candles[:-1]
    return list(candles)


def ema(values: list[float], n: int) -> list[float | None]:
    """Standard EMA (alpha = 2/(n+1)), seeded with the SMA of the first n values; None before that."""
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi(values: list[float], n: int = 14) -> list[float | None]:
    """Wilder RSI(n); None until n changes are available."""
    out: list[float | None] = [None] * len(values)
    if len(values) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n

    def val(ag, al):
        if al == 0:
            return 100.0 if ag > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + ag / al)
    out[n] = val(ag, al)
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = val(ag, al)
    return out


def indicators(closes: list[float]) -> tuple[list, list, list]:
    return ema(closes, 12), ema(closes, 26), rsi(closes, 14)


def signal_a(e12, e26, r, i) -> bool:
    """'Confluence' entry A: EMA12 crosses ABOVE EMA26 on closed candle i AND 50 <= RSI(14) <= 70 (momentum confirmed,
    not overbought). Own implementation of the generic RSI+EMA idea; the 'Cabbage' repo was NOT downloaded or read."""
    if i < 1 or None in (e12[i], e26[i], e12[i - 1], e26[i - 1], r[i]):
        return False
    return e12[i] > e26[i] and e12[i - 1] <= e26[i - 1] and 50.0 <= r[i] <= 70.0


def signal_b(e12, e26, r, i) -> bool:
    """Variant B: RSI(14) crosses UP through 30 (oversold bounce) while EMA12 > EMA26 (uptrend filter)."""
    if i < 1 or None in (e12[i], e26[i], r[i], r[i - 1]):
        return False
    return r[i - 1] < 30.0 <= r[i] and e12[i] > e26[i]


def exit_signal(e12, e26, i) -> bool:
    """Strategy exit: EMA12 crosses BELOW EMA26 on closed candle i."""
    if i < 1 or None in (e12[i], e26[i], e12[i - 1], e26[i - 1]):
        return False
    return e12[i] < e26[i] and e12[i - 1] >= e26[i - 1]


def ta_state(candles: list, interval_s: int = 3600, now: float | None = None, lookback: int = 3) -> dict | None:
    """Compact RSI/EMA state from CLOSED candles, for rec['ta'] and the narrow questions. None if < 40 closed candles."""
    cs = closed_only(candles or [], interval_s, now)
    if len(cs) < 40:
        return None
    closes = [float(c[4]) for c in cs]
    e12, e26, r = indicators(closes)
    i = len(closes) - 1
    conf = any(signal_a(e12, e26, r, j) for j in range(max(1, i - lookback + 1), i + 1))
    return {"interval_s": interval_s, "closed_candles": len(cs), "last_closed_ts": int(cs[-1][0]),
            "ema12": round(e12[i], 10), "ema26": round(e26[i], 10), "ema_up": e12[i] > e26[i],
            "rsi14": round(r[i], 2), "confluence_last_%d" % lookback: conf, "confluence": conf}


# ---------------------------------------------------------------- the questions
# kind "yn": x = +1 yes / -1 no / 0 unknown -> one weight.  kind "pick": one-hot over categories (all 0 if unknown).
QUESTIONS: list[dict] = [
    {"q": "liq_band", "kind": "pick", "cats": ["<100k", "100k-250k", "250k-1m", ">=1m"], "text": "Main-pool liquidity (USD) band?", "src": "DexScreener / Jupiter liquidity"},
    {"q": "top10_band", "kind": "pick", "cats": ["<=20", "20-40", ">40"], "text": "Top-10 non-pool holders' share (%) band?", "src": "RugCheck (Solana) / GoPlus (EVM)"},
    {"q": "pool_age_band", "kind": "pick", "cats": ["<24h", "1-7d", "7-90d", ">90d"], "text": "Pool / migration age band?", "src": "pairCreatedAt / graduatedAt"},
    {"q": "chg1h_band", "kind": "pick", "cats": ["<=-5", "-5..5", "5..25", ">25"], "text": "1 h price change band (>25% = chase)?", "src": "DexScreener / Jupiter % change"},
    {"q": "chase_6h", "kind": "yn", "text": "6 h change >= +60% (C3 chase territory)?", "src": "DexScreener / Jupiter % change"},
    {"q": "vol_liq_band", "kind": "pick", "cats": ["<0.5", "0.5-5", "5-40", ">40"], "text": "24 h volume / liquidity band (>40 = wash-like)?", "src": "DexScreener / Jupiter"},
    {"q": "smart_money_bought", "kind": "yn", "text": "At least one tracked smart wallet bought in the lookback window (24 h)?", "src": "smart_money.py / evm_smart_money.py (measured only)"},
    {"q": "mint_revoked", "kind": "yn", "text": "Mint authority revoked / not mintable?", "src": "RugCheck / GoPlus"},
    {"q": "freeze_revoked", "kind": "yn", "text": "Freeze authority revoked (Solana)?", "src": "RugCheck"},
    {"q": "lp_locked_90", "kind": "yn", "text": "Main-pool LP >= 90% locked/burned?", "src": "RugCheck / GoPlus"},
    {"q": "paid_promo", "kind": "yn", "text": "Paid promotion (DexScreener boosts/ads) present?", "src": "DexScreener boosts / orders"},
    {"q": "social_rising", "kind": "yn", "text": "Measured social momentum S4 >= 3?", "src": "social_data.py (measured only)"},
    {"q": "organic_high", "kind": "yn", "text": "Jupiter organic score label = high?", "src": "Jupiter Tokens v2"},
    {"q": "holders_growing", "kind": "yn", "text": "Holder count grew over 24 h?", "src": "Jupiter Tokens v2 holderChange 24h"},
    {"q": "cluster_elevated", "kind": "yn", "text": "Insider cluster / fresh-wallet share >= 15%?", "src": "RugCheck insiders / EVM fresh wallets"},
    {"q": "mcap_band", "kind": "pick", "cats": ["<1m", "1m-10m", "10m-100m", ">=100m"], "text": "Market cap band?", "src": "Jupiter / DexScreener / call post"},
    {"q": "pump_suffix", "kind": "yn", "text": "Address ends in 'pump' (pump.fun launch)?", "src": "address"},
    {"q": "ema_up", "kind": "yn", "text": "EMA12 > EMA26 on the last CLOSED hourly candle?", "src": "own candles (Binance.US / GeckoTerminal)"},
    {"q": "rsi_band", "kind": "pick", "cats": ["<30", "30-50", "50-70", ">70"], "text": "RSI(14) band on the last CLOSED hourly candle?", "src": "own candles"},
    {"q": "rsi_ema_confluence", "kind": "yn", "text": "RSI+EMA confluence (EMA12 crossed above EMA26 with RSI 50-70) in the last 3 closed hourly candles?", "src": "own candles"},
    {"q": "cost_le_2", "kind": "yn", "text": "Measured round trip at $100 <= 2%?", "src": "Jupiter quotes + venue fees"},
]
QMAP = {q["q"]: q for q in QUESTIONS}

# Hand-set NEUTRAL priors (logit units). Tiny on purpose: they only order candidates by the prompt's own logic and keep
# p near the base rate until a fit on real outcomes exists. Intercept = logit(0.42) ~ the martingale prior b/(a+b) for
# TP1 = 1R + ~3% cost at a 20% stop. See SCORING_NOTES.md "Narrow questions".
PRIOR_INTERCEPT = round(math.log(0.42 / 0.58), 4)
PRIOR_WEIGHTS: dict[str, float] = {
    "liq_band=<100k": -0.10, "liq_band=100k-250k": 0.0, "liq_band=250k-1m": 0.05, "liq_band=>=1m": 0.10,
    "top10_band=<=20": 0.10, "top10_band=20-40": 0.0, "top10_band=>40": -0.15,
    "pool_age_band=<24h": -0.10, "pool_age_band=1-7d": 0.0, "pool_age_band=7-90d": 0.05, "pool_age_band=>90d": 0.05,
    "chg1h_band=<=-5": -0.05, "chg1h_band=-5..5": 0.05, "chg1h_band=5..25": 0.0, "chg1h_band=>25": -0.15,
    "chase_6h": -0.10,
    "vol_liq_band=<0.5": -0.05, "vol_liq_band=0.5-5": 0.05, "vol_liq_band=5-40": 0.0, "vol_liq_band=>40": -0.15,
    "smart_money_bought": 0.10, "mint_revoked": 0.10, "freeze_revoked": 0.10, "lp_locked_90": 0.05,
    "paid_promo": -0.10, "social_rising": 0.05, "organic_high": 0.05, "holders_growing": 0.05, "cluster_elevated": -0.10,
    "mcap_band=<1m": -0.10, "mcap_band=1m-10m": 0.0, "mcap_band=10m-100m": 0.0, "mcap_band=>=100m": 0.05,
    "pump_suffix": 0.0, "ema_up": 0.05,
    "rsi_band=<30": 0.0, "rsi_band=30-50": 0.0, "rsi_band=50-70": 0.05, "rsi_band=>70": -0.05,
    "rsi_ema_confluence": 0.05, "cost_le_2": 0.05,
}


def columns() -> list[str]:
    cols = []
    for q in QUESTIONS:
        cols += [f"{q['q']}={c}" for c in q["cats"]] if q["kind"] == "pick" else [q["q"]]
    return cols


COLUMNS = columns()


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _band(v, edges: list[float], cats: list[str], right_closed=False):
    if v is None:
        return None
    for e, c in zip(edges, cats):
        if (v <= e) if right_closed else (v < e):
            return c
    return cats[-1]


def _yn(v):
    return None if v is None else bool(v)


# ---------------------------------------------------------------- views: a flat, source-agnostic reading of a record
def view_from_rec(rec: dict) -> dict:
    """Flatten a scanner record (Solana or EVM) into the inputs the questions need. Missing -> None."""
    chain = (rec.get("chain") or "solana").lower()
    rc, sec = rec.get("rugcheck") or {}, rec.get("security") or {}
    pc = rec.get("price_change") or {}
    sm, so = rec.get("smart_money") or {}, rec.get("social") or {}
    v = {"chain": chain, "address": rec.get("mint") or rec.get("address") or "",
         "liq": _f(rec.get("liquidity_usd")), "vol24": _f(rec.get("volume_24h")), "mcap": _f(rec.get("market_cap")),
         "pool_age_h": _f(rec.get("pool_age_h")), "chg_h1": _f(pc.get("h1")), "chg_h6": _f(pc.get("h6")),
         "organic": (rec.get("organic") or None), "holder_change_24h": _f(rec.get("holder_change_24h"))}
    if rc.get("ok"):
        v["top10"] = _f(rc.get("top10_nonpool_pct"))
        v["mint_revoked"] = not rc.get("mint_authority")
        v["freeze_revoked"] = not rc.get("freeze_authority")
        v["lp_locked_pct"] = _f(rc.get("lp_locked_main_pct"))
        v["cluster_pct"] = _f(rc.get("cluster_pct"))
    elif sec:
        v["top10"] = _f(sec.get("top10_nonpool_pct"))
        v["mint_revoked"] = None if sec.get("mintable") is None else not sec.get("mintable")
        v["lp_locked_pct"] = _f(sec.get("lp_locked_or_burned_pct"))
        v["cluster_pct"] = _f((rec.get("fresh_wallets") or {}).get("pct"))
    if sm.get("status") == "ok":
        v["sm_bought"] = bool(sm.get("buyers")) or (sm.get("level") or 0) >= 1
    paid = (so.get("paid") or {}).get("paid_promotion")
    if paid is True or rec.get("boosted") or any("boost" in str(s) for s in rec.get("sources") or []):
        v["paid"] = True
    elif paid is False or rec.get("ds_available"):
        v["paid"] = False
    if so.get("status") == "ok" and so.get("level") is not None:
        v["social_level"] = int(so["level"])
    c = rec.get("costs") or {}
    if chain == "solana":
        v["cost_pct"] = _f(c.get("phantom_app_pct"))
    elif c and not c.get("unknown"):
        opts = [_f(c.get(k)) for k in ("fomo_pct_with_1pct_allowance", "phantom_app_pct", "phantom_mcp_pct")]
        opts = [x for x in opts if x is not None]
        v["cost_pct"] = min(opts) if opts else None
    ta = rec.get("ta") or {}
    if ta:
        v.update(ema_up=ta.get("ema_up"), rsi=_f(ta.get("rsi14")), confluence=ta.get("confluence"))
    return v


def view_from_entry(e: dict) -> dict:
    """Flatten a shadow_tracker entry (its saved factor readings) for entries logged before rec['narrow'] existed,
    and for channel calls (only market cap at the call and the address are known). Post-entry data is NEVER used."""
    f = e.get("factors") or {}
    v = {"chain": e.get("chain"), "address": e.get("address") or "", "liq": _f(f.get("liquidity_usd")),
         "vol24": _f(f.get("volume_24h")), "mcap": _f(f.get("market_cap")) or _f(f.get("mc_at_call")) or _f(e.get("mc_at_call")),
         "pool_age_h": _f(f.get("pool_age_h")), "chg_h1": _f(f.get("chg_h1")), "chg_h6": _f(f.get("chg_h6")),
         "organic": f.get("organic"), "holder_change_24h": _f(f.get("holder_change_24h")),
         "top10": _f(f.get("rc_top10_nonpool_pct")), "cluster_pct": _f(f.get("rc_cluster_pct"))}
    failed = set(f.get("failed_checks") or [])
    if v["top10"] is not None:  # RugCheck ran (all its checks are evaluated together) -> absence of a failure = pass
        v["mint_revoked"] = not any(c.startswith("T2") for c in failed)
        v["freeze_revoked"] = not any(c.startswith("T3") for c in failed)
        v["lp_locked_pct"] = 0.0 if any(c.startswith("T4") for c in failed) else None
    if f.get("boosted"):   # a saved False can mean "DexScreener unavailable", so only a True is a known answer
        v["paid"] = True
    if f.get("smart_money_level") is not None:
        v["sm_bought"] = f["smart_money_level"] >= 1
    if f.get("social_level") is not None and f.get("social_measured", True):
        v["social_level"] = f["social_level"]
    return v


def answer(v: dict) -> dict:
    """The narrow answers. Every value is a yes/no, a category, or None (unknown)."""
    liq, vol = v.get("liq"), v.get("vol24")
    r = v.get("rsi")
    addr = v.get("address") or ""
    return {
        "liq_band": _band(liq, [100e3, 250e3, 1e6], QMAP["liq_band"]["cats"]),
        "top10_band": _band(v.get("top10"), [20, 40], QMAP["top10_band"]["cats"], right_closed=True),
        "pool_age_band": _band(v.get("pool_age_h"), [24, 168, 2160], QMAP["pool_age_band"]["cats"]),
        "chg1h_band": _band(v.get("chg_h1"), [-5, 5, 25], QMAP["chg1h_band"]["cats"], right_closed=True),
        "chase_6h": None if v.get("chg_h6") is None else v["chg_h6"] >= 60,
        "vol_liq_band": None if not liq or vol is None else _band(vol / liq, [0.5, 5, 40], QMAP["vol_liq_band"]["cats"]),
        "smart_money_bought": _yn(v.get("sm_bought")),
        "mint_revoked": _yn(v.get("mint_revoked")),
        "freeze_revoked": _yn(v.get("freeze_revoked")) if (v.get("chain") or "solana") == "solana" else None,
        "lp_locked_90": None if v.get("lp_locked_pct") is None else v["lp_locked_pct"] >= 90,
        "paid_promo": _yn(v.get("paid")),
        "social_rising": None if v.get("social_level") is None else v["social_level"] >= 3,
        "organic_high": None if not v.get("organic") else str(v["organic"]).lower() == "high",
        "holders_growing": None if v.get("holder_change_24h") is None else v["holder_change_24h"] > 0,
        "cluster_elevated": None if v.get("cluster_pct") is None else v["cluster_pct"] >= 15,
        "mcap_band": _band(v.get("mcap"), [1e6, 1e7, 1e8], QMAP["mcap_band"]["cats"]),
        "pump_suffix": (addr.lower().endswith("pump")) if addr else None,
        "ema_up": _yn(v.get("ema_up")),
        "rsi_band": _band(r, [30, 50, 70], QMAP["rsi_band"]["cats"]),
        "rsi_ema_confluence": _yn(v.get("confluence")),
        "cost_le_2": None if v.get("cost_pct") is None else v["cost_pct"] <= 2.0,
    }


def encode(features: dict) -> dict[str, float]:
    x = {}
    for q in QUESTIONS:
        a = features.get(q["q"])
        if q["kind"] == "yn":
            x[q["q"]] = 0.0 if a is None else (1.0 if a else -1.0)
        else:
            for c in q["cats"]:
                x[f"{q['q']}={c}"] = 1.0 if a == c else 0.0
    return x


def n_known(features: dict) -> int:
    return sum(1 for q in QUESTIONS if features.get(q["q"]) is not None)


def sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))


# ---------------------------------------------------------------- model
def prior_model() -> dict:
    return {"version": PRIOR_VERSION, "fitted": False, "intercept": PRIOR_INTERCEPT, "weights": dict(PRIOR_WEIGHTS)}


def load_model() -> dict:
    """A saved fit (fit --save, >= 50 labels) if present and complete, else the neutral priors."""
    try:
        with open(MODEL_PATH, encoding="utf-8") as f:
            m = json.load(f)
        if m.get("fitted") and m.get("n_labels", 0) >= MIN_LABELS and isinstance(m.get("weights"), dict):
            return m
    except (OSError, ValueError):
        pass
    return prior_model()


def predict(features: dict, model: dict | None = None) -> float:
    m = model or load_model()
    x = encode(features)
    z = m["intercept"] + sum(m["weights"].get(k, 0.0) * v for k, v in x.items())
    return sigmoid(z)


def narrow_block(rec: dict, model: dict | None = None) -> dict:
    """rec['narrow'] = {features, p, model_version, answered}. Informational only (SHADOW)."""
    m = model or load_model()
    feats = answer(view_from_rec(rec))
    return {"features": feats, "p": round(predict(feats, m), 4), "model_version": m["version"],
            "answered": f"{n_known(feats)}/{len(QUESTIONS)}", "shadow_only": True}


def version_hash(paths: list[str]) -> str:
    h = hashlib.sha1()
    for p in paths:
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(p.encode())
    return h.hexdigest()[:8]


# ---------------------------------------------------------------- fitting (L2 logistic, time-ordered hold-out)
def _solve(A: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        piv = M[c][c] if abs(M[c][c]) > 1e-12 else 1e-12
        for r in range(n):
            if r != c:
                f = M[r][c] / piv
                if f:
                    for k in range(c, n + 1):
                        M[r][k] -= f * M[c][k]
    return [M[i][n] / (M[i][i] if abs(M[i][i]) > 1e-12 else 1e-12) for i in range(n)]


def fit_logistic(X: list[list[float]], y: list[int], l2: float = 1.0, iters: int = 50) -> tuple[float, list[float]]:
    """Newton/IRLS for L2-penalised logistic regression. Intercept (column 0 implicit) is not penalised."""
    d = len(X[0]) if X else 0
    w = [0.0] * (d + 1)
    for _ in range(iters):
        g = [0.0] * (d + 1)
        H = [[0.0] * (d + 1) for _ in range(d + 1)]
        for xi, yi in zip(X, y):
            row = [1.0] + xi
            p = sigmoid(sum(a * b for a, b in zip(w, row)))
            s = p * (1 - p)
            for j in range(d + 1):
                if row[j]:
                    g[j] += (p - yi) * row[j]
                    rj = row[j] * s
                    for k in range(d + 1):
                        if row[k]:
                            H[j][k] += rj * row[k]
        for j in range(1, d + 1):
            g[j] += l2 * w[j]
            H[j][j] += l2
        H[0][0] += 1e-9
        step = _solve(H, g)
        w = [a - b for a, b in zip(w, step)]
        if max(abs(s) for s in step) < 1e-7:
            break
    return w[0], w[1:]


def metrics(ps: list[float], ys: list[int], thr: float = 0.5) -> dict:
    n = len(ys)
    if not n:
        return {"n": 0}
    acc = sum(1 for p, y in zip(ps, ys) if (p >= thr) == bool(y)) / n
    brier = sum((p - y) ** 2 for p, y in zip(ps, ys)) / n
    eps = 1e-6
    ll = -sum(y * math.log(min(max(p, eps), 1 - eps)) + (1 - y) * math.log(1 - min(max(p, eps), 1 - eps)) for p, y in zip(ps, ys)) / n
    pos = [p for p, y in zip(ps, ys) if y]
    neg = [p for p, y in zip(ps, ys) if not y]
    auc = None
    if pos and neg:
        auc = sum((1.0 if a > b else 0.5 if a == b else 0.0) for a in pos for b in neg) / (len(pos) * len(neg))
    cal = []
    for lo, hi in ((0, 0.3), (0.3, 0.45), (0.45, 0.6), (0.6, 0.75), (0.75, 1.01)):
        b = [(p, y) for p, y in zip(ps, ys) if lo <= p < hi]
        if b:
            cal.append({"bucket": f"{lo:.2f}-{min(hi, 1):.2f}", "n": len(b), "pred": round(sum(p for p, _ in b) / len(b), 3),
                        "actual": round(sum(y for _, y in b) / len(b), 3)})
    return {"n": n, "base_rate": round(sum(ys) / n, 4), "accuracy": round(acc, 4), "brier": round(brier, 4),
            "logloss": round(ll, 4), "auc": round(auc, 4) if auc is not None else None, "calibration": cal}


def labeled_rows(entries: list[dict], min_known: int = 3) -> list[dict]:
    """Resolved shadow entries (scanner + channel calls) with >= min_known narrow answers. WIN = 1; LOSS/AMBIGUOUS/
    RUGGED/NO_DATA/EXPIRED = 0 (the tracker's own convention); BAD_PRICE/SUPERSEDED excluded."""
    rows = []
    for e in entries:
        if e.get("status") != "RESOLVED" or e.get("outcome") in (None, "BAD_PRICE", "SUPERSEDED"):
            continue
        nb = e.get("narrow") or {}
        feats = nb.get("features") or answer(view_from_entry(e))
        if n_known(feats) < min_known:
            continue
        rows.append({"id": e.get("id"), "ts": e.get("entry_ts") or 0, "y": 1 if e.get("outcome") == "WIN" else 0,
                     "features": feats, "scanner_p": e.get("p"), "composite": e.get("composite"),
                     "source": e.get("source", "scanner")})
    rows.sort(key=lambda r: r["ts"])
    return rows


def fit(entries: list[dict], l2: float = 1.0, test_frac: float = 0.3, min_labels: int = MIN_LABELS) -> dict:
    min_labels = max(MIN_LABELS, int(min_labels))   # the 50-label floor can be raised, never lowered
    rows = labeled_rows(entries)
    out = {"n_labels": len(rows), "min_labels": min_labels, "at": time.strftime("%Y-%m-%d %H:%M %Z")}
    if len(rows) < min_labels:
        out.update(ok=False, message=f"too few labels ({len(rows)} resolved entries with narrow features; need >= {min_labels}). No fit reported.")
        return out
    k = max(1, int(round(len(rows) * test_frac)))
    train, test = rows[:-k], rows[-k:]
    if len({r["y"] for r in train}) < 2:
        out.update(ok=False, message="training labels are all one class; no fit reported")
        return out
    Xtr = [[encode(r["features"])[c] for c in COLUMNS] for r in train]
    b0, w = fit_logistic(Xtr, [r["y"] for r in train], l2=l2)
    model = {"version": f"narrow-fit-{time.strftime('%Y%m%d')}-n{len(train)}", "fitted": True, "intercept": round(b0, 5),
             "weights": {c: round(v, 5) for c, v in zip(COLUMNS, w)}, "n_labels": len(rows), "n_train": len(train),
             "l2": l2, "split": "time-ordered: oldest %d%% train, newest %d%% test" % (100 - round(test_frac * 100), round(test_frac * 100))}
    ys = [r["y"] for r in test]
    pri = prior_model()
    base = sum(r["y"] for r in train) / len(train)
    comp = [(r["composite"], r["y"]) for r in test if r["composite"] is not None]
    sp = [(r["scanner_p"], r["y"]) for r in test if r["scanner_p"] is not None]
    out.update(ok=True, model=model, test={
        "narrow_fitted": metrics([predict(r["features"], model) for r in test], ys),
        "narrow_prior": metrics([predict(r["features"], pri) for r in test], ys),
        "train_base_rate_constant": metrics([base] * len(test), ys),
        "scanner_p (P_WIN / derived prior)": metrics([p for p, _ in sp], [y for _, y in sp]),
        "composite (not a probability; accuracy at the 0.75 bar)": metrics([c for c, _ in comp], [y for _, y in comp], thr=0.75)})
    return out


def load_entries(path: str | None = None) -> list[dict]:
    path = path or os.path.join(DATA_DIR, "shadow_log.jsonl")
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


# ---------------------------------------------------------------- selftest (offline)
def selftest() -> list[tuple[str, bool, str]]:
    import random
    rows = []

    def add(n, ok, d=""):
        rows.append((n, bool(ok), str(d)[:200]))
    # EMA / RSI basics
    e = ema([1.0] * 30, 12)
    add("EMA of a constant series is that constant", abs(e[-1] - 1.0) < 1e-12 and e[10] is None, e[-1])
    up = [float(i) for i in range(1, 40)]
    add("RSI of a strictly rising series = 100", rsi(up)[-1] == 100.0, rsi(up)[-1])
    dn = [float(40 - i) for i in range(40)]
    add("RSI of a strictly falling series = 0", rsi(dn)[-1] == 0.0, rsi(dn)[-1])
    # known Wilder example value sanity (alternating +1/-1 -> RSI ~ 50)
    alt = [10 + (i % 2) for i in range(60)]
    add("RSI of alternating +1/-1 is ~50", 45 < rsi(alt)[-1] < 55, rsi(alt)[-1])
    # closed candles only
    now = 1_790_000_000
    cs = [(now - 3600 * (50 - i), 1, 1, 1, 1) for i in range(50)]   # last candle opened 1 h ago -> closed
    add("closed_only keeps a candle whose hour has ended", len(closed_only(cs, 3600, now)) == 50)
    cs2 = cs + [(now - 600, 1, 1, 1, 1)]
    add("closed_only drops the still-forming candle", len(closed_only(cs2, 3600, now)) == 50)
    # a forming candle that would flip the signal must not affect ta_state
    base = [100.0 - 0.3 * i for i in range(60)] + [82.0 + 0.9 * i for i in range(12)]
    cand = [(now - 3600 * (len(base) - i), b, b * 1.001, b * 0.999, b) for i, b in enumerate(base)]
    s1 = ta_state(cand, 3600, now)
    s2 = ta_state(cand + [(now - 60, 200, 300, 1, 300)], 3600, now)
    add("TA state ignores the forming candle (identical with/without it)", s1 == s2, (s1 or {}).get("rsi14"))
    # signal A fires on a cross with RSI 50-70 only
    e12 = [None, 1.0, 2.0]
    e26 = [None, 1.5, 1.5]
    add("signal A: bull cross + RSI 60 fires", signal_a(e12, e26, [None, 50, 60], 2))
    add("signal A: bull cross + RSI 80 (overbought) does not fire", not signal_a(e12, e26, [None, 50, 80], 2))
    add("signal B: RSI 28 -> 32 in uptrend fires", signal_b([None, 2, 2], [None, 1, 1], [None, 28, 32], 2))
    add("exit: bear cross", exit_signal([None, 2, 1], [None, 1.5, 1.5], 2))
    # questions
    rec = {"chain": "solana", "mint": "Abcpump", "liquidity_usd": 300e3, "volume_24h": 900e3, "market_cap": 5e6, "pool_age_h": 30,
           "price_change": {"h1": 30, "h6": 70}, "rugcheck": {"ok": True, "top10_nonpool_pct": 18, "mint_authority": None,
           "freeze_authority": "X", "lp_locked_main_pct": 100, "cluster_pct": 20}, "boosted": True, "ds_available": True,
           "smart_money": {"status": "ok", "buyers": [{"w": 1}], "level": 1}, "social": {"status": "ok", "level": 3},
           "organic": "high", "holder_change_24h": 2.0, "costs": {"phantom_app_pct": 2.5},
           "ta": {"ema_up": True, "rsi14": 61, "confluence": True}}
    a = answer(view_from_rec(rec))
    exp = {"liq_band": "250k-1m", "top10_band": "<=20", "pool_age_band": "1-7d", "chg1h_band": ">25", "chase_6h": True,
           "vol_liq_band": "0.5-5", "smart_money_bought": True, "mint_revoked": True, "freeze_revoked": False, "lp_locked_90": True,
           "paid_promo": True, "social_rising": True, "organic_high": True, "holders_growing": True, "cluster_elevated": True,
           "mcap_band": "1m-10m", "pump_suffix": True, "ema_up": True, "rsi_band": "50-70", "rsi_ema_confluence": True, "cost_le_2": False}
    bad = {k: (a.get(k), v) for k, v in exp.items() if a.get(k) != v}
    add("all 21 narrow answers derived mechanically from a full Solana record", not bad and len(a) == len(QUESTIONS), bad)
    empty = answer(view_from_rec({"chain": "base"}))
    add("empty record -> every answer unknown (None) except none", n_known(empty) == 0, n_known(empty))
    add("unknown answers are neutral: p(empty) = prior intercept", abs(predict(empty, prior_model()) - sigmoid(PRIOR_INTERCEPT)) < 1e-12)
    nb = narrow_block(rec, prior_model())
    add("narrow block has features/p/model_version and stays near base rate with priors", set(nb) >= {"features", "p", "model_version"}
        and 0.2 < nb["p"] < 0.7 and nb["model_version"] == PRIOR_VERSION, nb["p"])
    add("prior weights cover every encoded column", set(PRIOR_WEIGHTS) == set(COLUMNS), set(COLUMNS) ^ set(PRIOR_WEIGHTS))
    ent = {"chain": "solana", "address": "Zpump", "factors": {"liquidity_usd": 60e3, "volume_24h": 5e5, "market_cap": 2e6,
           "chg_h1": 1, "chg_h6": 2, "rc_top10_nonpool_pct": 25, "rc_cluster_pct": 3, "failed_checks": ["T2 mint authority revoked"], "boosted": False}}
    fe = answer(view_from_entry(ent))
    add("old shadow entry -> features from saved factors (T2 failure -> mint not revoked)", fe["mint_revoked"] is False and fe["freeze_revoked"] is True
        and fe["top10_band"] == "20-40" and fe["paid_promo"] is None, fe)
    call = {"chain": "solana", "address": "Qpump", "mc_at_call": 50_000, "factors": {"mc_at_call": 50_000, "pump_suffix": True}}
    add("channel call -> only at-call features (mcap band, pump suffix)", n_known(answer(view_from_entry(call))) == 2)
    # fit refusal and fit on synthetic data
    few = [{"status": "RESOLVED", "outcome": "WIN" if i % 2 else "LOSS", "entry_ts": i, "factors": ent["factors"], "chain": "solana",
            "address": "A"} for i in range(49)]
    r = fit(few)
    add("fit refuses below 50 labels ('too few labels')", not r["ok"] and "too few labels" in r["message"], r.get("message"))
    r = fit(few, min_labels=10)
    add("the 50-label floor cannot be lowered by the caller", not r["ok"] and r["min_labels"] == 50)
    rng = random.Random(3)
    syn = []
    for i in range(400):
        liq = rng.choice([60e3, 150e3, 400e3, 2e6])
        good = liq >= 400e3
        y = 1 if rng.random() < (0.7 if good else 0.2) else 0
        syn.append({"status": "RESOLVED", "outcome": "WIN" if y else "LOSS", "entry_ts": i, "chain": "solana", "address": "B",
                    "p": 0.43, "composite": None,
                    "factors": {"liquidity_usd": liq, "volume_24h": liq, "chg_h1": rng.uniform(-3, 3), "chg_h6": 1.0}})
    r = fit(syn, l2=1.0)
    t = r.get("test") or {}
    add("synthetic fit learns a real signal: fitted AUC > 0.7 and Brier beats the constant base rate",
        r["ok"] and t["narrow_fitted"]["auc"] > 0.7 and t["narrow_fitted"]["brier"] < t["train_base_rate_constant"]["brier"],
        {k: (v.get("auc"), v.get("brier")) for k, v in t.items()})
    add("fitted weight for liq >= 1m is positive, liq < 100k negative",
        r["ok"] and r["model"]["weights"]["liq_band=>=1m"] > 0 > r["model"]["weights"]["liq_band=<100k"])
    add("hold-out is time-ordered (test = newest)", r["ok"] and r["model"]["n_train"] == 280)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Narrow-question shadow probability (never a gate)")
    ap.add_argument("cmd", choices=["explain", "fit", "selftest"])
    ap.add_argument("--l2", type=float, default=1.0)
    ap.add_argument("--test-frac", type=float, default=0.3)
    ap.add_argument("--min-labels", type=int, default=MIN_LABELS)
    ap.add_argument("--log", help="shadow_log.jsonl path (default data/shadow_log.jsonl)")
    ap.add_argument("--save", action="store_true", help="save the fitted weights to data/narrow_model.json (only if the fit ran)")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        rs = selftest()
        for n, ok, d in rs:
            print("PASS" if ok else "FAIL", n, "" if ok else f":: {d}")
        print(f"{sum(1 for r in rs if r[1])}/{len(rs)} passed")
        return 0 if all(r[1] for r in rs) else 1
    if a.cmd == "explain":
        m = load_model()
        print(f"model {m['version']} (fitted={m['fitted']}), intercept {m['intercept']}")
        for q in QUESTIONS:
            cols = [f"{q['q']}={c}" for c in q["cats"]] if q["kind"] == "pick" else [q["q"]]
            print(f"- {q['q']} [{q['kind']}] {q['text']} (source: {q['src']}) weights: " + ", ".join(f"{c.split('=')[-1]}={m['weights'].get(c, 0):+.3f}" for c in cols))
        return 0
    r = fit(load_entries(a.log), l2=a.l2, test_frac=a.test_frac, min_labels=a.min_labels)
    if not r["ok"]:
        print(r["message"])
        return 0
    print(json.dumps({k: v for k, v in r.items() if k != "model"}, indent=1))
    if a.save:
        os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
        with open(MODEL_PATH + ".tmp", "w", encoding="utf-8") as f:
            json.dump(r["model"], f, indent=1)
        os.replace(MODEL_PATH + ".tmp", MODEL_PATH)
        print(f"saved {MODEL_PATH} ({r['model']['version']}); the scanner shows it as a SHADOW number only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
