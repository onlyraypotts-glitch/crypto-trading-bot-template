"""Static configuration. Protocol addresses below are public mainnet contracts/programs (verify before going live);
bot account addresses are loaded at runtime (see "bot accounts")."""
import json, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
KILL_SWITCH_FILE = ROOT / "KILL_SWITCH"          # if this file exists, NOTHING is signed/sent and no trade cycle starts
TRADE_LOG = STATE_DIR / "trades.jsonl"
ALLOWANCE_STATE = STATE_DIR / "evm_allowances.json"

# ---------------------------------------------------------------- DRY RUN
# Live mode requires BOTH env vars exactly as below. Anything else => dry run.
def dry_run() -> bool:
    return not (os.environ.get("BOT_DRY_RUN") == "false" and os.environ.get("BOT_LIVE_ACK") == "I-ACCEPT-LIVE-TRADING")

# ---------------------------------------------------------------- bot accounts
# Nothing owner-specific is hard-coded. Addresses come from (in order):
#   1. env BOT_EVM_ADDRESS / BOT_SOL_ADDRESS
#   2. accounts.json (path overridable with BOT_ACCOUNTS_FILE), written by create_accounts.py, gitignored.
# If neither exists the addresses are None and every guard / trade fails closed.
ACCOUNTS_FILE = Path(os.environ.get("BOT_ACCOUNTS_FILE") or (ROOT / "accounts.json"))


def account_names():
    prefix = os.environ.get("BOT_ACCOUNT_PREFIX", "trading-bot")
    return f"{prefix}-evm", f"{prefix}-sol"


def _load_accounts():
    evm, sol = os.environ.get("BOT_EVM_ADDRESS"), os.environ.get("BOT_SOL_ADDRESS")
    if (not evm or not sol) and ACCOUNTS_FILE.exists():
        d = json.loads(ACCOUNTS_FILE.read_text())
        evm = evm or (d.get("evm") or {}).get("address")
        sol = sol or (d.get("sol") or {}).get("address")
    return evm or None, sol or None


def save_accounts(evm_name, evm_addr, sol_name, sol_addr):
    ACCOUNTS_FILE.write_text(json.dumps({"evm": {"name": evm_name, "address": evm_addr},
                                         "sol": {"name": sol_name, "address": sol_addr}}, indent=2) + "\n")


def require_accounts():
    if not (BOT_EVM and BOT_SOL):
        raise RuntimeError("bot accounts unknown: run create_accounts.py (or set BOT_EVM_ADDRESS / BOT_SOL_ADDRESS)")
    return BOT_EVM, BOT_SOL


BOT_EVM, BOT_SOL = _load_accounts()

# ---------------------------------------------------------------- risk caps
MAX_POSITION_USD = 100.0            # per position, until TRADES_BEFORE_CAP_REVIEW logged live trades (owner raises manually)
TRADES_BEFORE_CAP_REVIEW = 20
POLICY_MAX_TX_USD = 150             # CDP policy backstop (EVM netUSDChange; Solana via lamport cap)
MAX_SLIPPAGE_BPS = 150
DEFAULT_SLIPPAGE_BPS = 100
MAX_LOSS_VS_ORACLE_BPS = 300        # min-out value must be >= 97% of input value (independent price)
MAX_PRIORITY_FEE_LAMPORTS = 2_000_000   # 0.002 SOL max priority fee per Solana tx
MAX_EVM_GAS_LIMIT = 1_500_000

# ---------------------------------------------------------------- Solana
SOL_NETWORK = "solana"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
# mints whose bot-owned ATAs go into the CDP policy splAddress allowlist (guard derives ATAs for ANY mint)
SOL_POLICY_MINTS = {WSOL_MINT: "token", USDC_MINT: "token", BONK_MINT: "token"}

JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
JUPITER_EVENT_AUTHORITY = "D8cy77BBepLMngZx6ZukaTff5hCt1HrWyKk3Hnd9oitf"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
# Verified from a real Jupiter /swap/v2/build SOL->BONK tx: top-level programs are exactly
# ComputeBudget, ATA, System, Token, JUP6 (Token-2022 appears for Token-2022 mints).
SOL_PROGRAM_ALLOWLIST = [JUPITER_V6, SYSTEM_PROGRAM, TOKEN_PROGRAM, TOKEN_2022_PROGRAM, ATA_PROGRAM, COMPUTE_BUDGET_PROGRAM]
SOL_POLICY_MAX_LAMPORTS_PER_IX = 1_200_000_000   # ~$145 @ $121/SOL; update if SOL price moves a lot
JUPITER_BASE = "https://api.jup.ag"

# ---------------------------------------------------------------- EVM
NATIVE_ETH = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
EVM_CHAINS = {
    "base": {
        "chain_id": 8453,
        "rpc": "https://base-rpc.publicnode.com",
        "weth": "0x4200000000000000000000000000000000000006",
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        # 0x Settler (taker-submitted, feature 2) = the router CDP's swap API returns. Rotates on redeploy;
        # guard ALSO checks live against the registry. current + announced next, from registry on 2026-09-25.
        "settlers": ["0x4f6f91599858bf0d19fabcf2c5d591fe13f7c059", "0x7d6839415693801e94cdc4a7d732c9a3fdbbd1d7"],
        "uni_swap_router02": "0x2626664c2603336E57B271c5C0b26F421741e481",
        "uni_quoter_v2": "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
    },
    "ethereum": {
        "chain_id": 1,
        "rpc": "https://ethereum-rpc.publicnode.com",
        "weth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "usdc": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "settlers": ["0x666fedd4cdd4e890a5ad20e7b60975409435a64a", "0x0cef320d0c5793ade37964e2661a033771e53cd5"],
        "uni_swap_router02": "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "uni_quoter_v2": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
    },
}
SETTLER_REGISTRY = "0x00000000000004533Fe15556B1E086BB1A72cEae"   # 0x Deployer/registry; ownerOf(2) = current taker-submitted Settler
SETTLER_FEATURE_TAKER_SUBMITTED = 2
# ERC-20s the bot may approve (exact amount, to SwapRouter02 only) i.e. may SELL on EVM. Add tokens here and re-run
# scripts/apply_policy.py so the CDP policy matches.
EVM_SELLABLE_TOKENS = {
    "base": ["0x4200000000000000000000000000000000000006", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"],
    "ethereum": ["0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"],
}
# Sell outputs (SwapRouter02 exactInputSingle only outputs ERC-20s; WETH can be unwrapped to self via withdraw()).
EVM_SELL_OUTPUTS = {c: [v["weth"], v["usdc"]] for c, v in EVM_CHAINS.items()}
POLICY_MAX_ETH_VALUE_WEI = 60_000_000_000_000_000        # 0.06 ETH (~$160 @ $2.7k) native value backstop
POLICY_MAX_APPROVE_VALUE = 10**33                         # anti-"unlimited approval" backstop in policy; guard requires exact
UINT256_MAX = 2**256 - 1

SUPPORTED_CHAINS = {"solana", "base", "ethereum"}         # Sui: research only, no code path
