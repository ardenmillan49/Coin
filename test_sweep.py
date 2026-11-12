import sys
import unittest
from unittest.mock import MagicMock, patch
import bot
import telegram_bot as tg
import sweep


class SweepTests(unittest.TestCase):
    def test_preview_confirm_and_confirmed_receipt(self):
        db = bot.open_db(":memory:")
        tg.init_chat(db)
        db.execute("INSERT INTO telegram(id,owner) VALUES(1,42)")
        db.execute("INSERT INTO live_settings(bot,wallet) VALUES(1,'parent')")
        chain = MagicMock()
        parent = MagicMock()
        parent.pubkey.return_value = "parent"
        chain.keys.return_value = (parent,[MagicMock() for _ in range(50)])
        chain.SYS = "system"
        chain.FINAL = {"commitment":"finalized"}
        chain.message_fee.return_value = 5000
        accounts = [None]*49+[{"owner":"system","lamports":100000,"executable":False}]
        with patch.dict(sys.modules,solana_live=chain), patch.object(sweep,"build",return_value=("sig","raw",90)):
            with patch.object(bot,"rpc",return_value={"value":[None]*50}):
                self.assertIsNone(sweep.preview(db,1)[0])
            with patch.object(bot,"rpc",return_value={"value":accounts}):
                ident,_ = sweep.preview(db,1)
            self.assertIsNone(sweep.running(db))
            sweep.confirm(db,1,ident)
            self.assertIn("قيد التنفيذ",sweep.confirm(db,1,ident))
            with patch.object(bot,"rpc",return_value={"value":{"err":None}}):
                sweep.tick(db,1)
            receipt = db.execute("SELECT * FROM withdrawals").fetchone()
            self.assertEqual((receipt["destination"],receipt["amount"]),("parent",95000))
            sweep.tick(db,1)
            self.assertEqual(sweep.running(db)["cursor"],0)
            db.execute("UPDATE withdrawals SET state='done'")
            sweep.tick(db,1)
            sweep.tick(db,1)
            self.assertIsNone(sweep.running(db))
            self.assertEqual(db.execute("SELECT total FROM sweeps").fetchone()[0],95000)
        db.close()
