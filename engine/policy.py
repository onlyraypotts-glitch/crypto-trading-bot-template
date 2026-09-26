"""Build CDP policy documents (OpenAPI wire format) from engine config.

Two account-scoped policies: one for the bot EVM account, one for the bot Solana account. Default-deny: anything not matching an
accept rule (including every operation type not listed: signEvmTransaction, signSolTransaction, user ops, ...)
is rejected by CDP. Explicit rejects for hash/message/typed-data signing are included for clarity.
"""
from solders.pubkey import Pubkey
from . import config as C

SETTLER_EXECUTE_ABI = {
    "type": "function", "name": "execute", "stateMutability": "payable", "outputs": [{"name": "", "type": "bool"}],
    "inputs": [
        {"name": "slippage", "type": "tuple", "internalType": "struct ISettlerBase.AllowedSlippage",
         "components": [{"name": "recipient", "type": "address"}, {"name": "buyToken", "type": "address"},
                        {"name": "minAmountOut", "type": "uint256"}]},
        {"name": "actions", "type": "bytes[]"},
        {"name": "zidAndAffiliate", "type": "bytes32"},
    ],
}
UNI_EXACT_INPUT_SINGLE_ABI = {
    "type": "function", "name": "exactInputSingle", "stateMutability": "payable",
    "outputs": [{"name": "amountOut", "type": "uint256"}],
    "inputs": [{"name": "params", "type": "tuple", "internalType": "struct IV3SwapRouter.ExactInputSingleParams",
                "components": [{"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
                               {"name": "fee", "type": "uint24"}, {"name": "recipient", "type": "address"},
                               {"name": "amountIn", "type": "uint256"}, {"name": "amountOutMinimum", "type": "uint256"},
                               {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
}
WETH_WITHDRAW_ABI = {"type": "function", "name": "withdraw", "stateMutability": "nonpayable", "outputs": [],
                     "inputs": [{"name": "wad", "type": "uint256"}]}


def ata(owner: str, mint: str, token_program: str = C.TOKEN_PROGRAM) -> str:
    return str(Pubkey.find_program_address(
        [bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(token_program)), bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(C.ATA_PROGRAM))[0])


def evm_policy(self_addr, networks, settlers, uni_routers, tokens, wethes, max_usd_cents, max_eth_wei,
               max_approve, nested_param_names=True):
    """nested_param_names: use 'slippage.recipient' / 'params.recipient' dot notation (verified on testnet)."""
    rec_settler = "slippage.recipient" if nested_param_names else "0"
    rec_uni = "params.recipient" if nested_param_names else "0"
    net = {"type": "evmNetwork", "networks": networks, "operator": "in"}
    usd = {"type": "netUSDChange", "changeCents": max_usd_cents, "operator": "<="}
    rules = [
        {"action": "accept", "operation": "sendEvmTransaction", "criteria": [
            net, {"type": "evmAddress", "addresses": settlers, "operator": "in"},
            {"type": "ethValue", "ethValue": str(max_eth_wei), "operator": "<="}, usd,
            {"type": "evmData", "abi": [SETTLER_EXECUTE_ABI], "conditions": [
                {"function": "execute", "params": [{"name": rec_settler, "operator": "==", "value": self_addr}]}]}]},
        {"action": "accept", "operation": "sendEvmTransaction", "criteria": [
            net, {"type": "evmAddress", "addresses": uni_routers, "operator": "in"},
            {"type": "ethValue", "ethValue": "0", "operator": "<="}, usd,
            {"type": "evmData", "abi": [UNI_EXACT_INPUT_SINGLE_ABI], "conditions": [
                {"function": "exactInputSingle", "params": [{"name": rec_uni, "operator": "==", "value": self_addr}]}]}]},
        {"action": "accept", "operation": "sendEvmTransaction", "criteria": [
            net, {"type": "evmAddress", "addresses": tokens, "operator": "in"},
            {"type": "ethValue", "ethValue": "0", "operator": "<="},
            {"type": "evmData", "abi": "erc20", "conditions": [
                {"function": "approve", "params": [
                    {"name": "spender", "operator": "in", "values": uni_routers},
                    {"name": "value", "operator": "<=", "value": str(max_approve)}]}]}]},
        {"action": "accept", "operation": "sendEvmTransaction", "criteria": [
            net, {"type": "evmAddress", "addresses": wethes, "operator": "in"},
            {"type": "ethValue", "ethValue": "0", "operator": "<="},
            {"type": "evmData", "abi": [WETH_WITHDRAW_ABI], "conditions": [{"function": "withdraw"}]}]},
        {"action": "reject", "operation": "signEvmHash"},
        {"action": "reject", "operation": "signEvmMessage", "criteria": [{"type": "evmMessage", "match": ".*"}]},
        {"action": "reject", "operation": "signEvmTypedData", "criteria": [
            {"type": "evmTypedDataVerifyingContract", "addresses": ["0x0000000000000000000000000000000000000001"],
             "operator": "not in"}]},
    ]
    return rules


def sol_policy(self_addr, network, policy_mints: dict, max_lamports):
    prog = {"token": C.TOKEN_PROGRAM, "token2022": C.TOKEN_2022_PROGRAM}
    wsol_ata = ata(self_addr, C.WSOL_MINT)
    spl_self = sorted({ata(self_addr, m, prog[p]) for m, p in policy_mints.items()})
    return [
        {"action": "accept", "operation": "sendSolTransaction", "criteria": [
            {"type": "solNetwork", "networks": [network], "operator": "in"},
            {"type": "programId", "programIds": C.SOL_PROGRAM_ALLOWLIST, "operator": "in"},
            {"type": "solAddress", "addresses": [self_addr, wsol_ata], "operator": "in"},
            {"type": "splAddress", "addresses": spl_self, "operator": "in"},
            {"type": "solValue", "solValue": str(max_lamports), "operator": "<="}]},
        {"action": "reject", "operation": "signSolMessage", "criteria": [{"type": "solMessage", "match": ".*"}]},
    ]


def mainnet_policies():
    bot_evm, bot_sol = C.require_accounts()      # from accounts.json / env, never hard-coded
    chains = C.EVM_CHAINS
    settlers = [a for c in chains.values() for a in c["settlers"]]
    routers = [c["uni_swap_router02"] for c in chains.values()]
    tokens = sorted({t for l in C.EVM_SELLABLE_TOKENS.values() for t in l})
    wethes = [c["weth"] for c in chains.values()]
    evm = {"scope": "account", "description": f"trading bot evm swaps to self only, {C.POLICY_MAX_TX_USD} USD cap",
           "rules": evm_policy(bot_evm, ["base", "ethereum"], settlers, routers, tokens, wethes,
                               C.POLICY_MAX_TX_USD * 100, C.POLICY_MAX_ETH_VALUE_WEI, C.POLICY_MAX_APPROVE_VALUE)}
    sol = {"scope": "account", "description": "trading bot sol Jupiter swaps, transfers to self only",
           "rules": sol_policy(bot_sol, "solana", C.SOL_POLICY_MINTS, C.SOL_POLICY_MAX_LAMPORTS_PER_IX)}
    return evm, sol
