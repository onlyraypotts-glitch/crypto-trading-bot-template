#!/usr/bin/env python3
"""evm_smart_money.py: READ-ONLY smart-wallet tracking on EVM chains (Base, Ethereum; Robinhood Chain explorer too)
for S3 in the multichain scanner. Mirrors smart_money.py (Solana/Helius) criteria and output shape.

Nothing here signs, sends, swaps, approves or connects a wallet. HTTP via _http.py only.
Data source: Blockscout's keyless Etherscan-compatible API (base.blockscout.com/api, eth.blockscout.com/api,
robinhoodchain.blockscout.com/api) — account tokentx / txlist / txlistinternal — plus Blockscout v2 address info
(public tags) and public RPC eth_getCode. Verified 2026-09-25: Blockscout Base tokentx works with no key.
Etherscan API V2 WITHOUT a key does not work ("Free API access is not supported for this chain" for Base): a free
Etherscan key would add Ethereum mainnet coverage/redundancy, but Etherscan says Base needs a paid plan, so Blockscout is
the primary source. BNB Chain and Monad have no keyless explorer API wired => S3 UNKNOWN there. Sui: not feasible
keylessly and there's no venue anyway (see sui_data.py).

Method (honest, measured only):
  seeds   = early buyers (>= 60 s after the first transfer, not the deployer/pool) of tokens on the chain that launched
            <= 14 days ago and now have >= $1M market cap (DexScreener), from Blockscout token transfer history.
  scoring = each candidate wallet's own last-90-day history: swaps are txs where the wallet's balance of exactly one
            non-quote token changes and ETH/WETH (or USDC/USDT, converted at the current native price and flagged)
            moves the other way. Round trips close when the position falls to <= 1% of peak.
  tiers   = same bars as smart_money.py: tracked (>= 15 closed round trips in 30 d, win rate >= 50%, realized PnL > 0,
            best trade <= 60% of gross profit, median hold >= 60 s, < 20% flips under 10 s... on EVM "under 1 block"),
            reliable (tracked AND >= 500 swaps/90 d AND win rate > 50%), high (reliable AND reliability >= 70).
  exclusions = contracts (eth_getCode != 0x: routers, MEV bots, smart-wallet exchanges), explorer-tagged exchanges /
            bridges / MEV, deployers (created a contract in the window), same-block buy+sell (MEV), > 100 swaps/day.
  score   = reliability / 100 in [0, 1] for tracked+ wallets; None ("unknown") when history is missing or below sample.
CLI:
  python3 evm_smart_money.py build base [--max-tokens 4] [--per-token 20]
  python3 evm_smart_money.py wallet base 0x...
  python3 evm_smart_money.py token base 0x... [--hours 24]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _http import get_json  # noqa: E402
import chain_common as cc  # noqa: E402
from chain_common import fnum  # noqa: E402

BLOCKSCOUT = {"base": "https://base.blockscout.com", "ethereum": "https://eth.blockscout.com",
              "robinhood": "https://robinhoodchain.blockscout.com"}
QUOTE_NATIVE = {"base": {"0x4200000000000000000000000000000000000006"},
                "ethereum": {"0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"}, "robinhood": set()}
QUOTE_USD = {"base": {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2"},
             "ethereum": {"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "0xdac17f958d2ee523a2206206994597c13d831ec7"},
             "robinhood": set()}
EXCHANGE_TAG_WORDS = ("exchange", "binance", "coinbase", "kraken", "okx", "bybit", "kucoin", "gate", "htx", "bitget",
                      "mev", "bot", "bridge", "router", "deployer", "hot wallet", "cex")
import paths as _paths  # noqa: E402
DATA = os.path.join(_paths.BASE_DATA_DIR, "evm_smart_wallets.json")
MIN_CLOSED_30D, MIN_WIN_RATE, MAX_TOP_TRADE_SHARE = 15, 0.50, 0.60
MIN_MEDIAN_HOLD_S, MAX_FAST_FLIP_FRAC, MAX_SWAPS_PER_DAY, MAX_SWAPS_ONE_DAY = 60, 0.20, 100, 300
RELIABLE_SWAPS_90D, HIGH_RELIABILITY, FRESH_ENTRY_H = 500, 70, 6


def _lc(a):
    return (a or "").lower()


def bs(chain: str, **params) -> tuple[bool, list | str]:
    base = BLOCKSCOUT.get(chain)
    if not base:
        return False, f"no keyless explorer API for {chain}"
    q = "&".join(f"{k}={v}" for k, v in params.items())
    r = get_json(f"{base}/api?{q}", cache_ttl=300, timeout=25)
    if not r.ok:
        return False, f"HTTP {r.status}: {r.error[:80]}"
    d = r.data or {}
    res = d.get("result")
    if isinstance(res, list):
        return True, res
    if str(d.get("message", "")).lower().startswith("no ") or res in (None, []):
        return True, []
    return False, str(res or d.get("message"))[:120]


def address_info(chain: str, addr: str) -> dict:
    base = BLOCKSCOUT.get(chain)
    r = get_json(f"{base}/api/v2/addresses/{addr}", cache_ttl=86400, timeout=20) if base else None
    if not r or not r.ok or not isinstance(r.data, dict):
        return {"ok": False}
    d = r.data
    tags = [str(t.get("display_name") or t.get("label") or "") for t in (d.get("public_tags") or [])]
    tags += [str(t.get("name") or "") for t in (((d.get("metadata") or {}).get("tags")) or [])]
    return {"ok": True, "is_contract": bool(d.get("is_contract")), "name": d.get("name"), "tags": [t for t in tags if t]}


def excluded(chain: str, addr: str) -> str | None:
    info = address_info(chain, addr)
    if not info.get("ok"):
        return None
    if info.get("is_contract"):
        return "contract (router / MEV bot / smart-wallet exchange)"
    hay = " ".join([info.get("name") or ""] + info.get("tags", [])).lower()
    for w in EXCHANGE_TAG_WORDS:
        if w in hay:
            return f"explorer tag '{w}'"
    return None


# ------------------------------------------------------------------ history -> swaps
def swaps_from_history(chain: str, wallet: str, tokentx: list, txlist: list, internal: list, native_usd: float | None) -> tuple[list, int, int]:
    """-> (swaps in smart_money format with 'sol' = native amount, deploys, same_block_flips)."""
    w = _lc(wallet)
    qn, qu = QUOTE_NATIVE.get(chain, set()), QUOTE_USD.get(chain, set())
    by: dict[str, dict] = {}
    for t in tokentx:
        h = t.get("hash")
        dec = int(fnum(t.get("tokenDecimal"), 18) or 18)
        amt = fnum(t.get("value"), 0) / 10 ** dec
        sgn = 1 if _lc(t.get("to")) == w else (-1 if _lc(t.get("from")) == w else 0)
        if not sgn:
            continue
        e = by.setdefault(h, {"tok": {}, "native": 0.0, "usd": 0.0, "ts": int(fnum(t.get("timeStamp"), 0)), "block": int(fnum(t.get("blockNumber"), 0))})
        c = _lc(t.get("contractAddress"))
        if c in qn:
            e["native"] += sgn * amt
        elif c in qu:
            e["usd"] += sgn * amt
        else:
            e["tok"][c] = e["tok"].get(c, 0.0) + sgn * amt
    deploys = 0
    for t in txlist:
        if _lc(t.get("from")) == w and not t.get("to") and t.get("contractAddress"):
            deploys += 1
        h = t.get("hash")
        if h in by and _lc(t.get("from")) == w and str(t.get("isError", "0")) == "0":
            by[h]["native"] -= fnum(t.get("value"), 0) / 1e18
    for t in internal:
        h = t.get("hash")
        if h in by and _lc(t.get("to")) == w and str(t.get("isError", "0")) == "0":
            by[h]["native"] += fnum(t.get("value"), 0) / 1e18
    swaps = []
    for h, e in by.items():
        toks = {k: v for k, v in e["tok"].items() if abs(v) > 0}
        if len(toks) != 1:
            continue
        mint, qty = next(iter(toks.items()))
        quote_native = e["native"]
        quote = "native"
        if abs(quote_native) < 1e-12 and abs(e["usd"]) > 0 and native_usd:
            quote_native, quote = e["usd"] / native_usd, "usd(converted at current native price)"
        if qty > 0 and quote_native < 0:
            side = "buy"
        elif qty < 0 and quote_native > 0:
            side = "sell"
        else:
            continue
        swaps.append({"side": side, "mint": mint, "qty": abs(qty), "sol": abs(quote_native), "ts": e["ts"], "slot": e["block"],
                      "sig": h, "quote": quote})
    blocks: dict[tuple, set] = {}
    for s in swaps:
        blocks.setdefault((s["mint"], s["slot"]), set()).add(s["side"])
    flips = sum(1 for v in blocks.values() if v == {"buy", "sell"})
    return swaps, deploys, flips


def round_trips(swaps: list[dict]) -> tuple[list[dict], dict]:
    """Copy of smart_money.round_trips (same cycle definition)."""
    pos: dict[str, dict] = {}
    closed = []
    for s in sorted(swaps, key=lambda x: (x["ts"], x["slot"])):
        p = pos.get(s["mint"])
        if s["side"] == "buy":
            if p is None:
                p = pos[s["mint"]] = {"qty": 0.0, "peak": 0.0, "cost": 0.0, "proceeds": 0.0, "t0": s["ts"], "slot0": s["slot"], "t1": s["ts"], "n": 0}
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


def wallet_stats(swaps: list, deploys: int, flips: int, now: float | None = None, complete: bool = True) -> dict:
    now = now or time.time()
    t30 = now - 30 * 86400
    closed, open_pos = round_trips(swaps)
    c30 = [c for c in closed if c["t1"] >= t30]
    wins = [c for c in c30 if c["pnl"] > 0]
    losses = [c for c in c30 if c["pnl"] <= 0]
    gw = sum(c["pnl"] for c in wins)
    days: dict[str, int] = {}
    for s in swaps:
        d = datetime.fromtimestamp(s["ts"], timezone.utc).strftime("%Y-%m-%d")
        days[d] = days.get(d, 0) + 1
    span = max(1.0, (now - min((s["ts"] for s in swaps), default=now)) / 86400)
    sw30 = [s for s in swaps if s["ts"] >= t30]
    holds = [c["hold_s"] for c in c30]
    eq = peak = mdd = 0.0
    for c in sorted(c30, key=lambda c: c["t1"]):
        eq += c["pnl"]
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    weeks: dict[int, float] = {}
    for c in c30:
        k = int((now - c["t1"]) // (7 * 86400))
        weeks[k] = weeks.get(k, 0) + c["pnl"]
    return {"swaps_90d": len(swaps), "swaps_30d": len(sw30), "history_complete_90d": complete, "closed_30d": len(c30),
            "wins_30d": len(wins), "win_rate_30d": round(len(wins) / len(c30), 4) if c30 else None,
            "realized_pnl_native_30d": round(sum(c["pnl"] for c in c30), 6),
            "median_pnl_pct_30d": round(statistics.median([c["pnl_pct"] for c in c30]), 2) if c30 else None,
            "median_hold_s": int(statistics.median(holds)) if holds else None,
            "fast_flip_frac": round(sum(1 for c in c30 if c["slots"] <= 1) / len(c30), 4) if c30 else None,
            "same_block_flips": flips, "swaps_per_day_avg": round(len(sw30) / min(30.0, span), 2) if swaps else 0.0,
            "swaps_max_day": max(days.values()) if days else 0,
            "top_trade_share": round(max((c["pnl"] for c in wins), default=0) / gw, 4) if gw > 0 else None,
            "avg_win": round(gw / len(wins), 6) if wins else None,
            "avg_loss": round(sum(c["pnl"] for c in losses) / len(losses), 6) if losses else None,
            "max_drawdown_30d": round(mdd, 6), "positive_weeks": sum(1 for v in weeks.values() if v > 0), "weeks_traded": len(weeks),
            "pnl_last_7d": round(sum(c["pnl"] for c in c30 if c["t1"] >= now - 7 * 86400), 6),
            "open_positions": len(open_pos), "deploy_txs": deploys,
            "usd_converted_swaps": sum(1 for s in swaps if s["quote"] != "native")}


def reliability(st: dict) -> float | None:
    if not st.get("closed_30d"):
        return None
    comp = {}
    med, pnl = st.get("median_pnl_pct_30d") or 0.0, st.get("realized_pnl_native_30d") or 0.0
    comp["profit"] = (25, max(0.0, min(1.0, 0.5 + med / 100)) if pnl > 0 else 0.0)
    comp["consistency"] = (15, st["positive_weeks"] / st["weeks_traded"] if st.get("weeks_traded") else 0.0)
    comp["win_rate"] = (15, max(0.0, min(1.0, ((st.get("win_rate_30d") or 0) - 0.3) / 0.5)))
    comp["recent"] = (10, 1.0 if (st.get("pnl_last_7d") or 0) > 0 else 0.0)
    gw = (st.get("avg_win") or 0) * (st.get("wins_30d") or 0)
    comp["drawdown"] = (10, max(0.0, 1 - st["max_drawdown_30d"] / gw) if gw > 0 else 0.0)
    aw, al = st.get("avg_win"), st.get("avg_loss")
    comp["exits"] = (5, min(1.0, aw / abs(al) / 2) if aw and al else (1.0 if aw else 0.0))
    comp["copyability"] = (5, min(1.0, (st.get("median_hold_s") or 0) / 1800))
    w = sum(v[0] for v in comp.values())
    return round(sum(v[0] * v[1] for v in comp.values()) / w * 100, 1)


def judge(st: dict) -> tuple[str, list[str]]:
    if st.get("deploy_txs"):
        return "dev", [f"created {st['deploy_txs']} contract(s) in the window (deployer: excluded)"]
    why = []
    if st["closed_30d"] < MIN_CLOSED_30D:
        why.append(f"closed round trips 30d {st['closed_30d']} < {MIN_CLOSED_30D}")
    if st["win_rate_30d"] is None or st["win_rate_30d"] < MIN_WIN_RATE:
        why.append(f"win rate {st['win_rate_30d']} < {MIN_WIN_RATE}")
    if st["realized_pnl_native_30d"] <= 0:
        why.append("realized PnL <= 0")
    if st["top_trade_share"] is not None and st["top_trade_share"] > MAX_TOP_TRADE_SHARE:
        why.append(f"one-trade concentration {st['top_trade_share']:.0%}")
    if st["median_hold_s"] is not None and st["median_hold_s"] < MIN_MEDIAN_HOLD_S:
        why.append(f"median hold {st['median_hold_s']}s < {MIN_MEDIAN_HOLD_S}s (bot-like)")
    if (st["fast_flip_frac"] or 0) >= MAX_FAST_FLIP_FRAC or st["same_block_flips"] > 0:
        why.append(f"same/next-block flips ({st['same_block_flips']} same-block) (MEV/bot)")
    if st["swaps_per_day_avg"] > MAX_SWAPS_PER_DAY or st["swaps_max_day"] > MAX_SWAPS_ONE_DAY:
        why.append(f"{st['swaps_per_day_avg']} swaps/day (bot)")
    if why:
        return "rejected", why
    rel = reliability(st) or 0
    if st["swaps_90d"] >= RELIABLE_SWAPS_90D and (st["win_rate_30d"] or 0) > 0.5:
        return ("high" if rel >= HIGH_RELIABILITY else "reliable"), []
    return "tracked", []


def score_wallet(chain: str, wallet: str, native_usd: float | None = None) -> dict:
    ex = excluded(chain, wallet)
    if ex:
        return {"wallet": wallet, "chain": chain, "tier": "excluded", "why": [ex], "score": None, "status": "excluded"}
    since = int(time.time() - 90 * 86400)
    ok1, tt = bs(chain, module="account", action="tokentx", address=wallet, sort="desc", offset=1000, page=1)
    ok2, tl = bs(chain, module="account", action="txlist", address=wallet, sort="desc", offset=1000, page=1)
    ok3, it = bs(chain, module="account", action="txlistinternal", address=wallet, sort="desc", offset=1000, page=1)
    if not (ok1 and ok2 and ok3):
        return {"wallet": wallet, "chain": chain, "tier": "unknown", "status": "unknown", "score": None,
                "why": [f"history unavailable: {x}" for x, ok in ((tt, ok1), (tl, ok2), (it, ok3)) if not ok]}
    tt = [t for t in tt if int(fnum(t.get("timeStamp"), 0)) >= since]
    complete = len(tt) < 1000
    swaps, deploys, flips = swaps_from_history(chain, wallet, tt, [t for t in tl if int(fnum(t.get("timeStamp"), 0)) >= since], it, native_usd)
    st = wallet_stats(swaps, deploys, flips, complete=complete)
    tier, why = judge(st)
    rel = reliability(st) if tier in ("tracked", "reliable", "high") else None
    return {"wallet": wallet, "chain": chain, "tier": tier, "why": why, "stats": st, "reliability": rel,
            "score": round(rel / 100, 4) if rel is not None else None,
            "status": "ok" if rel is not None else ("insufficient_data" if tier == "rejected" and st["closed_30d"] < MIN_CLOSED_30D else tier),
            "scored_at": int(time.time())}


# ------------------------------------------------------------------ seeds + watchlist
def load() -> dict:
    try:
        return json.load(open(DATA))
    except Exception:
        return {"version": 1, "wallets": {}, "rejected": {}}


def save(w: dict) -> None:
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    tmp = DATA + ".tmp"
    json.dump(w, open(tmp, "w"), indent=1)
    os.replace(tmp, DATA)


def seed_tokens(chain: str, max_tokens: int, log: list) -> list[dict]:
    import evm_data as ed
    cands = ed.discover(chain, log, 60)
    pairs = ed.dexscreener_pairs(chain, list(cands), log)
    out = []
    for a, ps in pairs.items():
        p = ed.main_pair(ps)
        age_d = (time.time() * 1000 - (p.get("pairCreatedAt") or 0)) / 86400000
        mc = fnum(p.get("marketCap")) or fnum(p.get("fdv")) or 0
        if age_d <= 14 and mc >= 1e6:
            out.append({"token": a, "pair": _lc(p.get("pairAddress")), "mcap": mc, "symbol": (p.get("baseToken") or {}).get("symbol")})
    return sorted(out, key=lambda x: -x["mcap"])[:max_tokens]


def early_buyers(chain: str, tok: dict, per_token: int) -> list[str]:
    ok, tt = bs(chain, module="account", action="tokentx", contractaddress=tok["token"], sort="asc", offset=300, page=1)
    if not ok or not tt:
        return []
    t0 = int(fnum(tt[0].get("timeStamp"), 0))
    deployer = _lc(tt[0].get("to"))
    out = []
    for t in tt:
        if int(fnum(t.get("timeStamp"), 0)) < t0 + 60:
            continue  # snipers / bundles inside the first 60 s are not candidates
        if _lc(t.get("from")) == tok["pair"]:
            a = _lc(t.get("to"))
            if a not in out and a != deployer and a not in cc.CHAINS:
                out.append(a)
        if len(out) >= per_token:
            break
    return out


def build(chain: str, max_tokens: int = 4, per_token: int = 20) -> dict:
    import evm_data as ed
    log: list = []
    w = load()
    px = ed.native_usd(chain)
    toks = seed_tokens(chain, max_tokens, log)
    n = 0
    for tok in toks:
        for a in early_buyers(chain, tok, per_token):
            key = f"{chain}:{a}"
            if key in w["wallets"] or key in w["rejected"]:
                continue
            r = score_wallet(chain, a, px)
            n += 1
            (w["wallets"] if r["tier"] in ("tracked", "reliable", "high") else w["rejected"])[key] = r
    save(w)
    return {"ok": True, "chain": chain, "seed_tokens": [t["symbol"] for t in toks], "scored": n,
            "tracked": sum(1 for k in w["wallets"] if k.startswith(chain + ":")), "log": log[-6:]}


def token_signal(chain: str, token: str, hours: float = 24, deployer: str | None = None) -> dict:
    """S3 reading for one EVM token: same fields/levels as smart_money.token_signal. Missing data -> 'unknown'."""
    token = _lc(token)
    out = {"chain": chain, "mint": token, "hours": hours, "status": "unknown", "level": None, "score": None, "confidence": 0.5,
           "insufficient_data": True, "buyers": [], "sellers": [], "negative_flags": [], "explanation": ""}
    if chain not in BLOCKSCOUT:
        out["explanation"] = f"UNKNOWN: no keyless explorer API for {chain}"
        return out
    tw = {k.split(":", 1)[1]: v for k, v in load()["wallets"].items() if k.startswith(chain + ":")}
    if not tw:
        out["explanation"] = f"UNKNOWN: no scored {chain} smart wallets (run evm_smart_money.py build {chain})"
        return out
    since = time.time() - hours * 3600
    fails = 0
    for a, e in tw.items():
        ok, tt = bs(chain, module="account", action="tokentx", address=a, contractaddress=token, sort="desc", offset=100, page=1)
        if not ok:
            fails += 1
            continue
        mine = [t for t in tt if int(fnum(t.get("timeStamp"), 0)) >= since]
        ins = [t for t in mine if _lc(t.get("to")) == a]
        outs = [t for t in mine if _lc(t.get("from")) == a]
        if not mine:
            continue
        rec = {"wallet": a, "tier": e.get("tier"), "reliability": e.get("reliability"),
               "first_entry_ago_h": round((time.time() - min(int(t["timeStamp"]) for t in ins)) / 3600, 2) if ins else None,
               "first_entry_slot": min(int(t["blockNumber"]) for t in ins) if ins else None}
        net = sum(fnum(t["value"], 0) for t in ins) - sum(fnum(t["value"], 0) for t in outs)
        rec["still_holding"] = net > 0
        (out["buyers"] if ins else out["sellers"]).append(rec)
    if fails and fails == len(tw):
        out["explanation"] = "UNKNOWN: tracked-wallet activity unavailable (explorer errors)"
        return out
    holders = [b for b in out["buyers"] if b["still_holding"]]
    exited = [b for b in out["buyers"] if not b["still_holding"]] + out["sellers"]

    def indep(rows):
        return len({r["first_entry_slot"] for r in rows})
    rel = [b for b in holders if b["tier"] in ("reliable", "high")]
    hf = [b for b in holders if b["tier"] == "high" and (b["first_entry_ago_h"] or 99) <= FRESH_ENTRY_H]
    lvl = 4 if indep(hf) >= 3 else (3 if indep(rel) >= 2 else (2 if rel else (1 if holders else 0)))
    neg = []
    if exited and len(exited) > len(holders):
        neg.append(f"{len(exited)} tracked wallet(s) exited vs {len(holders)} holding")
    if deployer:
        ok, tt = bs(chain, module="account", action="tokentx", address=deployer, contractaddress=token, sort="desc", offset=100, page=1)
        if ok:
            outs = [t for t in tt if _lc(t.get("from")) == _lc(deployer) and int(fnum(t.get("timeStamp"), 0)) >= since]
            if outs:
                neg.append(f"deployer {deployer[:8]}... moved/sold tokens {len(outs)}x in {hours:g} h")
    if neg:
        lvl = 0
    out.update(status="ok", level=lvl, score=lvl / 4, confidence=0.8 if fails == 0 else 0.6, insufficient_data=False,
               negative_flags=neg, coverage=f"{len(tw) - fails}/{len(tw)} wallets",
               explanation=f"S3 {lvl}/4 from {len(holders)} tracked holder(s), {len(exited)} exit(s) in {hours:g} h" + (f"; NEGATIVE: {'; '.join(neg)}" if neg else ""))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only EVM smart-wallet tracking (Blockscout, keyless)")
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build"); b.add_argument("chain"); b.add_argument("--max-tokens", type=int, default=4); b.add_argument("--per-token", type=int, default=20)  # noqa: E702
    w = sub.add_parser("wallet"); w.add_argument("chain"); w.add_argument("address")  # noqa: E702
    t = sub.add_parser("token"); t.add_argument("chain"); t.add_argument("address"); t.add_argument("--hours", type=float, default=24)  # noqa: E702
    a = ap.parse_args()
    if a.cmd == "build":
        print(json.dumps(build(a.chain, a.max_tokens, a.per_token), indent=1, default=str))
    elif a.cmd == "wallet":
        import evm_data as ed
        print(json.dumps(score_wallet(a.chain, _lc(a.address), ed.native_usd(a.chain)), indent=1, default=str))
    elif a.cmd == "token":
        print(json.dumps(token_signal(a.chain, a.address, a.hours), indent=1, default=str))
    else:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
