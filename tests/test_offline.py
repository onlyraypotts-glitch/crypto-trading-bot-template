"""Offline self-tests (no network, no CDP). Run: python -m unittest tests/test_offline.py -v"""
import copy, json, os, struct, sys, tempfile, unittest, base64
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Randomly generated throwaway test addresses (nobody's wallet). The fixtures were rewritten to use them, so the
# tests never depend on a real accounts.json. They must be set BEFORE engine.config is imported.
TEST_BOT_EVM = "0xA4Af779801f97576101A18f3Ff6323D22a42a112"
TEST_BOT_SOL = "6YSWw2xvR6FL6FtA3Q6VxoXZXgQ1GSKMbnbfS43QJjKK"
os.environ["BOT_EVM_ADDRESS"] = TEST_BOT_EVM
os.environ["BOT_SOL_ADDRESS"] = TEST_BOT_SOL
from eth_abi import encode, decode
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from engine import config as C, sol_tx, sol_guard, evm_guard, evm_swap, policy as P, executor
from engine.guard_common import GuardError

FX = ROOT / "tests" / "fixtures"
from solders.keypair import Keypair
FOREIGN_SOL = str(Keypair().pubkey())   # random, nobody's
FOREIGN_EVM = "0x000000000000000000000000000000000000dEaD"
ME, FR = Pubkey.from_string(C.BOT_SOL), Pubkey.from_string(FOREIGN_SOL)
SYS, TOK = Pubkey.from_string(C.SYSTEM_PROGRAM), Pubkey.from_string(C.TOKEN_PROGRAM)


class SolGuard(unittest.TestCase):
    def setUp(self):
        self.b = json.loads((FX / "jup_build_sol_bonk.json").read_text())
        self.alts = sol_tx.alts_from_build(self.b)
        self.amt = int(self.b["inAmount"])
        self.intent = {"input_mint": C.WSOL_MINT, "output_mint": C.BONK_MINT, "in_amount": self.amt,
                       "decimals": {C.WSOL_MINT: 9, C.BONK_MINT: 5}}
        sol_px = 121.07
        self.px = {C.WSOL_MINT: sol_px, C.BONK_MINT: self.amt / 1e9 * sol_px / (int(self.b["outAmount"]) / 1e5)}

    def chk(self, b=None, extra=(), intent=None, alts=None, px=None, **kw):
        raw = sol_tx.assemble(b or self.b, C.BOT_SOL, extra_ixs=extra, **kw)
        return sol_guard.check(raw, intent or self.intent, self.alts if alts is None else alts, px or self.px)

    def rej(self, *a, **k):
        with self.assertRaises(GuardError) as cm:
            self.chk(*a, **k)
        return " | ".join(cm.exception.reasons)

    def test_baseline_passes(self):
        r = self.chk()
        self.assertEqual(r["verdict"], "PASS")
        self.assertEqual(r["jupiter"]["name"], "shared_accounts_route_v2")

    def test_foreign_sol_transfer(self):
        x = Instruction(SYS, (2).to_bytes(4, "little") + (5).to_bytes(8, "little"), [AccountMeta(ME, True, True), AccountMeta(FR, False, True)])
        self.assertIn("FOREIGN", self.rej(extra=[x]))

    def test_foreign_output_ata(self):
        b = copy.deepcopy(self.b); b["swapInstruction"]["accounts"][5]["pubkey"] = sol_guard.ata(FOREIGN_SOL, C.BONK_MINT, C.TOKEN_PROGRAM)
        self.assertIn("foreign destination", self.rej(b))

    def test_foreign_swap_authority(self):
        b = copy.deepcopy(self.b); b["swapInstruction"]["accounts"][1]["pubkey"] = FOREIGN_SOL
        self.rej(b)

    def test_spl_transfer_and_approve_rejected(self):
        ata = Pubkey.from_string(sol_guard.ata(C.BOT_SOL, C.BONK_MINT, C.TOKEN_PROGRAM))
        t = Instruction(TOK, bytes([3]) + (1).to_bytes(8, "little"), [AccountMeta(ata, False, True), AccountMeta(FR, False, True), AccountMeta(ME, True, False)])
        a = Instruction(TOK, bytes([4]) + (2**64 - 1).to_bytes(8, "little"), [AccountMeta(ata, False, True), AccountMeta(FR, False, False), AccountMeta(ME, True, False)])
        self.assertIn("not allowed", self.rej(extra=[t]))
        self.assertIn("not allowed", self.rej(extra=[a]))

    def test_close_to_foreign(self):
        b = copy.deepcopy(self.b); b["cleanupInstruction"]["accounts"][1]["pubkey"] = FOREIGN_SOL
        self.assertIn("FOREIGN", self.rej(b))

    def test_unknown_program(self):
        m = Instruction(Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"), b"x", [])
        self.assertIn("not allowlisted", self.rej(extra=[m]))

    def _patch_data(self, fn):
        b = copy.deepcopy(self.b)
        d = bytearray(base64.b64decode(b["swapInstruction"]["data"])); fn(d)
        b["swapInstruction"]["data"] = base64.b64encode(bytes(d)).decode()
        return b

    def test_exact_out_discriminator_rejected(self):
        b = self._patch_data(lambda d: d.__setitem__(slice(0, 8), bytes.fromhex("3560e5cad8bbfa18")))
        self.assertIn("not allowed", self.rej(b))

    def test_platform_fee_rejected(self):
        b = self._patch_data(lambda d: d.__setitem__(slice(8 + 1 + 16 + 2, 8 + 1 + 16 + 4), (50).to_bytes(2, "little")))
        self.assertIn("fee", self.rej(b))

    def test_slippage_too_high(self):
        b = self._patch_data(lambda d: d.__setitem__(slice(8 + 1 + 16, 8 + 1 + 16 + 2), (900).to_bytes(2, "little")))
        self.assertIn("slippage", self.rej(b))

    def test_in_amount_mismatch(self):
        self.assertIn("in_amount", self.rej(intent={**self.intent, "in_amount": self.amt - 1}))

    def test_size_cap(self):
        self.assertIn("cap", self.rej(px={**self.px, C.WSOL_MINT: 121.07 * 20, C.BONK_MINT: self.px[C.BONK_MINT] * 20}))

    def test_bad_min_out_vs_oracle(self):
        self.assertIn("worse than", self.rej(px={**self.px, C.BONK_MINT: self.px[C.BONK_MINT] * 0.5}))

    def test_missing_price_fails_closed(self):
        self.assertIn("no independent price", self.rej(px={C.WSOL_MINT: 121.0}))

    def test_unresolved_alt(self):
        self.assertIn("not resolved", self.rej(alts={}))

    def test_wrong_fee_payer(self):
        raw = sol_tx.assemble(self.b, FOREIGN_SOL)
        with self.assertRaises(GuardError):
            sol_guard.check(raw, self.intent, self.alts, self.px)

    def test_priority_fee_cap(self):
        b = copy.deepcopy(self.b)
        b["computeBudgetInstructions"] = [{"programId": C.COMPUTE_BUDGET_PROGRAM, "accounts": [],
                                           "data": base64.b64encode(bytes([3]) + (10**7).to_bytes(8, "little")).decode()}]
        self.assertIn("priority fee", self.rej(b))

    def test_tip_instruction_rejected(self):
        b = copy.deepcopy(self.b)
        b["tipInstruction"] = {"programId": C.SYSTEM_PROGRAM, "accounts": [{"pubkey": C.BOT_SOL, "isSigner": True, "isWritable": True},
                               {"pubkey": FOREIGN_SOL, "isSigner": False, "isWritable": True}],
                               "data": base64.b64encode((2).to_bytes(4, "little") + (1000).to_bytes(8, "little")).decode()}
        self.rej(b)


class EvmGuard(unittest.TestCase):
    def setUp(self):
        q = json.loads((FX / "cdp_quote_base_eth_usdc.json").read_text())
        t = q["transaction"]
        self.tx = {"to": t["to"], "data": t["data"], "value": int(t["value"])}
        self.usdc = C.EVM_CHAINS["base"]["usdc"]; self.weth = C.EVM_CHAINS["base"]["weth"]
        self.router = C.EVM_CHAINS["base"]["uni_swap_router02"]
        self.intent = {"kind": "buy", "to_token": self.usdc, "wei": int(t["value"]), "min_out": int(q["minToAmount"])}
        self.px = {"in_price": 2680.0, "in_decimals": 18, "out_price": 1.0, "out_decimals": 6}
        self.live = "0x4f6f91599858bf0d19fabcf2c5d591fe13f7c059"

    def rej(self, chain, tx, intent, px=None, live=None):
        with self.assertRaises(GuardError) as cm:
            evm_guard.check(chain, tx, intent, px if px is not None else self.px, settler_live=live)
        return " | ".join(cm.exception.reasons)

    def _settler(self, **over):
        (rec, bt, mo), acts, z = decode(["(address,address,uint256)", "bytes[]", "bytes32"], bytes.fromhex(self.tx["data"][10:]))
        rec, bt, mo = over.get("rec", rec), over.get("bt", bt), over.get("mo", mo)
        return dict(self.tx, data=evm_swap.SEL["settler_execute"] + encode(["(address,address,uint256)", "bytes[]", "bytes32"], [(rec, bt, mo), acts, z]).hex())

    def test_buy_passes(self):
        self.assertEqual(evm_guard.check("base", self.tx, self.intent, self.px, settler_live=self.live)["verdict"], "PASS")

    def test_buy_foreign_recipient(self):
        self.assertIn("FOREIGN", self.rej("base", self._settler(rec=FOREIGN_EVM), self.intent))

    def test_buy_wrong_token_or_minout(self):
        self.rej("base", self._settler(bt=self.weth), self.intent)
        self.rej("base", self._settler(mo=1), self.intent)

    def test_buy_not_live_settler(self):
        self.rej("base", self.tx, self.intent, live=C.EVM_CHAINS["base"]["settlers"][1])

    def test_buy_value_tamper_and_size(self):
        self.rej("base", dict(self.tx, value=self.tx["value"] * 50), self.intent)
        self.assertIn("cap", self.rej("base", self.tx, self.intent, px={**self.px, "in_price": 2680.0 * 50, "out_price": 50}))

    def test_disallowed_chain(self):
        self.rej("arbitrum", self.tx, self.intent)

    def test_approve_exact_passes_and_unlimited_rejected(self):
        ok = evm_swap.build_approve(self.usdc, self.router, 10_000_000)
        self.assertEqual(evm_guard.check("base", ok, {"kind": "approve", "token": self.usdc, "amount": 10_000_000}, {})["verdict"], "PASS")
        self.assertIn("UNLIMITED", self.rej("base", evm_swap.build_approve(self.usdc, self.router, C.UINT256_MAX), {"kind": "approve", "token": self.usdc, "amount": 10_000_000}, {}))
        self.assertIn("exact", self.rej("base", evm_swap.build_approve(self.usdc, self.router, 10_000_001), {"kind": "approve", "token": self.usdc, "amount": 10_000_000}, {}))
        self.rej("base", evm_swap.build_approve(self.usdc, FOREIGN_EVM, 10_000_000), {"kind": "approve", "token": self.usdc, "amount": 10_000_000}, {})

    def test_revoke_passes(self):
        self.assertEqual(evm_guard.check("base", evm_swap.build_approve(self.usdc, self.router, 0), {"kind": "revoke", "token": self.usdc}, {})["verdict"], "PASS")

    def test_non_allowlisted_token_and_other_selectors(self):
        rnd = "0x1234567890123456789012345678901234567890"
        self.rej("base", evm_swap.build_approve(rnd, self.router, 5), {"kind": "approve", "token": rnd, "amount": 5}, {})
        transfer = {"to": self.usdc, "value": 0, "data": "0xa9059cbb" + encode(["address", "uint256"], [FOREIGN_EVM, 5]).hex()}
        self.rej("base", transfer, {"kind": "approve", "token": self.usdc, "amount": 5}, {})
        permit = {"to": self.usdc, "value": 0, "data": "0xd505accf" + "00" * 224}
        self.rej("base", permit, {"kind": "approve", "token": self.usdc, "amount": 5}, {})

    def _sell(self, rec=C.BOT_EVM, t_out=None, min_out=3_000_000_000_000_000):
        t_out = t_out or self.weth
        return {"to": self.router, "value": 0, "data": evm_swap.SEL["exactInputSingle"] + encode(
            ["(address,address,uint24,address,uint256,uint256,uint160)"], [(self.usdc, t_out, 500, rec, 10_000_000, min_out, 0)]).hex()}

    def sell_intent(self, t_out=None, min_out=3_000_000_000_000_000):
        return {"kind": "sell", "token_in": self.usdc, "token_out": t_out or self.weth, "amount_in": 10_000_000, "min_out": min_out}

    def test_sell(self):
        px = {"in_price": 1.0, "in_decimals": 6, "out_price": 3300.0, "out_decimals": 18}
        self.assertEqual(evm_guard.check("base", self._sell(), self.sell_intent(), px)["verdict"], "PASS")
        self.assertIn("FOREIGN", self.rej("base", self._sell(rec=FOREIGN_EVM), self.sell_intent(), px))
        rnd = "0x1234567890123456789012345678901234567890"
        self.rej("base", self._sell(t_out=rnd), self.sell_intent(t_out=rnd), px)
        self.rej("base", self._sell(min_out=0), self.sell_intent(min_out=0), px)
        self.assertIn("worse than", self.rej("base", self._sell(), self.sell_intent(), {**px, "out_price": 1000.0}))

    def test_unwrap(self):
        self.assertEqual(evm_guard.check("base", evm_swap.build_unwrap("base", 5), {"kind": "unwrap"}, {})["verdict"], "PASS")
        self.rej("base", dict(evm_swap.build_unwrap("base", 5), value=1), {"kind": "unwrap"}, {})


class Switches(unittest.TestCase):
    def test_dry_run_default_and_two_key_live(self):
        env = {k: os.environ.pop(k) for k in ("BOT_DRY_RUN", "BOT_LIVE_ACK") if k in os.environ}
        try:
            self.assertTrue(C.dry_run())
            os.environ["BOT_DRY_RUN"] = "false"; self.assertTrue(C.dry_run())
            os.environ["BOT_LIVE_ACK"] = "I-ACCEPT-LIVE-TRADING"; self.assertFalse(C.dry_run())
            os.environ["BOT_DRY_RUN"] = "true"; self.assertTrue(C.dry_run())
            del os.environ["BOT_DRY_RUN"], os.environ["BOT_LIVE_ACK"]
            self.assertTrue(executor.Engine(None, dry_run=False).dry_run)   # cannot force live from code
        finally:
            os.environ.pop("BOT_DRY_RUN", None); os.environ.pop("BOT_LIVE_ACK", None); os.environ.update(env)

    def test_kill_switch(self):
        orig = C.KILL_SWITCH_FILE
        with tempfile.TemporaryDirectory() as d:
            C.KILL_SWITCH_FILE = Path(d) / "KILL_SWITCH"
            try:
                executor.kill_check()
                C.KILL_SWITCH_FILE.write_text("stop")
                with self.assertRaises(executor.KillSwitch):
                    executor.kill_check()
                import asyncio
                with self.assertRaises(executor.KillSwitch):
                    asyncio.run(executor.Engine(None).sol_swap(C.WSOL_MINT, C.BONK_MINT, usd=10))
            finally:
                C.KILL_SWITCH_FILE = orig

    def test_position_cap(self):
        self.assertEqual(executor.position_cap(), 100.0)

    def test_test_addresses_in_use(self):
        self.assertEqual((C.BOT_EVM, C.BOT_SOL), (TEST_BOT_EVM, TEST_BOT_SOL))


class PolicyDoc(unittest.TestCase):
    def test_mainnet_policy_shape(self):
        evm, sol = P.mainnet_policies()
        for d in (evm, sol):
            self.assertEqual(d["scope"], "account")
            accepts = {r["operation"] for r in d["rules"] if r["action"] == "accept"}
            self.assertTrue(accepts <= {"sendEvmTransaction", "sendSolTransaction"}, accepts)
        s = json.dumps(evm)
        self.assertIn(C.BOT_EVM, s); self.assertNotIn(str(C.UINT256_MAX), s)
        self.assertEqual({r["operation"] for r in evm["rules"] if r["action"] == "reject"}, {"signEvmHash", "signEvmMessage", "signEvmTypedData"})
        usd = [c for r in evm["rules"] for c in r.get("criteria", []) if c["type"] == "netUSDChange"]
        self.assertTrue(all(c["changeCents"] == 15000 for c in usd) and len(usd) == 2)
        crit = {c["type"]: c for c in sol["rules"][0]["criteria"]}
        self.assertEqual(set(crit["programId"]["programIds"]), set(C.SOL_PROGRAM_ALLOWLIST))
        self.assertEqual(crit["solAddress"]["addresses"], [C.BOT_SOL, P.ata(C.BOT_SOL, C.WSOL_MINT)])
        self.assertEqual(crit["solNetwork"]["networks"], ["solana"])


if __name__ == "__main__":
    unittest.main()
