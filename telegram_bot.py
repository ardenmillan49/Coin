"""Private Telegram interface for the paper trader."""
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import select
import sys
import threading
import logging
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

import bot
import live

MENU = [["مراقبة عملة", "فحص عملة"], ["حالة الصفقة", "بيع تجريبي"], ["إلغاء المراقبة"]]
ACTIONS = dict(zip(sum(MENU, []), ["/watch", "/inspect", "/status", "/sell", "/cancel"]))
MENU += [["محفظة SOL", "/ready"], ["إعداد شراء حقيقي"], ["شراء حقيقي", "حالة التداول الحقيقي"], ["بيع حقيقي الآن", "إلغاء طلب الشراء"]]
HELP = "Coin للتداول الحقيقي والمحاكاة. اختار من الأزرار أو اكتب /watch أو /inspect أو /status أو /sell أو /cancel."


class TelegramError(Exception):
    def __init__(self, code, retry=5):
        super().__init__(f"Telegram API ({code})")
        self.code = code
        self.retry = retry


def api(token, method, **params):
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                     data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            payload = json.load(error)
        except ValueError:
            raise TelegramError(error.code) from None
    except (OSError, ValueError):
        raise TelegramError(0) from None
    if not payload.get("ok"):
        raise TelegramError(payload.get("error_code", 0), payload.get("parameters", {}).get("retry_after", 5))
    return payload["result"]


def setup(path):
    if path.exists():
        return json.loads(path.read_text())
    value = getpass.getpass("عنوان البوت (@name أو رابط t.me) أو توكن BotFather: ").strip()
    username = None
    if not re.match(r"^[0-9]+:", value):
        username = value.removeprefix("https://").removeprefix("t.me/").strip("@/ ")
        value = getpass.getpass("توكن البوت من BotFather: ").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", value):
        raise ValueError("صيغة توكن BotFather غير صحيحة")
    identity = api(value, "getMe")
    if username and username.lower() != identity["username"].lower():
        raise ValueError("عنوان البوت ما بيطابق التوكن")
    config = {"username": identity["username"], "token": value, "id": identity["id"]}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        json.dump(config, out)
    os.replace(out.name, path)
    return config


def init_chat(db):
    live.init(db)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS telegram (
          id INTEGER PRIMARY KEY, owner INTEGER, offset INTEGER DEFAULT 0,
          stage TEXT, draft TEXT, mint TEXT, budget INTEGER);
        CREATE TABLE IF NOT EXISTS pairing_requests (
          bot_id INTEGER, chat INTEGER, PRIMARY KEY(bot_id, chat));
        CREATE TABLE IF NOT EXISTS replies (
          id INTEGER PRIMARY KEY, bot_id INTEGER, chat INTEGER, text TEXT);
    """)


def tell(db, key, chat, text):
    db.execute("INSERT INTO replies(bot_id,chat,text) VALUES(?,?,?)", (key, chat, text))


def open_position(db):
    return db.execute("SELECT * FROM positions WHERE proceeds IS NULL").fetchone()


def parse_mint(value):
    from solders.pubkey import Pubkey
    try:
        return str(Pubkey.from_string(value))
    except ValueError:
        raise ValueError("عنوان العملة مو صالح") from None


def report_text(report):
    creator = report["creator_pct"]
    return (f"حصة المنشئ: {creator:.2f}%\n" if creator is not None else "حصة المنشئ مجهولة\n") + (
        f"أكبر حساب خارج المنحنى: {report['largest_external_account_pct']:.2f}%\n"
        f"الاحتياطي: {report['real_sol_reserve']:.6f} SOL\n"
        f"سعر العملة: {report['spot_sol_per_token']:.10g} SOL")


def sol(units):
    return format(Decimal(units) / bot.LAMPORTS, "f")


def command(db, row, text):
    key = row["id"]
    result = live.command(db, key, text)
    if result is not None:
        db.execute("UPDATE telegram SET stage=NULL,draft=NULL WHERE id=?", (key,))
        return result
    parts = ACTIONS.get(text, text).split()
    action = parts[0].split("@")[0] if parts else ""
    args = parts[1:]
    if action in ("/start", "/help"):
        return HELP
    if action == "/cancel":
        db.execute("UPDATE telegram SET stage=NULL,draft=NULL,mint=NULL,budget=NULL WHERE id=?", (key,))
        return "انلغت المراقبة وأي إعداد قيد الإدخال. فيك تشوف صفقتك بـ /status."
    if action == "/status":
        pos = db.execute("SELECT * FROM positions ORDER BY id DESC LIMIT 1").fetchone()
        lines = [f"عم راقب {row['mint']} بميزانية {sol(row['budget'])} SOL"] if row["mint"] else []
        if pos:
            if pos["proceeds"] is None:
                lines.append(f"صفقة تجريبية مفتوحة {pos['mint']}\nتكلفة الدخول {sol(pos['cost'])} SOL")
            else:
                lines.append(f"آخر صفقة مسكّرة. الصافي {sol(pos['proceeds']-pos['cost'])} SOL")
        return "\n".join(lines) or "ما في مراقبة أو صفقة حالياً."
    if action == "/sell":
        pos = open_position(db)
        if not pos:
            return "ما عندك صفقة مفتوحة."
        curve = bot.snapshot(pos["mint"])
        if not curve:
            return "المنحنى غير متاح. الصفقة بعدها مفتوحة."
        proceeds = bot.exit_quote(curve.reserves, pos["units"], pos["net"], pos["tx_fee"], pos["bps"])
        db.execute("UPDATE positions SET proceeds=? WHERE id=? AND proceeds IS NULL", (proceeds, pos["id"]))
        return f"تم البيع التجريبي. حصيلة البيع {sol(proceeds)} SOL، الربح أو الخسارة {sol(proceeds-pos['cost'])} SOL"
    if action.startswith("/") and action not in ("/watch", "/inspect"):
        return HELP
    if action in ("/watch", "/inspect"):
        stage = action[1:]
        if stage == "watch" and (open_position(db) or row["mint"]):
            return "عندك مراقبة أو صفقة مفتوحة. شوف /status."
        db.execute("UPDATE telegram SET stage=?,draft=NULL WHERE id=?", (stage, key))
    else:
        stage = row["stage"]
        args = text.split()
    if stage in ("inspect", "watch"):
        if not args:
            return "ابعت عنوان العملة على Solana."
        mint = parse_mint(args[0])
        if stage == "inspect":
            curve = bot.snapshot(mint)
            db.execute("UPDATE telegram SET stage=NULL WHERE id=?", (key,))
            return report_text(bot.holder_report(mint, curve)) if curve else "لسا ما ظهر منحنى العملة."
        db.execute("UPDATE telegram SET stage='budget',draft=? WHERE id=?", (mint, key))
        args = args[1:]
        if not args:
            return "قديش ميزانية المحاكاة بالـ SOL؟"
    elif stage == "budget":
        mint = row["draft"]
    else:
        return HELP
    if len(args) != 1:
        raise ValueError("ابعت رقم واحد للميزانية بالـ SOL")
    budget = bot.sol_amount(args[0])
    if budget <= 20000:
        raise ValueError("الميزانية ما بتغطي رسوم المحاكاة")
    if open_position(db) or row["mint"]:
        return "عندك مراقبة أو صفقة مفتوحة."
    db.execute("UPDATE telegram SET stage=NULL,draft=NULL,mint=?,budget=? WHERE id=?", (mint, budget, key))
    return f"بلّشت مراقبة {mint} بميزانية تجريبية {sol(budget)} SOL."


def receive(db, key, secret, update):
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM telegram WHERE id=?", (key,)).fetchone()
        if row["offset"] > update["update_id"]:
            return
        db.execute("UPDATE telegram SET offset=? WHERE id=?", (update["update_id"] + 1, key))
        msg = update.get("message", {})
        chat = msg.get("chat", {})
        sender = msg.get("from", {}).get("id")
        text = msg.get("text", "").strip()
        if not text or chat.get("type") != "private" or sender != chat.get("id"):
            return
        if row["owner"] is None:
            if text.split() == ["/start", secret]:
                db.execute("UPDATE telegram SET owner=? WHERE id=?", (sender, key))
                answer = "تم ربط حسابك. " + HELP
            else:
                db.execute("INSERT OR IGNORE INTO pairing_requests VALUES(?,?)", (key, sender))
                answer = (f"رقم حسابك: {sender}\nاكتب هالرقم بطرفية السيرفر واضغط Enter لربط حسابك. "
                          "فيك كمان تستخدم رابط الربط الخاص.")
        elif row["owner"] == sender:
            try:
                answer = command(db, row, text)
            except (ValueError, InvalidOperation) as exc:
                answer = f"تعذّر التنفيذ: {exc}"
            except OSError:
                answer = "الاتصال بشبكة Solana تعذّر. جرّب الأمر مرة تانية."
        else:
            return
        tell(db, key, sender, answer)


def tick(db, key):
    row = db.execute("SELECT * FROM telegram WHERE id=?", (key,)).fetchone()
    if not row["mint"]:
        return
    try:
        curve = bot.snapshot(row["mint"])
        if not curve:
            return
        report = bot.holder_report(row["mint"], curve)
        units, net, cost = bot.entry(curve.reserves, row["budget"], 10000, 125)
        with db:
            db.execute("INSERT INTO positions(mint,units,net,cost,tx_fee,bps) VALUES(?,?,?,?,10000,125)",
                       (row["mint"], units, net, cost))
            db.execute("UPDATE telegram SET mint=NULL,budget=NULL WHERE id=?", (key,))
            tell(db, key, row["owner"], f"تم الشراء التجريبي بكلفة {sol(cost)} SOL.\n" + report_text(report))
    except OSError:
        return
    except (ValueError, sqlite3.IntegrityError) as exc:
        with db:
            db.execute("UPDATE telegram SET mint=NULL,budget=NULL WHERE id=?", (key,))
            tell(db, key, row["owner"], f"توقفت المراقبة: {exc}")


def run():
    import fcntl
    folder = Path(os.getenv("COIN_CONFIG_DIR", "~/.config/coin")).expanduser()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (folder / "telegram.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("بوت Coin شغّال أصلاً")
        try:
            config = setup(folder / "telegram.json")
        except TelegramError as exc:
            raise SystemExit(f"{exc}. تعذّر التحقق من توكن BotFather. أعد التشغيل بعد التحقق من الاتصال والتوكن.")
        run_polling(config)


def pair_from_terminal(db, key, value):
    value = value.strip()
    if not re.fullmatch(r"[1-9][0-9]{0,16}", value):
        return False
    sender = int(value)
    with db:
        updated = db.execute("""UPDATE telegram SET owner=? WHERE id=? AND owner IS NULL
            AND EXISTS (SELECT 1 FROM pairing_requests WHERE bot_id=? AND chat=?)""",
                             (sender, key, key, sender))
        if updated.rowcount:
            tell(db, key, sender, "تم ربط حسابك. " + HELP)
            return True
    return False


def live_worker(path, key, stop):
    connection = bot.open_db(path)
    try:
        while not stop.is_set():
            try:
                live.tick(connection, key)
            except Exception:
                logging.exception("Live worker tick failed")
            stop.wait(1)
    finally:
        connection.close()


def run_polling(config):
    key, token = config["id"], config["token"]
    path = Path(__file__).resolve().with_name("paper.sqlite")
    db = bot.open_db(path)
    db.execute("PRAGMA journal_mode=WAL")
    init_chat(db)
    with db:
        db.execute("INSERT OR IGNORE INTO telegram(id) VALUES(?)", (key,))
    owner = db.execute("SELECT owner FROM telegram WHERE id=?", (key,)).fetchone()[0]
    secret = secrets.token_urlsafe(24)
    if owner is None:
        print(f"Send /start in Telegram, type your numeric ID here and press Enter.\nأو افتح رابط الربط الخاص واضغط Start:\nhttps://t.me/{config['username']}?start={secret}", flush=True)
    else:
        print(f"Coin شغّال على @{config['username']}", flush=True)
    stop = threading.Event()
    worker = threading.Thread(target=live_worker, args=(path, key, stop), daemon=True)
    worker.start()
    try:
        poll(db, key, token, secret)
    finally:
        stop.set()
        worker.join(timeout=2)
        db.close()


def poll(db, key, token, secret):
    while True:
        try:
            tick(db, key)
            owner = db.execute("SELECT owner FROM telegram WHERE id=?", (key,)).fetchone()[0]
            if owner is None and sys.stdin.isatty() and select.select([sys.stdin], [], [], 0)[0]:
                if pair_from_terminal(db, key, sys.stdin.readline()):
                    print("Telegram account paired.", flush=True)
                    continue
                print("Send /start and enter the numeric ID from the reply.", flush=True)
            for reply in db.execute("SELECT * FROM replies WHERE bot_id=? ORDER BY id", (key,)).fetchall():
                try:
                    params = {"chat_id": reply["chat"], "text": reply["text"]}
                    if reply["chat"] == owner:
                        params["reply_markup"] = {"keyboard": MENU, "resize_keyboard": True}
                    api(token, "sendMessage", **params)
                except TelegramError as exc:
                    if exc.code not in (400, 403):
                        raise
                    print(f"Telegram skipped undeliverable reply ({exc.code}).", flush=True)
                with db:
                    db.execute("DELETE FROM replies WHERE id=?", (reply["id"],))
            row = db.execute("SELECT offset,mint FROM telegram WHERE id=?", (key,)).fetchone()
            for update in api(token, "getUpdates", offset=row["offset"], timeout=2 if row["mint"] or owner is None or live.active(db, key) else 20,
                              allowed_updates=["message"]):
                receive(db, key, secret, update)
        except TelegramError as exc:
            if exc.code in (401, 404, 409):
                raise SystemExit(f"{exc}. تحقّق من التوكن ومن تشغيل نسخة واحدة بوضع polling.")
            time.sleep(max(1, exc.retry))


if __name__ == "__main__":
    run()
