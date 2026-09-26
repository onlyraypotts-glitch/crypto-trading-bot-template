"""Create (or update) and attach the account-scoped mainnet policies for the bot's EVM and Solana accounts.
The policy is built from the RUNTIME accounts file (accounts.json from create_accounts.py, or BOT_EVM_ADDRESS /
BOT_SOL_ADDRESS) - no addresses are hard-coded.
Needs a SEPARATE admin API key with scope policies#manage in CDP_ADMIN_API_KEY_ID / CDP_ADMIN_API_KEY_SECRET
(keeps that scope off the bot's trading key). The script refuses to run without it.
  --print   only print the policy JSON (no API calls)
Run scripts/devnet_policy_test.py FIRST (throwaway testnet accounts) - it verifies nested param support."""
import asyncio, json, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from secrets_loader import load, redact
load()
from engine import config as C, policy as P, cdp_policy_io as io
from engine.clients import admin_client

STATE = ROOT / "state" / "mainnet_policies.json"


async def main():
    bot_evm, bot_sol = C.require_accounts()
    evm_doc, sol_doc = P.mainnet_policies()
    STATE.parent.mkdir(exist_ok=True)
    (ROOT / "state" / "mainnet_policy_docs.json").write_text(json.dumps({"evm": evm_doc, "sol": sol_doc}, indent=2))
    if "--print" in sys.argv:
        print(json.dumps({"evm": evm_doc, "sol": sol_doc}, indent=2))
        return
    if not (os.environ.get("CDP_ADMIN_API_KEY_ID") and os.environ.get("CDP_ADMIN_API_KEY_SECRET")):
        print("CDP_ADMIN_API_KEY_ID / CDP_ADMIN_API_KEY_SECRET not set - need a separate key with policies#manage"); sys.exit(1)
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    async with admin_client() as cdp:
        for key, doc, attach, addr in [("evm", evm_doc, io.attach_evm, bot_evm), ("sol", sol_doc, io.attach_sol, bot_sol)]:
            p = await (io.update_policy(cdp, st[key], doc) if st.get(key) else io.create_policy(cdp, doc))
            st[key] = p.id
            STATE.write_text(json.dumps(st, indent=2))
            acct = await attach(cdp, addr, p.id)
            ok = p.id in (acct.policies or [])
            print(f"{key}: policy {p.id} ({len(doc['rules'])} rules) attached to {addr}: {'CONFIRMED' if ok else 'NOT CONFIRMED'}")
            back = await io.get_policy(cdp, p.id)
            print("   server copy rules:", [(r['action'], r['operation']) for r in back.get('rules', [])])

try:
    asyncio.run(main())
except Exception as e:
    print("ERR", type(e).__name__, redact(e)); sys.exit(1)
