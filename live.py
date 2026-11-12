"""Persist every signed transaction before broadcast; advance only from finalized receipts."""
from decimal import Decimal, InvalidOperation
import bot
import withdrawals
import time

N = 50
BATCH = 4
FEE_MARGIN = 2_000_000
ACTIONS = {"محفظة SOL": "/wallet", "إعداد شراء حقيقي": "/plan", "شراء حقيقي": "/buy",
           "حالة التداول الحقيقي": "/live", "بيع حقيقي الآن": "/exit", "إلغاء طلب الشراء": "/unarm"}
STAGES = {"planned": "بانتظار العقد", "buy": "بانتظار الشراء", "split": "توزيع",
          "hold": "مراقبة الهدف", "collect": "تجميع للبيع", "sell": "بيع", "done": "مكتملة"}


def init(db):
    withdrawals.init(db)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS live_settings (
            bot INTEGER PRIMARY KEY, wallet TEXT, budget INTEGER, target INTEGER, prompt TEXT);
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY, bot INTEGER, wallet TEXT, mint TEXT, budget INTEGER, target INTEGER,
            stage TEXT, cursor INTEGER DEFAULT 0, units INTEGER DEFAULT 0, spent INTEGER DEFAULT 0,
            locked INTEGER DEFAULT 0, manual INTEGER DEFAULT 0, error TEXT DEFAULT '',
            program TEXT, decimals INTEGER, last_sig TEXT, pending TEXT, raw TEXT, expiry INTEGER);
        CREATE TABLE IF NOT EXISTS live_receipts (
            signature TEXT PRIMARY KEY, campaign INTEGER, stage TEXT, cursor INTEGER,
            payer_delta INTEGER, network_fee INTEGER, success INTEGER);
        CREATE UNIQUE INDEX IF NOT EXISTS live_single ON campaigns((1)) WHERE stage != 'done';
    """)


def quantities(total):
    q, r = divmod(total, N)
    if q == 0:
        raise ValueError("الكمية أقل من عدد المحافظ")
    return [q + (i < r) for i in range(N)]


def target_amount(cost, target):
    return (cost * (10000 + target) + 9999) // 10000


def active(db, key):
    return db.execute("SELECT * FROM campaigns WHERE bot=? AND stage!='done'", (key,)).fetchone()


def status(row):
    return (f"{STAGES[row['stage']]} | {row['mint']}\n"
            f"الميزانية {row['budget']/1e9:g} SOL، الهدف +{row['target']/100:g}%\n"
            f"آخر دفعة: {row['cursor']}/50\nصافي المدفوع حتى الآن: {row['spent']/1e9:g} SOL"
            + (f"\n{row['error']}" if row["error"] else "")
            + (f"\nآخر معاملة: {row['last_sig']}" if row["last_sig"] else ""))


def command(db, key, text):
    action = ACTIONS.get(text, text)
    cfg = db.execute("SELECT * FROM live_settings WHERE bot=?", (key,)).fetchone()
    row = active(db, key)
    if action == "/wallet":
        import solana_live as chain
        wallet = str(chain.keys(create=not cfg)[0].pubkey())
        if cfg and cfg["wallet"] != wallet:
            raise ValueError("ملف مفتاح المحفظة ما بيطابق العنوان المحفوظ")
        db.execute("INSERT OR IGNORE INTO live_settings(bot,wallet) VALUES(?,?)", (key, wallet))
        try:
            balance = bot.rpc("getBalance", [wallet, chain.FINAL])["value"]
        except OSError:
            return f"عنوان محفظة البوت على Solana:\n{wallet}\nتعذّر جلب الرصيد من RPC."

        return f"عنوان محفظة التداول على Solana:\n{wallet}\nالرصيد {balance/1e9:g} SOL"
    if action == "/wallets" and cfg:
        import solana_live as chain
        master, children = chain.keys()
        if str(master.pubkey()) != cfg["wallet"]:
            raise ValueError("مش نفس مفتاح المحفظة")
        return "\n".join(f"{i}. {child.pubkey()}" for i, child in enumerate(children,1))
    if action == "/ready":
        import solana_live as chain
        if not cfg:
            return "افتح محفظة SOL أولاً."
        master, children = chain.keys()
        if str(master.pubkey()) != cfg["wallet"] or len({str(k.pubkey()) for k in children}) != N:
            raise ValueError("مفاتيح المحفظة غير متطابقة")
        balance = bot.rpc("getBalance",[cfg["wallet"],chain.FINAL])["value"]
        chain.verify_mainnet()
        # The live entry inspection uses this RPC method; many shared endpoints restrict it.
        try:
            bot.largest_accounts("4a9ZHEceRAfHAribgXDsdYxfR1hqf6MrRQja9caQpump",chain.FINAL)
        except OSError:
            return "تعذّر فحص الحيازات عبر المسارات المتاحة. اضبط SOLANA_HOLDERS_RPC_URL بمزوّد يدعم getTokenLargestAccounts في /etc/coin-bot.env ثم أعد تشغيل الخدمة."
        reserve = reserve_for_distribution()
        check = "فحص التوقيع متاح بعد التمويل."
        if balance >= 5000:
            from solders.compute_budget import set_compute_unit_limit
            _, raw, _ = chain.prepare([set_compute_unit_limit(20000)], [master])
            sim = bot.rpc("simulateTransaction",[raw,dict(chain.FINAL,encoding="base64",sigVerify=True)])["value"]
            if sim["err"]:
                raise ValueError(f"فشل اختبار التوقيع: {sim['err']}")
            check = "نجحت محاكاة معاملة موقعة بدون بث."
        funded = bool(cfg["budget"] and reserve < cfg["budget"] <= balance)
        return (f"اتصال Solana شغّال. تم التحقق من مفاتيح 50 محفظة. الرصيد {balance/1e9:g} SOL."
                f" احتياطي الحسابات والرسوم المقدر {reserve/1e9:g} SOL. {check}"
                + (" الميزانية مغطّاة." if funded else " اضبط الميزانية والتمويل قبل الشراء."))
    if action == "/live":
        latest = row or db.execute("SELECT * FROM campaigns WHERE bot=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        return status(latest) if latest else "ما في صفقة حقيقية."
    if action == "/unarm":
        if row and row["stage"] == "buy" and not row["pending"]:
            db.execute("UPDATE campaigns SET stage='done',error='تم إلغاء الشراء' WHERE id=?", (row["id"],))
        db.execute("UPDATE live_settings SET prompt=NULL WHERE bot=?", (key,))
        return "تم إلغاء الإدخال. الصفقة المرسلة أو المفتوحة بتضل مسجّلة."
    if action == "/resume":
        if not row or row["pending"]:
            return "ما في خطوة قابلة للاستئناف. المعاملة المرسلة بتنراجع تلقائياً."
        db.execute("UPDATE campaigns SET error='' WHERE id=?", (row["id"],))
        return "تم استئناف المتابعة."
    if action == "/exit":
        if not row or row["stage"] not in ("split", "hold", "collect", "sell"):
            return "ما في صفقة حقيقية مفتوحة للبيع."
        db.execute("UPDATE campaigns SET manual=1,error='' WHERE id=?", (row["id"],))
        return "تم طلب البيع اليدوي بسعر السوق مع حد انزلاق 3%."
    if action in ("/plan", "/buy"):
        if not cfg:
            return "اضغط «محفظة SOL» أول مرة."
        if row:
            return status(row)
        if action == "/plan":
            db.execute("UPDATE live_settings SET prompt='plan' WHERE bot=?", (key,))
            return "ابعت ميزانيتك الإجمالية بـ SOL ونسبة الربح برسالة واحدة. مثال: 0.5 100"
        if not cfg["budget"] or not cfg["target"]:
            return "اضبط حقلي «الميزانية» و«هدف الربح» أولاً."
        db.execute("UPDATE live_settings SET prompt='mint' WHERE bot=?", (key,))
        return f"ابعت عقد Pump.fun لتفعيل الشراء الحقيقي بحد {cfg['budget']/1e9:g} SOL وهدف +{cfg['target']/100:g}%."
    if not cfg or not cfg["prompt"]:
        return None
    if text.startswith("/") or text in ("مراقبة عملة","فحص عملة","حالة الصفقة","بيع تجريبي","إلغاء المراقبة"):
        db.execute("UPDATE live_settings SET prompt=NULL WHERE bot=?", (key,))
        return None
    if cfg["prompt"] == "plan":
        parts = text.split()
        if len(parts) != 2:
            raise ValueError("اكتب الميزانية ثم نسبة الهدف")
        budget = bot.sol_amount(parts[0])
        pct = Decimal(parts[1].rstrip("%")) * 100
        if not pct.is_finite() or not 1 <= pct <= 1000000 or pct != int(pct):
            raise ValueError("نسبة الهدف بين 0.01% و10000%")
        db.execute("UPDATE live_settings SET budget=?,target=?,prompt=NULL WHERE bot=?", (budget,int(pct),key))
        return "انحفظت الخطة لـ 50 محفظة. وقت الإطلاق اضغط «شراء حقيقي» وابعت العقد."
    import solana_live as chain
    mint = str(chain.pk(text.strip()))
    if row:
        return status(row)
    if withdrawals.pending(db):
        return "في سحب قيد التأكيد. انتظر نتيجته قبل الشراء."
    db.execute("INSERT INTO campaigns(bot,wallet,mint,budget,target,stage) VALUES(?,?,?,?,?,'buy')",
               (key,cfg["wallet"],mint,cfg["budget"],cfg["target"]))
    db.execute("UPDATE live_settings SET prompt=NULL WHERE bot=?", (key,))
    return "تم تفعيل الشراء الحقيقي. رح يوصلك تأكيد المعاملات."


def notify(db, row, message):
    db.execute("""INSERT INTO replies(bot_id,chat,text)
               SELECT id,owner,? FROM telegram WHERE id=? AND owner IS NOT NULL""", (message,row["bot"]))


def sell_slippage(error):
    if not isinstance(error, dict):
        return False
    instruction = error.get("InstructionError", [])
    return (len(instruction) == 2 and isinstance(instruction[1], dict)
            and instruction[1].get("Custom") in (6003,6004))


def receipt(db, row, meta):
    cost = meta["preBalances"][0] - meta["postBalances"][0]
    db.execute("INSERT INTO live_receipts VALUES(?,?,?,?,?,?,?)",
               (row["pending"],row["id"],row["stage"],row["cursor"],cost,meta["fee"],meta["err"] is None))
    spent = row["spent"] + cost
    stage, cursor, units, locked = row["stage"], row["cursor"], row["units"], row["locked"]
    err = ""
    if meta["err"] is not None and stage == "sell" and not row["manual"] and sell_slippage(meta["err"]):
        # The failed signature still incurred a network fee. Requote against the revised basis.
        pass
    elif meta["err"] is not None:
        err = "فشلت المعاملة. راجع التوقيع ثم /resume لإعادة المحاولة."
    elif stage == "buy":
        def held(side):
            return sum(int(x["uiTokenAmount"]["amount"]) for x in meta.get(side, [])
                       if x.get("owner") == row["wallet"] and x["mint"] == row["mint"])
        units = held("postTokenBalances") - held("preTokenBalances")
        stage = "split" if units >= N else "hold"
        notify(db, row, f"تأكّد الشراء. الكمية بوحدات العملة الخام: {units}.")
    elif stage in ("split", "collect"):
        end = min(cursor + BATCH, N)
        if stage == "split":
            locked += cost - meta["fee"]
        if end == N:
            stage = "hold" if stage == "split" else "sell"
            if stage == "hold":
                notify(db,row,"تأكّد التوزيع على 50 محفظة. بلّشت متابعة هدف البيع.")
            cursor = 0
        else:
            cursor = end
    else:
        stage = "done"
        notify(db,row,f"تأكّد البيع. الربح/الخسارة بعد حركة SOL والرسوم: {-spent/1e9:g} SOL.")
    if err:
        notify(db,row,err)
    db.execute("""UPDATE campaigns SET spent=?,stage=?,cursor=?,units=?,locked=?,error=?,
               last_sig=pending,pending=NULL,raw=NULL,expiry=NULL WHERE id=?""",
               (spent,stage,cursor,units,locked,err,row["id"]))


def reconcile(db, row, chain):
    sig = row["pending"]
    result = bot.rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory":True}])["value"][0]
    if result:
        if result["confirmationStatus"] == "finalized":
            tx = bot.rpc("getTransaction", [sig, dict(chain.FINAL, encoding="jsonParsed",maxSupportedTransactionVersion=0)])
            if tx and tx.get("meta"):
                with db:
                    receipt(db,row,tx["meta"])
        return
    if bot.rpc("getBlockHeight", [chain.FINAL]) > row["expiry"]:
        # Check finalized history even if the status-cache lookup was inconclusive.
        tx = bot.rpc("getTransaction", [sig, dict(chain.FINAL, encoding="jsonParsed",maxSupportedTransactionVersion=0)])
        if tx and tx.get("meta"):
            with db:
                receipt(db,row,tx["meta"])
        elif not row["error"]:
            with db:
                message = f"انتهت صلاحية المعاملة بدون إثبات تنفيذ. افحص التوقيع: {sig}. الإرسال المكرر متوقف."
                db.execute("UPDATE campaigns SET error=? WHERE id=?", (message,row["id"]))
                notify(db,row,message)
        return
    bot.rpc("sendTransaction", [row["raw"], {"encoding":"base64","skipPreflight":False,
                                            "preflightCommitment":"confirmed","maxRetries":2}])


def issue(db, row, chain, ix, signers):
    sig, raw, expiry = chain.prepare(ix, signers)
    wallet = row["wallet"]
    fee = chain.message_fee(raw)
    if row["spent"] + fee > row["budget"]:
        raise ValueError("ما بقي احتياطي كافي لرسم المعاملة")
    if row["stage"] == "sell" and not row["manual"] and fee > 5000:
        raise ValueError("رسم البيع أعلى من الرسم المحسوب بحد الهدف")
    before = bot.rpc("getBalance", [wallet,chain.FINAL])["value"]
    sim = bot.rpc("simulateTransaction", [raw, dict(chain.FINAL,encoding="base64",sigVerify=True,
                  accounts={"encoding":"base64","addresses":[wallet]})])["value"]
    if sim["err"]:
        # The on-chain sale floor may currently exceed the executable quote.
        if row["stage"] == "sell" and not row["manual"] and (sell_slippage(sim["err"]) or "slippage" in " ".join(sim.get("logs") or []).lower()):
            return
        raise ValueError(f"فشلت محاكاة {row['stage']} قبل الإرسال: {sim['err']}")
    delta = before - sim["accounts"][0]["lamports"]
    if row["spent"] + delta > row["budget"]:
        raise ValueError("التكلفة المتوقعة بتتجاوز الميزانية المحفوظة")
    if row["stage"] == "sell" and not row["manual"] and -delta < target_amount(row["spent"],row["target"]):
        return
    with db:
        updated = db.execute("""UPDATE campaigns SET pending=?,raw=?,expiry=? WHERE id=? AND
                         stage=? AND cursor=? AND pending IS NULL""",
                         (sig,raw,expiry,row["id"],row["stage"],row["cursor"]))
        if not updated.rowcount:
            return
    return sig



def reserve_for_distribution():
    return bot.rpc("getMinimumBalanceForRentExemption", [200]) * (N + 3) + FEE_MARGIN


def step(db, row, chain):
    master, children = chain.keys()
    user = master.pubkey()
    if str(user) != row["wallet"]:
        raise ValueError("استعد ملف مفتاح المحفظة الأصلي")
    if row["pending"]:
        reconcile(db,row,chain)
        return
    if row["error"]:
        return
    mint = chain.pk(row["mint"])
    stage = row["stage"]
    if stage == "buy":
        chain.verify_mainnet()
        venue = chain.market(mint)
        if not venue:
            return
        if venue[0] != "curve":
            raise ValueError("انتهت مرحلة إطلاق العملة قبل الدخول")
        info = chain.mint_info(mint)
        if info is None:
            return
        program, decimals = info
        reserve = reserve_for_distribution()
        spend = row["budget"] - reserve - row["spent"]
        if spend <= 0:
            raise ValueError(f"الميزانية ما بتغطي احتياطي التوزيع والرسوم ({reserve/1e9:g} SOL)")
        if bot.rpc("getBalance",[str(user),chain.FINAL])["value"] < row["budget"] - row["spent"]:
            raise ValueError("رصيد المحفظة أقل من الميزانية")
        curve = bot.snapshot(str(mint))
        if curve is None:
            return
        report = bot.holder_report(str(mint),curve)
        minimum = venue[2] * spend // (venue[3] + spend) * 97 // 100
        if minimum < N or minimum >= venue[4]:
            raise ValueError("الطلب خارج سيولة المنحنى")
        ix = [chain.create_ata(user,user,mint,program), chain.trade(user,mint,program,venue,True,spend,minimum)]
        with db:
            db.execute("UPDATE campaigns SET program=?,decimals=? WHERE id=?", (str(program),decimals,row["id"]))
        if not issue(db,row,chain,ix,[master]):
            return
        with db:
            notify(db,row,f"فحص الدخول: حصة المنشئ {report['creator_pct']}%، أكبر حساب {report['largest_external_account_pct']:.2f}%.")
        return
    program = chain.pk(row["program"])
    if stage in ("split","collect"):
        pieces = quantities(row["units"])
        ix, signers = [], [master]
        indices = range(row["cursor"],min(row["cursor"] + BATCH,N))
        balances = {}
        if stage == "collect":
            addresses = [str(chain.ata(children[i].pubkey(),mint,program)) for i in indices]
            accounts = bot.rpc("getMultipleAccounts",[addresses,dict(chain.FINAL,encoding="jsonParsed")])["value"]
            for i, account in zip(indices,accounts):
                if not account:
                    raise ValueError(f"حساب المحفظة الفرعية {i+1} غير موجود")
                balances[i] = int(account["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        for i in indices:
            child = children[i].pubkey()
            if stage == "split":
                ix.extend([chain.create_ata(user,child,mint,program),
                           chain.transfer(user,child,mint,program,pieces[i],row["decimals"])])
            else:
                ix.append(chain.transfer(child,user,mint,program,pieces[i],row["decimals"]))
                if balances[i] == pieces[i]:
                    ix.append(chain.close_ata(child,user,mint,program))
                signers.append(children[i])
        issue(db,row,chain,ix,signers)
        return
    venue = chain.market(mint)
    if not venue:
        return
    units = row["units"]
    gross = venue[3] * units // (venue[2] + units)
    if stage == "hold":
        estimated_net = gross * 985 // 1000
        basis = row["spent"] - row["locked"] + (N + (N+BATCH-1)//BATCH) * 5000
        if row["manual"] or estimated_net >= target_amount(basis,row["target"]) + 5000:
            with db:
                db.execute("UPDATE campaigns SET stage='collect',cursor=0 WHERE id=?", (row["id"],))
                notify(db,row,"بلّش تجميع الكمية للبيع. رح نعيد فحص سعر التنفيذ بعد التجميع.")
        return
    minimum = max(1, gross * 97 // 100 if row["manual"] else target_amount(row["spent"],row["target"]) + 5000)
    if venue[0] == "pool" and not row["manual"]:
        volume = chain.pda(chain.AMM,b"user_volume_accumulator",user)
        if chain.data_account(volume,chain.AMM) is None:
            minimum += bot.rpc("getMinimumBalanceForRentExemption",[200])
    if gross < minimum:
        return
    ix = [chain.trade(user,mint,program,venue,False,units,minimum)]
    if venue[0] == "pool":
        # Isolate this campaign from a pre-existing WSOL position.
        existing = chain.data_account(chain.ata(user,chain.WSOL,chain.SPL),chain.SPL)
        if existing is not None:
            raise ValueError("محفظة البوت فيها حساب WSOL موجود. فك التغليف قبل البيع ثم /resume.")
        ix = [chain.create_ata(user,user,chain.WSOL,chain.SPL), *ix, chain.close_ata(user,user,chain.WSOL,chain.SPL)]
    issue(db,row,chain,ix,[master])


_last_rpc_notice = {}


def tick(db, key):
    row = active(db,key)
    if not row:
        return
    try:
        import solana_live as chain
        step(db,row,chain)
    except OSError as exc:
        # Surface persistent provider restrictions without exposing URLs or credentials.
        now = time.monotonic()
        if now - _last_rpc_notice.get(row["id"], -1000) >= 60:
            _last_rpc_notice[row["id"]] = now
            message = f"تعذّر اتصال Solana RPC ({getattr(exc, 'code', None) or type(exc).__name__}). رح نعيد المحاولة."
            with db:
                notify(db,row,message)
        return
    except (ValueError, InvalidOperation, KeyError, TypeError, ImportError) as exc:
        message = f"توقف التنفيذ: {exc}"
        if row["error"] != message:
            with db:
                db.execute("UPDATE campaigns SET error=? WHERE id=?", (message,row["id"]))
                notify(db,row,message)
