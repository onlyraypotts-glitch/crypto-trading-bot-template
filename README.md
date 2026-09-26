# cdp-trading-bot: Coinbase CDP Server Wallet trading engine

This is an autonomous-trading engine for **Solana** (Jupiter), **Base** and **Ethereum** (CDP swap / 0x Settler buys, Uniswap V3 sells). It runs on Coinbase CDP Server Wallets. Every transaction is decoded by a fail-closed pre-sign guard before anything is signed, and a default-deny CDP policy is enforced server-side as a second layer.

**Safety defaults**
- DRY RUN is the default. For live mode, set **both** `BOT_DRY_RUN=false` and `BOT_LIVE_ACK=I-ACCEPT-LIVE-TRADING`.
- If a file named `KILL_SWITCH` exists in this folder, nothing is signed or sent.
- The cap is $100 per position (`MAX_POSITION_USD`), and the CDP policy backstop is $150 per transaction.
- Decoded minimum output must be worth at least 97% of the input at independent prices.
- EVM sells use an exact-amount approve, then the swap, then a revoke. There are never unlimited approvals, and there is no message, typed-data or hash signing.
- The engine has no transfer-out path. It can only swap to itself.

New owner? Start with **SETUP.md**.

## Layout
```
secrets_loader.py       reads secrets from env vars (optional fallback file via CDP_SECRETS_FILE); never prints values
create_accounts.py      get-or-create the two CDP accounts (default names trading-bot-evm / trading-bot-sol) -> accounts.json
balances.py             read-only native balances of the bot accounts
engine/config.py        protocol addresses, caps, DRY_RUN switch, kill-switch path; bot addresses loaded at runtime
engine/sol_tx.py        Jupiter Swap API v2 /build (raw ixs) -> unsigned v0 tx; ALTs re-fetched from chain
engine/sol_guard.py     Solana pre-sign guard (decodes every instruction)
engine/evm_swap.py      BUY: CDP swap API (native ETH -> token, 0x Settler). SELL: Uniswap V3 SwapRouter02 exactInputSingle
engine/evm_guard.py     EVM pre-sign guard (decodes calldata)
engine/prices.py        independent prices: Jupiter price v3, Coinbase spot ETH-USD, Uniswap QuoterV2
engine/executor.py      Engine: kill switch, dry run, guard, CDP send, receipt wait, trade log, approve/swap/revoke
engine/policy.py        builds the CDP policy docs from config + accounts.json; engine/cdp_policy_io.py sends them
scripts/devnet_policy_test.py testnet policy test on THROWAWAY accounts (needs admin key)
scripts/apply_policy.py       create/update + attach mainnet policies (needs admin key); --print = no API calls
scripts/dryrun_mainnet.py     real mainnet quotes + guard + tamper tests, never signs
tests/test_offline.py         offline self-tests (fixtures use randomly generated throwaway addresses)
state/                        created at runtime: trades.jsonl, evm_allowances.json, policy ids, results (gitignored)
```

## Configuration (environment variables)
| Variable | Purpose |
|---|---|
| `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET` | the bot's CDP API key. Do **not** give it the policies#manage scope |
| `CDP_WALLET_SECRET` | the CDP project's wallet secret |
| `JUPITER_API_KEY` | Jupiter API key (portal.jup.ag) |
| `HELIUS_API_KEY` | optional, a faster Solana RPC |
| `CDP_ADMIN_API_KEY_ID`, `CDP_ADMIN_API_KEY_SECRET` | a separate admin key **with** policies#manage, used only by the policy scripts |
| `CDP_SECRETS_FILE` | optional path to a JSON file holding any of the above (chmod 600, never committed) |
| `BOT_ACCOUNT_PREFIX` | account-name prefix, default `trading-bot` |
| `BOT_ACCOUNTS_FILE` | where the account addresses are stored (default `./accounts.json`) |
| `BOT_EVM_ADDRESS`, `BOT_SOL_ADDRESS` | optional, override the addresses in accounts.json |
| `BOT_DRY_RUN`, `BOT_LIVE_ACK` | live gate; both must be set exactly as shown above |

## How to run
```bash
python -m unittest tests/test_offline.py -v     # offline, no network, no secrets needed
python scripts/dryrun_mainnet.py               # real quotes, guard, tamper tests; never signs
python scripts/apply_policy.py --print         # show the policy JSON (no API calls)
```
In code:
```python
from engine.clients import bot_client; from engine.executor import Engine; from engine import config as C
async with bot_client() as cdp:
    eng = Engine(cdp)                      # DRY_RUN unless BOT_DRY_RUN=false AND BOT_LIVE_ACK=I-ACCEPT-LIVE-TRADING
    await eng.sol_swap(C.WSOL_MINT, C.BONK_MINT, usd=10)
    await eng.evm_buy("base", C.EVM_CHAINS["base"]["usdc"], usd=10)
    await eng.evm_sell("base", usdc, weth, amount_raw)          # approve exact -> swap -> revoke
```
**Kill switch:** run `touch KILL_SWITCH` in this folder. The engine checks for it at the start of every action and again right before every CDP send call. Delete the file to resume.

**Dry run:** `Engine(dry_run=False)` cannot override a dry-run environment.

## What the guard enforces (code, before any signing)
**Solana** (`sol_guard.check` on the exact bytes sent to CDP):
- The bot is the only signer and the fee payer.
- Top-level programs are limited to Jupiter v6, System, Token, Token-2022, ATA and ComputeBudget.
- Exactly one Jupiter ExactIn route (`shared_accounts_route_v2` / `route_v2` / v1 equivalents). Platform fee and positive-slippage fee must be 0, and slippage must be ≤150 bps.
- The swap authority is the bot. Source and **output** token accounts must equal the bot's ATA for the declared mints, derived locally.
- System transfers are allowed only from the bot to its own WSOL ATA (wrap), and never for more than the input amount.
- ATA creation must have payer and owner = bot.
- Token instructions are limited to SyncNative and CloseAccount. CloseAccount must send lamports to the bot. Every transfer, approve and set-authority instruction is rejected.
- Priority fee ≤ 0.002 SOL.
- Input value ≤ $100. The decoded min-out must be worth ≥97% of the input at independent prices.
- ALT contents are re-read from chain, not taken from the API.

**EVM** (`evm_guard.check` on the exact to/data/value):
- **Buy:** allowed only to a 0x Settler that is both in config and the live registry's `ownerOf(2)`. `slippage.recipient` = bot, `buyToken` = intended token, `minAmountOut` ≥ quote min, value = intended wei. Any quote that needs Permit2 is refused.
- **Approve:** only on allowlisted tokens, to SwapRouter02, for the **exact** sell amount. Unlimited or very large approvals are rejected.
- **Revoke:** `approve(router, 0)`.
- **Sell:** `exactInputSingle` with recipient = bot, tokenOut ∈ {WETH, USDC}, min-out > 0.
- **Unwrap:** `WETH.withdraw` only.
- Chains are limited to base and ethereum. $100 cap and min-out oracle check apply. Every other selector (permit, transfer, transferFrom, increaseAllowance, multicall, …) is rejected.
- **Sell flow:** approve exact → swap → revoke. If the swap fails, the bot revokes immediately. If the revoke fails, the allowance is recorded and later normal sells are blocked. Only `evm_sell(..., emergency=True)` (stop-loss) may run, and it uses a fresh exact approval.
- **Signing:** the engine never calls sign_message, sign_typed_data or sign_hash. It uses only `send_transaction`.

## Why EVM sells don't use the CDP swap API
A real CDP quote for USDC→ETH on Base returns a **Permit2 EIP-712 signature request** plus an approval to Permit2 (`issues.allowance.spender = 0x000000000022d473…`). That breaks the owner's no-typed-data/no-permit rule. So CDP swap (0x Settler) is used only for native-ETH buys, which need no approval or signature. Sells go through Uniswap V3 SwapRouter02, which uses a plain exact `approve` and a recipient field the guard can decode. EVM buys therefore must be paid in ETH. Buying with USDC would need an approval, and the rules allow approvals only on sells.

## CDP policy (generated by scripts/apply_policy.py)
The docs are account-scoped: one for the bot EVM account and one for the bot Solana account, built from `accounts.json` (`state/mainnet_policy_docs.json`). The policy is default-deny.
- **EVM:** `sendEvmTransaction` on base/ethereum is accepted in only four cases:
  - To Settler (current + announced next for each chain) with `slippage.recipient == bot`, value ≤ 0.06 ETH and netUSDChange ≤ $150.
  - To SwapRouter02 with `params.recipient == bot`, value 0 and ≤ $150.
  - To allowlisted tokens: `approve(spender ∈ routers, value ≤ 1e33)`.
  - To WETH: `withdraw`.
  - Hash, message and typed-data signing are explicitly rejected. signEvmTransaction and user ops are not accepted, so they are rejected by default.
- **Solana:** `sendSolTransaction` on `solana` is accepted only if every program is in the allowlist above, SOL recipients ∈ {bot, bot WSOL ATA}, SPL recipients ∈ bot ATAs (WSOL/USDC/BONK) and ≤1.2 SOL per instruction. signSolMessage is rejected. signSolTransaction is not accepted, so it is rejected by default.
- The nested `slippage.recipient` / `params.recipient` conditions are **unverified**: the docs say evmData supports only primitive types. `devnet_policy_test.py` tests exactly this (E1–E4). If CDP rejects the syntax, build the EVM rules with `nested_param_names=False` (engine/policy.py), or drop the recipient condition. The guard still enforces the recipient.

### Policy engine limits (the guard covers these)
- No USD cap on Solana. The cap is lamports only.
- Caps are per transaction. There is no daily or velocity limit.
- Jupiter's output token account is a program account input, not an SPL transfer, so the policy cannot pin it.
- Jupiter's inner CPIs into AMMs aren't inspected.
- CloseAccount destinations are not policy-checked (test S7 probes this).
- Settler inner `actions` (including CDP's ~0.15% protocol fee transfer) can't be decoded. Only the final recipient, token and minOut are enforced.
- The policy pins Settler addresses. When 0x rotates past the "next" Settler, buys will be rejected until an admin updates the policy.

## Maintenance
- To sell more EVM tokens, add them to `EVM_SELLABLE_TOKENS` and re-run `scripts/apply_policy.py`. To add Solana mints to the policy's SPL list, add them to `SOL_POLICY_MINTS` and re-run it.
- Update `SOL_POLICY_MAX_LAMPORTS_PER_IX` when the SOL price moves a lot.
- 0x Settler addresses rotate. When the guard reports "not the live registry Settler", update `settlers` in config and re-apply the policy.
- Raising `MAX_POSITION_USD` is a manual owner decision. Review it after 20 logged live trades (`executor.live_trade_count()`).

## Verification notes
- The Jupiter v6 IDL (`engine/jupiter_v6_idl.json`) was read from the on-chain IDL account. Discriminators and account orders come from it.
- A real /swap/v2/build SOL->BONK tx uses only ComputeBudget, ATA, System, Token and JUP6 (`shared_accounts_route_v2`).
- For the 0x Settler, the CDP quote `to` must equal `ownerOf(2)` on the 0x registry (`SETTLER_REGISTRY` in config).
- Uniswap SwapRouter02 and QuoterV2 addresses come from Uniswap's deployment docs.
- Re-verify every protocol address before going live.
