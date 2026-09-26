"""EVM quote/build.
BUY  (native ETH -> token): CDP swap API (SDK). Router = 0x Settler (taker-submitted). Native-ETH input needs no
     approval and no Permit2 signature. Any quote carrying Permit2 typed data is refused.
SELL (ERC-20 -> WETH/USDC): CDP's swap API requires a Permit2 EIP-712 signature + approval to Permit2 for
     ERC-20 input, which the owner's rules forbid, so sells use Uniswap V3 SwapRouter02.exactInputSingle
     (plain transferFrom, recipient field = bot) with approve(router, EXACT) before and approve(router, 0) after.
"""
import json
from eth_abi import encode, decode
from eth_utils import keccak, to_checksum_address
from . import config as C
from .net import eth_call

SEL = {k: "0x" + keccak(text=v)[:4].hex() for k, v in {
    "approve": "approve(address,uint256)", "allowance": "allowance(address,address)", "balanceOf": "balanceOf(address)",
    "exactInputSingle": "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))",
    "quoteExactInputSingle": "quoteExactInputSingle((address,address,uint256,uint24,uint160))",
    "settler_execute": "execute((address,address,uint256),bytes[],bytes32)", "withdraw": "withdraw(uint256)",
    "ownerOf": "ownerOf(uint256)"}.items()}
FEE_TIERS = (100, 500, 3000, 10000)


def settler_current(chain):
    r = eth_call(chain, C.SETTLER_REGISTRY, SEL["ownerOf"] + hex(C.SETTLER_FEATURE_TAKER_SUBMITTED)[2:].rjust(64, "0"))
    return to_checksum_address("0x" + r[-40:])


def allowance(chain, token, owner, spender):
    return int(eth_call(chain, token, SEL["allowance"] + encode(["address", "address"], [owner, spender]).hex()), 16)


def balance_of(chain, token, owner):
    return int(eth_call(chain, token, SEL["balanceOf"] + encode(["address"], [owner]).hex()), 16)


def uni_best_quote(chain, token_in, token_out, amount_in):
    q = C.EVM_CHAINS[chain]["uni_quoter_v2"]
    best = (None, 0)
    for fee in FEE_TIERS:
        try:
            r = eth_call(chain, q, SEL["quoteExactInputSingle"] + encode(
                ["(address,address,uint256,uint24,uint160)"], [(token_in, token_out, int(amount_in), fee, 0)]).hex())
            out = decode(["uint256", "uint160", "uint32", "uint256"], bytes.fromhex(r[2:]))[0]
            if out > best[1]:
                best = (fee, out)
        except Exception:
            continue
    return best


async def cdp_buy_quote(cdp, chain, to_token, wei, taker=None, slippage_bps=C.DEFAULT_SLIPPAGE_BPS):
    taker = taker or C.require_accounts()[0]
    from cdp.openapi_client.models.create_evm_swap_quote_request import CreateEvmSwapQuoteRequest
    resp = await cdp.api_clients.evm_swaps.create_evm_swap_quote_without_preload_content(CreateEvmSwapQuoteRequest(
        network=chain, from_token=C.NATIVE_ETH, to_token=to_token, from_amount=str(int(wei)), taker=taker,
        slippage_bps=slippage_bps))
    raw = json.loads(await resp.read())
    if not raw.get("liquidityAvailable"):
        raise ValueError(f"no liquidity: {str(raw)[:300]}")
    if raw.get("permit2"):
        raise ValueError("quote requires a Permit2 signature - refused by owner rules")
    tx = raw["transaction"]
    return {"tx": {"to": to_checksum_address(tx["to"]), "data": tx["data"], "value": int(tx.get("value") or 0),
                   "gas": int(tx["gas"]) if tx.get("gas") else None},
            "to_amount": int(raw["toAmount"]), "min_to_amount": int(raw["minToAmount"]), "issues": raw.get("issues"),
            "fees": raw.get("fees"), "block": raw.get("blockNumber")}


def build_approve(token, spender, amount):
    return {"to": to_checksum_address(token), "value": 0,
            "data": SEL["approve"] + encode(["address", "uint256"], [spender, int(amount)]).hex()}


def build_sell(chain, token_in, token_out, amount_in, slippage_bps=C.DEFAULT_SLIPPAGE_BPS, recipient=None):
    recipient = recipient or C.require_accounts()[0]
    fee, out = uni_best_quote(chain, token_in, token_out, amount_in)
    if not out:
        raise ValueError("no Uniswap V3 route")
    min_out = out * (10_000 - slippage_bps) // 10_000
    data = SEL["exactInputSingle"] + encode(["(address,address,uint24,address,uint256,uint256,uint160)"],
                                            [(token_in, token_out, fee, recipient, int(amount_in), min_out, 0)]).hex()
    return {"to": C.EVM_CHAINS[chain]["uni_swap_router02"], "data": data, "value": 0}, {"fee": fee, "quoted_out": out,
                                                                                       "min_out": min_out}


def build_unwrap(chain, amount):
    return {"to": C.EVM_CHAINS[chain]["weth"], "value": 0, "data": SEL["withdraw"] + encode(["uint256"], [int(amount)]).hex()}
