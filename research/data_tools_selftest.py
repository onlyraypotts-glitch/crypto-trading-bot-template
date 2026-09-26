#!/usr/bin/env python3
"""Run every data tool's --selftest (LIVE, read-only HTTP) and write $BOT_REPORTS_DIR/data-tools-test.md
(plus the stock_data section to stock-data-test.md). Read-only; no keys printed. Network 429s are expected."""
import argparse
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args()
import paths  # noqa: E402
import _http, crypto_data, crypto_news, options_data, scan_candidates, stock_data  # noqa: E401,E402
import free_apis  # noqa: E402
import smart_money  # noqa: E402  (Helius smart-money factor S3; read-only)
import multichain_selftest  # noqa: E402  (EVM/Sui modules, social S4, scoring aggregation, card rendering)


class _Multichain:
    """Adapter: multichain_selftest rows -> this file's row format. Offline checks always; the live part only when
    GROKBOT_MULTICHAIN_LIVE=1 (it hits DexScreener/GeckoTerminal/GoPlus, which rate-limit shared IPs)."""
    @staticmethod
    def selftest():
        multichain_selftest.ROWS.clear()
        multichain_selftest.offline()
        if os.environ.get("GROKBOT_MULTICHAIN_LIVE") == "1":
            multichain_selftest.live()
        return [{"tool": r["module"], "check": r["check"], "ok": r["result"] != "FAIL", "status": r["result"], "ms": 0,
                 "detail": r["detail"]} for r in multichain_selftest.ROWS]

ET = ZoneInfo("America/New_York")
start = datetime.now(ET)
sections = []
all_rows = []
for name, mod in (("_http.py (shared limiter + cache, offline)", _http), ("stock_data.py", stock_data),
                  ("crypto_data.py", crypto_data), ("options_data.py", options_data), ("crypto_news.py", crypto_news),
                  ("scan_candidates.py", scan_candidates), ("smart_money.py (Helius S3, read-only)", smart_money),
                  ("multichain (evm_data, evm_smart_money, sui_data, social_data, scoring, cards)", _Multichain)):
    t = time.perf_counter()
    rows = mod.selftest()
    all_rows += rows
    sections.append((name, rows, int(time.perf_counter() - t)))
# free_apis.py has a GET-only smoke suite plus the maintained read-only
# Helius getSlot wrapper. Keep it last and sequential with the other tools.
ft = time.perf_counter()
free_rows = []
for r in free_apis.run_tests():
    free_rows.append({"tool": "free_apis", "check": r.operation, "ok": r.ok,
                      "status": r.status if r.status is not None else "-", "ms": r.latency_ms,
                      "detail": (r.sample or r.error)[:220]})
h = free_apis.helius_rpc("getSlot")
helius_row = {"tool": "free_apis", "check": "helius_rpc getSlot (key from env; never printed)",
              "ok": h["ok"], "status": h["status"], "ms": h["latency_ms"], "detail": str({"slot": h["result"]})}
free_rows.append(helius_row)
sections.append(("free_apis.py", free_rows, int(time.perf_counter() - ft)))
all_rows.extend(free_rows)
end = datetime.now(ET)

md = [f"# Data tools live test ({start:%Y-%m-%d})", "",
      f"Run: {start:%Y-%m-%d %H:%M:%S} → {end:%H:%M:%S} ET. Read-only: HTTP GETs plus read-only "
      "JSON-RPC `get*` POSTs. No sign-ins, no swaps, no orders, no transactions. The Helius key was loaded from the "
      "HELIUS_API_KEY environment variable and never printed.", "",
      f"Totals: {sum(r['ok'] for r in all_rows)}/{len(all_rows)} checks passed.", ""]
for name, rows, secs in sections:
    md += [f"## {name} ({sum(r['ok'] for r in rows)}/{len(rows)} passed, {secs}s)", "", crypto_data.rows_to_md(rows), ""]
md += ["## Notes", "",
       "- Sequential run (one tool at a time). All tools share ONE polite limiter and file cache (_http.py): per-host spacing "
       "across processes, a shared cooldown of max(Retry-After, 60 s) escalating 2/5/15 min after a 429/1015/GoPlus 4029, and "
       "fail-fast during a cooldown. A FAIL caused by a cooldown means the provider throttled this IP, not a code error.",
       "- crypto_news: future-dated items are clamped to fetch time, flagged future_dated and down-weighted x0.5; feeds whose "
       "newest item is older than 30 days are marked stale and down-weighted x0.25.",
       "- CoinGecko and GeckoTerminal share CoinGecko's per-IP limit; 429s are expected under load (Retry-After is honored, "
       "capped at 30 s). Treat them as secondary; DexScreener, RugCheck, Jupiter and DefiLlama are primary.",
       "- GDELT allows about one request per 5 s; the tool spaces calls ≥ 6 s and backs off 10/20 s on 429.",
       "- CBOE and Nasdaq JSON endpoints are UNDOCUMENTED (they back public web pages) and may change; cross-check only. "
       "The Robinhood connector is the primary live source for quotes and chains.",
       "- Senate eFD requires a browser terms acceptance; it is not automated (manual cross-check only). House PTR details "
       "live in PDFs; the tool returns the machine-readable filing index.",
       f"- Limiter cooldowns/strikes at end of run: { {k: v for k, v in _http.limiter_status().items() if v['strikes'] or v['cooldown_s']} or 'none'}",
       "- Jupiter /swap/v1/quote is documented as deprecated; crypto_data falls back to /swap/v2/order (no taker) if v1 fails.",
       "- round_trip_cost assumes 0.0005 SOL priority fee per leg plus 5,000 lamports per signature (prompt §2B); "
       "slippage beyond the quote and refundable rent are excluded.",
       "- CryptoPanic is an optional keyed source (free tier needs a key); not used.",
       "- smart_money: offline parser/judge tests, key-not-printed check, fake-transport 429/timeout checks (no network), "
       "unknown-when-missing checks, and two small live Helius calls (BONK deployer lookup; one tracked wallet's 24 h swaps). "
       "Estimated Helius credits are logged to data/helius_usage.json.",
       "- multichain: offline checks for EVM gates, venue costs, the H8(c) EVM crash regime, EVM smart money, Sui research-only, "
       "social S4 parsing/grading (Reddit RSS, Telegram previews, news velocity, paid-boost and bot negatives), the scoring "
       "aggregation (prompt rule vs shadow renormalized+haircut), GeckoTerminal fallback pairs and card rendering. SOFT = a live "
       "source was rate limited (counted as not failed).", ""]
os.makedirs(paths.REPORTS_DIR, exist_ok=True)
open(os.path.join(paths.REPORTS_DIR, "data-tools-test.md"), "w").write("\n".join(md))
srows = sections[1][1]
open(os.path.join(paths.REPORTS_DIR, "stock-data-test.md"), "w").write(
    f"# stock_data.py live test ({start:%Y-%m-%d})\n\nRun: {start:%Y-%m-%d %H:%M:%S} ET. Read-only GETs; no keys. "
    f"SEC User-Agent from SEC_USER_AGENT (placeholder default).\n\n{sum(r['ok'] for r in srows)}/{len(srows)} passed.\n\n"
    + crypto_data.rows_to_md(srows) + "\n\nFull results for all tools: data-tools-test.md\n")
print("\n".join(md))
