"""Solana: quote + build a Jupiter swap (Swap API v2 /build, Metis router, raw instructions) into an UNSIGNED v0 tx."""
import base64
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.hash import Hash
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solders.address_lookup_table_account import AddressLookupTableAccount
from . import config as C
from .net import jup_get, sol_rpc

CU_LIMIT_DEFAULT = 600_000


def to_ix(j):
    return Instruction(Pubkey.from_string(j["programId"]),
                       base64.b64decode(j["data"]),
                       [AccountMeta(Pubkey.from_string(a["pubkey"]), a["isSigner"], a["isWritable"]) for a in j["accounts"]])


def cu_limit_ix(units):
    return Instruction(Pubkey.from_string(C.COMPUTE_BUDGET_PROGRAM), bytes([2]) + int(units).to_bytes(4, "little"), [])


def jupiter_build(input_mint, output_mint, amount, taker, slippage_bps=C.DEFAULT_SLIPPAGE_BPS):
    # No tipAmount, no platformFeeBps/feeAccount, no destinationTokenAccount/nativeDestinationAccount (defaults = taker).
    return jup_get("/swap/v2/build", {"inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount)),
                                      "taker": taker, "slippageBps": str(int(slippage_bps))})


def alts_from_build(b):
    return {k: v for k, v in (b.get("addressesByLookupTableAddress") or {}).items()}


def fetch_alts_onchain(keys, network="solana"):
    """Resolve ALT contents from chain (do not trust API-provided contents for the guard)."""
    out = {}
    if not keys:
        return out
    res = sol_rpc("getMultipleAccounts", [list(keys), {"encoding": "base64"}], network)
    for k, acc in zip(keys, res["value"]):
        if acc is None:
            raise ValueError(f"ALT {k} not found on chain")
        raw = base64.b64decode(acc["data"][0])
        # AddressLookupTable layout: 56-byte meta header then 32-byte addresses
        addrs = [str(Pubkey.from_bytes(raw[i:i + 32])) for i in range(56, len(raw), 32)]
        out[k] = addrs
    return out


def assemble(b, payer, blockhash=None, cu_limit=CU_LIMIT_DEFAULT, extra_ixs=(), alts=None):
    """Assemble unsigned v0 tx bytes from a /build response. extra_ixs only used by tests (tampering)."""
    ixs = [cu_limit_ix(cu_limit)] + [to_ix(i) for i in b.get("computeBudgetInstructions") or []]
    ixs += [to_ix(i) for i in b.get("setupInstructions") or []]
    ixs.append(to_ix(b["swapInstruction"]))
    if b.get("cleanupInstruction"):
        ixs.append(to_ix(b["cleanupInstruction"]))
    ixs += [to_ix(i) for i in b.get("otherInstructions") or []]
    if b.get("tipInstruction"):
        ixs.append(to_ix(b["tipInstruction"]))   # guard will reject (foreign destination) - we never request tips
    ixs += list(extra_ixs)
    alts = alts if alts is not None else alts_from_build(b)
    alt_accts = [AddressLookupTableAccount(Pubkey.from_string(k), [Pubkey.from_string(a) for a in v]) for k, v in alts.items()]
    bh = Hash.from_string(blockhash) if blockhash else Hash(bytes(b["blockhashWithMetadata"]["blockhash"]))
    msg = MessageV0.try_compile(Pubkey.from_string(payer), ixs, alt_accts, bh)
    tx = VersionedTransaction.populate(msg, [Signature.default()] * msg.header.num_required_signatures)
    return bytes(tx)


def tx_b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()
