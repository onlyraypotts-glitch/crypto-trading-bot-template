#!/usr/bin/env python3
"""Shared read-only HTTP helpers for the data tools (stdlib only).

- GET (and JSON-RPC POST for read-only RPC methods) with timeouts.
- ONE polite, cross-process rate limiter shared by every tool on the machine (file locks in STATE_DIR):
  each API host group has a minimum spacing between calls; concurrent runs reserve slots under an
  exclusive lock, so two scanners/selftests running at once do not burst a provider.
- Rate-limit handling per the prompts (§2G): on HTTP 429 / Cloudflare 1015 / a 200 body carrying a
  rate-limit code (callers report it via note_rate_limited), the host group goes into a shared COOLDOWN of
  max(Retry-After, 60 s), escalating 2 → 5 → 15 min on repeated hits within 30 min. During a cooldown calls
  fail fast (no hammering, no parallel retries) unless the caller allows a short wait (max_wait).
- Shared file cache with TTL (CACHE_DIR) for GETs, so repeated/concurrent runs reuse fresh responses.
  Default TTLs per host group; pass cache_ttl=0 to bypass. POSTs (RPC) are never cached.
  Env: GROKBOT_NO_CACHE=1 disables the cache; GROKBOT_HTTP_DIR overrides the state/cache root;
  GROKBOT_MAX_WAIT=<s> lets a batch job wait out cooldowns up to <s> seconds (one call after the wait; never parallel).
- Retries on 408/425/5xx and network errors with exponential backoff + jitter.
- Never logs secrets: callers pass `redact` strings that are scrubbed from errors/URLs; cache keys are
  SHA-256 hashes, so a key inside a URL is never written to disk (and keyed POSTs are not cached anyway).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

DEFAULT_UA = "grokbot-data-tools/1.1 (read-only; no-key)"
RETRY_CODES = {408, 425}
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths as _paths  # noqa: E402
ROOT_DIR = _paths.HTTP_DIR
STATE_DIR = os.path.join(ROOT_DIR, "limiter")
CACHE_DIR = os.path.join(ROOT_DIR, "cache")
COOLDOWN_STEPS = [60.0, 120.0, 300.0, 900.0]  # prompt §2G: >= 60 s, then 2, 5, 15 min
STRIKE_RESET_S = 1800.0
MAX_LOCK_WAIT_S = 5.0
MAX_SLOT_WAIT_S = 30.0
MAX_COOLDOWN_WAIT_S = 70.0   # batch scans (GROKBOT_MAX_WAIT) may sit out ONE minimum 60 s cooldown; longer ones fail fast
MAX_HTTP_TIMEOUT_S = 30.0
MAX_BACKOFF_S = 30.0

# (host suffix, group, min spacing seconds, default GET cache TTL seconds). First match wins.
HOST_RULES: list[tuple[str, str, float, float]] = [
    ("api.coingecko.com", "coingecko", 6.5, 120),       # CoinGecko + GeckoTerminal share the per-IP budget
    ("api.geckoterminal.com", "coingecko", 6.5, 300),   # (re-confirmed 2026-09-25: GT 429s right after CoinGecko calls)
    ("api.dexscreener.com", "dexscreener", 1.0, 180),   # ~300/min pairs; boosts/profiles ~60/min (see PATH_RULES).
                                                        # 2026-09-25: spacing 0.35->1.0 s and TTL 60->180 s after repeated CF1015 bans
    ("api.rugcheck.xyz", "rugcheck", 1.1, 600),
    ("api.gopluslabs.io", "goplus", 5.0, 3600),        # 2026-09-25: code 4029 all morning -> 5 s spacing, 1 h cache (EVM calls batched)
    ("api.jup.ag", "jupiter", 2.0, 15),                 # keyless ~0.5 req/s shared (prompt §2G.1)
    ("lite-api.jup.ag", "jupiter", 2.0, 15),
    ("coins.llama.fi", "defillama", 0.3, 120),
    ("api.llama.fi", "defillama", 0.3, 600),
    ("api.coinpaprika.com", "coinpaprika", 0.6, 120),
    ("api.alternative.me", "alternative", 1.0, 600),
    ("api.binance.us", "binanceus", 0.25, 120),
    ("api.gdeltproject.org", "gdelt", 6.0, 600),
    ("helius-rpc.com", "helius", 0.15, 0),
    ("api.mainnet-beta.solana.com", "solana-rpc", 0.25, 0),
    ("sec.gov", "sec", 0.15, 3600),
    ("cboe.com", "cboe", 0.5, 60),
    ("nasdaq.com", "nasdaq", 1.0, 300),
    ("stlouisfed.org", "fred", 0.5, 3600),
    # multichain + social (2026-09-25)
    ("api.honeypot.is", "honeypot", 1.0, 600),
    ("blockscout.com", "blockscout", 1.0, 300),         # base/eth.blockscout.com, keyless
    ("graphql.mainnet.sui.io", "sui", 0.5, 3600),
    ("publicnode.com", "evm-rpc", 0.2, 0), ("mainnet.base.org", "evm-rpc", 0.2, 0), ("rpc.monad.xyz", "evm-rpc", 0.2, 0),
    ("llamarpc.com", "evm-rpc", 0.2, 0), ("bnbchain.org", "evm-rpc", 0.2, 0),
    ("www.reddit.com", "reddit", 11.0, 1800),           # public search RSS (JSON API returns 403 keyless); keyless allowance
                                                        # observed 2026-09-25: 1 request per ~10 s window (x-ratelimit-reset)
    ("t.me", "telegram", 2.0, 3600),                    # public channel preview pages
]
PATH_RULES = [("api.dexscreener.com", ("/token-boosts", "/token-profiles", "/community-takeovers", "/ads"),
               "dexscreener-promo", 1.5, 600),         # chain-agnostic lists: one fetch serves every chain in a scan
              ("api.dexscreener.com", ("/orders/",), "dexscreener-promo", 1.5, 1800),
              ("api.geckoterminal.com", ("/api/v2/networks/",), "coingecko", 6.5, 300)]
DEFAULT_RULE = ("other", 0.5, 300)  # RSS feeds etc.: spacing is per host


@dataclass
class Resp:
    ok: bool
    status: int | None
    data: Any = None
    text: str = ""
    error: str = ""
    attempts: int = 0
    latency_ms: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""
    cached: bool = False


def _scrub(s: str, redact: list[str] | None) -> str:
    for r in redact or []:
        if r:
            s = s.replace(r, "***")
    return s


def _rate_headers(h: Any) -> dict[str, str]:
    out = {}
    for k, v in (h or {}).items():
        lk = k.lower()
        if any(t in lk for t in ("rate", "limit", "retry-after", "quota")):
            out[k] = v
    return out


# ---------------------------------------------------------------- shared polite limiter
def host_rule(url: str) -> tuple[str, float, float]:
    """(group, min spacing s, default cache TTL s) for a URL."""
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    for suffix, prefixes, group, spacing, ttl in PATH_RULES:
        if host.endswith(suffix) and p.path.startswith(prefixes):
            return group, spacing, ttl
    for suffix, group, spacing, ttl in HOST_RULES:
        if host == suffix or host.endswith("." + suffix) or host.endswith(suffix):
            return group, spacing, ttl
    return f"{DEFAULT_RULE[0]}-{host or 'unknown'}", DEFAULT_RULE[1], DEFAULT_RULE[2]


@contextmanager
def _locked_state(group: str):
    """Bounded lock for tiny limiter state files; cache writes are lock-free."""
    os.makedirs(STATE_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in group)
    fd = os.open(os.path.join(STATE_DIR, safe + ".json"), os.O_RDWR | os.O_CREAT, 0o644)
    with os.fdopen(fd, "r+") as f:
        deadline = time.monotonic() + MAX_LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"limiter state lock timeout for {group}")
                time.sleep(0.05)
        try:
            try:
                f.seek(0)
                raw = f.read()
                st = json.loads(raw) if raw.strip() else {}
            except (ValueError, OSError):
                st = {}
            box = {"state": st}
            yield box
            f.seek(0)
            f.truncate()
            f.write(json.dumps(box["state"]))
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def cooldown_remaining(url: str) -> float:
    group, _, _ = host_rule(url)
    try:
        with _locked_state(group) as box:
            return max(0.0, float(box["state"].get("cooldown_until", 0)) - time.time())
    except (OSError, TimeoutError, ValueError):
        return 0.0


def polite_acquire(url: str, max_wait: float = 0.0) -> tuple[bool, str]:
    """Reserve a slot, never waiting over 30 seconds for cooldown or spacing."""
    group, spacing, _ = host_rule(url)
    wait_budget = min(max(0.0, float(max_wait or 0.0)), MAX_COOLDOWN_WAIT_S)
    try:
        with _locked_state(group) as box:
            st = box["state"]
            now = time.time()
            cd = float(st.get("cooldown_until", 0)) - now
            if cd > wait_budget:
                return False, (f"polite-limiter: {group} cooling down {int(cd)}s more after a rate limit "
                               f"(strike {st.get('strikes', 0)}); not calling (prompt §2G)")
            start = max(now, float(st.get("next_ok", 0)), float(st.get("cooldown_until", 0)))
            wait = start - now
            if wait > max(MAX_SLOT_WAIT_S, wait_budget):
                return False, f"polite-limiter: {group} next slot is {int(wait)}s away; soft-failing"
            st["next_ok"] = start + spacing
    except (OSError, TimeoutError, ValueError) as exc:
        return False, f"polite-limiter unavailable: {exc}"
    if wait > 0:
        time.sleep(min(wait, max(MAX_SLOT_WAIT_S, wait_budget)))
    return True, ""


def note_rate_limited(url: str, retry_after: float = 0.0, why: str = "429") -> float:
    """Record a rate-limit hit; bookkeeping failure remains a soft failure."""
    group, _, _ = host_rule(url)
    cd = max(float(retry_after or 0), COOLDOWN_STEPS[0])
    try:
        with _locked_state(group) as box:
            st = box["state"]
            now = time.time()
            strikes = int(st.get("strikes", 0)) if now - float(st.get("last_hit", 0)) < STRIKE_RESET_S else 0
            cd = max(float(retry_after or 0), COOLDOWN_STEPS[min(strikes, len(COOLDOWN_STEPS) - 1)])
            st.update(strikes=strikes + 1, last_hit=now,
                      cooldown_until=max(float(st.get("cooldown_until", 0)), now + cd), last_reason=why)
    except (OSError, TimeoutError, ValueError):
        pass
    return cd


def note_success(url: str) -> None:
    group, _, _ = host_rule(url)
    with _locked_state(group) as box:
        st = box["state"]
        if st.get("strikes") and time.time() - float(st.get("last_hit", 0)) > STRIKE_RESET_S:
            st["strikes"] = 0


def limiter_status() -> dict:
    out = {}
    if not os.path.isdir(STATE_DIR):
        return out
    for fn in sorted(os.listdir(STATE_DIR)):
        try:
            st = json.load(open(os.path.join(STATE_DIR, fn)))
        except Exception:
            continue
        cd = float(st.get("cooldown_until", 0)) - time.time()
        out[fn[:-5]] = {"cooldown_s": max(0, int(cd)), "strikes": st.get("strikes", 0), "last_reason": st.get("last_reason")}
    return out


# ---------------------------------------------------------------- shared file cache
def _cache_path(method: str, url: str, body: Any) -> str:
    h = hashlib.sha256(f"{method} {url} {json.dumps(body, sort_keys=True) if body is not None else ''}".encode())
    return os.path.join(CACHE_DIR, h.hexdigest()[:40] + ".json")


def _cache_get(path: str, ttl: float) -> dict | None:
    try:
        if time.time() - os.path.getmtime(path) > ttl:
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _cache_put(path: str, obj: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
        if random.random() < 0.02:
            prune_cache()
    except (OSError, TypeError, ValueError):
        pass


def prune_cache(max_age_s: float = 86400) -> int:
    n = 0
    try:
        for fn in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, fn)
            if time.time() - os.path.getmtime(p) > max_age_s:
                os.remove(p)
                n += 1
    except OSError:
        pass
    return n


# ---------------------------------------------------------------- request
def request(url: str, *, method: str = "GET", body: Any = None, headers: dict[str, str] | None = None,
            timeout: float = 15.0, retries: int = 3, base_delay: float = 1.0, max_retry_after: float = 40.0,
            parse: str = "json", redact: list[str] | None = None, cache_ttl: float | None = None,
            max_wait: float | None = None, cache_if: Any = None) -> Resp:
    """Perform a read-only request. parse = 'json' | 'text' | 'bytes'.
    cache_ttl: None = host default for GETs; 0 = bypass. max_wait: seconds we may wait out a shared cooldown.
    cache_if: optional callable(data) -> bool; only responses it accepts are cached (e.g. skip 200 error bodies).
    max_retry_after is kept for backward compatibility (429s now go to the shared cooldown instead)."""
    shown = _scrub(url, redact)
    try:
        timeout = min(MAX_HTTP_TIMEOUT_S, max(0.1, float(timeout)))
    except (TypeError, ValueError):
        timeout = MAX_HTTP_TIMEOUT_S
    if max_wait is None:  # batch jobs (e.g. the scanner) may set GROKBOT_MAX_WAIT to wait out SHORT cooldowns
        max_wait = float(os.environ.get("GROKBOT_MAX_WAIT", "0") or 0)
    _, _, default_ttl = host_rule(url)
    ttl = default_ttl if cache_ttl is None else cache_ttl
    use_cache = method == "GET" and ttl > 0 and parse != "bytes" and os.environ.get("GROKBOT_NO_CACHE") != "1"
    cpath = _cache_path(method, url, body) if use_cache else ""
    if use_cache:
        hit = _cache_get(cpath, ttl)
        if hit is not None:
            cached_text = hit.get("text", "")
            return Resp(True, hit.get("status", 200),
                        data=hit.get("data") if parse == "json" else cached_text,
                        text=cached_text if parse == "text" else "",
                        headers=hit.get("headers", {}), url=shown, cached=True)
    hdrs = {"User-Agent": DEFAULT_UA, "Accept": "application/json, text/csv, text/plain, */*"}
    hdrs.update(headers or {})
    # Optional CoinGecko key (env only, never logged): COINGECKO_DEMO_API_KEY (free demo plan) or COINGECKO_PRO_API_KEY.
    if urllib.parse.urlparse(url).hostname == "api.coingecko.com":
        _cg = os.environ.get("COINGECKO_DEMO_API_KEY", "").strip()
        if _cg:
            hdrs.setdefault("x-cg-demo-api-key", _cg)
    elif urllib.parse.urlparse(url).hostname == "pro-api.coingecko.com":
        _cg = os.environ.get("COINGECKO_PRO_API_KEY", "").strip()
        if _cg:
            hdrs.setdefault("x-cg-pro-api-key", _cg)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    started = time.perf_counter()
    last = Resp(False, None, url=shown)
    for attempt in range(retries + 1):
        last.attempts = attempt + 1
        wait = base_delay * (2 ** attempt) + random.random() * 0.3
        ok_slot, why = polite_acquire(url, max_wait=max_wait)
        if not ok_slot:
            last.status, last.error = 429, why
            break
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                last.status = int(r.status)
                last.headers = _rate_headers(r.headers)
                if parse == "bytes":
                    last.data = raw
                    last.ok = 200 <= int(r.status) < 300
                    last.error = ""
                    break
                txt = raw.decode("utf-8", errors="replace")
                last.text = txt if parse == "text" else ""
                if parse == "json":
                    try:
                        last.data = json.loads(txt)
                    except Exception as exc:
                        last.error = f"non-JSON response ({r.headers.get('Content-Type','')}): {exc}; body starts: {txt[:120]!r}"
                        last.ok = False
                        break
                else:
                    last.data = txt
                last.ok = 200 <= last.status < 300
                last.error = ""
                break
        except urllib.error.HTTPError as exc:
            last.status = int(exc.code)
            last.headers = _rate_headers(exc.headers)
            try:
                b = exc.read(300).decode("utf-8", errors="replace")
            except Exception:
                b = ""
            last.error = _scrub((b or str(exc.reason)).replace("\n", " ")[:300], redact)
            if exc.code == 429 or "error code: 1015" in b:
                ra = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    ra_s = float(ra) if ra is not None else 0.0
                except ValueError:
                    ra_s = 0.0
                cd = note_rate_limited(url, ra_s, why=f"HTTP {exc.code}{' CF1015' if '1015' in b else ''}")
                last.error = f"rate limited (HTTP {exc.code}); shared cooldown {int(cd)}s set. {last.error[:120]}"
                break  # no immediate retries on a rate limit (prompt §2G)
            if exc.code not in RETRY_CODES and not 500 <= exc.code <= 599:
                break
        except Exception as exc:  # URLError, timeout, OSError
            last.error = _scrub(str(exc), redact)
        if attempt < retries:
            time.sleep(min(MAX_BACKOFF_S, max(0.0, wait)))
    last.latency_ms = int((time.perf_counter() - started) * 1000)
    if last.ok and use_cache and (cache_if is None or _safe_accept(cache_if, last.data)):
        _cache_put(cpath, {"status": last.status, "data": last.data if parse == "json" else None,
                           "text": last.text if parse == "text" else "", "headers": last.headers, "ts": time.time()})
    return last


def _safe_accept(fn: Any, data: Any) -> bool:
    try:
        return bool(fn(data))
    except Exception:
        return False


def get_json(url: str, **kw) -> Resp:
    return request(url, parse="json", **kw)


def get_json_retry(url: str, **kw) -> Resp:
    """get_json, plus ONE more attempt when the first hits a rate limit whose cooldown is short enough to sit out
    (<= MAX_COOLDOWN_WAIT_S; the limiter does the waiting, so there is never a burst). For primary data sources only
    (Jupiter token data, GeckoTerminal lists); never used for DexScreener while Cloudflare (1015) is banning the IP."""
    r = get_json(url, **kw)
    if r.ok or r.status != 429:
        return r
    if cooldown_remaining(url) > MAX_COOLDOWN_WAIT_S:
        return r
    kw = dict(kw)
    kw["max_wait"] = max(float(kw.get("max_wait") or 0), MAX_COOLDOWN_WAIT_S)
    return get_json(url, **kw)


def get_text(url: str, **kw) -> Resp:
    return request(url, parse="text", **kw)


def post_json(url: str, body: Any, **kw) -> Resp:
    return request(url, method="POST", body=body, parse="json", **kw)


def get_bytes(url: str, **kw) -> Resp:
    return request(url, parse="bytes", **kw)


def selftest() -> list[dict]:
    """Offline checks of the limiter and cache (uses a temp state dir; no network)."""
    import tempfile
    global STATE_DIR, CACHE_DIR
    rows = []
    old = (STATE_DIR, CACHE_DIR)
    tmp = tempfile.mkdtemp(prefix="http-selftest-")
    STATE_DIR, CACHE_DIR = os.path.join(tmp, "limiter"), os.path.join(tmp, "cache")
    try:
        u = "https://api.rugcheck.xyz/v1/x"
        t = time.perf_counter()
        polite_acquire(u)
        polite_acquire(u)
        dt = time.perf_counter() - t
        rows.append({"check": "spacing enforced between calls to one host group", "ok": dt >= 1.0, "detail": f"{dt:.2f}s for 2 calls (spacing 1.1s)"})
        cd = note_rate_limited(u, 0)
        ok, why = polite_acquire(u)
        rows.append({"check": "429 sets a shared cooldown >= 60 s and calls fail fast", "ok": cd >= 60 and not ok, "detail": why[:120]})
        cd2 = note_rate_limited(u, 0)
        rows.append({"check": "repeat 429 escalates cooldown (2 min)", "ok": cd2 >= 120, "detail": f"{cd2:.0f}s"})
        cd3 = note_rate_limited("https://api.dexscreener.com/x", 90)
        rows.append({"check": "Retry-After above 60 s honored", "ok": cd3 >= 90, "detail": f"{cd3:.0f}s"})
        p = _cache_path("GET", "https://example.com/a", None)
        _cache_put(p, {"status": 200, "data": {"a": 1}})
        rows.append({"check": "cache hit within TTL", "ok": (_cache_get(p, 60) or {}).get("data") == {"a": 1}, "detail": os.path.basename(p)})
        os.utime(p, (time.time() - 120, time.time() - 120))
        rows.append({"check": "cache miss after TTL", "ok": _cache_get(p, 60) is None, "detail": "ttl 60s, age 120s"})
        rows.append({"check": "GeckoTerminal shares CoinGecko's budget", "ok": host_rule("https://api.geckoterminal.com/x")[0] == host_rule("https://api.coingecko.com/x")[0], "detail": host_rule("https://api.geckoterminal.com/x")[0]})
        k = "SECRETKEY123"
        rows.append({"check": "cache filename never contains a URL key", "ok": k not in _cache_path("GET", f"https://x.io/?api-key={k}", None), "detail": "sha256 name"})
    finally:
        STATE_DIR, CACHE_DIR = old
    return [dict(tool="_http", status="-", ms=0, **r) for r in rows]


if __name__ == "__main__":
    import sys
    if any(x in sys.argv for x in ("-h", "--help")):
        print(__doc__)
        print("usage: python3 _http.py [--selftest]   (no args: print shared limiter status)")
        raise SystemExit(0)
    if "--selftest" in sys.argv:
        rs = selftest()
        for r in rs:
            print(("PASS" if r["ok"] else "FAIL"), r["check"], "|", r["detail"])
        raise SystemExit(0 if all(r["ok"] for r in rs) else 1)
    print(json.dumps(limiter_status(), indent=2))
