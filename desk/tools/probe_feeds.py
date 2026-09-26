#!/usr/bin/env python3
"""probe_feeds.py: one polite, READ-ONLY GET (or read-only POST for market-data/RPC) per endpoint, to verify which
free data feeds work from this machine right now. Writes $DESK_DIR/data/feed_probe.json (+ prints a table).
No keys are printed. Helius is tested through $DESK_TOOLS_DIR/crypto_data.helius_rpc (loads its key at runtime).
Usage: python3 $DESK_DIR/tools/probe_feeds.py [--only substring]
"""
import json, os, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
_DESK_CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESK_DIR = os.path.abspath(os.path.expanduser(os.environ.get("DESK_DIR") or _DESK_CODE))
DESK_TOOLS_DIR = os.path.abspath(os.path.expanduser(os.environ.get("DESK_TOOLS_DIR") or os.path.join(os.path.dirname(_DESK_CODE), "research")))
OUT = os.path.join(DESK_DIR, "data", "feed_probe.json")
UA = "Mozilla/5.0 (research desk feed probe; read-only)"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"; WSOL = "So11111111111111111111111111111111111111112"
# (name, category, url, post_body_or_None, expect_key_required)
E = [
 ("DexScreener search", "dex", "https://api.dexscreener.com/latest/dex/search?q=BONK", None, False),
 ("DexScreener token pairs", "dex", f"https://api.dexscreener.com/tokens/v1/solana/{BONK}", None, False),
 ("DexScreener boosts (paid promo)", "dex", "https://api.dexscreener.com/token-boosts/latest/v1", None, False),
 ("GeckoTerminal trending pools", "dex", "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools", None, False),
 ("GeckoTerminal new pools", "dex", "https://api.geckoterminal.com/api/v2/networks/base/new_pools", None, False),
 ("RugCheck summary", "security", f"https://api.rugcheck.xyz/v1/tokens/{BONK}/report/summary", None, False),
 ("GoPlus Solana security", "security", f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={BONK}", None, False),
 ("GoPlus EVM security (Base)", "security", "https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses=0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", None, False),
 ("honeypot.is (Base)", "security", "https://api.honeypot.is/v2/IsHoneypot?address=0x4ed4e862860bed51a9570b96d89af5e1b0efefed&chainID=8453", None, False),
 ("Jupiter price v3 (lite, keyless)", "dex", f"https://lite-api.jup.ag/price/v3?ids={WSOL},{BONK}", None, False),
 ("Jupiter tokens v2 toptrending (lite)", "dex", "https://lite-api.jup.ag/tokens/v2/toptrending/1h?limit=5", None, False),
 ("Jupiter quote (lite, keyless)", "dex", f"https://lite-api.jup.ag/swap/v1/quote?inputMint={WSOL}&outputMint={BONK}&amount=100000000&slippageBps=100", None, False),
 ("CoinGecko simple price", "market", "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_24hr_change=true", None, False),
 ("CoinGecko global (dominance)", "market", "https://api.coingecko.com/api/v3/global", None, False),
 ("CoinGecko trending searches", "social", "https://api.coingecko.com/api/v3/search/trending", None, False),
 ("CoinPaprika global", "market", "https://api.coinpaprika.com/v1/global", None, False),
 ("DefiLlama chains TVL", "flows", "https://api.llama.fi/v2/chains", None, False),
 ("DefiLlama stablecoins", "flows", "https://stablecoins.llama.fi/stablecoins?includePrices=false", None, False),
 ("DefiLlama stablecoin chains", "flows", "https://stablecoins.llama.fi/stablecoinchains", None, False),
 ("DefiLlama DEX volume overview", "flows", "https://api.llama.fi/overview/dexs?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true", None, False),
 ("DefiLlama fees overview", "flows", "https://api.llama.fi/overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true", None, False),
 ("DefiLlama bridges", "flows", "https://bridges.llama.fi/bridges", None, False),
 ("DefiLlama emissions/unlocks", "events", "https://api.llama.fi/emissions", None, False),
 ("DefiLlama coins price", "market", "https://coins.llama.fi/prices/current/coingecko:bitcoin", None, False),
 ("DefiLlama yields", "flows", "https://yields.llama.fi/pools", None, False),
 ("Binance futures funding (premiumIndex)", "derivs", "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", None, False),
 ("Binance futures open interest", "derivs", "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT", None, False),
 ("Binance.US spot klines", "market", "https://api.binance.us/api/v3/klines?symbol=SOLUSDT&interval=1h&limit=2", None, False),
 ("Bybit linear tickers (funding+OI)", "derivs", "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT", None, False),
 ("OKX funding rate", "derivs", "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP", None, False),
 ("OKX open interest", "derivs", "https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP", None, False),
 ("Deribit futures summary (funding/OI)", "derivs", "https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=future", None, False),
 ("Hyperliquid info (funding/OI, market data)", "derivs", "https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"}, False),
 ("Kraken futures tickers (funding/OI)", "derivs", "https://futures.kraken.com/derivatives/api/v3/tickers", None, False),
 ("Coinbase spot price", "market", "https://api.coinbase.com/v2/prices/BTC-USD/spot", None, False),
 ("Kraken spot ticker", "market", "https://api.kraken.com/0/public/Ticker?pair=XBTUSD", None, False),
 ("mempool.space fees", "onchain", "https://mempool.space/api/v1/fees/recommended", None, False),
 ("alternative.me Fear & Greed", "sentiment", "https://api.alternative.me/fng/?limit=1", None, False),
 ("FRED CSV 10y yield (no key)", "macro", "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10", None, False),
 ("FRED API (needs key)", "macro", "https://api.stlouisfed.org/fred/series/observations?series_id=DGS10&file_type=json", None, True),
 ("US Treasury FiscalData", "macro", "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/accounting/od/avg_interest_rates?page%5Bsize%5D=1&sort=-record_date", None, False),
 ("GitHub API repo commits", "dev", "https://api.github.com/repos/anza-xyz/agave/commits?per_page=1", None, False),
 ("Solana public RPC getSlot", "onchain", "https://api.mainnet-beta.solana.com", {"jsonrpc": "2.0", "id": 1, "method": "getSlot"}, False),
 ("Base public RPC blockNumber", "onchain", "https://mainnet.base.org", {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}, False),
 ("Blockscout Base API (keyless)", "onchain", "https://base.blockscout.com/api/v2/stats", None, False),
 ("Etherscan V2 (no key)", "onchain", "https://api.etherscan.io/v2/api?chainid=1&module=stats&action=ethprice", None, True),
 ("Reddit search RSS", "social", "https://www.reddit.com/search.rss?q=solana&sort=new", None, False),
 ("GDELT DOC API", "news", "https://api.gdeltproject.org/api/v2/doc/doc?query=bitcoin&mode=artlist&maxrecords=1&format=json", None, False),
 ("CoinDesk RSS", "news", "https://www.coindesk.com/arc/outboundfeeds/rss/", None, False),
 ("SEC press releases RSS", "regulators", "https://www.sec.gov/news/pressreleases.rss", None, False),
 ("CFTC press releases RSS", "regulators", "https://www.cftc.gov/RSS/RSSGP/rssgp.xml", None, False),
 ("Telegram public preview", "social", "https://t.me/s/solana", None, False),
 ("Polymarket gamma markets (read-only)", "events", "https://gamma-api.polymarket.com/markets?limit=1&active=true", None, False),
 ("Farside BTC ETF flows page", "flows", "https://farside.co.uk/btc/", None, False),
 ("CryptoPanic (needs key)", "news", "https://cryptopanic.com/api/developer/v2/posts/?public=true", None, True),
 ("Birdeye (needs key)", "wallets", f"https://public-api.birdeye.so/defi/price?address={BONK}", None, True),
 ("Coinglass open API (needs key)", "derivs", "https://open-api-v4.coinglass.com/api/futures/supported-coins", None, True),
 ("Arkham API (needs key)", "wallets", "https://api.arkm.com/intelligence/address/0x0000000000000000000000000000000000000000", None, True),
 ("Santiment GraphQL (key for most)", "social", "https://api.santiment.net/graphql", {"query": "{ getMetric(metric: \"social_volume_total\") { timeseriesData(slug: \"bitcoin\" from: \"utc_now-2d\" to: \"utc_now\" interval: \"1d\") { datetime value } } }"}, True),
 ("LunarCrush v4 (needs key)", "social", "https://lunarcrush.com/api4/public/coins/list/v1", None, True),
 ("X API recent search (needs key)", "social", "https://api.x.com/2/tweets/search/recent?query=solana", None, True),
]

def probe(e):
    name, cat, url, body, keyreq = e
    t0 = time.time()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "application/json, text/xml, */*",
                                                        **({"Content-Type": "application/json"} if data else {})})
    status, err, n, snippet = None, None, 0, ""
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            status = r.status; raw = r.read(400000); n = len(raw)
            snippet = raw[:160].decode("utf-8", "replace")
            if "farside" in url:  # page is HTML; flows table present?
                snippet = ("flows table found " if b"Total" in raw and b"IBIT" in raw else "no flows table ") + snippet
    except urllib.error.HTTPError as ex:
        status = ex.code; err = f"HTTP {ex.code}"
        try: snippet = ex.read(160).decode("utf-8", "replace")
        except Exception: pass
    except Exception as ex:
        err = type(ex).__name__ + ": " + str(ex)[:120]
    low = snippet.lower()
    ok = status == 200 and n > 20 and not any(m in low for m in ("error code: 1015", "cloudflare", "notok", "api key missing",
                                                                   "\"errors\"", "unauthorized", "invalid api key"))
    if ok and cat == "derivs" and ("restricted location" in snippet.lower() or "\"code\":0" in snippet and "error" in snippet.lower()):
        ok = False
    return {"name": name, "category": cat, "url": url.split("?api-key")[0], "method": "POST" if body is not None else "GET",
            "key_required_expected": keyreq, "status": status, "ok": ok, "bytes": n, "ms": int((time.time() - t0) * 1000),
            "error": err, "snippet": snippet.replace("\n", " ")[:120]}

def helius():
    t0 = time.time()
    try:
        sys.path.insert(0, DESK_TOOLS_DIR); import crypto_data as cd
        if not cd.helius_key_present():
            return {"name": "Helius RPC getSlot", "category": "onchain", "ok": False, "status": None, "error": "no key found", "ms": 0}
        r = cd.helius_rpc("getSlot")
        return {"name": "Helius RPC getSlot", "category": "onchain", "url": "https://mainnet.helius-rpc.com/?api-key=***",
                "method": "POST", "key_required_expected": True, "status": r.status, "ok": bool(r.ok), "bytes": 0,
                "ms": int((time.time() - t0) * 1000), "error": (r.error or None) and str(r.error)[:120], "snippet": "slot ok" if r.ok else ""}
    except Exception as ex:
        return {"name": "Helius RPC getSlot", "category": "onchain", "ok": False, "error": type(ex).__name__, "ms": 0}

if __name__ == "__main__":
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    es = [e for e in E if not only or only.lower() in e[0].lower()]
    with ThreadPoolExecutor(6) as ex:
        rows = list(ex.map(probe, es))
    rows.append(helius())
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    prev = {}
    if os.path.exists(OUT):  # merge: keep every endpoint's latest result and when it last worked
        try:
            prev = {r["name"]: r for r in json.load(open(OUT)).get("rows", [])}
        except Exception:
            prev = {}
    for r in rows:
        r["probed_at_utc"] = ts
        old = prev.get(r["name"], {})
        r["last_ok_utc"] = ts if r["ok"] else old.get("last_ok_utc")
        prev[r["name"]] = r
    out = {"last_run_utc": ts, "rows": list(prev.values())}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w"), indent=1)
    for r in rows:
        print(f"{'OK ' if r['ok'] else 'NO '} {str(r.get('status')):>4} {r.get('ms',0):>6}ms  {r['name']}  {r.get('error') or ''}  | {r.get('snippet','')[:70]}")
