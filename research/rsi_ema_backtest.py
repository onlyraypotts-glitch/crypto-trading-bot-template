#!/usr/bin/env python3
"""rsi_ema_backtest.py: standalone, READ-ONLY backtest of an RSI(14) + EMA(12/26) confluence entry (the generic idea
behind the 'Cabbage' bot the owner saw; that repo was NOT downloaded, cloned, read or run: this is our own implementation).

Signals (CLOSED candles only; decided at candle i's close, filled at candle i+1's OPEN, so no look-ahead):
  A  EMA12 crosses above EMA26 AND 50 <= RSI(14) <= 70           (narrow_questions.signal_a)
  B  RSI(14) crosses up through 30 while EMA12 > EMA26           (narrow_questions.signal_b)
Exit: EMA12 crosses below EMA26 (filled next open), or the % stop (intrabar low; a gap below the stop fills at the
open, i.e. worse than the stop). One position per symbol at a time, fixed $100 notional per trade, no compounding.
Stops tested: 25% (prompt H5 memecoin maximum) and 5% (stand-in for a strategy's own tight % stop; the real repo's
stop was not inspected). Costs per round trip: 3.0% (FOMO Solana) and 1.9% (Phantom). Buy-and-hold = one round trip
over the same window, same cost.

Data: Binance.US klines (5m and 1h) for liquid Solana majors, through research/_http.py (shared limiter + file cache; the
first 1h page uses the scanner's exact URL, so it is a cache hit when the scanner ran recently). A 429 stops all
further fetching (no retries/hammering). DEX sources (DexScreener/GeckoTerminal/Jupiter) are NOT used.
Usage: python3 rsi_ema_backtest.py [--pages5m 3] [--pages1h 2] [--symbols SOL,BONK,...] [--offline-cache-only]
Output: data/rsi_ema_backtest.{md,json}. Nothing here trades.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import narrow_questions as nq  # noqa: E402  (shared indicator + signal definitions)

ET = ZoneInfo("America/New_York")
import paths as _paths  # noqa: E402
OUT_MD = os.path.join(_paths.BASE_DATA_DIR, "rsi_ema_backtest.md")
OUT_JSON = os.path.join(_paths.BASE_DATA_DIR, "rsi_ema_backtest.json")
SYMBOLS = ["SOL", "BONK", "JUP", "WIF", "PENGU", "JTO", "TRUMP", "FARTCOIN", "POPCAT", "RENDER"]
INTERVAL_S = {"5m": 300, "1h": 3600}
STOPS = [0.25, 0.05]
COSTS = {"FOMO Solana 3.0%": 3.0, "Phantom 1.9%": 1.9}
STAKE = 100.0
BAD_PRINT_RATIO = 1.5   # a single candle with high/low > 1.5 on a Binance.US major = bad data, not a tradeable move


def fetch(sym: str, interval: str, pages: int, state: dict) -> tuple[list, str]:
    """Up to `pages` x 1000 klines ending now, oldest first. Stops everything on a 429."""
    from _http import get_json
    out: dict[int, tuple] = {}
    end = None
    for pg in range(pages):
        if state.get("stopped"):
            break
        url = f"https://api.binance.us/api/v3/klines?symbol={sym}USDT&interval={interval}&limit=1000"
        if end is not None:
            url += f"&endTime={end}"
        r = get_json(url, cache_ttl=900 if end is None else 86400)
        if r.status == 429 or "rate limit" in (r.error or "").lower() or "cooling down" in (r.error or "").lower():
            state["stopped"] = f"{sym} {interval}: {r.status} {(r.error or '')[:80]}"
            break
        if not r.ok or not isinstance(r.data, list) or not r.data:
            return sorted(out.values()), f"no data ({r.status} {(r.error or '')[:60]})" if not out else "partial"
        state["calls"] = state.get("calls", 0) + 1
        for k in r.data:
            out[int(k[0]) // 1000] = (int(k[0]) // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4]))
        end = int(r.data[0][0]) - 1
        if len(r.data) < 1000:
            break
    return sorted(out.values()), "ok"


def backtest(candles: list, interval_s: int, signal, stop: float, cost_pct: float, now: float | None = None) -> dict:
    """Trades on CLOSED candles. Returns per-trade net % (after cost) and summary."""
    cs = nq.closed_only(candles, interval_s, now)
    closes = [c[4] for c in cs]
    e12, e26, r = nq.indicators(closes)
    trades, i, n = [], 0, len(cs)
    while i < n - 1:
        if not signal(e12, e26, r, i):
            i += 1
            continue
        entry = cs[i + 1][1]  # next candle's open
        sp = entry * (1 - stop)
        j, exit_px, why = i + 1, None, None
        while j < n:
            o, lo = cs[j][1], cs[j][3]
            if lo <= sp:
                exit_px, why = (min(o, sp) if j > i + 1 else sp), "stop"
                break
            if nq.exit_signal(e12, e26, j) and j + 1 < n:
                exit_px, why = cs[j + 1][1], "ema_cross_down"
                j += 1
                break
            j += 1
        if exit_px is None:
            exit_px, why, j = cs[-1][4], "open_at_end", n - 1
        gross = (exit_px / entry - 1) * 100
        trades.append({"entry_ts": cs[i + 1][0], "exit_ts": cs[min(j, n - 1)][0], "gross_pct": round(gross, 3),
                       "net_pct": round(gross - cost_pct, 3), "exit": why})
        i = j + 1
    return summarize(trades, cs, cost_pct)


def summarize(trades: list, cs: list, cost_pct: float) -> dict:
    nets = [t["net_pct"] for t in trades]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for x in nets:
        eq += STAKE * x / 100
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    bh = ((cs[-1][4] / cs[0][4] - 1) * 100 - cost_pct) if len(cs) > 1 else None
    return {"trades": len(trades), "wins": sum(1 for x in nets if x > 0),
            "win_rate": round(sum(1 for x in nets if x > 0) / len(nets), 4) if nets else None,
            "expectancy_net_pct": round(sum(nets) / len(nets), 3) if nets else None,
            "expectancy_gross_pct": round(sum(t["gross_pct"] for t in trades) / len(trades), 3) if trades else None,
            "total_net_usd": round(sum(STAKE * x / 100 for x in nets), 2), "max_drawdown_usd": round(mdd, 2),
            "stops_hit": sum(1 for t in trades if t["exit"] == "stop"), "buy_hold_net_pct": round(bh, 2) if bh is not None else None,
            "candles": len(cs), "from": cs[0][0] if cs else None, "to": cs[-1][0] if cs else None, "trade_list": trades}


def pool(rows: list) -> dict:
    ts = [t for r in rows for t in r["trade_list"]]
    nets = [t["net_pct"] for t in sorted(ts, key=lambda t: t["exit_ts"])]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for x in nets:
        eq += STAKE * x / 100
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    bh = [r["buy_hold_net_pct"] for r in rows if r["buy_hold_net_pct"] is not None]
    return {"symbols": len(rows), "trades": len(nets), "win_rate": round(sum(1 for x in nets if x > 0) / len(nets), 4) if nets else None,
            "expectancy_net_pct": round(sum(nets) / len(nets), 3) if nets else None, "total_net_usd": round(sum(STAKE * x / 100 for x in nets), 2),
            "max_drawdown_usd": round(mdd, 2), "buy_hold_avg_net_pct": round(sum(bh) / len(bh), 2) if bh else None,
            "buy_hold_total_usd": round(sum(STAKE * x / 100 for x in bh), 2) if bh else None}


def et(ts):
    return datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d %H:%M ET") if ts else "-"


def run(symbols: list, pages: dict, now: float | None = None) -> dict:
    state: dict = {}
    data: dict = {}
    for iv in ("1h", "5m"):
        for s in symbols:
            if state.get("stopped"):
                break
            c, why = fetch(s, iv, pages[iv], state)
            wild = [x for x in c if x[3] > 0 and x[2] / x[3] > BAD_PRINT_RATIO]
            if wild:  # e.g. POPCAT on Binance.US: 0.011 -> 0.199 inside 5 minutes (thin book / bad prints) -> excluded, listed
                state.setdefault("excluded_bad_prints", []).append(f"{s} {iv}: {len(wild)} candle(s) with high/low > {BAD_PRINT_RATIO}")
            elif len(c) >= 100:
                data[(s, iv)] = c
            else:
                state.setdefault("skipped", []).append(f"{s} {iv}: {why}")
    results = {}
    for iv in ("1h", "5m"):
        for sname, sig in (("A: EMA cross up + RSI 50-70", nq.signal_a), ("B: RSI cross up 30 in EMA uptrend", nq.signal_b)):
            for stop in STOPS:
                for cname, cost in COSTS.items():
                    per = {s: backtest(c, INTERVAL_S[iv], sig, stop, cost, now) for (s, i2), c in data.items() if i2 == iv}
                    results[f"{iv} | {sname} | stop {int(stop*100)}% | {cname}"] = {"pooled": pool(list(per.values())),
                                                                                  "per_symbol": {k: {kk: vv for kk, vv in v.items() if kk != "trade_list"} for k, v in per.items()}}
    spans = {f"{s} {iv}": f"{et(c[0][0])} → {et(c[-1][0])} ({len(c)})" for (s, iv), c in data.items()}
    return {"generated_et": et(time.time()), "fetch": {"calls": state.get("calls", 0), "stopped_on_rate_limit": state.get("stopped"),
            "skipped": state.get("skipped", []), "excluded_bad_prints": state.get("excluded_bad_prints", [])}, "spans": spans, "results": results}


def render(rep: dict) -> str:
    L = ["# RSI(14) + EMA(12/26) confluence backtest (own implementation; READ-ONLY)", "",
         f"Generated {rep['generated_et']}. Binance.US klines, CLOSED candles only, entry at the next candle's open, fixed $100 per "
         "trade, one position per symbol. Exit on EMA12 crossing below EMA26 (next open) or the % stop (gap below the stop fills at the "
         "open). Expectancy and win rate are NET of the round-trip cost. Max drawdown = worst peak-to-trough of the summed $100-per-trade "
         "P&L (all symbols, by exit time). Buy & hold = one round trip per symbol over the same window, same cost.", "",
         f"Fetch: {rep['fetch']['calls']} Binance.US calls; stopped on rate limit: {rep['fetch']['stopped_on_rate_limit'] or 'no'}; "
         f"skipped: {', '.join(rep['fetch']['skipped']) or 'none'}; excluded for bad prints: "
         f"{', '.join(rep['fetch'].get('excluded_bad_prints') or []) or 'none'}.", "",
         "| Interval | Signal | Stop | Cost | Trades | Win rate | Expectancy net / trade | Total net ($100/trade) | Max DD ($) | Buy&hold avg net | Buy&hold total ($100 each) |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, v in rep["results"].items():
        iv, sig, stop, cost = [x.strip() for x in k.split("|")]
        p = v["pooled"]
        wr = "-" if p["win_rate"] is None else f"{p['win_rate']*100:.0f}%"
        ex = "-" if p["expectancy_net_pct"] is None else f"{p['expectancy_net_pct']:+.2f}%"
        L.append(f"| {iv} | {sig} | {stop.replace('stop ', '')} | {cost} | {p['trades']} | {wr} | {ex} | ${p['total_net_usd']:+.0f} | "
                 f"{p['max_drawdown_usd']:.0f} | {p['buy_hold_avg_net_pct']:+.1f}% | ${p['buy_hold_total_usd']:+.0f} |")
    L += ["", "## Data windows", ""] + [f"- {k}: {v}" for k, v in rep["spans"].items()]
    L += ["", "## Honest limits", "",
          "- Majors only (Binance.US). Solana memecoins on DEX pools are not tested here (DEX candle sources are often rate-limited).",
          "- Windows are short (~10 days of 5m, ~80 days of 1h) and one regime; nothing here is significant out of sample.",
          "- Fills at the next open with the cost as the only friction; real slippage on a fast move can be worse.",
          "- The 5% stop is a stand-in; the original bot's stop and exit rules were not inspected (repo not downloaded).", ""]
    return "\n".join(L)


def selftest() -> list[tuple[str, bool, str]]:
    rows = []

    def add(n, ok, d=""):
        rows.append((n, bool(ok), str(d)[:200]))
    now = 10_000_000
    # V-shape: down 60 candles, then up 40 -> one A entry on the cross, held to the end (no cross down), positive gross
    px = [100 - 0.5 * i for i in range(60)] + [70 + 0.8 * i for i in range(40)]
    cs = [(now - 3600 * (len(px) - k) - 3600, p, p * 1.002, p * 0.998, p) for k, p in enumerate(px)]
    r = backtest(cs, 3600, nq.signal_a, 0.25, 3.0, now)
    add("V-shape: signal A trades, entry at next open, net = gross - 3.0%", r["trades"] >= 1 and all(
        abs(t["net_pct"] - (t["gross_pct"] - 3.0)) < 1e-6 for t in r["trade_list"]), r["trade_list"][:1])
    r19 = backtest(cs, 3600, nq.signal_a, 0.25, 1.9, now)
    add("lower cost (1.9%) gives strictly better net expectancy on identical trades",
        r19["trades"] == r["trades"] and (r["trades"] == 0 or r19["expectancy_net_pct"] > r["expectancy_net_pct"]))
    # stop: take the V-shape trade, then put a gap-down candle right after the entry candle
    t0 = r["trade_list"][0]["entry_ts"] if r["trades"] else None
    k0 = next((k for k, c in enumerate(cs) if c[0] == t0), None)
    if k0 is not None:
        ent = cs[k0][1]
        cs2 = cs[:k0 + 1] + [(cs[k0][0] + 3600, ent * 0.80, ent * 0.80, ent * 0.70, ent * 0.75)]
        cs2 += [(cs2[-1][0] + 3600 * (1 + k), ent * 0.75, ent * 0.751, ent * 0.749, ent * 0.75) for k in range(5)]
        r2 = backtest(cs2, 3600, nq.signal_a, 0.05, 3.0, cs2[-1][0] + 7200)
        st_ = [t for t in r2["trade_list"] if t["exit"] == "stop"]
        add("gap below the stop fills at the open (-20%), not at the -5% stop", st_ and abs(st_[0]["gross_pct"] + 20.0) < 1e-6, r2["trade_list"])
    else:
        add("gap below the stop fills at the open (-20%), not at the -5% stop", False, "no base trade")
    # forming candle is excluded
    forming = cs + [(now - 100, 1, 1000, 0.1, 1000)]
    add("forming candle never used (identical result with/without it)",
        backtest(forming, 3600, nq.signal_a, 0.25, 3.0, now)["trade_list"] == r["trade_list"])
    # drawdown math
    s = summarize([{"net_pct": 10, "gross_pct": 13, "exit": "x", "exit_ts": 1}, {"net_pct": -20, "gross_pct": -17, "exit": "x", "exit_ts": 2},
                   {"net_pct": -5, "gross_pct": -2, "exit": "x", "exit_ts": 3}], [(0, 1, 1, 1, 1.0), (1, 1, 1, 1, 1.1)], 3.0)
    add("max drawdown on fixed $100 stakes = $25 (peak +10, trough -15)", s["max_drawdown_usd"] == 25.0 and s["buy_hold_net_pct"] == 7.0, s)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--pages5m", type=int, default=3)
    ap.add_argument("--pages1h", type=int, default=2)
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    a = ap.parse_args(argv)
    if a.selftest:
        rs = selftest()
        for n, ok, d in rs:
            print("PASS" if ok else "FAIL", n, "" if ok else f":: {d}")
        return 0 if all(r[1] for r in rs) else 1
    rep = run([s.strip().upper() for s in a.symbols.split(",") if s.strip()], {"5m": max(1, min(a.pages5m, 6)), "1h": max(1, min(a.pages1h, 3))})
    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    json.dump(rep, open(OUT_JSON, "w"), indent=1, default=str)
    md = render(rep)
    open(OUT_MD, "w").write(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
