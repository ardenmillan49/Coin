"""SOL funding followed by purchases and sales signed by each child trader."""
import time

import bot

COUNT = 50
FUND_BATCH = 12
TRADE_BATCH = 2
MASTER_RESERVE = 2_000_000


def init(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS market_samples (
          campaign INTEGER PRIMARY KEY, tokens TEXT NOT NULL, sol TEXT NOT NULL, observed INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS child_plans (
          campaign INTEGER PRIMARY KEY, allocation INTEGER, order_size INTEGER,
          next_cursor INTEGER, exit_basis INTEGER, exit_units INTEGER);
        CREATE TABLE IF NOT EXISTS child_trades (
          campaign INTEGER, idx INTEGER, owner TEXT, units INTEGER DEFAULT 0,
          PRIMARY KEY(campaign,idx), UNIQUE(campaign,owner));
    """)


def percentage_cap(balance, bps):
    return balance * bps // 10000


def sale_estimate(token, sol, units):
    return sol * units // (token + units) * 985 // 1000


def overhead():
    # Reserve native-account rent, token ATA, volume accounts and potential WSOL ATA.
    return 5 * bot.rpc("getMinimumBalanceForRentExemption",[200]) + 20000


def start(db,row,chain,master,children,mint,notify):
    chain.verify_mainnet()
    venue = chain.market(mint)
    if venue is None:
        return
    if venue[0] != "curve":
        raise ValueError("العملة تجاوزت مرحلة الدخول على منحنى Pump.")
    info = chain.mint_info(mint)
    curve = bot.snapshot(str(mint))
    if not info or not curve:
        return
    report = bot.holder_report(str(mint),curve)
    balance = bot.rpc("getBalance", [str(master.pubkey()), chain.FINAL])["value"]
    cap = row["budget"]
    if row["budget_bps"]:
        cap = min(cap, percentage_cap(balance, row["budget_bps"]))
    count = row["wallet_count"]
    allocation = (cap - MASTER_RESERVE) // count
    order = allocation - overhead()
    if order <= 0:
        raise ValueError("الميزانية ما بتغطي احتياطي المحافظ المختارة.")
    if balance < cap:
        raise ValueError("رصيد الرئيسية أقل من الميزانية.")
    if len(children) != COUNT or len({str(k.pubkey()) for k in children}) != COUNT:
        raise ValueError("عدد المحافظ الفرعية غير صحيح")
    with db:
        changed = db.execute("""UPDATE campaigns SET stage='fund50',program=?,decimals=?,budget=?
                        WHERE id=? AND stage='plan50'""",(str(info[0]),info[1],cap,row["id"]))
        if not changed.rowcount:
            return
        db.execute("INSERT INTO child_plans(campaign,allocation,order_size) VALUES(?,?,?)",
                   (row["id"],allocation,order))
        db.executemany("INSERT INTO child_trades(campaign,idx,owner) VALUES(?,?,?)",
                       [(row["id"],i,str(k.pubkey())) for i,k in enumerate(children[:count])])
        notify(db,row,f"حصة كل محفظة {allocation/1e9:g} SOL، مبلغ شرائها {order/1e9:g} SOL. "
                      f"أكبر حساب خارج المنحنى {report['largest_external_account_pct']:.2f}%.")


def settle(db,row,tx,notify,sell_slippage):
    meta = tx["meta"]
    keys = [k["pubkey"] if isinstance(k,dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    owners = {r["owner"]:r["idx"] for r in db.execute("SELECT * FROM child_trades WHERE campaign=?",(row["id"],))}
    controlled = set(owners) | {row["wallet"]}
    cost = sum(a-b for k,a,b in zip(keys,meta["preBalances"],meta["postBalances"]) if k in controlled)
    stage, cursor = row["stage"], row["cursor"]
    plan = db.execute("SELECT * FROM child_plans WHERE campaign=?",(row["id"],)).fetchone()
    error = ""
    if meta["err"] and stage=="sell50" and not row["manual"] and sell_slippage(meta["err"]):
        pass  # Keep the same cursor and recompute the sale quote after a rejected price.
    elif meta["err"]:
        error = f"فشلت دفعة المحافظ بدءاً من {cursor+1}: {meta['err']}. راجع /resume."
    else:
        if stage in ("buy50","sell50"):
            delta = {}
            for field,sign in (("preTokenBalances",-1),("postTokenBalances",1)):
                for item in meta.get(field,[]):
                    owner = item.get("owner")
                    if item["mint"]==row["mint"] and owner in owners:
                        idx = owners[owner]
                        delta[idx] = delta.get(idx,0) + sign*int(item["uiTokenAmount"]["amount"])
            for idx in range(cursor,plan["next_cursor"]):
                change = delta.get(idx,0)
                if (stage=="buy50" and change<=0) or (stage=="sell50" and change>=0):
                    raise ValueError("تعذّر مطابقة إيصال التداول مع كل محفظة. التوقيع محفوظ.")
                db.execute("UPDATE child_trades SET units=units+? WHERE campaign=? AND idx=?",
                           (change,row["id"],idx))
        cursor = plan["next_cursor"]
        if cursor==row["wallet_count"]:
            stage = {"fund50":"buy50","buy50":"hold50","sell50":"done"}[stage]
            cursor = 0
            notify(db,row,{"buy50":"تأكّد تمويل المحافظ المختارة. بلّش الشراء من عناوينها.",
                           "hold50":"تأكدت مشتريات المحافظ المختارة. بلّشت مراقبة هدف الربح.",
                           "done":"تأكد البيع. الـSOL بالمحافظ الفرعية، واختيار «تجميع SOL» بيرجعه للرئيسية."}[stage])
    db.execute("INSERT INTO live_receipts VALUES(?,?,?,?,?,?,?)",
               (row["pending"],row["id"],row["stage"],row["cursor"],cost,meta["fee"],not meta["err"]))
    db.execute("""UPDATE campaigns SET stage=?,cursor=?,error=?,spent=spent+?,last_sig=pending,
               raw=NULL,pending=NULL,expiry=NULL WHERE id=?""",(stage,cursor,error,cost,row["id"]))
    if error:
        notify(db,row,error)


def issue(db,row,chain,ix,signers,end,accounts):
    sig,raw,expiry = chain.prepare(ix,signers)
    fee = chain.message_fee(raw)
    if fee + row["spent"] > row["budget"]:
        raise ValueError("الميزانية ما بتغطي رسم الدفعة")
    before = bot.rpc("getMultipleAccounts",[accounts,dict(chain.FINAL,encoding="base64",
                     dataSlice={"offset":0,"length":0})])["value"]
    sim = bot.rpc("simulateTransaction",[raw,dict(chain.FINAL,encoding="base64",sigVerify=True,
                  accounts={"addresses":accounts,"encoding":"base64"})])["value"]
    if sim["err"]:
        if row["stage"]=="sell50" and not row["manual"] and any(
                s in " ".join(sim.get("logs") or []).lower() for s in ("slippage","toolittlesolreceived")):
            return
        raise ValueError(f"تعذرت محاكاة دفعة {row['cursor']+1}: {sim['err']}")
    after = sim["accounts"]
    if len(after)!=len(accounts) or len(before)!=len(accounts):
        raise ValueError("نتيجة فحص الأرصدة ناقصة")
    cost = sum((a or {}).get("lamports",0)-(b or {}).get("lamports",0) for a,b in zip(before,after))
    if row["spent"]+cost > row["budget"]:
        raise ValueError("تكلفة الدفعة بتتجاوز الميزانية")
    # Protect the master's operating reserve from funding retries as well as group signatures.
    debits = db.execute("SELECT COALESCE(SUM(network_fee),0) FROM live_receipts WHERE campaign=?",
                        (row["id"],)).fetchone()[0]
    if debits + fee > MASTER_RESERVE:
        raise ValueError("نفد احتياطي رسوم التواقيع")
    with db:
        changed = db.execute("""UPDATE campaigns SET pending=?,raw=?,expiry=? WHERE id=? AND stage=?
                     AND cursor=? AND pending IS NULL AND manual=?""",
                     (sig,raw,expiry,row["id"],row["stage"],row["cursor"],row["manual"]))
        if changed.rowcount:
            db.execute("UPDATE child_plans SET next_cursor=? WHERE campaign=?",(end,row["id"]))


def step(db,row,chain,master,children,mint,notify):
    from solders.system_program import TransferParams, transfer
    stage = row["stage"]
    if stage=="plan50":
        start(db,row,chain,master,children,mint,notify)
        return
    count = row["wallet_count"]
    children = children[:count]
    plan = db.execute("SELECT * FROM child_plans WHERE campaign=?",(row["id"],)).fetchone()
    legs = db.execute("SELECT * FROM child_trades WHERE campaign=? ORDER BY idx",(row["id"],)).fetchall()
    if any(str(key.pubkey())!=leg["owner"] for key,leg in zip(children,legs)) or len(legs)!=count or len(children)!=count:
        raise ValueError("عناوين المحافظ لا تطابق سجل الصفقة")
    total = sum(r["units"] for r in legs)
    if row["manual"] and stage in ("fund50","buy50","hold50"):
        with db:
            db.execute("UPDATE campaigns SET stage=?,cursor=0 WHERE id=?",
                       ("sell50" if total else "done",row["id"]))
            if not total:
                notify(db,row,"توقف الطلب بدون توكنات مفتوحة. فيك تجمع أي SOL موزّع.")
        return
    i = row["cursor"]
    payer = master.pubkey()
    if stage=="fund50":
        end = min(count,i+FUND_BATCH)
        ix = [transfer(TransferParams(from_pubkey=payer,to_pubkey=c.pubkey(),lamports=plan["allocation"]))
              for c in children[i:end]]
        issue(db,row,chain,ix,[master],end,[str(payer),*[l["owner"] for l in legs[i:end]]])
        return
    if stage=="sell50" and i<count and not legs[i]["units"]:
        next_i = next((j for j in range(i,count) if legs[j]["units"]),count)
        with db:
            db.execute("UPDATE campaigns SET cursor=?,stage=? WHERE id=?",
                       (next_i,"done" if next_i==count else stage,row["id"]))
            if next_i==count:
                notify(db,row,"انتهى بيع الكميات المشتراة. التجميع اليدوي متاح.")
        return
    venue = chain.market(mint)
    if not venue:
        return
    token,sol = venue[2:4]
    now = int(time.time())
    sample = db.execute("SELECT observed FROM market_samples WHERE campaign=?", (row["id"],)).fetchone()
    if not sample or now - sample[0] >= 5:
        with db:
            db.execute("INSERT OR REPLACE INTO market_samples VALUES(?,?,?,?)",
                       (row["id"], str(token), str(sol), now))
    if stage=="hold50":
        basis = row["spent"]
        if sale_estimate(token, sol, total) >= (basis*(10000+row["target"])+9999)//10000 + count*10000:
            with db:
                db.execute("UPDATE child_plans SET exit_basis=?,exit_units=? WHERE campaign=?",(basis,total,row["id"]))
                db.execute("UPDATE campaigns SET stage='sell50',cursor=0 WHERE id=?",(row["id"],))
        return
    program = chain.pk(row["program"])
    if stage=="buy50" and venue[0]!="curve":
        raise ValueError("هاجرت العملة أثناء الشراء. فيك تبيع الكمية المفتوحة بزر البيع اليدوي.")
    # Two independent trader signatures per curve transaction; pool exits need more accounts.
    end = min(count,i+(TRADE_BATCH if venue[0]=="curve" else 1))
    if stage=="sell50" and end>i+1 and not legs[i+1]["units"]:
        end = i+1
    from solders.compute_budget import set_compute_unit_limit
    ix = [set_compute_unit_limit(600000)]
    remaining = venue[4]
    for j in range(i,end):
        user = children[j].pubkey()
        if stage=="buy50":
            spend = plan["order_size"]
            out = token*spend//(sol+spend)
            if out <= 1 or out >= remaining:
                raise ValueError("حجم الدفعة أكبر من سيولة الإطلاق المتبقية")
            ix.extend([chain.create_ata(user,user,mint,program),
                       chain.trade(user,mint,program,venue,True,spend,max(1,out*97//100))])
            remaining -= out
            token -= out
            sol += spend
        else:
            units = legs[j]["units"]
            gross = sol*units//(token+units)
            floor = max(1,gross*97//100)
            if not row["manual"]:
                num = plan["exit_basis"]*(10000+row["target"])*units
                den = plan["exit_units"]*10000
                floor = (num+den-1)//den + 10000
                if venue[0]=="pool" and chain.data_account(chain.pda(chain.AMM,b"user_volume_accumulator",user),chain.AMM) is None:
                    floor += bot.rpc("getMinimumBalanceForRentExemption",[200])
                if gross<floor:
                    return
            if venue[0]=="pool":
                if chain.data_account(chain.ata(user,chain.WSOL,chain.SPL),chain.SPL) is not None:
                    raise ValueError("حساب WSOL موجود مسبقاً بالمحفظة الفرعية")
                ix.append(chain.create_ata(user,user,chain.WSOL,chain.SPL))
            ix.append(chain.trade(user,mint,program,venue,False,units,floor))
            if venue[0]=="pool":
                ix.append(chain.close_ata(user,user,chain.WSOL,chain.SPL))
            if chain.amount(chain.ata(user,mint,program))==units:
                ix.append(chain.close_ata(user,user,mint,program))
            token += units
            sol -= gross
    issue(db,row,chain,ix,[master,*children[i:end]],end,[str(payer),*[l["owner"] for l in legs[i:end]]])
