import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
import telegram_bot as tg


class TelegramTests(unittest.TestCase):
    def test_first_run_link_and_saved_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "telegram.json"
            with patch.object(tg.getpass, "getpass", side_effect=["https://t.me/CoinBot", "123:abc"]), patch.object(
                    tg, "api", return_value={"id": 123, "username": "CoinBot"}) as api:
                config = tg.setup(path)
                self.assertEqual(tg.setup(path), config)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                api.assert_called_once_with("123:abc", "getMe")

    def test_pair_watch_restart_and_sell(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper.sqlite"
            db = bot.open_db(path)
            tg.init_chat(db)
            with db:
                db.execute("INSERT INTO telegram(id) VALUES(123)")

            def send(update, text, sender=1, chat_type="private"):
                tg.receive(db, 123, "secret", {"update_id": update, "message": {"text": text,
                           "chat": {"id": sender, "type": chat_type}, "from": {"id": sender}}})

            send(1, "/start", 2)
            hint = db.execute("SELECT text FROM replies WHERE chat=2").fetchone()[0]
            self.assertIn("رابط", hint)
            self.assertNotIn("secret", hint)
            self.assertIsNone(db.execute("SELECT owner FROM telegram").fetchone()[0])
            send(2, "/start secret", 2, "group")
            send(3, "/start\nsecret")
            send(4, "مراقبة عملة", 2)
            self.assertEqual(db.execute("SELECT owner,stage FROM telegram").fetchone()[:], (1, None))
            with patch.object(tg, "parse_mint", return_value="mint"):
                send(5, "مراقبة عملة")
                send(6, "mint")
            send(7, "0.1")
            db.close()
            db = bot.open_db(path)
            curve = bot.Curve((10**15, 30*10**9, 8*10**14, 0), "curve", "creator", 1)
            report = {"creator_pct": 5, "largest_external_account_pct": 5,
                      "real_sol_reserve": 0, "spot_sol_per_token": 0.000001}
            with patch.object(bot, "snapshot", return_value=curve), patch.object(bot, "holder_report", return_value=report):
                tg.tick(db, 123)
                self.assertIsNotNone(tg.open_position(db))
                send(8, "بيع تجريبي", 2)
                self.assertIsNotNone(tg.open_position(db))
                send(9, "بيع تجريبي")
                self.assertIsNone(tg.open_position(db))
                before = db.execute("SELECT COUNT(*) FROM replies").fetchone()[0]
                send(9, "بيع تجريبي")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM replies").fetchone()[0], before)
            db.close()

    def test_terminal_pair_requires_request_and_cannot_replace_owner(self):
        db = bot.open_db(":memory:")
        tg.init_chat(db)
        with db:
            db.execute("INSERT INTO telegram(id) VALUES(123)")
        self.assertFalse(tg.pair_from_terminal(db, 123, "42"))
        tg.receive(db, 123, "secret", {"update_id": 8, "message": {"text": "/start",
                   "chat": {"id": 42, "type": "private"}, "from": {"id": 42}}})
        self.assertIn("42", db.execute("SELECT text FROM replies").fetchone()[0])
        self.assertTrue(tg.pair_from_terminal(db, 123, "42"))
        self.assertFalse(tg.pair_from_terminal(db, 123, "42"))
        self.assertEqual(db.execute("SELECT owner FROM telegram").fetchone()[0], 42)
        db.close()

    def test_inline_fields_and_message_reuse(self):
        db = bot.open_db(":memory:")
        tg.init_chat(db)
        with db:
            db.execute("INSERT INTO telegram(id,owner) VALUES(123,5)")
            db.execute("INSERT INTO live_settings(bot,wallet) VALUES(123,'wallet')")
            db.execute("INSERT INTO panels(bot,message,migrated) VALUES(123,75,1)")
        def press(number, data, sender=5, message=75):
            tg.receive(db,123,"secret",{"update_id":number,"callback_query":{"from":{"id":sender},"data":data,
                       "message":{"message_id":message,"chat":{"id":5,"type":"private"}}}})
        for i, op in enumerate(("budget","budget:0","budget:.","budget:5","budget:save",
                                "target","target:1","target:0","target:0","target:save"),1):
            press(i,op)
        self.assertEqual(tuple(db.execute("SELECT budget_bps,target FROM live_settings").fetchone()),(50,10000))
        self.assertIsNone(tg.live.active(db,123))
        press(11,"/buy",sender=4)
        press(12,"/buy",message=74)
        self.assertIsNone(db.execute("SELECT prompt FROM live_settings").fetchone()[0])
        press(13,"/buy")
        self.assertEqual(db.execute("SELECT prompt FROM live_settings").fetchone()[0],"mint")
        with patch.object(tg,"api",return_value=True) as api:
            tg.show_panel(db,123,"token",{"chat":5,"text":"حدث"})
            self.assertEqual(api.call_args.args,("token","editMessageText"))
            self.assertEqual(api.call_args.kwargs["message_id"],75)
        tg.receive(db,123,"secret",{"update_id":14,"message":{"from":{"id":5},"message_id":76,
                   "text":"/start","chat":{"id":5,"type":"private"}}})
        self.assertEqual(db.execute("SELECT message FROM input_cleanup").fetchone()[0],76)
        self.assertIsNone(db.execute("SELECT prompt FROM live_settings").fetchone()[0])
        db.close()
