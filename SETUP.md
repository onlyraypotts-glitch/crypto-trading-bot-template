# SETUP: first-time setup for a new owner

This guide takes a brand-new owner from nothing to a live, capped trading bot. Do the steps in order. **Nothing trades live until step 10**, and step 10 needs you to act on purpose.

> **Risk warning.** Crypto trading, and memecoin trading especially, can lose all of the money you put in. This software comes with no warranty. Fund only what you can afford to lose. Start small.

## 0. Requirements
- Python 3.10+ on an always-on machine you control
- A Coinbase Developer Platform (CDP) account: https://portal.cdp.coinbase.com
- A Jupiter API key: https://portal.jup.ag
- Optional: a Helius API key for faster Solana RPC

## 1. Install
```bash
git clone <this repo> cdp-trading-bot && cd cdp-trading-bot
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest tests/test_offline.py -v      # must be all OK (offline, needs no keys)
```

## 2. Create a CDP project
In the CDP portal, create a new project just for this bot. Don't reuse a project that holds other wallets.

## 3. Create the BOT API key (no policy scope)
Go to Portal, then API Keys, then Create API key. Give it the Server Wallet / accounts and transaction permissions it needs. **Do NOT grant `policies#manage`.** If the bot is ever compromised, it then cannot loosen or detach its own guard-rail policy. Save the result as:
- `CDP_API_KEY_ID`
- `CDP_API_KEY_SECRET`

## 4. Create a SEPARATE ADMIN key (policy scope only)
Create a second API key **with** `policies#manage`. The policy scripts use it, and nothing else does. Save it as:
- `CDP_ADMIN_API_KEY_ID`
- `CDP_ADMIN_API_KEY_SECRET`

After step 7 you can remove this key from the bot machine. Re-add it only when you change policies.

## 5. Wallet secret
Go to Portal, then Server Wallet, then Wallet Secret, and generate one. Copy it with the **copy icon**, not by selecting the text by hand, which picks up line breaks. Save it as `CDP_WALLET_SECRET`. If it was pasted with line breaks anyway, run `create_accounts.py --strip-ws`.

## 6. Jupiter key
Create a key at portal.jup.ag and save it as `JUPITER_API_KEY`.

### Where to put the secrets
The best place is the environment, injected by your host's secret manager. Otherwise, use a JSON file outside the repo:
```bash
cat > ~/.cdp-bot-secrets.json   # {"CDP_API_KEY_ID": "...", "CDP_API_KEY_SECRET": "...", ...}
chmod 600 ~/.cdp-bot-secrets.json
export CDP_SECRETS_FILE=~/.cdp-bot-secrets.json
```
Never paste secrets into chats, issues, logs or commits. The code never prints them. `redact()` scrubs them from error text.

## 7. Create the bot accounts
```bash
python create_accounts.py
```
This creates (or reuses) two CDP accounts, `trading-bot-evm` (Base + Ethereum) and `trading-bot-sol`. It writes their **public** addresses to `accounts.json`, which is gitignored. Set `BOT_ACCOUNT_PREFIX` first if you want different names.

## 8. Test the policy on testnets, then apply it
```bash
python scripts/devnet_policy_test.py        # THROWAWAY accounts on base-sepolia + solana-devnet (uses faucets)
```
Every line should read PASS (see `state/devnet_policy_results.json`). If the nested recipient conditions fail, see README, "CDP policy".
```bash
python scripts/apply_policy.py --print      # review the JSON: it is built from accounts.json
python scripts/apply_policy.py              # create + attach; both accounts must show CONFIRMED
```

## 9. Dry run on mainnet (no signing)
```bash
python scripts/dryrun_mainnet.py
```
This fetches real quotes and builds real transactions, and the guard must PASS them. Every tampered variant must be REJECTED. Nothing is signed.

## 10. Fund small, then go live
1. `python balances.py` shows the two addresses. Send **small** amounts only, from your own exchange or wallet. For example, about 0.2 SOL to the Solana address and about $50 of ETH on Base to the EVM address (EVM buys are paid in ETH, and gas is needed).
2. Run `dryrun_mainnet.py` again. Simulations should now succeed.
3. Go live on purpose:
```bash
export BOT_DRY_RUN=false
export BOT_LIVE_ACK=I-ACCEPT-LIVE-TRADING
```
If either variable is missing or different, the engine stays in dry run.

## Operating
- **Stop everything now:** run `touch KILL_SWITCH`. Delete the file to resume.
- **Position cap:** $100 per position (`MAX_POSITION_USD`). Keep it until at least 20 live trades are logged in `state/trades.jsonl`, then decide whether to raise it by editing config.
- **Withdrawals:** the bot cannot send funds out, by design. Move money out yourself through the CDP portal or SDK, using your own tooling.
- **Settler rotation:** if EVM buys start failing the guard with "not the live registry Settler", update `settlers` in `engine/config.py` and re-run `apply_policy.py` with the admin key.
