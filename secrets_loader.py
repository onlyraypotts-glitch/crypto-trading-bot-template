"""Load the bot's secrets into os.environ for this process only. NEVER prints values.

Resolution order for each name:
  1. an environment variable that is already set (recommended: inject via your host's secret manager)
  2. an optional JSON file whose path is given in CDP_SECRETS_FILE. The file may be flat
     {"CDP_API_KEY_ID": "...", ...} or nested under a "card" key {"card": {...}}. Keep it chmod 600
     and OUT of version control.
"""
import json, os

NAMES = ["CDP_API_KEY_ID", "CDP_API_KEY_SECRET", "CDP_WALLET_SECRET", "JUPITER_API_KEY",
         "HELIUS_API_KEY",                                    # optional: faster Solana RPC
         "CDP_ADMIN_API_KEY_ID", "CDP_ADMIN_API_KEY_SECRET"]  # optional: policy management only
REQUIRED_FOR_TRADING = ["CDP_API_KEY_ID", "CDP_API_KEY_SECRET", "CDP_WALLET_SECRET", "JUPITER_API_KEY"]


def load():
    """Fill missing names from CDP_SECRETS_FILE (if set). Returns {name: length} - never the values."""
    missing = [n for n in NAMES if not os.environ.get(n)]
    path = os.environ.get("CDP_SECRETS_FILE")
    if missing and path and os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        if isinstance(data.get("card"), dict):
            data = data["card"]
        for n in missing:
            if data.get(n):
                os.environ[n] = str(data[n])
    return {n: len(os.environ.get(n, "")) for n in NAMES}


def missing_required(names=REQUIRED_FOR_TRADING):
    return [n for n in names if not os.environ.get(n)]


def redact(msg, limit=1500):
    """Replace any loaded secret value that appears in msg."""
    m = str(msg)
    for n in NAMES:
        v = os.environ.get(n)
        if v and len(v) > 6:
            m = m.replace(v, "<" + n + ">")
    return m[:limit]
