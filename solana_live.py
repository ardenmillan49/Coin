"""Local builders for SOL Pump curve v3, canonical PumpSwap v2 and SPL transfers."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import struct
import tempfile

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
import bot

pk = Pubkey.from_string
PUMP = pk(bot.PROGRAM)
AMM = pk("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
WSOL = pk("So11111111111111111111111111111111111111112")
SPL = pk("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN22 = pk("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ATA = pk("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYS = Pubkey.default()
BUYBACK = pk("5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD")
FINAL = {"commitment": "finalized"}
COUNT = 50


def verify_mainnet():
    if bot.rpc("getGenesisHash", []) != "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d":
        raise ValueError("مزود RPC لا يشير إلى Solana mainnet")


def keys(create=False):
    folder = Path(os.getenv("COIN_CONFIG_DIR", "~/.config/coin")).expanduser()
    path = folder / "live-keypair.json"
    if create and not path.exists():
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder, mode="w", delete=False) as f:
            json.dump(list(bytes(Keypair())), f)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(f.name, path)
        except FileExistsError:
            pass
        finally:
            os.unlink(f.name)
    if not path.exists():
        raise ValueError("افتح محفظة البوت أول مرة.")
    if path.stat().st_mode & 0o077:
        raise ValueError("اضبط صلاحية ملف مفتاح المحفظة إلى 600.")
    master = Keypair.from_bytes(bytes(json.loads(path.read_text())))
    children = [Keypair.from_seed(hmac.digest(bytes(master)[:32], f"Coin/child/{i}".encode(), "sha256"))
                for i in range(COUNT)]
    return master, children


def pda(program, *seeds):
    return Pubkey.find_program_address([bytes(seed) for seed in seeds], program)[0]


def ata(owner, mint, program):
    return pda(ATA, owner, program, mint)


def instruction(program, data, addresses, writable, signers=()):
    return Instruction(program, data, [AccountMeta(a, i in signers, i in writable) for i, a in enumerate(addresses)])


def create_ata(payer, owner, mint, program):
    return instruction(ATA, b"\x01", [payer, ata(owner, mint, program), owner, mint, SYS, program], {0, 1}, {0})


def transfer(owner, dest, mint, program, units, decimals):
    return instruction(program, b"\x0c" + struct.pack("<QB", units, decimals),
                       [ata(owner, mint, program), mint, ata(dest, mint, program), owner], {0, 2}, {3})


def close_ata(owner, receiver, mint, program):
    return instruction(program, b"\x09", [ata(owner, mint, program), receiver, owner], {0, 1}, {2})


def data_account(address, program):
    result = bot.rpc("getAccountInfo", [str(address), dict(FINAL, encoding="base64")])["value"]
    if result is None:
        return None
    if result["owner"] != str(program):
        raise ValueError("Unexpected program owner")
    return base64.b64decode(result["data"][0], validate=True)


def mint_info(mint):
    acct = bot.rpc("getAccountInfo", [str(mint), dict(FINAL, encoding="jsonParsed")])["value"]
    if acct is None:
        return None
    program = pk(acct["owner"])
    if program not in (SPL, TOKEN22) or acct["data"]["parsed"]["type"] != "mint":
        raise ValueError("Unsupported mint")
    info = acct["data"]["parsed"]["info"]
    allowed = {"metadataPointer", "tokenMetadata"}
    if info.get("freezeAuthority") or any(x["extension"] not in allowed for x in info.get("extensions", [])):
        raise ValueError("Unsupported mint authority or token extension")
    return program, info["decimals"]


def amount(address):
    return int(bot.rpc("getTokenAccountBalance", [str(address), FINAL])["value"]["amount"])


def market(mint):
    curve = pda(PUMP, b"bonding-curve", mint)
    data = data_account(curve, PUMP)
    if data is None:
        return None
    if data[:8] != bot.TAG or len(data) < 49:
        raise ValueError("Unknown Pump curve")
    if any(data[83:115]) or data[82:83] == b"\x01":
        raise ValueError("Unsupported quote asset or cashback curve")
    if not data[48]:
        t, s, available, _ = bot.decode(data)
        return ("curve", curve, t, s, available)
    authority = pda(PUMP, b"pool-authority", mint)
    pool = pda(AMM, b"pool", b"\x00\x00", authority, mint, WSOL)
    data = data_account(pool, AMM)
    if data is None:
        return None
    if (len(data) < 211 or data[:8] != hashlib.sha256(b"account:Pool").digest()[:8]
            or data[43:75] != bytes(mint) or data[75:107] != bytes(WSOL)):
        raise ValueError("Unknown canonical pool")
    base = Pubkey.from_bytes(data[139:171])
    quote = Pubkey.from_bytes(data[171:203])
    virtual = int.from_bytes(data[245:261], "little", signed=True) if len(data) >= 261 else 0
    return ("pool", pool, amount(base), amount(quote) + virtual, 0, base, quote)


def trade(user, mint, program, venue, buy, units, limit):
    route, address = venue[:2]
    if route == "curve":
        name = "buy_exact_quote_in_v3" if buy else "sell_v3"
        addresses = [pda(PUMP, b"global"), mint, WSOL, program, SPL, address, ata(address, mint, program),
                     ata(address, WSOL, SPL), user, ata(user, mint, program), ata(user, WSOL, SPL),
                     pda(PUMP, b"user_volume_accumulator", user), pk("8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt"),
                     BUYBACK, SYS, pda(PUMP, b"__event_authority"), PUMP]
        target, writable, signers = PUMP, {5, 6, 7, 8, 9, 10, 11, 13}, {8}
    else:
        if buy:
            raise ValueError("Live entry requires an active Pump curve")
        name = "sell_v2"
        addresses = [address, user, pda(AMM, b"global_config"), mint, WSOL, ata(user, mint, program),
                     ata(user, WSOL, SPL), venue[5], venue[6], program, SPL, SYS,
                     pda(AMM, b"user_volume_accumulator", user), pk("5PHirr8joyTMp9JMm6nW7hNDVyEYdkzDqazxPD7RaTjx"),
                     ata(BUYBACK, WSOL, SPL), pda(AMM, b"__event_authority"), AMM]
        target, writable, signers = AMM, {0, 1, 5, 6, 7, 8, 12, 14}, {1}
    tag = hashlib.sha256(("global:" + name).encode()).digest()[:8]
    return instruction(target, tag + struct.pack("<QQ", units, limit), addresses, writable, signers)


def prepare(instructions, signers):
    block = bot.rpc("getLatestBlockhash", [FINAL])["value"]
    tx = Transaction.new_signed_with_payer(instructions, signers[0].pubkey(), signers, Hash.from_string(block["blockhash"]))
    raw = bytes(tx)
    if len(raw) > 1232:
        raise ValueError("Transaction exceeds Solana packet size")
    return str(tx.signatures[0]), base64.b64encode(raw).decode(), block["lastValidBlockHeight"]


def message_fee(raw):
    message = Transaction.from_bytes(base64.b64decode(raw)).message_data()
    result = bot.rpc("getFeeForMessage", [base64.b64encode(message).decode(), FINAL])["value"]
    if result is None:
        raise OSError("Blockhash expired while calculating fee")
    return result
