"""Return native SOL held by child wallets to their saved parent."""
import json
import secrets

import bot
import withdrawals


def init(db):
    db.execute("""CREATE TABLE IF NOT EXISTS sweeps (
        id TEXT PRIMARY KEY, bot INTEGER, wallet TEXT, items TEXT, fee INTEGER,
        cursor INTEGER DEFAULT 0, receipt TEXT, total INTEGER DEFAULT 0,
        state TEXT DEFAULT 'review')""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_sweep ON sweeps((1)) WHERE state='running'")


def running(db):
    return db.execute("SELECT * FROM sweeps WHERE state='running'").fetchone()


def build(chain, child, parent, units):
    from solders.system_program import TransferParams, transfer
    ix = transfer(TransferParams(from_pubkey=child.pubkey(),to_pubkey=parent,lamports=units))
    return chain.prepare([ix],[child])


def preview(db,key):
    import solana_live as chain
    cfg = db.execute("SELECT wallet FROM live_settings WHERE bot=?", (key,)).fetchone()
    if not cfg:
        return None,"افتح المحفظة أول مرة."
    chain.verify_mainnet()
    parent, children = chain.keys()
    if str(parent.pubkey()) != cfg[0]:
        raise ValueError("مفتاح الرئيسية ما بيطابق العنوان المحفوظ.")
    addresses = [str(child.pubkey()) for child in children]
    accounts = bot.rpc("getMultipleAccounts",[addresses,dict(chain.FINAL,encoding="base64",
                        dataSlice={"offset":0,"length":0})])["value"]
    if len(accounts) != len(children):
        raise OSError("Incomplete balances")
    balances = [(i,a["lamports"]) for i,a in enumerate(accounts) if a and a["lamports"] and
                a["owner"] == str(chain.SYS) and not a["executable"] and not a.get("space",0)]
    if not balances:
        return None,"فحصت الـ50. ما في SOL قابل للتجميع من المحافظ الفرعية."
    _,raw,_ = build(chain,children[balances[0][0]],parent.pubkey(),1)
    fee = chain.message_fee(raw)
    items = [(i,balance) for i,balance in balances if balance>fee]
    if not items:
        return None,"الأرصدة الموجودة ما بتغطّي رسم التحويل."
    ident = secrets.token_hex(8)
    db.execute("INSERT INTO sweeps(id,bot,wallet,items,fee) VALUES(?,?,?,?,?)",
               (ident,key,cfg[0],json.dumps(items),fee))
    return ident,""


def summary(row):
    items = json.loads(row["items"])
    fee = len(items)*row["fee"]
    net = sum(balance for _,balance in items)-fee
    return (f"تجميع من {len(items)} محفظة إلى الرئيسية:\n{row['wallet']}\n"
            f"صافي التجميع: {withdrawals.sol(net)} SOL\nرسوم الشبكة: {withdrawals.sol(fee)} SOL")


def confirm(db,key,ident):
    if running(db) or withdrawals.pending(db) or db.execute("SELECT 1 FROM campaigns WHERE stage!='done'").fetchone():
        return "في عملية قيد التنفيذ."
    count = db.execute("UPDATE sweeps SET state='running' WHERE id=? AND bot=? AND state='review'",
                       (ident,key)).rowcount
    return "بلّش تجميع SOL إلى الرئيسية." if count else "معاينة التجميع منتهية."


def stop(db,row,text,state):
    with db:
        db.execute("UPDATE sweeps SET state=? WHERE id=?", (state,row["id"]))
        db.execute("""INSERT INTO replies(bot_id,chat,text) SELECT id,owner,? FROM telegram WHERE id=?""",
                   (text,row["bot"]))


def tick(db,key):
    row = running(db)
    if not row or row["bot"] != key:
        return
    items = json.loads(row["items"])
    if row["receipt"]:
        receipt = db.execute("SELECT * FROM withdrawals WHERE id=?", (row["receipt"],)).fetchone()
        if receipt["state"] in ("sending","unknown"):
            return
        if receipt["state"] != "done":
            stop(db,row,"توقّف التجميع. فيك تعيد الفحص للأرصدة المتبقية.","stopped")
            return
        with db:
            db.execute("UPDATE sweeps SET receipt=NULL,cursor=cursor+1,total=total+? WHERE id=?",
                       (receipt["amount"],row["id"]))
        return
    if row["cursor"] == len(items):
        stop(db,row,f"اكتمل التجميع إلى الرئيسية: {withdrawals.sol(row['total'])} SOL.","done")
        return
    import solana_live as chain
    parent,children = chain.keys()
    index,balance = items[row["cursor"]]
    child = children[index]
    if str(parent.pubkey()) != row["wallet"]:
        stop(db,row,"تغيّر مفتاح الرئيسية. توقّف التجميع.","stopped")
        return
    value = balance-row["fee"]
    sig,raw,expiry = build(chain,child,parent.pubkey(),value)
    if chain.message_fee(raw) != row["fee"]:
        stop(db,row,"تغيّر رسم الشبكة. أعد فحص التجميع.","stopped")
        return
    sim = bot.rpc("simulateTransaction",[raw,dict(chain.FINAL,encoding="base64",sigVerify=True)])["value"]
    if sim["err"]:
        stop(db,row,f"توقّف التجميع قبل الإرسال عند المحفظة {index+1}: {sim['err']}","stopped")
        return
    identifier = f"{row['id']}:{index}"
    with db:
        db.execute("""INSERT INTO withdrawals(id,bot,destination,amount,fee,signature,raw,expiry,state)
                      VALUES(?,?,?,?,?,?,?,?,'sending')""",(identifier,key,row["wallet"],value,row["fee"],sig,raw,expiry))
        db.execute("UPDATE sweeps SET receipt=? WHERE id=?", (identifier,row["id"]))
