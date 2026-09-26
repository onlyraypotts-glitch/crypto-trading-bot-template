"""Pre-sign guard for EVM transactions (Base / Ethereum). Decodes the exact {to, data, value} that would be passed
to CDP send_transaction. Fail-closed.

Allowed shapes only:
  buy     : to = 0x Settler (in config AND == live registry ownerOf(2) when online), execute(slippage,...),
            slippage.recipient == bot, buyToken == intended token, minAmountOut >= intended min, value == intent wei
  approve : to = allowlisted sellable token, approve(SwapRouter02 of that chain, EXACT sell amount), value 0
  revoke  : same but amount 0
  sell    : to = SwapRouter02, exactInputSingle(params) with recipient == bot, tokenIn/amountIn == intent,
            tokenOut in allowed outputs, amountOutMinimum >= intended min > 0, value 0
  unwrap  : to = WETH, withdraw(amount), value 0 (ETH returns to msg.sender = bot)
Plus: chain in {base, ethereum}; size <= MAX_POSITION_USD; min-out value >= (1-MAX_LOSS) * input value.
No Permit/Permit2, no increaseAllowance, no transfer/transferFrom, no multicall, no unknown selectors.
"""
from eth_abi import decode
from eth_utils import to_checksum_address
from . import config as C
from .guard_common import GuardError
from .evm_swap import SEL


def _eq(a, b):
    return a is not None and b is not None and a.lower() == b.lower()


def check(chain, tx, intent, prices, settler_live=None, bot=None):
    """intent: {kind, ...}; prices (buy/sell): {in_price, in_decimals, out_price, out_decimals} = independent
    per-whole-token USD prices. settler_live: registry ownerOf(2) (None only in offline tests -> static list used)."""
    bot = bot or C.BOT_EVM
    if not bot:
        raise GuardError("bot EVM address unknown (run create_accounts.py) - refusing")
    errs = []
    if chain not in C.EVM_CHAINS:
        raise GuardError(f"chain {chain} not allowed")
    cfg = C.EVM_CHAINS[chain]
    to, data, value = tx.get("to"), (tx.get("data") or "0x").lower(), int(tx.get("value") or 0)
    extra = set(tx) - {"to", "data", "value", "gas"}
    if extra:
        errs.append(f"unexpected tx fields {sorted(extra)}")
    if tx.get("gas") and int(tx["gas"]) > C.MAX_EVM_GAS_LIMIT:
        errs.append(f"gas limit {tx['gas']} > {C.MAX_EVM_GAS_LIMIT}")
    sel, body = data[:10], bytes.fromhex(data[10:]) if len(data) > 10 else b""
    kind = intent["kind"]
    router = cfg["uni_swap_router02"]
    report = {"chain": chain, "kind": kind, "to": to}
    try:
        if kind == "buy":
            if not any(_eq(to, s) for s in cfg["settlers"]):
                errs.append(f"to {to} is not a configured 0x Settler for {chain}")
            if settler_live is not None and not _eq(to, settler_live):
                errs.append(f"to {to} != live registry Settler {settler_live}")
            if sel != SEL["settler_execute"]:
                errs.append(f"selector {sel} is not Settler.execute")
            else:
                (rec, buy_tok, min_out), actions, _ = decode(["(address,address,uint256)", "bytes[]", "bytes32"], body)
                report.update(recipient=rec, buy_token=buy_tok, min_out=min_out, n_actions=len(actions))
                if not _eq(rec, bot):
                    errs.append(f"OUTPUT recipient {rec} is FOREIGN")
                if not _eq(buy_tok, intent["to_token"]):
                    errs.append(f"buyToken {buy_tok} != intended {intent['to_token']}")
                if min_out < int(intent["min_out"]) or min_out == 0:
                    errs.append(f"minAmountOut {min_out} < intended {intent['min_out']}")
            if value != int(intent["wei"]):
                errs.append(f"value {value} != intended {intent['wei']}")
        elif kind in ("approve", "revoke"):
            if not any(_eq(to, t) for t in C.EVM_SELLABLE_TOKENS[chain]):
                errs.append(f"token {to} not in sellable allowlist")
            if not _eq(to, intent["token"]):
                errs.append("approve target differs from intent token")
            if sel != SEL["approve"]:
                errs.append(f"selector {sel} is not approve (no permit/increaseAllowance/etc)")
            else:
                spender, amt = decode(["address", "uint256"], body)
                report.update(spender=spender, amount=amt)
                if not _eq(spender, router):
                    errs.append(f"spender {spender} is not SwapRouter02")
                want = 0 if kind == "revoke" else int(intent["amount"])
                if amt >= C.POLICY_MAX_APPROVE_VALUE or amt == C.UINT256_MAX:
                    errs.append(f"UNLIMITED/huge approval {amt}")
                if amt != want:
                    errs.append(f"approval amount {amt} != exact {want}")
                if kind == "approve" and want <= 0:
                    errs.append("approve of zero is a revoke")
            if value:
                errs.append("value must be 0")
        elif kind == "sell":
            if not _eq(to, router):
                errs.append(f"to {to} is not SwapRouter02")
            if sel != SEL["exactInputSingle"]:
                errs.append(f"selector {sel} is not exactInputSingle")
            else:
                (t_in, t_out, fee, rec, amt_in, min_out, _lim), = decode(["(address,address,uint24,address,uint256,uint256,uint160)"], body)
                report.update(token_in=t_in, token_out=t_out, fee=fee, recipient=rec, amount_in=amt_in, min_out=min_out)
                if not _eq(rec, bot):
                    errs.append(f"OUTPUT recipient {rec} is FOREIGN")
                if not _eq(t_in, intent["token_in"]) or amt_in != int(intent["amount_in"]):
                    errs.append("tokenIn/amountIn differ from intent")
                if not any(_eq(t_out, o) for o in C.EVM_SELL_OUTPUTS[chain]) or not _eq(t_out, intent["token_out"]):
                    errs.append(f"tokenOut {t_out} not allowed/intended")
                if min_out == 0 or min_out < int(intent["min_out"]):
                    errs.append(f"amountOutMinimum {min_out} < intended {intent['min_out']}")
            if value:
                errs.append("value must be 0")
        elif kind == "unwrap":
            if not _eq(to, cfg["weth"]) or sel != SEL["withdraw"] or value:
                errs.append("unwrap must be WETH.withdraw with value 0")
        else:
            errs.append(f"unknown intent kind {kind}")
    except Exception as e:
        errs.append(f"calldata decode failed: {type(e).__name__}: {e}")
    if kind in ("buy", "sell") and "min_out" in report:
        # USD values computed from DECODED calldata amounts x independent unit prices (not from the quote API)
        try:
            amt_in = value if kind == "buy" else report["amount_in"]
            in_usd = amt_in / 10 ** prices["in_decimals"] * prices["in_price"]
            mo_usd = report["min_out"] / 10 ** prices["out_decimals"] * prices["out_price"]
        except (KeyError, TypeError):
            errs.append("no independent USD prices - refusing")
        else:
            report.update(in_usd=round(in_usd, 4), min_out_usd=round(mo_usd, 4))
            if in_usd > C.MAX_POSITION_USD:
                errs.append(f"size ${in_usd:.2f} > cap ${C.MAX_POSITION_USD}")
            if mo_usd < in_usd * (1 - C.MAX_LOSS_VS_ORACLE_BPS / 10_000):
                errs.append(f"min-out ${mo_usd:.2f} vs input ${in_usd:.2f}: worse than {C.MAX_LOSS_VS_ORACLE_BPS}bps")
    if errs:
        raise GuardError(errs)
    report["verdict"] = "PASS"
    return report
