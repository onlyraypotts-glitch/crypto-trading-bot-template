"""Pre-sign guard for Solana transactions. Decodes EVERY instruction of the exact bytes that would be sent to
CDP and rejects unless all rules hold. Fail-closed: anything unrecognised is rejected.

Rules enforced:
  * single signer = fee payer = bot; tx not already signed
  * every top-level program in SOL_PROGRAM_ALLOWLIST; exactly one Jupiter ExactIn route (route_v2 /
    shared_accounts_route_v2 / route / shared_accounts_route), with platform fee 0, positive-slippage fee 0,
    slippage <= MAX_SLIPPAGE_BPS
  * Jupiter user authority = bot; source & destination token accounts = bot's ATAs for the declared mints
    (derived locally from bot + mint + token program), mints match the intent, in_amount matches intent
  * System: only Transfer from bot to bot's own WSOL ATA (SOL wrap), total <= in_amount, only when selling SOL
  * ATA: only Create/CreateIdempotent with payer = owner = bot and correctly derived address
  * Token/Token-2022: only SyncNative on bot WSOL ATA and CloseAccount(bot ATA -> bot, authority bot).
    Transfer/Approve/SetAuthority/etc are rejected -> no approvals, no token sends.
  * ComputeBudget: only SetComputeUnitLimit/Price/HeapFrame/LoadedAccountsDataSize; priority fee <= cap
  * size: input USD value <= MAX_POSITION_USD; decoded min-out value >= (1 - MAX_LOSS_VS_ORACLE) * input value
"""
import struct
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.signature import Signature
from . import config as C
from .guard_common import GuardError

DISC = {  # Anchor discriminators, verified against on-chain JUP6 IDL (engine/jupiter_v6_idl.json)
    bytes.fromhex("bb64facc31c4af14"): "route_v2",
    bytes.fromhex("d19853937cfed8e9"): "shared_accounts_route_v2",
    bytes.fromhex("e517cb977ae3ad2a"): "route",
    bytes.fromhex("c1209b3341d69c81"): "shared_accounts_route",
}
REJECTED_JUP = {bytes.fromhex(h): n for h, n in [
    ("9d8ab85215f4f324", "exact_out_route_v2"), ("3560e5cad8bbfa18", "shared_accounts_exact_out_route_v2"),
    ("d033ef977b2bed5c", "exact_out_route"), ("b0d169a89a7d453e", "shared_accounts_exact_out_route"),
    ("96564774a75d0e68", "route_with_token_ledger"), ("e6798f50779f6aaa", "shared_accounts_route_with_token_ledger")]}
TOKEN_PROGRAMS = {C.TOKEN_PROGRAM, C.TOKEN_2022_PROGRAM}


def ata(owner, mint, token_program):
    return str(Pubkey.find_program_address(
        [bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(token_program)), bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(C.ATA_PROGRAM))[0])


def _resolve_keys(msg, alts):
    keys = [str(k) for k in msg.account_keys]
    if isinstance(msg, MessageV0):
        w, r = [], []
        for lk in msg.address_table_lookups:
            k = str(lk.account_key)
            if k not in alts:
                raise GuardError(f"address lookup table {k} not resolved (fetch from chain)")
            tbl = alts[k]
            try:
                w += [tbl[i] for i in bytes(lk.writable_indexes)]
                r += [tbl[i] for i in bytes(lk.readonly_indexes)]
            except IndexError:
                raise GuardError(f"ALT {k} index out of range")
        keys += w + r
    return keys


def _parse_jup(name, data):
    """Return dict of args for ExactIn routes. Layout per IDL arg order."""
    d = data[8:]
    if name in ("shared_accounts_route_v2", "route_v2"):
        off = 1 if name.startswith("shared") else 0     # id: u8
        in_amount, quoted_out = struct.unpack_from("<QQ", d, off)
        slippage, plat_fee, pos_slip = struct.unpack_from("<HHH", d, off + 16)
        return dict(in_amount=in_amount, quoted_out=quoted_out, slippage_bps=slippage, platform_fee_bps=plat_fee,
                    positive_slippage_bps=pos_slip)
    # v1: [id u8]? route_plan Vec<RoutePlanStep> first -> amounts are at the END: in_amount u64, quoted_out u64,
    # slippage_bps u16, platform_fee_bps u8  (17+2+1 = 19 bytes tail)
    in_amount, quoted_out, slippage, plat_fee = struct.unpack("<QQHB", d[-19:])
    return dict(in_amount=in_amount, quoted_out=quoted_out, slippage_bps=slippage, platform_fee_bps=plat_fee,
                positive_slippage_bps=0)


def check(raw_tx: bytes, intent: dict, alts: dict, prices: dict, bot: str = None):
    """intent: {input_mint, output_mint, in_amount:int, decimals:{mint:int}}; prices: {mint: usd}
    alts: {alt_address: [addresses]} fetched FROM CHAIN. Returns report dict or raises GuardError."""
    bot = bot or C.BOT_SOL
    if not bot:
        raise GuardError("bot Solana address unknown (run create_accounts.py) - refusing")
    errs = []
    tx = VersionedTransaction.from_bytes(raw_tx)
    msg = tx.message
    if msg.header.num_required_signatures != 1:
        errs.append(f"expected exactly 1 signer, got {msg.header.num_required_signatures}")
    if str(msg.account_keys[0]) != bot:
        errs.append(f"fee payer {msg.account_keys[0]} is not the bot")
    if any(s != Signature.default() for s in tx.signatures):
        errs.append("transaction already carries a signature")
    keys = _resolve_keys(msg, alts)
    in_mint, out_mint, in_amount = intent["input_mint"], intent["output_mint"], int(intent["in_amount"])
    wsol_ata = ata(bot, C.WSOL_MINT, C.TOKEN_PROGRAM)
    bot_atas = {ata(bot, m, tp) for m in (in_mint, out_mint) for tp in TOKEN_PROGRAMS}  # bot-owned by derivation
    jup = []
    wrapped = 0
    cu_limit, cu_price = None, 0
    for n, ix in enumerate(msg.instructions):
        prog = keys[ix.program_id_index]
        acc = [keys[i] for i in bytes(ix.accounts)]
        data = bytes(ix.data)
        tag = f"ix#{n} {prog[:6]}"
        if prog not in C.SOL_PROGRAM_ALLOWLIST:
            errs.append(f"{tag}: program {prog} not allowlisted"); continue
        if prog == C.COMPUTE_BUDGET_PROGRAM:
            if data[:1] == b"\x02" and len(data) == 5:
                cu_limit = struct.unpack("<I", data[1:5])[0]
            elif data[:1] == b"\x03" and len(data) == 9:
                cu_price = struct.unpack("<Q", data[1:9])[0]
            elif data[:1] in (b"\x01", b"\x04"):
                pass
            else:
                errs.append(f"{tag}: unknown ComputeBudget instruction {data[:1].hex()}")
        elif prog == C.SYSTEM_PROGRAM:
            if len(data) == 12 and data[:4] == (2).to_bytes(4, "little") and len(acc) == 2:
                lam = struct.unpack("<Q", data[4:12])[0]
                if acc[0] != bot:
                    errs.append(f"{tag}: SOL transfer source is not the bot")
                if acc[1] != wsol_ata:
                    errs.append(f"{tag}: SOL transfer to FOREIGN address {acc[1]} (only bot WSOL ATA allowed)")
                wrapped += lam
            else:
                errs.append(f"{tag}: System instruction other than Transfer is not allowed")
        elif prog == C.ATA_PROGRAM:
            if data in (b"", b"\x00", b"\x01") and len(acc) >= 6:
                payer, a, owner, mint, _sys, tprog = acc[:6]
                if payer != bot or owner != bot:
                    errs.append(f"{tag}: ATA create for foreign owner/payer ({owner})")
                elif tprog not in TOKEN_PROGRAMS or a != ata(bot, mint, tprog):
                    errs.append(f"{tag}: ATA address does not derive from bot/mint")
                else:
                    bot_atas.add(a)
            else:
                errs.append(f"{tag}: ATA instruction {data[:1].hex()} not allowed")
        elif prog in TOKEN_PROGRAMS:
            if data == b"\x11" and len(acc) == 1:          # SyncNative
                if acc[0] != wsol_ata:
                    errs.append(f"{tag}: SyncNative on non-bot account {acc[0]}")
            elif data == b"\x09" and len(acc) >= 3:        # CloseAccount(account, destination, owner)
                if acc[1] != bot:
                    errs.append(f"{tag}: CloseAccount sends rent/lamports to FOREIGN {acc[1]}")
                if acc[2] != bot:
                    errs.append(f"{tag}: CloseAccount authority is not bot")
                if acc[0] not in bot_atas and acc[0] != wsol_ata:
                    errs.append(f"{tag}: CloseAccount on account not proven bot-owned {acc[0]}")
            else:
                errs.append(f"{tag}: token instruction {data[:1].hex()} not allowed (no transfers/approvals)")
        elif prog == C.JUPITER_V6:
            name = DISC.get(data[:8])
            if not name:
                errs.append(f"{tag}: Jupiter instruction {REJECTED_JUP.get(data[:8], data[:8].hex())} not allowed"); continue
            try:
                a = _parse_jup(name, data)
            except Exception as e:
                errs.append(f"{tag}: cannot decode {name}: {e}"); continue
            a["name"] = name
            if name == "shared_accounts_route_v2":
                auth, src, dst, smint, dmint, stp, dtp = acc[1], acc[2], acc[5], acc[6], acc[7], acc[8], acc[9]
                extra_dst = None
            elif name == "route_v2":
                auth, src, dst, smint, dmint, stp, dtp = acc[0], acc[1], acc[2], acc[3], acc[4], acc[5], acc[6]
                extra_dst = acc[7]
            elif name == "shared_accounts_route":
                auth, src, dst, smint, dmint = acc[2], acc[3], acc[6], acc[7], acc[8]
                stp = dtp = acc[0]
                extra_dst = None
                if acc[9] != C.JUPITER_V6:
                    errs.append(f"{tag}: platform_fee_account set")
            else:  # route
                auth, src, dst, dmint = acc[1], acc[2], acc[3], acc[5]
                smint, stp, dtp = in_mint, acc[0], acc[0]
                extra_dst = acc[4]
                if acc[6] != C.JUPITER_V6:
                    errs.append(f"{tag}: platform_fee_account set")
            if auth != bot:
                errs.append(f"{tag}: swap authority {auth} is not the bot")
            if smint != in_mint or dmint != out_mint:
                errs.append(f"{tag}: mints {smint}->{dmint} differ from intent {in_mint}->{out_mint}")
            if stp not in TOKEN_PROGRAMS or dtp not in TOKEN_PROGRAMS:
                errs.append(f"{tag}: unexpected token program")
            else:
                if src != ata(bot, in_mint, stp):
                    errs.append(f"{tag}: source token account {src} is not bot's ATA")
                if dst != ata(bot, out_mint, dtp):
                    errs.append(f"{tag}: OUTPUT token account {dst} is not bot's ATA (foreign destination)")
                if extra_dst not in (None, C.JUPITER_V6, dst, bot):
                    errs.append(f"{tag}: OUTPUT destination_token_account {extra_dst} is FOREIGN")
            if a["in_amount"] != in_amount:
                errs.append(f"{tag}: in_amount {a['in_amount']} != intent {in_amount}")
            if a["platform_fee_bps"] != 0 or a["positive_slippage_bps"] != 0:
                errs.append(f"{tag}: platform/positive-slippage fee set")
            if a["slippage_bps"] > C.MAX_SLIPPAGE_BPS:
                errs.append(f"{tag}: slippage {a['slippage_bps']} > {C.MAX_SLIPPAGE_BPS}")
            jup.append(a)
    if len(jup) != 1:
        errs.append(f"expected exactly one Jupiter route instruction, found {len(jup)}")
    if in_mint == C.WSOL_MINT:
        if wrapped > in_amount:
            errs.append(f"wraps {wrapped} lamports > in_amount {in_amount}")
    elif wrapped:
        errs.append("SOL transfer present although input is not SOL")
    if cu_limit is None:
        errs.append("no explicit compute unit limit")
    else:
        prio = cu_limit * cu_price // 1_000_000
        if prio > C.MAX_PRIORITY_FEE_LAMPORTS:
            errs.append(f"priority fee {prio} lamports > cap {C.MAX_PRIORITY_FEE_LAMPORTS}")
    report = {"programs": sorted({keys[ix.program_id_index] for ix in msg.instructions}), "wrapped_lamports": wrapped,
              "cu_limit": cu_limit, "cu_price_microlamports": cu_price}
    if jup:
        a = jup[0]
        dec = intent["decimals"]
        try:
            in_usd = a["in_amount"] / 10 ** dec[in_mint] * prices[in_mint]
            min_out = a["quoted_out"] * (10_000 - a["slippage_bps"]) // 10_000
            min_out_usd = min_out / 10 ** dec[out_mint] * prices[out_mint]
        except KeyError as e:
            errs.append(f"no independent price/decimals for {e} - refusing"); in_usd = min_out_usd = None; min_out = None
        if in_usd is not None:
            if in_usd > C.MAX_POSITION_USD:
                errs.append(f"size ${in_usd:.2f} > cap ${C.MAX_POSITION_USD}")
            if min_out_usd < in_usd * (1 - C.MAX_LOSS_VS_ORACLE_BPS / 10_000):
                errs.append(f"min-out ${min_out_usd:.2f} vs input ${in_usd:.2f}: worse than {C.MAX_LOSS_VS_ORACLE_BPS}bps")
        report.update(jupiter=a, in_usd=in_usd, min_out=min_out, min_out_usd=min_out_usd)
    if errs:
        raise GuardError(errs)
    report["verdict"] = "PASS"
    return report
