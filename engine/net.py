"""Tiny HTTP / JSON-RPC helpers. Secrets only ever appear in headers/URLs, never in logs (see secrets_loader.redact)."""
import json, os, time, urllib.request, urllib.error
from secrets_loader import load, redact

load()


class NetError(Exception):
    pass


def http_json(url, method="GET", body=None, headers=None, timeout=30, retries=3):
    h = {"User-Agent": "cdp-trading-bot/0.1", "Accept": "application/json"}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            txt = e.read()[:600]
            last = NetError(redact(f"HTTP {e.code} {url.split('?')[0]}: {txt!r}"))
            if e.code not in (429, 500, 502, 503, 504):
                raise last
        except Exception as e:  # noqa
            last = NetError(redact(f"{type(e).__name__}: {e}"))
        time.sleep(1.0 + i * 1.5)
    raise last


def jup_get(path, params=None):
    import urllib.parse
    from . import config as C
    url = C.JUPITER_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    return http_json(url, headers={"x-api-key": os.environ["JUPITER_API_KEY"]})


def sol_rpc_url(network="solana"):
    if network == "solana-devnet":
        return "https://api.devnet.solana.com"
    k = os.environ.get("HELIUS_API_KEY")
    return f"https://mainnet.helius-rpc.com/?api-key={k}" if k else "https://api.mainnet-beta.solana.com"


def sol_rpc(method, params, network="solana"):
    r = http_json(sol_rpc_url(network), "POST", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in r:
        raise NetError(redact(f"sol rpc {method}: {r['error']}"))
    return r["result"]


def evm_rpc(chain, method, params, rpc_url=None):
    from . import config as C
    url = rpc_url or C.EVM_CHAINS[chain]["rpc"]
    r = http_json(url, "POST", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in r:
        raise NetError(f"evm rpc {method}: {r['error']}")
    return r["result"]


def eth_call(chain, to, data, rpc_url=None):
    return evm_rpc(chain, "eth_call", [{"to": to, "data": data}, "latest"], rpc_url)
