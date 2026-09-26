"""CDP client factories. The trading bot uses the normal key (should NOT have policies#manage scope).
Policy scripts use CDP_ADMIN_API_KEY_ID/SECRET (a separate key WITH policies#manage) when present."""
import os
from secrets_loader import load
load()


def bot_client():
    from cdp import CdpClient
    return CdpClient()


def admin_client():
    from cdp import CdpClient
    kid, sec = os.environ.get("CDP_ADMIN_API_KEY_ID"), os.environ.get("CDP_ADMIN_API_KEY_SECRET")
    if kid and sec:
        return CdpClient(api_key_id=kid, api_key_secret=sec, wallet_secret=os.environ.get("CDP_WALLET_SECRET"))
    return CdpClient()
