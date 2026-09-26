"""Orchestration: quote -> build -> GUARD -> [DRY_RUN stops here] -> kill-switch re-check -> CDP sign+send.
The guard runs on the exact bytes / {to,data,value} handed to CDP. DRY_RUN (default) never calls any CDP signing
or sending endpoint. Kill switch: if KILL_SWITCH file exists nothing is signed/sent and no cycle starts."""
import asyncio, json, time
from . import config as C, sol_tx, sol_guard, evm_guard, evm_swap, prices as PX
from .guard_common import GuardError
from .net import sol_rpc, evm_rpc


class KillSwitch(Exception):
    pass


def kill_check():
    if C.KILL_SWITCH_FILE.exists():
        raise KillSwitch(f"kill switch present: {C.KILL_SWITCH_FILE}")


def log_trade(rec):
    C.STATE_DIR.mkdir(exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **rec}
    with open(C.TRADE_LOG, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def live_trade_count():
    if not C.TRADE_LOG.exists():
        return 0
    n = 0
    for line in C.TRADE_LOG.read_text().splitlines():
        r = json.loads(line)
        n += r.get("mode") == "live" and r.get("status") == "confirmed" and r.get("kind") in ("sol_swap", "evm_buy", "evm_sell")
    return n


def position_cap():
    # Owner rule: max $100/position until 20 logged trades. Raising it is a manual owner decision (config).
    return C.MAX_POSITION_USD


class Engine:
    def __init__(self, cdp=None, dry_run=None):
        self.cdp = cdp
        self.dry_run = C.dry_run() if dry_run is None else (dry_run or C.dry_run())  # can force dry, never force live

    # ------------------------------------------------------------ Solana
    async def sol_swap(self, input_mint, output_mint, usd=None, amount=None, slippage_bps=C.DEFAULT_SLIPPAGE_BPS):
        kill_check()
        C.require_accounts()
        px, dec = PX.sol_prices([input_mint, output_mint])
        if amount is None:
            amount = int(usd / px[input_mint] * 10 ** dec[input_mint])
        b = sol_tx.jupiter_build(input_mint, output_mint, amount, C.BOT_SOL, slippage_bps)
        raw = sol_tx.assemble(b, C.BOT_SOL)
        alts = sol_tx.fetch_alts_onchain(list((b.get("addressesByLookupTableAddress") or {}).keys()))  # from chain
        intent = {"input_mint": input_mint, "output_mint": output_mint, "in_amount": amount, "decimals": dec}
        rep = {"kind": "sol_swap", "mode": "dry_run" if self.dry_run else "live", "intent": intent,
               "quote": {k: b[k] for k in ("inAmount", "outAmount", "otherAmountThreshold", "slippageBps", "priceImpactPct")},
               "route": [r["swapInfo"]["label"] for r in b["routePlan"]], "prices": px}
        try:
            rep["guard"] = sol_guard.check(raw, intent, alts, px)
        except GuardError as g:
            rep.update(status="guard_rejected", guard_errors=g.reasons); log_trade(rep); return rep
        rep["simulation"] = self._sol_simulate(raw)
        if self.dry_run:
            rep["status"] = "dry_run_ok"; log_trade(rep); return rep
        kill_check()
        sig = await self.cdp.solana.send_transaction(network=C.SOL_NETWORK, transaction=sol_tx.tx_b64(raw))
        rep.update(signature=sig, status=await self._sol_wait(sig)); log_trade(rep); return rep

    def _sol_simulate(self, raw):
        try:
            r = sol_rpc("simulateTransaction", [sol_tx.tx_b64(raw), {"encoding": "base64", "sigVerify": False,
                                                                    "replaceRecentBlockhash": True}])
            v = r["value"]
            return {"err": v.get("err"), "units": v.get("unitsConsumed"), "logs_tail": (v.get("logs") or [])[-3:]}
        except Exception as e:
            return {"error": str(e)[:300]}

    async def _sol_wait(self, sig, timeout=90):
        t = time.time()
        while time.time() - t < timeout:
            st = sol_rpc("getSignatureStatuses", [[sig]])["value"][0]
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                return "confirmed" if st.get("err") is None else f"failed:{st['err']}"
            await asyncio.sleep(2)
        return "unknown_timeout"

    # ------------------------------------------------------------ EVM
    async def evm_buy(self, chain, to_token, usd, slippage_bps=C.DEFAULT_SLIPPAGE_BPS):
        kill_check()
        C.require_accounts()
        eth = PX.eth_usd()
        wei = int(usd / eth * 1e18)
        q = await evm_swap.cdp_buy_quote(self.cdp, chain, to_token, wei, C.BOT_EVM, slippage_bps)
        tx = q["tx"]
        prices = {"in_price": eth, "in_decimals": 18, "out_price": PX.evm_token_usd(chain, to_token, eth),
                  "out_decimals": PX.erc20_decimals(chain, to_token)}
        intent = {"kind": "buy", "to_token": to_token, "wei": wei, "min_out": q["min_to_amount"]}
        rep = {"kind": "evm_buy", "chain": chain, "mode": "dry_run" if self.dry_run else "live", "intent": intent,
               "quote": {k: q[k] for k in ("to_amount", "min_to_amount", "issues", "fees", "block")}, "eth_usd": eth}
        try:
            rep["guard"] = evm_guard.check(chain, tx, intent, prices, settler_live=evm_swap.settler_current(chain))
        except GuardError as g:
            rep.update(status="guard_rejected", guard_errors=g.reasons); log_trade(rep); return rep
        if self.dry_run:
            rep["status"] = "dry_run_ok"; log_trade(rep); return rep
        rep.update(await self._evm_send(chain, tx)); log_trade(rep); return rep

    async def evm_sell(self, chain, token_in, token_out, amount_in, emergency=False, slippage_bps=C.DEFAULT_SLIPPAGE_BPS):
        """approve(router, EXACT) -> exactInputSingle(recipient=bot) -> approve(router, 0).
        If a previous revoke failed (allowance outstanding), only emergency/stop-loss sells may proceed."""
        kill_check()
        C.require_accounts()
        router = C.EVM_CHAINS[chain]["uni_swap_router02"]
        outstanding = evm_swap.allowance(chain, token_in, C.BOT_EVM, router)
        rep = {"kind": "evm_sell", "chain": chain, "mode": "dry_run" if self.dry_run else "live", "emergency": emergency,
               "outstanding_allowance": outstanding, "steps": []}
        if outstanding and not emergency:
            rep.update(status="blocked_outstanding_allowance"); log_trade(rep); return rep
        eth = PX.eth_usd()
        prices = {"in_price": PX.evm_token_usd(chain, token_in, eth), "in_decimals": PX.erc20_decimals(chain, token_in),
                  "out_price": PX.evm_token_usd(chain, token_out, eth), "out_decimals": PX.erc20_decimals(chain, token_out)}
        swap_tx, q = evm_swap.build_sell(chain, token_in, token_out, amount_in, slippage_bps)
        plan = [("approve", evm_swap.build_approve(token_in, router, amount_in), {"kind": "approve", "token": token_in, "amount": amount_in}),
                ("sell", swap_tx, {"kind": "sell", "token_in": token_in, "token_out": token_out, "amount_in": amount_in, "min_out": q["min_out"]}),
                ("revoke", evm_swap.build_approve(token_in, router, 0), {"kind": "revoke", "token": token_in})]
        try:
            for name, tx, intent in plan:     # guard ALL steps before signing anything
                rep["steps"].append({"step": name, "guard": evm_guard.check(chain, tx, intent, prices)})
        except GuardError as g:
            rep.update(status="guard_rejected", guard_errors=g.reasons); log_trade(rep); return rep
        if self.dry_run:
            rep["status"] = "dry_run_ok"; log_trade(rep); return rep
        for i, (name, tx, _) in enumerate(plan):
            res = await self._evm_send(chain, tx)
            rep["steps"][i].update(res)
            if res["status"] != "confirmed":
                if name == "revoke":
                    self._record_outstanding(chain, token_in, router, amount_in)
                    rep["status"] = "sold_but_REVOKE_FAILED"
                elif name == "sell":
                    r2 = await self._evm_send(chain, plan[2][1])      # swap failed -> revoke immediately
                    rep["steps"].append({"step": "revoke_after_failed_sell", **r2})
                    if r2["status"] != "confirmed":
                        self._record_outstanding(chain, token_in, router, amount_in)
                    rep["status"] = "sell_failed"
                else:
                    rep["status"] = "approve_failed"
                break
        else:
            rep["status"] = "confirmed"
        log_trade(rep); return rep

    def _record_outstanding(self, chain, token, spender, amount):
        st = json.loads(C.ALLOWANCE_STATE.read_text()) if C.ALLOWANCE_STATE.exists() else []
        st.append({"chain": chain, "token": token, "spender": spender, "amount": amount, "ts": time.time()})
        C.ALLOWANCE_STATE.write_text(json.dumps(st, indent=2))

    async def _evm_send(self, chain, tx):
        from cdp.evm_transaction_types import TransactionRequestEIP1559
        kill_check()
        h = await self.cdp.evm.send_transaction(address=C.BOT_EVM, network=chain, transaction=TransactionRequestEIP1559(
            to=tx["to"], data=tx["data"], value=int(tx["value"]), **({"gas": tx["gas"]} if tx.get("gas") else {})))
        t = time.time()
        while time.time() - t < 180:
            r = evm_rpc(chain, "eth_getTransactionReceipt", [h])
            if r:
                return {"tx_hash": h, "status": "confirmed" if r["status"] == "0x1" else "reverted"}
            await asyncio.sleep(3)
        return {"tx_hash": h, "status": "unknown_timeout"}
