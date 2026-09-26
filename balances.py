"""Read mainnet native balances for the bot accounts via public RPCs. Read-only."""
import json, urllib.request
from engine import config as C

evm, sol = C.require_accounts()


def rpc(url, method, params):
    req = urllib.request.Request(url, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                                 {"Content-Type": "application/json", "User-Agent": "cdp-trading-bot"})
    return json.load(urllib.request.urlopen(req, timeout=20))["result"]


for name, url in [("Ethereum", "https://ethereum-rpc.publicnode.com"), ("Base", "https://mainnet.base.org")]:
    wei = int(rpc(url, "eth_getBalance", [evm, "latest"]), 16)
    nonce = int(rpc(url, "eth_getTransactionCount", [evm, "latest"]), 16)
    print(f"{name}: {evm} balance={wei/1e18} ETH nonce={nonce}")
lam = rpc("https://api.mainnet-beta.solana.com", "getBalance", [sol])["value"]
print(f"Solana: {sol} balance={lam/1e9} SOL")
