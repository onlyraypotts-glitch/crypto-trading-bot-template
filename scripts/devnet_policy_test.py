"""Policy test on TESTNETS ONLY with THROWAWAY accounts (<prefix>-policytest-evm / <prefix>-policytest-sol, default prefix trading-bot).
Mirrors the mainnet policy structure (engine/policy.py) with testnet networks + dummy router addresses.
Never touches the real bot accounts. Results -> state/devnet_policy_results.json"""
import asyncio, json, sys, time, os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from secrets_loader import load, redact
load()
from eth_abi import encode
from eth_utils import keccak
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.instruction import Instruction, AccountMeta
from solders.hash import Hash
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from engine import config as C, policy as P, cdp_policy_io as io, sol_tx
from engine.net import sol_rpc, evm_rpc

STATE = ROOT / "state" / "test_policies.json"
OUT = ROOT / "state" / "devnet_policy_results.json"
(ROOT / "state").mkdir(exist_ok=True)
BASE_SEPOLIA_RPC = "https://sepolia.base.org"
SEP_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
SEP_WETH = "0x4200000000000000000000000000000000000006"
DUMMY_SETTLER = "0x1111111111111111111111111111111111111111"
DUMMY_ROUTER = "0x2222222222222222222222222222222222222222"
FOREIGN_EVM = "0x000000000000000000000000000000000000dEaD"
DEVNET_USDC = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
FOREIGN_SOL = str(Keypair().pubkey())   # random, nobody's
MEMO = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
results = []
PREFIX = os.environ.get("BOT_ACCOUNT_PREFIX", "trading-bot")


def sel(sig): return "0x" + keccak(text=sig)[:4].hex()


def settler_calldata(recipient, buy=SEP_USDC, min_out=1):
    return sel("execute((address,address,uint256),bytes[],bytes32)") + encode(
        ["(address,address,uint256)", "bytes[]", "bytes32"], [(recipient, buy, min_out), [], b"\x00" * 32]).hex()


def uni_calldata(recipient):
    return sel("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))") + encode(
        ["(address,address,uint24,address,uint256,uint256,uint160)"], [(SEP_USDC, SEP_WETH, 500, recipient, 1, 1, 0)]).hex()


def approve_calldata(spender, amount):
    return sel("approve(address,uint256)") + encode(["address", "uint256"], [spender, amount]).hex()


def classify(e):
    m = redact(e, 400)
    pol = any(s in m.lower() for s in ["policy", "rejected", "forbidden"])
    return ("REJECTED_BY_POLICY" if pol else "ERROR"), m


async def run(name, expect, coro):
    try:
        r = await coro
        got, detail = "ACCEPTED", str(r)[:200]
    except Exception as e:
        got, detail = classify(e)
    ok = (got == expect) or (expect == "REJECTED_BY_POLICY" and got == "REJECTED_BY_POLICY")
    results.append({"test": name, "expect": expect, "got": got, "pass": ok, "detail": detail})
    print(("PASS " if ok else "FAIL ") + f"{name}: expect={expect} got={got} :: {detail[:220]}")


async def main():
    from engine.clients import admin_client
    from cdp.evm_transaction_types import TransactionRequestEIP1559
    st = json.load(open(STATE)) if STATE.exists() else {}
    async with admin_client() as cdp:
        evm = await cdp.evm.get_or_create_account(name=PREFIX + "-policytest-evm")
        sol = await cdp.solana.get_or_create_account(name=PREFIX + "-policytest-sol")
        E, S = evm.address, sol.address
        assert E.lower() != (C.BOT_EVM or "").lower() and S != C.BOT_SOL
        print("throwaway EVM", E, "SOL", S)
        evm_doc = {"scope": "account", "description": "trading bot TEST evm base sepolia",
                   "rules": P.evm_policy(E, ["base-sepolia"], [DUMMY_SETTLER], [DUMMY_ROUTER], [SEP_USDC, SEP_WETH],
                                         [SEP_WETH], 15000, C.POLICY_MAX_ETH_VALUE_WEI, C.POLICY_MAX_APPROVE_VALUE,
                                         nested_param_names=os.environ.get("NESTED", "1") == "1")}
        sol_doc = {"scope": "account", "description": "trading bot TEST sol devnet",
                   "rules": P.sol_policy(S, "solana-devnet", {C.WSOL_MINT: "token", DEVNET_USDC: "token"},
                                         C.SOL_POLICY_MAX_LAMPORTS_PER_IX)}
        for key, doc, attach, addr in [("evm", evm_doc, io.attach_evm, E), ("sol", sol_doc, io.attach_sol, S)]:
            if st.get(key):
                p = await io.update_policy(cdp, st[key], doc)
            else:
                p = await io.create_policy(cdp, doc)
                st[key] = p.id
            acct = await attach(cdp, addr, p.id)
            if p.id not in (getattr(acct, "policies", None) or []):
                raise SystemExit(f"ABORT: policy {p.id} not confirmed attached to {addr}; not running tests")
            print("test policy", key, p.id, "attached + confirmed")
        json.dump(st, open(STATE, "w"), indent=2)

        # ---------- faucets (testnet only)
        bal = int(evm_rpc("x", "eth_getBalance", [E, "latest"], BASE_SEPOLIA_RPC), 16)
        if bal < 10**13:
            print("base-sepolia faucet:", await cdp.evm.request_faucet(address=E, network="base-sepolia", token="eth"))
        lam = sol_rpc("getBalance", [S], "solana-devnet")["value"]
        if lam < 5_000_000:
            print("solana-devnet faucet:", await cdp.solana.request_faucet(address=S, token="sol"))
        for _ in range(30):
            bal = int(evm_rpc("x", "eth_getBalance", [E, "latest"], BASE_SEPOLIA_RPC), 16)
            lam = sol_rpc("getBalance", [S], "solana-devnet")["value"]
            if bal >= 10**13 and lam >= 5_000_000:
                break
            time.sleep(3)
        print("balances: base-sepolia wei", bal, "devnet lamports", lam)

        # ---------- EVM tests (base-sepolia)
        def send(to, data="0x", value=0, network="base-sepolia"):
            return cdp.evm.send_transaction(address=E, network=network,
                                            transaction=TransactionRequestEIP1559(to=to, data=data, value=value, gas=120000))
        await run("E1 settler execute recipient=self", "ACCEPTED", send(DUMMY_SETTLER, settler_calldata(E)))
        await run("E2 settler execute recipient=FOREIGN", "REJECTED_BY_POLICY", send(DUMMY_SETTLER, settler_calldata(FOREIGN_EVM)))
        await run("E3 uni exactInputSingle recipient=self", "ACCEPTED", send(DUMMY_ROUTER, uni_calldata(E)))
        await run("E4 uni exactInputSingle recipient=FOREIGN", "REJECTED_BY_POLICY", send(DUMMY_ROUTER, uni_calldata(FOREIGN_EVM)))
        await run("E5 approve(router, exact 1000)", "ACCEPTED", send(SEP_USDC, approve_calldata(DUMMY_ROUTER, 1000)))
        await run("E6 revoke approve(router, 0)", "ACCEPTED", send(SEP_USDC, approve_calldata(DUMMY_ROUTER, 0)))
        await run("E7 approve(router, UINT256_MAX)", "REJECTED_BY_POLICY", send(SEP_USDC, approve_calldata(DUMMY_ROUTER, C.UINT256_MAX)))
        await run("E8 approve(FOREIGN spender, 1000)", "REJECTED_BY_POLICY", send(SEP_USDC, approve_calldata(FOREIGN_EVM, 1000)))
        await run("E9 native ETH transfer to FOREIGN (1 wei)", "REJECTED_BY_POLICY", send(FOREIGN_EVM, "0x", 1))
        await run("E10 ERC20 transfer to FOREIGN", "REJECTED_BY_POLICY", send(SEP_USDC, sel("transfer(address,uint256)") + encode(["address", "uint256"], [FOREIGN_EVM, 1]).hex()))
        await run("E11 settler recipient=self but value 1 ETH (> 0.06 cap)", "REJECTED_BY_POLICY", send(DUMMY_SETTLER, settler_calldata(E), 10**18))
        await run("E12 WETH withdraw(0) (unwrap to self)", "ACCEPTED", send(SEP_WETH, sel("withdraw(uint256)") + encode(["uint256"], [0]).hex()))
        await run("E13 call to non-allowlisted contract", "REJECTED_BY_POLICY", send("0x3333333333333333333333333333333333333333", settler_calldata(E)))
        await run("E14 allowed call on ethereum-sepolia (network not allowed)", "REJECTED_BY_POLICY", send(DUMMY_SETTLER, settler_calldata(E), 0, "ethereum-sepolia"))
        await run("E15 sign_message", "REJECTED_BY_POLICY", cdp.evm.sign_message(E, "hello"))
        await run("E16 sign_hash", "REJECTED_BY_POLICY", cdp.evm.sign_hash(E, "0x" + "11" * 32))
        td = {"types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                        "Permit": [{"name": "spender", "type": "address"}, {"name": "value", "type": "uint256"}]},
              "primaryType": "Permit", "domain": {"name": "Permit2", "chainId": 84532, "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3"},
              "message": {"spender": FOREIGN_EVM, "value": str(C.UINT256_MAX)}}
        await run("E17 sign_typed_data (Permit2-style)", "REJECTED_BY_POLICY",
                  cdp.evm.sign_typed_data(E, td["domain"], td["types"], td["primaryType"], td["message"]))
        await run("E18 signEvmTransaction (not allowed op)", "REJECTED_BY_POLICY",
                  cdp.evm.sign_transaction(E, "0x02e9830149a48084773594008477359400830186a094111111111111111111111111111111111111111180c0"))

        # ---------- Solana tests (devnet)
        me = Pubkey.from_string(S)
        wsol_ata = Pubkey.from_string(P.ata(S, C.WSOL_MINT))
        SYS, TOK, ATAP = (Pubkey.from_string(x) for x in (C.SYSTEM_PROGRAM, C.TOKEN_PROGRAM, C.ATA_PROGRAM))

        def transfer(to, lamports):
            return Instruction(SYS, (2).to_bytes(4, "little") + int(lamports).to_bytes(8, "little"),
                               [AccountMeta(me, True, True), AccountMeta(to, False, True)])
        cu = sol_tx.cu_limit_ix(200_000)
        ata_create = Instruction(ATAP, bytes([1]), [AccountMeta(me, True, True), AccountMeta(wsol_ata, False, True), AccountMeta(me, False, False),
                                                    AccountMeta(Pubkey.from_string(C.WSOL_MINT), False, False), AccountMeta(SYS, False, False), AccountMeta(TOK, False, False)])
        sync = Instruction(TOK, bytes([17]), [AccountMeta(wsol_ata, False, True)])

        def close(dest):
            return Instruction(TOK, bytes([9]), [AccountMeta(wsol_ata, False, True), AccountMeta(dest, False, True), AccountMeta(me, True, False)])

        def spl_transfer(dest_token_acct, amt):
            return Instruction(TOK, bytes([3]) + int(amt).to_bytes(8, "little"),
                               [AccountMeta(wsol_ata, False, True), AccountMeta(dest_token_acct, False, True), AccountMeta(me, True, False)])
        memo = Instruction(Pubkey.from_string(MEMO), b"policy-test", [])

        def build(ixs, alts=None):
            bh = sol_rpc("getLatestBlockhash", [{"commitment": "confirmed"}], "solana-devnet")["value"]["blockhash"]
            from solders.address_lookup_table_account import AddressLookupTableAccount
            al = [AddressLookupTableAccount(Pubkey.from_string(k), [Pubkey.from_string(a) for a in v]) for k, v in (alts or {}).items()]
            msg = MessageV0.try_compile(me, ixs, al, Hash.from_string(bh))
            return sol_tx.tx_b64(bytes(VersionedTransaction.populate(msg, [Signature.default()])))

        def ssend(ixs, alts=None):
            return cdp.solana.send_transaction(network="solana-devnet", transaction=build(ixs, alts))

        foreign = Pubkey.from_string(FOREIGN_SOL)
        await run("S1 SOL transfer to self", "ACCEPTED", ssend([cu, transfer(me, 1000)]))
        await run("S2 SOL transfer to FOREIGN", "REJECTED_BY_POLICY", ssend([cu, transfer(foreign, 1000)]))
        await run("S3 Jupiter-style wrap/unwrap (ATA idempotent, SOL->own WSOL ATA, syncNative, close->self)", "ACCEPTED",
                  ssend([cu, ata_create, transfer(wsol_ata, 1_000_000), sync, close(me)]))
        await run("S4 non-allowlisted program (Memo)", "REJECTED_BY_POLICY", ssend([cu, memo]))
        await run("S5 SPL transfer from own WSOL ATA to FOREIGN token acct", "REJECTED_BY_POLICY",
                  ssend([cu, ata_create, transfer(wsol_ata, 1_000_000), sync, spl_transfer(foreign, 1000), close(me)]))
        await run("S6 SOL to own WSOL ATA above lamport cap (2 SOL)", "REJECTED_BY_POLICY", ssend([cu, ata_create, transfer(wsol_ata, 2_000_000_000)]))
        await run("S7 closeAccount(own WSOL ATA) -> FOREIGN destination [policy gap probe]", "REJECTED_BY_POLICY",
                  ssend([cu, ata_create, transfer(wsol_ata, 1_000_000), sync, close(foreign)]))
        await run("S8 sign_message", "REJECTED_BY_POLICY", cdp.solana.sign_message(S, "hello"))
        await run("S9 signSolTransaction (not allowed op)", "REJECTED_BY_POLICY", cdp.solana.sign_transaction(S, build([cu, transfer(me, 1000)])))
        # Jupiter-shaped: real mainnet /build instructions for the THROWAWAY as taker, devnet blockhash -> can never land on mainnet.
        try:
            b = sol_tx.jupiter_build(C.WSOL_MINT, C.BONK_MINT, 10_000_000, S)
            jix = [sol_tx.cu_limit_ix(600_000)] + [sol_tx.to_ix(i) for i in b["computeBudgetInstructions"] + b["setupInstructions"]] + [sol_tx.to_ix(b["swapInstruction"])]
            if b.get("cleanupInstruction"):
                jix.append(sol_tx.to_ix(b["cleanupInstruction"]))
            alts = sol_tx.alts_from_build(b)
            await run("S10 real Jupiter SOL->BONK instruction set (devnet blockhash; accept by policy, then expected to fail on-chain)",
                      "ACCEPTED", ssend(jix, alts))
            await run("S11 same Jupiter tx + SOL transfer to FOREIGN", "REJECTED_BY_POLICY", ssend(jix + [transfer(foreign, 1000)], alts))
        except Exception as e:
            print("Jupiter-shaped test setup error:", redact(e))
    json.dump({"when": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "throwaway_evm": E, "throwaway_sol": S,
               "test_policy_ids": st, "results": results}, open(OUT, "w"), indent=2)
    print(f"\n{sum(r['pass'] for r in results)}/{len(results)} passed -> {OUT}")

asyncio.run(main())
