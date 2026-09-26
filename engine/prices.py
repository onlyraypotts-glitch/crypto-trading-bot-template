"""Independent USD prices used by the guards for size caps and min-out sanity checks."""
from . import config as C
from .net import jup_get, http_json, eth_call

STABLES = {C.EVM_CHAINS[c]["usdc"].lower() for c in C.EVM_CHAINS} | {C.USDC_MINT}


def sol_prices(mints):
    r = jup_get("/price/v3", {"ids": ",".join(mints)})
    return {m: float(r[m]["usdPrice"]) for m in mints if m in r and r[m].get("usdPrice")}, \
           {m: int(r[m]["decimals"]) for m in mints if m in r and r[m].get("decimals") is not None}


def eth_usd():
    """Coinbase public spot price (independent of the 0x/CDP swap quote and of Uniswap)."""
    return float(http_json("https://api.coinbase.com/v2/prices/ETH-USD/spot")["data"]["amount"])


def erc20_decimals(chain, token):
    if token.lower() == C.NATIVE_ETH.lower():
        return 18
    return int(eth_call(chain, token, "0x313ce567"), 16)


def evm_token_usd(chain, token, eth_price=None):
    t = token.lower()
    if t in STABLES:
        return 1.0
    if t in (C.NATIVE_ETH.lower(), C.EVM_CHAINS[chain]["weth"].lower()):
        return eth_price or eth_usd()
    # other tokens: best Uniswap V3 quote for 1 token -> WETH, times ETH/USD
    from .evm_swap import uni_best_quote
    dec = erc20_decimals(chain, token)
    fee, out = uni_best_quote(chain, token, C.EVM_CHAINS[chain]["weth"], 10 ** dec)
    if not out:
        raise ValueError(f"no independent price for {token} on {chain}")
    return out / 1e18 * (eth_price or eth_usd())
