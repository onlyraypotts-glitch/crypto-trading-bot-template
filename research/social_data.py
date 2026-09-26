#!/usr/bin/env python3
"""social_data.py: READ-ONLY, keyless social/attention evidence for the prompt's S4 (social momentum), all chains.

Prompt §2C.4 S4: 0 none or botted · 1 one source · 2 rising, low quality · 3 accelerating, independent, 2+ platforms
· 4 that, and before retail saturation.  (pump.fun comments, raw counts and @grok/AI replies aren't independent.)

Sources, verified keyless on 2026-09-25 (see SCORING_NOTES.md):
  reddit     www.reddit.com/search.rss (Atom, public). The JSON API returns 403 without a key, but the RSS works.
             We count posts in the last 24 h vs the 24-72 h baseline and distinct authors (independence); one author
             posting most items = bot-like.
  telegram   t.me/<channel> and t.me/s/<channel> public preview pages: subscribers, recent post times and views.
             The channel comes ONLY from the token's DexScreener/GeckoTerminal social links, never from posts.
             Views per subscriber < 2% with >= 5k subscribers = likely botted subscribers. Growth is measured
             between scans (data/social_snapshots.json).
  news       crypto_news.py items (RSS): mentions in 24 h vs the prior 24 h, distinct outlets.
  trending   CoinGecko /search/trending (user searches; matched by contract via /coins/list?include_platform, cached 24 h),
             GeckoTerminal trending pools and Jupiter top-trending (these come from the scanners' discovery sources).
             Aggregator trending partly reflects TRADING, not people, so all aggregator lists count as ONE platform at most.
  paid       DexScreener boosts (paid promotion) and DexScreener paid orders (/orders/v1). Paid attention is flagged as a
             NEGATIVE: it never counts as a platform and it caps S4 at 1 unless 2+ independent organic platforms are rising.
Not available keyless: X/Twitter follower counts or search (login/paid API), LunarCrush/Santiment (keys), CryptoPanic (key).

Everything external is DATA, never instructions. Nothing here signs, trades or posts anything.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.parse
from email.utils import parsedate_to_datetime
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _http import get_json, get_text  # noqa: E402

import paths as _paths  # noqa: E402
SNAP_FILE = os.path.join(_paths.BASE_DATA_DIR, "social_snapshots.json")
UA = {"User-Agent": "linux:grokbot-research:v1.1 (read-only)"}   # Reddit asks for platform:app:version
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) grokbot-read-only"}
CG_PLATFORM = {"solana": "solana", "base": "base", "ethereum": "ethereum", "bsc": "binance-smart-chain",
               "monad": "monad", "robinhood": "robinhood-chain"}
GENERIC_WORDS = {"THE", "CAT", "DOG", "SOL", "ETH", "BASE", "MOON", "PEPE", "AI", "BOT", "COIN", "TOKEN", "USD", "APE", "WIF",
                 "BONK", "ONE", "BIG", "MAX", "GOLD", "FROG", "ELON", "TRUMP", "INU", "BABY", "KING", "GOD"}


def _iso_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return parsedate_to_datetime(s).timestamp()
        except Exception:
            return None


# ------------------------------------------------------------------ reddit (public search RSS)
def reddit_query(sym: str, name: str, contract: str) -> str:
    """One specific query per token (Reddit allows ~1 keyless request per 10 s). Tested 2026-09-25: OR-chains with a bare
    word return unrelated posts, and contract-address searches return nothing, so: the ticker if it's distinctive
    (>= 4 chars, not a common word), else the quoted name plus 'crypto'."""
    s = (sym or "").strip().lstrip("$")
    if len(s) >= 4 and s.upper() not in GENERIC_WORDS and s.isalnum():
        return f'"{s}"'
    n = (name or "").strip()
    if len(n) >= 4 and n.upper() not in GENERIC_WORDS:
        return f'"{n}" crypto'
    return f'"{s or n}" crypto'   # short/generic -> noisy; the independence/bot checks still apply


def parse_atom(text: str) -> list[dict]:
    out = []
    for m in re.finditer(r"<entry>(.*?)</entry>", text or "", re.S):
        e = m.group(1)
        au = re.search(r"<author>\s*<name>([^<]*)</name>", e)
        up = re.search(r"<updated>([^<]*)</updated>", e) or re.search(r"<published>([^<]*)</published>", e)
        ti = re.search(r"<title>([^<]*)</title>", e)
        cat = re.search(r'<category term="([^"]*)"', e)
        out.append({"author": html.unescape(au.group(1)) if au else None, "ts": _iso_ts(up.group(1) if up else None),
                    "title": html.unescape(ti.group(1))[:140] if ti else "", "sub": cat.group(1) if cat else None})
    return out


def reddit_signal(sym: str, name: str, contract: str, now: float | None = None) -> dict:
    now = now or time.time()
    q = reddit_query(sym, name, contract)
    url = "https://www.reddit.com/search.rss?" + urllib.parse.urlencode({"q": q, "sort": "new", "t": "week", "limit": 100, "type": "link"})
    r = get_text(url, headers=UA, timeout=15, retries=1)
    if not r.ok or "<feed" not in (r.text or ""):
        return {"platform": "reddit", "ok": False, "error": f"HTTP {r.status}: {(r.error or '')[:80]}", "query": q}
    return reddit_from_entries(parse_atom(r.text), now) | {"query": q}


def reddit_from_entries(entries: list[dict], now: float, limit: int = 100) -> dict:
    entries = [e for e in entries if e.get("author") and e.get("ts")]   # drop subreddit/community results
    d1 = [e for e in entries if e["ts"] and now - e["ts"] <= 86400]
    d3 = [e for e in entries if e["ts"] and 86400 < now - e["ts"] <= 3 * 86400]
    authors = [e["author"] for e in d1 if e["author"]]
    uniq = len(set(authors))
    top_share = max((authors.count(a) for a in set(authors)), default=0) / len(authors) if authors else 0
    titles = [re.sub(r"\W+", " ", e["title"].lower()).strip() for e in d1]
    dup_share = (len(titles) - len(set(titles))) / len(titles) if titles else 0
    base = len(d3) / 2.0
    botlike = len(d1) >= 3 and (top_share > 0.5 or dup_share > 0.5)
    # If the newest-first page is full and all inside 24 h, the baseline window is cut off: "rising" can't be claimed.
    saturated_window = len(entries) >= limit and len(d1) == len(entries)
    return {"platform": "reddit", "ok": True, "posts_24h": len(d1), "posts_prev_daily": round(base, 2), "authors_24h": uniq,
            "top_author_share": round(top_share, 2), "duplicate_title_share": round(dup_share, 2),
            "baseline_cut_off": saturated_window,
            "active": len(d1) >= 1, "rising": (not saturated_window) and len(d1) >= 3 and len(d1) >= 1.5 * max(base, 1.0),
            "independent": uniq >= 3 and top_share <= 0.5, "botlike": botlike,
            "sample": [f"r/{e['sub']}: {e['title'][:80]}" for e in d1[:2]]}


# ------------------------------------------------------------------ telegram (public preview pages)
TG_RE = re.compile(r"^https?://(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{5,32})/?$")


def telegram_channel(links: list[str]) -> str | None:
    for u in links or []:
        m = TG_RE.match((u or "").strip())
        if m and m.group(1).lower() not in ("joinchat", "share", "addstickers", "proxy"):
            return m.group(1)
    return None


def _num(s: str) -> float | None:
    s = (s or "").strip().replace(",", "").replace(" ", "")
    m = re.match(r"^([\d.]+)([KkMm]?)$", s)
    if not m:
        return None
    v = float(m.group(1))
    return v * (1e3 if m.group(2) in "Kk" and m.group(2) else 1e6 if m.group(2) in "Mm" and m.group(2) else 1)


def telegram_from_html(page: str, now: float) -> dict:
    subs = None
    m = re.search(r'counter_value">([^<]+)</span>\s*<span class="counter_type">(subscribers|members)', page or "")
    if m:
        subs = _num(m.group(1))
    else:
        m = re.search(r'tgme_page_extra">([\d ,.KkMm]+)\s+(subscribers|members)', page or "")
        subs = _num(m.group(1)) if m else None
    times = [_iso_ts(x) for x in re.findall(r'<time[^>]*datetime="([^"]+)"', page or "")]
    times = [t for t in times if t]
    views = [_num(v) for v in re.findall(r'tgme_widget_message_views">([^<]+)<', page or "")]
    views = [v for v in views if v is not None][-10:]
    last = max(times) if times else None
    posts_24h = sum(1 for t in times if now - t <= 86400)
    avg_views = sum(views) / len(views) if views else None
    vps = (avg_views / subs) if (avg_views and subs) else None
    botlike = bool(subs and subs >= 5000 and vps is not None and vps < 0.02)
    return {"subscribers": subs, "last_post_age_h": round((now - last) / 3600, 1) if last else None, "posts_24h": posts_24h,
            "avg_views_recent": round(avg_views) if avg_views else None, "views_per_subscriber": round(vps, 3) if vps else None,
            "botlike": botlike, "active": bool(last and now - last <= 48 * 3600)}


def telegram_signal(channel: str | None, key: str, snaps: dict, now: float | None = None) -> dict:
    now = now or time.time()
    if not channel:
        return {"platform": "telegram", "ok": None, "note": "no Telegram link in the token's listed socials (not applicable)"}
    r = get_text(f"https://t.me/s/{channel}", headers=BROWSER_UA, timeout=15, retries=1)
    if not r.ok:
        return {"platform": "telegram", "ok": False, "error": f"HTTP {r.status}: {(r.error or '')[:80]}", "channel": channel}
    d = telegram_from_html(r.text, now)
    if d["subscribers"] is None:  # /s/ preview disabled for some channels -> plain page has the count
        r2 = get_text(f"https://t.me/{channel}", headers=BROWSER_UA, timeout=15, retries=1)
        if r2.ok:
            d["subscribers"] = telegram_from_html(r2.text, now)["subscribers"]
    growth = None
    old = [x for x in snaps.get(key, {}).get("tg", []) if 6 * 3600 <= now - x[0] <= 72 * 3600]
    if old and d["subscribers"] and old[0][1]:
        days = (now - old[0][0]) / 86400
        growth = round(((d["subscribers"] / old[0][1]) - 1) * 100 / max(days, 0.25), 2)  # % per day
    d.update(platform="telegram", ok=d["subscribers"] is not None, channel=channel, growth_pct_per_day=growth,
             rising=bool(d["active"] and not d["botlike"] and ((growth is not None and growth >= 5) or d["posts_24h"] >= 3)),
             independent=not d["botlike"])
    return d


# ------------------------------------------------------------------ news velocity (crypto_news items)
def news_signal(news: dict, name: str, sym: str, contract: str, now: float | None = None) -> dict:
    now = now or time.time()
    items = (news or {}).get("items")
    if items is None:
        return {"platform": "news", "ok": False, "error": "no news context"}
    terms = [t for t in {contract, f"${sym}" if sym else "", sym if sym and len(sym) >= 4 and sym.upper() not in GENERIC_WORDS else "",
                         name if name and len(name) >= 5 and name.upper() not in GENERIC_WORDS else ""} if t]
    d1, d2 = [], []
    for it in items:
        hay = f" {it.get('title', '')} {it.get('summary', '')} ".lower()
        if it.get("possible_injection") or (it.get("weight", 1.0) < 0.5):
            continue
        if any(re.search(r"(?<![a-z0-9])" + re.escape(t.lower()) + r"(?![a-z0-9])", hay) for t in terms):
            ts = it.get("_ts") or 0
            (d1 if now - ts <= 86400 else d2 if now - ts <= 2 * 86400 else []).append(it)
    outlets = {i.get("source") for i in d1}
    return {"platform": "news", "ok": True, "mentions_24h": len(d1), "mentions_prev_24h": len(d2), "outlets_24h": len(outlets),
            "active": len(d1) >= 1, "rising": len(d1) >= 2 and len(d1) > len(d2), "independent": len(outlets) >= 2, "botlike": False,
            "sample": [f"{i.get('source')}: {(i.get('title') or '')[:80]}" for i in d1[:2]]}


# ------------------------------------------------------------------ CoinGecko trending (matched by contract)
def coingecko_trending_contracts(log: list | None = None) -> dict[str, set]:
    """{chain: {contract(lower)}} for the coins on CoinGecko's trending list. Two calls, heavily cached."""
    tr = get_json("https://api.coingecko.com/api/v3/search/trending", cache_ttl=1800, timeout=20)
    ids = [((c or {}).get("item") or {}).get("id") for c in ((tr.data or {}).get("coins") or [])] if tr.ok and isinstance(tr.data, dict) else []
    out: dict[str, set] = {}
    if not ids:
        if log is not None:
            log.append({"source": "coingecko search/trending", "ok": False, "status": tr.status, "note": (tr.error or "")[:100]})
        return out
    lst = get_json("https://api.coingecko.com/api/v3/coins/list?include_platform=true", cache_ttl=86400, timeout=30)
    if not lst.ok or not isinstance(lst.data, list):
        if log is not None:
            log.append({"source": "coingecko coins/list (platform map)", "ok": False, "status": lst.status, "note": (lst.error or "")[:100]})
        return out
    idset = set(ids)
    inv = {v: k for k, v in CG_PLATFORM.items()}
    for c in lst.data:
        if c.get("id") in idset:
            for plat, addr in (c.get("platforms") or {}).items():
                if plat in inv and addr:
                    out.setdefault(inv[plat], set()).add(addr.lower() if addr.startswith("0x") else addr)
    if log is not None:
        log.append({"source": "coingecko trending (by contract)", "ok": True, "status": 200, "items": len(ids),
                    "note": f"{sum(len(v) for v in out.values())} contracts on tracked chains"})
    return out


# ------------------------------------------------------------------ paid promotion
def paid_signal(chain_ds: str, contract: str, pair: dict | None, sources: set | list, check_orders: bool = True) -> dict:
    boosts = ((pair or {}).get("boosts") or {}).get("active") or 0
    src_paid = [s for s in (sources or []) if "boost" in s]
    orders = []
    if check_orders:
        r = get_json(f"https://api.dexscreener.com/orders/v1/{chain_ds}/{contract}", timeout=15, retries=1)
        if r.ok and isinstance(r.data, list):
            orders = [f"{o.get('type')}:{o.get('status')}" for o in r.data if (o.get("status") or "") in ("approved", "processing")]
    paid = bool(boosts or src_paid or any(o.startswith(("tokenAd", "trendingBarAd")) for o in orders))
    return {"paid_promotion": paid, "active_boosts": boosts, "paid_sources": src_paid, "orders": orders[:6]}


# ------------------------------------------------------------------ S4
def s4_level(sig: dict, established: bool = False, mcap: float | None = None) -> tuple[int, float, str, list[str]]:
    """-> (level 0-4, confidence, explanation, negatives). Pure function (unit-tested)."""
    plats = [p for p in sig.get("platforms", []) if p.get("ok")]
    tried = [p for p in sig.get("platforms", []) if p.get("ok") is not None]
    negatives = []
    for p in plats:
        if p.get("botlike"):
            negatives.append(f"{p['platform']} looks botted")
    if sig.get("paid", {}).get("paid_promotion"):
        negatives.append("paid promotion (DexScreener boosts/ads)")
    active = [p["platform"] for p in plats if p.get("active")]
    if sig.get("aggregator_trending"):
        active.append("aggregator-trending")
    rising_indep = [p["platform"] for p in plats if p.get("rising") and p.get("independent") and not p.get("botlike")]
    if sig.get("coingecko_trending"):
        rising_indep.append("coingecko-trending")  # driven by user searches: independent attention
    elif sig.get("aggregator_trending") and not sig.get("paid", {}).get("paid_promotion"):
        rising_indep.append("aggregator-trending")
    rising_indep = sorted(set(rising_indep))
    if any("botted" in n for n in negatives):
        lvl = 0
    elif not active:
        lvl = 0
    elif len(rising_indep) >= 2:
        saturated = established or (mcap or 0) >= 5e8 or bool(sig.get("coingecko_trending"))
        lvl = 4 if (len(rising_indep) >= 3 and not saturated) else 3
    elif rising_indep or len(active) >= 2:
        lvl = 2
    else:
        lvl = 1
    if "paid promotion (DexScreener boosts/ads)" in negatives:
        lvl = min(lvl, 1) if len(rising_indep) < 2 else max(0, lvl - 1)
    ok_share = (len(plats) / len(tried)) if tried else 0.0
    conf = round(0.5 + 0.3 * ok_share, 2) if plats else 0.5   # 0.5 = nothing readable; max 0.8 (never "sure")
    expl = (f"S4 {lvl}: active on {', '.join(active) or 'none'}; rising+independent: {', '.join(rising_indep) or 'none'}"
            + (f"; NEGATIVE: {'; '.join(negatives)}" if negatives else "") + f"; sources read {len(plats)}/{len(tried)}")
    return lvl, conf, expl, negatives


def load_snaps() -> dict:
    try:
        return json.load(open(SNAP_FILE))
    except (OSError, ValueError):
        return {}


def save_snaps(snaps: dict, updates: dict) -> None:
    now = time.time()
    for k, v in updates.items():
        e = snaps.setdefault(k, {})
        for field, val in v.items():
            if val is not None:
                e.setdefault(field, []).append([now, val])
                e[field] = [x for x in e[field] if now - x[0] < 7 * 86400][-60:]
    os.makedirs(os.path.dirname(SNAP_FILE), exist_ok=True)
    json.dump(snaps, open(SNAP_FILE, "w"))


def token_social(chain: str, contract: str, sym: str, name: str, *, pair: dict | None, sources, news: dict,
                 cg_trending: dict | None, snaps: dict, updates: dict, established: bool = False, mcap: float | None = None,
                 extra_links: list[str] | None = None, use_reddit: bool = True, check_orders: bool = True) -> dict:
    """Collect every keyless signal for one token and grade S4. Never raises."""
    now = time.time()
    key = f"{chain}:{contract}"
    links = [s.get("url") for s in ((pair or {}).get("info") or {}).get("socials") or [] if isinstance(s, dict)]
    links += [f"https://t.me/{h}" for h in [((pair or {}).get("_gt_socials") or {}).get("telegram_handle")] if h]
    links += extra_links or []
    plats = []
    # A Reddit lookup skipped for the per-scan budget counts as NOT READ (ok False), so S4 confidence reflects the thin
    # coverage instead of rising because fewer sources were attempted.
    for fn in (lambda: reddit_signal(sym, name, contract, now) if use_reddit else
               {"platform": "reddit", "ok": False, "error": "skipped: per-scan Reddit budget used up (not read)"},
               lambda: telegram_signal(telegram_channel(links), key, snaps, now),
               lambda: news_signal(news, name, sym, contract, now)):
        try:
            plats.append(fn())
        except Exception as exc:  # optional evidence: an error is UNKNOWN, never a positive
            plats.append({"platform": "?", "ok": False, "error": f"{type(exc).__name__}: {exc}"[:120]})
    ds_chain = {"bsc": "bsc", "ethereum": "ethereum", "base": "base", "solana": "solana", "monad": "monad"}.get(chain, chain)
    try:
        paid = paid_signal(ds_chain, contract, pair, sources, check_orders)
    except Exception as exc:
        paid = {"paid_promotion": None, "error": str(exc)[:80]}
    srcs = set(sources or [])
    agg = sorted(s for s in srcs if s.startswith(("gt_trending", "jup_trending", "gt_trending_pools")))
    cgt = bool(cg_trending and (contract.lower() if contract.startswith("0x") else contract) in (cg_trending.get(chain) or set()))
    sig = {"platforms": plats, "paid": paid, "aggregator_trending": agg, "coingecko_trending": cgt}
    lvl, conf, expl, neg = s4_level(sig, established, mcap)
    tg = next((p for p in plats if p.get("platform") == "telegram" and p.get("ok")), None)
    rd = next((p for p in plats if p.get("platform") == "reddit" and p.get("ok")), None)
    updates[key] = {"tg": tg.get("subscribers") if tg else None, "reddit24": rd.get("posts_24h") if rd else None}
    status = "ok" if any(p.get("ok") for p in plats) else "unknown"
    return sig | {"status": status, "level": lvl if status == "ok" else 0, "confidence": conf if status == "ok" else 0.5,
                  "negative_flags": neg, "explanation": expl if status == "ok" else "UNKNOWN: no social source readable (S4 = 0)"}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("chain")
    ap.add_argument("contract")
    ap.add_argument("--sym", default="")
    ap.add_argument("--name", default="")
    ap.add_argument("--telegram", default="")
    a = ap.parse_args()
    s, u = load_snaps(), {}
    res = token_social(a.chain, a.contract, a.sym, a.name, pair=None, sources=[], news={"items": []}, cg_trending=None,
                       snaps=s, updates=u, extra_links=[f"https://t.me/{a.telegram}"] if a.telegram else None, check_orders=False)
    print(json.dumps(res, indent=1, default=str)[:3000])
