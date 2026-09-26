#!/usr/bin/env python3
"""receipts.py: compact per-record "change receipts" for the scanner (harness-engineering idea: every run leaves a
small, append-only record of what it looked at, what failed, what it decided and with which scorer versions).

One JSON line per scan record that reached deep checks (and therefore every move/watchlist card) is appended to
data/receipts.jsonl. The receipt id equals the shadow_tracker entry id for the same record
(sha1("<chain>:<address>:<scan ts>")[:14]), so shadow_tracker.py can attach the eventual outcome to it
(data/receipt_outcomes.json, written only by the tracker). Read-only research data; nothing here trades.
Usage: python3 receipts.py show <receipt_id>   |   python3 receipts.py tail [N]
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths as _paths  # noqa: E402
DATA_DIR = _paths.DATA_DIR
RECEIPTS = os.path.join(DATA_DIR, "receipts.jsonl")
OUTCOMES = os.path.join(DATA_DIR, "receipt_outcomes.json")
ET = ZoneInfo("America/New_York")
FACTOR_KEYS = ("S1", "S2", "S3", "S4", "C1", "E", "C2", "C3", "C5")


def scan_ts(generated_et: str) -> float | None:
    """Same parse as shadow_tracker.parse_scan_ts for 'YYYY-MM-DD HH:MM:SS ET'."""
    try:
        return datetime.strptime(generated_et.replace(" ET", "").strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET).timestamp()
    except (AttributeError, ValueError):
        return None


def receipt_id(chain: str, addr: str, ts: float) -> str:
    return hashlib.sha1(f"{(chain or 'solana').lower()}:{addr}:{ts}".encode()).hexdigest()[:14]


def reached_deep(rec: dict) -> bool:
    return bool(rec.get("rugcheck") or rec.get("security") or rec.get("scores"))


def _src_status(rec: dict) -> tuple[list, list]:
    """Per-record data sources: (used_ok, failed) with short names."""
    ok, bad = [], []

    def put(name, good, why=""):
        (ok if good else bad).append(name if good else f"{name}:{str(why)[:40]}")
    if "ds_available" in rec:
        put("dexscreener", rec.get("ds_available"), "unavailable (rate-limited?)")
    if rec.get("pair_source"):
        ok.append(str(rec["pair_source"])[:24])
    for key, name in (("rugcheck", "rugcheck"), ("goplus", "goplus"), ("jupiter", "jupiter-quote")):
        v = rec.get(key)
        if isinstance(v, dict):
            put(name, v.get("ok"), v.get("error") or "")
    if isinstance(rec.get("security"), dict):
        errs = rec["security"].get("errors") or []
        put("security(goplus/honeypot)", not errs, "; ".join(map(str, errs))[:40])
    pw = rec.get("p_win") or {}
    if pw:
        put("candles:" + str(pw.get("candle_source") or "none"), bool(pw.get("candles")), "no candles")
    for key, name in (("smart_money", "smart-money"), ("social", "social")):
        v = rec.get(key)
        if isinstance(v, dict):
            put(name, v.get("status") == "ok", v.get("status") or "unknown")
    return ok, bad


def build(rec: dict, res: dict, card_files: list[str], versions: dict) -> dict:
    ts = scan_ts(res.get("generated_et", ""))
    chain = (rec.get("chain") or "solana").lower()
    sc, pw, nb = rec.get("scores") or {}, rec.get("p_win") or {}, rec.get("narrow") or {}
    ok, bad = _src_status(rec)
    checks = rec.get("checks") or []
    fails = [str(c.get("id"))[:48] for c in checks if c.get("pass") is not True]
    status = rec.get("status")
    run_fail = sorted({str(s.get("source"))[:40] for s in res.get("sources") or [] if not s.get("ok")})
    limited = sorted(k for k, v in (res.get("limiter") or {}).items() if (v or {}).get("cooldown_s", 0) > 0)
    return {"rid": rec.get("receipt_id") or receipt_id(chain, rec.get("mint") or rec.get("address"), ts),
            "t": res.get("generated_et"), "chain": chain, "token": {"sym": rec.get("symbol"), "addr": rec.get("mint") or rec.get("address")},
            "src_ok": ok, "src_fail": bad, "run_src_fail": run_fail[:8], "rate_limited": limited,
            "hard": {"n": len(checks), "fail": fails[:8]},
            "factors": {k: sc.get(k) for k in FACTOR_KEYS if k in sc} or None,
            "composite": sc.get("composite"), "conf": sc.get("decision_confidence"), "p_win": pw.get("estimate"),
            "narrow_p": nb.get("p"), "narrow_answered": nb.get("answered"),
            "decision": status, "why": (rec.get("reason") or "; ".join((rec.get("needs") or [])[:2]))[:160] or None,
            "card": card_files if status in ("PASS", "WATCH") else None, "ver": versions}


def append(receipts: list[dict], path: str | None = None) -> int:
    path = path or RECEIPTS
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        for r in receipts:
            f.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)
    return len(receipts)


def load(path: str | None = None) -> list[dict]:
    out = []
    try:
        with open(path or RECEIPTS, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def main(argv=None) -> int:
    a = (argv if argv is not None else sys.argv[1:]) or ["tail"]
    if a[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    outs = {}
    try:
        outs = json.load(open(OUTCOMES))
    except (OSError, ValueError):
        pass
    rs = load()
    if a[0] == "show" and len(a) > 1:
        for r in rs:
            if r.get("rid") == a[1]:
                print(json.dumps(dict(r, outcome=outs.get(a[1])), indent=1))
        return 0
    for r in rs[-(int(a[1]) if len(a) > 1 else 10):]:
        o = outs.get(r["rid"]) or {}
        print(f"{r['rid']} {r['t']} {r['chain']} {r['token']['sym']} {r['decision']} comp={r.get('composite')} "
              f"p_win={r.get('p_win')} narrow={r.get('narrow_p')} outcome={o.get('outcome', 'open')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
