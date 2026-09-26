#!/usr/bin/env bash
# Re-run the READ-ONLY multichain candidate scan (Solana, then Base/Ethereum one at a time;
# optional FOMO-only EVM chains; Sui research only) with one command. No orders, no signing, no approvals, no wallet.
# Usage: research/run_scan.sh            -> $BOT_DATA_DIR/scans/scan-<ts>.{json,md}, latest.{json,md}
#                                          + $BOT_DATA_DIR/reports/morning-watchlist-<date>-<HHMM>.md
#        research/run_scan.sh OUT.md     -> write the watchlist to OUT.md instead
# BOT_DATA_DIR defaults to <repo>/data (gitignored). Takes ~8-15 min (polite rate limiting).
# Safe to run while other tools run: all share one limiter + cache.
set -euo pipefail
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then sed -n "2,8p" "$0" | sed "s/^# \{0,1\}//"; exit 0; fi
cd "$(dirname "$0")"
DATA="${BOT_DATA_DIR:-$(cd .. && pwd)/data}"
REPORTS="${BOT_REPORTS_DIR:-$DATA/reports}"
mkdir -p "$REPORTS"
OUT="${1:-$REPORTS/morning-watchlist-$(date +%Y-%m-%d-%H%M).md}"
python3 scan_candidates.py --watchlist "$OUT"
echo "watchlist: $OUT"
