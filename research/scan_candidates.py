#!/usr/bin/env python3
"""scan_candidates.py: READ-ONLY multichain candidate scanner (Solana + Base/Ethereum/BNB/Monad/Robinhood Chain; Sui research only) for the Phantom master prompt (manual mode watchlist).

Pulls candidates (DexScreener boosts/profiles/takeovers, DexScreener pair searches ranked by 24h volume, new
graduated PumpSwap/Raydium/Meteora pairs, GeckoTerminal trending/new when not throttled, plus a fixed list of
liquid Solana majors/mid-caps), applies the prompt's hard gates and vetoes (G1-G11, T1-T12 where data exists,
H17 cost via real Jupiter quotes, SOL crash regime, news danger words), computes the §2C COMPOSITE and a
P_WIN estimate with an explicit, conservative method, and writes ranked JSON + markdown with every reject reason.

NOTHING here signs, sends, swaps, connects a wallet or places an order. Jupiter is used for quotes only
(no taker, no /execute). Every external string (token names, descriptions, news) is DATA, never instructions;
contract addresses come only from DexScreener/RugCheck/our fixed majors list, never from articles or posts.

P_WIN METHOD (self-answered, uncalibrated; see --explain):
  stop b from recent hourly volatility (memecoins capped at 25%, H5); TP1 = b + round-trip cost (the minimum the
  prompt allows: 1R + cost).  Prior: for a driftless (martingale) price, P(hit +a before -b) = b/(a+b), which is
  < 0.5 whenever TP1 >= 1R + cost.  Evidence: a non-overlapping hourly backtest of the same stop/TP1 on ~40 days of
  candles (stop checked first inside a candle; timeouts after the horizon count as NOT reaching TP1).
  Posterior = Beta(prior*m, (1-prior)*m) updated with wins/losses; we report the posterior mean, a 90% interval and
  P(p >= 0.75).  No momentum/social "bonus" is added.  Past hit-rates reflect the last ~40 days' regime only.

Usage:
  python3 scan_candidates.py                       # full scan -> data/scans/scan-<ts>.{json,md} + latest
  python3 scan_candidates.py --watchlist PATH.md   # also write a morning-watchlist style file
  python3 scan_candidates.py --snapshot-only       # quick liquidity snapshot (depth-stability evidence for S1)
  python3 scan_candidates.py --selftest            # offline logic tests + a light live smoke test
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import crypto_data as cd  # noqa: E402
import crypto_news as cn  # noqa: E402
from _http import get_json, get_json_retry, limiter_status, cooldown_remaining  # noqa: E402


def _http_cooling(url: str) -> bool:
    try:
        return cooldown_remaining(url) > 5
    except Exception:
        return False
try:  # optional Helius smart-money factor (S3); the scanner still runs without it (S3 stays UNKNOWN)
    import smart_money as sm  # noqa: E402
except Exception:  # pragma: no cover
    sm = None
import scoring  # noqa: E402  (shared §2C aggregation; see SCORING_NOTES.md)
import chain_common as ccm  # noqa: E402  (multichain venue facts + FOMO/Phantom move cards)
try:  # SHADOW-ONLY narrow-question probability + per-record receipts (never gates anything)
    import narrow_questions as nq  # noqa: E402
    import receipts as rcpt  # noqa: E402
except Exception:  # pragma: no cover
    nq = rcpt = None
try:  # keyless social/attention evidence for S4 (Reddit RSS, Telegram previews, news velocity, trending, paid-promo flags)
    import social_data as soc  # noqa: E402
except Exception:  # pragma: no cover
    soc = None
try:  # EVM chains (Base/Ethereum + FOMO-only BNB/Monad/Robinhood Chain), read-only
    import evm_data  # noqa: E402
    import evm_smart_money  # noqa: E402
except Exception:  # pragma: no cover
    evm_data = evm_smart_money = None
try:  # Sui: research only (no venue)
    import sui_data  # noqa: E402
except Exception:  # pragma: no cover
    sui_data = None

ET = ZoneInfo("America/New_York")
import paths as _paths  # noqa: E402
OUT_DIR = _paths.SCANS_DIR
SNAP_FILE = os.path.join(OUT_DIR, "liquidity_snapshots.json")
CONFIG_FILE = os.path.join(HERE, "scan_config.json")
USDC = cd.USDC_MINT
SOL = cd.SOL_MINT

# Fixed majors / mid-caps (mint verified at run time against DexScreener baseToken + RugCheck).
MAJORS = {
    "SOL": SOL, "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
    "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R", "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
    "ORCA": "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE", "W": "85VBFQZC9TZkfaptBWjvUw7YbZjy52A6mjtPGjstQAmQ",
    "RENDER": "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof", "HNT": "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux",
    "POPCAT": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr", "MEW": "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",
    "FARTCOIN": "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump", "PENGU": "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",
    "TRUMP": "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN", "DRIFT": "DriFtupJYLTosbwoN8koMbEYSx54aFAVLddWsbksjwg7",
    "KMNO": "KMNo3nJsBXfcpJTVhZcXLW7RmTwTt4GVFE7suUBo9sS", "MOODENG": "ED5nyyWEzpPPiWimP8vYm7sD7TD3LAt3Q3gRTWHzPJBY",
    "PUMP": "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn", "TNSR": "TNSRxcUxoT9xBG3de7PiJyTDYu7kskLqcpddxnEJAS6",
}
BINANCE_US = {"SOL", "JUP", "JTO", "BONK", "WIF", "ORCA", "RENDER", "POPCAT", "MEW", "FARTCOIN", "PENGU", "TRUMP",
              "MOODENG", "PUMP"}
EXCLUDE = {  # stables, LSTs, wrapped majors: not trade candidates for this bot
    USDC, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So", "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v", "5oVNBeEEQvYi1cX3ir8Dx5n1P7pdxydbGF2X4TxVusJm",
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB", "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij", "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", "27G8MtK7VtTcCHkpASjSDdkWWYfoqT6ggEuKidVJidD4",
}
STABLE_SYMS = re.compile(r"^(USD.*|.*USD|USDT|USDC|EURC|PYUSD|FDUSD|DAI|.*SOL)$", re.I)  # *SOL = LSTs (JitoSOL, mSOL...)
# 2026-09-25: 9 -> 5 searches (the dropped ones mostly duplicated these); DexScreener rate limits were emptying scans.
DS_QUERIES = ["SOL", "USDC", "pumpswap", "raydium", "meteora"]
DANGER_WORDS = [k.strip() for k in cn.RISK_KEYWORDS]
CLASS_SLIP_CAP = {"major": 1.0, "mid": 3.0, "memecoin": 5.0}
PHANTOM_FEE_PCT = 0.85   # per swap (prompt §2E.8)
PHANTOM_FIXED_USD = 0.004
LEVEL_NAMES = {0: "0", 1: "1", 2: "2", 3: "3", 4: "4"}


def now_et() -> datetime:
    return datetime.now(ET)


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def fnum(x, default=None):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- candidate discovery
def discover(cfg: dict, log: list) -> dict[str, set]:
    """mint -> set of discovery sources. External text never supplies a mint except DexScreener/GT/RugCheck fields."""
    cands: dict[str, set] = {}

    def add(mint, src):
        if mint and isinstance(mint, str) and 32 <= len(mint) <= 44:
            cands.setdefault(mint, set()).add(src)

    for path, tag in (("token-boosts/top/v1", "ds_boost_top"), ("token-boosts/latest/v1", "ds_boost_latest"),
                      ("token-profiles/latest/v1", "ds_profiles"), ("community-takeovers/latest/v1", "ds_cto")):
        r = get_json(f"https://api.dexscreener.com/{path}")
        rows = r.data if r.ok and isinstance(r.data, list) else []
        n = 0
        for x in rows:
            if x.get("chainId") == "solana":
                add(x.get("tokenAddress"), tag)
                n += 1
        log.append({"source": f"dexscreener {path}", "ok": r.ok, "status": r.status, "solana": n,
                    "note": "" if r.ok else r.error[:140]})
    vol_pairs = []
    for q in DS_QUERIES:
        r = cd.dexscreener_search(q)
        pairs = [p for p in ((r.data or {}).get("pairs") or [])] if r.ok else []
        pairs = [p for p in pairs if p.get("chainId") == "solana"]
        vol_pairs += pairs
        log.append({"source": f"dexscreener search '{q}'", "ok": r.ok, "status": r.status, "solana": len(pairs),
                    "note": "" if r.ok else r.error[:140]})
    now_ms = time.time() * 1000
    seen = set()
    for p in sorted(vol_pairs, key=lambda p: fnum((p.get("volume") or {}).get("h24"), 0), reverse=True):
        m = (p.get("baseToken") or {}).get("address")
        if m in seen:
            continue
        seen.add(m)
        if len(seen) <= 40:
            add(m, "ds_top_volume")
        created = p.get("pairCreatedAt") or 0
        if p.get("dexId") in ("pumpswap", "raydium", "meteora", "meteoradbc", "launchlab") and created and \
                now_ms - created < 72 * 3600 * 1000:
            add(m, "ds_new_graduated")
    for url, tag in (("https://api.geckoterminal.com/api/v2/networks/solana/trending_pools", "gt_trending"),
                     ("https://api.geckoterminal.com/api/v2/networks/solana/new_pools", "gt_new_pools")):
        r = get_json_retry(url, cache_ttl=300)
        n = 0
        if r.ok and isinstance(r.data, dict):
            for pool in r.data.get("data") or []:
                base = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
                if base.startswith("solana_"):
                    add(base[7:], tag)
                    n += 1
        log.append({"source": f"geckoterminal {tag}", "ok": r.ok, "status": r.status, "solana": n,
                    "note": "" if r.ok else r.error[:140]})
    for path, tag in (("toptrending/1h", "jup_trending_1h"), ("toptraded/24h", "jup_top_traded_24h"),
                      ("toporganicscore/24h", "jup_top_organic_24h")):
        r = get_json_retry(f"https://api.jup.ag/tokens/v2/{path}?limit=50", cache_ttl=180)
        rows = r.data if r.ok and isinstance(r.data, list) else []
        for t in rows:
            add(t.get("id"), tag)
        log.append({"source": f"jupiter tokens/v2/{path}", "ok": r.ok, "status": r.status, "solana": len(rows),
                    "note": "" if r.ok else r.error[:140]})
    for sym, m in MAJORS.items():
        add(m, "major_list")
    return cands


def jupiter_tokens(mints: list[str], log: list) -> dict[str, dict]:
    """Jupiter Tokens v2 (keyless, reliable): price, mcap, liquidity, holders, 5m/1h/6h/24h stats incl. liquidity and
    holder change, organic score, audit, graduation. Up to 100 mints per call."""
    out: dict[str, dict] = {}
    for i in range(0, len(mints), 100):
        chunk = mints[i:i + 100]
        r = get_json_retry("https://api.jup.ag/tokens/v2/search?query=" + ",".join(chunk), cache_ttl=120)
        if not r.ok or not isinstance(r.data, list):
            log.append({"source": "jupiter tokens/v2/search batch", "ok": False, "status": r.status, "note": r.error[:140]})
            continue
        for t in r.data:
            if t.get("id") in chunk:
                out[t["id"]] = t
    return out


def dexscreener_batch(mints: list[str], log: list) -> dict[str, list]:
    """All pairs per mint via /tokens/v1 (30 per call)."""
    out: dict[str, list] = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        r = get_json("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk))
        if not r.ok or not isinstance(r.data, list):
            log.append({"source": "dexscreener tokens/v1 batch", "ok": False, "status": r.status, "note": r.error[:140]})
            if r.status == 429 or "cooling down" in (r.error or ""):
                break  # Jupiter Tokens v2 covers the rest (fallback already in evaluate)
            continue
        for p in r.data:
            b = (p.get("baseToken") or {}).get("address")
            if b in chunk and p.get("chainId") == "solana":
                out.setdefault(b, []).append(p)
    return out


def main_pair(pairs: list) -> dict | None:
    return max(pairs, key=lambda p: fnum((p.get("liquidity") or {}).get("usd"), 0)) if pairs else None


# ---------------------------------------------------------------- snapshots (S1 depth-stability evidence)
def load_snaps() -> dict:
    try:
        return json.load(open(SNAP_FILE))
    except (OSError, ValueError):
        return {}


def save_snaps(snaps: dict, entries: dict[str, float]) -> None:
    t = time.time()
    for m, liq in entries.items():
        snaps.setdefault(m, []).append([t, liq])
    for m in list(snaps):
        snaps[m] = [x for x in snaps[m] if t - x[0] < 24 * 3600][-50:]
        if not snaps[m]:
            snaps.pop(m)
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(snaps, open(SNAP_FILE, "w"))


def depth_trend(snaps: dict, mint: str, liq_now: float) -> dict:
    """Stable over the last hour = a snapshot 20-90 min old and liquidity now >= 95% of it."""
    t = time.time()
    old = [x for x in snaps.get(mint, []) if 20 * 60 <= t - x[0] <= 90 * 60]
    if not old or not liq_now:
        return {"evidence": False, "note": "no snapshot 20-90 min old (depth stability UNKNOWN)"}
    ref = old[0]
    chg = (liq_now / ref[1] - 1) * 100 if ref[1] else None
    return {"evidence": True, "age_min": round((t - ref[0]) / 60), "change_pct": round(chg, 2) if chg is not None else None,
            "stable": chg is not None and chg >= -5, "growing": chg is not None and chg >= 2}


# ---------------------------------------------------------------- price-action judgments (heuristic, uncalibrated)
def classify_price_action(pc: dict) -> tuple[str, str, float]:
    h1, h6, h24 = (fnum(pc.get(k), 0.0) for k in ("h1", "h6", "h24"))
    if h24 <= -40 or h6 <= -30:
        c2 = "COLLAPSE"
    elif h6 <= -15 and h1 <= -5:
        c2 = "DISTRIBUTION"
    elif h1 >= 40 or h6 >= 100 or h24 >= 300:
        c2 = "BLOW-OFF"
    elif h1 >= 10 or h6 >= 25:
        c2 = "BREAKOUT"
    elif 5 <= h24 <= 100 and h1 > -5:
        c2 = "HEALTHY EXPANSION"
    else:
        c2 = "ACCUMULATION"
    if h1 >= 25 or h6 >= 60:
        c3 = "CHASE"
    elif h1 <= 0 and h24 >= 0:
        c3 = "GOOD"
    else:
        c3 = "ACCEPTABLE"
    return c2, c3, 0.65  # heuristic from DexScreener % changes only: confidence deliberately modest


def trend_stage(age_h: float, pc: dict, established: bool) -> tuple[str, float, float]:
    h24 = fnum(pc.get("h24"), 0.0)
    if established:
        return ("EXPANDING", 0.5, 0.6) if h24 > 5 else ("CROWDED", 0.1, 0.55)
    if age_h < 48 and h24 > 0:
        return "EMERGING", 0.85, 0.55
    if age_h < 48:
        return "DECLINING", 0.0, 0.55
    return ("EXPANDING", 0.5, 0.55) if h24 > 0 else ("DECLINING", 0.0, 0.55)


# ---------------------------------------------------------------- OHLCV + P_WIN
def ohlcv_hourly(sym: str | None, pool: str | None, log: list) -> tuple[list, str]:
    """Hourly candles ascending [(ts,o,h,l,c)]. Binance.US for listed majors (reliable), else GeckoTerminal pool."""
    if sym and sym in BINANCE_US:
        r = get_json(f"https://api.binance.us/api/v3/klines?symbol={sym}USDT&interval=1h&limit=1000", cache_ttl=900)
        if r.ok and isinstance(r.data, list) and len(r.data) > 100:
            return [(int(k[0]) // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in r.data], "binance.us 1h"
    if pool:
        r = get_json(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/hour?aggregate=1&limit=1000",
                     cache_ttl=900)
        try:
            lst = r.data["data"]["attributes"]["ohlcv_list"]
            c = sorted((int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in lst)
            if len(c) > 24:
                return c, "geckoterminal 1h"
        except Exception:
            log.append({"source": "geckoterminal ohlcv", "ok": False, "status": r.status, "note": (r.error or "")[:100]})
    return [], "none"


def realized_vol_daily(candles: list) -> float | None:
    closes = [c[4] for c in candles[-169:] if c[4] > 0]
    if len(closes) < 25:
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    return statistics.pstdev(rets) * math.sqrt(24) if len(rets) > 10 else None


def choose_stop(cls: str, vol_d: float | None, pc: dict) -> tuple[float, str]:
    lo, hi = (0.05, 0.15) if cls == "major" else ((0.08, 0.20) if cls == "mid" else (0.12, 0.25))
    if vol_d:
        return round(min(hi, max(lo, 1.5 * vol_d)), 4), f"1.5 x daily vol {vol_d*100:.1f}% clamped to [{lo*100:.0f}%, {hi*100:.0f}%]"
    proxy = max(abs(fnum(pc.get("h24"), 0)), 2 * abs(fnum(pc.get("h6"), 0))) / 100
    return round(min(hi, max(lo, proxy)), 4), f"no candles: |24h| / 2x|6h| move proxy clamped to [{lo*100:.0f}%, {hi*100:.0f}%]"


def backtest(candles: list, b: float, a: float, horizon: int) -> dict:
    """Non-overlapping trades: enter at close, stop checked before TP1 inside each candle (conservative),
    timeout after `horizon` candles counts as NOT reaching TP1."""
    wins = losses = timeouts = 0
    i, n = 0, len(candles)
    while i < n - 2:
        entry = candles[i][4]
        stop, tp = entry * (1 - b), entry * (1 + a)
        j, res = i + 1, None
        while j < n and j <= i + horizon:
            _, _, hi, lo, _ = candles[j]
            if lo <= stop:
                res = "L"
                break
            if hi >= tp:
                res = "W"
                break
            j += 1
        if res is None:
            if j >= n:
                break  # unresolved at the end of data: not counted
            res = "T"
        wins += res == "W"
        losses += res == "L"
        timeouts += res == "T"
        i = j + 1
    return {"wins": wins, "losses": losses, "timeouts": timeouts, "n": wins + losses + timeouts}


def beta_summary(alpha: float, beta: float, bar: float, seed: int = 7) -> dict:
    rng = random.Random(seed)
    xs = sorted(rng.betavariate(alpha, beta) for _ in range(20000))
    return {"mean": round(alpha / (alpha + beta), 4), "lo90": round(xs[1000], 4), "hi90": round(xs[19000], 4),
            "p_ge_bar": round(sum(1 for x in xs if x >= bar) / len(xs), 4)}


def p_win(b: float, cost_pct: float, candles: list, horizon: int, m: float, bar: float) -> dict:
    a = b + cost_pct / 100.0  # TP1 = 1R + cost (the minimum the prompt allows)
    prior = b / (a + b)
    bt = backtest(candles, b, a, horizon) if candles else {"wins": 0, "losses": 0, "timeouts": 0, "n": 0}
    alpha = prior * m + bt["wins"]
    beta_ = (1 - prior) * m + bt["losses"] + bt["timeouts"]
    s = beta_summary(alpha, beta_, bar)
    return {"stop_pct": round(b * 100, 2), "tp1_pct": round(a * 100, 2), "tp1_R": round(a / b, 3),
            "tp2_pct": round((2 * b + cost_pct / 100) * 100, 2), "prior_martingale": round(prior, 4),
            "backtest": bt, "posterior": s, "estimate": s["mean"],
            "label": "self-answered (uncalibrated)", "passes_bar": s["mean"] >= bar}


# ---------------------------------------------------------------- safety (RugCheck + GoPlus)
def rugcheck_facts(mint: str, main_pool: str | None, cfg: dict) -> dict:
    r = cd.rugcheck_report(mint)
    if not r.ok or not isinstance(r.data, dict):
        return {"ok": False, "error": f"rugcheck {r.status}: {(r.error or '')[:100]}"}
    d = r.data
    ka = d.get("knownAccounts") or {}
    markets = d.get("markets") or []
    pool_keys = {m.get("pubkey") for m in markets}
    for m in markets:
        pool_keys |= {m.get("liquidityA"), m.get("liquidityB")}
        for acct in ("liquidityAAccount", "liquidityBAccount"):
            if isinstance(m.get(acct), dict):
                pool_keys.add(m[acct].get("owner"))
    pool_keys.discard(None)
    nonpool = []
    for h in d.get("topHolders") or []:
        t_owner = (ka.get(h.get("owner")) or {}).get("type")
        t_addr = (ka.get(h.get("address")) or {}).get("type")
        if t_owner in ("AMM", "LOCKER") or t_addr in ("AMM", "LOCKER") or h.get("owner") in pool_keys \
                or h.get("address") in pool_keys:
            continue
        nonpool.append(h)
    top10 = round(sum(fnum(h.get("pct"), 0) for h in nonpool[:10]), 2) if d.get("topHolders") else None
    supply = fnum((d.get("token") or {}).get("supply"), 0)
    nets = d.get("insiderNetworks") or []
    cluster = round(sum(fnum(n.get("tokenAmount"), 0) for n in nets) / supply * 100, 2) if supply else None
    lp_main, main_type = None, None
    if not main_pool or main_pool not in {m.get("pubkey") for m in markets}:
        # DexScreener main pool unknown/not listed: use RugCheck's deepest market (baseUSD + quoteUSD)
        best = max(markets, key=lambda m: fnum((m.get("lp") or {}).get("baseUSD"), 0) + fnum((m.get("lp") or {}).get("quoteUSD"), 0),
                   default=None)
        main_pool = (best or {}).get("pubkey")
    for m in markets:
        if main_pool and m.get("pubkey") == main_pool:
            lp_main = fnum((m.get("lp") or {}).get("lpLockedPct"))
            main_type = m.get("marketType")
    only_curve = bool(markets) and all((m.get("marketType") or "") == "pump_fun" for m in markets)
    lp_any = max([fnum((m.get("lp") or {}).get("lpLockedPct"), 0) for m in markets] or [0])
    ext = d.get("token_extensions") or {}
    hostile_ext = []
    if (fnum((d.get("transferFee") or {}).get("pct"), 0) or 0) > 0 or ext.get("transferFeeConfig"):
        hostile_ext.append("transfer fee")
    if ext.get("permanentDelegate"):
        hostile_ext.append("permanent delegate")
    if ext.get("transferHook") or ext.get("transferHookProgramId"):
        hostile_ext.append("transfer hook")
    if ext.get("nonTransferable"):
        hostile_ext.append("non-transferable")
    if (ext.get("defaultAccountState") or "") in ("frozen", 2, "2"):
        hostile_ext.append("default frozen accounts")
    ver = d.get("verification") or {}
    meta = d.get("tokenMeta") or {}
    return {"ok": True, "mint": d.get("mint"), "symbol": meta.get("symbol") or ver.get("symbol"), "name": meta.get("name"),
            "mint_authority": d.get("mintAuthority"), "freeze_authority": d.get("freezeAuthority"),
            "top10_nonpool_pct": top10, "cluster_pct": cluster, "insider_networks": len(nets),
            "graph_insiders": d.get("graphInsidersDetected"), "holders": d.get("totalHolders"),
            "lp_locked_main_pct": lp_main, "lp_locked_max_pct": lp_any, "lp_providers": d.get("totalLPProviders"),
            "markets": len(markets), "risks": [{"name": x.get("name"), "level": x.get("level")} for x in d.get("risks") or []],
            "rugged": bool(d.get("rugged")), "score_normalised": d.get("score_normalised"),
            "creator": d.get("creator"), "creator_balance": d.get("creatorBalance"),
            "jup_verified": bool(ver.get("jup_verified")), "jup_strict": bool(ver.get("jup_strict")),
            "launchpad": (d.get("launchpad") or {}).get("name"), "detected_at": d.get("detectedAt"),
            "token_program": d.get("tokenProgram"), "hostile_extensions": hostile_ext,
            "mutable_metadata": bool(meta.get("mutable")), "main_pool": main_pool, "main_pool_type": main_type,
            "only_bonding_curve_market": only_curve,
            "top_nonpool_holders": [{"owner": h.get("owner"), "pct": fnum(h.get("pct"), 0)} for h in nonpool[:10]]}


def goplus_facts(mint: str) -> dict:
    g = cd.goplus_solana(mint)
    if not g.ok:
        return {"ok": False, "error": g.error[:120]}
    res = ((g.data or {}).get("result") or {})
    item = res.get(mint) or (next(iter(res.values())) if res else {})
    if not item:
        return {"ok": False, "error": "GoPlus returned no result"}

    def st(k):
        v = item.get(k)
        return (v or {}).get("status") if isinstance(v, dict) else v
    bad = []
    if str(st("mintable")) == "1":
        bad.append("mintable")
    if str(st("freezable")) == "1":
        bad.append("freezable")
    if str(st("balance_mutable_authority")) == "1":
        bad.append("balance mutable (permanent delegate)")
    if item.get("transfer_hook"):
        bad.append("transfer hook")
    tf = item.get("transfer_fee") or {}
    if isinstance(tf, dict) and fnum(tf.get("current_fee_rate"), 0):
        bad.append(f"transfer fee {tf.get('current_fee_rate')}")
    if str(st("non_transferable")) == "1":
        bad.append("non-transferable")
    if str(st("closable")) == "1":
        bad.append("closable")
    return {"ok": True, "flags": bad, "trusted_token": item.get("trusted_token")}


# ---------------------------------------------------------------- costs (Jupiter quotes, read-only)
def round_trip(mint: str, usd: float, sol_usd: float) -> dict:
    """SOL->token->SOL (or USDC->SOL->USDC for SOL itself). pct = pool fees + impact at quote time."""
    if mint == SOL:
        amt_in = int(usd * 1e6)
        buy = cd.jupiter_quote(USDC, SOL, amt_in, 50)
        if not buy.ok:
            return {"ok": False, "error": f"buy quote {buy.status}: {buy.error[:80]}"}
        sell = cd.jupiter_quote(SOL, USDC, int(buy.data["outAmount"]), 50)
    else:
        amt_in = int(usd / sol_usd * 1e9)
        buy = cd.jupiter_quote(SOL, mint, amt_in, 100)
        if not buy.ok:
            return {"ok": False, "error": f"buy quote {buy.status}: {buy.error[:80]}"}
        sell = cd.jupiter_quote(mint, SOL, int(buy.data["outAmount"]), 100)
    if not sell.ok:
        return {"ok": False, "error": f"SELL quote failed {sell.status}: {sell.error[:80]} (possible honeypot/no route)"}
    back = int(sell.data["outAmount"])
    pct = (1 - back / amt_in) * 100
    return {"ok": True, "pct_pool_and_impact": round(pct, 4),
            "buy_impact_pct": round(abs(fnum(buy.data.get("priceImpactPct"), 0)) * 100, 4),
            "sell_impact_pct": round(abs(fnum(sell.data.get("priceImpactPct"), 0)) * 100, 4),
            "routes": [x.get("swapInfo", {}).get("label") for x in (buy.data.get("routePlan") or [])],
            "endpoint": buy.data.get("_endpoint"), "tokens_out": int(buy.data["outAmount"])}


def venue_costs(rt: dict, usd: float, liq: float, cls: str) -> dict:
    exp_slip = 0.1 if liq >= 250_000 else 0.25  # expected slippage per leg (quote-to-fill), both ways counted
    base = rt["pct_pool_and_impact"] + 2 * exp_slip
    phantom = base + 2 * PHANTOM_FEE_PCT + PHANTOM_FIXED_USD / usd * 100
    phantom_excl_buy_slip = phantom - exp_slip
    slip_tol = min(CLASS_SLIP_CAP[cls], 3.0 - phantom_excl_buy_slip)

    def fomo_fee(v):
        return 0.10 if v < 5 else (v * 0.02 if v < 47.5 else (0.95 if v < 190 else v * 0.005))
    fomo = base + (fomo_fee(usd) + max(fomo_fee(usd), fomo_fee(usd * 0.75))) / usd * 100
    fomo_allow = fomo - exp_slip + 1.0  # FOMO slippage setting unverified -> 1% buy-slippage allowance (§2E.8)
    return {"size_usd": usd, "expected_slippage_per_leg_pct": exp_slip, "phantom_app_pct": round(phantom, 3),
            "phantom_buy_slippage_tolerance_pct": round(slip_tol, 2),
            "phantom_ok": phantom <= 3.0 and phantom_excl_buy_slip <= 2.7 and slip_tol >= 0.3,
            "fomo_pct_at_size": round(fomo, 3), "fomo_pct_with_1pct_allowance": round(fomo_allow, 3),
            "fomo_ok": fomo_allow <= 3.0}


# ---------------------------------------------------------------- news
def news_context(log: list) -> dict:
    try:
        items, errs = cn.fetch_all(True)
    except Exception as exc:  # news is optional context; failure never enables a buy
        log.append({"source": "news feeds", "ok": False, "note": repr(exc)[:120]})
        return {"items": [], "errors": [repr(exc)], "health": {}}
    cutoff = time.time() - 48 * 3600
    items = [i for i in items if not i["_ts"] or i["_ts"] >= cutoff]
    log.append({"source": "news feeds (RSS/status)", "ok": True, "items_48h": len(items),
                "note": "; ".join(errs)[:200]})
    return {"items": items, "errors": errs, "health": dict(cn.FEED_HEALTH)}


def news_hits(news: dict, name: str, sym: str) -> dict:
    terms = [t for t in {name or "", sym or "", f"${sym}" if sym else ""} if t and len(t) >= 3]
    hits, danger = [], []
    for it in news["items"]:
        hay = f" {it['title']} {it['summary']} ".lower()
        if any(re.search(r"(?<![a-z0-9])" + re.escape(t.lower()) + r"(?![a-z0-9])", hay) for t in terms):
            dw = [k for k in DANGER_WORDS if k in hay]
            rec = {"title": it["title"][:140], "source": it["source"], "time_et": it["time_et"], "danger": dw,
                   "weight": it.get("weight", 1.0), "possible_injection": it["possible_injection"]}
            hits.append(rec)
            if dw and it.get("weight", 1.0) >= 0.5 and not it["possible_injection"]:
                danger.append(rec)
    return {"mentions": len(hits), "danger": danger[:5], "sample": hits[:3]}


# ---------------------------------------------------------------- evaluation
def _jstat(jt: dict, win: str, key: str):
    return fnum(((jt or {}).get(f"stats{win}") or {}).get(key))


def evaluate(mint, pairs, sources, ctx, cfg, log, jt: dict | None = None) -> dict:
    """Stage A (cheap) gates from DexScreener (main pool) and Jupiter Tokens v2 (token-wide)."""
    p = main_pair(pairs)
    jt = jt or {}
    rec = {"mint": mint, "sources": sorted(sources), "checks": [], "status": None}
    fails = []

    def chk(cid, ok, value, note=""):
        rec["checks"].append({"id": cid, "pass": ok, "value": value, "note": note})
        if ok is False or ok is None:
            fails.append(f"{cid}: {note or value}")
        return ok

    if not p and not jt:
        rec.update(status="REJECT", reason="no DexScreener pair and no Jupiter token data (no executable pool data)")
        return rec
    bt = (p or {}).get("baseToken") or {}
    sym = bt.get("symbol") or jt.get("symbol") or "?"
    name = bt.get("name") or jt.get("name") or "?"
    ds_liq = fnum(((p or {}).get("liquidity") or {}).get("usd"))
    j_liq = fnum(jt.get("liquidity"))
    liq = ds_liq if ds_liq is not None else (j_liq or 0)
    j_vol = (_jstat(jt, "24h", "buyVolume") or 0) + (_jstat(jt, "24h", "sellVolume") or 0) if jt.get("stats24h") else None
    vol24 = fnum(((p or {}).get("volume") or {}).get("h24"))
    vol24 = vol24 if vol24 is not None else (j_vol or 0)
    pc = dict((p or {}).get("priceChange") or {})
    for win, k in (("1h", "h1"), ("6h", "h6"), ("24h", "h24"), ("5m", "m5")):
        if pc.get(k) is None and _jstat(jt, win, "priceChange") is not None:
            pc[k] = round(_jstat(jt, win, "priceChange"), 2)
    created_ms = (p or {}).get("pairCreatedAt")
    grad = jt.get("graduatedAt")
    ts_iso = grad or ((jt.get("firstPool") or {}).get("createdAt"))
    if not created_ms and ts_iso:
        try:
            created_ms = datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            created_ms = None
    if grad:  # G10 counts from migration when known
        try:
            created_ms = max(created_ms or 0, datetime.fromisoformat(grad.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            pass
    age_min = (time.time() * 1000 - created_ms) / 60000 if created_ms else None
    mcap = fnum(jt.get("mcap")) or fnum((p or {}).get("marketCap")) or fnum((p or {}).get("fdv"))
    price = fnum(jt.get("usdPrice")) or fnum((p or {}).get("priceUsd"))
    ds_price = fnum((p or {}).get("priceUsd"))
    txh24 = ((p or {}).get("txns") or {}).get("h24") or {}
    rec.update(symbol=sym, name=name, dex=(p or {}).get("dexId") or "unknown (DexScreener unavailable)",
               pair=(p or {}).get("pairAddress"), price_usd=price, price_ds=ds_price, market_cap=mcap,
               liquidity_usd=liq, liquidity_token_total=j_liq, volume_24h=vol24, price_change=pc,
               pool_age_h=round(age_min / 60, 1) if age_min else None, txns_24h=txh24, boosted=bool((p or {}).get("boosts")),
               ds_available=bool(p), holders_jup=jt.get("holderCount"), organic=jt.get("organicScoreLabel"),
               organic_score=fnum(jt.get("organicScore")), liq_change_1h=_jstat(jt, "1h", "liquidityChange"),
               holder_change_1h=_jstat(jt, "1h", "holderChange"), holder_change_24h=_jstat(jt, "24h", "holderChange"),
               jup_audit=jt.get("audit"), jup_verified=bool(jt.get("isVerified")), launchpad=jt.get("launchpad"),
               graduated_at=grad,
               socials=[s.get("url") for s in (((p or {}).get("info") or {}).get("socials") or []) if isinstance(s, dict)]
               + [u for u in (jt.get("telegram"), jt.get("twitter")) if isinstance(u, str) and u.startswith("http")])
    rec["_pair"] = p
    if ds_price and fnum(jt.get("usdPrice")) and abs(ds_price / fnum(jt["usdPrice"]) - 1) > 0.03:
        rec["price_mismatch_pct"] = round((ds_price / fnum(jt["usdPrice"]) - 1) * 100, 2)
    major_sym = next((s for s, m in MAJORS.items() if m == mint), None)
    established = bool(major_sym) or ((age_min or 0) > 365 * 24 * 60 and (mcap or 0) >= 3e8)
    cls = "major" if (mcap or 0) >= 1e9 and established else ("mid" if (mcap or 0) >= 1e8 else "memecoin")
    rec.update(established=established, cls=cls, major_symbol=major_sym)

    if mint in cfg.get("owner_mints", []):
        chk("G11 owner's own coin", False, "owner mint list", "owner's own coin: never traded")
    on_curve = (p or {}).get("dexId") == "pumpfun" or (bool(pairs) and all(x.get("dexId") == "pumpfun" for x in pairs)) \
        or (bool(jt.get("launchpad")) and not jt.get("graduatedPool") and not established and not pairs)
    chk("G10 not on bonding curve", not on_curve, (p or {}).get("dexId") or jt.get("launchpad"),
        "un-graduated launchpad token (bonding curve)")
    chk("G10 migration wait", age_min is not None and age_min >= cfg["migration_wait_min"],
        f"{age_min:.0f} min" if age_min else "UNKNOWN",
        "pool/migration younger than MIGRATION_WAIT" if age_min is not None else "pool creation time UNKNOWN")
    chk("liquidity >= min (scanner)", liq >= cfg["min_liquidity_usd"], f"${liq:,.0f}", f"liquidity ${liq:,.0f} < ${cfg['min_liquidity_usd']:,}")
    chk("24h volume >= min (scanner)", vol24 >= cfg["min_volume_24h_usd"], f"${vol24:,.0f}", f"24h volume ${vol24:,.0f} < ${cfg['min_volume_24h_usd']:,}")
    c2, c3, pa_conf = classify_price_action(pc)
    rec.update(c2=c2, c3=c3)
    chk("C2 price action not BLOW-OFF/DISTRIBUTION/COLLAPSE", c2 not in ("BLOW-OFF", "DISTRIBUTION", "COLLAPSE"), c2,
        f"C2 {c2} (1h {pc.get('h1')}%, 6h {pc.get('h6')}%, 24h {pc.get('h24')}%)")
    chk("C3 entry not CHASE", c3 != "CHASE", c3, f"C3 CHASE (1h {pc.get('h1')}%, 6h {pc.get('h6')}%)")
    ntx = (txh24.get("buys") or 0) + (txh24.get("sells") or 0)
    if not ntx and jt.get("stats24h"):
        ntx = int((_jstat(jt, "24h", "numBuys") or 0) + (_jstat(jt, "24h", "numSells") or 0))
    turnover = vol24 / liq if liq else 0
    org = (jt.get("organicScoreLabel") or "").lower()
    wash_p = 0.10
    if not established and (turnover > 40 or (ntx and vol24 / ntx > 5000)):
        wash_p = 0.35
    if not established and org == "low":
        wash_p = max(wash_p, 0.35)
    elif org == "medium":
        wash_p = max(wash_p, 0.20)
    rec["t9_wash_p"] = wash_p
    chk("T9 fake/wash volume p < 0.30 (judgment)", wash_p < 0.30,
        f"turnover {turnover:.1f}x, avg trade ${vol24 / ntx if ntx else 0:,.0f}, Jupiter organic {org or '?'}",
        f"wash-volume suspicion p={wash_p} (turnover {turnover:.1f}x liquidity, Jupiter organic score {org or 'UNKNOWN'})")
    if fails:
        rec.update(status="REJECT", reason=fails[0], all_failures=fails)
        return rec
    rec["_stageA_ok"] = True
    return rec


def deep_checks(rec, ctx, cfg, log) -> dict:
    fails = []

    def chk(cid, ok, value, note=""):
        rec["checks"].append({"id": cid, "pass": ok, "value": value, "note": note})
        if ok is False or ok is None:
            fails.append(f"{cid}: {note or value}")
        return ok
    mint, sym = rec["mint"], rec["symbol"]
    rc = rugcheck_facts(mint, rec["pair"], cfg)
    rec["rugcheck"] = rc
    if not rc["ok"]:
        chk("T11 RugCheck report available", None, "UNKNOWN", rc["error"] + " (UNKNOWN = VETO)")
    else:
        norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())  # noqa: E731
        verified = rc["mint"] == mint and norm(rc["symbol"]) == norm(sym)
        if rec.get("major_symbol"):
            verified = verified and norm(sym) in (norm(rec["major_symbol"]), "wsol", "sol")
        rec["address_verified"] = verified
        rec["verified_against"] = ("DexScreener pair baseToken + RugCheck report" if rec.get("ds_available")
                                   else "Jupiter Tokens v2 + RugCheck report (DexScreener unavailable this run)")
        if not rec.get("pair"):
            rec["pair"] = rc.get("main_pool")
            rec["dex"] = f"{rc.get('main_pool_type')} (RugCheck deepest market)"
        chk("T1 mint matches pair source + RugCheck", verified, f"pair {sym} / RC {rc['symbol']}",
            "mint/symbol mismatch between DexScreener/Jupiter and RugCheck")
        chk("G10 not on bonding curve (RugCheck markets)", not rc["only_bonding_curve_market"], rc.get("main_pool_type"),
            "only a pump.fun bonding-curve market exists")
        if mint != SOL:
            chk("T2 mint authority revoked", not rc["mint_authority"], rc["mint_authority"] or "none", "mint authority ACTIVE")
            chk("T3 freeze authority revoked", not rc["freeze_authority"], rc["freeze_authority"] or "none", "freeze authority ACTIVE")
        chk("T5 no hostile Token-2022 extension", not rc["hostile_extensions"], rc["hostile_extensions"] or "none",
            ", ".join(rc["hostile_extensions"]))
        lp = rc["lp_locked_main_pct"] if rc["lp_locked_main_pct"] is not None else rc["lp_locked_max_pct"]
        diversified = rec["established"] and (rc["markets"] or 0) >= 5 and (rc["lp_providers"] or 0) >= 50
        if mint == SOL:
            chk("T4 LP locked/burned", True, "n/a (SOL)", "")
        elif lp is not None and lp >= 90:
            chk("T4 LP locked/burned", True, f"{lp:.0f}% (main pool)")
        elif diversified:
            chk("T4 LP locked/burned", True, f"{lp or 0:.0f}% locked; {rc['markets']} markets / {rc['lp_providers']} LP providers",
                "INTERPRETATION: established token with diversified multi-venue liquidity; LP lock not applicable (flagged)")
        else:
            chk("T4 LP locked/burned", False, f"{lp}%", f"main-pool LP only {lp}% locked/burned")
        limit = cfg["t6_top10_limit_established_pct"] if rec["established"] else cfg["t6_top10_limit_pct"]
        t10 = rc["top10_nonpool_pct"]
        if mint != SOL:
            chk(f"T6 top-10 non-pool holders <= {limit}%", t10 is not None and t10 <= limit, f"{t10}%",
                f"top-10 non-pool holders {t10}% > {limit}%" if t10 is not None else "holders UNKNOWN")
        cl = rc["cluster_pct"]
        t7 = "OK"
        if cl is None:
            chk("T7 cluster/bundle", None, "UNKNOWN", "cluster data UNKNOWN = VETO")
        elif cl > cfg["t7_cluster_veto_pct"]:
            chk("T7 cluster/bundle", False, f"{cl}%", f"insider cluster {cl}% > {cfg['t7_cluster_veto_pct']}%")
        else:
            t7 = "ELEVATED" if cl >= cfg["t7_cluster_elevated_pct"] else "OK"
            chk("T7 cluster/bundle", True, f"{cl}% ({rc['insider_networks']} networks) {t7}")
        rec["t7_state"] = t7
        danger = [r["name"] for r in rc["risks"] if (r.get("level") or "").lower() == "danger"]
        chk("T11 RugCheck not dangerous", not danger and not rc["rugged"], danger or "no danger-level risks",
            f"RugCheck danger: {', '.join(danger) or 'rugged'}")
        if mint != SOL:
            h = rc["holders"] or 0
            chk("G10 holders >= 100 (current non-pool proxy)", h >= cfg["min_holders"], h, f"only {h} holders")
        creators = set(cfg.get("owner_addresses", []))
        chk("G11 not the owner's coin", not (rc["creator"] and rc["creator"] in creators), rc["creator"] or "unknown creator",
            "creator is an owner-shared address")
    if not fails and mint != SOL:
        gp = goplus_facts(mint)
        rec["goplus"] = gp
        if not gp["ok"]:
            chk("T11 GoPlus available", None, "UNKNOWN", f"GoPlus {gp['error']} (UNKNOWN = VETO per T11)")
        else:
            chk("T11/T5 GoPlus clean (honeypot/tax/authority)", not gp["flags"], gp["flags"] or "clean", ", ".join(gp["flags"]))
    if not fails:
        rt = round_trip(mint, cfg["quote_usd"], ctx["sol_usd"])
        rec["jupiter"] = rt
        if not rt["ok"]:
            chk("G7 sell route / honeypot check", False, "no route", rt["error"])
        else:
            worst = max(rt["buy_impact_pct"], rt["sell_impact_pct"])
            chk("T10 impact per leg <= 3%% at $%d" % cfg["quote_usd"], worst <= cfg["max_impact_per_leg_pct"], f"{worst:.2f}%",
                f"impact {worst:.2f}% per leg at ${cfg['quote_usd']}")
            vc = venue_costs(rt, cfg["quote_usd"], rec["liquidity_usd"], rec["cls"])
            rec["costs"] = vc
            chk("H17 round trip <= 3% (Phantom app)", vc["phantom_ok"], f"{vc['phantom_app_pct']:.2f}%",
                f"Phantom app round trip {vc['phantom_app_pct']:.2f}% (or buy-slippage room < 0.3%)")
    news = news_hits(ctx["news"], rec["name"], sym)
    rec["news"] = news
    chk("news danger-word scan (veto only)", not news["danger"], f"{news['mentions']} mentions",
        "danger words in recent news: " + "; ".join(f"{d['danger']} {d['title'][:60]}" for d in news["danger"][:2]))
    if fails:
        rec.update(status="REJECT", reason=fails[0], all_failures=fails)
    return rec


def score(rec, ctx, cfg, log) -> dict:
    rc, vc, rt = rec["rugcheck"], rec["costs"], rec["jupiter"]
    liq = rec["liquidity_usd"]
    trend = depth_trend(ctx["snaps"], rec["mint"], liq)
    if not trend["evidence"] and rec.get("liq_change_1h") is not None:
        ch = rec["liq_change_1h"]
        trend = {"evidence": True, "source": "Jupiter Tokens v2 stats1h.liquidityChange", "change_pct": round(ch, 2),
                 "stable": ch >= -5, "growing": ch >= 2}
    rec["depth_trend"] = trend
    worst = max(rt["buy_impact_pct"], rt["sell_impact_pct"])
    lp = rc.get("lp_locked_main_pct") or 0
    if worst > 3:
        s1 = 1
    elif not trend["evidence"] or not trend["stable"] or liq < 250_000:
        s1 = 2
    elif worst <= 1 and trend.get("stable") and (lp >= 90):
        s1 = 4
    else:
        s1 = 3
    s1_conf = 0.9 if trend["evidence"] else 0.7
    t10, cl, h = rc.get("top10_nonpool_pct") or 100, rc.get("cluster_pct") or 0, rc.get("holders") or 0
    if rec["mint"] == SOL:
        s2, s2_conf = 3, 0.8
    elif cl >= 30:
        s2, s2_conf = 0, 0.8
    elif cl >= 15:
        s2, s2_conf = 1, 0.8
    elif t10 < 15 and cl < 5 and h >= 5000 and (rec.get("holder_change_24h") or 0) > 0 and rec.get("organic") == "high":
        s2, s2_conf = 4, 0.8  # broad, organic (Jupiter organic score high), growing (24h holder change > 0)
    elif t10 < 15 and cl < 5 and h >= 5000:
        s2, s2_conf = 3, 0.8
    elif t10 < 20 and cl < 5 and h >= 1000:
        s2, s2_conf = 3, 0.75
    else:
        s2, s2_conf = 2, 0.75
    # S3 smart money (prompt §2C/§3.2) from smart_money.py (Helius, read-only). Calibration, kept honest:
    #  - The prompt's weights are NOT changed: S3 still enters COMPOSITE as 0.20 * S3/4, so its contribution is one
    #    of {0, 0.05, 0.10, 0.15, 0.20}. The 0.75 bar is unchanged.
    #  - Levels follow the prompt's definitions (1 = one unproven wallet, 2 = one reliable, 3 = two+ independent
    #    reliable, 4 = three+ independent high-reliability with fresh entries). "Reliable" uses the prompt's own bar
    #    (500+ swaps over 90 days, win rate > 50%, measured from on-chain history), so wallets that only meet the
    #    task's minimum sample (>= 15 closed round trips / 30 d) can lift S3 to 1 at most (+0.05).
    #  - Only wallets that bought in the lookback window AND still hold count; exits do not add points.
    #  - Negative on-chain signals (dev/deployer sold or moved tokens out, fresh-wallet concentration >= 15% among
    #    top non-pool holders, more tracked wallets exiting than entering) force S3 = 0 AND are added to `needs`
    #    as blockers (stricter than before; the prompt allows changes that only make buying stricter).
    #  - Missing data (no key, empty watchlist, fetch failures) => S3 = 0 with confidence 0.5, exactly as before
    #    ("UNKNOWN = level 0"). S3's confidence feeds decision_confidence = min(all confidences), unchanged.
    s3, s3_conf, sm_sig = 0, 0.5, None
    if ctx.get("sm_recent") is not None and sm is not None:
        try:
            sm_sig = sm.token_signal(rec["mint"], ctx["sm_recent"], hours=ctx["sm_recent"].get("hours", 24),
                                     deployer=rc.get("creator"), holders=rc.get("top_nonpool_holders"),
                                     established=rec["established"] or rec["mint"] == SOL)
        except Exception as exc:  # never let the optional factor break a scan
            sm_sig = {"status": "unknown", "score": None, "level": None, "confidence": 0.5, "negative_flags": [],
                      "insufficient_data": True, "explanation": f"UNKNOWN: smart_money error {type(exc).__name__}: {exc}"[:200]}
        if sm_sig.get("status") == "ok" and sm_sig.get("level") is not None:
            s3, s3_conf = int(sm_sig["level"]), float(sm_sig.get("confidence") or 0.5)
            if sm_sig.get("negative_flags"):
                s3 = 0  # defensive: negative on-chain signals never add points
    rec["smart_money"] = sm_sig or {"status": "unknown", "score": None, "level": None, "insufficient_data": True,
                                    "explanation": ctx.get("sm_note") or "UNKNOWN: smart-money factor disabled or unavailable"}
    # S4 social momentum from social_data.py (keyless: Reddit search RSS, Telegram public previews from the token's
    # own listed links, crypto_news mention velocity, CoinGecko/GeckoTerminal/Jupiter trending; paid DexScreener
    # boosts/ads and bot-like patterns are NEGATIVES). Unreadable -> UNKNOWN = 0 at confidence 0.5, as before.
    s4, s4_conf, soc_sig = 0, 0.5, None
    if soc is not None and cfg.get("social_enabled", True):
        try:
            soc_sig = soc.token_social("solana", rec["mint"], rec["symbol"], rec["name"], pair=rec.get("_pair"),
                                       sources=rec["sources"], news=ctx["news"], cg_trending=ctx.get("cg_trending"),
                                       snaps=ctx.setdefault("social_snaps", {}), updates=ctx.setdefault("social_updates", {}),
                                       established=rec["established"], mcap=rec.get("market_cap"), extra_links=rec.get("socials"),
                                       use_reddit=ctx.get("reddit_budget", 0) > 0)
            if ctx.get("reddit_budget", 0) > 0:
                ctx["reddit_budget"] -= 1
        except Exception as exc:  # optional factor: an error is UNKNOWN, never a positive
            soc_sig = {"status": "unknown", "level": 0, "confidence": 0.5, "negative_flags": [],
                       "explanation": f"UNKNOWN: social error {type(exc).__name__}"}
        if soc_sig.get("status") == "ok":
            s4, s4_conf = int(soc_sig["level"]), float(soc_sig["confidence"])
    rec["social"] = soc_sig or {"status": "unknown", "level": None, "explanation": "UNKNOWN: social factor disabled"}
    c1, e, c1_conf = trend_stage((rec.get("pool_age_h") or 0), rec["price_change"], rec["established"])
    candles, src = ohlcv_hourly(rec.get("major_symbol"), rec["pair"], log)
    if nq is not None:  # RSI(14)/EMA(12/26) state on CLOSED hourly candles: a SHADOW feature only (rec["narrow"])
        try:
            rec["ta"] = nq.ta_state(candles, 3600)
        except Exception:  # noqa: BLE001
            rec["ta"] = None
    vol_d = realized_vol_daily(candles)
    b, stop_note = choose_stop(rec["cls"], vol_d, rec["price_change"])
    pw = p_win(b, vc["phantom_app_pct"], candles, cfg["backtest_horizon_h"], cfg["prior_strength"], cfg["entry_bar"])
    pw.update(candles=len(candles), candle_source=src, stop_method=stop_note,
              daily_vol_pct=round(vol_d * 100, 2) if vol_d else None)
    c5 = "BUY" if (s1 >= 3 and s2 >= 3 and rec["c2"] in ("ACCUMULATION", "HEALTHY EXPANSION", "BREAKOUT")) else "WATCH"
    # Confidences: evidence-based (scoring.py) instead of the old constants C2 0.65 / C3 0.65 / C5 0.60, which made
    # DECISION CONFIDENCE (= min, prompt §2C.5) structurally unable to reach 0.75. Aggregation follows the prompt
    # (fixed weights, UNKNOWN = 0, min confidence) unless scan_config scoring_mode = "renormalized" (owner approval).
    c2_conf, c3_conf, pa_note = scoring.price_action_confidence(classify_price_action, rec["price_change"], candles, rec["c2"], rec["c3"])
    c1_conf, c1_note = scoring.trend_confidence(c1, c1_conf, candles)
    confs = {"S1": s1_conf, "S2": s2_conf, "S3": s3_conf, "S4": s4_conf, "C1": c1_conf, "C2": c2_conf, "C3": c3_conf}
    confs["C5"] = scoring.c5_confidence(confs)
    known = {"S3": (sm_sig or {}).get("status") == "ok", "S4": (soc_sig or {}).get("status") == "ok"}
    agg = scoring.aggregate({"S1": s1, "S2": s2, "S3": s3, "S4": s4}, confs, known, e, cfg["entry_bar"], cfg.get("scoring_mode", "prompt"))
    composite, dconf = agg["gate_composite"], agg["gate_confidence"]
    rec["scores"] = {"S1": s1, "S2": s2, "S3": s3, "S4": s4, "C1": c1, "E": e, "C2": rec["c2"], "C3": rec["c3"], "C5": c5,
                     "confidences": confs, "confidence_notes": {"C1": c1_note, "C2/C3": pa_note},
                     "decision_confidence": dconf, "composite": round(composite, 4),
                     "floors_ok": s1 >= 3 and s2 >= 3, "aggregation": agg}
    rec["p_win"] = pw
    bar = cfg["entry_bar"]
    needs = []
    if ctx["crash"].get("crash") or not ctx["crash"].get("ok"):
        needs.append("G2: SOL crash regime active or UNKNOWN")
    if s1 < 3:
        needs.append("S1 >= 3 (" + ("needs a liquidity snapshot 20-90 min old showing stable depth" if not trend["evidence"]
                                    else f"depth change {trend.get('change_pct')}% / liquidity ${liq:,.0f}") + ")")
    if s2 < 3:
        needs.append(f"S2 >= 3 (top-10 {t10}%, cluster {cl}%, holders {h})")
    for flag in (sm_sig or {}).get("negative_flags") or []:
        needs.append(f"smart-money NEGATIVE signal must clear: {flag}")
    for flag in (soc_sig or {}).get("negative_flags") or []:
        needs.append(f"social NEGATIVE: {flag}")
    if composite < bar:
        needs.append(f"COMPOSITE {composite:.2f} < {bar} (S3 {'measured ' + str(s3) if known['S3'] else 'UNKNOWN = 0'}, "
                     f"S4 {'measured ' + str(s4) if known['S4'] else 'UNKNOWN = 0'}; max reachable now "
                     f"{agg['max_composite_if_unknown_stay_0']:.2f}; shadow adjusted {agg['composite_adjusted']:.2f})")
    if dconf < bar:
        needs.append(f"decision confidence {dconf:.2f} < {bar} (lowest: {agg['confidence_set_by']}; shadow adjusted {agg['confidence_adjusted']:.2f})")
    if pw["estimate"] < bar:
        needs.append(f"P_WIN {pw['estimate']:.2f} < {bar} (90% {pw['posterior']['lo90']:.2f}-{pw['posterior']['hi90']:.2f})")
    if c5 != "BUY":
        needs.append("C5 = BUY")
    rec["needs"] = needs
    rec["status"] = "PASS" if not needs else "WATCH"
    ref = rec["price_usd"] or 0
    slip = vc["phantom_buy_slippage_tolerance_pct"]
    stop_px = ref * (1 - b)
    rec["plan"] = {"reference_price": ref, "entry_high": ref * (1 + slip / 100), "entry_low": max(stop_px, ref * 0.95),
                   "stop": stop_px, "tp1": ref * (1 + pw["tp1_pct"] / 100), "tp2": ref * (1 + pw["tp2_pct"] / 100),
                   "buy_slippage_pct": slip, "permitted_max_pct_equity": round(min(10.0, 2.0 / b), 2),
                   "size_cap_usd": 100, "t7_half": rec.get("t7_state") == "ELEVATED",
                   "venue": "Phantom app" if vc["phantom_ok"] else "none (cost)",
                   "fomo": "OK" if vc["fomo_ok"] else f"FOMO not viable at ${cfg['quote_usd']} ({vc['fomo_pct_with_1pct_allowance']:.2f}% with 1% slippage allowance)"}
    return rec


# ---------------------------------------------------------------- run
def run(cfg: dict, snapshot_only: bool = False) -> dict:
    t0 = now_et()
    log: list = []
    cands = discover(cfg, log)
    cands = {m: s for m, s in cands.items() if m not in EXCLUDE or m == SOL}
    pairs = dexscreener_batch(list(cands), log)
    jts = jupiter_tokens(list(cands), log)
    log.append({"source": "dexscreener tokens/v1 (pairs for candidates)", "ok": bool(pairs), "status": "-",
                "solana": len(pairs), "note": "" if pairs else "no DexScreener pair data (rate-limited?)"})
    log.append({"source": "jupiter tokens/v2 (token data for candidates)", "ok": bool(jts), "status": "-", "solana": len(jts)})
    snaps = load_snaps()
    liq_now = {m: fnum((main_pair(ps) or {}).get("liquidity", {}).get("usd"), 0) for m, ps in pairs.items()}
    if snapshot_only:
        save_snaps(snaps, liq_now)
        return {"snapshot_at": t0.isoformat(), "tokens": len(liq_now), "sources": log}
    crash = cd.sol_crash_regime()
    sol_usd = fnum((jts.get(SOL) or {}).get("usdPrice"))
    if not sol_usd:
        jp = cd.jupiter_price([SOL])
        sol_usd = fnum(((jp.data or {}).get(SOL) or {}).get("usdPrice")) if jp.ok else None
    if not sol_usd:
        dl = cd.defillama_prices(["solana:" + SOL])
        sol_usd = fnum((((dl.data or {}).get("coins") or {}).get("solana:" + SOL) or {}).get("price")) if dl.ok else None
    sol_usd = sol_usd or fnum(crash.get("price_now"))
    news = news_context(log)
    ctx = {"crash": crash, "sol_usd": sol_usd, "news": news, "snaps": snaps, "sm_recent": None, "sm_note": None}
    if soc is not None and cfg.get("social_enabled", True):
        ctx["social_snaps"] = soc.load_snaps()
        ctx["social_updates"] = {}
        ctx["cg_trending"] = None   # fetched after discovery (CoinGecko shares GeckoTerminal's tight per-IP budget)
        ctx["reddit_budget"] = int(cfg.get("reddit_max_solana", 6))
    # S3: fetch every tracked wallet's recent swaps ONCE per scan (per-token lookups are then local + a few calls).
    if cfg.get("smart_money_enabled", True) and sm is not None and sm.helius_key_present():
        tw = sm.tracked_wallets()
        if tw:
            rc_ = sm.prefetch_recent(cfg.get("smart_money_lookback_h", 24), tw, sol_usd=sol_usd)
            ctx["sm_recent"] = rc_ if rc_.get("ok") else None
            ctx["sm_note"] = None if rc_.get("ok") else f"UNKNOWN: tracked-wallet fetch failed ({rc_.get('error') or rc_.get('failed', [{}])[:1]})"
            log.append({"source": "helius smart-money prefetch (tracked wallets' recent swaps)", "ok": bool(rc_.get("ok")),
                        "status": "-", "solana": len(tw), "note": f"coverage {rc_.get('coverage')}, window {rc_.get('hours')} h"})
        else:
            ctx["sm_note"] = "UNKNOWN: smart-wallet watchlist is empty (run smart_money.py build)"
    else:
        ctx["sm_note"] = "UNKNOWN: smart-money factor disabled (config) or no Helius key"
    results, rejects = [], []
    stage_a = []
    for m, srcs in cands.items():
        ps = pairs.get(m, [])
        base_sym = ((main_pair(ps) or {}).get("baseToken") or {}).get("symbol", "") or (jts.get(m) or {}).get("symbol", "")
        if m != SOL and STABLE_SYMS.match(base_sym or "") and "major_list" not in srcs:
            continue
        rec = evaluate(m, ps, srcs, ctx, cfg, log, jts.get(m))
        (stage_a if rec.get("_stageA_ok") else rejects).append(rec)
    stage_a.sort(key=lambda r: ("major_list" in r["sources"], math.log10(max(r["liquidity_usd"], 1)) + math.log10(max(r["volume_24h"], 1))), reverse=True)
    deep = stage_a[:cfg["deep_check_limit"]]
    for r in stage_a[cfg["deep_check_limit"]:]:
        r.update(status="NOT DEEP-CHECKED", reason=f"ranked below the top {cfg['deep_check_limit']} for deep checks this run")
        rejects.append(r)
    if not sol_usd:
        for r in deep:
            r.update(status="REJECT", reason="no SOL price (cannot size quotes)")
            rejects.append(r)
        deep = []
    if soc is not None and cfg.get("social_enabled", True) and ctx.get("cg_trending") is None:
        ctx["cg_trending"] = soc.coingecko_trending_contracts(log)
    for r in deep:
        try:   # one bad record must never kill the whole (scheduled) scan; it is rejected with the error instead
            deep_checks(r, ctx, cfg, log)
            if r.get("status") == "REJECT":
                rejects.append(r)
                continue
            score(r, ctx, cfg, log)
        except Exception as e:  # noqa: BLE001
            r.update(status="REJECT", reason=f"internal error during checks/scoring: {type(e).__name__}: {str(e)[:160]}")
            rejects.append(r)
            continue
        results.append(r)
    save_snaps(snaps, liq_now)
    for r in results + rejects:
        r.pop("_stageA_ok", None)
        r.pop("_pair", None)
        r.setdefault("chain", "solana")
        r.setdefault("caip2", "solana:mainnet")
        r.setdefault("venues", ccm.venues_for("solana"))
        r.setdefault("phantom_auto_chain", True)
    # ---- EVM chains (manual FOMO cards on every FOMO chain; Phantom autonomous only on Base/Ethereum), run AFTER
    # Solana and strictly one chain at a time through the shared limiter, reusing the news + social context.
    evm_res = None
    evm_chains = [c for c in cfg.get("manual_card_chains", ["solana"]) if c != "solana"]
    if evm_data is not None and evm_chains and cfg.get("multichain_enabled", True):
        try:
            evm_cfg = dict({k: cfg[k] for k in ("min_liquidity_usd", "min_volume_24h_usd", "migration_wait_min", "min_holders",
                                                 "t6_top10_limit_pct", "t6_top10_limit_established_pct", "t7_cluster_veto_pct",
                                                 "t7_cluster_elevated_pct", "max_round_trip_pct", "max_impact_per_leg_pct",
                                                 "quote_usd", "backtest_horizon_h", "prior_strength", "entry_bar", "owner_mints") if k in cfg},
                           **(cfg.get("evm") or {}), scoring_mode=cfg.get("scoring_mode", "prompt"),
                           social_enabled=cfg.get("social_enabled", True))
            evm_cfg["owner_addresses"] = list(cfg.get("owner_addresses", [])) + list((cfg.get("evm") or {}).get("owner_addresses", []))
            shared = {k: ctx[k] for k in ("social_snaps", "social_updates", "cg_trending") if k in ctx}
            shared["reddit_budget"] = int(cfg.get("reddit_max_evm", 8))
            esm = evm_smart_money if cfg.get("evm_smart_money_enabled", True) else None
            evm_res = evm_data.scan(evm_chains, evm_cfg, news={"items": news["items"], "errors": news.get("errors", []),
                                                                "danger_words": DANGER_WORDS},
                                    limit_per_chain=int(cfg.get("evm_limit_per_chain", 40)), smart_money=esm, shared_ctx=shared)
            results += evm_res["passes"] + evm_res["watch"]
            rejects += evm_res["rejects"]
            log += [dict(s, source=f"[{s.get('chain', 'evm')}] {s.get('source')}") for s in evm_res["sources"]]
        except Exception as exc:  # EVM is additive: a failure never breaks the Solana scan
            log.append({"source": "evm_data.scan", "ok": False, "status": "-", "note": f"{type(exc).__name__}: {exc}"[:200]})
    sui_res = None
    if sui_data is not None and cfg.get("sui_research_enabled", False):
        try:
            sui_res = sui_data.scan(limit=10, deep=3)
        except Exception as exc:
            log.append({"source": "sui_data.scan", "ok": False, "status": "-", "note": f"{type(exc).__name__}: {exc}"[:200]})
    if soc is not None and ctx.get("social_updates"):
        soc.save_snaps(ctx.get("social_snaps", {}), ctx["social_updates"])
    results.sort(key=lambda r: (r["status"] == "PASS", r["p_win"]["estimate"], r["scores"]["composite"]), reverse=True)
    add_narrow(results + rejects)  # SHADOW: computed after every status/sort decision; read by nothing that gates
    by_chain = {}
    for c in cfg.get("manual_card_chains", ["solana"]):
        by_chain[c] = {"pass": sum(1 for r in results if r.get("chain") == c and r["status"] == "PASS"),
                       "watch": sum(1 for r in results if r.get("chain") == c and r["status"] == "WATCH"),
                       "reject": sum(1 for r in rejects if r.get("chain") == c and r.get("status") == "REJECT"),
                       "not_deep_checked": sum(1 for r in rejects if r.get("chain") == c and r.get("status") == "NOT DEEP-CHECKED"),
                       "crash_regime": ((evm_res or {}).get("crash_regime") or {}).get(c) if c != "solana" else crash}
    t1 = now_et()
    return {"generated_et": t1.strftime("%Y-%m-%d %H:%M:%S ET"), "started_et": t0.strftime("%Y-%m-%d %H:%M:%S ET"),
            "mode": "READ-ONLY research (manual mode watchlist); no orders, no signing, no wallet connection",
            "entry_bar": cfg["entry_bar"], "sol_usd": sol_usd, "sol_crash_regime": crash,
            "candidates_seen": len(cands) + sum(len([r for r in (evm_res or {}).get(k, [])]) for k in ("passes", "watch", "rejects")),
            "by_chain": by_chain, "sui_research": sui_res, "scoring_mode": cfg.get("scoring_mode", "prompt"),
            "passes": [r for r in results if r["status"] == "PASS"],
            "watch": [r for r in results if r["status"] == "WATCH"], "rejects": rejects, "sources": log,
            "news_feed_health": news.get("health"), "limiter": limiter_status(), "config": cfg,
            "method": __doc__.split("P_WIN METHOD")[1].split("Usage:")[0].strip()}


def add_narrow(recs: list) -> None:
    """rec['narrow'] = {features, p, model_version, answered} for every record (SHADOW ONLY: never changes a status,
    the 0.75 bar, or the ranking). Failures are recorded, never raised."""
    if nq is None:
        return
    try:
        model = nq.load_model()
    except Exception:  # noqa: BLE001
        return
    for r in recs:
        try:
            r["narrow"] = nq.narrow_block(r, model)
        except Exception as exc:  # noqa: BLE001
            r["narrow"] = {"p": None, "error": f"{type(exc).__name__}: {exc}"[:120]}


def scorer_versions() -> dict:
    v = {}
    for name in ("scoring.py", "scan_candidates.py", "narrow_questions.py", "evm_data.py", "chain_common.py"):
        try:
            v[name.split(".")[0]] = nq.version_hash([os.path.join(HERE, name)]) if nq else None
        except Exception:  # noqa: BLE001
            v[name.split(".")[0]] = None
    try:
        v["narrow_model"] = nq.load_model()["version"] if nq else None
    except Exception:  # noqa: BLE001
        v["narrow_model"] = None
    return v


def assign_receipt_ids(res: dict) -> list:
    """Give every deep-checked record a receipt id (= the shadow_tracker entry id for the same record)."""
    if rcpt is None:
        return []
    ts = rcpt.scan_ts(res.get("generated_et", ""))
    deep = [r for r in res["passes"] + res["watch"] + res["rejects"] if rcpt.reached_deep(r)]
    for r in deep:
        if ts and (r.get("mint") or r.get("address")):
            r["receipt_id"] = rcpt.receipt_id(r.get("chain") or "solana", r.get("mint") or r.get("address"), ts)
    return deep


def write_receipts(res: dict, deep: list, card_files: list) -> int:
    if rcpt is None or not deep:
        return 0
    try:
        ver = scorer_versions()
        return rcpt.append([rcpt.build(r, res, card_files, ver) for r in deep])
    except Exception as exc:  # noqa: BLE001  receipts are bookkeeping: never break a scan
        print(f"receipts: not written ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 0


def _narrow_line(r: dict) -> str:
    nb = r.get("narrow") or {}
    if nb.get("p") is None:
        return "- Narrow-question p (SHADOW, not a gate): unavailable"
    f = nb.get("features") or {}
    key = ", ".join(f"{k}={'yes' if v is True else 'no' if v is False else v}" for k, v in f.items()
                    if k in ("liq_band", "top10_band", "chg1h_band", "vol_liq_band", "rsi_band", "ema_up", "rsi_ema_confluence") and v is not None)
    return (f"- Narrow-question p (SHADOW, not a gate; {nb.get('model_version')}, {nb.get('answered')} answered): **{nb['p']:.2f}**"
            + (f" · {key}" if key else "") + ". Uncalibrated until fitted on >= 50 resolved shadow outcomes.")


# ---------------------------------------------------------------- output
def money(x, d=0):
    if x is None:
        return "?"
    if abs(x) >= 1e9:
        return f"${x/1e9:.2f}B"
    if abs(x) >= 1e6:
        return f"${x/1e6:.2f}M"
    if abs(x) >= 1e3:
        return f"${x/1e3:.1f}k"
    return f"${x:,.{d}f}"


def px(x):
    if not x:
        return "?"
    if x >= 1:
        return f"${x:,.4f}"
    return f"${x:.{max(2, -int(math.floor(math.log10(x))) + 3)}f}"


def render_md(res: dict, title: str = "Multichain candidate scan (Solana + FOMO EVM chains)") -> str:
    cr = res["sol_crash_regime"]
    L = [f"# {title}", "", f"Data timestamp: {res['generated_et']} (scan started {res['started_et']}). {res['mode']}.", "",
         "**These are WATCHLIST cards, not live MOVE CARDS.** Prices move fast: before any trade, re-run the scanner (or "
         "re-check the exact mint, price, liquidity, quote and cost) and treat every card as expiring 15 minutes after its "
         "data timestamp. P_WIN and scores are self-answered (uncalibrated) estimates.", "",
         f"Market regime: SOL {px(res['sol_usd'])}; 60-min change {cr.get('change_60m_pct')}% via {cr.get('source')} → "
         f"{'CRASH REGIME: exits only (H8)' if cr.get('crash') else ('clear' if cr.get('ok') else 'UNKNOWN: treat as NOT clear (G2)')}.",
         f"Candidates seen: {res['candidates_seen']}; passes 0.75 now: {len(res['passes'])}; watch: {len(res['watch'])}; "
         f"rejected/not deep-checked: {len(res['rejects'])}.", ""]
    if res.get("by_chain"):
        L += ["| Chain | Venues (manual cards) | Pass | Watch | Reject | Not deep-checked | Crash regime (60 min) |", "|---|---|---|---|---|---|---|"]
        for c, v in res["by_chain"].items():
            cr_ = v.get("crash_regime") or {}
            ch = cr_.get("change_60m_pct")
            L.append(f"| {ccm.CHAINS.get(c, {}).get('name', c)} | {', '.join(ccm.venues_for(c)) or 'none'} | {v['pass']} | {v['watch']} | "
                     f"{v['reject']} | {v['not_deep_checked']} | {('%.2f%%' % ch) if isinstance(ch, (int, float)) else 'UNKNOWN'}"
                     f"{' CRASH' if cr_.get('crash') else ''} |")
        L += ["", "Sui: research only (Phantom ended Sui support 2026-09-24; FOMO has no Sui), so no cards. Scoring gate: "
              f"{res.get('scoring_mode', 'prompt')} (prompt rule: fixed weights, UNKNOWN = 0, decision confidence = lowest factor "
              "confidence). The adjusted numbers on cards are a SHADOW (renormalized over measured factors with an explicit "
              "haircut) and don't gate anything unless the owner approves. Nothing is calibrated until 20+ logged trades.", ""]
    for label, rows in (("(a) Passes 0.75 now", res["passes"]), ("(b) Watch: close, needs X", res["watch"])):
        L += [f"## {label}", ""]
        if not rows:
            L += ["None." if label.startswith("(a)") else "None qualified.", ""]
            continue
        L += ["| # | Chain | Token | Contract (full) | Price | MCap | Liq | 24h vol | Round trip (best venue) | Stop / TP1 / TP2 | P_WIN (90% CI) | Composite (shadow adj.) | Confidence (shadow adj.) |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for i, r in enumerate(rows, 1):
            pl, pw = r["plan"], r["p_win"]
            ag = r["scores"].get("aggregation") or {}
            L.append(f"| {i} | {ccm.CHAINS.get(r.get('chain', 'solana'), {}).get('name', r.get('chain'))} | {r['name']} (${r['symbol']}) | `{r['mint']}` | "
                     f"{px(r['price_usd'])} | {money(r['market_cap'])} | {money(r['liquidity_usd'])} | {money(r['volume_24h'])} | {_rt_str(r)} | "
                     f"-{pw['stop_pct']}% / +{pw['tp1_pct']}% / +{pw['tp2_pct']}% | {pw['estimate']:.2f} ({pw['posterior']['lo90']:.2f}-{pw['posterior']['hi90']:.2f}) | "
                     f"{r['scores']['composite']:.2f} ({ag.get('composite_adjusted', 0):.2f}) | {r['scores']['decision_confidence']:.2f} ({ag.get('confidence_adjusted', 0):.2f}) |")
        L.append("")
        for i, r in enumerate(rows, 1):
            L += card_md(i, r, res) if r.get("chain", "solana") == "solana" else evm_card_md(i, r, res)
    L += ["## Notable rejects", "", "| Chain | Token | Contract | Liq | Reason (first failing check) |", "|---|---|---|---|---|"]
    rej = sorted([r for r in res["rejects"] if r.get("status") == "REJECT"],
                 key=lambda r: ("major_list" in r.get("sources", []), r.get("liquidity_usd") or 0), reverse=True)
    for r in rej[:60]:
        L.append(f"| {r.get('chain', 'solana')} | {r.get('name', '?')} (${r.get('symbol', '?')}) | `{r['mint']}` | {money(r.get('liquidity_usd'))} | "
                 f"{str(r.get('reason', '')).replace('|', '/')[:160]} |")
    nd = [r for r in res["rejects"] if r.get("status") == "NOT DEEP-CHECKED"]
    if nd:
        L += ["", f"Not deep-checked this run (passed cheap filters, ranked lower): " + ", ".join(f"${r['symbol']}" for r in nd[:30])]
    L += ["", "## Data sources this run", "", "| Source | OK | HTTP | Solana items | Note |", "|---|---|---|---|---|"]
    for s in res["sources"]:
        L.append(f"| {s['source']} | {'yes' if s.get('ok') else 'NO'} | {s.get('status', '-')} | {s.get('solana', s.get('items_48h', '-'))} | {str(s.get('note', ''))[:120].replace('|', '/')} |")
    stale = [k for k, v in (res.get("news_feed_health") or {}).items() if v.get("stale")]
    L += ["", f"News feeds marked stale (newest item > 30 days; down-weighted): {', '.join(stale) or 'none'}.", "",
          "## Method (transparent, conservative)", "", res["method"], "",
          "Gates applied in order: G11 owner coins, G10 bonding curve / 20-min migration wait / ≥100 holders, scanner liquidity "
          f"≥ ${res['config']['min_liquidity_usd']:,} and 24h volume ≥ ${res['config']['min_volume_24h_usd']:,} (tightening filters), "
          "C2/C3 vetoes, T9 wash heuristic, T1 mint verification (DexScreener pair vs RugCheck), T2/T3 authorities, T5 Token-2022 "
          f"extensions, T4 LP lock, T6 top-10 non-pool holders ≤ {res['config']['t6_top10_limit_pct']}% "
          f"({res['config']['t6_top10_limit_established_pct']}% for established majors, flagged), T7 insider clusters, T11 RugCheck + GoPlus "
          "(UNKNOWN = veto), G7/T10/H17 via real Jupiter buy+sell quotes at $100 plus the Phantom app's 0.85%/swap, news danger words "
          "(veto only), SOL crash regime (G2). S3 smart money comes from smart_money.py (Helius, read-only): tracked wallets "
          "scored on their own 30/90-day on-chain records; prompt weights unchanged (0.20 x S3/4); dev selling / fresh-wallet "
          "concentration / net tracked exits force S3 = 0 and block. When S3 is unavailable it is UNKNOWN = 0. S4 social comes from "
          "social_data.py (keyless: Reddit search RSS, Telegram public previews from the token's own links, crypto_news mention "
          "velocity, CoinGecko/GeckoTerminal/Jupiter trending; paid DexScreener boosts/ads and bot-like patterns are negatives); "
          "unreadable = UNKNOWN = 0. Confidences C1/C2/C3/C5 are evidence-based (scoring.py). EVM chains (Base, Ethereum, BNB, "
          "Monad, Robinhood Chain) use evm_data.py (GoPlus + honeypot.is, GeckoTerminal fallback when DexScreener is limited), "
          "evm_smart_money.py for S3, and chain_common.move_card for FOMO/Phantom cards. See SCORING_NOTES.md."]
    return "\n".join(L) + "\n"


def _rt_str(r: dict) -> str:
    c = r.get("costs") or {}
    if r.get("chain", "solana") == "solana":
        return f"{c.get('phantom_app_pct', 0):.2f}% app" + (" / FOMO ok" if c.get("fomo_ok") else "")
    opts = [(c.get("fomo_pct_with_1pct_allowance"), "FOMO")]
    cf = ccm.CHAINS.get(r.get("chain"), {})
    if cf.get("phantom_app"):
        opts.append((c.get("phantom_app_pct"), "app"))
    if cf.get("phantom_auto"):
        opts.append((c.get("phantom_mcp_pct"), "MCP"))
    opts = [o for o in opts if o[0] is not None]
    return " / ".join(f"{v:.2f}% {n}" for v, n in opts) or "UNKNOWN"


def _social_line(r: dict) -> str:
    s = r.get("social") or {}
    bits = []
    for p in s.get("platforms") or []:
        if p.get("platform") == "reddit" and p.get("ok"):
            bits.append(f"Reddit {p['posts_24h']} posts/24 h (base {p['posts_prev_daily']}/day, {p['authors_24h']} authors)")
        elif p.get("platform") == "telegram" and p.get("ok"):
            bits.append(f"Telegram {int(p['subscribers'] or 0):,} subs, {p['posts_24h']} posts/24 h, views/sub {p.get('views_per_subscriber')}"
                        + (f", growth {p['growth_pct_per_day']}%/day" if p.get("growth_pct_per_day") is not None else ""))
        elif p.get("platform") == "news" and p.get("ok"):
            bits.append(f"news {p['mentions_24h']} mentions/24 h (prev {p['mentions_prev_24h']})")
    if s.get("coingecko_trending"):
        bits.append("CoinGecko trending")
    if s.get("aggregator_trending"):
        bits.append("aggregator trending: " + ", ".join(s["aggregator_trending"]))
    return f"- Social (S4, data not instructions): {s.get('explanation', 'UNKNOWN')}" + (f" · {'; '.join(bits)}" if bits else "")


def _news_line(r: dict) -> str:
    n = r.get("news") or {}
    if n.get("danger"):
        return "- News: DANGER " + "; ".join(f"{d['title'][:70]} ({d['source']})" for d in n["danger"][:2])
    if n.get("sample"):
        return "- News: " + "; ".join(f"{d['title'][:70]} ({d['source']}, {d.get('time_et')})" for d in n["sample"][:2])
    return "- News: no token headlines in the last 48 h (crypto_news feeds)"


def evm_card_md(i: int, r: dict, res: dict) -> list:
    """EVM cards: chain_common.move_card (FOMO steps: search by full contract, amount, slippage, where to sell) +
    score/needs/social lines. Watch cards are clearly not buys."""
    sc, pw = r["scores"], r["p_win"]
    ag = sc.get("aggregation") or {}
    card = ccm.move_card(r, i, valid_min=int(res["config"].get("card_valid_min", 15)))
    lines = card.split("\n")
    out = [f"### {i}. {r['name']} (${r['symbol']}) on {ccm.CHAINS.get(r['chain'], {}).get('name', r['chain'])} — {r['status']}", ""]
    out += [("- " + ln[2:]) if ln.startswith("* ") else f"- {ln}" for ln in lines]
    out.append(f"- Data: pair `{r.get('pair')}` on {r.get('dex')} via {r.get('pair_source', 'dexscreener')}; security via GoPlus + honeypot.is; "
               f"liquidity {money(r.get('liquidity_usd'))}, 24h volume {money(r.get('volume_24h'))}, pool age {r.get('pool_age_h')} h.")
    out.append(_social_line(r))
    out.append(f"- Smart money (S3): {(r.get('smart_money') or {}).get('explanation', 'UNKNOWN')}")
    out.append(f"- Score detail: S1 {sc['S1']} S2 {sc['S2']} S3 {sc['S3']} S4 {sc['S4']} E {sc['E']}; confidences {sc['confidences']}; "
               f"gate {ag.get('gate_mode')}; shadow adjusted composite {ag.get('composite_adjusted')} / confidence {ag.get('confidence_adjusted')}.")
    out.append(_narrow_line(r))
    out.append(f"- Needs: {'; '.join(r['needs']) if r['needs'] else 'nothing: passes every gate this scan (still refresh at trade time)'}")
    if r.get("reason"):
        out.append(f"- Note: {r['reason']}")
    out.append("")
    return out


def card_md(i: int, r: dict, res: dict) -> list:
    pl, pw, c, sc = r["plan"], r["p_win"], r["costs"], r["scores"]
    rc = r["rugcheck"]
    valid = res["generated_et"]
    risks = []
    if rc.get("mutable_metadata"):
        risks.append("mutable metadata")
    if r.get("t7_state") == "ELEVATED":
        risks.append(f"insider cluster {rc.get('cluster_pct')}% (ELEVATED → half size)")
    if not r["established"]:
        risks.append("memecoin / low-cap: gaps through stops are common (checks ~5 min apart)")
    if rc.get("top10_nonpool_pct") is not None:
        risks.append(f"top-10 non-pool holders {rc['top10_nonpool_pct']}%")
    if "INTERPRETATION" in " ".join(ch["note"] for ch in r["checks"]):
        risks.append("T4 passed only via the diversified-liquidity interpretation (LP not locked)")
    risks += [f"RugCheck warn: {x['name']}" for x in rc.get("risks", []) if (x.get("level") or "") == "warn"][:3]
    risks.append(f"P_WIN backtest {pw['backtest']['wins']}W/{pw['backtest']['losses']}L/{pw['backtest']['timeouts']}T on {pw['candles']} candles ({pw['candle_source']})")
    size_pct = pl["permitted_max_pct_equity"]
    return [f"### {i}. {r['name']} (${r['symbol']}) — {r['status']}", "",
            f"- Contract (full mint, verified DexScreener pair `{r['pair']}` on {r['dex']} ↔ RugCheck: {'yes' if r.get('address_verified') else 'NO'}): `{r['mint']}`",
            f"- Price {px(r['price_usd'])} · MCap {money(r['market_cap'])} · Liquidity (main pool) {money(r['liquidity_usd'])} · 24h volume {money(r['volume_24h'])} · 24h change {r['price_change'].get('h24')}% · pool age {r.get('pool_age_h')} h",
            f"- Key risks: {'; '.join(risks)}",
            f"- Plan (watchlist, refresh before use): entry range {px(pl['entry_low'])}–{px(pl['entry_high'])} (ref {px(pl['reference_price'])}, buy slippage ≤ {pl['buy_slippage_pct']:.2f}%); "
            f"stop {px(pl['stop'])} (−{pw['stop_pct']}%, {pw['stop_method']}); TP1 {px(pl['tp1'])} (+{pw['tp1_pct']}%, {pw['tp1_R']}R = 1R + cost); TP2 {px(pl['tp2'])} (+{pw['tp2_pct']}%).",
            f"- Size (equity unknown): PERMITTED_MAX = min(10%, 2% ÷ {pw['stop_pct']}% stop = {min(10.0, 200 / pw['stop_pct']):.1f}%) = **{size_pct:.1f}% of equity, never above $100** until 20 closed trades"
            + (" · T7 ELEVATED → half" if pl["t7_half"] else "") + "; the §2C.5 band (PROBE 0.25× unless band confidence ≥ 0.75) applies after a BUY. Max loss at stop ≤ 2% of equity.",
            f"- Round trip at $100: Phantom app {c['phantom_app_pct']:.2f}% (Jupiter pool fees+impact {r['jupiter']['pct_pool_and_impact']:.2f}% + 2×0.85% app fee + expected slippage) → {'≤ 3% OK' if c['phantom_ok'] else 'FAILS 3%'}; {pl['fomo']}.",
            f"- App: **{pl['venue']}**. Steps: Swap → receive token → search → paste the full mint above → match every character → amount (≤ size) → Slippage Custom {pl['buy_slippage_pct']:.2f}% (never Auto) → Priority fee Custom (minimum; never Auto) → execute only if price impact ≤ 3%, Phantom fee ≤ 0.85% and the quote is inside the entry range. Anything else (another token, an approval, a connect/sign request) → cancel.",
            f"- Chain: Solana (solana:mainnet). FOMO: " + ("search → paste the full mint → confirm chain Solana and every character → custom amount → set slippage "
            f"{pl['buy_slippage_pct']:.2f}% if offered (FOMO documents no slippage control; otherwise check the quote) → sell later from your profile's open positions → Sell % (minimum sell $2; no spot stop orders, I alert at levels)."
            if c.get("fomo_ok") else f"not viable at $100 on Solana (tier fees: {pl['fomo']}); use the Phantom app steps below, or FOMO only for exits."),
            _social_line(r),
            _news_line(r),
            f"- Smart money (S3): {(r.get('smart_money') or {}).get('explanation', 'UNKNOWN')}",
            f"- Score: COMPOSITE {sc['composite']:.2f} (S1 {sc['S1']}, S2 {sc['S2']}, S3 {sc['S3']}{'' if (r.get('smart_money') or {}).get('status') == 'ok' else ' unknown'}, S4 {sc['S4']}{'' if (r.get('social') or {}).get('status') == 'ok' else ' unknown'}, C1 {sc['C1']} E {sc['E']}); C2 {sc['C2']}, C3 {sc['C3']}, C5 {sc['C5']}; decision confidence {sc['decision_confidence']:.2f}; "
            f"P_WIN {pw['estimate']:.2f} (90% {pw['posterior']['lo90']:.2f}–{pw['posterior']['hi90']:.2f}; P(p ≥ 0.75) = {pw['posterior']['p_ge_bar']:.2f}; martingale prior {pw['prior_martingale']:.2f}) — {pw['label']}.",
            _narrow_line(r),
            f"- Needs: {'; '.join(r['needs']) if r['needs'] else 'nothing: passes every gate this scan (still refresh at trade time)'}",
            f"- Card data time {valid}; a live card would be valid 15 minutes from a fresh re-check, never from this file.", ""]


# ---------------------------------------------------------------- selftest
def selftest() -> list[dict]:
    rows = []

    def add(check, ok, detail):
        rows.append({"tool": "scan_candidates", "check": check, "ok": bool(ok), "status": "-", "ms": 0, "detail": str(detail)[:220]})
    # martingale prior < 0.5 when TP1 >= 1R + cost
    pw = p_win(0.20, 2.0, [], 48, 10, 0.75)
    add("P_WIN prior (no data) = b/(a+b) < 0.5", abs(pw["prior_martingale"] - 0.2 / 0.42) < 1e-3 and pw["estimate"] < 0.5, pw["prior_martingale"])
    # synthetic random walk: estimate stays near prior, never inflated
    rng = random.Random(1)
    price, candles = 1.0, []
    for k in range(1000):
        o = price
        price *= math.exp(rng.gauss(0, 0.02))
        candles.append((k, o, max(o, price) * 1.005, min(o, price) * 0.995, price))
    pw2 = p_win(0.10, 2.0, candles, 48, 10, 0.75)
    add("random-walk backtest does not clear 0.75", pw2["estimate"] < 0.6, f"{pw2['estimate']} bt={pw2['backtest']}")
    # strong uptrend synthetic: method can exceed the bar with real evidence (not structurally blocked)
    price, up = 1.0, []
    for k in range(1000):
        o = price
        price *= 1.004
        up.append((k, o, price * 1.001, o * 0.999, price))
    pw3 = p_win(0.05, 1.0, up, 48, 10, 0.75)
    add("steady uptrend evidence can clear 0.75", pw3["estimate"] >= 0.75, pw3["estimate"])
    add("backtest counts stop first when both hit in a candle",
        backtest([(0, 1, 1, 1, 1), (1, 1, 1.5, 0.5, 1), (2, 1, 1, 1, 1), (3, 1, 1, 1, 1)], 0.1, 0.1, 5)["losses"] == 1, "tie -> loss")
    c2, c3, _ = classify_price_action({"h1": 50, "h6": 120, "h24": 400})
    add("C2/C3 flags a blow-off/chase", c2 == "BLOW-OFF" and c3 == "CHASE", (c2, c3))
    c2, _, _ = classify_price_action({"h1": -8, "h6": -35, "h24": -50})
    add("C2 flags a collapse", c2 == "COLLAPSE", c2)
    vc = venue_costs({"pct_pool_and_impact": 0.6}, 100, 1_000_000, "mid")
    add("Phantom-app cost math (0.6% pool + 1.7% app + 0.2% slip)", abs(vc["phantom_app_pct"] - 2.504) < 0.01 and vc["phantom_ok"], vc)
    vc2 = venue_costs({"pct_pool_and_impact": 2.6}, 100, 1_000_000, "memecoin")
    add("1.25%-fee pool (2.6% RT) fails H17 in the Phantom app", not vc2["phantom_ok"], vc2["phantom_app_pct"])
    add("FOMO at $100 with 1% allowance fails 3% for a 0.6% pool", not vc["fomo_ok"], vc["fomo_pct_with_1pct_allowance"])
    b, _ = choose_stop("memecoin", 0.5, {})
    add("memecoin stop capped at 25% (H5)", b <= 0.25, b)
    add("sizing: 2%/stop and 10% cap", min(10.0, 2.0 / 0.25) == 8.0 and min(10.0, 2.0 / 0.1) == 10.0, "25% stop -> 8%, 10% stop -> 10%")
    fake = {"mint": "X" * 44, "sources": {"t"}}
    rec = evaluate("X" * 44, [{"chainId": "solana", "dexId": "pumpfun", "baseToken": {"symbol": "FAKE", "name": "Fake"},
                               "liquidity": {"usd": 1e6}, "volume": {"h24": 1e6}, "priceChange": {}, "pairCreatedAt": 1}],
                   fake["sources"], {}, load_config(), [])
    add("bonding-curve pair is rejected (G10)", rec["status"] == "REJECT" and "bonding curve" in rec["reason"], rec.get("reason"))
    cfg = dict(load_config(), owner_mints=["Y" * 44])
    rec = evaluate("Y" * 44, [{"chainId": "solana", "dexId": "raydium", "baseToken": {"symbol": "OWN", "name": "Own"},
                               "liquidity": {"usd": 1e6}, "volume": {"h24": 1e6}, "priceChange": {}, "pairCreatedAt": 1}],
                   {"t"}, {}, cfg, [])
    add("owner's own coin is rejected (G11)", rec["status"] == "REJECT" and "G11" in rec["reason"], rec.get("reason"))
    # light live smoke test: one DexScreener batch + one RugCheck report (BONK)
    lg = []
    ps = dexscreener_batch([MAJORS["BONK"]], lg)
    mp = main_pair(ps.get(MAJORS["BONK"], []))
    jt = jupiter_tokens([MAJORS["BONK"]], lg).get(MAJORS["BONK"], {})
    add("live: BONK pool data (DexScreener main pair, else Jupiter Tokens v2 fallback)", bool(mp) or bool(jt),
        f"DexScreener pair {(mp or {}).get('pairAddress')}" if mp else
        f"DexScreener unavailable ({(lg[0]['note'][:70] if lg else '?')}); Jupiter liquidity ${fnum(jt.get('liquidity'), 0):,.0f}")
    rc = rugcheck_facts(MAJORS["BONK"], (mp or {}).get("pairAddress"), load_config())
    add("live: RugCheck facts for BONK (mint verified)", rc.get("ok") and rc.get("mint") == MAJORS["BONK"],
        {k: rc.get(k) for k in ("symbol", "top10_nonpool_pct", "cluster_pct", "holders", "error")})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only Solana candidate scanner (Phantom prompt gates)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--snapshot-only", action="store_true", help="record a liquidity snapshot only (S1 evidence)")
    ap.add_argument("--watchlist", help="also write a watchlist markdown file at this path")
    ap.add_argument("--out-dir", default=OUT_DIR)
    a = ap.parse_args()
    if a.selftest:
        rows = selftest()
        print(cd.rows_to_md(rows))
        return 0 if all(r["ok"] for r in rows) else 1
    # Only a real scan opts into a brief cooldown wait; importing this module
    # must not change the aggregate selftest's process-wide policy.
    os.environ.setdefault("GROKBOT_MAX_WAIT", "65")   # wait out 60 s cooldowns (GeckoTerminal/Reddit); longer ones fail fast
    cfg = load_config()
    res = run(cfg, snapshot_only=a.snapshot_only)
    os.makedirs(a.out_dir, exist_ok=True)
    if a.snapshot_only:
        print(json.dumps(res, indent=1, default=str)[:2000])
        return 0
    stamp = now_et().strftime("%Y-%m-%d-%H%M")
    jpath, mpath = os.path.join(a.out_dir, f"scan-{stamp}.json"), os.path.join(a.out_dir, f"scan-{stamp}.md")
    deep = assign_receipt_ids(res)
    json.dump(res, open(jpath, "w"), indent=1, default=str)
    md = render_md(res)
    open(mpath, "w").write(md)
    for src, dst in ((jpath, "latest.json"), (mpath, "latest.md")):
        d = os.path.join(a.out_dir, dst)
        if os.path.lexists(d):
            os.remove(d)
        os.symlink(os.path.basename(src), d)
    if a.watchlist:
        open(a.watchlist, "w").write(render_md(res, title=f"Morning watchlist — {now_et():%a %b %d, %Y}"))
    n_rc = write_receipts(res, deep, [mpath] + ([a.watchlist] if a.watchlist else []))
    print(f"receipts: {n_rc} appended to {rcpt.RECEIPTS if rcpt else '-'}")
    print(f"wrote {jpath}\nwrote {mpath}" + (f"\nwrote {a.watchlist}" if a.watchlist else ""))
    print(f"passes {len(res['passes'])} | watch {len(res['watch'])} | rejects {len(res['rejects'])} | data {res['generated_et']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
