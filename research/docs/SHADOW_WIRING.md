# Shadow tracker wiring notes (2026-09-25)

`research/shadow_tracker.py` reads the scanner's saved outputs only (`data/scans/scan-*.json`, `latest.json` is resolved
through its symlink and never double-counted). It does not import or edit `scan_candidates.py`, `scan_config.json` or
`data_tools_selftest.py`. Schedule: `python3 research/shadow_tracker.py run` every ~2 h (quiet; exit 0 unless a code error),
ideally a few minutes after a scan finishes. Self-tests: `python3 research/shadow_selftest.py` (offline).

## What the tracker reads from each candidate record
- Identity: `chain` (defaults to `solana` for old scans), `mint` (also accepts `address`/`contract`/`token_address`), `symbol`, `name`, `pair`, `cls`, `major_symbol`.
- Verdict: `status` (PASS / WATCH / REJECT / NOT DEEP-CHECKED) from `passes`, `watch`, `rejects` (and a future `chains.<chain>.{passes,watch,rejects}` nesting).
- Entry: `plan.reference_price`, else `price_usd`. Scan time: `generated_et`.
- Levels: `plan.stop`, `plan.tp1`, `p_win.stop_pct/tp1_pct/estimate/posterior` (PASS/WATCH). Rejects have none of these, so the tracker re-derives them (marked `derived`).
- Costs: `costs.phantom_app_pct` (Solana) or the cheapest EVM venue in `costs`, else derived from §2E.8.
- Factors: `scores.*`, `price_change`, liquidity/volume/holders/organic, `t9_wash_p`, `c2/c3`, failed check ids, `smart_money.level`, `social.level`.

## Small hooks in scan_candidates.py that would help later (NOT applied)
1. **Save stop/TP1/cost for every priced reject.** Today rejects stop before `score()`, so the tracker must use the
   no-candle proxy stop (`choose_stop(cls, None, price_change)`) instead of the scanner's candle-based volatility stop.
   Suggested: after `evaluate()`/`deep_checks()`, for any record with `price_usd`, add
   `rec["shadow"] = {"stop_pct": b*100, "tp1_pct": (b + cost)/1*100, "cost_pct": cost, "stop_method": note,
   "p_prior": b/(a+b)}` (using the same `choose_stop` / `venue_costs` it already has; cost may be an estimate). The tracker
   will prefer `rec["shadow"]` over its own derivation automatically once that key exists (see below).
2. **Record the price timestamp and source per candidate** (`price_ts`, `price_source`: jupiter / dexscreener / geckoterminal).
   Entry is currently pinned to `generated_et`, which can be up to ~6 min after the price was read.
3. **Keep the pool address even when DexScreener is throttled** (e.g. from Jupiter/GeckoTerminal). 04:17 and 10:15 scans have
   `pair: null` for most records, which costs the tracker an extra pool lookup per 30 tokens.
4. Optional: call `shadow_tracker.py run` at the end of `run_scan.sh` (after the scan's JSON is written), wrapped in
   `|| true` so tracker problems never affect a scan.

The tracker already accepts a `shadow` block on a record: if `rec["shadow"]` has `stop_pct` and `tp1_pct`, those levels
are used with `levels_source = "scanner-shadow"`.

## Watched call sources (added 2026-09-25 ~11:50 AM ET)
- Config: `data/watched_sources.json` (one object per source; `type: "telegram"`, `channel`, `call_keywords`,
  `stop_pct` 25, `max_hold_h` 72, `min_age_h` 24, `dedupe_h` 24, `backfill_pages_per_run` 40). Add channels by appending.
- Collector: `research/telegram_calls.py` (public `t.me/s/<channel>` preview via `_http`, telegram group 2 s spacing, newest
  page cached 15 min, older pages 24 h). Parsed calls: `data/src_<id>_calls.jsonl`; brag posts: `data/src_<id>_brags.jsonl`;
  cursor state: `data/telegram_state.json` (backfill resumes from `oldest_id`).
- `shadow_tracker.py run` fetches sources, logs each call as `source = "tg:<channel>"`, `verdict = CALL`, entry = price of the
  5-minute candle at the post time (flagged `first_candle_after_post` when no candle exists at that moment), and resolves with
  the same TP1-before-stop rules plus `max_mult_24h` and `price_24h_ratio`. Calls are only checked once they are ≥ 24 h old
  (one candle call then usually settles them), newest first. GeckoTerminal calls reserved for sources: `--max-gt-src` (8).
- Channel text is untrusted data: only regex fields (ticker, MC, contract, time, id) are stored, plus a 160-char excerpt.
  Nothing in this pipeline can trade.
