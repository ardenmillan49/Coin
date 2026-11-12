"""Read-only discovery of recent Pump launches using the configured Solana RPC."""
import json
import os
import time
import urllib.request

import bot
import solana_live as chain


def initialized_mints(tx, authority):
    meta = tx.get("meta") or {}
    if meta.get("err") or not meta:
        return
    instructions = list(tx["transaction"]["message"]["instructions"])
    for group in meta.get("innerInstructions") or []:
        instructions.extend(group["instructions"])
    for ix in instructions:
        parsed = ix.get("parsed") or {}
        if not isinstance(parsed, dict) or ix.get("programId") not in (str(chain.SPL), str(chain.TOKEN22)):
            continue
        if parsed.get("type") in ("initializeMint", "initializeMint2"):
            info = parsed["info"]
            if info.get("mintAuthority") == authority:
                yield info["mint"]


def from_history(read):
    authority = str(chain.pda(chain.PUMP, b"mint-authority"))
    signatures = read("getSignaturesForAddress", [authority, dict(chain.FINAL, limit=16)])
    cutoff = time.time() - 1800
    for item in signatures:
        if item["err"] or (item.get("blockTime") or 0) < cutoff:
            continue
        tx = read("getTransaction", [item["signature"], dict(chain.FINAL, encoding="jsonParsed",
                                                                 maxSupportedTransactionVersion=0)])
        if not tx:
            continue
        yield from initialized_mints(tx, authority)


def from_listing():
    url = ("https://frontend-api-v3.pump.fun/coins?offset=0&limit=30&sort=created_timestamp"
           "&order=DESC&includeNsfw=false")
    with urllib.request.urlopen(url, timeout=5) as response:
        records = json.load(response)
    if not isinstance(records, list):
        raise ValueError("Unexpected listing")
    now = time.time()
    for coin in records:
        if not isinstance(coin, dict) or coin.get("complete") or not isinstance(coin.get("mint"), str):
            continue
        created = coin.get("created_timestamp")
        if isinstance(created, (int,float)) and now - 1800 <= created/1000 <= now + 60:
            yield coin["mint"]


class ReadFailure(Exception):
    pass


def read_error(exc):
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return f"HTTP {code}"
    return "تعذّر الاتصال" if isinstance(exc, OSError) else "ردّ غير صالح"


def find():
    primary = os.getenv("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
    backup = os.getenv("SOLANA_HOLDERS_RPC_URL") or "https://solana-rpc.publicnode.com"
    endpoints = list(dict.fromkeys([primary, backup]))
    failed = {}
    def reader(endpoint):
        def read(method, params):
            key = (endpoint, method)
            if key not in failed:
                try:
                    return bot.rpc(method, params, endpoint=endpoint)
                except (OSError, ValueError) as exc:
                    failed[key] = f"{method} ({read_error(exc)})"
            raise ReadFailure(failed[key])
        return read

    readers = [reader(endpoint) for endpoint in endpoints]
    sources = [lambda: from_history(readers[0]), from_listing]
    if len(readers) > 1:
        sources.append(lambda: from_history(readers[1]))
    errors = []
    seen = set()
    for source in sources:
        try:
            for address in source():
                if address in seen:
                    continue
                seen.add(address)
                try:
                    mint = chain.pk(address)
                except ValueError:
                    continue
                for read in readers:
                    try:
                        market = chain.market(mint, entry_only=True, reader=read)
                        if market and market[0] == "curve" and market[4] > 0 and chain.mint_info(mint, reader=read):
                            return ("عقد إطلاق حديث على Solana:\n" + address +
                                    "\nمنحنى SOL مفتوح وقت الفحص. فيك تنسخ العنوان وتفحصه أو تستخدمه بطلب شراء منفصل.")
                        break
                    except ReadFailure as exc:
                        errors.append(str(exc))
                    except ValueError:
                        break
        except ReadFailure as exc:
            errors.append(str(exc))
        except (OSError, ValueError) as exc:
            errors.append("قائمة Pump.fun (" + read_error(exc) + ")")
    if errors:
        return "ما قدرت أثبت وجود عقد مناسب بهالفحص. الطلبات المتعذّرة: " + "، ".join(dict.fromkeys(errors))
    return "ما لقيت منحنى SOL مفتوح ومدعوم بين آخر الإطلاقات المفحوصة. جرّب البحث مجدداً."
