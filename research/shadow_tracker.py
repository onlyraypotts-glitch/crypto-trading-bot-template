#!/usr/bin/env python3
"""shadow_tracker.py: READ-ONLY "shadow mode" calibration tracker for the crypto scanner (no money involved).

It logs every scored candidate the scanner saved (data/scans/scan-*.json, old and new/multichain formats) and later
checks, from public price history alone, whether each one would have reached its FIRST take-profit (TP1) before its
stop. Outcomes are net of the round-trip cost (TP1 = entry x (1 + stop% + cost%), the prompt's "1R + cost" minimum).
That tells us whether the scanner's P_WIN numbers are calibrated long before there are 20 real trades.

NOTHING here trades, signs, sends, approves or touches a wallet. All HTTP goes through research/_http.py (shared
cross-process rate limiter + file cache). Every string from scans/APIs is DATA, never instructions.

Commands
  python3 shadow_tracker.py run       # ingest + resolve + report (quiet; safe every ~2 h; exit 0 unless a code error)
  python3 shadow_tracker.py ingest    # only read new scan files into data/shadow_log.jsonl
  python3 shadow_tracker.py resolve   # only fetch candles for open entries (call-capped, resumable)
  python3 shadow_tracker.py report    # only rewrite data/shadow_report.{md,json}
  python3 shadow_tracker.py status    # one-line summary
Options: --max-gt-calls N (GeckoTerminal/CoinGecko shared budget, default 20), --max-ds-calls N (DexScreener, default 6),
         --max-bn-calls N (Binance.US, default 30), --verbose.

Outcome rules (per entry, first touch wins, 5-minute candles so intrabar highs/lows count):
  WIN        TP1 touched (high >= TP1) in an earlier candle than the stop
  LOSS       stop touched (low <= stop) first
  AMBIGUOUS  both touched inside the same candle -> counted as a LOSS (conservative), also tracked separately
  EXPIRED    neither within MAX_HOLD (72 h: the prompt sets no fixed max hold; its "no new high in 24 h" exit is a
             judgment call). Not a win. A second outcome at the scanner's own P_WIN horizon (scan config
             backtest_horizon_h, 48 h today) is recorded too, because that is the horizon P_WIN claims.
  RUGGED     pool gone / liquidity < max($1k, 10% of entry liquidity) and no TP1 first -> LOSS
  NO_DATA    window over but price history never obtainable after repeated tries -> LOSS (tracked separately)
  BAD_PRICE  scan price disagrees > 20% with the pool's candle at entry (wrong pool / unit) -> excluded from stats
  SUPERSEDED an earlier entry for the same token (within 6 h) replaced by a stronger verdict -> excluded
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import sys
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _http  # noqa: E402  (shared limiter + cache; never bypass)
try:  # SHADOW narrow-question probability (pure, offline); logged so it can be graded like P_WIN
    import narrow_questions as nq  # noqa: E402
except Exception:  # pragma: no cover
    nq = None

ET = ZoneInfo("America/New_York")
import paths as _paths  # noqa: E402
SCANS_DIR = _paths.SCANS_DIR
DATA_DIR = _paths.DATA_DIR
LOG_PATH = os.path.join(DATA_DIR, "shadow_log.jsonl")
STATE_PATH = os.path.join(DATA_DIR, "shadow_state.json")
REPORT_MD = os.path.join(DATA_DIR, "shadow_report.md")
REPORT_JSON = os.path.join(DATA_DIR, "shadow_report.json")
RECEIPT_OUTCOMES = os.path.join(DATA_DIR, "receipt_outcomes.json")   # receipt id -> eventual outcome (tracker is the only writer)
LOCK_PATH = os.path.join(DATA_DIR, ".shadow.lock")

DEDUPE_WINDOW_S = 6 * 3600
MAX_HOLD_H = 72.0                 # prompt has no fixed max hold -> task default 72 h
DEFAULT_SCANNER_HORIZON_H = 48.0  # scan config backtest_horizon_h (the P_WIN definition) when a scan lacks it
MIN_AGE_S = 30 * 60               # don't check entries younger than this
RECHECK_S = 4 * 3600              # interim re-check spacing for still-open entries
CANDLE_S = 300                    # 5-minute candles
BAD_PRICE_PCT = 20.0
RUG_ABS_USD = 1000.0
RUG_FRAC = 0.10
NO_DATA_AFTER_FAILS = 3
GT_EXTRA_GAP_S = float(os.environ.get("SHADOW_GT_GAP_S", 6.0))  # extra politeness on top of the shared 6.5 s spacing
SMALL_N = 30
VERDICT_RANK = {"PASS": 4, "WATCH": 3, "REJECT": 2, "NOT_DEEP_CHECKED": 1}
LOSSLIKE = {"LOSS", "AMBIGUOUS", "RUGGED", "NO_DATA"}
EXCLUDED = {"BAD_PRICE", "SUPERSEDED"}
BUCKETS = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.75), (0.75, 0.85), (0.85, 1.0001)]
EVM_CHAINS = {"base", "ethereum", "bsc", "monad", "robinhood"}
DS_CHAIN = {"solana": "solana", "base": "base", "ethereum": "ethereum", "bsc": "bsc", "monad": "monad", "robinhood": "robinhood"}
GT_NET = {"solana": "solana", "base": "base", "ethereum": "eth", "bsc": "bsc", "monad": "monad"}
# mirrored from scan_candidates.py (not imported: that file is being edited by another worker; keep in sync)
BINANCE_US = {"SOL", "JUP", "JTO", "BONK", "WIF", "ORCA", "RENDER", "POPCAT", "MEW", "FARTCOIN", "PENGU", "TRUMP",
              "MOODENG", "PUMP"}
PHANTOM_FEE_PCT, PHANTOM_FIXED_USD, QUOTE_USD = 0.85, 0.004, 100.0
FOMO_EVM_FEE_PCT = 0.50
EVM_GAS_RT_USD = {"base": 0.05, "ethereum": 3.0, "bsc": 0.15, "monad": 0.05, "robinhood": 0.05}  # derived-cost proxy


# ---------------------------------------------------------------- small utils
def fnum(x, default=None):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def et_str(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d %H:%M ET") if ts else "-"


def norm_addr(chain: str, a: str) -> str:
    return (a or "").lower() if chain in EVM_CHAINS or (a or "").startswith("0x") else (a or "")


def load_log() -> list[dict]:
    out = []
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass  # a torn line never kills the run
    return out


def save_log(entries: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = LOG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":"), default=str) + "\n")
    os.replace(tmp, LOG_PATH)


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"ingested": {}, "runs": []}


def save_state(st: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- stop / target / cost rules (prompt + scanner)
def choose_stop(cls: str, vol_d: float | None, pc: dict) -> tuple[float, str]:
    """Same rule as scan_candidates.choose_stop (prompt H5: memecoin stop <= 25%). Without candles the scanner uses
    the |24h| / 2x|6h| move proxy; saved scans carry no candles for rejects, so derived stops always use the proxy."""
    lo, hi = (0.05, 0.15) if cls == "major" else ((0.08, 0.20) if cls == "mid" else (0.12, 0.25))
    if vol_d:
        return round(min(hi, max(lo, 1.5 * vol_d)), 4), f"1.5 x daily vol {vol_d*100:.1f}% clamped [{lo*100:.0f}%,{hi*100:.0f}%]"
    pc = pc or {}
    proxy = max(abs(fnum(pc.get("h24"), 0)), 2 * abs(fnum(pc.get("h6"), 0))) / 100
    return round(min(hi, max(lo, proxy)), 4), f"no candles: |24h| / 2x|6h| proxy clamped [{lo*100:.0f}%,{hi*100:.0f}%]"


def derived_cost_pct(rec: dict, chain: str) -> tuple[float, str]:
    """Round-trip cost % at $100 (prompt §2E.8 / H17). Uses the scan's own measured costs when present."""
    c = rec.get("costs") or {}
    if chain == "solana" and fnum(c.get("phantom_app_pct")):
        return float(c["phantom_app_pct"]), "scan costs (Phantom app, Jupiter quotes)"
    if chain != "solana" and c and not c.get("unknown"):
        opts = [fnum(c.get("fomo_pct_with_1pct_allowance")), fnum(c.get("phantom_app_pct")), fnum(c.get("phantom_mcp_pct"))]
        opts = [x for x in opts if x]
        if opts:
            return min(opts), "scan costs (cheapest venue)"
    liq = fnum(rec.get("liquidity_usd"), 0) or 0
    exp_slip = 0.1 if liq >= 250_000 else 0.25
    impact_leg = QUOTE_USD / (liq / 2 + QUOTE_USD) * 100 if liq > 0 else 3.0
    jr = rec.get("jupiter") or {}
    if chain == "solana":
        pool_impact = fnum(jr.get("pct_pool_and_impact")) if jr.get("ok") else None
        if pool_impact is None:
            fee_leg = 0.05 if rec.get("cls") == "major" else 0.25
            pool_impact = 2 * fee_leg + 2 * impact_leg
        return round(pool_impact + 2 * exp_slip + 2 * PHANTOM_FEE_PCT + PHANTOM_FIXED_USD / QUOTE_USD * 100, 3), \
            "derived: Phantom app 2x0.85% + pool fees/impact + expected slippage (prompt §2E.8)"
    gas = EVM_GAS_RT_USD.get(chain, 0.1) / QUOTE_USD * 100
    return round(2 * 0.3 + 2 * impact_leg + exp_slip + 1.0 + 2 * FOMO_EVM_FEE_PCT + gas, 3), \
        "derived: FOMO 2x0.5% + 0.3% pool fee/leg + impact + 1% slippage allowance + gas proxy (§2E.8)"


FACTOR_KEYS_REC = ["cls", "c2", "c3", "t9_wash_p", "organic", "organic_score", "liquidity_usd", "volume_24h", "market_cap",
                   "pool_age_h", "holders_jup", "holder_change_1h", "holder_change_24h", "liq_change_1h", "boosted",
                   "established", "launchpad", "jup_verified", "t7_state", "address_verified", "price_mismatch_pct"]


def factor_readings(rec: dict) -> dict:
    f = {k: rec.get(k) for k in FACTOR_KEYS_REC if rec.get(k) is not None}
    pc = rec.get("price_change") or {}
    for k in ("m5", "h1", "h6", "h24"):
        if fnum(pc.get(k)) is not None:
            f[f"chg_{k}"] = fnum(pc.get(k))
    ja = rec.get("jup_audit") or {}
    if fnum(ja.get("topHoldersPercentage")) is not None:
        f["top_holders_pct"] = fnum(ja.get("topHoldersPercentage"))
    rc = rec.get("rugcheck") or {}
    for k in ("top10_nonpool_pct", "cluster_pct", "holders", "lp_locked_pct"):
        if fnum(rc.get(k)) is not None:
            f[f"rc_{k}"] = fnum(rc.get(k))
    sc = rec.get("scores") or {}
    for k in ("S1", "S2", "S3", "S4", "C1", "E", "C2", "C3", "C5", "decision_confidence", "composite"):
        if sc.get(k) is not None:
            f[f"score_{k}"] = sc.get(k)
    if (rec.get("smart_money") or {}).get("level") is not None:
        f["smart_money_level"] = rec["smart_money"]["level"]
    if (rec.get("social") or {}).get("level") is not None:
        f["social_level"] = rec["social"]["level"]
    fails = rec.get("all_failures") or ([rec["reason"]] if rec.get("reason") else [])
    f["n_failures"] = len(fails)
    f["failed_checks"] = sorted({str(c.get("id"))[:40] for c in (rec.get("checks") or []) if c.get("pass") is False})[:12]
    return f


# ---------------------------------------------------------------- ingest
def parse_scan_ts(d: dict, path: str) -> float | None:
    for k in ("generated_et", "started_et"):
        s = d.get(k)
        if isinstance(s, str):
            try:
                return datetime.strptime(s.replace(" ET", "").strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET).timestamp()
            except ValueError:
                pass
    for k in ("generated_utc", "generated_at", "ts"):
        s = d.get(k)
        if isinstance(s, (int, float)):
            return float(s)
        if isinstance(s, str):
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    base = os.path.basename(path)  # scan-YYYY-MM-DD-HHMM.json
    try:
        return datetime.strptime(base[5:20], "%Y-%m-%d-%H%M").replace(tzinfo=ET).timestamp()
    except ValueError:
        return None


def iter_records(d: dict):
    """Yield candidate records from old (Solana-only) and new (multichain) scan layouts."""
    seen = set()

    def emit(lst, default_status, chain=None):
        for r in lst or []:
            if isinstance(r, dict) and id(r) not in seen:
                seen.add(id(r))
                if chain and not r.get("chain"):
                    r = dict(r, chain=chain)
                yield r, default_status
    for key, st in (("passes", "PASS"), ("watch", "WATCH"), ("rejects", "REJECT"), ("results", None)):
        yield from emit(d.get(key), st)
    chains = d.get("chains") or d.get("per_chain")  # tolerate a future per-chain nesting
    if isinstance(chains, dict):
        for c, sub in chains.items():
            if isinstance(sub, dict):
                for key, st in (("passes", "PASS"), ("watch", "WATCH"), ("rejects", "REJECT")):
                    yield from emit(sub.get(key), st, chain=c)


def norm_verdict(raw: str | None, default: str | None) -> str:
    s = (raw or default or "REJECT").upper().replace("-", "_").replace(" ", "_")
    if s.startswith("NOT_DEEP"):
        return "NOT_DEEP_CHECKED"
    return s if s in VERDICT_RANK else ("REJECT" if "REJ" in s or "SKIP" in s else s)


def build_entry(rec: dict, default_status: str | None, scan: dict) -> tuple[dict | None, str]:
    chain = (rec.get("chain") or "solana").lower()
    addr = rec.get("mint") or rec.get("address") or rec.get("contract") or rec.get("token_address")
    if not addr:
        return None, "no address"
    plan, pw, sc = rec.get("plan") or {}, rec.get("p_win") or {}, rec.get("scores") or {}
    price = fnum(plan.get("reference_price")) or fnum(rec.get("price_usd")) or fnum(rec.get("price"))
    if not price or price <= 0:
        return None, "no price"
    verdict = norm_verdict(rec.get("status"), default_status)
    horizon = fnum((scan.get("config") or {}).get("backtest_horizon_h"), DEFAULT_SCANNER_HORIZON_H)
    e = {"id": hashlib.sha1(f"{chain}:{addr}:{scan['ts']}".encode()).hexdigest()[:14],
         "scan_file": scan["file"], "entry_ts": scan["ts"], "entry_et": et_str(scan["ts"]),
         "chain": chain, "address": addr, "symbol": rec.get("symbol"), "name": rec.get("name"),
         "verdict": verdict, "verdict_raw": rec.get("status"), "reason": (rec.get("reason") or "")[:200],
         "entry_price": price, "pair": rec.get("pair"), "dex": rec.get("dex"), "cls": rec.get("cls"),
         "major_symbol": rec.get("major_symbol"), "liquidity_usd_entry": fnum(rec.get("liquidity_usd")),
         "composite": fnum(sc.get("composite")), "decision_confidence": fnum(sc.get("decision_confidence")),
         "max_hold_h": MAX_HOLD_H, "scanner_horizon_h": horizon}
    stop, tp1 = fnum(plan.get("stop")), fnum(plan.get("tp1"))
    if stop and tp1 and fnum(pw.get("stop_pct")) and fnum(pw.get("tp1_pct")):
        e.update(stop=stop, tp1=tp1, stop_pct=float(pw["stop_pct"]), tp1_pct=float(pw["tp1_pct"]),
                 cost_pct=round(float(pw["tp1_pct"]) - float(pw["stop_pct"]), 3), levels_source="scanner",
                 cost_source="scanner (TP1 - stop = round-trip cost)", stop_method=pw.get("stop_method"))
    elif fnum((rec.get("shadow") or {}).get("stop_pct")) and fnum((rec.get("shadow") or {}).get("tp1_pct")):
        sh = rec["shadow"]  # optional scanner hook (SHADOW_WIRING.md): levels computed by the scanner for rejects
        b, a = float(sh["stop_pct"]) / 100, float(sh["tp1_pct"]) / 100
        e.update(stop=price * (1 - b), tp1=price * (1 + a), stop_pct=round(b * 100, 2), tp1_pct=round(a * 100, 2),
                 cost_pct=round(fnum(sh.get("cost_pct"), (a - b) * 100), 3), levels_source="scanner-shadow",
                 cost_source="scanner shadow block", stop_method=sh.get("stop_method"))
    else:
        b, note = choose_stop(rec.get("cls") or "memecoin", None, rec.get("price_change") or {})
        cost, cnote = derived_cost_pct(rec, chain)
        a = b + cost / 100
        e.update(stop=price * (1 - b), tp1=price * (1 + a), stop_pct=round(b * 100, 2), tp1_pct=round(a * 100, 2),
                 cost_pct=round(cost, 3), levels_source="derived", cost_source=cnote, stop_method=note)
    if fnum(pw.get("estimate")) is not None:
        post = pw.get("posterior") or {}
        e.update(p=float(pw["estimate"]), p_source="scanner", p_lo90=fnum(post.get("lo90")), p_hi90=fnum(post.get("hi90")),
                 p_backtest=pw.get("backtest"))
    else:  # rejects carry no P_WIN: log the scanner's own martingale prior b/(a+b) for the same stop/TP1, marked derived
        b, a = e["stop_pct"] / 100, e["tp1_pct"] / 100
        e.update(p=round(b / (a + b), 4), p_source="derived_prior")
    e["factors"] = factor_readings(rec)
    nb = rec.get("narrow") if isinstance(rec.get("narrow"), dict) and rec["narrow"].get("p") is not None else None
    if nb is None and nq is not None:  # older scans: answer the same questions from the saved record (no TA features)
        try:
            nb = dict(nq.narrow_block(rec), computed_at="ingest")
        except Exception:  # noqa: BLE001
            nb = None
    if nb:
        e["narrow"] = {k: nb.get(k) for k in ("p", "model_version", "answered", "features", "computed_at") if nb.get(k) is not None}
        e["narrow_p"] = nb.get("p")
    if rec.get("receipt_id"):
        e["receipt_id"] = rec["receipt_id"]
    e.update(status="OPEN", outcome=None, outcome_scanner_h=None, checks=0, fails=0, last_check_ts=None, dup_count=0)
    return e, "ok"


def scan_files() -> list[str]:
    out, seen = [], set()
    if not os.path.isdir(SCANS_DIR):
        return out
    for fn in sorted(os.listdir(SCANS_DIR)):
        if fn.endswith(".json") and (fn.startswith("scan-") or fn.startswith("latest")):
            rp = os.path.realpath(os.path.join(SCANS_DIR, fn))
            if rp not in seen and os.path.isfile(rp):
                seen.add(rp)
                out.append(rp)
    return out


def ingest(entries: list[dict], state: dict, verbose=False) -> dict:
    stats = {"files": 0, "added": 0, "dupes": 0, "superseded": 0, "skipped_no_price": 0, "skipped_other": 0, "bad_files": 0}
    idx: dict[tuple, list[dict]] = {}
    for e in entries:
        idx.setdefault((e["chain"], norm_addr(e["chain"], e["address"])), []).append(e)
    todo = []
    for path in scan_files():
        st = os.stat(path)
        key = os.path.basename(path)
        sig = f"{st.st_size}:{int(st.st_mtime)}"
        if state["ingested"].get(key, {}).get("sig") == sig:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError) as exc:
            stats["bad_files"] += 1
            state["ingested"][key] = {"sig": sig, "error": str(exc)[:120]}
            continue
        ts = parse_scan_ts(d, path)
        if not ts:
            stats["bad_files"] += 1
            continue
        todo.append((ts, key, sig, d))
    for ts, key, sig, d in sorted(todo, key=lambda x: x[0]):
        stats["files"] += 1
        scan = {"ts": ts, "file": key, "config": d.get("config") or {}}
        n_file = 0
        for rec, dflt in iter_records(d):
            e, why = build_entry(rec, dflt, scan)
            if e is None:
                stats["skipped_no_price" if why == "no price" else "skipped_other"] += 1
                continue
            k = (e["chain"], norm_addr(e["chain"], e["address"]))
            near = [o for o in idx.get(k, []) if o["status"] != "SUPERSEDED" and abs(o["entry_ts"] - e["entry_ts"]) < DEDUPE_WINDOW_S]
            if near:
                o = near[0]
                if VERDICT_RANK.get(e["verdict"], 0) > VERDICT_RANK.get(o["verdict"], 0) and o["status"] == "OPEN" and not o["checks"]:
                    o.update(status="SUPERSEDED", outcome="SUPERSEDED", superseded_by=e["id"])
                    stats["superseded"] += 1
                else:
                    o["dup_count"] = o.get("dup_count", 0) + 1
                    o.setdefault("dup_scans", []).append(key)
                    if e.get("receipt_id"):  # the duplicate's receipt inherits this entry's outcome
                        o.setdefault("dup_receipt_ids", []).append(e["receipt_id"])
                    stats["dupes"] += 1
                    continue
            entries.append(e)
            idx.setdefault(k, []).append(e)
            stats["added"] += 1
            n_file += 1
        state["ingested"][key] = {"sig": sig, "scan_ts": ts, "added": n_file, "at": time.time()}
    return stats


SOURCE_DEFAULT_COST_PCT = 3.0  # liquidity at call time unknown -> assume the H17 cap (the most a bot trade may cost)


def ingest_sources(entries: list[dict], fetch: bool = True) -> dict:
    """Log calls from watched sources (data/watched_sources.json) as shadow entries (source = e.g. tg:<channel>).
    Entry price is set later from the candle at the post time (entry_mode = at_post)."""
    try:
        import telegram_calls as tgc
    except Exception as exc:  # optional module
        return {"error": f"telegram_calls unavailable: {exc}"}
    out = {"fetch": tgc.fetch_all() if fetch else "skipped"}
    have = {e["id"] for e in entries}
    for e in entries:  # repeat posts folded into an earlier call are remembered by message id
        for mid in e.get("repeat_msg_ids") or []:
            have.add(hashlib.sha1(f"{e.get('source')}:{mid}".encode()).hexdigest()[:14])
    idx: dict[tuple, list[dict]] = {}
    for e in entries:
        if e.get("source", "scanner") != "scanner":
            idx.setdefault((e["source"], norm_addr(e["chain"], e["address"])), []).append(e)
    for src in tgc.load_config():
        sid = src["id"]
        st = {"calls_seen": 0, "added": 0, "repeats": 0, "skipped": 0}
        dedupe = float(src.get("dedupe_h", 24)) * 3600
        b = float(src.get("stop_pct", 25.0)) / 100
        cost = float(src.get("cost_pct", SOURCE_DEFAULT_COST_PCT))
        for c in sorted(tgc.load_calls(sid), key=lambda c: (c.get("ts") or 0, c["msg_id"])):
            st["calls_seen"] += 1
            eid = hashlib.sha1(f"{sid}:{c['msg_id']}".encode()).hexdigest()[:14]
            if eid in have:
                continue
            chain = c.get("chain_guess") or "solana"
            if not c.get("ts") or chain not in ("solana", "evm"):
                st["skipped"] += 1
                continue
            k = (sid, norm_addr(chain, c["contract"]))
            near = [o for o in idx.get(k, []) if 0 <= c["ts"] - o["entry_ts"] < dedupe]
            if near:
                near[0]["repeat_count"] = near[0].get("repeat_count", 0) + 1
                near[0].setdefault("repeat_msg_ids", []).append(c["msg_id"])
                st["repeats"] += 1
                have.add(eid)
                continue
            a = b + cost / 100
            e = {"id": eid, "source": sid, "scan_file": None, "msg_id": c["msg_id"], "entry_ts": c["ts"], "entry_et": et_str(c["ts"]),
                 "chain": chain, "address": c["contract"], "symbol": c.get("ticker"), "name": None, "verdict": "CALL",
                 "verdict_raw": "call", "reason": "", "entry_price": None, "entry_mode": "at_post", "mc_at_call": c.get("mc_at_call"),
                 "pair": None, "dex": None, "cls": "memecoin", "major_symbol": None, "liquidity_usd_entry": None,
                 "composite": None, "decision_confidence": None, "max_hold_h": float(src.get("max_hold_h", MAX_HOLD_H)),
                 "scanner_horizon_h": 24.0, "min_age_h": float(src.get("min_age_h", 24)),
                 "stop": None, "tp1": None, "stop_pct": round(b * 100, 2), "tp1_pct": round(a * 100, 2), "cost_pct": cost,
                 "levels_source": "derived", "cost_source": f"derived: {cost}% (H17 cap; liquidity at call unknown)",
                 "stop_method": f"fixed {b*100:.0f}% memecoin stop (prompt H5 max)", "p": round(b / (a + b), 4), "p_source": "derived_prior",
                 "pump_suffix": c["contract"].endswith("pump"),
                 "factors": {"mc_at_call": c.get("mc_at_call"), "pump_suffix": c["contract"].endswith("pump")},
                 "status": "OPEN", "outcome": None, "outcome_scanner_h": None, "checks": 0, "fails": 0, "last_check_ts": None, "dup_count": 0}
            if nq is not None:  # only at-call facts (market cap, address): most narrow answers are unknown for calls
                try:
                    fe = nq.answer(nq.view_from_entry(e))
                    e["narrow"] = {"p": round(nq.predict(fe), 4), "model_version": nq.load_model()["version"],
                                   "answered": f"{nq.n_known(fe)}/{len(nq.QUESTIONS)}", "features": {k: v for k, v in fe.items() if v is not None}}
                    e["narrow_p"] = e["narrow"]["p"]
                except Exception:  # noqa: BLE001
                    pass
            entries.append(e)
            have.add(eid)
            idx.setdefault(k, []).append(e)
            st["added"] += 1
        out[sid] = st
    return out


# ---------------------------------------------------------------- price history
class Budget:
    def __init__(self, gt: int, ds: int, bn: int):
        self.left = {"gt": gt, "ds": ds, "bn": bn}
        self.used = {"gt": 0, "ds": 0, "bn": 0}
        self.blocked: dict[str, str] = {}
        self.cache_hits = 0

    def take(self, k: str) -> bool:
        if k in self.blocked or self.left[k] <= 0:
            return False
        self.left[k] -= 1
        self.used[k] += 1
        return True

    def result(self, k: str, r) -> None:
        if r.cached:  # a cache hit costs no provider budget
            self.left[k] += 1
            self.used[k] -= 1
            self.cache_hits += 1
        if not r.ok and r.status == 429:
            self.blocked[k] = (r.error or "rate limited")[:140]


def ds_pairs_batch(chain: str, addrs: list[str], bud: Budget) -> dict[str, dict] | None:
    """DexScreener tokens/v1 (<=30 addresses per call): best pool + liquidity per token. None = unavailable."""
    dc = DS_CHAIN.get(chain)
    if not dc:
        return None
    out: dict[str, dict] = {}
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i + 30]
        if not bud.take("ds"):
            return out or None
        r = _http.get_json(f"https://api.dexscreener.com/tokens/v1/{dc}/{','.join(chunk)}", cache_ttl=900)
        bud.result("ds", r)
        if not r.ok or not isinstance(r.data, list):
            return out or None
        for want in chunk:
            w = norm_addr(chain, want)
            ps = [p for p in r.data if isinstance(p, dict) and norm_addr(chain, (p.get("baseToken") or {}).get("address")) == w]
            best = max(ps, key=lambda p: fnum((p.get("liquidity") or {}).get("usd"), 0) or 0) if ps else None
            out[want] = {"found": bool(ps), "pools": {p.get("pairAddress"): fnum((p.get("liquidity") or {}).get("usd"), 0) for p in ps},
                         "pool": (best or {}).get("pairAddress"), "liq": sum(fnum((p.get("liquidity") or {}).get("usd"), 0) or 0 for p in ps),
                         "price": fnum((best or {}).get("priceUsd")), "src": "dexscreener",
                         "mc": fnum((best or {}).get("marketCap")) or fnum((best or {}).get("fdv")),
                         "dexes": sorted({str(p.get("dexId")) for p in ps}), "best_dex": (best or {}).get("dexId")}
    return out


def gt_pools_batch(chain: str, addrs: list[str], bud: Budget) -> dict[str, dict]:
    """GeckoTerminal tokens/multi (<=30 per call) with top pools: fallback pool discovery + reserve (liquidity)."""
    net = GT_NET.get(chain)
    out: dict[str, dict] = {}
    if not net:
        return out
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i + 30]
        if not bud.take("gt"):
            break
        _gt_gap()
        r = _http.get_json(f"https://api.geckoterminal.com/api/v2/networks/{net}/tokens/multi/{','.join(chunk)}?include=top_pools",
                           cache_ttl=1800)
        bud.result("gt", r)
        if not r.ok or not isinstance(r.data, dict):
            break
        pools = {}
        for inc in r.data.get("included") or []:
            at = inc.get("attributes") or {}
            pools[inc.get("id")] = (at.get("address"), fnum(at.get("reserve_in_usd"), 0))
        for t in r.data.get("data") or []:
            at = t.get("attributes") or {}
            a = at.get("address")
            tops = [pools.get(x.get("id")) for x in ((t.get("relationships") or {}).get("top_pools") or {}).get("data") or []]
            tops = [x for x in tops if x and x[0]]
            match = next((w for w in chunk if norm_addr(chain, w) == norm_addr(chain, a)), a)
            out[match] = {"found": bool(tops), "pool": tops[0][0] if tops else None, "liq": sum(x[1] or 0 for x in tops),
                          "pools": {x[0]: x[1] for x in tops}, "src": "geckoterminal"}
    return out


_LAST_GT = [0.0]


def _gt_gap() -> None:
    """GeckoTerminal shares CoinGecko's per-IP budget with every other tool on the box: keep our own calls sparse."""
    wait = _LAST_GT[0] + GT_EXTRA_GAP_S - time.time()
    if wait > 0 and _LAST_GT[0]:
        time.sleep(min(wait, GT_EXTRA_GAP_S))
    _LAST_GT[0] = time.time()


def gt_candles(chain: str, pool: str, token: str, since: float, until: float, bud: Budget):
    """5-minute candles [(ts,o,h,l,c)] ascending for [since - 3 candles, until], one call (limit <= 1000)."""
    net = GT_NET.get(chain)
    if not net or not pool:
        return None, "no GT network/pool"
    if not bud.take("gt"):
        return None, "gt budget/blocked"
    _gt_gap()
    limit = int(min(1000, math.ceil((until - since) / CANDLE_S) + 4))
    before = int(until)
    final = until < time.time() - 600
    u = (f"https://api.geckoterminal.com/api/v2/networks/{net}/pools/{pool}/ohlcv/minute?aggregate=5&limit={limit}"
         f"&before_timestamp={before}&currency=usd&token={token}")
    r = _http.get_json(u, cache_ttl=86400 if final else 900)
    bud.result("gt", r)
    if not r.ok:
        return None, f"GT {r.status}: {(r.error or '')[:90]}"
    try:
        lst = r.data["data"]["attributes"]["ohlcv_list"] or []
        return sorted((int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in lst), "geckoterminal 5m"
    except (KeyError, TypeError, ValueError, IndexError):
        return None, "GT unexpected payload"


def bn_candles(sym: str, since: float, until: float, bud: Budget):
    if not bud.take("bn"):
        return None, "binance budget/blocked"
    start = int(since - 3 * CANDLE_S) * 1000
    limit = int(min(1000, math.ceil((until - since) / CANDLE_S) + 4))
    final = until < time.time() - 600
    r = _http.get_json(f"https://api.binance.us/api/v3/klines?symbol={sym}USDT&interval=5m&startTime={start}&limit={limit}",
                       cache_ttl=86400 if final else 900)
    bud.result("bn", r)
    if not r.ok or not isinstance(r.data, list):
        return None, f"binance.us {r.status}: {(r.error or '')[:90]}"
    rows = [k for k in r.data if int(k[0]) // 1000 <= until]
    if not stale_market([(float(k[2]), float(k[3]), float(k[5])) for k in rows]):
        return [(int(k[0]) // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in rows], "binance.us 5m"
    return None, "binance.us stale market (no trades): use the DEX pool instead"


def stale_market(hlv: list[tuple]) -> bool:
    """True when a CEX market barely trades (Binance.US lists some memecoins with flat, zero-volume candles)."""
    if len(hlv) < 6:
        return False
    moving = sum(1 for h, l, v in hlv if h > l and v > 0)
    return moving / len(hlv) < 0.3


# ---------------------------------------------------------------- outcome logic (pure; unit-tested)
def evaluate(candles: list, entry_ts: float, entry_price: float, stop: float, tp1: float, horizon_s: float,
             data_until: float) -> dict:
    """First-touch evaluation. Candles opening before entry_ts are skipped (they include pre-entry prices)."""
    pre = [c for c in candles if c[0] < entry_ts]
    post = [c for c in candles if entry_ts <= c[0] < entry_ts + horizon_s]
    ref = pre[-1][4] if pre else (post[0][1] if post else None)
    mism = round((ref / entry_price - 1) * 100, 2) if ref and entry_price else None
    hi = max((c[2] for c in post), default=None)
    lo = min((c[3] for c in post), default=None)
    res = {"n_candles": len(post), "max_high": hi, "min_low": lo, "ref_price": ref, "entry_mismatch_pct": mism,
           "last_close": post[-1][4] if post else None, "at_ts": None}
    for c in post:
        hs, ht = c[3] <= stop, c[2] >= tp1
        if hs and ht:
            return dict(res, outcome="AMBIGUOUS", at_ts=c[0])
        if hs:
            return dict(res, outcome="LOSS", at_ts=c[0])
        if ht:
            return dict(res, outcome="WIN", at_ts=c[0])
    if data_until >= entry_ts + horizon_s:
        return dict(res, outcome="EXPIRED")
    return dict(res, outcome="OPEN")


def rug_state(pool_info: dict | None, entry_liq: float | None) -> str:
    """'hard' = pool listed but liquidity < max($1k, 10% of entry liquidity); 'soft' = token not listed by the pool
    source (could be an indexing gap); '' = fine or unknown (unknown is never a rug)."""
    if not pool_info:
        return ""
    if not pool_info.get("found"):
        return "soft"
    liq = fnum(pool_info.get("liq"), 0) or 0
    return "hard" if liq < max(RUG_ABS_USD, RUG_FRAC * (entry_liq or 0)) else ""


def is_rugged(pool_info: dict | None, entry_liq: float | None) -> bool:
    return rug_state(pool_info, entry_liq) != ""


def net_return(e: dict, outcome: str, last_close: float | None) -> float | None:
    if outcome == "WIN":
        return round(e["tp1_pct"] - e["cost_pct"], 3)
    if outcome in ("LOSS", "AMBIGUOUS"):
        return round(-e["stop_pct"] - e["cost_pct"], 3)
    if outcome in ("RUGGED", "NO_DATA"):
        return -100.0 if outcome == "RUGGED" else round(-e["stop_pct"] - e["cost_pct"], 3)
    if outcome == "EXPIRED":
        lc = last_close if last_close else e["entry_price"]
        return round((lc / e["entry_price"] - 1) * 100 - e["cost_pct"], 3)
    return None


def apply_check(e: dict, candles: list | None, src: str, pool_info: dict | None, now: float) -> None:
    """Update one entry from a fetched candle set (None = fetch failed) and current pool info."""
    e["checks"] = e.get("checks", 0) + 1
    e["last_check_ts"] = now
    horizon = e["max_hold_h"] * 3600
    window_over = now >= e["entry_ts"] + horizon
    rug = rug_state(pool_info, e.get("liquidity_usd_entry"))
    if pool_info:
        e["liq_now"] = pool_info.get("liq")
    if candles is None:
        e["fails"] = e.get("fails", 0) + 1
        e["last_error"] = src
        if rug:
            e["rug_seen"] = e.get("rug_seen", 0) + 1
            if e["rug_seen"] >= 2 or window_over:
                finalize(e, "RUGGED", None, None, f"pool {'not listed' if rug == 'soft' else 'liquidity vanished'}; no candles")
        elif window_over and e["fails"] >= NO_DATA_AFTER_FAILS:
            finalize(e, "NO_DATA", None, None, f"no price history after {e['fails']} tries: {src}")
        return
    e["candle_source"] = src
    if e.get("entry_mode") == "at_post":
        if not set_entry_from_candles(e, candles, now):
            e["fails"] = e.get("fails", 0) + 1
            e["last_error"] = "no candle at/after the post time"
            if window_over and e["fails"] >= NO_DATA_AFTER_FAILS:
                finalize(e, "NO_DATA", None, None, "no candles after the call")
            return
        source_stats(e, candles, pool_info, now)
    r = evaluate(candles, e["entry_ts"], e["entry_price"], e["stop"], e["tp1"], horizon, min(now, e["entry_ts"] + horizon))
    hs = e["scanner_horizon_h"] * 3600
    rs = evaluate(candles, e["entry_ts"], e["entry_price"], e["stop"], e["tp1"], hs, min(now, e["entry_ts"] + hs))
    e["path"] = {k: r[k] for k in ("n_candles", "max_high", "min_low", "last_close", "ref_price", "entry_mismatch_pct")}
    if r["entry_mismatch_pct"] is not None and abs(r["entry_mismatch_pct"]) > BAD_PRICE_PCT:
        finalize(e, "BAD_PRICE", None, None, f"scan price vs pool candle differ {r['entry_mismatch_pct']}%")
        return
    if rs["outcome"] != "OPEN" and not e.get("outcome_scanner_h"):
        e["outcome_scanner_h"] = rs["outcome"]
    out = r["outcome"]
    if out in ("WIN", "LOSS", "AMBIGUOUS"):
        finalize(e, out, r["at_ts"], r["last_close"], None)
        return
    if rug == "hard" or (rug == "soft" and not r["n_candles"]):
        e["rug_seen"] = e.get("rug_seen", 0) + 1
        if e["rug_seen"] >= 2 or window_over:
            finalize(e, "RUGGED", None, r["last_close"], f"liquidity now {(pool_info or {}).get('liq')} (entry {e.get('liquidity_usd_entry')})")
            return
    if out == "EXPIRED":
        finalize(e, "EXPIRED", None, r["last_close"], None)


def set_entry_from_candles(e: dict, candles: list, now: float) -> bool:
    """Entry for a channel call = the price at the post time: close of the last candle opening at or before the post
    (flag candle_at_post), else the open of the first later candle (flag first_candle_after_post + gap)."""
    if e.get("entry_price"):
        return True
    t = e["entry_ts"]
    before = [c for c in candles if c[0] <= t and t - c[0] <= 2 * CANDLE_S]
    after = [c for c in candles if c[0] > t]
    if before:
        px, flag = before[-1][4], "candle_at_post"
    elif after:
        px, flag = after[0][1], f"first_candle_after_post (+{(after[0][0] - t) / 60:.0f} min)"
    else:
        return False
    if not px or px <= 0:
        return False
    e.update(entry_price=px, entry_flag=flag, stop=px * (1 - e["stop_pct"] / 100), tp1=px * (1 + e["tp1_pct"] / 100))
    return True


def source_stats(e: dict, candles: list, pool_info: dict | None, now: float) -> None:
    """Max multiple within 24 h, price 24 h later, bonding-curve status and a posted-MC sanity check."""
    t, px = e["entry_ts"], e["entry_price"]
    w = [c for c in candles if t <= c[0] < t + 86400]
    if w:
        e["max_mult_24h"] = round(max(c[2] for c in w) / px, 3)
        e["min_mult_24h"] = round(min(c[3] for c in w) / px, 3)
    post = [c for c in candles if c[0] >= t]
    if post:
        e["max_mult_seen"] = round(max(c[2] for c in post) / px, 3)
    if now >= t + 86400:
        upto = [c for c in candles if c[0] <= t + 86400]
        if upto and upto[-1][0] >= t:
            e["price_24h_ratio"] = round(upto[-1][4] / px, 4)
        elif pool_info and not pool_info.get("found"):
            e["price_24h_ratio"] = 0.0
    if pool_info:
        dexes = pool_info.get("dexes") or []
        e["dexes"] = dexes
        e["on_curve"] = bool(dexes) and all("pumpfun" == d.lower() or "pump.fun" in d.lower() for d in dexes)
        if pool_info.get("mc") and pool_info.get("price") and e.get("mc_at_call"):
            supply = pool_info["mc"] / pool_info["price"]
            e["mc_implied_at_post"] = round(supply * px, 2)
            e["mc_ratio_posted_vs_implied"] = round(e["mc_at_call"] / e["mc_implied_at_post"], 3) if e["mc_implied_at_post"] else None


def finalize(e: dict, outcome: str, at_ts, last_close, note) -> None:
    e.update(status="RESOLVED", outcome=outcome, resolved_at_ts=at_ts, resolved_check_ts=e.get("last_check_ts"),
             net_return_pct=net_return(e, outcome, last_close), outcome_note=note)
    if at_ts:
        e["hours_to_outcome"] = round((at_ts - e["entry_ts"]) / 3600, 2)
    if outcome in ("RUGGED", "NO_DATA", "BAD_PRICE") and not e.get("outcome_scanner_h"):
        e["outcome_scanner_h"] = outcome


# ---------------------------------------------------------------- resolve
def resolve(entries: list[dict], bud: Budget, now: float | None = None, verbose=False, which: str = "all",
            gt_cap: int | None = None) -> dict:
    """which = 'scanner' | 'sources' | 'all'. gt_cap limits this part's GeckoTerminal calls (shared Budget)."""
    now = now or time.time()
    stats = {"eligible": 0, "checked": 0, "resolved": 0, "fetch_fail": 0, "sources": {}}
    cands = []
    for e in entries:
        if e.get("status") != "OPEN":
            continue
        is_src = e.get("source", "scanner") != "scanner"
        if (which == "scanner" and is_src) or (which == "sources" and not is_src):
            continue
        if e["chain"] not in GT_NET and e["chain"] != "solana":
            continue  # e.g. an EVM call whose chain is not resolved yet
        age = now - e["entry_ts"]
        if age < max(MIN_AGE_S, float(e.get("min_age_h") or 0) * 3600):
            continue
        final_due = age >= e["max_hold_h"] * 3600
        if not final_due and e.get("last_check_ts") and now - e["last_check_ts"] < RECHECK_S:
            continue
        cands.append(e)
    stats["eligible"] = len(cands)
    if which == "sources":  # newest completed-24h calls first (recent channel quality), then the backlog
        cands.sort(key=lambda e: (-(now - e["entry_ts"] >= e["max_hold_h"] * 3600 and e.get("checks", 0) > 0),
                                  e.get("checks", 0) > 0, -e["entry_ts"]))
    else:
        cands.sort(key=lambda e: (-(now - e["entry_ts"] >= e["max_hold_h"] * 3600), -VERDICT_RANK.get(e["verdict"], 0),
                                  e.get("checks", 0) > 0, e["entry_ts"]))
    bn_ok = [e for e in cands if (e.get("major_symbol") or "").upper() in BINANCE_US and e["chain"] == "solana"
             and not e.get("bn_stale")]
    gt_list = [e for e in cands if e not in bn_ok]
    # only as many GT entries as the candle budget can serve this run (pool discovery shares that budget)
    try:  # DexScreener cooling down -> spend GeckoTerminal calls on entries whose pool is already known
        if _http.cooldown_remaining("https://api.dexscreener.com/tokens/v1/solana/x") > 0 or "ds" in bud.blocked or bud.left["ds"] <= 0:
            gt_list.sort(key=lambda e: not e.get("pair"))
    except Exception:
        pass
    cap = bud.left["gt"] if gt_cap is None else min(gt_cap, bud.left["gt"])
    sel = gt_list[:max(0, cap)]
    bn_sel = bn_ok[:max(0, bud.left["bn"])]
    by_chain: dict[str, list[dict]] = {}
    for e in sel:
        by_chain.setdefault(e["chain"], []).append(e)
    pool_info: dict[tuple, dict] = {}
    for chain, es in by_chain.items():
        addrs = list(dict.fromkeys(e["address"] for e in es))
        info = ds_pairs_batch(chain, addrs, bud)
        if info is None:  # DexScreener unavailable -> GT batch for tokens without a known pool
            need = [e["address"] for e in es if not e.get("pair")]
            info = gt_pools_batch(chain, list(dict.fromkeys(need)), bud) if need else {}
        for a, v in (info or {}).items():
            pool_info[(chain, a)] = v
    for e in bn_sel:
        until = min(now, e["entry_ts"] + e["max_hold_h"] * 3600)
        c, src = bn_candles(e["major_symbol"].upper(), e["entry_ts"], until, bud)
        if c is None and "stale" in src:
            e["bn_stale"] = True  # route to the DEX pool (GeckoTerminal) from now on; not a data failure
            continue
        apply_check(e, c, src, None, now)  # majors on Binance: no rug concept
        _tally(stats, e, c, src)
    for e in sel:
        pi = pool_info.get((e["chain"], e["address"]))
        pool = e.get("pair") or (pi or {}).get("pool")  # the scan's own pool first (its price came from it)
        if not pool:
            if pi and not pi.get("found"):
                apply_check(e, None, "no pool listed", pi, now)
                _tally(stats, e, None, "no pool listed")
            else:
                stats["skipped_lookup_unavailable"] = stats.get("skipped_lookup_unavailable", 0) + 1
            continue
        e["pool_used"] = pool
        until = min(now, e["entry_ts"] + e["max_hold_h"] * 3600)
        since = e["entry_ts"] - (1800 if e.get("entry_mode") == "at_post" else 0)
        c, src = gt_candles(e["chain"], pool, e["address"], since, until, bud)
        if c is None and ("budget" in src or "blocked" in src or "429" in src):
            if "429" in src:
                e["last_error"] = src
            continue  # not a data failure; try next run
        apply_check(e, c, src, pi, now)
        _tally(stats, e, c, src)
    return stats


def _tally(stats, e, c, src):
    stats["checked"] += 1
    stats["resolved"] += e.get("status") == "RESOLVED"
    stats["fetch_fail"] += c is None
    k = src if c is not None else "fail: " + src.split(":")[0]
    stats["sources"][k] = stats["sources"].get(k, 0) + 1


# ---------------------------------------------------------------- report
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    ph = k / n
    den = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / den
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)


def is_win(e, field="outcome"):
    return e.get(field) == "WIN"


def summarize(es: list[dict], field="outcome") -> dict:
    n = len(es)
    k = sum(1 for e in es if is_win(e, field))
    lo, hi = wilson(k, n)
    ps = [e["p"] for e in es if e.get("p") is not None]
    rets = [e["net_return_pct"] for e in es if e.get("net_return_pct") is not None and field == "outcome"]
    out = {"n": n, "wins": k, "win_rate": round(k / n, 4) if n else None, "wilson95": [lo, hi],
           "pred_avg": round(sum(ps) / len(ps), 4) if ps else None,
           "brier": round(sum((e["p"] - (1 if is_win(e, field) else 0)) ** 2 for e in es if e.get("p") is not None) / len(ps), 4) if ps else None,
           "small_sample": n < SMALL_N}
    if rets:
        out["mean_net_return_pct"] = round(sum(rets) / len(rets), 2)
    counts = {}
    for e in es:
        counts[e.get(field)] = counts.get(e.get(field), 0) + 1
    out["outcomes"] = counts
    return out


def auc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg:
        return None
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    ranks, i = {}, 0
    rsum = 0.0
    while i < len(allv):
        j = i
        while j < len(allv) and allv[j][0] == allv[i][0]:
            j += 1
        r = (i + j + 1) / 2
        rsum += r * sum(1 for t in allv[i:j] if t[1] == 1)
        i = j
    return round((rsum - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)), 4)


def factor_separation(es: list[dict]) -> list[dict]:
    rows = []
    keys = set()
    for e in es:
        keys.update((e.get("factors") or {}).keys())
    keys |= {"p", "stop_pct", "cost_pct", "composite", "narrow_p"}
    for k in sorted(keys):
        if k == "failed_checks":
            continue
        vals = [((e.get("factors") or {}).get(k) if k in (e.get("factors") or {}) else e.get(k), is_win(e)) for e in es]
        vals = [(v, w) for v, w in vals if v is not None]
        if len(vals) < 5:
            continue
        num = [(fnum(v), w) for v, w in vals if not isinstance(v, bool) and fnum(v) is not None]
        if len(num) == len(vals):
            pos, neg = [v for v, w in num if w], [v for v, w in num if not w]
            a = auc(pos, neg)
            if a is None:
                continue
            rows.append({"factor": k, "type": "numeric", "n": len(num), "n_win": len(pos), "auc": a,
                         "separation": round(abs(a - 0.5) * 2, 4), "direction": "higher->wins" if a > 0.5 else "lower->wins",
                         "mean_win": round(sum(pos) / len(pos), 4) if pos else None, "mean_loss": round(sum(neg) / len(neg), 4) if neg else None})
        else:
            groups = {}
            for v, w in vals:
                g = groups.setdefault(str(v), [0, 0])
                g[0] += 1
                g[1] += w
            rates = {g: {"n": c[0], "win_rate": round(c[1] / c[0], 3)} for g, c in groups.items()}
            big = [r["win_rate"] for r in rates.values() if r["n"] >= 5]
            rows.append({"factor": k, "type": "categorical", "n": len(vals), "groups": rates,
                         "separation": round(max(big) - min(big), 4) if len(big) >= 2 else 0.0})
    rows.sort(key=lambda r: -r["separation"])
    return rows


def receipt_outcomes(entries: list[dict]) -> dict:
    """receipt id -> eventual outcome, for scanner entries that carry a receipt (plus duplicates folded into them and
    entries superseded by a stronger verdict). Written atomically to data/receipt_outcomes.json each report."""
    by_id = {e["id"]: e for e in entries}
    out = {}
    for e in entries:
        rids = ([e["receipt_id"]] if e.get("receipt_id") else []) + list(e.get("dup_receipt_ids") or [])
        if not rids:
            continue
        tgt, via = e, "self"
        if e.get("status") == "SUPERSEDED" and by_id.get(e.get("superseded_by")):
            tgt, via = by_id[e["superseded_by"]], f"superseded by {e['superseded_by']}"
        if tgt.get("status") != "RESOLVED":
            continue
        rec = {"entry_id": tgt["id"], "outcome": tgt.get("outcome"), "outcome_48h": tgt.get("outcome_scanner_h"),
               "net_return_pct": tgt.get("net_return_pct"), "resolved_et": et_str(tgt.get("resolved_at_ts") or tgt.get("resolved_check_ts")),
               "narrow_p": tgt.get("narrow_p"), "p": tgt.get("p")}
        for i, rid in enumerate(rids):
            out[rid] = dict(rec, via=via if i == 0 else f"duplicate folded into {e['id']}")
    return out


def narrow_summary(res: list[dict]) -> dict:
    """Grade the SHADOW narrow p on resolved scanner entries next to the scanner's own p (same entries)."""
    es = [e for e in res if e.get("narrow_p") is not None]
    n = len(es)
    out = {"n": n, "min_labels_for_fit": getattr(nq, "MIN_LABELS", 50),
           "note": "too few labels" if n < getattr(nq, "MIN_LABELS", 50) else "enough labels: run narrow_questions.py fit"}
    if not n:
        return out
    ys = [1 if is_win(e) else 0 for e in es]
    out["brier_narrow"] = round(sum((e["narrow_p"] - y) ** 2 for e, y in zip(es, ys)) / n, 4)
    ps = [(e.get("p"), y) for e, y in zip(es, ys) if e.get("p") is not None]
    out["brier_scanner_p_same_entries"] = round(sum((p - y) ** 2 for p, y in ps) / len(ps), 4) if ps else None
    out["win_rate"] = round(sum(ys) / n, 4)
    out["auc_narrow"] = auc([e["narrow_p"] for e, y in zip(es, ys) if y], [e["narrow_p"] for e, y in zip(es, ys) if not y])
    out["models"] = sorted({(e.get("narrow") or {}).get("model_version") or "?" for e in es})
    return out


def report(entries: list[dict], state: dict, run_stats: dict | None = None) -> dict:
    all_live = [e for e in entries if e.get("status") != "SUPERSEDED"]
    live = [e for e in all_live if e.get("source", "scanner") == "scanner"]
    res = [e for e in live if e.get("status") == "RESOLVED" and e.get("outcome") not in EXCLUDED]
    res_s = [e for e in live if e.get("outcome_scanner_h") and e.get("outcome_scanner_h") not in EXCLUDED | {"OPEN"}]
    counts = {}
    for e in entries:
        k = e.get("outcome") or "OPEN"
        counts[k] = counts.get(k, 0) + 1

    def buckets(es, field="outcome"):
        out = []
        for lo, hi in BUCKETS:
            b = [e for e in es if e.get("p") is not None and lo <= e["p"] < hi]
            s = summarize(b, field)
            s["bucket"] = f"{lo:.2f}-{min(hi, 1.0):.2f}"
            out.append(s)
        return out

    def group(es, key, field="outcome"):
        gs = {}
        for e in es:
            gs.setdefault(str(e.get(key)), []).append(e)
        return {g: summarize(v, field) for g, v in sorted(gs.items())}

    rep = {"generated_et": et_str(time.time()), "entries_total": len(entries), "open": sum(1 for e in live if e["status"] == "OPEN"),
           "resolved_counted": len(res), "outcome_counts": counts,
           "overall": summarize(res), "overall_scanner_horizon": summarize(res_s, "outcome_scanner_h"),
           "calibration_all": buckets(res), "calibration_scanner_p": buckets([e for e in res if e.get("p_source") == "scanner"]),
           "calibration_derived_prior": buckets([e for e in res if e.get("p_source") == "derived_prior"]),
           "calibration_scanner_horizon": buckets(res_s, "outcome_scanner_h"),
           "by_chain": group(res, "chain"), "by_verdict": group(res, "verdict"), "by_p_source": group(res, "p_source"),
           "by_class": group(res, "cls"), "by_levels_source": group(res, "levels_source"),
           "factors": factor_separation(res), "guard_bucket_0.75_0.85": summarize([e for e in res if 0.75 <= (e.get("p") or 0) < 0.85]),
           "narrow_shadow": narrow_summary(res), "calibration_narrow_p": [dict(summarize([e for e in res if e.get("narrow_p") is not None and lo <= e["narrow_p"] < hi]), bucket=f"{lo:.2f}-{min(hi, 1.0):.2f}") for lo, hi in BUCKETS],
           "sources": source_report(all_live), "source_overlap": source_overlap(all_live), "last_run": run_stats, "settings": {"max_hold_h": MAX_HOLD_H, "dedupe_window_h": DEDUPE_WINDOW_S / 3600,
                                                 "candle": "5m", "recheck_h": RECHECK_S / 3600, "small_n": SMALL_N}}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(REPORT_JSON + ".tmp", "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1, default=str)
    os.replace(REPORT_JSON + ".tmp", REPORT_JSON)
    with open(REPORT_MD + ".tmp", "w", encoding="utf-8") as f:
        f.write(render_md(rep))
    os.replace(REPORT_MD + ".tmp", REPORT_MD)
    ro = receipt_outcomes(entries)
    with open(RECEIPT_OUTCOMES + ".tmp", "w", encoding="utf-8") as f:
        json.dump(ro, f, separators=(",", ":"), default=str)
    os.replace(RECEIPT_OUTCOMES + ".tmp", RECEIPT_OUTCOMES)
    rep["receipt_outcomes_written"] = len(ro)
    return rep


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else round((xs[m - 1] + xs[m]) / 2, 4)


def source_overlap(all_live: list[dict]) -> dict:
    """Contracts called by 2+ sources (coordinated promotion shows up here) and who called first."""
    by = {}
    for e in all_live:
        by.setdefault(norm_addr(e["chain"], e["address"]), []).append(e)
    multi = []
    for a, es in by.items():
        srcs = sorted({e.get("source", "scanner") for e in es})
        if len(srcs) >= 2:
            first = min(es, key=lambda e: e["entry_ts"])
            multi.append({"address": a, "symbol": first.get("symbol"), "sources": srcs, "first": first.get("source", "scanner"),
                          "first_et": first.get("entry_et")})
    tg = [m for m in multi if sum(1 for x in m["sources"] if x.startswith("tg:")) >= 2]
    return {"contracts_in_2plus_sources": len(multi), "contracts_in_2plus_tg_channels": len(tg),
            "examples": sorted(tg, key=lambda m: -len(m["sources"]))[:10]}


def source_report(all_live: list[dict]) -> dict:
    out = {}
    srcs = sorted({e.get("source") for e in all_live if e.get("source", "scanner") != "scanner"})
    try:
        import telegram_calls as tgc
    except Exception:
        tgc = None
    for sid in srcs:
        es = [e for e in all_live if e.get("source") == sid]
        res = [e for e in es if e.get("status") == "RESOLVED" and e.get("outcome") not in EXCLUDED]
        checked = [e for e in es if e.get("entry_price")]
        m24 = [e["max_mult_24h"] for e in checked if e.get("max_mult_24h") is not None]
        p24 = [e["price_24h_ratio"] for e in checked if e.get("price_24h_ratio") is not None]
        rep = {"calls_logged": len(es), "repeat_posts": sum(e.get("repeat_count", 0) for e in es),
               "contracts_called_repeatedly": sorted([(e.get("symbol"), e["address"], e.get("repeat_count", 0) + 1) for e in es
                                                      if e.get("repeat_count")], key=lambda x: -x[2])[:10],
               "checked": len(checked), "resolved": summarize(res),
               "hit_rate_excl_no_data": summarize([e for e in res if e.get("outcome") != "NO_DATA"]),
               "hit_rate_bot_eligible": summarize([e for e in res if e.get("on_curve") is False]),
               "entry_flags": {}, "max_mult_24h": {"n": len(m24), "median": _median(m24),
                                                   "share_ge_2x": round(sum(1 for x in m24 if x >= 2) / len(m24), 3) if m24 else None,
                                                   "share_ge_5x": round(sum(1 for x in m24 if x >= 5) / len(m24), 3) if m24 else None},
               "price_24h_ratio": {"n": len(p24), "median": _median(p24),
                                   "share_above_entry": round(sum(1 for x in p24 if x > 1) / len(p24), 3) if p24 else None,
                                   "share_below_half": round(sum(1 for x in p24 if x < 0.5) / len(p24), 3) if p24 else None},
               "pump_suffix": sum(1 for e in es if e.get("pump_suffix")),
               "on_bonding_curve_when_checked": sum(1 for e in es if e.get("on_curve")),
               "curve_status_known": sum(1 for e in es if "on_curve" in e),
               "mc_mismatch_gt2x": [(e.get("symbol"), e.get("mc_at_call"), e.get("mc_implied_at_post")) for e in es
                                    if e.get("mc_ratio_posted_vs_implied") and not 0.5 <= e["mc_ratio_posted_vs_implied"] <= 2][:10]}
        for e in checked:
            k = (e.get("entry_flag") or "?").split(" (")[0]
            rep["entry_flags"][k] = rep["entry_flags"].get(k, 0) + 1
        if tgc is not None:
            bs = tgc.load_brags(sid)
            by_msg = {e.get("msg_id"): e for e in es}
            by_addr = {norm_addr(e["chain"], e["address"]): e for e in es}
            cmp_rows = []
            for b in bs:
                e = by_msg.get(b.get("call_msg_id")) or by_addr.get(norm_addr("solana", b.get("call_contract") or ""))
                if e and e.get("max_mult_seen"):
                    cmp_rows.append({"ticker": e.get("symbol"), "brag_x": b.get("multiple"), "data_max_x": e["max_mult_seen"],
                                     "suspicious": b.get("multiple", 0) > 1.5 * e["max_mult_seen"]})
            linked = {b.get("call_msg_id") for b in bs if b.get("call_msg_id")}
            pub_mc = {}
            for c in tgc.load_calls(sid):
                if c.get("mc_at_call"):
                    pub_mc.setdefault(c["contract"], []).append((c.get("ts") or 0, c["mc_at_call"]))
            below, no_pub = [], []
            for b in bs:
                if not b.get("contract"):
                    continue
                pubs = [mc for ts, mc in pub_mc.get(b["contract"], []) if ts <= (b.get("ts") or 0)]
                if not pubs:
                    no_pub.append((b.get("tickers") or ["?"])[0])
                elif b.get("brag_entry_mc") and b["brag_entry_mc"] < 0.8 * min(pubs):
                    below.append({"ticker": (b.get("tickers") or ["?"])[0], "brag_entry_mc": b["brag_entry_mc"],
                                  "lowest_public_call_mc": min(pubs), "brag_x": b.get("multiple")})
            rep["brags"] = {"posts": len(bs), "linked_to_calls": sum(1 for b in bs if b.get("call_msg_id")),
                            "distinct_calls_bragged": len(linked),
                            "share_of_calls_bragged": round(len(linked) / len(es), 3) if es else None,
                            "compared_with_price_data": len(cmp_rows),
                            "brag_exceeds_data_1.5x": [r for r in cmp_rows if r["suspicious"]][:10],
                            "brags_without_prior_public_call": len(no_pub), "brags_without_prior_public_call_examples": sorted(set(no_pub))[:8],
                            "brag_entry_below_public_call": len(below), "brag_entry_below_public_call_examples": below[:6]}
        out[sid] = rep
    return out


def _pct(x):
    return "-" if x is None else f"{x*100:.0f}%"


def _row(label, s):
    lo, hi = s["wilson95"]
    ci = "-" if lo is None else f"{lo*100:.0f}–{hi*100:.0f}%"
    pa = "-" if s["pred_avg"] is None else f"{s['pred_avg']:.3f}"
    flag = "too small" if s["small_sample"] else "ok"
    oc = ", ".join(f"{k}:{v}" for k, v in sorted(s["outcomes"].items(), key=lambda kv: str(kv[0])))
    return f"| {label} | {s['n']} | {pa} | {_pct(s['win_rate'])} | {ci} | {flag} | {oc} |"


def render_md(rep: dict) -> str:
    L = [f"# Scanner shadow-mode calibration report", "",
         f"Generated {rep['generated_et']}. READ-ONLY: no trades; outcomes are simulated from public 5-minute candles.", "",
         f"Entries logged: {rep['entries_total']} · open: {rep['open']} · resolved and counted: {rep['resolved_counted']}.",
         "Outcome counts (all entries): " + ", ".join(f"{k} {v}" for k, v in sorted(rep["outcome_counts"].items())), ""]
    o = rep["overall"]
    if o["n"] < SMALL_N:
        L += [f"> **Sample too small to conclude anything** ({o['n']} resolved; need ≥ {SMALL_N} per bucket). "
              "Treat every number below as descriptive only.", ""]
    L += ["WIN = TP1 (stop% + round-trip cost above entry) touched before the stop. AMBIGUOUS (both in one candle), RUGGED "
          "and NO_DATA count as losses; EXPIRED (neither within 72 h) is not a win. BAD_PRICE/SUPERSEDED are excluded.", ""]
    hdr = ["| Bucket (P) | n | predicted avg | actual win rate | Wilson 95% | sample | outcomes |", "|---|---|---|---|---|---|---|"]
    for title, key in (("Calibration: all logged candidates (72 h)", "calibration_all"),
                       ("Calibration: scanner P_WIN only (PASS/WATCH, 72 h)", "calibration_scanner_p"),
                       ("Calibration: rejects with derived martingale prior b/(a+b) (72 h)", "calibration_derived_prior"),
                       ("Calibration at the scanner's own P_WIN horizon (scan config backtest_horizon_h, 48 h)", "calibration_scanner_horizon")):
        L += [f"## {title}", ""] + hdr + [_row(b["bucket"], b) for b in rep[key] if b["n"]] + \
             (["| (none yet) | 0 | - | - | - | - | - |"] if not any(b["n"] for b in rep[key]) else []) + [""]
    ns = rep.get("narrow_shadow") or {}
    L += ["## Narrow-question p (SHADOW, never a gate)", "",
          f"Resolved scanner entries with a narrow p: {ns.get('n', 0)} — {ns.get('note', '')}. "
          f"Brier narrow {ns.get('brier_narrow', '-')} vs scanner p {ns.get('brier_scanner_p_same_entries', '-')} on the same entries; "
          f"AUC narrow {ns.get('auc_narrow', '-')}; models {ns.get('models', [])}.", ""] + hdr + \
         ([_row(b["bucket"], b) for b in rep.get("calibration_narrow_p", []) if b["n"]] or ["| (none yet) | 0 | - | - | - | - | - |"]) + [""]
    g = rep["guard_bucket_0.75_0.85"]
    L += [f"§2C.9 guard bucket (P 0.75–0.85): n={g['n']}, win rate {_pct(g['win_rate'])}"
          + (" — insufficient sample" if g["n"] < 20 else (" — below 55%: guard would raise the bar" if (g["win_rate"] or 0) < 0.55 else "")), ""]
    for title, key in (("By chain", "by_chain"), ("By verdict", "by_verdict"), ("By probability source", "by_p_source"),
                       ("By token class", "by_class"), ("By stop/target source", "by_levels_source")):
        L += [f"## {title}", "", "| Group | n | predicted avg | actual win rate | Wilson 95% | sample | outcomes |", "|---|---|---|---|---|---|---|"]
        L += [_row(k, s) for k, s in rep[key].items()] or ["| (none yet) | 0 | - | - | - | - | - |"]
        L.append("")
    L += ["## Factors that best separated winners from losers", "",
          "Numeric: AUC of the reading for wins vs non-wins (0.5 = no separation). Categorical: spread of win rates across "
          "groups with ≥ 5 entries. With small samples these are noise.", "",
          "| Factor | type | n | AUC / spread | direction | mean win | mean non-win |", "|---|---|---|---|---|---|---|"]
    for r in rep["factors"][:15]:
        if r["type"] == "numeric":
            L.append(f"| {r['factor']} | numeric | {r['n']} | {r['auc']} | {r['direction']} | {r['mean_win']} | {r['mean_loss']} |")
        else:
            gs = "; ".join(f"{k}: {v['win_rate']*100:.0f}% (n={v['n']})" for k, v in list(r["groups"].items())[:5])
            L.append(f"| {r['factor']} | categorical | {r['n']} | {r['separation']} | {gs} | | |")
    if not rep["factors"]:
        L.append("| (need ≥ 5 resolved entries) | | | | | | |")
    if rep.get("sources"):
        L += ["## Call-source ranking (forward test; sorted by Wilson lower bound of the TP1-before-stop hit rate)", "",
              "| Source | calls | checked | resolved | hit rate | Wilson 95% | bot-eligible (migrated) hit rate | median max 24 h | sample |",
              "|---|---|---|---|---|---|---|---|---|"]
        rk = sorted(rep["sources"].items(), key=lambda kv: -(kv[1]["resolved"]["wilson95"][0] or 0))
        for sid, sr in rk:
            rs, be = sr["resolved"], sr["hit_rate_bot_eligible"]
            L.append(f"| {sid} | {sr['calls_logged']} | {sr['checked']} | {rs['n']} | {_pct(rs['win_rate'])} | "
                     f"{_pct(rs['wilson95'][0])}–{_pct(rs['wilson95'][1])} | {_pct(be['win_rate'])} (n={be['n']}) | "
                     f"{sr['max_mult_24h']['median']} | {'too small' if rs['small_sample'] else 'ok'} |")
        ov = rep.get("source_overlap") or {}
        L += ["", f"Overlap: {ov.get('contracts_in_2plus_tg_channels')} contracts were called by 2+ Telegram channels "
              f"({ov.get('contracts_in_2plus_sources')} across any sources incl. the scanner). Examples: "
              + "; ".join(f"{m['symbol']} ({', '.join(m['sources'])}; first {m['first']} {m['first_et']})" for m in ov.get("examples", [])[:5]), ""]
    for sid, sr in (rep.get("sources") or {}).items():
        rs = sr["resolved"]
        L += [f"## Source {sid} (forward test; calls never trigger trades)", "",
              f"- Calls logged {sr['calls_logged']} (+{sr['repeat_posts']} repeat posts of the same contract within 24 h, "
              f"not counted twice); price-checked {sr['checked']}; resolved {rs['n']}.",
              f"- TP1-before-stop hit rate (25% stop, TP1 = stop + 3% cost, 72 h): {_pct(rs['win_rate'])} "
              f"(Wilson 95% {_pct(rs['wilson95'][0])}–{_pct(rs['wilson95'][1])}); outcomes {rs['outcomes']}"
              + (" — **too small to conclude**" if rs["small_sample"] else ""),
              f"- Excluding NO_DATA: {_pct(sr['hit_rate_excl_no_data']['win_rate'])} of {sr['hit_rate_excl_no_data']['n']}.",
              f"- Max multiple within 24 h: median {sr['max_mult_24h']['median']}x (n={sr['max_mult_24h']['n']}), "
              f"≥2x {_pct(sr['max_mult_24h']['share_ge_2x'])}, ≥5x {_pct(sr['max_mult_24h']['share_ge_5x'])}.",
              f"- Price 24 h after the call vs entry: median {sr['price_24h_ratio']['median']} (n={sr['price_24h_ratio']['n']}); "
              f"above entry {_pct(sr['price_24h_ratio']['share_above_entry'])}, below half {_pct(sr['price_24h_ratio']['share_below_half'])}.",
              f"- pump.fun-style addresses (…pump): {sr['pump_suffix']} of {sr['calls_logged']}; still on the bonding curve when "
              f"checked: {sr['on_bonding_curve_when_checked']} of {sr['curve_status_known']} with known status (the bot never buys curve tokens, G10).",
              f"- Entry price source: {sr['entry_flags']} (entry = candle at the post time; 'first_candle_after_post' = flagged).",
              f"- Contracts called repeatedly: {sr['contracts_called_repeatedly'][:5]}",
              f"- Posted MC vs price-implied MC off by >2x: {sr['mc_mismatch_gt2x'][:5]}"]
        if sr.get("brags"):
            bb = sr["brags"]
            L.append(f"- Brag posts ('hit Nx'): {bb['posts']} ({bb['distinct_calls_bragged']} calls, share of calls bragged "
                     f"{_pct(bb['share_of_calls_bragged'])}); brag multiple > 1.5x the price-data max: {bb['brag_exceeds_data_1.5x'][:5]}; "
                     f"brags with no earlier public call (claimed 'VIP/private' entries): {bb['brags_without_prior_public_call']} "
                     f"{bb['brags_without_prior_public_call_examples'][:5]}; brag entry MC < 80% of the lowest public call MC: "
                     f"{bb['brag_entry_below_public_call']} {bb['brag_entry_below_public_call_examples'][:3]}")
        L.append("")
    lr = rep.get("last_run") or {}
    L += ["", "## Last run", "", "```", json.dumps(lr, indent=1, default=str)[:3000], "```", "",
          "## Limitations", "",
          "- Survivorship/selection: only tokens the scanner saw and saved with a price are logged (the 04:10 scan saved none).",
          "- Rejects carry no scanner P_WIN or stop: their stop uses the scanner's no-candle proxy rule, TP1 = stop + cost, cost is "
          "derived (prompt §2E.8), and their P is the martingale prior b/(a+b) — all marked 'derived'.",
          "- Entry = scan-time price; candles opening before the scan finished are skipped. Fills, slippage beyond the cost estimate, "
          "and gaps through stops (worse than the stop price) are not modeled beyond the cost number.",
          "- GeckoTerminal omits no-trade candles; a pool with no trades is treated as flat. Rug detection uses current DexScreener/"
          "GeckoTerminal liquidity (pool gone or < max($1k, 10% of entry liquidity)).",
          "- EXPIRED uses a 72 h max hold (the prompt sets none); the 48 h table matches the scanner's P_WIN definition.",
          ""]
    return "\n".join(L)


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["run", "ingest", "resolve", "report", "status"])
    ap.add_argument("--max-gt-calls", type=int, default=int(os.environ.get("SHADOW_MAX_GT", 20)))
    ap.add_argument("--max-ds-calls", type=int, default=int(os.environ.get("SHADOW_MAX_DS", 6)))
    ap.add_argument("--max-bn-calls", type=int, default=int(os.environ.get("SHADOW_MAX_BN", 30)))
    ap.add_argument("--max-gt-src", type=int, default=int(os.environ.get("SHADOW_MAX_GT_SRC", 8)),
                    help="part of --max-gt-calls reserved for watched-source calls (Telegram etc.)")
    ap.add_argument("--no-sources", action="store_true", help="skip watched sources (watched_sources.json)")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args(argv)
    os.makedirs(DATA_DIR, exist_ok=True)
    lockf = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if a.verbose:
            print("another shadow_tracker run holds the lock; skipping")
        return 0
    entries, state = load_log(), load_state()
    run = {"started_et": et_str(time.time()), "cmd": a.cmd}
    if a.cmd in ("run", "ingest"):
        run["ingest"] = ingest(entries, state, a.verbose)
        if not a.no_sources:
            try:
                run["ingest_sources"] = ingest_sources(entries)
            except Exception as exc:  # sources are additive: never break the scanner tracking
                run["ingest_sources"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        save_log(entries)
    if a.cmd in ("run", "resolve"):
        bud = Budget(a.max_gt_calls, a.max_ds_calls, a.max_bn_calls)
        try:
            src_cap = 0 if a.no_sources else max(0, min(a.max_gt_src, a.max_gt_calls))
            run["resolve"] = resolve(entries, bud, verbose=a.verbose, which="scanner", gt_cap=a.max_gt_calls - src_cap)
            if not a.no_sources:
                run["resolve_sources"] = resolve(entries, bud, verbose=a.verbose, which="sources", gt_cap=bud.left["gt"])  # reserved share + anything the scanner part left
        finally:
            save_log(entries)  # keep partial progress even if something breaks mid-run
        run["calls"] = {"used": bud.used, "cache_hits": bud.cache_hits, "rate_limited": bud.blocked}
    if a.cmd in ("run", "report", "resolve", "ingest"):
        state.setdefault("runs", []).append(run)
        state["runs"] = state["runs"][-50:]
        save_state(state)
        rep = report(entries, state, run)
        if a.verbose or a.cmd != "run":
            print(json.dumps({"run": run, "outcome_counts": rep["outcome_counts"], "open": rep["open"]}, indent=1, default=str))
    if a.cmd == "status":
        c = {}
        for e in entries:
            c[e.get("outcome") or "OPEN"] = c.get(e.get("outcome") or "OPEN", 0) + 1
        print(f"entries {len(entries)} | " + " ".join(f"{k}:{v}" for k, v in sorted(c.items())))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:  # a real code error -> nonzero
        traceback.print_exc()
        sys.exit(2)
