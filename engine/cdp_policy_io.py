"""Create/update/attach/read policies. The SDK 1.48.1 generated models cannot serialize custom-ABI evmData
criteria (AbiInner oneOf serializes to None), so policy bodies are sent as plain wire-format JSON through the
SDK's own authenticated ApiClient (same endpoint/auth as cdp.policies.*)."""
import json
from cdp.openapi_client.models.update_evm_account_request import UpdateEvmAccountRequest
from cdp.openapi_client.models.update_solana_account_request import UpdateSolanaAccountRequest


class PolicyObj(dict):
    @property
    def id(self):
        return self["id"]


async def _raw(cdp, method, path, body=None):
    api = cdp.api_clients.policies.api_client
    hdr = {"Accept": "application/json"}
    if body is not None:
        hdr["Content-Type"] = "application/json"
    params = api.param_serialize(method=method, resource_path=path, header_params=hdr, body=body,
                                 auth_settings=["apiKeyAuth"])
    resp = await api.call_api(*params)
    raw = await resp.read()
    if resp.status >= 300:
        raise RuntimeError(f"policy API {method} {path} -> {resp.status}: {raw[:800]!r}")
    return PolicyObj(json.loads(raw)) if raw else PolicyObj()


async def create_policy(cdp, doc):
    return await _raw(cdp, "POST", "/v2/policy-engine/policies", doc)


async def update_policy(cdp, pid, doc):
    return await _raw(cdp, "PUT", f"/v2/policy-engine/policies/{pid}", {"description": doc.get("description"), "rules": doc["rules"]})


async def get_policy(cdp, pid):
    return await _raw(cdp, "GET", f"/v2/policy-engine/policies/{pid}")


async def attach_evm(cdp, address, pid):
    return await cdp.api_clients.evm_accounts.update_evm_account(
        address=address, update_evm_account_request=UpdateEvmAccountRequest(account_policy=pid))


async def attach_sol(cdp, address, pid):
    return await cdp.api_clients.solana_accounts.update_solana_account(
        address=address, update_solana_account_request=UpdateSolanaAccountRequest(account_policy=pid))
