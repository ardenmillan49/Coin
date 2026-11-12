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
            send(3, "/start secret")
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
