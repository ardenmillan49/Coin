import time
import unittest
from unittest.mock import patch

import bot
import panel
import telegram_bot as tg


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.db = bot.open_db(":memory:")
        self.addCleanup(self.db.close)
        tg.init_chat(self.db)
        self.db.execute("INSERT INTO telegram(id,owner) VALUES(1,2)")
        panel.handle(self.db,1,"/live",True)

    def screen(self):
        return panel.render(self.db,1,"")["text"]

    def test_empty_dashboard_and_navigation(self):
        self.assertIn("ما في صفقة", self.screen())
        panel.handle(self.db,1,"home",True)
        self.assertEqual(panel.state(self.db,1)["view"], "home")

    def test_partial_sales_and_final_result(self):
        self.db.execute("""INSERT INTO campaigns(bot,mint,budget,target,stage,spent,decimals)
                         VALUES(1,'mint',3000000000,10000,'sell50',600000000,6)""")
        self.db.execute("INSERT INTO child_trades VALUES(1,0,'child',100)")
        self.db.execute("INSERT INTO live_receipts VALUES('b',1,'buy50',0,1000000000,15000,1)")
        self.assertIn("بانتظار تسعير", self.screen())
        self.db.execute("INSERT INTO market_samples VALUES(1,'900','10000000000',?)",(int(time.time())-65,))
        text = self.screen()
        self.assertIn("+38.50%", text)
        self.assertIn("آخر تسعير قديم", text)
        self.db.execute("UPDATE campaigns SET stage='done',spent=-200000000")
        self.db.execute("UPDATE child_trades SET units=0")
        text = self.screen()
        self.assertIn("+20.00%", text)
        self.assertNotIn("التقديرية", text)

    def test_refresh_edits_existing_message(self):
        self.db.execute("UPDATE panels SET message=77,migrated=1 WHERE bot=1")
        with patch.object(tg,"api") as api:
            tg.show_panel(self.db,1,"token",{"text":"","chat":2})
        self.assertEqual(api.call_args.args[1],"editMessageText")
        self.assertEqual(api.call_args.kwargs["message_id"],77)
