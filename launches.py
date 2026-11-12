"""Read-only discovery of recent Pump launches using the configured Solana RPC."""
import time

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
        if ix.get("programId") not in (str(chain.SPL), str(chain.TOKEN22)):
            continue
        if parsed.get("type") in ("initializeMint", "initializeMint2"):
            info = parsed["info"]
            if info.get("mintAuthority") == authority:
                yield info["mint"]


def find():
    authority = str(chain.pda(chain.PUMP, b"mint-authority"))
    signatures = bot.rpc("getSignaturesForAddress", [authority, dict(chain.FINAL, limit=16)])
    cutoff = time.time() - 1800
    for item in signatures:
        if item["err"] or (item.get("blockTime") or 0) < cutoff:
            continue
        tx = bot.rpc("getTransaction", [item["signature"], dict(chain.FINAL, encoding="jsonParsed",
                                                                 maxSupportedTransactionVersion=0)])
        if not tx:
            continue
        for address in initialized_mints(tx, authority):
            mint = chain.pk(address)
            try:
                market = chain.market(mint)
                if market and market[0] == "curve" and market[4] > 0 and chain.mint_info(mint):
                    return ("عقد إطلاق حديث على Solana:\n" + address +
                            "\nمنحنى SOL مفتوح وقت الفحص. فيك تنسخ العنوان وتفحصه أو تستخدمه بطلب شراء منفصل.")
            except ValueError:
                continue
    return "ما لقيت منحنى SOL مفتوح ومدعوم بين آخر الإطلاقات المفحوصة. جرّب البحث مجدداً."
