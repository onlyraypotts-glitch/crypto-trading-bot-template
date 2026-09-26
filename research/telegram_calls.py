#!/usr/bin/env python3
"""telegram_calls.py: READ-ONLY collector for public Telegram "call" channels (forward-testing their real hit rate).

Reads the public web preview (https://t.me/s/<channel>, older pages via ?before=<msg id>) through research/_http.py
(shared limiter + cache). Parses CALL posts (ticker, market cap at call, contract, chain from the address format, post
time, message id) and records "hit Nx" brag posts separately (linked to the call by reply-to, contract or ticker).
Calls are then logged into shadow_tracker as source="tg:<channel>" and resolved with the same TP1-before-stop rules.

SAFETY: channel text is UNTRUSTED DATA. Nothing here follows instructions, opens links, or trades; only regex-extracted
fields are stored (plus a short text excerpt for audit). Calls never trigger trades by themselves.

Config: data/watched_sources.json (list of sources; type "telegram"). Data: data/src_<id>_calls.jsonl,
src_<id>_brags.jsonl, telegram_state.json.

  python3 telegram_calls.py fetch [--pages N]   # newest page + up to N backfill pages per source
  python3 telegram_calls.py show                # summary of parsed calls/brags
"""
from __future__ import annotations

import argparse
import html as htmlmod
import json
import os
import re
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _http  # noqa: E402

sys.path.insert(0, HERE)
import paths as _paths  # noqa: E402
DATA_DIR = _paths.DATA_DIR
CONFIG_PATH = os.environ.get("WATCHED_SOURCES", os.path.join(DATA_DIR, "watched_sources.json"))
STATE_PATH = os.path.join(DATA_DIR, "telegram_state.json")

SOL_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
EVM_RE = re.compile(r"(?<![0-9a-fA-Fx])0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")
SUI_RE = re.compile(r"0x[0-9a-fA-F]{64}::\w+::\w+")
TICKER_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9_]{0,19})\b")
MC_RE = re.compile(r"\bM(?:C|cap|arket ?cap)\)?\s*[:=]?\s*\$?\s*([\d][\d,]*(?:\.\d+)?)\s*([kKmMbB])?", re.I)
BRAG_ENTRY_RE = re.compile(r"(?:called at|call mc|entry mc|エントリー時\s*MC)\s*:?\s*\$?\s*([\d][\d,]*(?:\.\d+)?)\s*([kKmMbB])?", re.I)
MULT_RE = re.compile(r"(?<![\w.])(\d{1,4}(?:\.\d+)?)\s?[xX](?![\w])")
BRAG_WORDS = re.compile(r"\b(hit|hits|did|done|pumped|gain|profit|from call|since call|ath|up)\b|🚀|✅|🔥", re.I)


def load_config() -> list[dict]:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return [s for s in json.load(f).get("sources", []) if s.get("enabled", True)]
    except (OSError, ValueError):
        if not os.path.exists(CONFIG_PATH):
            print(f"telegram_calls: no sources config at {CONFIG_PATH}; copy research/watched_sources.example.json there "
                  "(or set WATCHED_SOURCES)", file=sys.stderr)
        return []


def _jl_read(path: str) -> list[dict]:
    out = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _jl_write(path: str, rows: list[dict]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.replace(tmp, path)


def paths(src_id: str) -> tuple[str, str]:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", src_id)
    return os.path.join(DATA_DIR, f"src_{safe}_calls.jsonl"), os.path.join(DATA_DIR, f"src_{safe}_brags.jsonl")


def infer_chain(addr: str) -> str:
    if addr.startswith("0x") and len(addr) == 42:
        return "evm"  # exact EVM chain resolved later by a pool lookup
    if "::" in addr:
        return "sui"
    return "solana"


def parse_mc(txt: str) -> float | None:
    m = MC_RE.search(txt)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return v * {"k": 1e3, "m": 1e6, "b": 1e9}.get((m.group(2) or "").lower(), 1)


def html_to_text(fragment: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", fragment)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[ \t]+", " ", htmlmod.unescape(t)).strip()


def parse_page(page: str, channel: str) -> tuple[list[dict], int | None]:
    """Return (messages, data-before id for the next older page)."""
    msgs = []
    for part in re.split(r'(?=<div class="tgme_widget_message_wrap)', page)[1:]:
        pid = re.search(rf'data-post="{re.escape(channel)}/(\d+)"', part, re.I)
        if not pid:
            continue
        tm = re.search(r'<time datetime="([^"]+)"', part)
        body = re.search(r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>', part, re.S)
        reply = re.search(r'class="tgme_widget_message_reply"[^>]*href="https?://t\.me/[^/]+/(\d+)"', part)
        ts = None
        if tm:
            try:
                ts = datetime.fromisoformat(tm.group(1)).timestamp()
            except ValueError:
                pass
        msgs.append({"msg_id": int(pid.group(1)), "ts": ts, "text": html_to_text(body.group(1)) if body else "",
                     "reply_to": int(reply.group(1)) if reply else None})
    more = re.search(r'tme_messages_more[^>]*data-before="(\d+)"', page)
    return msgs, int(more.group(1)) if more else None


def classify(msg: dict, src: dict) -> tuple[str, dict]:
    """('call' | 'brag' | 'other', fields). Pure; unit-tested."""
    txt = msg.get("text") or ""
    kws = [k.lower() for k in src.get("call_keywords", [])]
    has_kw = any(k in txt.lower() for k in kws) if kws else False
    sui = SUI_RE.findall(txt)
    evm = [a for a in EVM_RE.findall(txt) if not any(a in s for s in sui)]
    sol = [a for a in SOL_RE.findall(txt) if not re.fullmatch(r"[1-9]+", a)]
    addrs = sui + evm + sol
    tickers = TICKER_RE.findall(txt)
    mults = [float(x) for x in MULT_RE.findall(txt)]
    bkws = [k.lower() for k in src.get("brag_keywords", [])]
    brag_kw = any(k in txt.lower() for k in bkws) if bkws else False
    is_brag = not has_kw and (brag_kw or (bool(mults) and bool(BRAG_WORDS.search(txt))))  # a call keyword wins
    if addrs and (has_kw or (not src.get("require_keyword") and parse_mc(txt))) and not is_brag:
        a = addrs[0]
        return "call", {"contract": a, "chain_guess": infer_chain(a), "ticker": tickers[0] if tickers else None,
                        "mc_at_call": parse_mc(txt), "n_addresses": len(addrs)}
    if is_brag:
        be = BRAG_ENTRY_RE.search(txt)
        entry_mc = None
        if be:
            try:
                entry_mc = float(be.group(1).replace(",", "")) * {"k": 1e3, "m": 1e6, "b": 1e9}.get((be.group(2) or "").lower(), 1)
            except ValueError:
                pass
        return "brag", {"multiple": max(mults) if mults else None, "tickers": tickers[:3],
                        "contract": addrs[0] if addrs else None, "brag_entry_mc": entry_mc}
    return "other", {}


def fetch_source(src: dict, state: dict, max_pages: int | None = None, verbose=False) -> dict:
    ch = src["channel"]
    st = state.setdefault(src["id"], {"newest_id": 0, "oldest_id": None, "backfill_done": False, "pages": 0})
    calls_p, brags_p = paths(src["id"])
    calls = {c["msg_id"]: c for c in _jl_read(calls_p)}
    brags = {b["msg_id"]: b for b in _jl_read(brags_p)}
    stats = {"pages": 0, "new_calls": 0, "new_brags": 0, "errors": []}
    budget = src.get("backfill_pages_per_run", 40) if max_pages is None else max_pages
    max_days = src.get("backfill_max_days")

    def get(before: int | None):
        url = f"https://t.me/s/{ch}" + (f"?before={before}" if before else "")
        r = _http.get_text(url, cache_ttl=900 if before is None else 86400, timeout=20)
        stats["pages"] += 1
        if not r.ok:
            stats["errors"].append(f"{r.status}: {(r.error or '')[:100]}")
            return None, None
        return parse_page(r.data or "", ch)

    def absorb(msgs):
        for m in msgs:
            if m["msg_id"] in calls or m["msg_id"] in brags:
                continue
            kind, f = classify(m, src)
            base = {"source": src["id"], "msg_id": m["msg_id"], "ts": m["ts"], "reply_to": m["reply_to"],
                    "excerpt": (m["text"] or "")[:300]}
            if kind == "call":
                calls[m["msg_id"]] = dict(base, **f)
                stats["new_calls"] += 1
            elif kind == "brag":
                brags[m["msg_id"]] = dict(base, **f)
                stats["new_brags"] += 1

    prev_newest = st.get("newest_id") or 0
    msgs, before = get(None)  # newest page: once per run
    if msgs:
        absorb(msgs)
        st["newest_id"] = max([prev_newest] + [m["msg_id"] for m in msgs])
        if st.get("oldest_id") is None:
            st["oldest_id"] = min(m["msg_id"] for m in msgs)
            st["oldest_ts"] = min((m["ts"] for m in msgs if m["ts"]), default=None)
        # gap fill: more than one page arrived since the last run -> walk back until we reach known ids
        walk, page_min = before, min(m["msg_id"] for m in msgs)
        while prev_newest and walk and page_min > prev_newest + 1 and budget > 0:
            gm, walk = get(walk)
            budget -= 1
            if not gm:
                break
            absorb(gm)
            page_min = min(m["msg_id"] for m in gm)
    cursor = st.get("oldest_id")
    while budget > 0 and not st.get("backfill_done") and cursor and cursor > 1:
        msgs, nxt = get(cursor)
        budget -= 1
        if msgs is None:
            break
        if not msgs:
            st["backfill_done"] = True
            break
        absorb(msgs)
        oldest = min(m["msg_id"] for m in msgs)
        st["oldest_id"] = min(oldest, cursor)
        st["oldest_ts"] = min((m["ts"] for m in msgs if m["ts"]), default=st.get("oldest_ts"))
        if max_days and st.get("oldest_ts") and st["oldest_ts"] < time.time() - max_days * 86400:
            st["backfill_done"] = True
        if not nxt or nxt >= cursor:
            st["backfill_done"] = True
            break
        cursor = nxt
    st["pages"] = st.get("pages", 0) + stats["pages"]
    link_brags(calls, brags)
    _jl_write(calls_p, sorted(calls.values(), key=lambda c: c["msg_id"]))
    _jl_write(brags_p, sorted(brags.values(), key=lambda b: b["msg_id"]))
    stats.update(total_calls=len(calls), total_brags=len(brags), oldest_id=st.get("oldest_id"),
                 oldest_ts=st.get("oldest_ts"), backfill_done=st.get("backfill_done"))
    return stats


def link_brags(calls: dict, brags: dict) -> None:
    """Attach each brag to the call it refers to: reply-to id, else contract, else the latest earlier call with the ticker."""
    by_contract, by_ticker = {}, {}
    for c in sorted(calls.values(), key=lambda c: c["msg_id"]):
        by_contract.setdefault(c["contract"], []).append(c)
        if c.get("ticker"):
            by_ticker.setdefault(c["ticker"].upper(), []).append(c)
    for b in brags.values():
        target = None
        if b.get("reply_to") in calls:
            target, how = calls[b["reply_to"]], "reply"
        elif b.get("contract") in by_contract:
            target, how = by_contract[b["contract"]][0], "contract"
        else:
            for t in b.get("tickers") or []:
                earlier = [c for c in by_ticker.get(t.upper(), []) if c["msg_id"] < b["msg_id"]
                           and (b.get("ts") or 0) - (c.get("ts") or 0) < 7 * 86400]  # tickers get reused
                if earlier:
                    target, how = earlier[-1], "ticker"
                    break
        if target:
            b.update(call_msg_id=target["msg_id"], call_contract=target["contract"], link=how)


def fetch_all(max_pages: int | None = None, verbose=False) -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        state = json.load(open(STATE_PATH))
    except (OSError, ValueError):
        state = {}
    out = {}
    for src in load_config():
        if src.get("type") != "telegram":
            continue
        try:
            out[src["id"]] = fetch_source(src, state, max_pages, verbose)
        except Exception as exc:  # a broken page never breaks the tracker run
            out[src["id"]] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    tmp = STATE_PATH + ".tmp"
    json.dump(state, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE_PATH)
    return out


def load_calls(src_id: str) -> list[dict]:
    return _jl_read(paths(src_id)[0])


def load_brags(src_id: str) -> list[dict]:
    return _jl_read(paths(src_id)[1])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "show"])
    ap.add_argument("--pages", type=int, default=None)
    a = ap.parse_args()
    if a.cmd == "fetch":
        print(json.dumps(fetch_all(a.pages), indent=1, default=str))
    else:
        for src in load_config():
            cs, bs = load_calls(src["id"]), load_brags(src["id"])
            print(src["id"], "calls", len(cs), "brags", len(bs),
                  "oldest", datetime.fromtimestamp(min((c["ts"] for c in cs if c["ts"]), default=time.time())).strftime("%Y-%m-%d %H:%M"))
