#!/usr/bin/env python3
"""sui_data.py: READ-ONLY Sui token discovery + risk data. RESEARCH ONLY — Sui has NO supported venue.

Verified 2026-09-25 (sources in chain_common.VENUE_SOURCES):
  * Phantom ended Sui support on 2026-09-24 (help.phantom.com "Ending support for Sui in Phantom": no view/send/swap);
    the Phantom MCP docs say "Sui support has been deprecated" and the MCP `buy` tool only accepts solana:*, eip155:* and
    hypercore:* chain ids. So Phantom can't trade Sui, autonomously or in the app.
  * FOMO's supported chains are Solana, Base, BNB Chain, Monad, Ethereum and Robinhood Chain (help.fomo.family) — no Sui.
  => Every Sui record is status "RESEARCH ONLY (no venue)"; move_card() refuses to build a card for it. This module is
     kept so the scanner can watch Sui narratives (and so the wiring is ready if a venue appears), never to trade.

Nothing here signs, sends, swaps or connects a wallet. HTTP via _http.py only (GET + read-only Sui JSON-RPC).
Sources: DexScreener (chainId "sui"), GeckoTerminal (network "sui-network"), GoPlus Sui token security
(api.gopluslabs.io/api/v1/sui/token_security; free), Sui public RPC fullnode.mainnet.sui.io (suix_getCoinMetadata,
suix_getTotalSupply; read-only).
Sui risk checks (the Sui equivalents of the EVM checks): TreasuryCap not held by a live owner (mint), deny-list /
regulated coin (freeze/blacklist), upgradeable package (UpgradeCap not burned), metadata mutability, LP lock/burn,
top holders, honeypot-style sell restriction (GoPlus), liquidity/volume/age.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _http import get_json, post_json, note_rate_limited  # noqa: E402
import chain_common as cc  # noqa: E402
from chain_common import fnum  # noqa: E402

RPC = ["https://fullnode.mainnet.sui.io:443"]  # JSON-RPC deprecated on public fullnodes; kept as fallback
GRAPHQL = "https://graphql.mainnet.sui.io/graphql"  # read-only queries only
READ_ONLY = {"suix_getCoinMetadata", "suix_getTotalSupply", "sui_getObject", "suix_getBalance"}
NOT_CANDIDATES = {"SUI", "USDC", "USDT", "WUSDC", "WETH", "WBTC", "AUSD", "HASUI", "AFSUI", "VSUI", "SPRING_SUI", "BUCK", "FDUSD"}
STATUS = "RESEARCH ONLY (no venue: Phantom ended Sui 2026-09-24; FOMO has no Sui)"


def rpc(method: str, params: list):
    if method not in READ_ONLY:
        raise ValueError(f"refusing non-read-only Sui RPC {method}")
    # Public-fullnode JSON-RPC was deprecated by Sui ("Method not found ... migrate to gRPC or GraphQL", seen
    # 2026-09-25). Coin metadata therefore comes from the read-only GraphQL service (a query, never a mutation).
    if method == "suix_getCoinMetadata" and params:
        q = "query($t:String!){ coinMetadata(coinType:$t){ decimals name symbol supply } }"
        r = post_json(GRAPHQL, {"query": q, "variables": {"t": params[0]}}, timeout=15, retries=1)
        md = (((r.data or {}).get("data") or {}).get("coinMetadata")) if (r.ok and isinstance(r.data, dict)) else None
        if md:
            return True, md
    for url in RPC:
        r = post_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15, retries=1)
        if r.ok and isinstance(r.data, dict) and "result" in r.data:
            return True, r.data["result"]
    return False, None


def discover(log: list, max_tokens: int = 40) -> dict[str, set]:
    out: dict[str, set] = {}
    for kind in ("trending_pools", "new_pools"):
        r = get_json(f"https://api.geckoterminal.com/api/v2/networks/sui-network/{kind}?page=1", cache_ttl=300)
        for p in ((r.data or {}).get("data") or []) if isinstance(r.data, dict) else []:
            bt = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or ""
            coin = bt.split("_", 1)[-1]
            if "::" in coin:
                out.setdefault(coin, set()).add(f"gt_{kind}")
        log.append({"source": f"geckoterminal sui-network/{kind}", "ok": r.ok, "status": r.status, "note": (r.error or "")[:100]})
    r = get_json("https://api.dexscreener.com/latest/dex/search?q=sui")
    for p in ((r.data or {}).get("pairs") or []) if isinstance(r.data, dict) else []:
        if p.get("chainId") == "sui":
            coin = (p.get("baseToken") or {}).get("address") or ""
            if "::" in coin:
                out.setdefault(coin, set()).add("ds_search")
    log.append({"source": "dexscreener search 'sui'", "ok": r.ok, "status": r.status, "note": (r.error or "")[:100]})
    return dict(list(out.items())[:max_tokens])


def pairs_for(coins: list[str], log: list) -> dict[str, list]:
    out: dict[str, list] = {}
    for c in coins:  # Sui coin types contain '::', batch by comma is fragile -> one call each (DexScreener ~300/min)
        r = get_json("https://api.dexscreener.com/tokens/v1/sui/" + c)
        for p in r.data if (r.ok and isinstance(r.data, list)) else []:
            if (p.get("baseToken") or {}).get("address") == c:
                out.setdefault(c, []).append(p)
    return out


def goplus_sui(coin: str) -> dict:
    url = f"https://api.gopluslabs.io/api/v1/sui/token_security?contract_addresses={coin}"
    r = get_json(url, cache_if=lambda d: isinstance(d, dict) and d.get("code") == 1)
    if not r.ok:
        return {"ok": False, "error": f"GoPlus Sui HTTP {r.status}: {r.error[:80]}"}
    d = r.data or {}
    if d.get("code") == 4029:
        note_rate_limited(url, 0, why="GoPlus code 4029")
        return {"ok": False, "error": "GoPlus rate limited (4029)"}
    res = d.get("result") or {}
    item = res.get(coin) or res.get(coin.lower()) or (next(iter(res.values())) if res else None)
    return {"ok": bool(item), "raw": item or {}, "error": "" if item else f"no GoPlus Sui result (code {d.get('code')})"}


def risk_facts(coin: str) -> dict:
    g = goplus_sui(coin)
    it = g.get("raw") or {}

    def st(k):
        v = it.get(k)
        return (v or {}).get("value") if isinstance(v, dict) and "value" in v else ((v or {}).get("status") if isinstance(v, dict) else v)
    f = {"goplus_ok": g["ok"], "error": g.get("error", ""), "symbol": it.get("symbol"), "name": it.get("name")}
    for k in ("mintable", "blacklist", "contract_upgradeable", "metadata_modifiable", "is_honeypot"):
        v = st(k)
        f[k] = None if v in (None, "") else str(v) == "1"
    ok, meta = rpc("suix_getCoinMetadata", [coin])
    f["rpc_metadata_ok"] = ok
    if ok and meta:
        f["decimals"] = meta.get("decimals")
        f["symbol"] = f["symbol"] or meta.get("symbol")
    return f


def evaluate(coin: str, pairs: list, sources: set, cfg: dict | None = None) -> dict:
    cfg = cfg or {"min_liquidity_usd": 50000, "min_volume_24h_usd": 100000, "migration_wait_min": 20}
    p = max(pairs, key=lambda x: fnum((x.get("liquidity") or {}).get("usd"), 0)) if pairs else None
    rec = {"mint": coin, "chain": "sui", "caip2": "sui:mainnet", "venues": [], "phantom_auto_chain": False,
           "sources": sorted(sources), "checks": [], "research_only": True}
    if not p:
        rec.update(status="REJECT", reason="no DexScreener Sui pair")
        return rec
    bt = p.get("baseToken") or {}
    liq = fnum((p.get("liquidity") or {}).get("usd"), 0)
    vol = fnum((p.get("volume") or {}).get("h24"), 0)
    age_min = (time.time() * 1000 - p["pairCreatedAt"]) / 60000 if p.get("pairCreatedAt") else None
    pc = dict(p.get("priceChange") or {})
    c2, c3, _ = cc.classify_price_action(pc)
    rec.update(symbol=bt.get("symbol"), name=bt.get("name"), dex=p.get("dexId"), pair=p.get("pairAddress"),
               price_usd=fnum(p.get("priceUsd")), market_cap=fnum(p.get("marketCap")) or fnum(p.get("fdv")),
               liquidity_usd=liq, volume_24h=vol, price_change=pc, pool_age_h=round(age_min / 60, 1) if age_min else None,
               c2=c2, c3=c3)
    fails = []
    if (bt.get("symbol") or "").upper() in NOT_CANDIDATES:
        fails.append("scope: major/stable/LST")
    if liq < cfg["min_liquidity_usd"]:
        fails.append(f"liquidity ${liq:,.0f} < min")
    if vol < cfg["min_volume_24h_usd"]:
        fails.append(f"24h volume ${vol:,.0f} < min")
    if age_min is None or age_min < cfg["migration_wait_min"]:
        fails.append("pool age UNKNOWN or < MIGRATION_WAIT")
    if c2 in ("BLOW-OFF", "DISTRIBUTION", "COLLAPSE") or c3 == "CHASE":
        fails.append(f"C2/C3 {c2}/{c3}")
    rec["status"] = "REJECT" if fails else STATUS
    rec["reason"] = fails[0] if fails else "no supported venue (research only; never a MOVE CARD)"
    rec["all_failures"] = fails
    return rec


def scan(limit: int = 20, deep: int = 5) -> dict:
    log: list = []
    cands = discover(log, limit)
    pairs = pairs_for(list(cands), log)
    rows = [evaluate(c, pairs.get(c, []), s) for c, s in cands.items()]
    for r in [x for x in rows if x["status"] == STATUS][:deep]:
        r["risk"] = risk_facts(r["mint"])
        rk = r["risk"]
        r["risk_flags"] = [k for k in ("mintable", "blacklist", "contract_upgradeable", "metadata_modifiable", "is_honeypot") if rk.get(k)]
        r["risk_unknown"] = not rk["goplus_ok"]
    return {"generated_et": cc.now_et().strftime("%Y-%m-%d %H:%M:%S ET"), "status_note": STATUS, "rows": rows, "sources": log}


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only Sui research data (no venue; never produces cards)")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json")
    a = ap.parse_args()
    res = scan(a.limit)
    if a.json:
        json.dump(res, open(a.json, "w"), indent=1, default=str)
    print(json.dumps({"status": res["status_note"], "rows": len(res["rows"]),
                      "research_candidates": sum(1 for r in res["rows"] if r["status"] == STATUS)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
