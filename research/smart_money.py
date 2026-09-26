#!/usr/bin/env python3
"""smart_money.py: READ-ONLY Helius-based smart-money wallet tracking for the Solana candidate scanner (S3).

Nothing here signs, sends, swaps, connects a wallet or places an order. Only read-only Helius JSON-RPC `get*`
methods are used (via crypto_data.helius_rpc, which refuses anything else) plus the Helius Wallet Identity lookup
(read-only; paid plans only - a 403 is cached and treated as "not available"). All HTTP goes through _http.py
(shared polite limiter, 30 s caps, soft rate-limit failures). The Helius key is read from the HELIUS_API_KEY
environment variable only and is NEVER printed, logged or written to disk.

What it does
  1. build/rescore: maintain $BOT_DATA_DIR/smart_wallets.json.
     Seeds = early buyers of recent Solana tokens that ran (Jupiter Tokens v2 trending/top-traded lists: launched
     <= 14 days ago and now >= $1M market cap => >= ~100x from a typical launchpad start). For each seed token the
     mint's own history (getTransactionsForAddress, oldest first) gives the earliest swaps; a wallet is a candidate
     if it SIGNED a buy >= 60 s after the first transaction at a price <= 1/5 of today's price (and >= 0.2 SOL).
     Buyers inside the first 60 s (snipers/bundles) and the deployer are not candidates (deployers are flagged).
     Every candidate is then scored ONLY on its own measured on-chain record (its last 90 days of successful
     transactions, parsed locally from pre/post balances): closed round trips, win rate, realized PnL in SOL,
     holding times, trades/day, one-trade concentration, deployer activity.
  2. token_signal(mint): which tracked wallets bought/sold the mint in the last N hours (entry time, size, still
     holding), dev/deployer selling, and fresh-wallet concentration among the top non-pool holders (bundles).
  3. An S3 level 0-4 per the Phantom prompt §2C/§3.2, a score = level/4 in [0,1], a confidence, and an
     "insufficient data" flag with an explanation. Missing data => status "unknown" (never invented).

Tiers (the prompt's definitions are stricter than the task's minimum sample, so both are kept):
  tracked  = task bar: >= 15 closed round trips in the last 30 days, win rate >= 50%, realized PnL > 0 SOL,
             best trade <= 60% of gross profit (no one-hit wonders), median hold >= 60 s, < 20% of round trips
             held < 10 s, <= 100 swaps/day average and <= 300 on any day, not a deployer, not a program/exchange.
             For S3 a tracked-but-not-reliable wallet counts as "unproven" (prompt: unverifiable = unproven).
  reliable = tracked AND >= 500 swaps over 90 days AND win rate > 50% (prompt §3.2: "500+ trades over 90 days and
             a win rate above 50%, verified from on-chain data").
  high     = reliable AND reliability score >= 70 (prompt §3.2 weights; components that cannot be measured are
             dropped and the weights renormalized - listed per wallet under reliability_missing).

S3 levels (prompt §2C): 0 none or low-reliability only · 1 one unproven (tracked) wallet · 2 one reliable ·
3 two+ independent reliable · 4 three+ independent high-reliability wallets with fresh entries (<= 6 h, still
holding). "Independent" = first entries not in the same slot as another counted wallet (same-slot entries are
treated as one coordinated actor). Only wallets that bought in the lookback window AND still hold count.
Negative signals (dev/deployer sold or moved tokens out in the window, fresh wallets holding >= 15% of supply among
top non-pool holders, or more tracked wallets exiting than entering) cap S3 at 0 and are reported as blockers.

CLI:
  python3 smart_money.py build [--max-tokens 8] [--max-wallets 120]   # seed + score + rescore stale (heavy; spaced)
  python3 smart_money.py rescore                                      # re-score entries older than RESCORE_DAYS
  python3 smart_money.py token <MINT> [--hours 24]                    # token lookup (JSON)
  python3 smart_money.py status                                       # watchlist summary + credit ledger
  python3 smart_money.py --selftest
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import statistics
import sys
import time
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from typing import Any, Callable

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _http  # noqa: E402
import crypto_data as cd  # noqa: E402
from _http import Resp, get_json, post_json  # noqa: E402

import paths as _paths  # noqa: E402
DATA_DIR = _paths.BASE_DATA_DIR
WATCHLIST = os.path.join(DATA_DIR, "smart_wallets.json")
USAGE_FILE = os.path.join(DATA_DIR, "helius_usage.json")
TOKEN_CACHE = os.path.join(DATA_DIR, "smart_money_token_cache.json")

SOL = cd.SOL_MINT
USDC = cd.USDC_MINT
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_USD = {USDC, USDT}
SYSTEM_PROGRAM = "11111111111111111111111111111111"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
NOT_TRADES = {  # LSTs / wrapped majors / stables: swaps into these are not memecoin trades
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn", "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1", "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v",
    "5oVNBeEEQvYi1cX3ir8Dx5n1P7pdxydbGF2X4TxVusJm", "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo", "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh", "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
}

# ---- criteria (documented above; change only to make the bar STRICTER)
MIN_CLOSED_30D = 15
MIN_WIN_RATE = 0.50
MAX_TOP_TRADE_SHARE = 0.60
MIN_MEDIAN_HOLD_S = 60
MAX_FAST_FLIP_FRAC = 0.20       # round trips held < 10 s
MAX_SWAPS_PER_DAY = 100
MAX_SWAPS_ONE_DAY = 300
RELIABLE_SWAPS_90D = 500
HIGH_RELIABILITY = 70
RESCORE_DAYS = 3.0
REJECT_RECHECK_DAYS = 7.0
HISTORY_DAYS = 90
HISTORY_PAGE_CAP = 3            # x 1000 full transactions (<= 300 credits per wallet)
FRESH_WALLET_H = 72
FRESH_ELEVATED_PCT = 15.0
FRESH_ENTRY_H = 6


# ---------------------------------------------------------------- key + transport (key never printed)
def helius_key_present() -> bool:
    return cd.helius_key_present()


def _usage_add(method: str, credits: float) -> None:
    """Estimated credit ledger per ET day (Helius meters server-side; this is our own estimate)."""
    try:
        day = datetime.now().strftime("%Y-%m-%d")
        u = json.load(open(USAGE_FILE)) if os.path.exists(USAGE_FILE) else {}
        d = u.setdefault(day, {"calls": 0, "credits_est": 0, "by_method": {}})
        d["calls"] += 1
        d["credits_est"] += credits
        m = d["by_method"].setdefault(method, {"calls": 0, "credits_est": 0})
        m["calls"] += 1
        m["credits_est"] += credits
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = USAGE_FILE + f".{os.getpid()}.tmp"
        json.dump(u, open(tmp, "w"), indent=1)
        os.replace(tmp, USAGE_FILE)
    except (OSError, ValueError):
        pass


def _default_rpc(method: str, params: list, timeout: float = 30.0) -> Resp:
    return cd.helius_rpc(method, params, timeout=timeout)


RPC: Callable[..., Resp] = _default_rpc   # injectable for self-tests


def rpc(method: str, params: list, timeout: float = 30.0, max_cooldown_wait: float = 0.0) -> Resp:
    """One read-only call through the shared limiter. On a shared cooldown we optionally wait (bounded) once."""
    r = RPC(method, params, timeout=timeout)
    if not r.ok and r.status == 429 and max_cooldown_wait > 0:
        wait = min(max_cooldown_wait, _http.cooldown_remaining("https://mainnet.helius-rpc.com/"))
        if 0 < wait <= max_cooldown_wait:
            time.sleep(wait + 1)
            r = RPC(method, params, timeout=timeout)
    if r.ok:
        n = 0
        res = (r.data or {}).get("result")
        if method == "getTransactionsForAddress":
            full = (params[1] if len(params) > 1 else {}).get("transactionDetails") == "full"
            n = len((res or {}).get("data") or []) if isinstance(res, dict) else 0
            _usage_add(method + (":full" if full else ":signatures"), max(10, 10 * math.ceil(n / 100)) if full else 10)
        else:
            _usage_add(method, 1)
    return r


def txs_for_address(addr: str, *, full: bool = True, sort: str = "desc", limit: int = 1000, filters: dict | None = None,
                    token: str | None = None, max_cooldown_wait: float = 0.0) -> tuple[list, str | None, str]:
    """(rows, pagination token, error). Helius getTransactionsForAddress (read-only)."""
    cfg: dict[str, Any] = {"transactionDetails": "full" if full else "signatures", "sortOrder": sort, "limit": limit,
                           "filters": dict(filters or {})}
    cfg["filters"].setdefault("status", "succeeded")
    if full:
        cfg.update(encoding="json", maxSupportedTransactionVersion=1)
    if token:
        cfg["paginationToken"] = token
    r = rpc("getTransactionsForAddress", [addr, cfg], max_cooldown_wait=max_cooldown_wait)
    if not r.ok:
        return [], None, (r.error or f"HTTP {r.status}")[:200]
    res = (r.data or {}).get("result") or {}
    return res.get("data") or [], res.get("paginationToken"), ""


# ---------------------------------------------------------------- local transaction parsing
def _keys(tx: dict) -> list[str]:
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [k if isinstance(k, str) else k.get("pubkey") for k in msg.get("accountKeys") or []]
    la = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return keys + list(la.get("writable") or []) + list(la.get("readonly") or [])


def _ui(b: dict) -> float:
    ta = b.get("uiTokenAmount") or {}
    try:
        return float(ta.get("uiAmountString") if ta.get("uiAmountString") is not None else (ta.get("uiAmount") or 0))
    except (TypeError, ValueError):
        return 0.0


def token_deltas(tx: dict, owner: str) -> dict[str, float]:
    meta = tx.get("meta") or {}
    out: dict[str, float] = {}
    for sign, arr in ((-1, meta.get("preTokenBalances") or []), (1, meta.get("postTokenBalances") or [])):
        for b in arr:
            if b.get("owner") == owner and b.get("mint"):
                out[b["mint"]] = out.get(b["mint"], 0.0) + sign * _ui(b)
    return {m: v for m, v in out.items() if abs(v) > 1e-12}


def classify(tx: dict, wallet: str, sol_usd: float | None = None) -> dict | None:
    """Swap made by `wallet` in this tx -> {side, mint, qty, sol, ts, slot, sig}; None if not a simple swap.
    sol = SOL-equivalent spent (buy) or received (sell), incl. network fees/ATA rent when the wallet pays them.
    USDC/USDT legs are converted at today's SOL price (approximation, flagged via quote='usd')."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    keys = _keys(tx)
    if wallet not in keys:
        return None
    idx = keys.index(wallet)
    hdr = ((tx.get("transaction") or {}).get("message") or {}).get("header") or {}
    if idx >= int(hdr.get("numRequiredSignatures") or 1):
        return None  # wallet did not sign: not its own trade
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    lam = (post[idx] - pre[idx]) / 1e9 if idx < len(pre) and idx < len(post) else 0.0
    td = token_deltas(tx, wallet)
    sol_delta = lam + td.pop(SOL, 0.0)
    quote = "sol"
    usd = sum(td.pop(m, 0.0) for m in list(td) if m in QUOTE_USD)
    if usd:
        if not sol_usd:
            return None
        sol_delta += usd / sol_usd
        quote = "usd"
    if any(m in NOT_TRADES for m in td):
        return None
    if len(td) != 1:
        return None
    mint, qty = next(iter(td.items()))
    if abs(sol_delta) < 0.001:
        return None
    if qty > 0 and sol_delta < 0:
        side = "buy"
    elif qty < 0 and sol_delta > 0:
        side = "sell"
    else:
        return None
    sig = ((tx.get("transaction") or {}).get("signatures") or [""])[0]
    return {"side": side, "mint": mint, "qty": abs(qty), "sol": abs(sol_delta), "ts": tx.get("blockTime") or 0,
            "slot": tx.get("slot") or 0, "sig": sig, "quote": quote}


def is_deploy_tx(tx: dict, wallet: str) -> bool:
    """wallet paid for a tx that created a mint (spl InitializeMint* or pump.fun Create)."""
    keys = _keys(tx)
    if not keys or keys[0] != wallet:
        return False
    logs = (tx.get("meta") or {}).get("logMessages") or []
    for i, line in enumerate(logs):
        if "Instruction: InitializeMint" in line:
            return True
        if line.startswith(f"Program {PUMP_PROGRAM} invoke") and i + 1 < len(logs) and \
                logs[i + 1].strip() in ("Program log: Instruction: Create", "Program log: Instruction: CreateV2"):
            return True
    return False


def round_trips(swaps: list[dict]) -> tuple[list[dict], dict]:
    """Per-mint cycles, chronological. A cycle opens on a buy with no position and closes when the position falls to
    <= 1% of its peak. Sells with no tracked buy (position opened before the window) are ignored.
    Returns (closed round trips, open positions)."""
    pos: dict[str, dict] = {}
    closed = []
    for s in sorted(swaps, key=lambda x: (x["ts"], x["slot"])):
        p = pos.get(s["mint"])
        if s["side"] == "buy":
            if p is None:
                p = pos[s["mint"]] = {"qty": 0.0, "peak": 0.0, "cost": 0.0, "proceeds": 0.0, "t0": s["ts"],
                                      "slot0": s["slot"], "t1": s["ts"], "n": 0}
            p["qty"] += s["qty"]
            p["peak"] = max(p["peak"], p["qty"])
            p["cost"] += s["sol"]
            p["n"] += 1
        else:
            if p is None:
                continue
            sold = min(s["qty"], p["qty"])
            frac = sold / s["qty"] if s["qty"] else 0
            p["qty"] -= sold
            p["proceeds"] += s["sol"] * frac
            p["t1"] = s["ts"]
            p["n"] += 1
            if p["qty"] <= 0.01 * p["peak"]:
                pnl = p["proceeds"] - p["cost"]
                closed.append({"mint": s["mint"], "cost": p["cost"], "proceeds": p["proceeds"], "pnl": pnl,
                               "pnl_pct": pnl / p["cost"] * 100 if p["cost"] else 0.0, "t0": p["t0"], "t1": p["t1"],
                               "hold_s": max(0, p["t1"] - p["t0"]), "slots": s["slot"] - p["slot0"], "legs": p["n"]})
                pos.pop(s["mint"])
    return closed, pos


def wallet_stats(wallet: str, txs: list[dict], now: float | None = None, sol_usd: float | None = None,
                 history_complete: bool = True) -> dict:
    now = now or time.time()
    swaps, deploys, days = [], 0, {}
    for tx in txs:
        if is_deploy_tx(tx, wallet):
            deploys += 1
        s = classify(tx, wallet, sol_usd)
        if s:
            swaps.append(s)
            d = datetime.fromtimestamp(s["ts"], timezone.utc).strftime("%Y-%m-%d")
            days[d] = days.get(d, 0) + 1
    t30 = now - 30 * 86400
    closed, open_pos = round_trips(swaps)
    c30 = [c for c in closed if c["t1"] >= t30]
    wins = [c for c in c30 if c["pnl"] > 0]
    losses = [c for c in c30 if c["pnl"] <= 0]
    gross_win = sum(c["pnl"] for c in wins)
    span_days = max(1.0, (now - min((s["ts"] for s in swaps), default=now)) / 86400)
    swaps30 = [s for s in swaps if s["ts"] >= t30]
    holds = [c["hold_s"] for c in c30]
    # cumulative realized PnL drawdown (SOL) over the 30-day round trips
    eq = peak = mdd = 0.0
    for c in sorted(c30, key=lambda c: c["t1"]):
        eq += c["pnl"]
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    weeks: dict[int, float] = {}
    for c in c30:
        weeks[int((now - c["t1"]) // (7 * 86400))] = weeks.get(int((now - c["t1"]) // (7 * 86400)), 0) + c["pnl"]
    st = {
        "swaps_90d": len(swaps), "swaps_30d": len(swaps30), "history_days_covered": round(span_days, 1),
        "history_complete_90d": history_complete, "txs_scanned": len(txs),
        "closed_30d": len(c30), "wins_30d": len(wins), "win_rate_30d": round(len(wins) / len(c30), 4) if c30 else None,
        "realized_pnl_sol_30d": round(sum(c["pnl"] for c in c30), 4),
        "median_pnl_pct_30d": round(statistics.median([c["pnl_pct"] for c in c30]), 2) if c30 else None,
        "avg_cost_sol_30d": round(statistics.mean([c["cost"] for c in c30]), 4) if c30 else None,
        "median_hold_s": int(statistics.median(holds)) if holds else None,
        "fast_flip_frac": round(sum(1 for h in holds if h < 10) / len(holds), 4) if holds else None,
        "swaps_per_day_avg": round(len(swaps30) / min(30.0, span_days), 2) if swaps else 0.0,
        "swaps_max_day": max(days.values()) if days else 0,
        "top_trade_share": round(max((c["pnl"] for c in wins), default=0) / gross_win, 4) if gross_win > 0 else None,
        "avg_win_sol": round(gross_win / len(wins), 4) if wins else None,
        "avg_loss_sol": round(sum(c["pnl"] for c in losses) / len(losses), 4) if losses else None,
        "max_drawdown_sol_30d": round(mdd, 4), "positive_weeks": sum(1 for v in weeks.values() if v > 0),
        "weeks_traded": len(weeks), "pnl_last_7d_sol": round(sum(c["pnl"] for c in c30 if c["t1"] >= now - 7 * 86400), 4),
        "distinct_mints_30d": len({c["mint"] for c in c30}), "open_positions": len(open_pos),
        "deploy_txs": deploys, "usd_quoted_swaps": sum(1 for s in swaps if s["quote"] == "usd"),
    }
    return st


def reliability(st: dict) -> tuple[float | None, list[str]]:
    """Prompt §3.2 weights; components we cannot measure from wallet history are dropped and weights renormalized."""
    comp: dict[str, tuple[float, float]] = {}
    missing = []
    if st.get("closed_30d"):
        med = st.get("median_pnl_pct_30d") or 0.0
        pnl = st.get("realized_pnl_sol_30d") or 0.0
        comp["realized_profitability"] = (25, max(0.0, min(1.0, 0.5 + med / 100))) if pnl > 0 else (25, 0.0)
        comp["consistency"] = (15, st["positive_weeks"] / st["weeks_traded"] if st.get("weeks_traded") else 0.0)
        comp["win_rate"] = (15, max(0.0, min(1.0, ((st.get("win_rate_30d") or 0) - 0.3) / 0.5)))
        comp["recent"] = (10, 1.0 if (st.get("pnl_last_7d_sol") or 0) > 0 else 0.0)
        gw = (st.get("avg_win_sol") or 0) * (st.get("wins_30d") or 0)
        comp["drawdown"] = (10, max(0.0, 1 - st["max_drawdown_sol_30d"] / gw) if gw > 0 else 0.0)
        aw, al = st.get("avg_win_sol"), st.get("avg_loss_sol")
        comp["exits"] = (5, min(1.0, aw / abs(al) / 2) if aw and al else (1.0 if aw else 0.0))
        comp["copyability"] = (5, min(1.0, (st.get("median_hold_s") or 0) / 1800))
    missing += ["early_entry (10%)", "liquidity_quality (5%)"]
    if not comp:
        return None, missing + ["all"]
    w = sum(v[0] for v in comp.values())
    return round(sum(v[0] * v[1] for v in comp.values()) / w * 100, 1), missing


def judge(st: dict) -> tuple[str, list[str]]:
    """-> (tier, reasons it failed). tier in high|reliable|tracked|rejected|dev."""
    why = []
    if st.get("deploy_txs"):
        return "dev", [f"deployed {st['deploy_txs']} mint(s) in the window (dev wallet: separate signal, not smart money)"]
    if st["closed_30d"] < MIN_CLOSED_30D:
        why.append(f"closed round trips 30d {st['closed_30d']} < {MIN_CLOSED_30D}")
    if st["win_rate_30d"] is None or st["win_rate_30d"] < MIN_WIN_RATE:
        why.append(f"win rate {st['win_rate_30d']} < {MIN_WIN_RATE}")
    if st["realized_pnl_sol_30d"] <= 0:
        why.append(f"realized PnL {st['realized_pnl_sol_30d']} SOL <= 0")
    if st["top_trade_share"] is not None and st["top_trade_share"] > MAX_TOP_TRADE_SHARE:
        why.append(f"one-trade concentration {st['top_trade_share']:.0%} > {MAX_TOP_TRADE_SHARE:.0%}")
    if st["median_hold_s"] is not None and st["median_hold_s"] < MIN_MEDIAN_HOLD_S:
        why.append(f"median hold {st['median_hold_s']}s < {MIN_MEDIAN_HOLD_S}s (bot/sniper-like)")
    if st["fast_flip_frac"] is not None and st["fast_flip_frac"] >= MAX_FAST_FLIP_FRAC:
        why.append(f"{st['fast_flip_frac']:.0%} of round trips held < 10 s (bot/MEV)")
    if st["swaps_per_day_avg"] > MAX_SWAPS_PER_DAY or st["swaps_max_day"] > MAX_SWAPS_ONE_DAY:
        why.append(f"{st['swaps_per_day_avg']} swaps/day avg, {st['swaps_max_day']} max day (bot)")
    if why:
        return "rejected", why
    rel, _ = reliability(st)
    if st["swaps_90d"] >= RELIABLE_SWAPS_90D and (st["win_rate_30d"] or 0) > 0.5:
        return ("high" if (rel or 0) >= HIGH_RELIABILITY else "reliable"), []
    return "tracked", []


# ---------------------------------------------------------------- watchlist persistence
def load_watchlist() -> dict:
    try:
        w = json.load(open(WATCHLIST))
        if isinstance(w, dict) and "wallets" in w:
            return w
    except (OSError, ValueError):
        pass
    return {"version": 1, "wallets": {}, "flagged": {}, "rejected": {}, "seeds": [], "identity_api": None}


def save_watchlist(w: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    w["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    w["criteria"] = {"min_closed_30d": MIN_CLOSED_30D, "min_win_rate": MIN_WIN_RATE, "realized_pnl_sol_gt": 0,
                     "max_top_trade_share": MAX_TOP_TRADE_SHARE, "min_median_hold_s": MIN_MEDIAN_HOLD_S,
                     "max_fast_flip_frac": MAX_FAST_FLIP_FRAC, "max_swaps_per_day": MAX_SWAPS_PER_DAY,
                     "max_swaps_one_day": MAX_SWAPS_ONE_DAY, "reliable_swaps_90d": RELIABLE_SWAPS_90D,
                     "high_reliability": HIGH_RELIABILITY, "history_days": HISTORY_DAYS, "rescore_days": RESCORE_DAYS}
    tmp = WATCHLIST + f".{os.getpid()}.tmp"
    json.dump(w, open(tmp, "w"), indent=1)
    os.replace(tmp, WATCHLIST)


def tracked_wallets(w: dict | None = None) -> dict[str, dict]:
    w = w or load_watchlist()
    return {a: e for a, e in w.get("wallets", {}).items() if e.get("tier") in ("tracked", "reliable", "high")}


# ---------------------------------------------------------------- seeding
def sol_price() -> float | None:
    """Jupiter price, else DefiLlama (both keyless, via the shared limiter)."""
    r = cd.jupiter_price([SOL])
    try:
        if r.ok:
            return float(r.data[SOL]["usdPrice"])
    except (KeyError, TypeError, ValueError):
        pass
    r = cd.defillama_prices(["solana:" + SOL])
    try:
        return float(r.data["coins"]["solana:" + SOL]["price"]) if r.ok else None
    except (KeyError, TypeError, ValueError):
        return None


def seed_tokens(max_tokens: int, log: list, skip: set | None = None) -> list[dict]:
    """Recent tokens that ran: launched <= 14 d ago (>= 12 h), market cap now >= $1M, liquidity >= $50k.
    `skip` = mints already seeded recently (their early buyers were already scored)."""
    seen, out = set(skip or ()), []
    for path in ("toptrending/24h", "toptraded/24h", "toporganicscore/24h"):
        r = get_json(f"https://api.jup.ag/tokens/v2/{path}?limit=100", max_wait=30, cache_ttl=300)
        rows = r.data if r.ok and isinstance(r.data, list) else []
        log.append(f"jupiter {path}: {'ok' if r.ok else r.error[:80]} ({len(rows)})")
        for t in rows:
            m = t.get("id")
            if not m or m in seen or m in NOT_TRADES or m in QUOTE_USD or m == SOL:
                continue
            seen.add(m)
            try:
                created = datetime.fromisoformat((t.get("createdAt") or "").replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            age_d = (time.time() - created) / 86400
            if 0.5 <= age_d <= 14 and (t.get("mcap") or 0) >= 1e6 and (t.get("liquidity") or 0) >= 5e4 \
                    and (t.get("organicScoreLabel") or "") != "low" and t.get("usdPrice"):
                out.append({"mint": m, "symbol": t.get("symbol"), "age_d": round(age_d, 1), "mcap": t.get("mcap"),
                            "usd_price": t.get("usdPrice"), "dev": t.get("dev"), "created": created})
    out.sort(key=lambda x: x["mcap"], reverse=True)
    return out[:max_tokens]


def early_buyers(tok: dict, sol_usd: float, per_token: int, log: list) -> tuple[list[dict], str | None]:
    """Buy signers from the mint's earliest history (<= 3,000 txs, oldest first). Returns (candidates, deployer)."""
    rows, token, err = [], None, ""
    for _ in range(3):
        page, token, err = txs_for_address(tok["mint"], full=True, sort="asc", limit=1000, token=token,
                                           filters={"status": "succeeded"}, max_cooldown_wait=30)
        rows += page
        if err or not token or not page:
            break
    if not rows:
        log.append(f"seed {tok['symbol']}: no history ({err})")
        return [], tok.get("dev")
    t_first = rows[0].get("blockTime") or 0
    deployer = _keys(rows[0])[0] if _keys(rows[0]) else tok.get("dev")
    now_px_sol = tok["usd_price"] / sol_usd
    buyers: dict[str, dict] = {}
    for tx in rows:
        keys = _keys(tx)
        if not keys:
            continue
        signer = keys[0]
        if signer in (deployer, tok.get("dev")):
            continue
        s = classify(tx, signer, sol_usd)
        if not s or s["side"] != "buy" or s["mint"] != tok["mint"]:
            continue
        if (tx.get("blockTime") or 0) - t_first < 60:
            continue  # sniper / bundle window
        px = s["sol"] / s["qty"] if s["qty"] else None
        if s["sol"] < 0.2 or not px or px > now_px_sol / 5:
            continue
        b = buyers.setdefault(signer, {"wallet": signer, "sol": 0.0, "first_ts": s["ts"], "x_now": 0.0})
        b["sol"] += s["sol"]
        b["x_now"] = max(b["x_now"], round(now_px_sol / px, 1))
    lst = sorted(buyers.values(), key=lambda b: b["sol"], reverse=True)[:per_token]
    log.append(f"seed {tok['symbol']}: {len(rows)} early txs, {len(buyers)} qualifying early buyers, kept {len(lst)}")
    return lst, deployer


def identity_filter(addrs: list[str], w: dict, log: list) -> dict[str, str]:
    """Known exchange/protocol/program labels via Helius Wallet Identity (paid plans; 403 cached) + an on-chain check
    that each address is a plain system-owned, non-executable account. -> {addr: exclusion reason}."""
    bad: dict[str, str] = {}
    for i in range(0, len(addrs), 100):
        chunk = addrs[i:i + 100]
        r = rpc("getMultipleAccounts", [chunk, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}],
                max_cooldown_wait=30)
        vals = ((r.data or {}).get("result") or {}).get("value") if r.ok else None
        if vals is None:
            log.append(f"getMultipleAccounts failed: {r.error[:80]}")
            continue
        for a, v in zip(chunk, vals):
            if v and (v.get("executable") or v.get("owner") != SYSTEM_PROGRAM):
                bad[a] = f"not a plain wallet (owner {v.get('owner')}, executable {v.get('executable')})"
    if w.get("identity_api") == "unavailable (403: paid plan required)":
        return bad
    key = cd._load_helius_key()
    if not key:
        return bad
    for i in range(0, len(addrs), 100):
        chunk = [a for a in addrs[i:i + 100] if a not in bad]
        if not chunk:
            continue
        url = "https://api.helius.xyz/v1/wallet/batch-identity?api-key=" + urllib.parse.quote(key)
        r = post_json(url, {"addresses": chunk}, timeout=30, redact=[key, urllib.parse.quote(key)], retries=1)
        if r.status in (401, 403):
            w["identity_api"] = "unavailable (403: paid plan required)" if r.status == 403 else "unavailable (401)"
            log.append(f"Helius Wallet Identity: {w['identity_api']}; relying on on-chain heuristics only")
            break
        if r.ok and isinstance(r.data, list):
            _usage_add("wallet-batch-identity", 100)
            w["identity_api"] = "ok"
            for ent in r.data:
                if ent.get("address") and ent.get("type") not in (None, "unknown") and \
                        (ent.get("category") or "") not in ("Key Opinion Leader", ""):
                    bad[ent["address"]] = f"labelled {ent.get('name')} ({ent.get('category')})"
    return bad


def fetch_history(wallet: str, days: int = HISTORY_DAYS) -> tuple[list, bool, str]:
    rows, token = [], None
    since = int(time.time() - days * 86400)
    for _ in range(HISTORY_PAGE_CAP):
        page, token, err = txs_for_address(wallet, full=True, sort="desc", limit=1000, token=token,
                                           filters={"blockTime": {"gte": since}, "tokenAccounts": "balanceChanged"},
                                           max_cooldown_wait=30)
        if err:
            return rows, False, err
        rows += page
        if not token or not page:
            return rows, True, ""
    return rows, False, ""  # page cap reached: history truncated (older part missing)


def prescreen(wallet: str) -> tuple[bool, str]:
    """One signatures-only call (10 credits): reject bot-like volume (1,000 txs inside < 5 days) or inactivity."""
    since = int(time.time() - 30 * 86400)
    rows, _, err = txs_for_address(wallet, full=False, sort="desc", limit=1000,
                                   filters={"blockTime": {"gte": since}}, max_cooldown_wait=30)
    if err:
        return False, f"prescreen error: {err}"
    if len(rows) < 30:
        return False, f"only {len(rows)} successful txs in 30 days"
    if len(rows) >= 1000:
        span = (rows[0].get("blockTime") or 0) - (rows[-1].get("blockTime") or 0)
        if span < 5 * 86400:
            return False, f"1000 txs within {span / 86400:.1f} days (>200/day: bot/MEV/exchange-like)"
    return True, ""


def score_wallet(wallet: str, sol_usd: float | None) -> dict:
    txs, complete, err = fetch_history(wallet)
    if err and not txs:
        return {"tier": "error", "why": [err]}
    st = wallet_stats(wallet, txs, sol_usd=sol_usd, history_complete=complete)
    del txs
    tier, why = judge(st)
    rel, missing = reliability(st)
    return {"tier": tier, "why": why, "stats": st, "reliability": rel, "reliability_missing": missing,
            "scored_at": datetime.now().astimezone().isoformat(timespec="seconds"), "scored_ts": time.time(),
            "fetch_error": err or None}


def build(max_tokens: int = 8, per_token: int = 25, max_wallets: int = 120, verbose: bool = True) -> dict:
    if not helius_key_present():
        return {"ok": False, "error": "HELIUS_API_KEY not available"}
    log: list[str] = []
    w = load_watchlist()
    sol_usd = sol_price()
    if not sol_usd:
        return {"ok": False, "error": "no SOL price"}
    recent_seeds = {x["mint"] for x in w.get("seeds") or [] if time.time() - x.get("at", 0) < RESCORE_DAYS * 86400}
    toks = seed_tokens(max_tokens, log, skip=recent_seeds)
    cands: dict[str, list[str]] = {}
    for tok in toks:
        buyers, deployer = early_buyers(tok, sol_usd, per_token, log)
        if deployer:
            w["flagged"].setdefault(deployer, {"reason": "deployer of a seed token (dev wallet: separate signal)",
                                               "tokens": []})
            if tok["mint"] not in w["flagged"][deployer]["tokens"]:
                w["flagged"][deployer]["tokens"].append(tok["mint"])
        for b in buyers:
            cands.setdefault(b["wallet"], []).append(
                f"early buyer of {tok['symbol']} ({tok['mint'][:6]}..., {b['sol']:.2f} SOL, now ~{b['x_now']}x)")
        w["seeds"] = (w.get("seeds") or [])[-50:] + [{"mint": tok["mint"], "symbol": tok["symbol"], "age_d": tok["age_d"],
                                                      "mcap": round(tok["mcap"]), "at": time.time()}]
        if verbose:
            print(log[-1], flush=True)
    now = time.time()
    todo = []
    for a, srcs in cands.items():
        if a in w["flagged"]:
            continue
        e = w["wallets"].get(a) or w["rejected"].get(a)
        if e and now - e.get("scored_ts", 0) < (RESCORE_DAYS if a in w["wallets"] else REJECT_RECHECK_DAYS) * 86400:
            if a in w["wallets"]:
                w["wallets"][a]["discovered_via"] = sorted(set(w["wallets"][a].get("discovered_via", []) + srcs))
            continue
        todo.append(a)
    todo = todo[:max_wallets]
    bad = identity_filter(todo, w, log) if todo else {}
    stats = {"seed_tokens": len(toks), "candidates": len(cands), "to_score": len(todo), "excluded_identity": len(bad),
             "prescreen_rejected": 0, "scored": 0}
    for i, a in enumerate(todo, 1):
        srcs = cands[a]
        if a in bad:
            w["rejected"][a] = {"why": [bad[a]], "scored_ts": now, "discovered_via": srcs}
            continue
        ok, why = prescreen(a)
        if not ok:
            stats["prescreen_rejected"] += 1
            w["rejected"][a] = {"why": [why], "scored_ts": time.time(), "discovered_via": srcs}
            continue
        res = score_wallet(a, sol_usd)
        stats["scored"] += 1
        res["discovered_via"] = srcs
        res["address"] = a
        if res["tier"] in ("tracked", "reliable", "high"):
            w["wallets"][a] = res
            w["rejected"].pop(a, None)
        elif res["tier"] == "dev":
            w["flagged"][a] = {"reason": res["why"][0], "stats": res.get("stats"), "scored_ts": res.get("scored_ts")}
            w["wallets"].pop(a, None)
        else:
            w["rejected"][a] = {"why": res["why"], "scored_ts": res.get("scored_ts", time.time()), "discovered_via": srcs,
                                "stats": res.get("stats")}
            w["wallets"].pop(a, None)
        if verbose:
            print(f"[{i}/{len(todo)}] {a[:6]}... {res['tier']} {('; '.join(res['why']))[:120]}", flush=True)
        if i % 10 == 0:
            save_watchlist(w)
    stats.update(rescored=rescore(w, sol_usd, verbose=verbose, save=False))
    save_watchlist(w)
    stats["tracked_total"] = len(tracked_wallets(w))
    return {"ok": True, "stats": stats, "log": log}


def rescore(w: dict | None = None, sol_usd: float | None = None, verbose: bool = False, save: bool = True) -> int:
    own = w is None
    w = w or load_watchlist()
    sol_usd = sol_usd or sol_price()
    n = 0
    for a, e in list(w["wallets"].items()):
        if time.time() - e.get("scored_ts", 0) < RESCORE_DAYS * 86400:
            continue
        res = score_wallet(a, sol_usd)
        n += 1
        if res["tier"] == "error":
            continue  # keep the old record; retry next time
        res["discovered_via"] = e.get("discovered_via", [])
        res["address"] = a
        if res["tier"] in ("tracked", "reliable", "high"):
            w["wallets"][a] = res
        else:
            w["wallets"].pop(a)
            (w["flagged"] if res["tier"] == "dev" else w["rejected"])[a] = {"why": res["why"], "scored_ts": time.time(),
                                                                             "stats": res.get("stats")}
        if verbose:
            print(f"rescore {a[:6]}... -> {res['tier']}", flush=True)
    if save or own:
        save_watchlist(w)
    return n


# ---------------------------------------------------------------- token lookup
def prefetch_recent(hours: float = 24, wallets: dict | None = None, sol_usd: float | None = None) -> dict:
    """Fetch each tracked wallet's swaps in the last `hours` ONCE per scan (<= 10 credits per 100 txs per wallet),
    so per-token lookups are local. -> {"ok", "swaps": {wallet: [swap]}, "failed": [...], "wallets": n, ...}."""
    wallets = tracked_wallets() if wallets is None else wallets
    out = {"ok": bool(wallets), "hours": hours, "swaps": {}, "failed": [], "wallets": len(wallets), "fetched_at": time.time()}
    if not wallets:
        out["error"] = "no tracked wallets in the watchlist (run: python3 smart_money.py build)"
        return out
    if not helius_key_present():
        out.update(ok=False, error="HELIUS_API_KEY not available")
        return out
    sol_usd = sol_usd or sol_price()
    since = int(time.time() - hours * 3600)
    for a in wallets:
        rows, token, err = txs_for_address(a, full=True, sort="desc", limit=1000,
                                           filters={"blockTime": {"gte": since}, "tokenAccounts": "balanceChanged"},
                                           max_cooldown_wait=20)
        if err:
            out["failed"].append({"wallet": a, "error": err[:120]})
            continue
        sw = [s for s in (classify(tx, a, sol_usd) for tx in rows) if s]
        out["swaps"][a] = sw
        if token:  # > 1000 txs in the window: partial (noted)
            out.setdefault("truncated", []).append(a)
    out["coverage"] = round(len(out["swaps"]) / len(wallets), 3)
    out["ok"] = out["coverage"] > 0
    return out


def _holding(wallet: str, mint: str) -> bool | None:
    r = rpc("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed"}], timeout=15)
    if not r.ok:
        return None
    try:
        vals = r.data["result"]["value"]
        return any(float(v["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) > 0 for v in vals)
    except (KeyError, TypeError, ValueError):
        return None


def _cache() -> dict:
    try:
        return json.load(open(TOKEN_CACHE))
    except (OSError, ValueError):
        return {}


def _cache_put(c: dict) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        now = time.time()
        c = {k: v for k, v in c.items() if now - v.get("ts", 0) < 7 * 86400}
        tmp = TOKEN_CACHE + f".{os.getpid()}.tmp"
        json.dump(c, open(tmp, "w"))
        os.replace(tmp, TOKEN_CACHE)
    except OSError:
        pass


def deployer_of(mint: str, hint: str | None = None) -> tuple[str | None, str]:
    if hint:
        return hint, "RugCheck/Jupiter creator field"
    c = _cache()
    if (c.get("dep:" + mint) or {}).get("v"):
        return c["dep:" + mint]["v"], "Helius first transaction (cached)"
    rows, _, err = txs_for_address(mint, full=True, sort="asc", limit=1, filters={"status": "succeeded"})
    if err or not rows:
        return None, f"unknown ({err or 'no history'})"
    dep = (_keys(rows[0]) or [None])[0]
    c["dep:" + mint] = {"v": dep, "ts": time.time()}
    _cache_put(c)
    return dep, "Helius first transaction fee payer"


def token_supply(mint: str) -> float | None:
    r = rpc("getTokenSupply", [mint], timeout=15)
    try:
        return float(r.data["result"]["value"]["uiAmountString"]) if r.ok else None
    except (KeyError, TypeError, ValueError):
        return None


def top_holders_helius(mint: str, supply: float | None) -> list[dict]:
    """Top non-pool holders via Helius (3 credits): largest token accounts -> owners -> keep system-owned wallets
    (pools/lockers are program-owned PDAs)."""
    if not supply:
        return []
    r = rpc("getTokenLargestAccounts", [mint], timeout=15)
    accts = ((r.data or {}).get("result") or {}).get("value") if r.ok else None
    if not accts:
        return []
    r2 = rpc("getMultipleAccounts", [[a["address"] for a in accts], {"encoding": "jsonParsed"}], timeout=15)
    vals = ((r2.data or {}).get("result") or {}).get("value") if r2.ok else None
    if not vals:
        return []
    rows = []
    for a, v in zip(accts, vals):
        try:
            owner = v["data"]["parsed"]["info"]["owner"]
            rows.append({"owner": owner, "pct": float(a.get("uiAmountString") or a.get("uiAmount") or 0) / supply * 100})
        except (KeyError, TypeError, ValueError):
            continue
    owners = list({x["owner"] for x in rows})
    r3 = rpc("getMultipleAccounts", [owners, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}], timeout=15)
    ov = ((r3.data or {}).get("result") or {}).get("value") if r3.ok else None
    if ov is None:
        return []
    wallets = {o for o, v in zip(owners, ov) if v and v.get("owner") == SYSTEM_PROGRAM and not v.get("executable")}
    agg: dict[str, float] = {}
    for x in rows:
        if x["owner"] in wallets:
            agg[x["owner"]] = agg.get(x["owner"], 0) + x["pct"]
    return [{"owner": o, "pct": p} for o, p in sorted(agg.items(), key=lambda kv: -kv[1])][:10]


DEV_MOVE_PCT = 0.5   # dev transfers out >= 0.5% of supply in the window count as a negative signal


def dev_activity(mint: str, deployer: str, hours: float, supply: float | None = None) -> dict:
    """Deployer's movements of this mint in the window: sells (token out + SOL in) and plain transfers out."""
    since = int(time.time() - hours * 3600)
    rows, _, err = txs_for_address(deployer, full=True, sort="desc", limit=100,
                                   filters={"blockTime": {"gte": since}, "tokenAccounts": "balanceChanged",
                                            "tokenTransfer": {"mint": mint}})
    if err:
        return {"ok": False, "error": err}
    sold = moved = 0.0
    n_sell = n_out = 0
    for tx in rows:
        s = classify(tx, deployer)
        if s and s["mint"] == mint and s["side"] == "sell":
            sold += s["sol"]
            n_sell += 1
            continue
        d = token_deltas(tx, deployer).get(mint, 0.0)
        if d < 0:
            moved += -d
            n_out += 1
    moved_pct = round(moved / supply * 100, 3) if supply else None
    big_move = n_out > 0 and (moved_pct is None or moved_pct >= DEV_MOVE_PCT)
    return {"ok": True, "sells": n_sell, "sold_sol": round(sold, 4), "transfers_out": n_out,
            "tokens_moved_out": round(moved, 2), "moved_pct_supply": moved_pct, "negative": n_sell > 0 or big_move}


def fresh_holders(mint: str, holders: list[dict] | None) -> dict:
    """Share of supply held by fresh wallets (first successful tx < 72 h ago) among the top non-pool holders.
    holders: [{"owner", "pct"}] (e.g. RugCheck top holders with pools/lockers removed)."""
    if not holders:
        return {"ok": False, "error": "no holder list"}
    c = _cache()
    fresh_pct, checked, fresh = 0.0, 0, []
    for h in holders[:10]:
        o = h.get("owner")
        if not o:
            continue
        key = "age:" + o
        first = (c.get(key) or {}).get("v")
        if first is None:
            rows, _, err = txs_for_address(o, full=False, sort="asc", limit=1, filters={"status": "succeeded"})
            if err:
                continue
            first = (rows[0].get("blockTime") if rows else 0) or 0
            c[key] = {"v": first, "ts": time.time()}
        checked += 1
        if first and time.time() - first < FRESH_WALLET_H * 3600:
            fresh_pct += float(h.get("pct") or 0)
            fresh.append({"owner": o, "pct": round(float(h.get("pct") or 0), 2),
                          "age_h": round((time.time() - first) / 3600, 1)})
    _cache_put(c)
    if not checked:
        return {"ok": False, "error": "holder age lookups failed"}
    return {"ok": True, "checked": checked, "fresh": fresh, "fresh_pct": round(fresh_pct, 2),
            "negative": fresh_pct >= FRESH_ELEVATED_PCT}


def token_signal(mint: str, recent: dict | None = None, *, hours: float = 24, deployer: str | None = None,
                 holders: list[dict] | None = None, check_holding: bool = True, established: bool = False) -> dict:
    """S3 reading for one mint. Never invents: missing inputs -> status 'unknown', score None, level None."""
    out: dict[str, Any] = {"mint": mint, "hours": hours, "status": "unknown", "level": None, "score": None,
                           "confidence": 0.5, "insufficient_data": True, "buyers": [], "sellers": [],
                           "negative_flags": [], "explanation": ""}
    w = load_watchlist()
    tw = tracked_wallets(w)
    if not tw:
        out["explanation"] = "UNKNOWN: no scored smart wallets in data/smart_wallets.json (run smart_money.py build)"
        return out
    if recent is None:
        recent = prefetch_recent(hours, tw)
    if not recent.get("ok"):
        out["explanation"] = f"UNKNOWN: tracked-wallet activity unavailable ({recent.get('error') or 'all fetches failed'})"
        return out
    now = time.time()
    for a, sw in recent["swaps"].items():
        mine = [s for s in sw if s["mint"] == mint]
        if not mine:
            continue
        e = tw.get(a, {})
        buys = [s for s in mine if s["side"] == "buy"]
        sells = [s for s in mine if s["side"] == "sell"]
        rec = {"wallet": a, "tier": e.get("tier"), "reliability": e.get("reliability"),
               "win_rate_30d": (e.get("stats") or {}).get("win_rate_30d"),
               "bought_sol": round(sum(s["sol"] for s in buys), 4), "sold_sol": round(sum(s["sol"] for s in sells), 4),
               "first_entry_ago_h": round((now - min(s["ts"] for s in buys)) / 3600, 2) if buys else None,
               "first_entry_slot": min(s["slot"] for s in buys) if buys else None,
               "last_exit_ago_h": round((now - max(s["ts"] for s in sells)) / 3600, 2) if sells else None}
        net = sum(s["qty"] for s in buys) - sum(s["qty"] for s in sells)
        if buys:
            rec["still_holding"] = (_holding(a, mint) if check_holding else None)
            if rec["still_holding"] is None:
                rec["still_holding"] = net > 0.01 * sum(s["qty"] for s in buys) if check_holding is False else None
            out["buyers"].append(rec)
        else:
            out["sellers"].append(rec)
    holders_now = [b for b in out["buyers"] if b.get("still_holding")]
    exited = [b for b in out["buyers"] if b.get("still_holding") is False] + out["sellers"]
    # independence: collapse wallets whose first entry shares a slot
    def independent(rows):
        seen, n = set(), 0
        for b in sorted(rows, key=lambda r: r["first_entry_slot"] or 0):
            if b["first_entry_slot"] in seen:
                continue
            seen.add(b["first_entry_slot"])
            n += 1
        return n
    rel = [b for b in holders_now if b["tier"] in ("reliable", "high")]
    high_fresh = [b for b in holders_now if b["tier"] == "high" and (b["first_entry_ago_h"] or 99) <= FRESH_ENTRY_H]
    unproven = [b for b in holders_now if b["tier"] == "tracked"]
    if independent(high_fresh) >= 3:
        lvl = 4
    elif independent(rel) >= 2:
        lvl = 3
    elif rel:
        lvl = 2
    elif unproven:
        lvl = 1
    else:
        lvl = 0
    # negative signals
    neg = []
    if len(exited) > len(holders_now) and exited:
        neg.append(f"{len(exited)} tracked wallet(s) exited vs {len(holders_now)} holding")
    dev_info = None
    if not established:
        supply = token_supply(mint)
        dep, dep_src = deployer_of(mint, deployer)
        if dep:
            dev_info = dev_activity(mint, dep, hours, supply)
            dev_info.update(deployer=dep, source=dep_src)
            if dev_info.get("negative"):
                neg.append(f"dev/deployer {dep[:6]}... sold {dev_info['sells']}x ({dev_info['sold_sol']} SOL) / "
                           f"moved {dev_info['moved_pct_supply']}% of supply out ({dev_info['transfers_out']} transfers) "
                           f"in {hours:g} h")
        else:
            dev_info = {"ok": False, "error": dep_src}
        if not holders:
            holders = top_holders_helius(mint, supply)
        fh = fresh_holders(mint, holders)
        out["fresh_holders"] = fh
        if fh.get("negative"):
            neg.append(f"fresh wallets (< {FRESH_WALLET_H} h old) hold {fh['fresh_pct']}% among top non-pool holders (bundle risk)")
    out["dev"] = dev_info
    out["negative_flags"] = neg
    raw = lvl
    if neg:
        lvl = 0
    cov = round(len(set(recent.get("swaps") or {}) & set(tw)) / len(tw), 3)  # vs the WHOLE watchlist
    n_rel = sum(1 for e in tw.values() if e.get("tier") in ("reliable", "high"))
    # confidence: coverage of the watchlist fetch x size of the reliable set (a small list makes "none" weak evidence)
    conf = round(min(0.85, 0.6 + 0.05 * min(n_rel, 5)) * cov, 3)
    out.update(status="ok", level=lvl, raw_level=raw, score=lvl / 4, confidence=max(0.5, conf) if cov >= 0.9 else 0.5,
               insufficient_data=cov < 0.9 or n_rel == 0, watchlist_size=len(tw), reliable_in_watchlist=n_rel,
               fetch_coverage=cov)
    parts = [f"S3={lvl}/4 from {len(tw)} tracked wallets ({n_rel} reliable/high; activity fetched for {cov:.0%}) "
             f"over {hours:g} h:"]
    parts.append(f"{len(holders_now)} bought & still hold ({len(rel)} reliable/high, {len(unproven)} unproven), "
                 f"{len(exited)} exited")
    if neg:
        parts.append(f"NEGATIVE (S3 capped 0 from raw {raw}): " + "; ".join(neg))
    if out["insufficient_data"]:
        parts.append("INSUFFICIENT DATA flag: " + ("no reliable (500+ trades/90d) wallets in the watchlist yet"
                                                   if n_rel == 0 else f"only {cov:.0%} of wallets fetched"))
    out["explanation"] = " ".join(parts)
    return out


# ---------------------------------------------------------------- self-tests
def _fake_tx(wallet, ts, slot, lam_delta, mint=None, tok_delta=0.0, fee_payer=None, logs=None):
    keys = [fee_payer or wallet] + ([wallet] if fee_payer and fee_payer != wallet else []) + ["Pool111"]
    idx = keys.index(wallet)
    pre = [10_000_000_000] * len(keys)
    post = list(pre)
    post[idx] += int(lam_delta * 1e9)
    preT, postT = [], []
    if mint:
        preT = [{"accountIndex": 5, "mint": mint, "owner": wallet, "uiTokenAmount": {"uiAmountString": "100"}}]
        postT = [{"accountIndex": 5, "mint": mint, "owner": wallet, "uiTokenAmount": {"uiAmountString": str(100 + tok_delta)}}]
    return {"blockTime": ts, "slot": slot, "transaction": {"signatures": [f"s{slot}"], "message": {
        "accountKeys": keys, "header": {"numRequiredSignatures": 1}}},
        "meta": {"err": None, "preBalances": pre, "postBalances": post, "preTokenBalances": preT,
                 "postTokenBalances": postT, "logMessages": logs or [], "loadedAddresses": {}}}


def selftest() -> list[dict]:
    global RPC
    rows = []

    def add(check, ok, detail, ms=0, status="-"):
        rows.append({"tool": "smart_money", "check": check, "ok": bool(ok), "status": status, "ms": ms,
                     "detail": str(detail)[:220]})
    # 1. key loading never prints the key
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        present = helius_key_present()
        k = cd._load_helius_key() or ""
    add("Helius key loads from env without printing it", present and k and k not in buf.getvalue(),
        f"present={present}; nothing written to stdout/stderr ({len(buf.getvalue())} chars)")
    # 2. offline parsing / round trips / judging
    W, M = "Wallet1111", "Mint11111"
    t0 = 1_700_000_000
    buy = _fake_tx(W, t0, 1, -1.0, M, +50)
    sell = _fake_tx(W, t0 + 600, 2, +1.5, M, -50)
    b, s = classify(buy, W), classify(sell, W)
    add("classify: SOL->token = buy, token->SOL = sell", b and b["side"] == "buy" and s and s["side"] == "sell"
        and abs(b["sol"] - 1.0) < 1e-9, (b, s))
    add("classify: tx the wallet did not sign is ignored", classify(_fake_tx(W, t0, 3, -1, M, 5, fee_payer="Other1"), W) is None, "non-signer")
    closed, _ = round_trips([b, s])
    add("round trip: +0.5 SOL, hold 600 s", len(closed) == 1 and abs(closed[0]["pnl"] - 0.5) < 1e-9 and closed[0]["hold_s"] == 600, closed)
    now = t0 + 86400
    txs = []
    for i in range(20):
        win = i % 4 != 0
        txs += [_fake_tx(W, t0 + i * 1000, 10 + 2 * i, -1.0, f"M{i}", +10),
                _fake_tx(W, t0 + i * 1000 + 300, 11 + 2 * i, +1.3 if win else -0 + 0.7, f"M{i}", -10)]
    st = wallet_stats(W, txs, now=now)
    tier, why = judge(st)
    add("judge: 20 round trips, 75% wins, 300 s holds -> tracked (not reliable: < 500 swaps/90d)",
        tier == "tracked" and st["closed_30d"] == 20 and st["win_rate_30d"] == 0.75, (tier, why, st["win_rate_30d"]))
    fast = []
    for i in range(20):
        fast += [_fake_tx(W, t0 + i * 100, 100 + 2 * i, -1.0, f"F{i}", +10), _fake_tx(W, t0 + i * 100 + 2, 101 + 2 * i, +1.1, f"F{i}", -10)]
    tier2, why2 = judge(wallet_stats(W, fast, now=now))
    add("judge: sub-10 s flips are rejected as bot/MEV", tier2 == "rejected" and any("10 s" in x for x in why2), why2[:2])
    dep = _fake_tx(W, t0, 999, -0.02, logs=[f"Program {PUMP_PROGRAM} invoke [1]", "Program log: Instruction: Create"])
    tier3, _ = judge(wallet_stats(W, txs + [dep], now=now))
    add("judge: wallet that deployed a mint is flagged 'dev', not smart money", tier3 == "dev", tier3)
    # 3. unknown when data is missing (empty watchlist / failed prefetch)
    global WATCHLIST
    old_wl = WATCHLIST
    WATCHLIST = "/nonexistent/smart_wallets.json"
    try:
        sig = token_signal(M, recent={"ok": True, "swaps": {}, "coverage": 1.0})
        add("empty watchlist -> status unknown, score None (never invented)", sig["status"] == "unknown" and sig["score"] is None, sig["explanation"][:120])
    finally:
        WATCHLIST = old_wl
    sig = token_signal(M, recent={"ok": False, "error": "timeout"}) if tracked_wallets() else \
        {"status": "unknown", "score": None, "explanation": "(no watchlist yet: covered by the check above)"}
    add("failed wallet-activity fetch -> unknown", sig["status"] == "unknown" and sig["score"] is None, sig["explanation"][:120])
    # 4. graceful 429 / timeout handling (fake transport; no network)
    old = RPC
    try:
        RPC = lambda m, p, timeout=30: Resp(False, 429, error="polite-limiter: helius cooling down 60s more")  # noqa: E731
        t = time.perf_counter()
        r1 = prefetch_recent(24, {"A" * 44: {"tier": "reliable"}}, sol_usd=150.0)
        sig = token_signal(M, recent=r1)
        add("429 / shared cooldown -> soft failure, no retry storm, S3 unknown",
            not r1["ok"] and r1["failed"] and time.perf_counter() - t < 3 and sig["score"] is None,
            f"{r1['failed'][0]['error'][:70]}; {time.perf_counter() - t:.2f}s")
        def boom(m, p, timeout=30):
            return Resp(False, None, error="The read operation timed out")
        RPC = boom
        rows_, _, err = txs_for_address("B" * 44)
        d, src = deployer_of("C" * 44)
        add("timeout -> error string returned, deployer 'unknown', no exception", err and not rows_ and d is None, f"{err[:50]} | {src[:60]}")
    finally:
        RPC = old
    # 5. live: known-mint lookup (BONK deployer via first tx) and a real wallet-activity call
    if helius_key_present():
        t = time.perf_counter()
        d, src = deployer_of(cd.BONK_MINT)
        add("live: BONK deployer via Helius first transaction", bool(d), f"{(d or '')[:8]}... ({src})",
            int((time.perf_counter() - t) * 1000))
        t = time.perf_counter()
        tw = tracked_wallets()
        if tw:
            one = dict(list(tw.items())[:1])
            rec = prefetch_recent(24, one)
            sig = token_signal(cd.BONK_MINT, recent=rec, established=True)
            add("live: BONK token_signal, 1 of N wallets fetched -> coverage < 90% flags insufficient data",
                sig["status"] in ("ok", "unknown") and sig["explanation"] and (len(tw) == 1 or sig["insufficient_data"]),
                sig["explanation"][:200],
                int((time.perf_counter() - t) * 1000))
        else:
            add("live: token_signal (watchlist empty -> unknown)", token_signal(cd.BONK_MINT)["status"] == "unknown",
                "no tracked wallets yet")
    else:
        add("live: Helius key missing -> module reports unknown", token_signal(cd.BONK_MINT)["status"] == "unknown", "no key")
    return rows


def status() -> dict:
    w = load_watchlist()
    tw = tracked_wallets(w)
    def rng(key):
        v = [e["stats"][key] for e in tw.values() if (e.get("stats") or {}).get(key) is not None]
        return [min(v), max(v)] if v else None
    tiers: dict[str, int] = {}
    for e in tw.values():
        tiers[e["tier"]] = tiers.get(e["tier"], 0) + 1
    try:
        usage = json.load(open(USAGE_FILE))
    except (OSError, ValueError):
        usage = {}
    return {"tracked": len(tw), "tiers": tiers, "flagged_dev": len(w.get("flagged", {})), "rejected": len(w.get("rejected", {})),
            "ranges": {k: rng(k) for k in ("closed_30d", "win_rate_30d", "realized_pnl_sol_30d", "median_hold_s",
                                            "swaps_90d", "swaps_per_day_avg", "median_pnl_pct_30d", "avg_cost_sol_30d")},
            "reliability": [min((e.get("reliability") or 0) for e in tw.values()), max((e.get("reliability") or 0) for e in tw.values())] if tw else None,
            "identity_api": w.get("identity_api"), "updated_at": w.get("updated_at"), "usage": usage}


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only Helius smart-money tracker")
    ap.add_argument("cmd", nargs="?", default="status", choices=["build", "rescore", "token", "status"])
    ap.add_argument("mint", nargs="?")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--max-tokens", type=int, default=8)
    ap.add_argument("--per-token", type=int, default=25)
    ap.add_argument("--max-wallets", type=int, default=120)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        rs = selftest()
        for r in rs:
            print(("PASS" if r["ok"] else "FAIL"), r["check"], "|", r["detail"])
        return 0 if all(r["ok"] for r in rs) else 1
    if a.cmd == "build":
        print(json.dumps(build(a.max_tokens, a.per_token, a.max_wallets), indent=1, default=str))
    elif a.cmd == "rescore":
        print(json.dumps({"rescored": rescore(verbose=True)}))
    elif a.cmd == "token":
        if not a.mint:
            ap.error("token needs a MINT")
        print(json.dumps(token_signal(a.mint, hours=a.hours), indent=1, default=str))
    else:
        print(json.dumps(status(), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
