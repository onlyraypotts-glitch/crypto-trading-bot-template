# Scoring notes: why nothing reached 0.75, and what changed (2026-09-25)

Written for the owner and whoever maintains the scanners. Code: `scoring.py` (aggregation), `social_data.py` (S4),
`scan_candidates.py` / `evm_data.py` (both use the same functions). Nothing here changes the 0.75 bar, the prompt's
weights, or any hard limit.

## What was blocking every coin

1. **S4 social was hard-coded "UNKNOWN = 0".** COMPOSITE = 0.30·S1 + 0.25·S2 + 0.20·S3 + 0.15·S4 + 0.10·E (prompt §2C.4),
   so the maximum was 0.85, or 0.65 when S3 was also unknown.
2. **Three confidences were constants.** C2 and C3 were always 0.65 and C5 always 0.60. The prompt's DECISION CONFIDENCE
   is the *lowest* of S1–S4, C1, C2, C3 and C5 (§2C.5), so it could never exceed 0.60, whatever the evidence. This was the
   structural blocker: even a perfect coin would fail.
3. **Any unknown factor sat at 0.5 inside that minimum.**

## What changed

### a. A real, free social/attention source (S4)
Checked on 2026-09-25, without keys:

| Source | Works keyless? | Used for |
|---|---|---|
| Reddit search **RSS** (`www.reddit.com/search.rss`) | **Yes** (the JSON API returns 403) | posts in 24 h vs the 24–72 h baseline, distinct authors; one author posting most items = bot-like |
| Telegram public previews (`t.me/s/<channel>`) | **Yes** | subscribers, posts in 24 h, views per subscriber (< 2% with ≥ 5k subs = botted), growth between scans. The channel comes only from the token's own listed links |
| crypto_news.py items | Yes | mentions in 24 h vs the prior 24 h, distinct outlets (items flagged as possible injection are ignored) |
| CoinGecko `/search/trending` + `/coins/list?include_platform` | Yes (rate-limited, cached 30 min / 24 h) | trending driven by user searches, matched by contract, never by ticker |
| GeckoTerminal trending pools, Jupiter top-trending | Yes (already in discovery) | counted together as at most ONE "aggregator" platform, because they partly reflect trading, not people |
| DexScreener boosts / `orders/v1` | Yes (DexScreener often rate-limits shared IPs) | **negative**: paid promotion |
| X/Twitter followers or search | **No** (login or paid API) | not used |
| LunarCrush, Santiment, CryptoPanic | No (keys) | not used |

Grading follows the prompt's S4 wording (0 none or botted · 1 one source · 2 rising, low quality · 3 accelerating,
independent, 2+ platforms · 4 that, before retail saturation):
- Any bot-like platform sets S4 to 0.
- Paid promotion never counts as a platform, caps S4 at 1 unless 2+ independent organic platforms are rising, and costs a
  level even then.
- Level 4 needs 3+ rising independent platforms and no saturation. Saturation means an established coin, a market cap
  ≥ $500M, or already on CoinGecko trending.
- S4 confidence = 0.5 + 0.3 × (share of sources actually read), so the maximum is 0.8. A Reddit lookup skipped for the
  per-scan budget counts as not read.
- If nothing is readable, S4 is UNKNOWN = 0 at confidence 0.5, exactly as before.

### b. Confidence aggregation, reviewed
- **The constants are gone.** C2 and C3 confidence comes from two independent price views: the pair source's % changes and
  our own hourly candles. 0.80 if they agree, 0.55 if they conflict, 0.65 with one view only (the old value). C1 rises
  to 0.75 only if 72 h of candles agree with the stage's direction, and drops to 0.45 if they contradict it. C5 is computed
  from S1, S2 and C2, so its confidence is min(S1, S2, C2).
- **The gate still follows the prompt**: fixed weights, UNKNOWN = 0, decision confidence = the minimum. Why not switch to
  "treat missing as missing, renormalize over known factors, apply an explicit haircut"? That method is more defensible
  statistically, but it **loosens** buying compared with the prompt. With S3 unknown, a coin at S1 4 / S2 4 / S4 2 moves
  from 0.675 to about 0.76. The prompt says weights may change only to make buying stricter, and the confidence rule is
  written into §2C.5. A scanner that quietly used a looser rule would print cards the prompt-following bot has to call
  non-BUY. So the adjusted method is **computed on every card as a SHADOW** ("shadow adjusted" composite and confidence)
  and becomes the gate only if `scan_config.json` → `"scoring_mode": "renormalized"`.
- Shadow formula: adjusted = (Σ known w·s + 0.10·E) / (1 − missing weight) × (1 − 0.5 × missing weight). With S4
  missing the multiplier is ×0.925; with S3 and S4 missing it's ×0.825. Adjusted confidence = the lowest *known*
  confidence × the same haircut. Missing data always costs something, but never blocks on its own. A self-test confirms a
  mediocre coin (S1 2, S2 2) can't reach 0.75 this way.
- **Draft §2C wording if the owner wants the adjusted method as the gate** (needs his approval, a changelog entry and a
  checksum update): "If S3 or S4 cannot be measured (no data, not a measured 'none'), exclude it: COMPOSITE = (sum of the
  measured weighted terms) ÷ (1 − missing weight) × (1 − 0.5 × missing weight), and DECISION CONFIDENCE = the lowest
  confidence among the measured answers × the same factor. A measured 'none' is level 0 with its own confidence. The bar
  stays 0.75."

### c. Honest limits
- **None of these numbers are calibrated.** Levels, confidences and P_WIN are self-answered heuristics until 20+ closed
  trades are logged. After that, the prompt's §2C.9 audit compares outcomes with the bar and may raise it
  (0.75 → 0.90 in 0.05 steps).
- Passes stay rare by design. P_WIN (a backtest of the same stop/TP1 against a martingale prior) is usually below 0.75 for
  memecoins. S1 ≥ 3 needs a liquidity snapshot 20–90 min old, so the first scan of the day can't pass S1 for new tokens.
  S3 needs scored smart wallets (Solana: Helius; EVM: `evm_smart_money.py build base`).
- The social data is thin: no X, Reddit is capped per scan, and Telegram needs the token's own link. A coin can look
  "quiet" simply because its community lives on X.

## Self-tests
`multichain_selftest.py` (offline part, also run inside `data_tools_selftest.py`) covers:
- the scoring aggregation (prompt rule vs shadow, haircut, "mediocre coins stay below 0.75");
- evidence-based confidences (agree, conflict, one view, and the C5 cap);
- Reddit/Telegram parsers, bot and paid negatives, S4 levels 0/3/4, S4 confidence;
- news velocity ignoring injection-flagged items;
- the GeckoTerminal fallback pair and a full render of a multichain card.

## Narrow questions, receipts, RSI/EMA test (added 2026-09-25 ~5 PM ET) — SHADOW ONLY

Code: `narrow_questions.py` (questions, logistic model, fit), `receipts.py` (per-record receipts),
`rsi_ema_backtest.py` (standalone backtest). Wiring: `scan_candidates.py` (`add_narrow`, `rec["ta"]`, card line,
receipts), `shadow_tracker.py` (logs `narrow`/`narrow_p`/`receipt_id`, writes `data/receipt_outcomes.json`).
**Nothing here changes PASS/WATCH/REJECT, the 0.75 bar, the ranking, the prompt text or its checksums.**

### Narrow questions (21, each answered mechanically; unknown = 0 = neutral)
Pick-one: liquidity band (<100k / 100k–250k / 250k–1M / ≥1M) · top-10 non-pool share (≤20 / 20–40 / >40%) · pool age
(<24 h / 1–7 d / 7–90 d / >90 d) · 1 h change (≤−5 / −5..5 / 5..25 / >25% = chase) · 24 h volume ÷ liquidity (<0.5 / 0.5–5 /
5–40 / >40 = wash-like) · market cap (<1M / 1–10M / 10–100M / ≥100M) · RSI(14) on the last closed hourly candle
(<30 / 30–50 / 50–70 / >70).
Yes/no: 6 h change ≥ +60% · a tracked smart wallet bought in the lookback (measured only) · mint authority revoked / not
mintable · freeze authority revoked (Solana only) · LP ≥ 90% locked/burned · paid promotion (DexScreener boosts/ads) ·
measured S4 ≥ 3 · Jupiter organic = high · holders grew over 24 h · insider cluster / fresh wallets ≥ 15% · address ends in
"pump" · EMA12 > EMA26 (closed hourly) · RSI+EMA confluence in the last 3 closed hourly candles · measured round trip ≤ 2%.

Model: p = sigmoid(b + Σ w·x); yes/no x = +1/−1 (0 unknown), pick-one = one-hot (all 0 unknown).
**Hand-set neutral priors (`narrow-prior-v1`)**: intercept b = logit(0.42) ≈ −0.323 (≈ the martingale prior b/(a+b) for
TP1 = 1R + ~3% cost); weights are tiny (|w| ≤ 0.15) and signed by the prompt's own logic (thin liquidity, >40% top-10,
<24 h pools, chase, wash-like turnover, paid promotion, clusters and <$1M caps slightly negative; deep liquidity,
revoked authorities, locked LP, smart-money buys slightly positive; RSI/EMA/cost ±0.05). Theoretical range: every answer
favourable ≈0.75, every answer bad ≈0.11; replayed on the 13:53 scan the 138 records spanned 0.26–0.61 (median 0.44).
These are orderings, **not calibrated probabilities**.

Fit: `python3 narrow_questions.py fit [--save]` uses resolved shadow entries (scanner entries + channel calls with ≥ 3
answered questions; channel calls only know market cap and the "pump" suffix, so they rarely qualify). L2-penalised
logistic (Newton/IRLS, λ = 1, intercept unpenalised), time-ordered hold-out (oldest 70% train, newest 30% test). Reports
accuracy, Brier, log-loss, AUC and calibration buckets for the fitted model, the priors, a constant base rate, the
scanner's p (P_WIN / derived prior) and the composite (not a probability; accuracy at the 0.75 bar). **Below 50 labels it
prints "too few labels" and reports nothing**; the 50 floor can be raised, never lowered. A saved fit
(`data/narrow_model.json`) is still shown only as a shadow number. Labels: WIN = 1; LOSS/AMBIGUOUS/RUGGED/NO_DATA/EXPIRED
= 0 (the tracker's convention). Old entries without `rec["narrow"]` are answered from their saved factor readings
(post-entry data is never used). Status 2026-09-25 17:00 ET: 1 resolved label → too few labels.

### Draft prompt wording (NOT applied; needs the owner's approval, a changelog entry and a checksum update)
Only if a fit on ≥ 200 resolved labels beats the scanner's P_WIN on the hold-out (lower Brier and higher AUC) would a gate
make sense, and only as a STRICTER extra condition: "§2C.10 (optional, owner-approved): the scanner's fitted
narrow-question probability NARROW_P must also be ≥ 0.75 for a BUY. NARROW_P never replaces COMPOSITE, DECISION
CONFIDENCE or P_WIN and can only block, never enable, a buy. If NARROW_P is unavailable or its model has fewer than 200
resolved labels, this rule is skipped (it never loosens anything)." The bar stays 0.75.

### Receipts
Every record that reaches deep checks (so every move/watchlist card) appends one line to `data/receipts.jsonl`
(~1.0–1.4 kB): rid, time, chain, token, per-record sources OK/failed, run-level failed sources and rate-limited
groups, hard-rule count + failed ids, factor levels, composite, confidence, P_WIN, narrow p, decision (+ first reason or
needs), card file(s), and scorer versions (short file hashes + narrow model version). rid = the shadow-tracker entry id
for the same record, so `shadow_tracker.py` writes the eventual outcome to `data/receipt_outcomes.json` (including
duplicates folded into an entry and entries superseded by a stronger verdict). `python3 receipts.py tail` / `show <rid>`.

### RSI(14) + EMA(12/26) confluence (own implementation; the 'Cabbage' repo was not downloaded or read)
Signal A = EMA12 crosses above EMA26 with 50 ≤ RSI ≤ 70; variant B = RSI crosses up through 30 while EMA12 > EMA26;
exit on EMA12 crossing below EMA26 or the % stop. Closed candles only; fills at the next open. Result on Binance.US majors
(2026-09-25, `data/rsi_ema_backtest.md`): **it loses after costs** in every configuration once bad-print symbols are
excluded (e.g. 1 h A: 139 trades, 15% net win rate, −2.3%/trade at 3.0% cost, −1.2% at 1.9%; buy-and-hold +31% avg over
the same window). Gross edge is about zero; the round-trip cost is the whole loss. So it stays a narrow-question feature
with a ±0.05 prior, never a gate.
