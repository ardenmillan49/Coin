"""Persistent inline Telegram dashboard."""
from decimal import Decimal
import bot
import dashboard
import live
import sweep
import withdrawals

HOME = [
    [("الميزانية", "budget"), ("هدف الربح", "target")],
    [("عدد المحافظ", "count")],
    [("المحفظة", "/wallet"), ("فحص الجاهزية", "/ready")],
    [("شراء حقيقي", "/buy"), ("لوحة الصفقة 📊", "/live")],
    [("بيع حقيقي الآن", "/exit"), ("إلغاء طلب الشراء", "/unarm")],
    [("فحص المحافظ الـ50", "/checkwallets"), ("تجميع SOL", "sweep")],
    [("سحب SOL", "withdraw"), ("المحاكاة", "paper")],
]
PAPER = [
    [("العثور على عقد إطلاق", "find_launch")],
    [("مراقبة عملة", "/watch"), ("فحص عملة", "/inspect")],
    [("حالة الصفقة", "/status"), ("بيع تجريبي", "/sell")],
    [("إلغاء المراقبة", "/cancel"), ("رجوع", "home")],
]
FIELDS = {"count": "عدد محافظ الشراء (1–50)", "budget": "نسبة الشراء من رصيد الرئيسية (%)", "target": "هدف الربح للبيع (%)"}


def init(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS panels (
          bot INTEGER PRIMARY KEY, message INTEGER, view TEXT DEFAULT 'home',
          draft TEXT DEFAULT '', migrated INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS input_cleanup (
          bot INTEGER, chat INTEGER, message INTEGER, PRIMARY KEY(bot,chat,message));
    """)


def state(db, key):
    db.execute("INSERT OR IGNORE INTO panels(bot) VALUES(?)", (key,))
    return db.execute("SELECT * FROM panels WHERE bot=?", (key,)).fetchone()


def buttons(view, draft=""):
    if view == "count":
        return [[(str(n), f"count:pick{n}") for n in group] for group in ((1,5,10),(20,30,50))] + [[("رجوع","home")]]
    if view == "dashboard":
        return [[("تحديث", "/live"), ("بيع حقيقي الآن", "/exit")], [("رجوع", "home")]]
    if view=="sweep_review":
        return [[("تأكيد التجميع","collect:"+draft),("إلغاء","home")]]
    if view.startswith("withdraw_"):
        rows = []
        if view == "withdraw_amount":
            rows.append([("سحب كامل الرصيد بعد الرسم","wd_all")])
        elif view == "withdraw_review":
            rows.append([("تأكيد السحب","wd_ok:"+draft)])
        return rows + [[("إلغاء","home")]]
    if view not in FIELDS:
        return PAPER if view == "paper" else HOME
    rows = [[(d, view + ":" + d) for d in digits] for digits in ("123", "456", "789", ".0")]
    if view == "budget":
        rows.insert(0, [(f"{pct}%", f"budget:pct{pct}") for pct in (50, 80, 100)])
    rows[-1].append(("⌫", view + ":del"))
    return rows + [[("حفظ", view + ":save"), ("رجوع", "home")]]


def render(db, key, note):
    current = state(db, key)
    cfg = db.execute("SELECT budget_bps,target,wallet_count FROM live_settings WHERE bot=?", (key,)).fetchone()
    budget = f"{Decimal(cfg[0]) / 100}% من رصيد الرئيسية" if cfg and cfg[0] else "غير محددة"
    target = f"+{Decimal(cfg[1])/100}%" if cfg and cfg[1] else "غير محدد"
    view = current["view"]
    title = "Coin · المحاكاة" if view == "paper" else "Coin · التداول الحقيقي"
    text = f"{title}\n\nالميزانية: {budget}\nمحافظ الشراء: {cfg[2] if cfg else 50} من 50\nهدف الربح للبيع: {target}"
    if view == "dashboard":
        row = db.execute("SELECT * FROM campaigns WHERE bot=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        text = dashboard.summary(db, row, live.STAGES)
    if view in FIELDS:
        text += f"\n\n{FIELDS[view]}: {current['draft'] or '…'}"
    if view == "withdraw_address":
        text += "\n\nابعت عنوان محفظتك الرئيسية على شبكة Solana."
    if view == "withdraw_amount":
        text += "\n\nالوجهة:\n" + current["draft"] + "\nابعت مبلغ السحب بـ SOL أو اختار كامل الرصيد."
    if view == "sweep_review":
        quote = db.execute("SELECT * FROM sweeps WHERE bot=? AND id=?", (key,current["draft"])).fetchone()
        if quote:
            text += "\n\n" + sweep.summary(quote)
    if view == "withdraw_review":
        quote = db.execute("SELECT * FROM withdrawals WHERE id=? AND bot=?", (current["draft"],key)).fetchone()
        if quote:
            text += "\n\n" + withdrawals.preview(quote)
    if note:
        text += "\n\n" + note
    keyboard = [[{"text":label, "callback_data":value} for label,value in row] for row in buttons(view,current["draft"])]
    return {"text":text[:4096], "reply_markup":{"inline_keyboard":keyboard}}


def reset_input(db,key):
    db.execute("UPDATE live_settings SET prompt=NULL WHERE bot=?", (key,))
    db.execute("UPDATE telegram SET stage=NULL,draft=NULL WHERE id=?", (key,))


def home(db,key,view="home"):
    state(db,key)
    db.execute("DELETE FROM sweeps WHERE bot=? AND state='review'", (key,))
    db.execute("DELETE FROM withdrawals WHERE bot=? AND state='review'", (key,))
    db.execute("UPDATE panels SET view=?,draft='' WHERE bot=?", (view,key))
    reset_input(db,key)


def save(db,key,view,value):
    if live.active(db,key):
        raise ValueError("في صفقة حقيقية قيد التنفيذ.")
    if view == "count":
        value = value.strip()
        if not value.isdecimal() or len(value) > 2 or not 1 <= int(value) <= 50:
            raise ValueError("أدخل عدد صحيح من 1 إلى 50.")
        db.execute("UPDATE live_settings SET wallet_count=? WHERE bot=?", (int(value),key))
        home(db,key)
        return f"انحفظ العدد: {int(value)} محفظة."
    value = value.strip().replace("٫",".").replace(",",".").rstrip("٪%")
    if view == "budget":
        number = live.budget_percent(value)
        db.execute("UPDATE live_settings SET budget=NULL,budget_bps=? WHERE bot=?", (number,key))
    else:
        number = Decimal(value) * 100
        if not number.is_finite() or not 1 <= number <= 1000000 or number != int(number):
            raise ValueError("أدخل نسبة بين 0.01 و10000 بدقّة منزلتين.")
        db.execute("UPDATE live_settings SET target=? WHERE bot=?", (int(number),key))
    home(db,key)
    return "انحفظت القيمة."


def handle(db,key,action,callback):
    current = state(db,key)
    view = current["view"]
    if callback and action not in {v for row in buttons(view,current["draft"]) for _,v in row}:
        return "اللوحة تغيّرت. اختار من الأزرار الظاهرة."
    if callback and action == "find_launch":
        home(db,key,"paper")
        db.execute("UPDATE panels SET draft='find_launch' WHERE bot=?", (key,))
        return "عم افحص أحدث الإطلاقات على الشبكة."
    if callback and view == "paper" and current["draft"] == "find_launch":
        db.execute("UPDATE panels SET draft='' WHERE bot=?", (key,))
    if action == "/live":
        home(db,key,"dashboard")
        return ""
    if action in ("/start","/help") or (callback and action in ("home","paper")):
        home(db,key,"paper" if action=="paper" else "home")
        return ""
    if callback and action == "sweep":
        if sweep.running(db) or live.active(db,key) or withdrawals.pending(db):
            return "في عملية قيد التنفيذ. التجميع اليدوي متاح بعد انتهائها."
        home(db,key)
        ident,note = sweep.preview(db,key)
        if ident:
            db.execute("UPDATE panels SET view='sweep_review',draft=? WHERE bot=?", (ident,key))
        return note
    if view=="sweep_review":
        if callback and action=="collect:"+current["draft"]:
            result = sweep.confirm(db,key,current["draft"])
            home(db,key)
            return result
        return "راجع وجهة التجميع واختار تأكيد أو إلغاء."
    if callback and action == "withdraw":
        if sweep.running(db):
            return "تجميع SOL قيد التنفيذ."
        pending = withdrawals.pending(db)
        if pending:
            return "في سحب بانتظار التأكيد. التوقيع:\n" + pending["signature"]
        if live.active(db,key):
            return "رصيد المحفظة مخصص لصفقة حقيقية قيد التنفيذ."
        if not db.execute("SELECT 1 FROM live_settings WHERE bot=?", (key,)).fetchone():
            return "افتح «المحفظة» أول مرة."
        home(db,key,"withdraw_address")
        return ""
    if view.startswith("withdraw_"):
        if not callback and action.startswith("/"):
            home(db,key)
            return None
        if view == "withdraw_address" and not callback:
            wallet = db.execute("SELECT wallet FROM live_settings WHERE bot=?", (key,)).fetchone()[0]
            address = withdrawals.destination(action,wallet)
            db.execute("UPDATE panels SET view='withdraw_amount',draft=? WHERE bot=?",(address,key))
            return ""
        if view == "withdraw_amount":
            identifier = withdrawals.quote(db,key,current["draft"],"all" if callback else action)
            db.execute("UPDATE panels SET view='withdraw_review',draft=? WHERE bot=?",(identifier,key))
            return ""
        if view == "withdraw_review" and callback and action == "wd_ok:"+current["draft"]:
            result = withdrawals.confirm(db,key,current["draft"])
            home(db,key)
            return result
        return "راجع المعاينة واختار تأكيد أو إلغاء."
    if callback and action in FIELDS:
        if live.active(db,key):
            return "في صفقة حقيقية قيد التنفيذ."
        if not db.execute("SELECT 1 FROM live_settings WHERE bot=?", (key,)).fetchone():
            return "افتح «المحفظة» أول مرة."
        home(db,key,action)
        if action == "count":
            return "اختار العدد أو ابعته برسالة من 1 إلى 50."
        return "اختار الأرقام واضغط حفظ."
    if view in FIELDS:
        if callback:
            op = action.split(":",1)[1]
            draft = current["draft"]
            if view == "count" and op.startswith("pick"):
                return save(db,key,view,op[4:])
            if op.startswith("pct"):
                return save(db,key,view,op[3:])
            if op=="save":
                if not draft or draft==".":
                    return "أدخل القيمة أولاً."
                return save(db,key,view,draft)
            if op=="del":
                draft = draft[:-1]
            elif len(draft)<20 and (op!="." or "." not in draft):
                draft += op
            db.execute("UPDATE panels SET draft=? WHERE bot=?", (draft,key))
            return ""
        if not action.startswith("/"):
            return save(db,key,view,action)
        home(db,key)
    if callback:
        reset_input(db,key)
    return None
