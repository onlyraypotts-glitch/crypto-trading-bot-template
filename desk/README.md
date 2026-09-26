# LET'S GET RICH TEAM — shared desk (data layer + playbooks)

Research and measurement only. Nothing in this folder trades, signs, sends, or reads secrets.

| Path | What |
|---|---|
| `desk.py` | CLI + importable module (stdlib only). `python3 desk.py --help` |
| `desk.db` | created by `python3 desk.py init` (SQLite, WAL mode; 20 tables; append-only triggers; veto + entry-bar triggers). Never committed. |
| `playbooks/protocol.md` | shared rules. **Read first.** Then your role: `hq.md`, `alpha-hunter.md`, `smart-money.md`, `crypto-radar.md`, `rug-shield.md`, `quant-lab.md`, `risk-execution.md` |
| `playbooks/schedule.md` | suggested staggered schedule for the 7 bots |
| `CRYPTO_INTELLIGENCE_OS.md` | the generic operating system every bot follows |
| `DATA_FEEDS.md` | free vs paid data inventory (snapshot; re-run the probe) |
| `tools/regime_snapshot.py` | market_regime snapshot from free feeds |
| `tools/probe_feeds.py` | re-test every data endpoint → `$DESK_DIR/data/feed_probe.json` |
| `tests/` | `python3 -m unittest discover -s desk/tests -v` (offline, no keys) |
| `reports/` | HQ Daily Desk Reports (`daily-YYYY-MM-DD.md`), created at runtime, never committed |

## Configuration (environment variables)
| Variable | Default | Meaning |
|---|---|---|
| `DESK_DIR` | the folder holding `desk.py` | where `desk.db`, `data/` and `reports/` live |
| `DESK_DB` | `$DESK_DIR/desk.db` | explicit database path (tests use a temp file) |
| `DESK_TOOLS_DIR` | `<repo>/research` | the read-only research tools (`_http.py`, `crypto_data.py`, `smart_money.py`, ...) |
| `DESK_TOOLS_DATA` | `$BOT_DATA_DIR` or `<repo>/data` | shadow-tracker / smart-wallet outputs that `seed-shadow` imports read-only |
| `DESK_TIMEZONE` | `UTC` | display timezone stored in settings (DB timestamps are always UTC) |

```
git clone https://github.com/onlyraypotts-glitch/crypto-trading-bot-template && cd crypto-trading-bot-template
export DESK_DIR="$PWD/desk"          # or any folder you like
python3 -m unittest discover -s desk/tests -v
python3 desk/desk.py init && python3 desk/desk.py stats
```

Safety rules: the `trading-safety-rules` skill (`prompts/trading-safety-rules.md`; locked; referenced, never loosened).
Refresh shadow-tracker data: `python3 desk.py seed-shadow` (reads `$DESK_TOOLS_DATA` read-only).
Loosening the entry bar below its current value is blocked by a trigger on purpose. Only the owner can decide that, outside the bots.
