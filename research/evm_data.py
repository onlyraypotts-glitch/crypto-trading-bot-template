#!/usr/bin/env python3
"""evm_data.py: READ-ONLY EVM memecoin discovery + safety data for the Phantom/FOMO manual-mode scanner.

Chains: Base and Ethereum (Phantom MCP autonomous + Phantom app + FOMO), and the FOMO-only EVM chains BNB Chain,
Monad and Robinhood Chain (manual FOMO cards only; see chain_common.CHAINS for venue facts and sources).

Nothing here signs, sends, swaps, approves, connects a wallet or places an order. HTTP is GET or read-only JSON-RPC
(eth_gasPrice, eth_getTransactionCount, eth_getCode, eth_call) through _http.py (shared polite limiter + cache).
No API keys are needed or read.

Sources (verified live 2026-09-25 unless noted; see multichain-test-2026-09-25.md):
  DexScreener  api.dexscreener.com  token-profiles/latest/v1, token-boosts/latest/v1 (paid promotion = discovery only),
               latest/dex/search, tokens/v1/{chain}/{addrs}  (pairs, liquidity, volume, price change, pairCreatedAt)
  GeckoTerminal api.geckoterminal.com/api/v2 networks/{net}/new_pools, trending_pools, pools/{pool} (fee in name),
               pools/{pool}/ohlcv/hour (shares the CoinGecko per-IP budget: ~10/min)
  GoPlus       api.gopluslabs.io/api/v1/token_security/{chain_id} (free; code 4029 = rate limited -> cooldown)
  honeypot.is  api.honeypot.is/v2/IsHoneypot?address=&chainID= (free, no key; buy/sell simulation, taxes, gas)
  Public RPC   mainnet.base.org, ethereum-rpc.publicnode.com, bsc-rpc.publicnode.com, rpc.monad.xyz (gas price, nonces)
  DefiLlama    coins.llama.fi (native token USD prices)

Candidate records use the same keys as scan_candidates.evaluate/deep_checks/score (mint = contract address, plus
`chain`, `caip2`, `venues`, `security`), so the renderer and the wiring can treat them uniformly.
CLI:  python3 evm_data.py scan --chains base,ethereum [--limit 12] [--json OUT] [--cards]
      python3 evm_data.py token base 0x...        (one token: stage A + deep checks + score)
"""
from __future__ import annotations

import argparse
import json
import urllib.parse
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _http  # noqa: E402
from _http import get_json, post_json, note_rate_limited  # noqa: E402
import chain_common as cc  # noqa: E402
import scoring  # noqa: E402
try:
    import social_data as soc  # noqa: E402
except Exception:  # pragma: no cover
    soc = None
from chain_common import fnum  # noqa: E402

EVM = {  # per-chain data endpoints. None = no known free source (-> UNKNOWN = veto where it matters)
    "base":      {"ds": "base", "gt": "base", "goplus": "8453", "honeypot": 8453,
                  "rpc": ["https://mainnet.base.org", "https://base-rpc.publicnode.com"], "llama": "coingecko:ethereum",
                  "wrapped": "0x4200000000000000000000000000000000000006", "curve_dex": {"virtuals", "zora", "flaunch"}},
    "ethereum":  {"ds": "ethereum", "gt": "eth", "goplus": "1", "honeypot": 1,
                  "rpc": ["https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com"], "llama": "coingecko:ethereum",
                  "wrapped": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "curve_dex": set()},
    "bsc":       {"ds": "bsc", "gt": "bsc", "goplus": "56", "honeypot": 56,
                  "rpc": ["https://bsc-rpc.publicnode.com", "https://bsc-dataseed.bnbchain.org"], "llama": "coingecko:binancecoin",
                  "wrapped": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", "curve_dex": {"fourmeme", "four.meme"}},
    "monad":     {"ds": "monad", "gt": "monad", "goplus": None, "honeypot": None,
                  "rpc": ["https://rpc.monad.xyz"], "llama": "coingecko:monad", "wrapped": None, "curve_dex": {"nadfun", "nad.fun"}},
    "robinhood": {"ds": "robinhood", "gt": None, "goplus": None, "honeypot": None,
                  "rpc": [], "llama": "coingecko:ethereum", "wrapped": None, "curve_dex": set()},
}
# Stables, wrapped majors, LSTs and majors are out of scope (majors belong to the separate Robinhood bot).
NOT_CANDIDATES_SYM = {"WETH", "ETH", "WBTC", "CBBTC", "BTC", "TBTC", "USDC", "USDT", "DAI", "USDS", "USDE", "SUSDE", "FDUSD",
                      "PYUSD", "USDBC", "EURC", "WBNB", "BNB", "STETH", "WSTETH", "RETH", "CBETH", "WEETH", "EZETH",
                      "WMON", "MON", "USD1", "LUSD", "CRVUSD", "GHO", "FRAX", "TUSD", "USDG"}
DEAD = {"0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000001"}
GAS_UNITS = {"buy": 220_000, "sell": 180_000, "approve": 50_000, "revoke": 30_000}  # conservative defaults
DEFAULT_CFG = {"min_liquidity_usd": 50000, "min_volume_24h_usd": 100000, "migration_wait_min": 20, "min_holders": 100,
               "t6_top10_limit_pct": 30, "t6_top10_limit_established_pct": 60, "t7_cluster_veto_pct": 30,
               "t7_cluster_elevated_pct": 15, "max_round_trip_pct": 3.0, "max_impact_per_leg_pct": 3.0, "quote_usd": 100,
               "deep_check_limit": 8, "backtest_horizon_h": 48, "prior_strength": 10, "entry_bar": 0.75,
               "max_tax_pct": 5.0, "owner_addresses": [], "owner_mints": []}


def _lc(a: str | None) -> str:
    return (a or "").lower()


# ------------------------------------------------------------------ discovery
GT_PAIRS: dict[tuple, list] = {}   # (chain, contract) -> DexScreener-shaped pairs synthesized from GeckoTerminal pool data


def gt_pool_to_pair(chain: str, pool: dict, tokens: dict) -> dict | None:
    """GeckoTerminal pool (+ included base token) -> the DexScreener pair fields evaluate() reads. Used when DexScreener
    is rate-limited/banned so a scan isn't empty; values come from the same discovery response (no extra calls)."""
    a = pool.get("attributes") or {}
    rel = pool.get("relationships") or {}
    bid = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
    tok = (tokens.get(bid) or {}).get("attributes") or {}
    addr = _lc(tok.get("address") or bid.split("_", 1)[-1])
    if not addr.startswith("0x"):
        return None
    pcp = a.get("price_change_percentage") or {}
    tx = (a.get("transactions") or {}).get("h24") or {}
    created = cc._iso_ms(a.get("pool_created_at")) if hasattr(cc, "_iso_ms") else None
    if created is None and a.get("pool_created_at"):
        try:
            from datetime import datetime as _dt
            created = int(_dt.fromisoformat(a["pool_created_at"].replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            created = None
    dex = ((rel.get("dex") or {}).get("data") or {}).get("id")
    return {"chainId": EVM[chain]["ds"], "dexId": dex, "pairAddress": a.get("address"), "_source": "geckoterminal",
            "baseToken": {"address": addr, "symbol": tok.get("symbol") or (a.get("name") or "?").split(" / ")[0],
                          "name": tok.get("name") or (a.get("name") or "?").split(" / ")[0]},
            "quoteToken": {"symbol": (a.get("name") or "").split(" / ")[-1].split(" ")[0]},
            "priceUsd": a.get("base_token_price_usd"), "liquidity": {"usd": fnum(a.get("reserve_in_usd"))},
            "volume": {"h24": fnum((a.get("volume_usd") or {}).get("h24"))},
            "priceChange": {k: fnum(pcp.get(g)) for k, g in (("m5", "m5"), ("h1", "h1"), ("h6", "h6"), ("h24", "h24"))},
            "txns": {"h24": {"buys": tx.get("buys"), "sells": tx.get("sells")}}, "pairCreatedAt": created,
            "fdv": fnum(a.get("fdv_usd")), "marketCap": fnum(a.get("market_cap_usd")), "labels": [a.get("name") or ""]}


def discover(chain: str, log: list, max_tokens: int = 60) -> dict[str, set]:
    """-> {contract(lowercase): {sources}} from DexScreener profiles/boosts/search and GeckoTerminal new/trending pools.
    Call budget per chain: 3 DexScreener promo lists (chain-agnostic, cached 10 min so one fetch serves all chains),
    1-2 DexScreener searches (cached 3 min), 2 GeckoTerminal lists (cached 5 min, with base tokens included)."""
    e = EVM[chain]
    out: dict[str, set] = {}

    def add(addr, src):
        a = _lc(addr)
        if a.startswith("0x") and len(a) == 42 and a != _lc(e.get("wrapped")):
            out.setdefault(a, set()).add(src)
    for path, src in (("token-profiles/latest/v1", "ds_profiles"), ("token-boosts/latest/v1", "ds_boosts(paid)"),
                      ("token-boosts/top/v1", "ds_boosts_top(paid)")):
        r = get_json(f"https://api.dexscreener.com/{path}")
        rows = r.data if isinstance(r.data, list) else []
        n0 = len(out)
        for x in rows:
            if (x.get("chainId") or "") == e["ds"]:
                add(x.get("tokenAddress"), src)
        log.append({"source": f"dexscreener {path}", "chain": chain, "ok": r.ok, "status": r.status,
                    "items": len(out) - n0, "note": (r.error or "")[:100]})
    q = {"base": ["base", "aerodrome"], "ethereum": ["WETH"], "bsc": ["WBNB"],
         "monad": ["monad"], "robinhood": ["robinhood chain"]}.get(chain, [chain])
    for term in q:
        r = get_json("https://api.dexscreener.com/latest/dex/search?q=" + term.replace(" ", "%20"))
        n0 = len(out)
        for p in ((r.data or {}).get("pairs") or []) if isinstance(r.data, dict) else []:
            if p.get("chainId") == e["ds"]:
                add((p.get("baseToken") or {}).get("address"), "ds_search")
        log.append({"source": f"dexscreener search '{term}'", "chain": chain, "ok": r.ok, "status": r.status,
                    "items": len(out) - n0, "note": (r.error or "")[:100]})
    if e.get("gt"):
        for kind in ("trending_pools", "new_pools"):
            r = _http.get_json_retry(f"https://api.geckoterminal.com/api/v2/networks/{e['gt']}/{kind}?page=1&include=base_token,dex", cache_ttl=300)
            n0 = len(out)
            toks = {x.get("id"): x for x in ((r.data or {}).get("included") or [])} if isinstance(r.data, dict) else {}
            for p in ((r.data or {}).get("data") or []) if isinstance(r.data, dict) else []:
                bt = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or ""
                add(bt.split("_", 1)[-1], f"gt_{kind}")
                gp = gt_pool_to_pair(chain, p, toks)
                if gp:
                    GT_PAIRS.setdefault((chain, gp["baseToken"]["address"]), []).append(gp)
            log.append({"source": f"geckoterminal {e['gt']}/{kind}", "chain": chain, "ok": r.ok, "status": r.status,
                        "items": len(out) - n0, "note": (r.error or "")[:100]})
    return dict(list(out.items())[:max_tokens])


BLOCKSCOUT = {"base": "https://base.blockscout.com", "ethereum": "https://eth.blockscout.com"}


def v3_fee_pct(chain: str, pool: str | None) -> float | None:
    """Uniswap-v3-style pool fee via a read-only eth_call to fee() (selector 0xddca3f43; units of 1e-6)."""
    if not pool:
        return None
    try:
        ok, res, _ = rpc(chain, "eth_call", [{"to": pool, "data": "0xddca3f43"}, "latest"])
        if ok and isinstance(res, str) and len(res) >= 66:
            v = int(res, 16)
            return v / 1e4 if 0 < v <= 100000 else None
    except Exception:
        return None
    return None


def fallback_discover(chain: str, log: list, max_tokens: int = 25, cfg: dict | None = None) -> dict[str, set]:
    """When BOTH DexScreener and GeckoTerminal are rate-limited (all of 2026-09-25 on a shared IP during testing), build
    candidates from sources that were still answering: Blockscout's token list (price, token-wide 24 h volume incl.
    CEX, market cap, holders), honeypot.is GetPairs (deepest pool: address, liquidity, creation time) and DefiLlama
    (1 h / 6 h / 24 h % change). No candles here, so P_WIN stays at its conservative prior. Base/Ethereum only (keyless
    Blockscout). Pair records are labelled pair_source='fallback (blockscout+honeypot.is+defillama)'."""
    cfg = cfg or DEFAULT_CFG
    base = BLOCKSCOUT.get(chain)
    out: dict[str, set] = {}
    if not base:
        return out
    toks, params = [], ""
    for _ in range(4 if chain == "ethereum" else 3):   # pages of 50, sorted by market cap (Ethereum's first ~100 are > $1B)
        r = get_json(f"{base}/api/v2/tokens?type=ERC-20{params}", cache_ttl=900, timeout=25)
        d = r.data if r.ok and isinstance(r.data, dict) else {}
        toks += d.get("items") or []
        npp = d.get("next_page_params")
        if not npp:
            break
        params = "&" + urllib.parse.urlencode({k: (str(v).lower() if isinstance(v, bool) else v) for k, v in npp.items() if v is not None})
    log.append({"source": f"blockscout {chain} token list (fallback)", "chain": chain, "ok": bool(toks), "status": "-", "items": len(toks)})
    picks = []
    for x in toks:
        sym = (x.get("symbol") or "").upper()
        a = _lc(x.get("address_hash") or x.get("address"))
        vol, mc = fnum(x.get("volume_24h"), 0), fnum(x.get("circulating_market_cap"), 0)
        if not a.startswith("0x") or sym in NOT_CANDIDATES_SYM or a == _lc(EVM[chain].get("wrapped")):
            continue
        if vol < cfg.get("min_volume_24h_usd", 100000) or mc >= 1e9 or not x.get("exchange_rate"):
            continue
        picks.append((vol, a, x))
    picks = sorted(picks, reverse=True)[:max_tokens]
    if not picks:
        return out
    coins = ",".join(f"{chain}:{a}" for _, a, _ in picks)
    pct = {}
    for per in ("1h", "6h", "24h"):
        r = get_json(f"https://coins.llama.fi/percentage/{coins}?period={per}", cache_ttl=300, timeout=25)
        pct[per] = ((r.data or {}).get("coins") or {}) if r.ok and isinstance(r.data, dict) else {}
    n = 0
    for vol, a, x in picks:
        r = get_json(f"https://api.honeypot.is/v1/GetPairs?address={a}&chainID={EVM[chain]['honeypot']}", cache_ttl=1800, timeout=20)
        prs = r.data if r.ok and isinstance(r.data, list) else []
        if not prs:
            continue
        best = max(prs, key=lambda q: fnum(q.get("Liquidity"), 0))
        pname = ((best.get("Pair") or {}).get("Name") or "")
        paddr = _lc((best.get("Pair") or {}).get("Address"))
        fee = v3_fee_pct(chain, paddr) if "v3" in pname.lower() else (0.30 if "v2" in pname.lower() and "uniswap" in pname.lower() else None)
        label = f"{pname} {fee}%" if fee is not None else pname
        k = f"{chain}:{a}"
        ch = {kk: (round(pct[per][k], 2) if isinstance(pct[per].get(k), (int, float)) else None)
              for kk, per in (("h1", "1h"), ("h6", "6h"), ("h24", "24h"))}
        if any(v is None for v in ch.values()):
            continue   # no price-change data -> C2/C3 can't be judged -> not a candidate
        created = best.get("CreatedAtTimestamp")
        pair = {"chainId": EVM[chain]["ds"], "dexId": pname.split(":")[0].strip().lower().replace(" ", "_") or "unknown",
                "pairAddress": paddr, "_source": "fallback (blockscout+honeypot.is+defillama)",
                "baseToken": {"address": a, "symbol": x.get("symbol"), "name": x.get("name")}, "quoteToken": {"symbol": "WETH"},
                "priceUsd": x.get("exchange_rate"), "liquidity": {"usd": fnum(best.get("Liquidity"))},
                "volume": {"h24": fnum(x.get("volume_24h"))}, "priceChange": ch, "txns": {"h24": {}},
                "pairCreatedAt": int(created) * 1000 if created else None, "marketCap": fnum(x.get("circulating_market_cap")),
                "labels": [label], "_volume_note": "token-wide 24 h volume (includes CEX; Blockscout/CoinGecko)"}
        GT_PAIRS.setdefault((chain, a), []).append(pair)
        out.setdefault(a, set()).add("fallback_blockscout_top_volume")
        n += 1
    log.append({"source": f"fallback pools (honeypot.is + DefiLlama) {chain}", "chain": chain, "ok": n > 0, "status": "-", "items": n,
                "note": "DexScreener + GeckoTerminal unavailable; established tokens only, no candles (P_WIN at prior)"})
    return out


def dexscreener_pairs(chain: str, addrs: list[str], log: list) -> dict[str, list]:
    """tokens/v1/{chain}/{up to 30 comma-separated} -> {addr: [pairs where it is the base token]}."""
    e = EVM[chain]
    out: dict[str, list] = {}
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i + 30]
        r = get_json(f"https://api.dexscreener.com/tokens/v1/{e['ds']}/" + ",".join(chunk))
        if not r.ok:
            log.append({"source": "dexscreener tokens/v1", "chain": chain, "ok": False, "status": r.status, "note": r.error[:100]})
            if r.status == 429 or "cooling down" in (r.error or ""):
                break   # don't queue more calls into a ban; GeckoTerminal fallback below
            continue
        for p in r.data if isinstance(r.data, list) else []:
            a = _lc((p.get("baseToken") or {}).get("address"))
            if a in chunk and p.get("chainId") == e["ds"]:
                out.setdefault(a, []).append(p)
    fb = 0
    for a in addrs:
        if a not in out and GT_PAIRS.get((chain, a)):
            out[a] = GT_PAIRS[(chain, a)]
            fb += 1
    if fb:
        log.append({"source": "geckoterminal pool data (fallback pairs)", "chain": chain, "ok": True, "status": "-",
                    "items": fb, "note": "DexScreener pair data missing for these tokens; used fallback pool records (GeckoTerminal pools, or Blockscout+honeypot.is+DefiLlama)"})
    return out


def main_pair(pairs: list) -> dict | None:
    return max(pairs, key=lambda p: fnum((p.get("liquidity") or {}).get("usd"), 0)) if pairs else None


# ------------------------------------------------------------------ security
GOPLUS_PREFETCH: dict[tuple, dict] = {}


def goplus_prefetch(chain: str, addrs: list[str], log: list) -> int:
    """Try one GoPlus call for several EVM contracts (the documented EVM endpoint accepts comma-separated addresses).
    Observed 2026-09-25 without a key: only the FIRST address comes back, so in practice this saves nothing yet; any
    token missing from the reply falls back to its own single call in goplus(). Kept because it costs nothing extra."""
    cid = EVM[chain].get("goplus")
    addrs = [a for a in addrs if (chain, _lc(a)) not in GOPLUS_PREFETCH]
    if not cid or not addrs:
        return 0
    n = 0
    for i in range(0, len(addrs), 20):
        chunk = addrs[i:i + 20]
        url = f"https://api.gopluslabs.io/api/v1/token_security/{cid}?contract_addresses={','.join(chunk)}"
        r = get_json(url, cache_if=lambda d: isinstance(d, dict) and d.get("code") == 1, timeout=25)
        d = r.data if r.ok and isinstance(r.data, dict) else {}
        if d.get("code") == 4029:
            note_rate_limited(url, 0, why="GoPlus code 4029")
            log.append({"source": "goplus batch", "chain": chain, "ok": False, "status": 4029, "note": "rate limited"})
            break
        for a, item in (d.get("result") or {}).items():
            if item:
                GOPLUS_PREFETCH[(chain, _lc(a))] = item
                n += 1
        log.append({"source": "goplus batch", "chain": chain, "ok": r.ok, "status": r.status, "items": len(d.get("result") or {}),
                    "note": (r.error or "")[:80]})
    return n


def goplus(chain: str, addr: str) -> dict:
    cid = EVM[chain].get("goplus")
    if not cid:
        return {"ok": False, "error": f"GoPlus has no {chain} endpoint configured (UNKNOWN)"}
    if (chain, _lc(addr)) in GOPLUS_PREFETCH:
        return {"ok": True, "raw": GOPLUS_PREFETCH[(chain, _lc(addr))]}
    url = f"https://api.gopluslabs.io/api/v1/token_security/{cid}?contract_addresses={addr}"
    r = get_json(url, cache_if=lambda d: isinstance(d, dict) and d.get("code") == 1)
    if not r.ok:
        return {"ok": False, "error": f"GoPlus HTTP {r.status}: {r.error[:100]}"}
    d = r.data or {}
    if d.get("code") == 4029:
        note_rate_limited(url, 0, why="GoPlus code 4029")
        return {"ok": False, "error": "GoPlus rate limited (code 4029)"}
    item = ((d.get("result") or {}).get(_lc(addr))) or next(iter((d.get("result") or {}).values()), None)
    if not item:
        return {"ok": False, "error": f"GoPlus returned no result (code {d.get('code')}, {str(d.get('message'))[:60]})"}
    return {"ok": True, "raw": item}


def honeypot_is(chain: str, addr: str) -> dict:
    cid = EVM[chain].get("honeypot")
    if not cid:
        return {"ok": False, "error": f"honeypot.is does not cover {chain} (UNKNOWN)"}
    r = get_json(f"https://api.honeypot.is/v2/IsHoneypot?address={addr}&chainID={cid}", cache_ttl=600)
    if not r.ok:
        return {"ok": False, "error": f"honeypot.is HTTP {r.status}: {r.error[:100]}"}
    d = r.data or {}
    if not d.get("simulationSuccess"):
        return {"ok": True, "simulation_ok": False, "raw": d,
                "error": f"simulation failed: {str(d.get('simulationError') or d.get('summary'))[:100]}"}
    return {"ok": True, "simulation_ok": True, "raw": d}


def _b(item: dict, k: str) -> bool | None:
    v = item.get(k)
    if v in (None, ""):
        return None
    return str(v) == "1"


def security_facts(chain: str, addr: str, pair_addr: str | None = None) -> dict:
    """Normalized EVM safety facts from GoPlus + honeypot.is. Missing = None (UNKNOWN)."""
    g, h = goplus(chain, addr), honeypot_is(chain, addr)
    f: dict = {"goplus_ok": g["ok"], "honeypot_ok": h["ok"] and h.get("simulation_ok", False),
               "errors": [x["error"] for x in (g, h) if not x["ok"] or x.get("error")]}
    it = g.get("raw") or {}
    hr = h.get("raw") or {}
    sim = hr.get("simulationResult") or {}
    f["symbol"] = it.get("token_symbol") or (hr.get("token") or {}).get("symbol")
    f["name"] = it.get("token_name") or (hr.get("token") or {}).get("name")
    gp_hp = _b(it, "is_honeypot") if it else None
    hp_res = hr.get("honeypotResult") or {}
    if hp_res.get("isHoneypot") or gp_hp is True:
        f["is_honeypot"] = True           # either source says honeypot -> honeypot
    elif h.get("simulation_ok") and hp_res.get("isHoneypot") is False:
        f["is_honeypot"] = False          # a successful buy/sell simulation says sellable
    else:
        f["is_honeypot"] = gp_hp          # GoPlus only (False/None)
    if h.get("ok") and not h.get("simulation_ok"):
        f["is_honeypot"] = f["is_honeypot"] or None  # simulation failed -> UNKNOWN unless GoPlus says honeypot
    taxes = [fnum(sim.get("buyTax")), fnum(it.get("buy_tax"), None)]
    f["buy_tax_pct"] = max([t if t is not None else 0 for t in (taxes[0], (taxes[1] or 0) * 100 if taxes[1] is not None else None)]) \
        if any(t is not None for t in taxes) else None
    st = [fnum(sim.get("sellTax")), fnum(it.get("sell_tax"), None)]
    f["sell_tax_pct"] = max([t if t is not None else 0 for t in (st[0], (st[1] or 0) * 100 if st[1] is not None else None)]) \
        if any(t is not None for t in st) else None
    f["cannot_sell_all"] = _b(it, "cannot_sell_all")
    f["cannot_buy"] = _b(it, "cannot_buy")
    owner = _lc(it.get("owner_address"))
    f["owner_address"] = owner or None
    f["owner_renounced"] = (owner in DEAD) if it else None
    # owner privileges (only matter while an owner exists or a hidden owner can reclaim)
    f["mintable"] = _b(it, "is_mintable")
    f["blacklist"] = _b(it, "is_blacklisted")
    f["whitelist"] = _b(it, "is_whitelisted")
    f["pausable"] = _b(it, "transfer_pausable")
    f["tax_modifiable"] = bool(_b(it, "slippage_modifiable") or _b(it, "personal_slippage_modifiable")) if it else None
    f["balance_modifiable"] = _b(it, "owner_change_balance")
    f["hidden_owner"] = _b(it, "hidden_owner")
    f["take_back_ownership"] = _b(it, "can_take_back_ownership")
    f["selfdestruct"] = _b(it, "selfdestruct")
    f["external_call"] = _b(it, "external_call")
    f["trading_cooldown"] = _b(it, "trading_cooldown")
    f["anti_whale_modifiable"] = _b(it, "anti_whale_modifiable")
    f["proxy"] = _b(it, "is_proxy")
    f["open_source"] = _b(it, "is_open_source")
    f["creator"] = _lc(it.get("creator_address")) or None
    f["creator_pct"] = round(fnum(it.get("creator_percent"), 0) * 100, 3) if it.get("creator_percent") is not None else None
    f["holder_count"] = int(fnum(it.get("holder_count"), 0)) if it.get("holder_count") is not None else \
        (int(fnum((hr.get("token") or {}).get("totalHolders"), 0)) or None)
    # LP lock / burn (GoPlus lp_holders; v3 NFT positions usually aren't listed -> UNKNOWN)
    lps = it.get("lp_holders") or []
    locked = burned = 0.0
    for lp in lps:
        pct = fnum(lp.get("percent"), 0) * 100
        if _lc(lp.get("address")) in DEAD:
            burned += pct
        elif str(lp.get("is_locked")) == "1":
            locked += pct
    f["lp_holders_listed"] = len(lps)
    f["lp_burned_pct"] = round(burned, 2) if lps else None
    f["lp_locked_pct"] = round(locked, 2) if lps else None
    f["lp_locked_or_burned_pct"] = round(locked + burned, 2) if lps else None
    # top holders (non-contract, non-locked, non-dead) -> T6; addresses kept for the fresh-wallet T7 proxy
    hs = it.get("holders") or []
    top = []
    for x in hs:
        a = _lc(x.get("address"))
        if a in DEAD or str(x.get("is_locked")) == "1" or str(x.get("is_contract")) == "1":
            continue
        top.append({"address": a, "pct": round(fnum(x.get("percent"), 0) * 100, 4), "tag": x.get("tag") or ""})
    f["top_nonpool_holders"] = top[:10]
    f["top10_nonpool_pct"] = round(sum(x["pct"] for x in top[:10]), 2) if hs else None
    f["honeypot_flags"] = [x.get("flag") if isinstance(x, dict) else str(x) for x in ((hr.get("summary") or {}).get("flags") or [])]
    f["honeypot_risk"] = (hr.get("summary") or {}).get("risk")
    f["buy_gas"], f["sell_gas"] = fnum(sim.get("buyGas")), fnum(sim.get("sellGas"))
    pr = hr.get("pair") or {}
    f["pair_from_honeypot"] = {"name": ((pr.get("pair") or {}).get("name")), "address": _lc((pr.get("pair") or {}).get("address")),
                               "liquidity_usd": fnum(pr.get("liquidity"))} if pr else None
    f["trusted_token"] = _b(it, "trust_list")
    return f


# ------------------------------------------------------------------ RPC (read-only)
READ_ONLY_RPC = {"eth_gasPrice", "eth_getTransactionCount", "eth_getCode", "eth_call", "eth_blockNumber", "eth_chainId",
                 "eth_getBalance"}


def rpc(chain: str, method: str, params: list) -> tuple[bool, object, str]:
    if method not in READ_ONLY_RPC:
        raise ValueError(f"refusing non-read-only RPC method {method}")
    last = "no public RPC configured"
    for url in EVM[chain]["rpc"]:
        r = post_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15, retries=1)
        if r.ok and isinstance(r.data, dict) and "result" in r.data:
            return True, r.data["result"], url
        last = f"{url}: HTTP {r.status} {(r.error or str((r.data or {}).get('error')))[:80]}"
    return False, None, last


def native_usd(chain: str) -> float | None:
    k = EVM[chain]["llama"]
    r = get_json(f"https://coins.llama.fi/prices/current/{k}")
    return fnum((((r.data or {}).get("coins") or {}).get(k) or {}).get("price")) if r.ok else None


def crash_regime(chain: str, drop_pct: float = 5.0) -> dict:
    """H8(c) crash regime for EVM chains: native gas token (ETH for Base/Ethereum/Robinhood Chain, BNB, MON)
    down >= drop_pct over the last 60 min. UNKNOWN (feed down) is treated as 'no new PASS' by scan()."""
    k = EVM[chain]["llama"]
    r = get_json(f"https://coins.llama.fi/percentage/{k}?period=1h", cache_ttl=120)
    ch = fnum((((r.data or {}).get("coins") or {}).get(k))) if (r.ok and isinstance(r.data, dict)) else None
    return {"chain": chain, "asset": k, "change_60m_pct": ch, "crash": None if ch is None else ch <= -drop_pct,
            "source": "coins.llama.fi/percentage period=1h"}


def gas_costs_usd(chain: str, buy_gas: float | None = None, sell_gas: float | None = None) -> dict:
    ok, gp, src = rpc(chain, "eth_gasPrice", [])
    px = native_usd(chain)
    if not ok or px is None:
        return {"buy": None, "sell": None, "approve": None, "revoke": None, "note": f"gas UNKNOWN ({src if not ok else 'no native price'})"}
    wei = int(gp, 16) * 1.25  # 25% headroom for base-fee moves between quote and fill
    units = dict(GAS_UNITS)
    if buy_gas:
        units["buy"] = max(units["buy"], buy_gas * 1.2)
    if sell_gas:
        units["sell"] = max(units["sell"], sell_gas * 1.2)
    out = {k: round(u * wei / 1e18 * px, 4) for k, u in units.items()}
    out.update(gas_price_gwei=round(int(gp, 16) / 1e9, 4), native_usd=px, rpc=src.split("/")[2] if "//" in src else src)
    return out


def fresh_wallet_pct(chain: str, holders: list[dict], max_nonce: int = 2, max_check: int = 10) -> dict:
    """T7 proxy on EVM (no RugCheck/Bubblemaps equivalent keyless): % of supply held by top non-pool holders whose
    account nonce is <= max_nonce (never or barely sent a tx = typical bundle/sniper distribution wallets)."""
    if not holders:
        return {"ok": False, "pct": None, "note": "no holder list (UNKNOWN)"}
    fresh, checked, errs = 0.0, 0, 0
    for h in holders[:max_check]:
        ok, n, _ = rpc(chain, "eth_getTransactionCount", [h["address"], "latest"])
        if not ok:
            errs += 1
            continue
        checked += 1
        if int(n, 16) <= max_nonce:
            fresh += h["pct"]
    if checked < min(5, len(holders)):
        return {"ok": False, "pct": None, "note": f"only {checked} holders checked ({errs} RPC errors): UNKNOWN"}
    return {"ok": True, "pct": round(fresh, 2), "checked": checked, "note": f"nonce <= {max_nonce} among top {checked}"}


def pool_fee_pct(chain: str, pair: dict | None, sec: dict) -> tuple[float | None, str]:
    """Pool fee per leg. GeckoTerminal pool name carries the tier ('X / WETH 1%'); else honeypot.is pair name;
    else Uniswap-v2-style 0.30% only when DexScreener labels it v2; otherwise UNKNOWN."""
    import re
    e = EVM[chain]
    # Free sources first (GeckoTerminal-fallback pair name, honeypot.is pair name); a GeckoTerminal pool call (shared,
    # tight CoinGecko budget) only when those don't carry the fee tier.
    names = [str(x) for x in ((pair or {}).get("labels") or []) if "%" in str(x)]
    names.append((sec.get("pair_from_honeypot") or {}).get("name"))
    for nm in names:
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", nm or "")
        if m:
            return float(m.group(1)), f"pool name '{nm}'"
    if pair and e.get("gt") and pair.get("pairAddress"):
        r = get_json(f"https://api.geckoterminal.com/api/v2/networks/{e['gt']}/pools/{pair.get('pairAddress')}", cache_ttl=3600)
        nm = (((r.data or {}).get("data") or {}).get("attributes") or {}).get("name") if r.ok else None
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", nm or "")
        if m:
            return float(m.group(1)), f"pool name '{nm}'"
    labels = [str(x).lower() for x in ((pair or {}).get("labels") or [])]
    if "v2" in labels and (pair or {}).get("dexId") in ("uniswap", "sushiswap", "pancakeswap", "baseswap"):
        return (0.25 if (pair or {}).get("dexId") == "pancakeswap" else 0.30), f"{pair.get('dexId')} v2 standard fee"
    return None, "pool fee UNKNOWN"


def ohlcv_hourly(chain: str, pool: str | None, log: list) -> tuple[list, str]:
    e = EVM[chain]
    if not pool or not e.get("gt"):
        return [], "none"
    r = get_json(f"https://api.geckoterminal.com/api/v2/networks/{e['gt']}/pools/{pool}/ohlcv/hour?aggregate=1&limit=1000",
                 cache_ttl=900)
    try:
        lst = r.data["data"]["attributes"]["ohlcv_list"]
        c = sorted((int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in lst)
        if len(c) > 24:
            return c, "geckoterminal 1h"
    except Exception:
        log.append({"source": "geckoterminal ohlcv", "chain": chain, "ok": False, "status": r.status, "note": (r.error or "")[:100]})
    return [], "none"


# ------------------------------------------------------------------ candidate pipeline (same record shape as Solana)
def evaluate(chain: str, addr: str, pairs: list, sources: set, cfg: dict, log: list) -> dict:
    """Stage A (cheap) gates from DexScreener. Same keys as scan_candidates.evaluate + chain fields."""
    cf = cc.CHAINS[chain]
    p = main_pair(pairs)
    rec = {"mint": addr, "chain": chain, "caip2": cf["caip2"], "venues": cc.venues_for(chain),
           "phantom_auto_chain": cf["phantom_auto"], "sources": sorted(sources), "checks": [], "status": None}
    fails = []

    def chk(cid, ok, value, note=""):
        rec["checks"].append({"id": cid, "pass": ok, "value": value, "note": note})
        if ok is False or ok is None:
            fails.append(f"{cid}: {note or value}")
    if not p:
        rec.update(status="REJECT", reason="no DexScreener or GeckoTerminal pool data on this chain (no executable pool data)")
        return rec
    bt = p.get("baseToken") or {}
    sym, name = bt.get("symbol") or "?", bt.get("name") or "?"
    liq = fnum((p.get("liquidity") or {}).get("usd"), 0)
    vol24 = fnum((p.get("volume") or {}).get("h24"), 0)
    pc = dict(p.get("priceChange") or {})
    created_ms = p.get("pairCreatedAt")
    age_min = (time.time() * 1000 - created_ms) / 60000 if created_ms else None
    mcap = fnum(p.get("marketCap")) or fnum(p.get("fdv"))
    txh24 = (p.get("txns") or {}).get("h24") or {}
    rec["_pair"] = p
    rec.update(symbol=sym, name=name, dex=p.get("dexId"), pair=p.get("pairAddress"), price_usd=fnum(p.get("priceUsd")),
               market_cap=mcap, liquidity_usd=liq, volume_24h=vol24, price_change=pc,
               pool_age_h=round(age_min / 60, 1) if age_min else None, txns_24h=txh24, boosted=bool(p.get("boosts")),
               ds_available=p.get("_source") != "geckoterminal", pair_source=p.get("_source") or "dexscreener",
               socials=[s.get("url") for s in ((p.get("info") or {}).get("socials") or []) if isinstance(s, dict)], quote_token=(p.get("quoteToken") or {}).get("symbol"), labels=p.get("labels") or [])
    established = (age_min or 0) > 365 * 24 * 60 and (mcap or 0) >= 3e8
    cls = "major" if (mcap or 0) >= 1e9 and established else ("mid" if (mcap or 0) >= 1e8 else "memecoin")
    rec.update(established=established, cls=cls, major_symbol=None)
    if sym.upper() in NOT_CANDIDATES_SYM or cls == "major":
        chk("scope: memecoins/tokens only (majors, stables, wrapped/LST out of scope)", False, sym, "major/stable/wrapped asset: out of scope")
    if _lc(addr) in {_lc(x) for x in cfg.get("owner_mints", [])}:
        chk("G11 owner's own coin", False, "owner list", "owner's own coin: never traded")
    curve = (p.get("dexId") or "").lower() in EVM[chain]["curve_dex"]
    chk("G10 not on a launchpad bonding curve", not curve, p.get("dexId"), "launchpad bonding-curve market")
    chk("G10 migration wait", age_min is not None and age_min >= cfg["migration_wait_min"],
        f"{age_min:.0f} min" if age_min else "UNKNOWN", "pool younger than MIGRATION_WAIT" if age_min is not None else "pool creation time UNKNOWN")
    chk("liquidity >= min (scanner)", liq >= cfg["min_liquidity_usd"], f"${liq:,.0f}", f"liquidity ${liq:,.0f} < ${cfg['min_liquidity_usd']:,}")
    chk("24h volume >= min (scanner)", vol24 >= cfg["min_volume_24h_usd"], f"${vol24:,.0f}", f"24h volume ${vol24:,.0f} < ${cfg['min_volume_24h_usd']:,}")
    c2, c3, _ = cc.classify_price_action(pc)
    rec.update(c2=c2, c3=c3)
    chk("C2 price action not BLOW-OFF/DISTRIBUTION/COLLAPSE", c2 not in ("BLOW-OFF", "DISTRIBUTION", "COLLAPSE"), c2,
        f"C2 {c2} (1h {pc.get('h1')}%, 6h {pc.get('h6')}%, 24h {pc.get('h24')}%)")
    chk("C3 entry not CHASE", c3 != "CHASE", c3, f"C3 CHASE (1h {pc.get('h1')}%, 6h {pc.get('h6')}%)")
    ntx = (txh24.get("buys") or 0) + (txh24.get("sells") or 0)
    turnover = vol24 / liq if liq else 0
    wash_p = 0.35 if (not established and (turnover > 40 or (ntx and vol24 / ntx > 5000))) else 0.10
    rec["t9_wash_p"] = wash_p
    chk("T9 fake/wash volume p < 0.30 (judgment)", wash_p < 0.30, f"turnover {turnover:.1f}x, avg trade ${vol24 / ntx if ntx else 0:,.0f}",
        f"wash-volume suspicion p={wash_p}")
    if fails:
        rec.update(status="REJECT", reason=fails[0], all_failures=fails)
        return rec
    rec["_stageA_ok"] = True
    return rec


def deep_checks(rec: dict, ctx: dict, cfg: dict, log: list) -> dict:
    """EVM T-checks (prompt: honeypot/sell tax, owner privileges, proxy, LP lock/burn, holders, clusters), costs, news."""
    chain, addr = rec["chain"], rec["mint"]
    fails = []

    def chk(cid, ok, value, note=""):
        rec["checks"].append({"id": cid, "pass": ok, "value": value, "note": note})
        if ok is False or ok is None:
            fails.append(f"{cid}: {note or value}")
    s = security_facts(chain, addr, rec.get("pair"))
    rec["security"] = s
    norm = lambda x: "".join(ch for ch in (x or "").lower() if ch.isalnum())  # noqa: E731
    chk("T11 GoPlus report available", True if s["goplus_ok"] else None, "ok" if s["goplus_ok"] else "UNKNOWN",
        "GoPlus unavailable (UNKNOWN = VETO)")
    chk("T11 honeypot.is simulation available", True if s["honeypot_ok"] else None, "ok" if s["honeypot_ok"] else "UNKNOWN",
        "honeypot.is simulation unavailable (UNKNOWN = VETO)")
    if s["goplus_ok"] or s["honeypot_ok"]:
        rec["address_verified"] = bool(s.get("symbol")) and norm(s.get("symbol")) == norm(rec["symbol"])
        chk("T1 contract matches pair source + security source", rec["address_verified"], f"pair {rec['symbol']} / sec {s.get('symbol')}",
            "symbol mismatch between DexScreener and GoPlus/honeypot.is")
        chk("EVM honeypot (buy+sell simulation)", s["is_honeypot"] is False, s["is_honeypot"],
            "HONEYPOT or sell simulation failed/UNKNOWN")
        mt = cfg.get("max_tax_pct", 5.0)
        chk(f"EVM buy/sell tax <= {mt}%", s["buy_tax_pct"] is not None and s["sell_tax_pct"] is not None
            and max(s["buy_tax_pct"], s["sell_tax_pct"]) <= mt, f"{s['buy_tax_pct']}/{s['sell_tax_pct']}%", "tax too high or UNKNOWN")
        chk("EVM can sell all", s["cannot_sell_all"] is not True, s["cannot_sell_all"], "cannot sell all")
        active_owner = not s["owner_renounced"] or s["hidden_owner"] or s["take_back_ownership"]
        for key, label in (("mintable", "T2 owner can mint"), ("blacklist", "owner blacklist"), ("pausable", "owner can pause transfers"),
                           ("tax_modifiable", "owner can change tax"), ("balance_modifiable", "owner can change balances")):
            v = s.get(key)
            bad = v is True and (active_owner or key == "balance_modifiable")
            chk(f"EVM {label}", (not bad) if v is not None else None, v, f"{label} (owner {'active' if active_owner else 'renounced'})"
                if v is not None else f"{label}: UNKNOWN")
        chk("EVM hidden owner / reclaimable ownership", not (s["hidden_owner"] or s["take_back_ownership"]),
            f"hidden {s['hidden_owner']}, reclaim {s['take_back_ownership']}", "hidden owner or ownership can be taken back")
        chk("EVM proxy/upgradeable contract", s["proxy"] is False, s["proxy"], "proxy/upgradeable (logic can change) or UNKNOWN")
        chk("EVM selfdestruct", s["selfdestruct"] is not True, s["selfdestruct"], "selfdestruct present")
        chk("EVM contract source verified", s["open_source"] is True, s["open_source"], "unverified source code (UNKNOWN = VETO)")
        lp = s["lp_locked_or_burned_pct"]
        if lp is not None and lp >= 90:
            chk("T4 LP locked/burned", True, f"{lp}% (burned {s['lp_burned_pct']}%, locked {s['lp_locked_pct']}%)")
        else:
            chk("T4 LP locked/burned", False if lp is not None else None, f"{lp}%" if lp is not None else "UNKNOWN",
                f"LP only {lp}% locked/burned" if lp is not None else
                "LP lock/burn UNKNOWN (v3/v4 NFT or unlisted LP) = VETO")
        limit = cfg["t6_top10_limit_established_pct"] if rec["established"] else cfg["t6_top10_limit_pct"]
        t10 = s["top10_nonpool_pct"]
        chk(f"T6 top-10 non-pool holders <= {limit}%", t10 is not None and t10 <= limit, f"{t10}%", "holders over limit or UNKNOWN")
        fw = fresh_wallet_pct(chain, s["top_nonpool_holders"])
        rec["fresh_wallets"] = fw
        cl = None if not fw["ok"] else round(fw["pct"] + (s["creator_pct"] or 0), 2)
        t7 = "OK"
        if cl is None:
            chk("T7 cluster/bundle (EVM proxy: fresh top holders + creator %)", None, "UNKNOWN", fw["note"] + " = VETO")
        elif cl > cfg["t7_cluster_veto_pct"]:
            chk("T7 cluster/bundle (EVM proxy: fresh top holders + creator %)", False, f"{cl}%", f"fresh+creator {cl}% > {cfg['t7_cluster_veto_pct']}%")
        else:
            t7 = "ELEVATED" if cl >= cfg["t7_cluster_elevated_pct"] else "OK"
            chk("T7 cluster/bundle (EVM proxy: fresh top holders + creator %)", True, f"{cl}% {t7}")
        rec["t7_state"] = t7
        h = s["holder_count"] or 0
        chk("G10 holders >= 100", h >= cfg["min_holders"], h, f"only {h} holders")
        own = {_lc(x) for x in cfg.get("owner_addresses", [])}
        chk("G11 not the owner's coin", not (s["creator"] and s["creator"] in own), s["creator"] or "unknown creator",
            "creator is an owner-shared address")
    if not fails:
        gas = gas_costs_usd(chain, s.get("buy_gas"), s.get("sell_gas"))
        fee, fee_src = pool_fee_pct(chain, {"pairAddress": rec["pair"], "dexId": rec["dex"], "labels": rec.get("labels")}, s)
        imp = cc.impact_pct(cfg["quote_usd"], rec["liquidity_usd"])
        vc = cc.evm_venue_costs(cfg["quote_usd"], fee, imp, s["buy_tax_pct"] or 0, s["sell_tax_pct"] or 0, gas, rec["liquidity_usd"])
        vc["pool_fee_source"] = fee_src
        rec["costs"] = vc
        chk("T10 impact per leg <= 3%% at $%d (estimate)" % cfg["quote_usd"], imp is not None and imp <= cfg["max_impact_per_leg_pct"],
            f"{imp:.2f}%" if imp is not None else "UNKNOWN", "impact over 3% or UNKNOWN")
        if vc.get("unknown"):
            chk("H17 round trip <= 3%", None, "UNKNOWN", vc["note"] + f" ({fee_src}; {gas.get('note', '')})")
        else:
            any_ok = vc["fomo_ok"] or (vc["phantom_app_ok"] and cc.CHAINS[chain]["phantom_app"]) or \
                (vc["phantom_mcp_ok"] and cc.CHAINS[chain]["phantom_auto"])
            chk("H17 round trip <= 3% on at least one venue (incl. gas)", any_ok,
                f"FOMO {vc['fomo_pct_with_1pct_allowance']}% / Phantom app {vc['phantom_app_pct']}% / MCP {vc['phantom_mcp_pct']}%",
                f"round trip over 3% at ${cfg['quote_usd']} on every venue (FOMO {vc['fomo_pct_with_1pct_allowance']}%, "
                f"MCP {vc['phantom_mcp_pct']}% incl. buy+approve+sell+revoke gas ${sum(gas[k] for k in ('buy','approve','sell','revoke')):.2f})")
    news = cc.news_hits(ctx.get("news") or {}, rec["name"], rec["symbol"], [addr])
    rec["news"] = news
    rec["chain_news"] = cc.chain_news(ctx.get("news") or {}, chain)
    chk("news danger-word scan (veto only)", not news["danger"], f"{news['mentions']} mentions",
        "danger words in recent news: " + "; ".join(f"{d['danger']} {d['title'][:60]}" for d in news["danger"][:2]))
    if fails:
        rec.update(status="REJECT", reason=fails[0], all_failures=fails)
    return rec


def social_factor(rec: dict, ctx: dict, cfg: dict) -> tuple[int, float, dict | None]:
    """S4 from social_data.py (keyless). Disabled/unavailable -> UNKNOWN = 0 at confidence 0.5 (unchanged rule)."""
    if soc is None or not cfg.get("social_enabled", True):
        rec["social"] = {"status": "unknown", "level": None, "explanation": "UNKNOWN: social factor disabled"}
        return 0, 0.5, None
    try:
        sig = soc.token_social(rec["chain"], rec["mint"], rec["symbol"], rec["name"], pair=rec.get("_pair"),
                               sources=rec["sources"], news=ctx.get("news") or {}, cg_trending=ctx.get("cg_trending"),
                               snaps=ctx.setdefault("social_snaps", {}), updates=ctx.setdefault("social_updates", {}),
                               established=rec["established"], mcap=rec.get("market_cap"), extra_links=rec.get("socials"),
                               use_reddit=ctx.get("reddit_budget", 0) > 0)
        if ctx.get("reddit_budget", 0) > 0:
            ctx["reddit_budget"] -= 1
    except Exception as exc:
        sig = {"status": "unknown", "level": 0, "confidence": 0.5, "negative_flags": [], "explanation": f"UNKNOWN: {type(exc).__name__}"}
    rec["social"] = {k: v for k, v in sig.items() if k != "platforms"} | {"platforms": sig.get("platforms", [])}
    if sig.get("status") != "ok":
        return 0, 0.5, sig
    return int(sig["level"]), float(sig["confidence"]), sig


def score(rec: dict, ctx: dict, cfg: dict, log: list) -> dict:
    """Same scoring as scan_candidates.score (prompt weights, 0.75 bar). S1 from estimated impact + liquidity (no depth
    snapshot yet => level 2 max), S2 from holders/clusters, S3 from evm_smart_money if available, S4 UNKNOWN = 0."""
    s, vc = rec["security"], rec["costs"]
    liq = rec["liquidity_usd"]
    imp = vc.get("impact_per_leg_pct") or 99
    lp = s.get("lp_locked_or_burned_pct") or 0
    trend = ctx.get("depth", {}).get((rec["chain"], rec["mint"])) or {"evidence": False}
    if imp > 3:
        s1 = 1
    elif not trend.get("evidence") or not trend.get("stable") or liq < 250_000:
        s1 = 2
    elif imp <= 1 and lp >= 90:
        s1 = 4
    else:
        s1 = 3
    s1_conf = 0.9 if trend.get("evidence") else 0.7
    t10, cl, h = s.get("top10_nonpool_pct") or 100, (rec.get("fresh_wallets") or {}).get("pct") or 0, s.get("holder_count") or 0
    if cl >= 30:
        s2, s2_conf = 0, 0.8
    elif cl >= 15:
        s2, s2_conf = 1, 0.8
    elif t10 < 15 and cl < 5 and h >= 5000:
        s2, s2_conf = 3, 0.75
    elif t10 < 20 and cl < 5 and h >= 1000:
        s2, s2_conf = 3, 0.7
    else:
        s2, s2_conf = 2, 0.7
    s3, s3_conf, sm_sig = 0, 0.5, None
    esm = ctx.get("evm_smart_money")
    if esm is not None:
        try:
            sm_sig = esm.token_signal(rec["chain"], rec["mint"], hours=24, deployer=s.get("creator"))
        except Exception as exc:
            sm_sig = {"status": "unknown", "level": None, "score": None, "explanation": f"UNKNOWN: {type(exc).__name__}"}
        if sm_sig.get("status") == "ok" and sm_sig.get("level") is not None:
            s3, s3_conf = int(sm_sig["level"]), float(sm_sig.get("confidence") or 0.5)
    rec["smart_money"] = sm_sig or {"status": "unknown", "score": None, "level": None, "insufficient_data": True,
                                    "explanation": "UNKNOWN: EVM smart-money factor not wired or no scored wallets"}
    s4, s4_conf, soc_sig = social_factor(rec, ctx, cfg)
    c1, e, c1_conf = cc.trend_stage(rec.get("pool_age_h") or 0, rec["price_change"], rec["established"])
    candles, src = ohlcv_hourly(rec["chain"], rec["pair"], log)
    vol_d = cc.realized_vol_daily(candles)
    b, stop_note = cc.choose_stop(rec["cls"], vol_d, rec["price_change"])
    venue_cost = min([x for x in (vc.get("fomo_pct_with_1pct_allowance"), vc.get("phantom_app_pct") if cc.CHAINS[rec["chain"]]["phantom_app"] else None,
                                  vc.get("phantom_mcp_pct") if cc.CHAINS[rec["chain"]]["phantom_auto"] else None) if x is not None] or [3.0])
    pw = cc.p_win(b, venue_cost, candles, cfg["backtest_horizon_h"], cfg["prior_strength"], cfg["entry_bar"])
    pw.update(candles=len(candles), candle_source=src, stop_method=stop_note, daily_vol_pct=round(vol_d * 100, 2) if vol_d else None)
    c5 = "BUY" if (s1 >= 3 and s2 >= 3 and rec["c2"] in ("ACCUMULATION", "HEALTHY EXPANSION", "BREAKOUT")) else "WATCH"
    # Evidence-based confidences (scoring.py): replaces the old constants C2 0.65 / C3 0.65 / C5 0.60.
    c2_conf, c3_conf, pa_note = scoring.price_action_confidence(cc.classify_price_action, rec["price_change"], candles, rec["c2"], rec["c3"])
    c1_conf, c1_note = scoring.trend_confidence(c1, c1_conf, candles)
    confs = {"S1": s1_conf, "S2": s2_conf, "S3": s3_conf, "S4": s4_conf, "C1": c1_conf, "C2": c2_conf, "C3": c3_conf}
    confs["C5"] = scoring.c5_confidence(confs)
    known = {"S3": (sm_sig or {}).get("status") == "ok", "S4": (soc_sig or {}).get("status") == "ok"}
    agg = scoring.aggregate({"S1": s1, "S2": s2, "S3": s3, "S4": s4}, confs, known, e, cfg["entry_bar"], cfg.get("scoring_mode", "prompt"))
    composite, dconf = agg["gate_composite"], agg["gate_confidence"]
    rec["scores"] = {"S1": s1, "S2": s2, "S3": s3, "S4": s4, "C1": c1, "E": e, "C2": rec["c2"], "C3": rec["c3"], "C5": c5,
                     "confidences": confs, "confidence_notes": {"C1": c1_note, "C2/C3": pa_note},
                     "decision_confidence": dconf, "composite": round(composite, 4), "floors_ok": s1 >= 3 and s2 >= 3,
                     "aggregation": agg}
    rec["p_win"] = pw
    bar = cfg["entry_bar"]
    needs = []
    if s1 < 3:
        needs.append("S1 >= 3 (needs a depth snapshot showing stable liquidity and >= $250k)" if not trend.get("evidence") else f"S1 >= 3 (liquidity ${liq:,.0f})")
    if s2 < 3:
        needs.append(f"S2 >= 3 (top-10 {t10}%, fresh+creator {cl}%, holders {h})")
    for flag in (sm_sig or {}).get("negative_flags") or []:
        needs.append(f"smart-money NEGATIVE signal cleared: {flag}")
    for flag in (soc_sig or {}).get("negative_flags") or []:
        needs.append(f"social NEGATIVE: {flag}")
    if composite < bar:
        needs.append(f"COMPOSITE {composite:.2f} < {bar} (S4 {'measured ' + str(s4) if known['S4'] else 'UNKNOWN = 0'}; "
                     f"S3 {'measured ' + str(s3) if known['S3'] else 'UNKNOWN = 0'}; shadow adjusted {agg['composite_adjusted']:.2f})")
    if dconf < bar:
        needs.append(f"decision confidence {dconf:.2f} < {bar} (lowest: {agg['confidence_set_by']}; shadow adjusted {agg['confidence_adjusted']:.2f})")
    if pw["estimate"] < bar:
        needs.append(f"P_WIN {pw['estimate']:.2f} < {bar}")
    if c5 != "BUY":
        needs.append("C5 = BUY")
    rec["needs"] = needs
    rec["status"] = "PASS" if not needs else "WATCH"
    ref = rec["price_usd"] or 0
    slip = max(cc.BUY_SLIP_FLOOR, min(5.0, 3.0 - (venue_cost - vc.get("expected_slippage_per_leg_pct", 0.25))))
    stop_px = ref * (1 - b)
    rec["plan"] = {"reference_price": ref, "entry_high": ref * (1 + slip / 100), "entry_low": max(stop_px, ref * 0.95),
                   "stop": stop_px, "tp1": ref * (1 + pw["tp1_pct"] / 100), "tp2": ref * (1 + pw["tp2_pct"] / 100),
                   "buy_slippage_pct": round(slip, 2), "permitted_max_pct_equity": round(min(10.0, 2.0 / b), 2), "size_cap_usd": 100,
                   "t7_half": rec.get("t7_state") == "ELEVATED",
                   "venue": ("FOMO" if vc.get("fomo_ok") else ("Phantom app" if vc.get("phantom_app_ok") and cc.CHAINS[rec["chain"]]["phantom_app"] else "none (cost)")),
                   "phantom_mcp_executable_chain": cc.CHAINS[rec["chain"]]["phantom_auto"]}
    return rec


import paths as _paths  # noqa: E402
SNAP_FILE = os.path.join(_paths.SCANS_DIR, "liquidity_snapshots.json")   # shared with scan_candidates (keys "chain:addr" here)


def _load_snaps() -> dict:
    try:
        return json.load(open(SNAP_FILE))
    except (OSError, ValueError):
        return {}


def _depth_trend(snaps: dict, key: str, liq_now: float) -> dict:
    """Same rule as scan_candidates.depth_trend: a snapshot 20-90 min old and liquidity now >= 95% of it = stable."""
    t = time.time()
    old = [x for x in snaps.get(key, []) if 20 * 60 <= t - x[0] <= 90 * 60]
    if not old or not liq_now:
        return {"evidence": False, "note": "no snapshot 20-90 min old (depth stability UNKNOWN)"}
    chg = (liq_now / old[0][1] - 1) * 100 if old[0][1] else None
    return {"evidence": True, "source": "scanner liquidity snapshots", "age_min": round((t - old[0][0]) / 60),
            "change_pct": round(chg, 2) if chg is not None else None, "stable": chg is not None and chg >= -5,
            "growing": chg is not None and chg >= 2}


def _save_snaps(entries: dict) -> None:
    # re-read right before writing so a concurrent Solana write isn't lost (same file, disjoint keys)
    snaps = _load_snaps()
    t = time.time()
    for k, liq in entries.items():
        if liq:
            snaps.setdefault(k, []).append([t, liq])
    for k in list(snaps):
        snaps[k] = [x for x in snaps[k] if t - x[0] < 24 * 3600][-50:]
        if not snaps[k]:
            snaps.pop(k)
    os.makedirs(os.path.dirname(SNAP_FILE), exist_ok=True)
    tmp = SNAP_FILE + ".tmp"
    json.dump(snaps, open(tmp, "w"))
    os.replace(tmp, SNAP_FILE)


def scan(chains: list[str], cfg: dict | None = None, news: dict | None = None, limit_per_chain: int = 40,
         smart_money=None, shared_ctx: dict | None = None) -> dict:
    """Chains run strictly one after another through the shared limiter (never in parallel)."""
    cfg = dict(DEFAULT_CFG, **(cfg or {}))
    t0 = cc.now_et()
    log: list = []
    news = news if news is not None else cc.news_context(log)
    ctx = {"news": news, "evm_smart_money": smart_money, "depth": {}}
    ctx.update(shared_ctx or {})   # social snapshots, CoinGecko trending map, reddit budget shared with the Solana scan
    if soc is not None and cfg.get("social_enabled", True):
        ctx.setdefault("social_snaps", soc.load_snaps())
        ctx.setdefault("social_updates", {})
        ctx.setdefault("reddit_budget", cfg.get("reddit_max_per_scan", 12))
        if ctx.get("cg_trending") is None:
            ctx["cg_trending"] = soc.coingecko_trending_contracts(log)
    snaps = _load_snaps()
    liq_now: dict[str, float] = {}
    results, rejects = [], []
    for chain in chains:
        if chain not in EVM:
            log.append({"source": "evm_data", "chain": chain, "ok": False, "note": "unknown EVM chain"})
            continue
        cr = crash_regime(chain)
        ctx.setdefault("crash", {})[chain] = cr
        log.append({"source": "crash regime (H8c)", "chain": chain, "ok": cr["crash"] is not None, "note": str(cr["change_60m_pct"])})
        cands = discover(chain, log, limit_per_chain)
        pairs = dexscreener_pairs(chain, list(cands), log)
        # Fallback when no candidate got pool data (DexScreener and GeckoTerminal both throttled; a cached promo
        # list can still "succeed", so judge by the pair data we actually have, not by which list calls answered).
        if not any(pairs.get(a) for a in cands) and chain in BLOCKSCOUT and cfg.get("fallback_discovery", True):
            for a, srcs in fallback_discover(chain, log, cfg=cfg).items():
                cands.setdefault(a, set()).update(srcs)
                pairs[a] = list(GT_PAIRS.get((chain, a)) or [])
        stage_a = []
        for a, srcs in cands.items():
            try:
                r = evaluate(chain, a, pairs.get(a, []), srcs, cfg, log)
            except Exception as exc:  # noqa: BLE001  one bad record must not kill the chain
                rejects.append({"chain": chain, "mint": a, "symbol": "?", "status": "REJECT",
                                "reason": f"internal error: {type(exc).__name__}: {str(exc)[:160]}"})
                continue
            if r.get("liquidity_usd"):
                key = f"{chain}:{a}"
                liq_now[key] = r["liquidity_usd"]
                ctx["depth"][(chain, a)] = _depth_trend(snaps, key, r["liquidity_usd"])
                r["depth_trend"] = ctx["depth"][(chain, a)]
            (stage_a if r.get("_stageA_ok") else rejects).append(r)
        stage_a.sort(key=lambda r: math.log10(max(r["liquidity_usd"], 1)) + math.log10(max(r["volume_24h"], 1)), reverse=True)
        for r in stage_a[cfg["deep_check_limit"]:]:
            r.update(status="NOT DEEP-CHECKED", reason=f"ranked below the top {cfg['deep_check_limit']} this run")
            rejects.append(r)
        goplus_prefetch(chain, [r["mint"] for r in stage_a[:cfg["deep_check_limit"]]], log)
        for r in stage_a[:cfg["deep_check_limit"]]:
            deep_checks(r, ctx, cfg, log)
            if r.get("status") == "REJECT":
                rejects.append(r)
                continue
            score(r, ctx, cfg, log)
            results.append(r)
    for r in results + rejects:
        r.pop("_stageA_ok", None)
        r.pop("_pair", None)
    _save_snaps(liq_now)
    if soc is not None and ctx.get("social_updates") and not shared_ctx:
        soc.save_snaps(ctx.get("social_snaps", {}), ctx["social_updates"])
    for r in results:  # H8(c): crash regime (or UNKNOWN) on that chain -> no new PASS; card stays a WATCH
        cr = (ctx.get("crash") or {}).get(r.get("chain")) or {}
        if r.get("status") == "PASS" and cr.get("crash") is not False:
            r["status"] = "WATCH"
            r["reason"] = ("H8(c) crash regime: " if cr.get("crash") else "H8(c) crash regime UNKNOWN: ") + \
                f"{cr.get('asset')} 60-min change {cr.get('change_60m_pct')}%; no new entries"
    results.sort(key=lambda r: (r["status"] == "PASS", r["p_win"]["estimate"], r["scores"]["composite"]), reverse=True)
    return {"generated_et": cc.now_et().strftime("%Y-%m-%d %H:%M:%S ET"), "started_et": t0.strftime("%Y-%m-%d %H:%M:%S ET"),
            "mode": "READ-ONLY research (manual-mode watchlist); no orders, no signing, no approvals, no wallet connection",
            "chains": chains, "crash_regime": ctx.get("crash"), "entry_bar": cfg["entry_bar"], "passes": [r for r in results if r["status"] == "PASS"],
            "watch": [r for r in results if r["status"] == "WATCH"], "rejects": rejects, "sources": log,
            "limiter": _http.limiter_status(), "config": cfg}


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only EVM candidate data (Base/Ethereum + FOMO-only EVM chains)")
    sub = ap.add_subparsers(dest="cmd")
    s1 = sub.add_parser("scan")
    s1.add_argument("--chains", default="base,ethereum")
    s1.add_argument("--limit", type=int, default=40)
    s1.add_argument("--deep", type=int, default=DEFAULT_CFG["deep_check_limit"])
    s1.add_argument("--json")
    s1.add_argument("--cards", action="store_true")
    s2 = sub.add_parser("token")
    s2.add_argument("chain")
    s2.add_argument("address")
    a = ap.parse_args()
    if a.cmd == "scan":
        res = scan([c.strip() for c in a.chains.split(",") if c.strip()], {"deep_check_limit": a.deep}, limit_per_chain=a.limit)
        if a.json:
            json.dump(res, open(a.json, "w"), indent=1, default=str)
        print(f"passes {len(res['passes'])} watch {len(res['watch'])} rejects {len(res['rejects'])}")
        if a.cards:
            for i, r in enumerate(res["passes"] + res["watch"], 1):
                print(cc.move_card(r, i), "\n")
        return 0
    if a.cmd == "token":
        cfg, log = dict(DEFAULT_CFG), []
        addr = _lc(a.address)
        pairs = dexscreener_pairs(a.chain, [addr], log).get(addr, [])
        r = evaluate(a.chain, addr, pairs, {"cli"}, cfg, log)
        if r.get("_stageA_ok"):
            ctx = {"news": cc.news_context(log), "depth": {}}
            deep_checks(r, ctx, cfg, log)
            if r.get("status") != "REJECT":
                score(r, ctx, cfg, log)
        print(json.dumps(r, indent=1, default=str)[:6000])
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
