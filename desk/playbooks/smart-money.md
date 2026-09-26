# Smart Money playbook — wallets with a *realized*, repeatable record

Read `protocol.md` and the locked safety SKILL first.

## Mission
Keep a permanent wallet database (`smart_money_wallets`) and say which proven wallets are entering or exiting desk
opportunities. **Require a repeatable realized record, measured on-chain.** Screenshots, PnL cards, "up 50x" posts,
and unrealized gains never count (`pnl_basis` must be `realized_onchain` or `realized_exchange`; else `unverified`
and the wallet can't support a bullish verdict). One huge win isn't skill.

## What to measure per wallet
Realized PnL, win rate, median return, avg/median hold time, entry/exit timing vs price (early vs chasing), position
sizing, chains, market-cap range, current holdings, recent entries/exits, number of closed trades.
Classify (`label`): `skilled` (≥ 15 closed round trips/30d, win rate ≥ 50%, realized PnL > 0, best trade ≤ 60% of
gross profit, not a deployer: the bar in `$DESK_TOOLS_DIR/smart_money.py`), `market_maker` (two-sided, tiny
edges, very high frequency), `developer` (deployed or funded the token), `insider` (funded by the deployer or bought in
the first slot/block), `influencer` (public identity, buys before posting), `bundled` (same-slot or same-funder
clusters), `wash` (self-trading, circular flows), `lucky` (profit concentrated in 1–2 trades), `unknown`.
Only `skilled` wallets with tier `reliable`/`high` count as positive evidence. Developer/insider/bundled activity
is evidence for Rug Shield (send it there).

## Tools (template repo `research/` folder)
```
python3 $DESK_TOOLS_DIR/smart_money.py status                         # Solana tracked wallets (Helius, read-only)
python3 $DESK_TOOLS_DIR/smart_money.py token <mint> --hours 24        # which tracked wallets bought/sold; dev selling; fresh-wallet concentration
python3 $DESK_TOOLS_DIR/smart_money.py rescore | build --max-tokens 8 # refresh/extend the wallet DB (Helius credits: be frugal)
python3 $DESK_TOOLS_DIR/evm_smart_money.py build base --max-tokens 4 --per-token 20   # Base/Ethereum via keyless Blockscout
python3 $DESK_TOOLS_DIR/evm_smart_money.py wallet base 0x... ; evm_smart_money.py token base 0x... --hours 24
python3 $DESK_DIR/desk.py seed-shadow                            # re-import tools/data/smart_wallets.json into desk.db
```
Helius key loads at runtime inside crypto_data.py (never print it). BNB/Monad have no keyless explorer: record UNKNOWN.
Paid options that would add labeled PnL (not bought): Nansen, Arkham, Birdeye, GMGN. See DATA_FEEDS.md.

## Output
Verdict per opportunity: `POSITIVE` (≥ 2 independent skilled reliable wallets entered fresh and still hold),
`NEUTRAL`, `NEGATIVE` (skilled wallets exiting, or dev/insider selling), `INSUFFICIENT_DATA`.
```
python3 $DESK_DIR/desk.py verdict --opp 12 --bot smart-money --verdict POSITIVE --confidence 0.6 \
  --evidence "2 reliable wallets (win 58%/61%, 23/41 closed trades 30d, realized +28.8/+40.1 SOL) bought 1-3h ago in different slots; still holding" \
  --links "https://solscan.io/tx/..."
python3 $DESK_DIR/desk.py upsert --table smart_money_wallets --key address,chain --json '{"address":"...","chain":"solana","label":"skilled","tier":"reliable","pnl_basis":"realized_onchain","realized_pnl":28.8,"pnl_unit":"SOL_30d","win_rate":0.57,"median_return":8.7,"avg_hold":"median 604s","n_trades":23,"last_scored":"2026-09-26T16:00:00Z","evidence":"...","source_bot":"smart-money"}'
python3 $DESK_DIR/desk.py add --table wallet_events --json '{"address":"...","chain":"solana","event_type":"BUY","contract":"<mint>","usd_value":420,"tx_hash":"...","block_time":"...","source_bot":"smart-money"}'
```
"Independent" means different first-entry slots/blocks and no shared funder. Same-slot entries count as one actor.

## Handoffs
→ Alpha Hunter: 2+ skilled wallets entering a token that isn't in reports. → Rug Shield: developer/insider/bundled findings.
→ HQ: verdicts. → Quant Lab: wallet-copy strategy outcomes (the smart-money-copying strategy).

## Schedule
Every 30 min: `token` checks for open WATCH/DEVELOPING tokens only. Daily: rescore wallets older than 7 days. Weekly: `build` new seeds.
Suggested team schedule (owner's timezone, 7 days, not overnight): 8:22 AM, 2:22 PM, 8:22 PM. Within a run, use the cadences above. See `schedule.md`.
