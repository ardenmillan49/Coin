"""Single-position paper trader for SOL-paired Pump bonding curves."""
import argparse
import base64
import hashlib
import json
import os
import sqlite3
import struct
import sys
import time
from dataclasses import dataclass
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
LAMPORTS = 10**9
TAG = hashlib.sha256(b"account:BondingCurve").digest()[:8]


def sol_amount(value):
    amount = Decimal(value) * LAMPORTS
    if not amount.is_finite() or amount != amount.to_integral_value() or not 0 < amount < 2**63:
        raise ValueError("Enter a positive SOL amount with at most nine decimals")
    return int(amount)


def decode(data):
    if len(data) < 49 or data[:8] != TAG:
        raise ValueError("Unrecognized bonding curve")
    if data[48]:
        raise ValueError("Completed curve: migrated pools are outside this version")
    if any(data[83:115]):
        raise ValueError("Only SOL-quoted curves are supported")
    token, sol, available, cash, supply = struct.unpack_from("<5Q", data, 8)
    if not token or not sol or not supply:
        raise ValueError("Invalid reserves")
    return token, sol, available, cash


@dataclass(frozen=True)
class Curve:
    reserves: tuple
    address: str
    creator: str | None
    slot: int


def rpc(method, params, *, endpoint=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(endpoint or os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        result = json.load(response)
    if "error" in result:
        raise OSError(f"{method}: {result['error'].get('message', 'RPC error')}")
    return result["result"]


def snapshot(mint):
    from solders.pubkey import Pubkey
    program = Pubkey.from_string(PROGRAM)
    address, _ = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))], program)
    result = rpc("getAccountInfo", [str(address), {"encoding": "base64", "commitment": "confirmed"}])
    account = result["value"]
    if account is None:
        return None
    if account["owner"] != PROGRAM:
        raise ValueError("Unexpected account owner")
    data = base64.b64decode(account["data"][0], validate=True)
    reserves = decode(data)
    creator = str(Pubkey.from_bytes(data[49:81])) if len(data) >= 81 and any(data[49:81]) else None
    return Curve(reserves, str(address), creator, result["context"]["slot"])


def largest_accounts(mint, config):
    """Look up holder accounts without routing any transaction through a fallback."""
    params = [mint, config]
    try:
        return rpc("getTokenLargestAccounts", params)
    except OSError:
        pass
    backup = os.getenv("SOLANA_HOLDERS_RPC_URL", "https://solana-rpc.publicnode.com")
    try:
        return rpc("getTokenLargestAccounts", params, endpoint=backup)
    except OSError:
        pass

    # A mint filter keeps this scan scoped to the launch being inspected.
    mint_account = rpc("getAccountInfo", [mint, dict(config, encoding="base64", dataSlice={"offset":0,"length":0})])["value"]
    if not mint_account or mint_account["owner"] not in (
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
        raise OSError("Mint owner unavailable for holder lookup")
    opts = dict(config, encoding="jsonParsed", withContext=True,
                filters=[{"memcmp":{"offset":0,"bytes":mint}}])
    try:
        result = rpc("getProgramAccounts", [mint_account["owner"], opts])
    except OSError:
        result = rpc("getProgramAccounts", [mint_account["owner"], opts], endpoint=backup)
    amounts = []
    for item in result["value"]:
        data = item["account"]["data"]
        if not isinstance(data, dict) or "parsed" not in data:
            raise OSError("Incomplete parsed token account")
        parsed = data["parsed"]
        if parsed["type"] == "account" and parsed["info"]["mint"] == mint:
            amounts.append((int(parsed["info"]["tokenAmount"]["amount"]), item["pubkey"]))
    if not amounts:
        raise OSError("No token accounts in holder scan")
    return {"context":result["context"],
            "value":[{"address":addr} for _, addr in sorted(amounts, reverse=True)[:20]]}


def holder_report(mint, curve):
    config = {"commitment": "confirmed"}
    largest = largest_accounts(mint, config)
    slot = max(curve.slot, largest["context"]["slot"])
    parsed = dict(config, encoding="jsonParsed", minContextSlot=slot)
    addresses = [row["address"] for row in largest["value"]]
    accounts = rpc("getMultipleAccounts", [addresses, parsed])["value"] if addresses else []
    supply_data = rpc("getTokenSupply", [mint, config])
    supply = int(supply_data["value"]["amount"])
    decimals = supply_data["value"]["decimals"]
    if supply <= 0 or supply_data["context"]["slot"] < curve.slot:
        raise OSError("Supply snapshot unavailable")
    external = []
    for account in accounts:
        if account is None:
            raise OSError("Holder accounts changed while inspecting")
        info = account["data"]["parsed"]["info"]
        if info["mint"] != mint:
            raise OSError("Holder mint changed while inspecting")
        if info["owner"] != curve.address:
            external.append(int(info["tokenAmount"]["amount"]))
    creator_units = None
    if curve.creator:
        rows = rpc("getTokenAccountsByOwner", [curve.creator, {"mint": mint}, parsed])["value"]
        creator_units = sum(int(row["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for row in rows)
    token, sol, available, cash = curve.reserves
    return {"curve_slot": curve.slot, "creator": curve.creator,
            "creator_pct": 100 * creator_units / supply if creator_units is not None else None,
            "largest_external_account_pct": 100 * max(external, default=0) / supply,
            "spot_sol_per_token": sol * 10**decimals / (LAMPORTS * token),
            "sellable_token_inventory": available / 10**decimals,
            "real_sol_reserve": cash / LAMPORTS}


def check_limits(report, creator_limit, account_limit):
    for value, limit, label in [(report["creator_pct"], creator_limit, "creator"),
                                (report["largest_external_account_pct"], account_limit, "external account")]:
        if limit is not None and (value is None or value > limit):
            raise ValueError(f"Paper entry skipped: {label} concentration outside configured limit")



def fee(amount, bps):
    return (amount * bps + 9999) // 10000


def entry(reserves, budget, tx_fee, bps):
    token, sol, available, _ = reserves
    net = (budget - 2 * tx_fee) * 10000 // (10000 + bps)
    if net <= 0:
        raise ValueError("Budget does not cover estimated fees")
    units = token * net // (sol + net)
    if not 0 < units < available:
        raise ValueError("Order too small or exceeds remaining curve inventory")
    return units, net, net + fee(net, bps) + tx_fee


def exit_quote(reserves, units, net, tx_fee, bps):
    token, sol, _, cash = reserves
    # Overlay the paper buy on the observed curve to model its own impact.
    if token <= units:
        raise ValueError("Paper overlay no longer valid")
    gross = (sol + net) * units // token
    if gross > cash + net:
        raise ValueError("Insufficient curve liquidity")
    return gross - fee(gross, bps) - tx_fee


def open_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY, mint TEXT, units INTEGER, net INTEGER,
        cost INTEGER, tx_fee INTEGER, bps INTEGER, proceeds INTEGER)""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_open ON positions((1)) WHERE proceeds IS NULL")
    return db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="paper.sqlite")
    sub = parser.add_subparsers(dest="command", required=True)
    watch = sub.add_parser("watch")
    watch.add_argument("mint")
    watch.add_argument("--budget-sol", required=True)
    watch.add_argument("--tx-fee-sol", default="0.00001")
    watch.add_argument("--fee-bps", type=int, default=125)
    watch.add_argument("--max-creator-pct", type=float)
    watch.add_argument("--max-account-pct", type=float)
    sub.add_parser("inspect").add_argument("mint")
    sub.add_parser("status")
    sub.add_parser("sell")
    args = parser.parse_args()
    if args.command == "inspect":
        curve = snapshot(args.mint)
        if curve is None:
            raise ValueError("Curve unavailable")
        print(json.dumps(holder_report(args.mint, curve), indent=2))
        return
    db = open_db(args.db)
    pos = db.execute("SELECT * FROM positions WHERE proceeds IS NULL").fetchone()
    if args.command == "status":
        for row in db.execute("SELECT *, proceeds-cost AS profit_lamports FROM positions ORDER BY id"):
            print(json.dumps(dict(row)))
        if not pos:
            print("No open paper position")
        return
    if args.command == "watch":
        if pos:
            raise ValueError("A paper position is already open")
        budget, tx = sol_amount(args.budget_sol), sol_amount(args.tx_fee_sol)
        if budget <= 2 * tx or not 0 <= args.fee_bps <= 10000:
            raise ValueError("Invalid budget or fee assumption")
        if any(v is not None and not 0 <= v <= 100 for v in (args.max_creator_pct, args.max_account_pct)):
            raise ValueError("Concentration limits must be percentages between 0 and 100")
        print("Watching for confirmed curve. Ctrl+C cancels.", flush=True)
        while True:
            try:
                curve = snapshot(args.mint)
                if curve is not None:
                    report = holder_report(args.mint, curve)
                    print(json.dumps(report), flush=True)
                    check_limits(report, args.max_creator_pct, args.max_account_pct)
                    break
            except OSError as exc:
                print(f"RPC unavailable ({type(exc).__name__}), retrying", flush=True)
                time.sleep(5)
                continue
            time.sleep(2)
        units, net, cost = entry(curve.reserves, budget, tx, args.fee_bps)
        with db:
            db.execute("INSERT INTO positions(mint,units,net,cost,tx_fee,bps) VALUES(?,?,?,?,?,?)",
                       (args.mint, units, net, cost, tx, args.fee_bps))
        print(f"Paper buy: {units} raw units, total cost {cost} lamports")
    else:
        if not pos:
            raise ValueError("No open paper position")
        curve = snapshot(pos["mint"])
        if curve is None:
            raise ValueError("Curve unavailable")
        proceeds = exit_quote(curve.reserves, pos["units"], pos["net"], pos["tx_fee"], pos["bps"])
        with db:
            updated = db.execute("UPDATE positions SET proceeds=? WHERE id=? AND proceeds IS NULL",
                                 (proceeds, pos["id"]))
            if not updated.rowcount:
                raise ValueError("Position already closed")
        print(f"Paper sell: {proceeds} lamports net, P/L {proceeds - pos['cost']} lamports")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 1:
            import telegram_bot
            telegram_bot.run()
        else:
            main()
    except (ValueError, InvalidOperation, OSError, sqlite3.Error) as error:
        raise SystemExit(str(error))
