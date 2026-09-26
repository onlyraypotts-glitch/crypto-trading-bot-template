"""Mainnet DRY RUN: real quotes, real tx building, guard - NO signing, NO sending (Engine forced dry_run=True).
Also shows the guard rejecting tampered variants. Output -> state/dryrun_results.json"""
import asyncio, json, sys, base64, copy
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from secrets_loader import redact
from eth_abi import encode, decode
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.instruction import Instruction, AccountMeta
from engine import config as C, sol_tx, sol_guard, evm_guard, evm_swap, prices as PX
from engine.executor import Engine
from engine.guard_common import GuardError
from engine.clients import bot_client

FOREIGN_SOL = str(Keypair().pubkey())   # random, nobody's
FOREIGN_EVM = "0x000000000000000000000000000000000000dEaD"
out = {}


def expect_reject(name, fn):
    try:
        fn(); res = {"test": name, "result": "PASSED GUARD (BAD!)"}
    except GuardError as g:
        res = {"test": name, "result": "REJECTED", "reasons": g.reasons}
    print(("OK  " if res["result"] == "REJECTED" else "BAD ") + name + " -> " + res["result"] + " " + str(res.get("reasons", ""))[:260])
    out.setdefault("tamper", []).append(res)


async def main():
    C.require_accounts()
    (ROOT / "state").mkdir(exist_ok=True)
    async with bot_client() as cdp:
        eng = Engine(cdp, dry_run=True)
        assert eng.dry_run
        # ---------------- Solana $10 SOL -> BONK
        r = await eng.sol_swap(C.WSOL_MINT, C.BONK_MINT, usd=10)
        out["sol_swap"] = r
        print("SOL->BONK:", r["status"], "in", r["quote"]["inAmount"], "out", r["quote"]["outAmount"], "route", r["route"],
              "guard", r.get("guard", {}).get("verdict"), r.get("guard_errors", ""), "sim", r.get("simulation", {}).get("err"))
        # tamper variants on a fresh build
        px, dec = PX.sol_prices([C.WSOL_MINT, C.BONK_MINT])
        amt = int(10 / px[C.WSOL_MINT] * 1e9)
        b = sol_tx.jupiter_build(C.WSOL_MINT, C.BONK_MINT, amt, C.BOT_SOL)
        alts = sol_tx.fetch_alts_onchain(list(b["addressesByLookupTableAddress"].keys()))
        intent = {"input_mint": C.WSOL_MINT, "output_mint": C.BONK_MINT, "in_amount": amt, "decimals": dec}
        me, fr = Pubkey.from_string(C.BOT_SOL), Pubkey.from_string(FOREIGN_SOL)
        sol_guard.check(sol_tx.assemble(b, C.BOT_SOL), intent, alts, px)  # baseline passes
        sysp, tokp = Pubkey.from_string(C.SYSTEM_PROGRAM), Pubkey.from_string(C.TOKEN_PROGRAM)
        xfer = Instruction(sysp, (2).to_bytes(4, "little") + (1000).to_bytes(8, "little"), [AccountMeta(me, True, True), AccountMeta(fr, False, True)])
        expect_reject("SOL: extra SOL transfer to foreign", lambda: sol_guard.check(sol_tx.assemble(b, C.BOT_SOL, extra_ixs=[xfer]), intent, alts, px))
        b2 = copy.deepcopy(b)
        foreign_bonk_ata = sol_guard.ata(FOREIGN_SOL, C.BONK_MINT, C.TOKEN_PROGRAM)
        b2["swapInstruction"]["accounts"][5]["pubkey"] = foreign_bonk_ata
        expect_reject("SOL: Jupiter output -> foreign BONK ATA", lambda: sol_guard.check(sol_tx.assemble(b2, C.BOT_SOL), intent, alts, px))
        bonk_ata = Pubkey.from_string(sol_guard.ata(C.BOT_SOL, C.BONK_MINT, C.TOKEN_PROGRAM))
        approve = Instruction(tokp, bytes([4]) + (2**64 - 1).to_bytes(8, "little"), [AccountMeta(bonk_ata, False, True), AccountMeta(fr, False, False), AccountMeta(me, True, False)])
        expect_reject("SOL: unlimited SPL Approve to foreign delegate", lambda: sol_guard.check(sol_tx.assemble(b, C.BOT_SOL, extra_ixs=[approve]), intent, alts, px))
        b3 = copy.deepcopy(b)
        b3["cleanupInstruction"]["accounts"][1]["pubkey"] = FOREIGN_SOL
        expect_reject("SOL: closeAccount (unwrap) -> foreign", lambda: sol_guard.check(sol_tx.assemble(b3, C.BOT_SOL), intent, alts, px))
        big = int(150 / px[C.WSOL_MINT] * 1e9)
        bb = sol_tx.jupiter_build(C.WSOL_MINT, C.BONK_MINT, big, C.BOT_SOL)
        expect_reject("SOL: $150 position (> $100 cap)", lambda: sol_guard.check(sol_tx.assemble(bb, C.BOT_SOL), {**intent, "in_amount": big},
                      sol_tx.fetch_alts_onchain(list(bb["addressesByLookupTableAddress"].keys())), px))

        # ---------------- Base $10 ETH -> USDC (CDP swap API, 0x Settler)
        usdc = C.EVM_CHAINS["base"]["usdc"]
        r = await eng.evm_buy("base", usdc, 10)
        out["base_buy"] = r
        print("Base ETH->USDC:", r["status"], "wei", r["intent"]["wei"], "toAmount", r["quote"]["to_amount"], "min", r["quote"]["min_to_amount"],
              "guard", r.get("guard", {}).get("verdict"), r.get("guard_errors", ""))
        eth = PX.eth_usd()
        wei = int(10 / eth * 1e18)
        q = await evm_swap.cdp_buy_quote(cdp, "base", usdc, wei)
        live = evm_swap.settler_current("base")
        prices = {"in_price": eth, "in_decimals": 18, "out_price": 1.0, "out_decimals": 6}
        intent_b = {"kind": "buy", "to_token": usdc, "wei": wei, "min_out": q["min_to_amount"]}
        evm_guard.check("base", q["tx"], intent_b, prices, settler_live=live)   # baseline passes
        (rec, bt, mo), acts, z = decode(["(address,address,uint256)", "bytes[]", "bytes32"], bytes.fromhex(q["tx"]["data"][10:]))
        tampered = dict(q["tx"], data=evm_swap.SEL["settler_execute"] + encode(["(address,address,uint256)", "bytes[]", "bytes32"], [(FOREIGN_EVM, bt, mo), acts, z]).hex())
        expect_reject("EVM: Settler recipient -> foreign", lambda: evm_guard.check("base", tampered, intent_b, prices, settler_live=live))
        expect_reject("EVM: unlimited approve(router, MAX)", lambda: evm_guard.check("base", evm_swap.build_approve(usdc, C.EVM_CHAINS["base"]["uni_swap_router02"], C.UINT256_MAX),
                      {"kind": "approve", "token": usdc, "amount": 10_000_000}, {}))
        expect_reject("EVM: approve to foreign spender", lambda: evm_guard.check("base", evm_swap.build_approve(usdc, FOREIGN_EVM, 10_000_000),
                      {"kind": "approve", "token": usdc, "amount": 10_000_000}, {}))
        expect_reject("EVM: Settler call with value raised to 1 ETH", lambda: evm_guard.check("base", dict(q["tx"], value=10**18), intent_b, prices, settler_live=live))
        expect_reject("EVM: router not the live registry Settler", lambda: evm_guard.check("base", dict(q["tx"], to=C.EVM_CHAINS["base"]["settlers"][1]), intent_b, prices, settler_live=live))
        expect_reject("EVM: plain ETH transfer to foreign", lambda: evm_guard.check("base", {"to": FOREIGN_EVM, "data": "0x", "value": 1}, intent_b, prices, settler_live=live))

        # ---------------- Base sell dry-run: 10 USDC -> WETH via SwapRouter02 (approve exact / swap / revoke)
        r = await eng.evm_sell("base", usdc, C.EVM_CHAINS["base"]["weth"], 10_000_000)
        out["base_sell"] = r
        print("Base USDC->WETH sell:", r["status"], [(s["step"], s["guard"]["verdict"]) for s in r.get("steps", [])], r.get("guard_errors", ""))
    (ROOT / "state" / "dryrun_results.json").write_text(json.dumps(out, indent=2, default=str))
    bad = [t for t in out.get("tamper", []) if t["result"] != "REJECTED"]
    print(f"\ntamper tests: {len(out.get('tamper', [])) - len(bad)}/{len(out.get('tamper', []))} rejected")

try:
    asyncio.run(main())
except Exception as e:
    import traceback; print("ERR", redact(traceback.format_exc(), 4000)); sys.exit(1)
