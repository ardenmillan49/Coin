import io
import unittest
from unittest.mock import Mock, patch

import bot
import live
import panel
import solana_live as chain
import telegram_bot
import wallet_trades


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.db = bot.open_db(":memory:")
        self.addCleanup(self.db.close)
        telegram_bot.init_chat(self.db)
        self.db.execute("INSERT INTO live_settings(bot,wallet,budget,target) VALUES(1,'master',2000000000,10000)")

    def test_legacy_limit_requires_selection_and_presets_save(self):
        self.assertIn("اضبط", live.command(self.db, 1, "/buy"))
        for percent in (50, 80, 100):
            panel.handle(self.db, 1, "budget", True)
            panel.handle(self.db, 1, "budget:pct" + str(percent), True)
            cfg = self.db.execute("SELECT budget_bps,budget FROM live_settings").fetchone()
            self.assertEqual(tuple(cfg), (percent * 100, None))
        for invalid in ("0", "-2", "100.01", "0.001", "Infinity"):
            with self.assertRaises(ValueError):
                live.budget_percent(invalid)
        self.assertEqual(live.budget_percent("٢٥٫٥٪"), 2550)

    def test_limit_on_order_and_before_first_funding(self):
        panel.save(self.db, 1, "budget", "80")
        mint = chain.Keypair().pubkey()
        master = chain.Keypair()
        children = [chain.Keypair() for _ in range(50)]
        for balance, expected in [(1000000001, 800000000), (3000000000, 1600000000)]:
            live.command(self.db, 1, "/buy")
            with patch.object(bot, "rpc", return_value={"value": 2000000001}), patch.object(wallet_trades, "overhead", return_value=10000):
                live.command(self.db, 1, str(mint))
            row = live.active(self.db, 1)
            self.assertEqual(row["budget"], 1600000000)
            venue = Mock(FINAL=chain.FINAL)
            venue.market.return_value = ("curve",)
            venue.mint_info.return_value = (mint, 6)
            with patch.object(bot, "rpc", return_value={"value": balance}), patch.object(bot, "snapshot", return_value=True), patch.object(bot, "holder_report", return_value={"largest_external_account_pct": 0}), patch.object(wallet_trades, "overhead", return_value=10000):
                wallet_trades.start(self.db, row, venue, master, children, mint, Mock())
            self.assertEqual(live.active(self.db, 1)["budget"], expected)
            allocation, order = self.db.execute("SELECT allocation,order_size FROM child_plans WHERE campaign=?", (row["id"],)).fetchone()
            self.assertLessEqual(50 * allocation + wallet_trades.MASTER_RESERVE, expected)
            self.assertEqual(order + 10000, allocation)
            self.db.execute("UPDATE campaigns SET stage='done'")

    def test_wallet_quote_and_outage(self):
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"price":"81.50"}')):
            self.assertIn("40.75 USDT", live.wallet_value(500000000))
        with patch("urllib.request.urlopen", side_effect=OSError):
            self.assertIn("0.5 SOL", live.wallet_value(500000000))
            self.assertIn("0.00 USDT", live.wallet_value(0))
