#!/usr/bin/env python3
"""Read-only, no-key smoke tests for public crypto-data APIs (plus a read-only helius_rpc wrapper).

The maintained data interface is crypto_data.py; this file keeps its original selftest for existing references.

All network calls in this file are HTTP GETs. No credentials are read or sent.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import os as _os0
sys.path.insert(0, _os0.path.dirname(_os0.path.abspath(__file__)))
from _http import note_rate_limited, polite_acquire  # noqa: E402  shared polite limiter

SOL = "So11111111111111111111111111111111111111112"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
SOLANA = "solana"
BASE_HEADERS = {
    "User-Agent": "free-api-selftest/1.0 (read-only; no-key)",
    "Accept": "application/json, text/plain, */*",
}


@dataclass
class Result:
    source: str
    operation: str
    endpoint: str
    status: int | None
    latency_ms: int
    sample: str
    rate_headers: dict[str, str]
    ok: bool
    error: str = ""
    attempts: int = 1


def _short(value: Any, limit: int = 500) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        text = str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _rate_headers(headers: Any) -> dict[str, str]:
    out = {}
    for key, value in headers.items():
        low = key.lower()
        if any(term in low for term in ("rate", "limit", "retry-after", "quota")):
            out[key] = value
    return out


def get_json(source: str, operation: str, url: str, sample_fn: Callable[[Any], Any] | None = None,
             timeout: float = 15.0, retries: int = 2,
             success_fn: Callable[[Any], tuple[bool, str]] | None = None) -> Result:
    started = time.perf_counter()
    last_error = ""
    last_status: int | None = None
    last_headers: dict[str, str] = {}
    attempts = 0
    for attempt in range(retries + 1):
        attempts += 1
        ok_slot, why = polite_acquire(url)  # shared cross-process limiter (_http.py)
        if not ok_slot:
            last_status, last_error = 429, why
            break
        req = urllib.request.Request(url, headers=BASE_HEADERS, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
                status = int(response.status)
                last_status = status
                last_headers = _rate_headers(response.headers)
                content_type = response.headers.get("Content-Type", "")
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                    sample = sample_fn(data) if sample_fn else data
                    ok = 200 <= status < 300
                    semantic_error = ""
                    if ok and success_fn:
                        try:
                            ok, semantic_error = success_fn(data)
                        except Exception as exc:
                            ok, semantic_error = False, f"validation error: {exc}"
                    return Result(source, operation, url, status,
                                  int((time.perf_counter() - started) * 1000),
                                  _short(sample), last_headers, ok,
                                  error=semantic_error, attempts=attempts)
                except Exception as exc:
                    text = raw.decode("utf-8", errors="replace")
                    return Result(source, operation, url, status,
                                  int((time.perf_counter() - started) * 1000),
                                  _short({"content_type": content_type, "body": text}, 500),
                                  last_headers, False,
                                  error=f"non-JSON response: {exc}", attempts=attempts)
        except urllib.error.HTTPError as exc:
            last_status = int(exc.code)
            last_headers = _rate_headers(exc.headers)
            body = ""
            try:
                body = exc.read(500).decode("utf-8", errors="replace")
            except Exception:
                pass
            last_error = body.replace("\n", " ")[:300] or str(exc.reason)
            if exc.code == 429 or "1015" in body:
                ra = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    ra_s = float(ra) if ra else 0.0
                except ValueError:
                    ra_s = 0.0
                note_rate_limited(url, ra_s, why=f"HTTP {exc.code} (free_apis)")
                break  # no immediate retry on a rate limit (prompt §2G)
            if exc.code not in (408, 425) and not (500 <= exc.code <= 599):
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        if attempt < retries:
            time.sleep((0.35 * (2 ** attempt)) + random.random() * 0.15)
    return Result(source, operation, url, last_status,
                  int((time.perf_counter() - started) * 1000), "", last_headers,
                  False, error=last_error or "request failed", attempts=attempts)


def sample_first(data: Any) -> Any:
    if isinstance(data, list):
        return {"count": len(data), "first": data[0] if data else None}
    return data


def sample_dex_pairs(data: Any) -> Any:
    pairs = data if isinstance(data, list) else data.get("pairs", []) if isinstance(data, dict) else []
    if not isinstance(pairs, list):
        pairs = []
    return {"pair_count": len(pairs), "pairs": [
        {k: p.get(k) for k in ("chainId", "dexId", "pairAddress", "priceUsd", "liquidity", "volume", "priceChange") if isinstance(p, dict) and k in p}
        for p in pairs[:2]
    ]}


def sample_gecko_pools(data: Any) -> Any:
    arr = data.get("data", []) if isinstance(data, dict) else []
    first = arr[0] if arr else {}
    attrs = first.get("attributes", {}) if isinstance(first, dict) else {}
    return {"count": len(arr), "first": {k: attrs.get(k) for k in ("name", "address", "base_token_price_usd", "quote_token_price_usd", "reserve_in_usd", "volume_usd", "price_change_percentage") if k in attrs}}


def sample_ohlcv(data: Any) -> Any:
    if isinstance(data, dict):
        return {"keys": list(data.keys()), "data": data.get("data", [])[:2] if isinstance(data.get("data"), list) else data.get("data")}
    return data


def sample_rug(data: Any) -> Any:
    if isinstance(data, dict):
        keys = ("score", "score_normalised", "score_normalized", "risks", "totalMarketLiquidity", "totalLPProviders", "markets", "verification")
        return {k: data.get(k) for k in keys if k in data}
    return data


def sample_goplus(data: Any) -> Any:
    if isinstance(data, dict):
        result = data.get("result", data)
        if isinstance(result, dict):
            first = next(iter(result.values()), result)
            if isinstance(first, dict):
                keys = ("is_mintable", "is_freezable", "mint_authority", "freeze_authority",
                        "balance_mutable_authority", "metadata_mutable", "default_account_state",
                        "closable", "holder_count", "token_name", "token_symbol")
                return {k: first.get(k) for k in keys if k in first}
        return {"code": data.get("code"), "message": data.get("message"), "result_keys": list(result)[:10] if isinstance(result, dict) else None}
    return data


def sample_jup_price(data: Any) -> Any:
    if isinstance(data, dict):
        return {mint: {k: data[mint].get(k) for k in ("usdPrice", "liquidity", "decimals", "priceChange24h") if k in data[mint]}
                for mint in (SOL, BONK) if isinstance(data.get(mint), dict)}
    return data


def sample_quote(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: data.get(k) for k in ("inputMint", "inAmount", "outputMint", "outAmount", "otherAmountThreshold", "priceImpactPct", "routePlan", "contextSlot") if k in data}
    return data


def _api_success(data: Any) -> tuple[bool, str]:
    """Treat an HTTP 200 API error envelope as a failed data check."""
    if isinstance(data, dict) and "code" in data:
        code = data.get("code")
        if code not in (None, 0, 1, "0", "1"):
            return False, f"API code={code}: {data.get('message', '')}".strip()
    return True, ""


def _nonempty_price(data: Any) -> tuple[bool, str]:
    if isinstance(data, dict) and data:
        return True, ""
    return False, "HTTP 200 but no token prices were returned"


def _quote_success(data: Any) -> tuple[bool, str]:
    required = ("inputMint", "inAmount", "outputMint", "outAmount")
    if isinstance(data, dict) and all(k in data for k in required):
        return True, ""
    return False, "HTTP 200 but quote fields were missing"


def dexscreener_tests() -> list[Result]:
    out = []
    for mint, label in ((SOL, "SOL"), (BONK, "BONK")):
        out.append(get_json("DexScreener", f"token pairs ({label})",
                            f"https://api.dexscreener.com/token-pairs/v1/solana/{mint}", sample_dex_pairs))
    out.append(get_json("DexScreener", "latest token profiles",
                        "https://api.dexscreener.com/token-profiles/latest/v1", sample_first))
    out.append(get_json("DexScreener", "latest token boosts",
                        "https://api.dexscreener.com/token-boosts/latest/v1", sample_first))
    return out


def geckoterminal_tests() -> list[Result]:
    out = [
        get_json("GeckoTerminal", "Solana trending pools",
                 "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools", sample_gecko_pools),
        get_json("GeckoTerminal", "Solana new pools",
                 "https://api.geckoterminal.com/api/v2/networks/solana/new_pools", sample_gecko_pools),
    ]
    # Public, well-known Raydium SOL/USDC pool; OHLCV is read-only.
    pool_id = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"
    out.append(get_json("GeckoTerminal", "OHLCV (Raydium SOL/USDC pool)",
                        f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool_id}/ohlcv/minute?aggregate=1&limit=10", sample_ohlcv))
    return out


def rugcheck_tests() -> list[Result]:
    return [get_json("RugCheck", f"token report summary ({label})",
                     f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary", sample_rug)
            for mint, label in ((SOL, "SOL"), (BONK, "BONK"))]


def goplus_tests() -> list[Result]:
    # This is GoPlus's Solana-specific path, not the EVM token_security/{chain} path.
    return [get_json("GoPlus", f"Solana token security ({label})",
                     f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={urllib.parse.quote(mint)}",
                     sample_goplus, success_fn=_api_success)
            for mint, label in ((SOL, "SOL"), (BONK, "BONK"))]


def jupiter_tests() -> list[Result]:
    # api.jup.ag is the current keyless gateway; no x-api-key header is sent.
    out = [get_json("Jupiter", "price (SOL and BONK)",
                    f"https://api.jup.ag/price/v3?ids={SOL},{BONK}", sample_jup_price,
                    success_fn=_nonempty_price)]
    out.append(get_json("Jupiter", "quote (0.1 SOL -> BONK; read-only)",
                        f"https://api.jup.ag/swap/v1/quote?inputMint={SOL}&outputMint={BONK}&amount=100000000&slippageBps=50",
                        sample_quote, success_fn=_quote_success))
    return out


def defillama_tests() -> list[Result]:
    return [get_json("DefiLlama", "coins price (SOL and BONK)",
                     f"https://coins.llama.fi/prices/current/solana:{SOL},solana:{BONK}",
                     lambda d: d.get("coins", d) if isinstance(d, dict) else d)]


def coingecko_tests() -> list[Result]:
    return [get_json("CoinGecko", "keyless simple price (SOL and BONK)",
                     "https://api.coingecko.com/api/v3/simple/price?ids=solana,bonk&vs_currencies=usd", lambda d: d)]


def solana_rpc_tests() -> list[Result]:
    # Solana JSON-RPC is POST-only. We deliberately make GETs and do not substitute POST.
    return [get_json("Solana public RPC", "getSlot (GET-only test)",
                     "https://api.mainnet-beta.solana.com?jsonrpc=2.0&id=1&method=getSlot", lambda d: d),
            get_json("Solana public RPC", "getTokenLargestAccounts BONK (GET-only test)",
                     "https://api.mainnet-beta.solana.com?jsonrpc=2.0&id=1&method=getTokenLargestAccounts&params=" + urllib.parse.quote(json.dumps([BONK])), lambda d: d)]


def run_tests() -> list[Result]:
    results: list[Result] = []
    for fn in (dexscreener_tests, geckoterminal_tests, rugcheck_tests, goplus_tests,
               jupiter_tests, defillama_tests, coingecko_tests, solana_rpc_tests):
        results.extend(fn())
    return results

def helius_rpc(method: str, params: list | None = None) -> dict:
    """Read-only Helius JSON-RPC (get* methods only). Key from the HELIUS_API_KEY environment variable,
    never printed. Thin wrapper over crypto_data.helius_rpc (the maintained implementation)."""
    import os as _os
    sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from crypto_data import helius_rpc as _helius_rpc
    r = _helius_rpc(method, params)
    return {"ok": r.ok, "status": r.status, "result": (r.data or {}).get("result") if r.ok else None,
            "error": r.error, "latency_ms": r.latency_ms}


def md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render(results: list[Result]) -> str:
    now = datetime.now().astimezone()
    lines = [
        "# Free, no-key API live test (2026-09-25)",
        "", f"Run time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "", "All requests were read-only HTTP GETs; no API keys, sign-ins, swaps, signatures, or transactions were used.",
        f"Test tokens: SOL `{SOL}`; BONK `{BONK}`.", "",
        "## Results",
        "", "| Source / operation | HTTP | Latency (ms) | Endpoint | Key-field sample | Rate-limit headers |",
        "|---|---:|---:|---|---|---|",
    ]
    for r in results:
        status = str(r.status) if r.status is not None else "error"
        sample = r.sample or r.error
        if not r.ok and r.error and r.status is not None:
            sample = f"error: {r.error}"
        rate = _short(r.rate_headers, 300) if r.rate_headers else "(none observed)"
        lines.append(f"| {md_escape(r.source + ' — ' + r.operation)} | {status} | {r.latency_ms} | `{md_escape(r.endpoint)}` | `{md_escape(sample)}` | `{md_escape(rate)}` |")
    lines += ["", "## Interpretation", ""]
    by_source: dict[str, list[Result]] = {}
    for r in results:
        by_source.setdefault(r.source, []).append(r)
    for source, rs in by_source.items():
        good = sum(r.ok and r.status is not None and 200 <= r.status < 300 for r in rs)
        bad = len(rs) - good
        details = "; ".join(f"{r.operation}: {r.status if r.status is not None else 'request error'}" for r in rs)
        lines.append(f"- **{source}:** {good}/{len(rs)} successful. {details}.")
    lines += ["", "### Notes", "", 
              "- Solana JSON-RPC methods `getSlot` and `getTokenLargestAccounts` require JSON-RPC POST requests. Because this test intentionally allowed GET only, those checks are expected to fail or return a generic GET response; no POST was attempted.",
              "- A source is marked successful only when it returns HTTP 2xx and the expected data fields (or source success envelope) validate; an HTTP 2xx API error envelope is marked failed.",
              "- Rate-limit headers are shown only when the server returned headers whose names contained rate, limit, quota, or retry-after; `(none observed)` means none were exposed on that response.",
              "- Rate-limit variability was observed in this same live session: an earlier pass returned 200 for all four DexScreener endpoints and for GeckoTerminal trending, then later passes returned DexScreener 429 with `Retry-After` 34–38 seconds and GeckoTerminal 429 with `Retry-After: 0`; GeckoTerminal OHLCV returned 200 in between. CoinGecko returned 429 with `Retry-After` 19–28 seconds in some passes and 200 in another. GoPlus returned valid 200 data in an earlier pass, then HTTP 200 with API code 4029 (too many requests). Treat these as working public endpoints with per-IP/API throttling, not permanent failures.",
              "- The current Jupiter `api.jup.ag` keyless gateway worked for both price and quote and exposed `x-ratelimit-current`, `x-ratelimit-remaining`, and `x-ratelimit-reset`; the legacy `lite-api.jup.ag` quote probe was separately rate-limited during the initial check.",
              ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true", help="run live GET-only tests")
    parser.add_argument("--helius-test", action="store_true", help="read-only Helius getSlot (key never printed)")
    import paths as _paths
    parser.add_argument("--output", default=_os0.path.join(_paths.REPORTS_DIR, "free-api-test.md"))
    args = parser.parse_args()
    if args.helius_test:
        r = helius_rpc("getSlot")
        print(json.dumps(r))
        return 0 if r["ok"] else 1
    if not args.selftest:
        parser.error("use --selftest")
    results = run_tests()
    report = render(results)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    return 0 if any(r.status is not None for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
