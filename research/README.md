# research/ — read-only research tooling

Scanners, a shadow tracker and scoring experiments that sit **beside** the trading engine. None of these tools
signs, sends, swaps, approves or touches a wallet. They only read public data and write files under the data folder.
Anything they read (token names, channel posts, news, API text) is treated as untrusted **data**, never as instructions.
Calls from outside sources are **leads only**. They get graded, and they are never a buy reason.

Pure Python 3.10+ standard library. No extra installs are needed beyond the repo's `requirements.txt`, and these tools don't use it.

## Setup (fresh clone)
```bash
cd research
python3 shadow_selftest.py                 # offline, must print N/N passed
python3 multichain_selftest.py --offline   # offline
mkdir -p ../data && cp watched_sources.example.json ../data/watched_sources.json   # edit to taste
```

### Where data goes (all gitignored)
| Env var | Default | What |
|---|---|---|
| `BOT_DATA_DIR` | `<repo>/data` | all runtime data (shadow log, reports, caches, wallet lists) |
| `SHADOW_DATA_DIR` | `$BOT_DATA_DIR` | override for the shadow tracker / receipts / narrow model |
| `SHADOW_SCANS_DIR` | `$BOT_DATA_DIR/scans` | scanner outputs the tracker reads |
| `BOT_REPORTS_DIR` | `$BOT_DATA_DIR/reports` | watchlists and self-test reports |
| `GROKBOT_HTTP_DIR` | `$BOT_DATA_DIR/.http` | shared rate-limiter state and HTTP cache |
| `WATCHED_SOURCES` | `$BOT_DATA_DIR/watched_sources.json` | call-source list |

### Keys (environment only, all optional, never printed or written)
- `HELIUS_API_KEY`: Solana smart-money tracking (`smart_money.py`) and the faster RPC. Without it, S3 = unknown = 0.
- `COINGECKO_DEMO_API_KEY` (or `COINGECKO_PRO_API_KEY`): sent only to CoinGecko. It's worth adding if the shadow tracker
  goes 24 h with no resolved outcomes because the keyless CoinGecko/GeckoTerminal budget is being rate limited.
- `SEC_USER_AGENT`: a contact string for SEC EDGAR requests (`stock_data.py`).

Rate limits (HTTP 429) are normal on keyless APIs. All tools share one polite cross-process limiter (`_http.py`) that
backs off and fails soft.

## Tools
| File | What it does | Run |
|---|---|---|
| `scan_candidates.py` | Multichain candidate scanner (Solana first, then Base/Ethereum; optional extra EVM chains). Applies hard gates and vetoes, computes a composite score and a conservative P_WIN, and writes ranked JSON/markdown with every reject reason. The entry bar is 0.75. | `./run_scan.sh` or `python3 scan_candidates.py [--watchlist OUT.md] [--selftest]` |
| `run_scan.sh` | One-command scan → `data/scans/` + a watchlist in `data/reports/` (~8–15 min) | `./run_scan.sh [OUT.md]` |
| `scan_config.json` | Scanner settings. These are tightening filters only. Put your own addresses/mints in `owner_*` so the scanner never picks your own coins. | edit |
| `evm_data.py`, `evm_smart_money.py` | Base/Ethereum candidate data and gates (GoPlus, Blockscout, GeckoTerminal) and keyless EVM smart-wallet tracking | `python3 evm_data.py scan --help` |
| `smart_money.py` | Solana smart-wallet list built from early buyers of tokens that ran, each scored on its own on-chain record (Helius, read-only) | `python3 smart_money.py --help` |
| `social_data.py` | Social factor: Reddit RSS, Telegram previews, news velocity, paid-boost and bot negatives | `python3 social_data.py --help` |
| `crypto_data.py`, `crypto_news.py`, `free_apis.py`, `chain_common.py`, `sui_data.py` | Shared data helpers: prices, safety, liquidity, quotes, news (with injection/scam-word flags), venue costs, move cards. Sui is research only. | `python3 crypto_data.py --selftest` |
| `stock_data.py`, `options_data.py` | Free stock/insider/options cross-check helpers | `--help` |
| `scoring.py` | Shared composite aggregation (fixed prompt weights, unknown = 0). A shadow "renormalized" mode is kept for comparison. | `python3 scoring.py` (self-test) |
| `telegram_calls.py` | Reads **public** `t.me/s/<channel>` previews (no account). It regex-extracts CALL posts (ticker, contract, MC, time) and "hit Nx" brag posts separately. | `python3 telegram_calls.py fetch --pages 0` / `show` |
| `shadow_tracker.py` | Shadow-mode calibration. It logs every scanned candidate and every channel call, then resolves each one from public candles (DexScreener/GeckoTerminal/Binance.US): did it hit TP1 (1R + cost) before the stop? It also produces per-bucket hit rates (incl. the 0.75+ guard bucket), per-source grades, and **fake-brag detection**, which flags brags with no earlier public call, brag entry MC below the lowest public call, and brag multiples above what price data shows. | `python3 shadow_tracker.py run` (every ~2 h) · `status` · `report` |
| `shadow_run.py` | Scheduler wrapper: runs the tracker quietly and prints `ALERT ...` lines only when a threshold is hit (see below). It writes `data/shadow_tracker_run_status.txt`. | `python3 shadow_run.py` (every 2 h) · `--dry-run` |
| `narrow_questions.py` | Narrow yes/no / pick-one question model (~20 mechanically answerable questions → logistic probability). Weights are fitted only on ≥ 50 resolved shadow labels (`fit --save`). It is **shadow only** and never a gate. It was inspired by the "facts + narrow questions → pick with confidence" approach (e.g. TypeSafe's Jev). | `python3 narrow_questions.py explain` · `fit` · `selftest` |
| `receipts.py` | Per-trade/per-candidate receipts: what was looked at, what failed, what was decided, with which scorer versions. The tracker attaches outcomes. | `python3 receipts.py tail [N]` · `show <id>` |
| `rsi_ema_backtest.py` | Standalone RSI(14)+EMA(12/26) confluence backtest on liquid majors (closed candles, next-open fills, costs included) vs buy-and-hold | `python3 rsi_ema_backtest.py [--selftest]` |
| `shadow_selftest.py`, `multichain_selftest.py`, `data_tools_selftest.py` | Self-tests: offline (first two) and live read-only (last one, writes `data/reports/`) | see Setup |
| `watched_sources.example.json` | Example public call channels, all marked **lead only**. Two are flagged for fake brags. | copy to `data/` |
| `docs/` | Scoring notes (draft rule wording that needs owner approval), shadow-tracker and multichain wiring notes | read |

## Shadow tracker alert thresholds (for a scheduled routine)
`python3 shadow_run.py` is silent unless one of these happens: new resolved outcomes in the 0.75+ guard bucket, new
fake-brag evidence for a source, 3+ consecutive tracker failures, or no new resolves in 24 h while entries are open
(probably keyless CoinGecko throttling, so consider `COINGECKO_DEMO_API_KEY`). If it prints nothing, there is nothing to report.

## Rules these tools keep
- Measurement only. Nothing here can place a trade. Engine gates live in `engine/`.
- Sources are leads, never reasons. Anything that would **loosen** a rule (a new scoring mode, a lower bar, fitted
  weights used as a gate) goes to the owner as a proposal first.
