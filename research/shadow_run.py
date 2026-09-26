#!/usr/bin/env python3
"""shadow_run.py: scheduler wrapper for shadow_tracker.py (run every ~2 h). QUIET unless an alert threshold is hit.

Runs `shadow_tracker.py run`, then compares data/shadow_report.json with the previous run's state and prints ALERT lines
only for:
  1. new resolved outcomes in the 0.75+ guard bucket (P 0.75-0.85)
  2. new fake-brag evidence for a source (brags with no earlier public call, brag entry MC below the lowest public
     call, or brag multiple > 1.5x what price data shows); each source is alerted once, and again only if it gets worse
  3. repeated failures (3+ consecutive non-zero tracker exits)
  4. no new resolved outcomes in 24 h while entries are open (likely keyless CoinGecko/GeckoTerminal throttling;
     consider setting COINGECKO_DEMO_API_KEY); at most once per 24 h
Writes data/shadow_tracker_run_status.txt (key=value) and data/shadow_alert_state.json. Measurement only: nothing here
trades, signs or touches a wallet.

  python3 shadow_run.py              # run tracker + evaluate thresholds
  python3 shadow_run.py --no-run     # evaluate the existing report only (offline)
  python3 shadow_run.py --dry-run    # like --no-run and does not save state (safe to try)
Exit 0 always unless this wrapper itself crashes (the tracker's exit code is recorded, not propagated).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402

ET = ZoneInfo("America/New_York")
REPORT = os.path.join(paths.DATA_DIR, "shadow_report.json")
STATE = os.path.join(paths.DATA_DIR, "shadow_alert_state.json")
STATUS = os.path.join(paths.DATA_DIR, "shadow_tracker_run_status.txt")
DAY = 86400


def _load(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def brag_score(src: dict) -> int:
    b = (src or {}).get("brags") or {}
    return int(b.get("brags_without_prior_public_call") or 0) + int(b.get("brag_entry_below_public_call") or 0) \
        + len(b.get("brag_exceeds_data_1.5x") or [])


def evaluate(rep: dict, st: dict, rc: int | None, now: float) -> tuple[list[str], dict]:
    alerts, st = [], dict(st)
    if rc is not None:
        st["consecutive_nonzero"] = 0 if rc == 0 else int(st.get("consecutive_nonzero", 0)) + 1
        if st["consecutive_nonzero"] >= 3 and st["consecutive_nonzero"] % 3 == 0:
            alerts.append(f"ALERT repeated failures: shadow_tracker exited non-zero {st['consecutive_nonzero']} runs in a row (last rc={rc})")
    if not rep:
        return alerts, st
    g = rep.get("guard_bucket_0.75_0.85") or {}
    gn, prev_gn = int(g.get("n") or 0), int(st.get("guard_075_n", 0))
    if gn > prev_gn:
        alerts.append(f"ALERT guard bucket 0.75+: {gn - prev_gn} new resolved outcome(s); now n={gn}, wins={g.get('wins')}, "
                      f"win rate={g.get('win_rate')} (Wilson95 {g.get('wilson95')})")
    st["guard_075_n"] = gn
    resolved = int(rep.get("resolved_counted") or 0)
    if resolved > int(st.get("resolved_counted", -1)):
        st["last_resolve_ts"] = now
    st.setdefault("last_resolve_ts", now)
    st["resolved_counted"] = resolved
    if int(rep.get("open") or 0) > 0 and now - float(st["last_resolve_ts"]) > DAY \
            and now - float(st.get("stale_alert_ts", 0)) > DAY:
        alerts.append("ALERT no new resolved outcomes in 24 h while entries are open: probably keyless CoinGecko/GeckoTerminal "
                      "rate limiting. Consider a free COINGECKO_DEMO_API_KEY (env only).")
        st["stale_alert_ts"] = now
    brags = dict(st.get("brag_scores") or {})
    for sid, src in (rep.get("sources") or {}).items():
        sc = brag_score(src)
        if sc > 0 and sc > int(brags.get(sid, 0)) * 1.5:
            b = src.get("brags") or {}
            alerts.append(f"ALERT fake-brag evidence {sid}: {b.get('brags_without_prior_public_call', 0)} brags with no earlier public "
                          f"call, {b.get('brag_entry_below_public_call', 0)} with entry MC below the lowest public call, "
                          f"{len(b.get('brag_exceeds_data_1.5x') or [])} above price data. Source stays lead-only.")
            brags[sid] = sc
    st["brag_scores"] = brags
    return alerts, st


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Quiet scheduler wrapper for shadow_tracker.py (alerts on thresholds only)")
    ap.add_argument("--no-run", action="store_true", help="do not run the tracker; evaluate the existing report")
    ap.add_argument("--dry-run", action="store_true", help="--no-run and do not write state/status files")
    ap.add_argument("tracker_args", nargs="*", help="extra args passed to 'shadow_tracker.py run' (after --)")
    a = ap.parse_args(argv)
    rc = None
    if not (a.no_run or a.dry_run):
        rc = subprocess.run([sys.executable, os.path.join(HERE, "shadow_tracker.py"), "run", *a.tracker_args],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    now = time.time()
    alerts, st = evaluate(_load(REPORT, {}), _load(STATE, {}), rc, now)
    for line in alerts:
        print(line)
    if a.dry_run:
        return 0
    os.makedirs(paths.DATA_DIR, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    with open(STATUS, "w", encoding="utf-8") as f:
        f.write(f"exit={rc if rc is not None else 'skipped'}\nended_et={datetime.now(ET):%Y-%m-%d %H:%M} ET\n"
                f"resolved_counted={st.get('resolved_counted', 0)}\nguard_075_n={st.get('guard_075_n', 0)}\n"
                f"consecutive_nonzero={st.get('consecutive_nonzero', 0)}\nalerts={len(alerts)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
