#!/usr/bin/env python3
"""options_data.py: free, no-key, read-only options cross-check for the Robinhood bot.

PRIMARY live source = the Robinhood connector (get_option_chains / get_option_instruments / get_option_quotes).
This tool is the FREE CROSS-CHECK: CBOE delayed quotes JSON
  https://cdn.cboe.com/api/global/delayed_quotes/options/<SYMBOL>.json   (indexes: _SPX, _VIX, ...)
UNDOCUMENTED (the JSON behind cboe.com's public delayed-quotes pages), ~15-minute delayed, may change without
notice. Never use it as an execution price. Read-only GETs only; nothing here places or previews orders.

Per contract: mid, spread %, open interest, volume, IV, delta, DTE, breakeven.
Robinhood-rule filter: long calls/puts only, DTE >= 30, spread <= 5% of mid, OI >= 500, volume >= 100, |delta| >= 0.20.
Sizing: max loss per contract (premium at the ask + est. fees) and contracts allowed under a 2%-of-equity cap.
Expected move from the ATM straddle. IV-rank PROXY: current CBOE iv30 vs the 1-year distribution of 30-day
REALIZED volatility built from CBOE's free daily history (a true IV percentile needs IV history, which isn't free here).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _http import get_json  # noqa: E402

ET = ZoneInfo("America/New_York")
CBOE_OPTIONS = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
CBOE_HISTORY = "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{sym}.json"
OCC_RE = re.compile(r"^(?P<root>[A-Z.]+?)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")
EST_FEE_PER_CONTRACT = 0.05  # conservative estimate of regulatory/exchange fees; read the real number from review_option_order

RULES = {"min_dte": 30, "max_spread_pct": 5.0, "min_oi": 500, "min_volume": 100, "min_abs_delta": 0.20}


def cboe_symbol(ticker: str) -> str:
    t = ticker.upper()
    return "_" + t if t in {"SPX", "VIX", "NDX", "RUT", "XSP", "DJX"} else t


def fetch_chain(ticker: str):
    return get_json(CBOE_OPTIONS.format(sym=cboe_symbol(ticker)), timeout=30)


def parse_occ(sym: str) -> dict | None:
    m = OCC_RE.match(sym)
    if not m:
        return None
    y, mo, d = int(m["ymd"][:2]), int(m["ymd"][2:4]), int(m["ymd"][4:])
    return {"root": m["root"], "expiration": date(2000 + y, mo, d), "type": "call" if m["cp"] == "C" else "put",
            "strike": int(m["strike"]) / 1000}


def contract_metrics(opt: dict, today: date | None = None) -> dict | None:
    p = parse_occ(opt.get("option", ""))
    if not p:
        return None
    today = today or datetime.now(ET).date()
    bid, ask = float(opt.get("bid") or 0), float(opt.get("ask") or 0)
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else None
    spread_pct = (ask - bid) / mid * 100 if mid else None
    be = None
    if mid is not None:
        be = p["strike"] + mid if p["type"] == "call" else p["strike"] - mid
    return {"contract": opt["option"], **p, "expiration": p["expiration"].isoformat(),
            "dte": (p["expiration"] - today).days, "bid": bid, "ask": ask,
            "mid": round(mid, 4) if mid else None, "spread_pct": round(spread_pct, 2) if spread_pct is not None else None,
            "open_interest": int(opt.get("open_interest") or 0), "volume": int(opt.get("volume") or 0),
            "iv": opt.get("iv"), "delta": opt.get("delta"), "theta": opt.get("theta"),
            "breakeven": round(be, 4) if be is not None else None,
            "premium_per_contract_at_ask": round(ask * 100, 2)}


def passes_rules(m: dict, rules: dict = RULES) -> tuple[bool, list[str]]:
    why = []
    if m["dte"] < rules["min_dte"]:
        why.append(f"DTE {m['dte']} < {rules['min_dte']}")
    if m["spread_pct"] is None or m["spread_pct"] > rules["max_spread_pct"]:
        why.append(f"spread {m['spread_pct']}% > {rules['max_spread_pct']}%")
    if m["open_interest"] < rules["min_oi"]:
        why.append(f"OI {m['open_interest']} < {rules['min_oi']}")
    if m["volume"] < rules["min_volume"]:
        why.append(f"volume {m['volume']} < {rules['min_volume']} (delayed data; re-check live)")
    if m["delta"] is None or abs(float(m["delta"])) < rules["min_abs_delta"]:
        why.append(f"|delta| {m['delta']} < {rules['min_abs_delta']}")
    return (not why), why


def size_for_cap(ask: float, equity: float, cap_pct: float = 2.0, fee: float = EST_FEE_PER_CONTRACT) -> dict:
    """Long option max loss = premium paid + fees. Contracts allowed under cap_pct of equity (H1: 2%)."""
    per = ask * 100 + fee
    cap = equity * cap_pct / 100
    n = int(cap // per) if per > 0 else 0
    return {"max_loss_per_contract": round(per, 2), "cap_usd": round(cap, 2), "contracts": n,
            "max_loss_total": round(n * per, 2), "fits": n >= 1}


def chain(ticker: str, kind: str | None = None, rules_only: bool = False, equity: float | None = None) -> dict:
    r = fetch_chain(ticker)
    if not r.ok:
        return {"ok": False, "status": r.status, "error": r.error}
    d = r.data["data"]
    today = datetime.now(ET).date()
    rows = []
    for o in d.get("options", []):
        m = contract_metrics(o, today)
        if not m or (kind and m["type"] != kind):
            continue
        ok, why = passes_rules(m)
        m["passes_rh_rules"], m["fail_reasons"] = ok, why
        if equity:
            m["sizing_2pct"] = size_for_cap(m["ask"], equity)
        if rules_only and not ok:
            continue
        rows.append(m)
    return {"ok": True, "source": "CBOE delayed (undocumented; cross-check only)", "symbol": d.get("symbol"),
            "underlying_price": d.get("current_price"), "iv30": d.get("iv30"), "cboe_timestamp": r.data.get("timestamp"),
            "last_trade_time": d.get("last_trade_time"), "count": len(rows), "contracts": rows}


def expected_move(ticker: str, min_dte: int = 0) -> dict:
    """ATM straddle (mid call + mid put at the strike nearest spot) for the first expiry with DTE >= min_dte."""
    r = fetch_chain(ticker)
    if not r.ok:
        return {"ok": False, "status": r.status, "error": r.error}
    d = r.data["data"]
    spot = float(d["current_price"])
    today = datetime.now(ET).date()
    ms = [m for m in (contract_metrics(o, today) for o in d["options"]) if m and m["mid"] and m["dte"] >= min_dte]
    if not ms:
        return {"ok": False, "error": "no quoted contracts"}
    exp = min(m["expiration"] for m in ms)
    same = [m for m in ms if m["expiration"] == exp]
    strike = min({m["strike"] for m in same}, key=lambda k: abs(k - spot))
    c = next((m for m in same if m["strike"] == strike and m["type"] == "call"), None)
    p = next((m for m in same if m["strike"] == strike and m["type"] == "put"), None)
    if not c or not p:
        return {"ok": False, "error": "ATM pair incomplete"}
    straddle = c["mid"] + p["mid"]
    return {"ok": True, "expiration": exp, "dte": c["dte"], "spot": spot, "atm_strike": strike,
            "straddle": round(straddle, 3), "expected_move_usd": round(straddle, 3),
            "expected_move_pct": round(straddle / spot * 100, 2),
            "expected_move_pct_0_85x": round(0.85 * straddle / spot * 100, 2)}


def iv_rank_proxy(ticker: str) -> dict:
    """PROXY ONLY: percentile of current iv30 within the trailing-252-day distribution of 30-day realized vol."""
    q = fetch_chain(ticker)
    h = get_json(CBOE_HISTORY.format(sym=cboe_symbol(ticker)), timeout=30)
    if not (q.ok and h.ok):
        return {"ok": False, "error": f"chain {q.status} / history {h.status}"}
    iv30 = float(q.data["data"].get("iv30") or 0)
    closes = [float(x["close"]) for x in h.data["data"][-300:] if float(x["close"]) > 0]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    hv = []
    for i in range(30, len(rets) + 1):
        w = rets[i - 30:i]
        mu = sum(w) / 30
        hv.append(math.sqrt(sum((x - mu) ** 2 for x in w) / 29) * math.sqrt(252) * 100)
    hv = hv[-252:]
    if not hv or not iv30:
        return {"ok": False, "error": "insufficient data"}
    pct = sum(1 for x in hv if x <= iv30) / len(hv) * 100
    return {"ok": True, "iv30": iv30, "hv30_now": round(hv[-1], 2), "hv30_min": round(min(hv), 2),
            "hv30_max": round(max(hv), 2), "iv30_percentile_vs_hv30_1y": round(pct, 1),
            "label": "PROXY (iv30 vs realized-vol history), not a true IV percentile"}


def selftest() -> list[dict]:
    rows = []

    def add(check, fn, summ):
        t = time.perf_counter()
        try:
            ok, st, det = summ(fn())
        except Exception as exc:
            ok, st, det = False, "exception", repr(exc)
        rows.append({"tool": "options_data", "check": check, "ok": ok, "status": st,
                     "ms": int((time.perf_counter() - t) * 1000), "detail": str(det)[:220]})
        time.sleep(0.5)

    add("CBOE chain AAPL (undocumented)", lambda: chain("AAPL"),
        lambda d: (d["ok"] and d["count"] > 0, 200 if d["ok"] else d.get("status"),
                   {k: d.get(k) for k in ("underlying_price", "iv30", "count", "cboe_timestamp", "error")}))
    add("RH-rule filter AAPL calls (DTE>=30, spread<=5%, OI>=500, vol>=100, |delta|>=0.20)",
        lambda: chain("AAPL", "call", rules_only=True, equity=250),
        lambda d: (d["ok"], "-", {"passing": d.get("count"),
                                  "first": ({k: d["contracts"][0][k] for k in ("contract", "dte", "mid", "spread_pct",
                                            "open_interest", "iv", "delta", "breakeven", "sizing_2pct")}
                                            if d.get("count") else None)}))
    add("CBOE chain SPY + index _SPX", lambda: (chain("SPY")["count"], chain("SPX")["count"]),
        lambda d: (d[0] > 0 and d[1] > 0, "-", {"SPY contracts": d[0], "SPX contracts": d[1]}))
    add("size_for_cap(ask 0.45, equity 250)", lambda: size_for_cap(0.45, 250),
        lambda d: (d["contracts"] == 0 and d["max_loss_per_contract"] == 45.05, "-", d))
    add("expected_move AAPL (first expiry >= 7 DTE)", lambda: expected_move("AAPL", 7),
        lambda d: (d["ok"], "-", d))
    add("iv_rank_proxy AAPL", lambda: iv_rank_proxy("AAPL"), lambda d: (d["ok"], "-", d))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="CBOE delayed options cross-check (undocumented source)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("cmd", nargs="?", choices=["chain", "filter", "move", "ivproxy", "size"])
    ap.add_argument("ticker", nargs="?")
    ap.add_argument("--type", choices=["call", "put"])
    ap.add_argument("--equity", type=float)
    ap.add_argument("--ask", type=float)
    a = ap.parse_args()
    if a.selftest:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from crypto_data import rows_to_md
        rows = selftest()
        print(rows_to_md(rows))
        return 0 if all(r["ok"] for r in rows) else 1
    if a.cmd == "size":
        out = size_for_cap(a.ask, a.equity)
    elif a.cmd in ("chain", "filter"):
        out = chain(a.ticker, a.type, rules_only=a.cmd == "filter", equity=a.equity)
    elif a.cmd == "move":
        out = expected_move(a.ticker)
    elif a.cmd == "ivproxy":
        out = iv_rank_proxy(a.ticker)
    else:
        ap.print_help()
        return 2
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
