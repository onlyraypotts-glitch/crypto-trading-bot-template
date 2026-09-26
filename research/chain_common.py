#!/usr/bin/env python3
"""chain_common.py: shared, pure helpers for the multichain (EVM/Sui) research modules. READ-ONLY.

Nothing here signs, sends, swaps, approves, connects a wallet or places an order.
The price-action, stop, backtest and P_WIN helpers are verbatim copies of the pure functions in
scan_candidates.py (copied, not imported, so these modules keep working while that file is being edited).
If scan_candidates.py changes those formulas, keep these in sync (MULTICHAIN_WIRING.md, step 6).

Also here: per-chain venue facts (verified 2026-09-25, sources in VENUE_SOURCES), venue cost math that counts gas for
buy + approve + sell + revoke on EVM (prompt H17 / EVM approval exception), news matching via crypto_news.py, and
MOVE CARD rendering for manual mode (FOMO / Phantom app).
"""
from __future__ import annotations

import math
import random
import re
import statistics
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ------------------------------------------------------------------ venue facts (verified 2026-09-25)
VENUE_SOURCES = {
    "phantom_mcp_buy_schema": "Phantom MCP `buy` tool schema (as of 2026-09-25): sell/buy chain must be solana:*, eip155:* "
                              "or hypercore:*; examples eip155:1 Ethereum, eip155:8453 Base. No sui:* namespace.",
    "phantom_mcp_docs": "https://docs.phantom.com/phantom-mcp-server ('Sui support has been deprecated'); "
                        "https://docs.phantom.com/llms.txt ('Monad and Sui support have been deprecated')",
    "phantom_sui_end": "https://help.phantom.com/articles/ending-support-for-sui-in-phantom-53868478441491 "
                       "(Sui support ended 2026-09-24: no view/send/swap)",
    "phantom_networks": "https://help.phantom.com/articles/supported-networks-chains-in-phantom-41372840389651 "
                        "(Arbitrum, BSC unsupported in the app)",
    "phantom_cli_source": "@phantom/cli 2.0.1 src/utils/swap.ts + allowance.ts (npm): EVM same-chain buy with an ERC-20 sell "
                          "token auto-sends approve(allowanceTarget, approvalExactAmount ?? sellAmount) before the swap "
                          "(resets a non-zero allowance to 0 first); it never revokes afterwards.",
    "fomo_chains": "https://help.fomo.family/en/articles/14436558-supported-chains (Solana, Base, BNB Chain, Monad, "
                   "Ethereum, Robinhood Chain); https://help.fomo.family/en/articles/15170883-what-is-fomo",
    "fomo_fees": "https://help.fomo.family/en/articles/14436214-trading-fees-on-fomo (Solana tiered; every other network "
                 "0.50% per trade plus network fees paid by the user)",
    "fomo_min_sell": "https://help.fomo.family/en/articles/14728938-why-can-t-i-buy-or-sell-this-token (min sell 2 USDC; "
                     "5 USDC on Ethereum)",
    "fomo_addresses": "https://help.fomo.family/en/articles/14436186-where-to-find-your-wallet-addresses (one EVM address "
                      "for Base, BNB Chain, Monad, Ethereum, Robinhood Chain)",
}

# key -> facts. phantom_auto = autonomous Phantom MCP swaps allowed by the prompt; phantom_app = manual Phantom app venue;
# fomo = manual FOMO venue. Chains with no venue are research-only (never a MOVE CARD).
CHAINS = {
    "solana":    {"name": "Solana", "caip2": "solana:mainnet", "phantom_auto": True, "phantom_app": True, "fomo": True,
                  "native": "SOL", "fomo_min_sell": 2.0, "explorer": "solscan.io"},
    "base":      {"name": "Base", "caip2": "eip155:8453", "chain_id": 8453, "phantom_auto": True, "phantom_app": True,
                  "fomo": True, "native": "ETH", "fomo_min_sell": 2.0, "explorer": "basescan.org"},
    "ethereum":  {"name": "Ethereum", "caip2": "eip155:1", "chain_id": 1, "phantom_auto": True, "phantom_app": True,
                  "fomo": True, "native": "ETH", "fomo_min_sell": 5.0, "explorer": "etherscan.io"},
    "bsc":       {"name": "BNB Chain", "caip2": "eip155:56", "chain_id": 56, "phantom_auto": False, "phantom_app": False,
                  "fomo": True, "native": "BNB", "fomo_min_sell": 2.0, "explorer": "bscscan.com"},
    "monad":     {"name": "Monad", "caip2": "eip155:143", "chain_id": 143, "phantom_auto": False, "phantom_app": False,
                  "fomo": True, "native": "MON", "fomo_min_sell": 2.0, "explorer": "monad.socialscan.io"},
    "robinhood": {"name": "Robinhood Chain", "caip2": "eip155:4663", "chain_id": 4663, "phantom_auto": False,
                  "phantom_app": True, "fomo": True, "native": "ETH", "fomo_min_sell": 2.0,
                  "explorer": "robinhoodchain.blockscout.com"},
    "sui":       {"name": "Sui", "caip2": "sui:mainnet", "phantom_auto": False, "phantom_app": False, "fomo": False,
                  "native": "SUI", "fomo_min_sell": None, "explorer": "suivision.xyz"},
}
PHANTOM_APP_FEE_PCT = 0.85   # per swap (prompt §2E.8)
FOMO_EVM_FEE_PCT = 0.50      # per trade on non-Solana networks, plus network fees (FOMO help)
MAX_RT_PCT = 3.0             # H17 (never loosened)
BUY_SLIP_FLOOR = 0.3         # §2B / §2E.8 binding rule


def fnum(x, default=None):
    try:
        if x is None or x == "":
            return default
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def now_et() -> datetime:
    return datetime.now(ET)


def venues_for(chain: str) -> list[str]:
    c = CHAINS.get(chain, {})
    out = []
    if c.get("fomo"):
        out.append("FOMO")
    if c.get("phantom_app"):
        out.append("Phantom app")
    return out


# ------------------------------------------------------------------ copies of scan_candidates.py pure functions
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
    return c2, c3, 0.65


def trend_stage(age_h: float, pc: dict, established: bool) -> tuple[str, float, float]:
    h24 = fnum(pc.get("h24"), 0.0)
    if established:
        return ("EXPANDING", 0.5, 0.6) if h24 > 5 else ("CROWDED", 0.1, 0.55)
    if age_h < 48 and h24 > 0:
        return "EMERGING", 0.85, 0.55
    if age_h < 48:
        return "DECLINING", 0.0, 0.55
    return ("EXPANDING", 0.5, 0.55) if h24 > 0 else ("DECLINING", 0.0, 0.55)


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
                break
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
    a = b + cost_pct / 100.0
    prior = b / (a + b)
    bt = backtest(candles, b, a, horizon) if candles else {"wins": 0, "losses": 0, "timeouts": 0, "n": 0}
    alpha = prior * m + bt["wins"]
    beta_ = (1 - prior) * m + bt["losses"] + bt["timeouts"]
    s = beta_summary(alpha, beta_, bar)
    return {"stop_pct": round(b * 100, 2), "tp1_pct": round(a * 100, 2), "tp1_R": round(a / b, 3),
            "tp2_pct": round((2 * b + cost_pct / 100) * 100, 2), "prior_martingale": round(prior, 4),
            "backtest": bt, "posterior": s, "estimate": s["mean"],
            "label": "self-answered (uncalibrated)", "passes_bar": s["mean"] >= bar}


# ------------------------------------------------------------------ costs
def impact_pct(trade_usd: float, liquidity_usd: float) -> float | None:
    """Constant-product estimate per leg: trade / (quote-side reserve + trade), reserve ~ half the pool's USD liquidity
    (prompt §2E.8 fallback). Conservative for concentrated-liquidity pools."""
    if not liquidity_usd or liquidity_usd <= 0:
        return None
    r = liquidity_usd / 2
    return trade_usd / (r + trade_usd) * 100


def evm_venue_costs(size_usd: float, pool_fee_pct: float | None, impact_leg_pct: float | None, buy_tax_pct: float,
                    sell_tax_pct: float, gas_usd: dict, liq_usd: float) -> dict:
    """Round trip per venue as % of position (H17: fees, gas, slippage, impact and token taxes, both ways).
    gas_usd = {"buy":, "sell":, "approve":, "revoke":} in USD (None = unknown -> venue not OK).
    - Phantom MCP (autonomous): no Phantom fee; gas for buy + approve + sell + revoke (prompt's EVM exception).
    - FOMO: 0.50% per trade + network fees (buy + sell; FOMO's own approval handling is not documented, so the approve
      gas is counted too, conservatively). FOMO's slippage setting is unverified -> 1% buy-slippage allowance (§2E.8).
    - Phantom app (manual): 0.85% per swap + gas buy + approve + sell (+ revoke, which the card asks for)."""
    exp_slip = 0.1 if liq_usd >= 250_000 else 0.25
    out = {"size_usd": size_usd, "expected_slippage_per_leg_pct": exp_slip, "pool_fee_pct": pool_fee_pct,
           "impact_per_leg_pct": None if impact_leg_pct is None else round(impact_leg_pct, 4),
           "buy_tax_pct": buy_tax_pct, "sell_tax_pct": sell_tax_pct, "gas_usd": gas_usd}
    if pool_fee_pct is None or impact_leg_pct is None or any(gas_usd.get(k) is None for k in ("buy", "sell", "approve", "revoke")):
        out.update(unknown=True, phantom_mcp_ok=False, fomo_ok=False, phantom_app_ok=False,
                   note="pool fee, impact or gas UNKNOWN -> round trip UNKNOWN -> SKIP (H17)")
        return out
    base = 2 * pool_fee_pct + 2 * impact_leg_pct + 2 * exp_slip + buy_tax_pct + sell_tax_pct
    g = gas_usd
    mcp = base + (g["buy"] + g["approve"] + g["sell"] + g["revoke"]) / size_usd * 100
    fomo = base + 2 * FOMO_EVM_FEE_PCT + (g["buy"] + g["approve"] + g["sell"]) / size_usd * 100
    fomo_allow = fomo - exp_slip + 1.0
    app = base + 2 * PHANTOM_APP_FEE_PCT + (g["buy"] + g["approve"] + g["sell"] + g["revoke"]) / size_usd * 100
    fixed_mcp = g["buy"] + g["approve"] + g["sell"] + g["revoke"]
    pct_only = base
    min_viable_mcp = fixed_mcp / ((MAX_RT_PCT - pct_only) / 100) if pct_only < MAX_RT_PCT else None
    fixed_fomo = g["buy"] + g["approve"] + g["sell"]
    pct_fomo = base + 2 * FOMO_EVM_FEE_PCT - exp_slip + 1.0
    min_viable_fomo = fixed_fomo / ((MAX_RT_PCT - pct_fomo) / 100) if pct_fomo < MAX_RT_PCT else None
    slip_tol_mcp = min(5.0, MAX_RT_PCT - (mcp - exp_slip))
    out.update(unknown=False, phantom_mcp_pct=round(mcp, 3), fomo_pct_at_size=round(fomo, 3),
               fomo_pct_with_1pct_allowance=round(fomo_allow, 3), phantom_app_pct=round(app, 3),
               phantom_mcp_buy_slippage_tolerance_pct=round(slip_tol_mcp, 2),
               phantom_mcp_ok=mcp <= MAX_RT_PCT and slip_tol_mcp >= BUY_SLIP_FLOOR,
               fomo_ok=fomo_allow <= MAX_RT_PCT, phantom_app_ok=app <= MAX_RT_PCT and (app - exp_slip) <= MAX_RT_PCT - BUY_SLIP_FLOOR,
               min_viable_usd_phantom_mcp=None if min_viable_mcp is None else round(min_viable_mcp, 2),
               min_viable_usd_fomo=None if min_viable_fomo is None else round(min_viable_fomo, 2))
    return out


# ------------------------------------------------------------------ news (crypto_news.py items)
DANGER_WORDS = ["hack", "hacked", "exploit", "drained", "rug", "rugpull", "rug pull", "delist", "delisting", "sec ",
                "lawsuit", "halt", "paused", "outage", "insolvent", "scam", "phishing", "vulnerability"]


def news_context(log: list, hours: int = 48) -> dict:
    """Headlines from crypto_news.py (RSS + status feeds). Failure never enables a buy."""
    try:
        import crypto_news as cn
        items, errs = cn.fetch_all(True)
        dw = [k.strip() for k in getattr(cn, "RISK_KEYWORDS", DANGER_WORDS)]
    except Exception as exc:
        log.append({"source": "news feeds", "ok": False, "note": repr(exc)[:120]})
        return {"items": [], "errors": [repr(exc)], "danger_words": DANGER_WORDS}
    cutoff = time.time() - hours * 3600
    items = [i for i in items if not i.get("_ts") or i["_ts"] >= cutoff]
    log.append({"source": "news feeds (crypto_news.py)", "ok": True, "items": len(items), "note": "; ".join(errs)[:160]})
    return {"items": items, "errors": errs, "danger_words": dw}


def news_hits(news: dict, name: str, sym: str, extra_terms: list[str] | None = None) -> dict:
    terms = [t for t in {name or "", sym or "", f"${sym}" if sym else ""} | set(extra_terms or []) if t and len(t) >= 3]
    hits, danger = [], []
    dws = news.get("danger_words") or DANGER_WORDS
    for it in news.get("items", []):
        hay = f" {it.get('title', '')} {it.get('summary', '')} ".lower()
        if any(re.search(r"(?<![a-z0-9])" + re.escape(t.lower()) + r"(?![a-z0-9])", hay) for t in terms):
            dw = [k for k in dws if k in hay]
            rec = {"title": (it.get("title") or "")[:140], "source": it.get("source"), "time_et": it.get("time_et"),
                   "danger": dw, "weight": it.get("weight", 1.0), "possible_injection": it.get("possible_injection")}
            hits.append(rec)
            if dw and it.get("weight", 1.0) >= 0.5 and not it.get("possible_injection"):
                danger.append(rec)
    return {"mentions": len(hits), "danger": danger[:5], "sample": hits[:3]}


def chain_news(news: dict, chain: str, limit: int = 3) -> list[dict]:
    """Chain-level headlines (e.g. 'Base', 'Ethereum') for card context; injection-flagged items are excluded."""
    nm = CHAINS.get(chain, {}).get("name", chain)
    terms = {"base": ["Base", "Coinbase L2"], "ethereum": ["Ethereum", "ETH"], "bsc": ["BNB Chain", "BNB", "BSC"],
             "monad": ["Monad"], "robinhood": ["Robinhood Chain"], "sui": ["Sui"], "solana": ["Solana", "SOL"]}.get(chain, [nm])
    out = []
    for it in news.get("items", []):
        if it.get("possible_injection"):
            continue
        t = it.get("title") or ""
        if any(re.search(r"(?<![A-Za-z0-9])" + re.escape(x) + r"(?![A-Za-z0-9])", t) for x in terms):
            out.append({"title": t[:120], "source": it.get("source"), "time_et": it.get("time_et")})
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ MOVE CARD (manual mode)
def _px(x):
    if not x:
        return "?"
    if x >= 1:
        return f"${x:,.4f}"
    return f"${x:.{max(2, -int(math.floor(math.log10(x))) + 3)}f}"


def move_card(r: dict, n: int = 1, venue: str | None = None, valid_min: int = 15, equity_note: str | None = None) -> str:
    """Plain-text MOVE CARD (prompt §2E.4) for a scored candidate record from evm_data / sui_data.
    WATCH/REJECT records produce a WATCHLIST card (clearly not a BUY). No links, full contract, custom slippage."""
    chain = r.get("chain", "?")
    cf = CHAINS.get(chain, {})
    vs = venues_for(chain)
    if not vs:
        return (f"NO CARD: {r.get('name')} (${r.get('symbol')}) on {cf.get('name', chain)} — no supported venue "
                f"(Phantom and FOMO don't support this chain). Research only.")
    c = r.get("costs") or {}
    if venue is None:
        venue = "FOMO" if "FOMO" in vs and c.get("fomo_ok") else ("Phantom app" if "Phantom app" in vs and c.get("phantom_app_ok") else vs[0])
    pl, pw = r.get("plan") or {}, r.get("p_win") or {}
    is_buy = r.get("status") == "PASS"
    action = "BUY" if is_buy else "WATCH (not a buy)"
    ts = now_et()
    valid = (ts.timestamp() + valid_min * 60)
    valid_s = datetime.fromtimestamp(valid, ET).strftime("%-I:%M %p ET")
    slip = pl.get("buy_slippage_pct")
    rt = c.get("fomo_pct_with_1pct_allowance") if venue == "FOMO" else c.get("phantom_app_pct")
    L = [f"MOVE CARD #{n}: {action}. Mode MANUAL. Venue {venue}. Chain {cf.get('name', chain)} ({cf.get('caip2', '?')}).",
         f"* Token {r.get('name')} (${r.get('symbol')}), FULL contract {r.get('mint')} on {cf.get('name', chain)}. "
         f"Never choose by ticker or name: copycats share tickers, and the same ticker exists on other chains.",
         f"* Amount: ≤ PERMITTED_MAX = {pl.get('permitted_max_pct_equity', '?')}% of MANUAL_EQUITY, never above $100 until 20 closed trades"
         + (" · T7 ELEVATED → half" if pl.get("t7_half") else "") + (f" ({equity_note})" if equity_note else "")
         + f". Venue minimum viable size for 3% round trip ≈ ${c.get('min_viable_usd_fomo') if venue == 'FOMO' else '?'}.",
         f"* Entry range {_px(pl.get('entry_low'))}–{_px(pl.get('entry_high'))} (ref {_px(pl.get('reference_price'))}). "
         f"Valid until {valid_s} (15 min). Outside the range or past the time → SKIP; never chase."]
    if venue == "FOMO":
        L.append(f"* FOMO steps: Search → paste the full contract → confirm the chain shows {cf.get('name', chain)} and the "
                 f"contract matches character by character → a WARNING label means no buy → custom amount (not a preset) → "
                 f"set slippage {slip if slip else 1.0}% if offered (not documented by FOMO; if absent, check the quote) → "
                 f"execute only if the fee line is 0.50% + a network fee that keeps the total round trip ≤ 3% "
                 f"(this card's estimate {rt}%), and impact ≤ 3%. On {cf.get('name', chain)} you pay the network fee (FOMO pays gas only on Solana).")
        L.append(f"* Selling in FOMO: open the token from your profile's open positions → Sell → choose % → confirm the quote. "
                 f"Minimum sell ${cf.get('fomo_min_sell')} on {cf.get('name', chain)}; keep each sell and the remainder above it or sell 100%. "
                 f"FOMO documents no spot stop/limit orders (stop/TP only for perps, which are forbidden), so I alert you when a level is hit.")
    else:
        L.append(f"* PHANTOM APP steps: Swap → network {cf.get('name', chain)} → pay with {cf.get('native')} (never a token that needs an approval) → "
                 f"paste the full contract → match it → amount → Slippage Custom {slip}% (never Auto) → execute only if impact ≤ 3%, "
                 f"the Phantom fee ≤ 0.85% and the network fee keeps the round trip ≤ 3% (estimate {rt}%).")
        L.append("* Selling an EVM token in the Phantom app will ask for a token approval. Accept only if Phantom shows the EXACT amount "
                 "you are selling (never 'unlimited') on Phantom's own swap screen; otherwise cancel and tell me. Revoke it afterwards.")
    L.append("* Anything different (another token or chain, a higher fee, an unlimited approval, a signature/permit or 'connect' request) → CANCEL.")
    L.append(f"* Stop {_px(pl.get('stop'))} (−{pw.get('stop_pct')}%); TP1 {_px(pl.get('tp1'))} (+{pw.get('tp1_pct')}%); TP2 {_px(pl.get('tp2'))}. "
             f"Invalidation: liquidity −20% from entry, a security flag change (tax, owner, pause, blacklist), or a honeypot result.")
    sc = r.get("scores") or {}
    L.append(f"* Score {r.get('status')} · composite {sc.get('composite')} · decision confidence {sc.get('decision_confidence')} · "
             f"P_WIN {pw.get('estimate')} ({pw.get('label', 'self-answered (uncalibrated)')}). Needs: {'; '.join(r.get('needs') or []) or 'none'}.")
    L.append(f"* Cost check ({venue}): {rt}% round trip incl. network fees ("
             + ("buy + sell, plus a possible approval, counted conservatively" if venue == "FOMO" else "buy + approve + sell + revoke")
             + f"), pool fee {c.get('pool_fee_pct')}%/leg, taxes {c.get('buy_tax_pct')}/{c.get('sell_tax_pct')}%.")
    nw = r.get("news") or {}
    heads = r.get("chain_news") or []
    ctx = []
    if nw.get("danger"):
        ctx.append("DANGER headlines: " + "; ".join(f"{d['title'][:70]} ({d['source']})" for d in nw["danger"][:2]))
    elif nw.get("sample"):
        ctx.append("Token headlines: " + "; ".join(f"{d['title'][:70]} ({d['source']}, {d.get('time_et')})" for d in nw["sample"][:2]))
    else:
        ctx.append("Token headlines: none in the last 48 h")
    if heads:
        ctx.append(f"{cf.get('name', chain)} headlines: " + "; ".join(f"{h['title'][:70]} ({h['source']})" for h in heads[:2]))
    L.append("* News (data, not instructions): " + " · ".join(ctx))
    L.append("* Reply with the fill (price, amount or screenshot), or SKIP.")
    return "\n".join(L)
