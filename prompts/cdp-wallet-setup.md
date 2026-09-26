<!-- Let's Get Rich Team · prompt `cdp-wallet-setup` · install only from a pinned commit and verify its sha256 against prompts/MANIFEST.json -->
> **cdp-wallet-setup**: Setting up the capped Coinbase CDP Server Wallet engine for autonomous mode.
>
> Not financial advice. Crypto and stocks can go to zero. The bot never asks for, stores or accepts seed phrases, private keys, passwords or 2FA codes. Paths are configurable: `$BOT_REPO` (your clone of this repo), `$DESK_DIR` (default `$BOT_REPO/desk`), `$DESK_TOOLS_DIR` (default `$BOT_REPO/research`), `$BOT_DATA_DIR` (default `$BOT_REPO/data`).

# CDP Wallet Setup

Use this skill to set up the cdp-trading-bot engine for a new owner, so the bot can trade autonomously from its own Coinbase CDP Server Wallet accounts. Do the steps in order and report after each one. Nothing trades live until the final step, and the final step needs the owner's explicit typed confirmation.

## Ground rules for the whole setup
- Secrets are requested ONLY through the assistant's secure secret request feature, stored as environment secrets. Never ask the owner to paste a key, secret or wallet secret into chat, email, notes or a file. Never print, echo, log, screenshot or copy a secret value, and check only that a value exists and how long it is. If a secret ever appears in chat or a screenshot, don't use it: tell the owner to delete that key in the portal and create a new one.
- CDP wallet private keys stay inside Coinbase's enclave. Nobody ever needs a seed phrase or private key.
- The bot never moves money. Funding and withdrawing are the owner's job.
- If a step fails, stop, explain it in plain words, and fall back to MANUAL MODE (move cards) until the problem is fixed.

## Step 1: Get the code
Get the engine from https://github.com/onlyraypotts-glitch/crypto-trading-bot-template (clone the same pinned commit the prompts were installed from) and put it in a working folder, for example `cdp-trading-bot/`. Then:
```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest tests/test_offline.py -v
```
All the offline tests must pass. They need no keys and no network. If any fail, stop and report.
The clone also includes `desk/` (the team's shared database, playbooks and tests: run `python3 -m unittest discover -s desk/tests -v` and `python3 desk/desk.py init`) and `research/` (scanner, shadow tracker, call-channel ingest, narrow-question model). It's read-only and needs no pip installs. Check it with `python3 research/shadow_selftest.py`. Its data goes to `data/` (gitignored). If it isn't set up yet, see `research/README.md` and the shadow-tracker, source-discovery and jev-signup-watch routines.

## Step 2: Owner creates the CDP project and keys
Walk the owner through this one step at a time, waiting for "done" after each:
1. Sign in at portal.cdp.coinbase.com (the portal, not the docs site). Create a new project just for this bot.
2. **Bot API key:** API Keys → Create API key (choose Ed25519 if asked). Give it Server Wallet account and transaction permissions, **but not the policies#manage scope.** Then the running bot can never loosen its own guard rails.
3. **Admin API key:** create a second, separate key **with** the policies#manage scope. It is used only to apply policies and can be deleted from the bot after setup.
4. **Wallet secret:** on the Server Wallet security page, click Generate Wallet Secret. Copy it with the **copy icon** (selecting text by hand adds line breaks that break the secret). It's one long line.
5. **Jupiter key:** create a free API key at portal.jup.ag (developers.jup.ag) for Solana swaps.
6. Optional: a free Helius key for faster Solana RPC.

## Step 3: Request each secret securely
Send one secure secret request per value, using exactly these names:
- `CDP_API_KEY_ID` and `CDP_API_KEY_SECRET` (bot key)
- `CDP_WALLET_SECRET`
- `JUPITER_API_KEY`
- `CDP_ADMIN_API_KEY_ID` and `CDP_ADMIN_API_KEY_SECRET` (admin key)
- optional: `HELIUS_API_KEY` (also used by the research tools' smart-money factor)
- optional: `COINGECKO_DEMO_API_KEY` (free demo key; only helps the shadow tracker when keyless CoinGecko is rate limited)

Check each one only by presence and length (`secrets_loader.load()` returns lengths only). A Key ID is a 36-character UUID. If the wallet secret has spaces or line breaks in it, use `--strip-ws` in the next step.

## Step 4: Create the bot accounts
Run `python create_accounts.py`. It creates, or re-uses, `trading-bot-evm` (one address for both Base and Ethereum) and `trading-bot-sol`, and saves their public addresses to `accounts.json` (gitignored). Confirm both addresses have zero balance with `python balances.py`. Show the owner both addresses and ask them to confirm in chat. The addresses are public, so showing them is fine.

## Step 5: Test policies on devnet
Run `python scripts/devnet_policy_test.py`. It uses THROWAWAY accounts on Base Sepolia and Solana devnet only, with faucet funds. Every line must read PASS:
- allowed swaps to self are accepted
- foreign recipients, transfers, unlimited approvals and message/typed-data/hash signing are rejected

If the nested recipient conditions aren't supported, follow the fallback in the README. Report the results.

## Step 6: Apply the mainnet policy
1. Run `python scripts/apply_policy.py --print` and summarize the policy for the owner in plain words: swaps to self only, a $150 per-transaction backstop, allowlisted routers and programs, exact approvals only to the sell router, and no signing.
2. Run `python scripts/apply_policy.py`, which uses the admin key. Both accounts must show CONFIRMED.
3. Recommend the owner deletes the admin key secret afterwards. It is only needed again to change policies.

## Step 7: Mainnet dry run
Run `python scripts/dryrun_mainnet.py`. It pulls real quotes and builds real transactions, and nothing is signed. Every guard baseline must PASS and every tampered variant must be REJECTED. Test the kill switch: create `KILL_SWITCH`, confirm a trade call is refused, then remove it. Report the results.

## Step 8: Owner funds small amounts
Tell the owner the two addresses again, with the right network for each:
- Solana address: SOL on Solana
- EVM address: ETH on **Base** (or Ethereum mainnet, where gas is expensive)

Suggest a small first amount that they can afford to lose completely, for example about 0.2 SOL and about $50 of ETH on Base, including gas. Say plainly that the bot never sends money back. Withdrawing is done by the owner. After funding, run `balances.py` and the dry run again, so simulations succeed with real balances.

## Step 9: Go live only after explicit confirmation
Ask the owner: "Everything is tested. Do you want me to start live autonomous trading with the $100-per-position cap? Reply YES to go live." Only after a clear typed yes:
1. Set `BOT_DRY_RUN=false` and `BOT_LIVE_ACK=I-ACCEPT-LIVE-TRADING` in the trading routine's environment.
2. Save a memory recording the go-live time and the confirmed addresses.
3. Start the autonomous-trading-loop routine.

Anything short of a clear yes means staying in dry run with move cards.

## Afterwards
- The owner says "stop": create `KILL_SWITCH` and confirm.
- The owner says "CDP off": stop new buys, only exit or hold what's open, and never move funds.
- 0x Settler address rotation shows up as guard errors about the live registry Settler. Tell the owner the policy needs updating with the admin key. Never loosen the guard yourself.

---
## STANDING RESEARCH CLAUSE (always on; applies to every bot on the team)
Always keep researching proven crypto trading results and theories: peer-reviewed and working papers (momentum, reversal,
liquidity, on-chain and sentiment factors), published backtests that include costs, exchange/DEX microstructure studies,
post-mortems, and documented trader track records. Weight evidence by quality (replicated > single study > vendor claim >
anecdote). Turn each finding into a **pre-specified, testable hypothesis** (written down before looking at results), log it,
and test it **shadow → paper → small live** before it changes any weight. **Research never changes hard safety limits.**
