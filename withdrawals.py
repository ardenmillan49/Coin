"""Native SOL transfers. Only the exact transaction shown for review can be sent."""
from decimal import Decimal
import secrets
import bot


def init(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id TEXT PRIMARY KEY, bot INTEGER NOT NULL, destination TEXT NOT NULL,
            amount INTEGER NOT NULL, fee INTEGER NOT NULL, signature TEXT NOT NULL,
            raw TEXT NOT NULL, expiry INTEGER NOT NULL, state TEXT NOT NULL,
            attempted INTEGER NOT NULL DEFAULT 0);
        CREATE UNIQUE INDEX IF NOT EXISTS withdrawal_in_flight ON withdrawals((1))
            WHERE state IN ('sending','unknown');
    """)


def pending(db):
    return db.execute("SELECT * FROM withdrawals WHERE state IN ('sending','unknown')").fetchone()


def sol(value):
    return format(Decimal(value) / bot.LAMPORTS, "f")


def preview(row):
    return (f"راجع حوالة SOL قبل التأكيد\nعنوان الاستلام:\n{row['destination']}\n"
            f"المبلغ اللي رح يوصلك: {sol(row['amount'])} SOL\nرسم الشبكة: {sol(row['fee'])} SOL\n"
            f"إجمالي الخصم: {sol(row['amount'] + row['fee'])} SOL")


def destination(text, wallet):
    from solders.pubkey import Pubkey
    try:
        address = Pubkey.from_string(text.strip())
    except ValueError:
        raise ValueError("ابعت عنوان محفظة Solana كامل.") from None
    if str(address) == wallet or address == Pubkey.default():
        raise ValueError("اختار عنوان محفظة استلام مختلف.")
    account = bot.rpc("getAccountInfo", [str(address), {"encoding":"base64","commitment":"finalized",
                                                        "dataSlice":{"offset":0,"length":0}}])["value"]
    if account and (account["executable"] or account["owner"] != str(Pubkey.default()) or account.get("space",0)):
        raise ValueError("هاد عنوان حساب برنامج. ابعت عنوان محفظتك الرئيسية على Solana.")
    return str(address)


def quote(db, key, address, text):
    import solana_live as chain
    from solders.system_program import TransferParams, transfer
    chain.verify_mainnet()
    master = chain.keys()[0]
    payer = master.pubkey()
    configured = db.execute("SELECT wallet FROM live_settings WHERE bot=?", (key,)).fetchone()
    if not configured or str(payer) != configured[0]:
        raise ValueError("مفتاح المحفظة ما بيطابق عنوان محفظة البوت.")
    balance = bot.rpc("getBalance",[str(payer),chain.FINAL])["value"]
    all_funds = text == "all"
    value = 1 if all_funds else bot.sol_amount(text.strip().replace("٫",".").replace(",","."))
    def build(amount):
        ix = transfer(TransferParams(from_pubkey=payer,to_pubkey=chain.pk(address),lamports=amount))
        return chain.prepare([ix],[master])
    sig, raw, expiry = build(value)
    fee = chain.message_fee(raw)
    if all_funds:
        value = balance - fee
        if value <= 0:
            raise ValueError("الرصيد ما بيغطي رسم السحب.")
        sig, raw, expiry = build(value)
        fee = chain.message_fee(raw)
    if value + fee > balance:
        raise ValueError("الرصيد ما بيغطي المبلغ ورسم الشبكة.")
    result = bot.rpc("simulateTransaction",[raw,dict(chain.FINAL,encoding="base64",sigVerify=True)])["value"]
    if result["err"]:
        raise ValueError(f"تعذّرت محاكاة السحب: {result['err']}")
    if db.execute("SELECT 1 FROM withdrawals WHERE signature=? AND state!='review'",(sig,)).fetchone():
        raise ValueError("نفس التوقيع مسجل من قبل. انتظر تحديث الشبكة قبل طلب سحب جديد.")
    identifier = secrets.token_hex(8)
    db.execute("""INSERT INTO withdrawals(id,bot,destination,amount,fee,signature,raw,expiry,state)
                  VALUES(?,?,?,?,?,?,?,?,'review')""",(identifier,key,address,value,fee,sig,raw,expiry))
    return identifier


def confirm(db, key, identifier):
    row = db.execute("SELECT * FROM withdrawals WHERE id=? AND bot=? AND state='review'",(identifier,key)).fetchone()
    if not row or pending(db):
        return "طلب السحب غير متاح أو قيد التنفيذ."
    if db.execute("SELECT 1 FROM campaigns WHERE stage!='done'").fetchone():
        return "في صفقة حقيقية قيد التنفيذ. الرصيد مخصص إلها."
    if bot.rpc("getBlockHeight",[{"commitment":"finalized"}]) > row["expiry"]:
        db.execute("UPDATE withdrawals SET state='expired',raw='' WHERE id=?",(identifier,))
        return "انتهت صلاحية المعاينة. افتح السحب من جديد."
    db.execute("UPDATE withdrawals SET state='sending' WHERE id=?", (identifier,))
    return "انحفظ طلب السحب. عم نتابع تأكيد الشبكة.\n" + row["signature"]


def finish(db,row,state,text):
    with db:
        db.execute("UPDATE withdrawals SET state=?,raw='' WHERE id=?", (state,row["id"]))
        db.execute("""INSERT INTO replies(bot_id,chat,text)
                      SELECT id,owner,? FROM telegram WHERE id=?""",(text,row["bot"]))


def tick(db,key):
    row = pending(db)
    if not row or row["bot"] != key:
        return
    sig = row["signature"]
    result = bot.rpc("getSignatureStatuses",[[sig],{"searchTransactionHistory":True}])["value"][0]
    if result:
        if result["confirmationStatus"] != "finalized":
            return
        err = result["err"]
    elif bot.rpc("getBlockHeight",[{"commitment":"finalized"}]) > row["expiry"]:
        tx = bot.rpc("getTransaction",[sig,{"commitment":"finalized","maxSupportedTransactionVersion":0}])
        if not tx or not tx.get("meta"):
            if not row["attempted"]:
                finish(db,row,"expired","انتهت صلاحية السحب قبل إرساله.")
            elif row["state"] != "unknown":
                finish(db,row,"unknown","تعذّر حسم نتيجة السحب. راجع التوقيع قبل أي محاولة ثانية:\n"+sig)
            return
        err = tx["meta"]["err"]
    else:
        if row["state"]=="sending":
            with db:
                db.execute("UPDATE withdrawals SET attempted=1 WHERE id=?",(row["id"],))
            bot.rpc("sendTransaction",[row["raw"],{"encoding":"base64","skipPreflight":False,
                                                   "preflightCommitment":"confirmed","maxRetries":2}])
        return
    if err:
        finish(db,row,"failed",f"فشل السحب على الشبكة: {err}\n{sig}")
    else:
        finish(db,row,"done",f"تأكّد سحب {sol(row['amount'])} SOL إلى:\n{row['destination']}\nhttps://solscan.io/tx/{sig}")
