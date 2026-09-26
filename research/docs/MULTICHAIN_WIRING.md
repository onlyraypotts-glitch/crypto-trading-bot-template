# Multichain wiring spec (2026-09-25)

Status (updated 2026-09-25, late morning): **APPLIED.** scan_candidates.py now runs Solana first, then the EVM chains in
`manual_card_chains` one at a time (evm_data.scan with a shared news/social context), merges the records by `chain`, and
renders EVM cards with chain_common.move_card. scan_config.json has the keys from §3, and data_tools_selftest.py runs
multichain_selftest.offline(). New since the first draft: social_data.py (S4), scoring.py (shared aggregation;
SCORING_NOTES.md), GeckoTerminal fallback pairs when DexScreener is limited, batched GoPlus for EVM chains, EVM liquidity
snapshots (S1 evidence), and _http.py host rules (§4, applied). Backups: *.bak-multichain-wiring-2026-09-25.

Everything here is read-only: nothing signs, sends, swaps, approves or connects a wallet. EVM and Sui RPC calls go
through allow-lists of read-only methods (`evm_data.rpc`, `sui_data.rpc`).

## Chain eligibility (source of truth: chain_common.CHAINS / VENUE_SOURCES)

| chain | CAIP-2 | Phantom MCP autonomous | Phantom app manual | FOMO manual | cards? |
|---|---|---|---|---|---|
| solana | solana:mainnet | yes (existing scanner) | yes | yes | yes |
| base | eip155:8453 | yes (H19 approval exception on sells) | yes | yes | yes |
| ethereum | eip155:1 | yes (gas usually makes <$250 fail H17) | yes | yes (min sell $5) | yes |
| bsc | eip155:56 | no (Phantom doesn't support BNB) | no | yes | yes, FOMO-only |
| monad | eip155:143 | no (Phantom deprecated Monad) | no | yes | yes, FOMO-only |
| robinhood | Robinhood Chain | no (not in MCP `buy`) | yes | yes | yes. Liquidity data is sparse, so most records are UNKNOWN and become WATCH |
| sui | sui:mainnet | no (Phantom ended Sui 2026-09-24) | no | no | **no**: research only (`sui_data.py`) |

Manual-mode (FOMO) cards are first-class: every FOMO chain gets scanned and can produce cards even where Phantom
auto-trading can't trade. H19 (exact-amount sell approvals) applies only to autonomous EVM trades, not to manual FOMO trades.

## 1. Scanner call (scan_candidates.run)

After the Solana scan builds `ctx` (news already fetched once):

```python
import evm_data, evm_smart_money, chain_common as cc
evm_chains = cfg.get("manual_card_chains_evm", ["base", "ethereum", "bsc", "monad", "robinhood"])
evm = evm_data.scan(evm_chains, cfg.get("evm", {}), news=ctx["news"], smart_money=evm_smart_money if cfg.get("evm_smart_money_enabled", True) else None)
# merge: Solana records have chain="solana" (add it if missing); EVM records carry chain + caip2 + venues + security
res["passes"] += evm["passes"]; res["watch"] += evm["watch"]; res["rejects"] += evm["rejects"]
res["sources"] += evm["sources"]; res["crash_regime_evm"] = evm["crash_regime"]
```

- Sort the merged list by `(status=="PASS", p_win.estimate, scores.composite)`, the same key both scanners use.
- An autonomous PASS needs `chain in cfg["evm_chains_auto"]` (base, ethereum). Any other PASS is manual-card only.
- H1–H3 counters (daily loss, open positions, trades per day) are shared across all chains. The scanner must not
  size positions per chain independently.
- EVM crash regime: `evm_data.scan` downgrades PASS→WATCH if the 60-min change of the chain's gas token
  (ETH for Base/Ethereum/Robinhood Chain, BNB, MON) is ≤ −5% or UNKNOWN (DefiLlama `coins.llama.fi/percentage`).

## 2. Cards

`chain_common.move_card(rec, n, venue=None, equity_note=...)` renders the prompt's §2E.4 card for any EVM record. Each card includes:
- the chain line and CAIP-2 id, and the FULL contract ("never choose by ticker", with a warning about same-ticker tokens on other chains);
- the amount (≤ PERMITTED_MAX, T7-half) and the venue's minimum viable size for the 3% round-trip cap;
- FOMO steps: Search → paste the contract → check the chain and match the contract character by character → WARNING
  label = no buy → custom amount → slippage (FOMO doesn't document a slippage control, so the card says "if offered;
  otherwise check the quote") → fee check;
- where to sell: FOMO profile → open positions → Sell %. Minimum sell is $2, or $5 on Ethereum. There are no spot
  stop/limit orders, so the bot alerts at levels;
- for the Phantom app: an exact-amount approval warning;
- a cost line and news context: token headlines or danger words, plus chain headlines from crypto_news via `cc.news_context`.

Sui records return `NO CARD … research only`. For Solana, keep the existing card renderer, but add the `Chain Solana
(solana:mainnet)` line and the news line so every card has the same format.

## 3. Proposed scan_config.json keys (not applied)

```json
"evm_chains_auto": ["base", "ethereum"],
"manual_card_chains": ["solana", "base", "ethereum", "bsc", "monad", "robinhood"],
"evm": {"max_tax_pct": 5, "deep_check_limit": 8, "quote_usd": 100, "entry_bar": 0.75,
        "owner_addresses": ["<the owner's Phantom + FOMO EVM addresses>"], "owner_mints": []},
"evm_smart_money_enabled": true,
"sui_research_enabled": false
```

## 4. _http.py HOST_RULES to add (polite limiter)

```python
("api.honeypot.is", "honeypot", 1.0, 300),
("blockscout.com", "blockscout", 0.6, 300),      # base.blockscout.com / eth.blockscout.com, keyless
("api.gopluslabs.io", "goplus", 2.5, 600),       # returned code 4029 (rate limit) repeatedly today
("coins.llama.fi", "defillama", 0.3, 60),
("graphql.mainnet.sui.io", "sui", 0.5, 300),     # Sui public JSON-RPC is deprecated -> GraphQL
("publicnode.com", "evm-rpc", 0.2, 30), ("mainnet.base.org", "evm-rpc", 0.2, 30), ("rpc.monad.xyz", "evm-rpc", 0.2, 30),
```

DexScreener and GeckoTerminal already have rules. Both were returning 429 from a shared IP during testing
(shared per-IP budget with the Solana scans), so run the EVM scan after the Solana scan, not in parallel.

## 5. Self-tests

In data_tools_selftest.py: `import multichain_selftest as mt; mt.offline(); rows += mt.ROWS` (34 offline
checks, no network). Keep the live part optional.

## 6. Keep in sync

chain_common.py has **copies** of the scan_candidates pure functions (classify_price_action, trend_stage,
choose_stop, backtest, beta_summary, p_win, realized_vol_daily), to avoid importing a file that's being edited.
After the smart-money work lands, either import them from scan_candidates or diff them when you change either copy.

## 7. Seeding EVM smart money

`python3 evm_smart_money.py build base` (Blockscout, keyless; spaced calls) writes data/evm_smart_wallets.json.
Until then, S3 for EVM is UNKNOWN (level 0). Nothing in S3 ever enables a buy on its own. BNB and Monad have no keyless
explorer wired, so S3 stays UNKNOWN there. A free Etherscan V2 key would add Ethereum only; Base needs a paid plan.

## 2026-09-25 ~12:10 PM ET addendum: fallback discovery and crash guards
- `evm_data.fallback_discover` (Base/Ethereum): runs only when both DexScreener and GeckoTerminal failed for that chain.
  It uses Blockscout's token list, the deepest pool from honeypot.is GetPairs, and DefiLlama's 1 h/6 h/24 h % change;
  the v3 pool fee comes from a read-only `fee()` eth_call. Volume is token-wide (includes CEX), there are no candles,
  so P_WIN stays at its prior. It finds established tokens only, not fresh launches. Toggle: scan_config `fallback_discovery`.
- Fixed a latent crash in scan_candidates T10 (`"3% at $%d" % ...` raised TypeError the first time a coin reached T10;
  it killed scan 2 at 12:16 PM). Deep checks and scoring are now wrapped per record (Solana and EVM), so one bad record
  becomes a REJECT with the error instead of ending the scheduled scan.
