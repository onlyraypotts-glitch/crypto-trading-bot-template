#!/usr/bin/env python3
"""regime_snapshot.py: build one market_regime snapshot from FREE, keyless, read-only endpoints and append it to desk.db.

Sources (all verified live 2026-09-26, see DATA_FEEDS.md): Coinbase spot (BTC/ETH/SOL), CoinPaprika global + BTC ticker
(total mcap, BTC dominance, BTC 24h %), DefiLlama stablecoins (total supply, 7d change), DefiLlama DEX overview (24h DEX
volume), OKX public funding + open interest (BTC-USDT-SWAP; market data only), alternative.me Fear & Greed, FRED CSV DGS10.
HTTP goes through $DESK_TOOLS_DIR/_http.py (default <repo>/research; shared rate limiter + cache). Missing data stays NULL (UNKNOWN).

Classification = heuristic v1 (uncalibrated; Quant Lab should test it). First match wins:
  CRASH     BTC 24h <= -8%  or  Fear&Greed <= 10
  RISK_OFF  BTC 24h <= -3%  or  Fear&Greed < 25  or  stablecoin supply 7d < -0.5%
  EUPHORIA  Fear&Greed >= 85 and BTC funding >= 0.05% per 8h
  RISK_ON   BTC 24h >= +2% and Fear&Greed >= 55 and stablecoin 7d >= 0
  NEUTRAL   otherwise;  UNKNOWN if BTC 24h and Fear&Greed are both missing
Usage: python3 $DESK_DIR/tools/regime_snapshot.py [--dry-run]
"""
import csv, io, json, os, sys
_DESK_CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(os.path.expanduser(os.environ.get("DESK_TOOLS_DIR") or os.path.join(os.path.dirname(_DESK_CODE), "research"))))
sys.path.insert(0, _DESK_CODE)
try:
    import _http  # noqa: E402  (from $DESK_TOOLS_DIR; only needed for live fetches)
except ImportError:  # classify() stays importable/testable without the research tools
    _http = None
import desk  # noqa: E402


def j(url, ttl=120):
    if _http is None:
        raise SystemExit("regime_snapshot: _http.py not found. Set DESK_TOOLS_DIR to the repo's research/ folder.")
    r = _http.request(url, parse="json", cache_ttl=ttl, timeout=20, retries=1)
    return r.data if r.ok else None, url


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def classify(btc24, fg, stab7, funding):
    if btc24 is None and fg is None:
        return "UNKNOWN"
    if (btc24 is not None and btc24 <= -8) or (fg is not None and fg <= 10):
        return "CRASH"
    if (btc24 is not None and btc24 <= -3) or (fg is not None and fg < 25) or (stab7 is not None and stab7 < -0.5):
        return "RISK_OFF"
    if fg is not None and fg >= 85 and funding is not None and funding >= 0.0005:
        return "EUPHORIA"
    if btc24 is not None and btc24 >= 2 and fg is not None and fg >= 55 and (stab7 is None or stab7 >= 0):
        return "RISK_ON"
    return "NEUTRAL"


def snapshot():
    links, row, missing = [], {}, []
    for sym, key in (("BTC", "btc_price"), ("ETH", "eth_price"), ("SOL", "sol_price")):
        d, u = j(f"https://api.coinbase.com/v2/prices/{sym}-USD/spot", 60)
        row[key] = f(d and d.get("data", {}).get("amount")); links.append(u)
    d, u = j("https://api.coinpaprika.com/v1/global", 300); links.append(u)
    if d:
        row["total_mcap"] = f(d.get("market_cap_usd")); row["btc_dominance"] = f(d.get("bitcoin_dominance_percentage"))
    d, u = j("https://api.coinpaprika.com/v1/tickers/btc-bitcoin", 120); links.append(u)
    row["btc_24h_pct"] = f(d and d.get("quotes", {}).get("USD", {}).get("percent_change_24h"))
    d, u = j("https://stablecoins.llama.fi/stablecoins?includePrices=false", 1800); links.append(u)
    if d:
        now = sum(f((a.get("circulating") or {}).get("peggedUSD")) or 0 for a in d.get("peggedAssets", []))
        wk = sum(f((a.get("circulatingPrevWeek") or {}).get("peggedUSD")) or 0 for a in d.get("peggedAssets", []))
        row["stablecoin_mcap"] = round(now, 0) if now else None
        row["stablecoin_7d_pct"] = round((now / wk - 1) * 100, 3) if now and wk else None
    d, u = j("https://api.llama.fi/overview/dexs?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true", 1800); links.append(u)
    row["dex_volume_24h"] = f(d and d.get("total24h"))
    d, u = j("https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP", 300); links.append(u)
    row["btc_funding"] = f(d and d.get("data") and d["data"][0].get("fundingRate"))
    d, u = j("https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP", 300); links.append(u)
    row["btc_oi_usd"] = f(d and d.get("data") and d["data"][0].get("oiUsd"))
    d, u = j("https://api.alternative.me/fng/?limit=1", 1800); links.append(u)
    row["fear_greed"] = int(d["data"][0]["value"]) if d and d.get("data") else None
    r = _http.request("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10", parse="text", cache_ttl=21600, timeout=20, retries=1)
    links.append("https://fred.stlouisfed.org/series/DGS10")
    if r.ok:
        vals = [x for x in csv.reader(io.StringIO(r.text)) if len(x) == 2 and x[1] not in ("", ".", "DGS10")]
        row["us10y"] = f(vals[-1][1]) if vals else None
    missing = [k for k, v in row.items() if v is None]
    row["regime"] = classify(row.get("btc_24h_pct"), row.get("fear_greed"), row.get("stablecoin_7d_pct"), row.get("btc_funding"))
    row["confidence"] = 0.5 if not missing else 0.35
    row["notes"] = "heuristic v1 (uncalibrated). OKX funding/OI = public market data for context only (no derivatives trading)." + \
                   (f" UNKNOWN fields: {missing}" if missing else "")
    row["source_links"] = links
    row["source_bot"] = "crypto-radar"
    return row


if __name__ == "__main__":
    row = snapshot()
    if "--dry-run" in sys.argv:
        print(json.dumps(row, indent=1)); sys.exit(0)
    con = desk.connect()
    rid = desk.insert(con, "market_regime", row)
    print(json.dumps({"id": rid, **{k: row[k] for k in row if k != "source_links"}}, indent=1))
