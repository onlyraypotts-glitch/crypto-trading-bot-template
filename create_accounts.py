"""Idempotently get-or-create the bot's two CDP server-wallet accounts and record their PUBLIC addresses in
accounts.json (gitignored, created at runtime). No transactions, no signing.

Account names default to trading-bot-evm / trading-bot-sol. Override the prefix with BOT_ACCOUNT_PREFIX
(letters, digits, hyphens; do not put a person's name in it if you ever share logs).
Usage:  python create_accounts.py            (add --strip-ws if the wallet secret was pasted with line breaks)
"""
import asyncio, os, sys
from secrets_loader import load, redact, missing_required
from engine import config as C

load()
if "--strip-ws" in sys.argv and os.environ.get("CDP_WALLET_SECRET"):
    os.environ["CDP_WALLET_SECRET"] = "".join(os.environ["CDP_WALLET_SECRET"].split())


async def main():
    miss = missing_required(["CDP_API_KEY_ID", "CDP_API_KEY_SECRET", "CDP_WALLET_SECRET"])
    if miss:
        print("missing secrets:", miss); sys.exit(1)
    from cdp import CdpClient
    evm_name, sol_name = C.account_names()
    try:
        async with CdpClient() as c:
            evm = await c.evm.get_or_create_account(name=evm_name)
            sol = await c.solana.get_or_create_account(name=sol_name)
        C.save_accounts(evm_name, evm.address, sol_name, sol.address)
        print(f"EVM  {evm_name}: {evm.address}   (Base + Ethereum)")
        print(f"SOL  {sol_name}: {sol.address}")
        print(f"saved -> {C.ACCOUNTS_FILE}")
    except Exception as e:
        print("ERR", type(e).__name__, redact(e)); sys.exit(1)

asyncio.run(main())
