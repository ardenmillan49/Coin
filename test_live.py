import base64
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import bot
import live
import telegram_bot as tg


class LiveStateTests(unittest.TestCase):
    def setUp(self):
        self.db = bot.open_db(":memory:")
        tg.init_chat(self.db)
        self.db.execute("INSERT INTO telegram(id,owner) VALUES(1,7)")
        self.db.execute("INSERT INTO live_settings(bot,wallet) VALUES(1,'payer')")
        self.db.execute("""INSERT INTO campaigns(bot,wallet,mint,budget,target,stage)
                        VALUES(1,'payer','mint',1000000000,10000,'buy')""")
        self.db.commit()

    def meta(self, delta, fee=5000):
        return {"err":None,"fee":fee,"preBalances":[1000000000],"postBalances":[1000000000-delta]}

    def finalized(self, meta, sig):
        with self.db:
            self.db.execute("UPDATE campaigns SET pending=?", (sig,))
            live.receipt(self.db,self.row(),meta)

    def row(self):
        return self.db.execute("SELECT * FROM campaigns").fetchone()

    def test_settle_all_wallets_and_aggregate_profit(self):
        meta = self.meta(100005000)
        meta["postTokenBalances"] = [{"owner":"payer","mint":"mint","uiTokenAmount":{"amount":"100001"}}]
        self.finalized(meta,"buy")
        self.assertEqual(sum(live.quantities(self.row()["units"])),100001)
        for i in range(0,50,4):
            self.finalized(self.meta(min(4,50-i)*2000000+5000),f"out{i}")
        self.assertEqual((self.row()["stage"],self.row()["locked"]),("hold",100000000))
        c = MagicMock()
        c.keys.return_value = (MagicMock(),[])
        c.keys.return_value[0].pubkey.return_value = "payer"
        c.market.return_value = ("curve","pool",100001,10000,0)
        live.step(self.db,self.row(),c)
        self.assertEqual(self.row()["stage"],"hold")
        c.market.return_value = ("curve","pool",100001,600000000,0)
        live.step(self.db,self.row(),c)
        self.assertEqual(self.row()["stage"],"collect")
        for i in range(0,50,4):
            self.finalized(self.meta(-min(4,50-i)*2000000+25000,25000),f"back{i}")
        self.assertEqual(self.row()["stage"],"sell")
        c.market.return_value = ("curve","pool",100001,10000,0)
        live.step(self.db,self.row(),c)
        self.assertFalse(c.trade.called)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM live_receipts").fetchone()[0],27)
        self.finalized(self.meta(-250000000),"exit")
        self.assertEqual(self.row()["stage"],"done")
        self.assertLess(self.row()["spent"],-100000000)

    def test_uncertain_send_reuses_signed_bytes(self):
        c = MagicMock()
        c.FINAL = {"commitment":"finalized"}
        c.message_fee.return_value = 5000
        c.prepare.return_value = ("sig","signed",88)
        def rpc(method, args):
            return {"getBalance":{"value":500000000},"simulateTransaction":{"value":{"err":None,"accounts":[{"lamports":490000000}]}},
                    "getSignatureStatuses":{"value":[None]},"getBlockHeight":50,"sendTransaction":"sig"}[method]
        with patch.object(bot,"rpc",side_effect=rpc) as call:
            live.issue(self.db,self.row(),c,[],[])
            for _ in range(2):
                live.reconcile(self.db,self.row(),c)
            self.assertEqual([x.args[1][0] for x in call.call_args_list if x.args[0]=="sendTransaction"], ["signed","signed"])
            self.assertEqual(c.prepare.call_count,1)

    def test_over_budget_and_expiry(self):
        c = MagicMock()
        c.message_fee.return_value = 5000
        c.prepare.return_value = ("sig","raw",1)
        with patch.object(bot,"rpc",side_effect=[{"value":2000000000},{"value":{"err":None,"accounts":[{"lamports":0}]}}]):
            with self.assertRaisesRegex(ValueError,"الميزانية"):
                live.issue(self.db,self.row(),c,[],[])
        self.assertIsNone(self.row()["pending"])
        self.db.execute("UPDATE campaigns SET pending='expired',expiry=1")
        with patch.object(bot,"rpc",side_effect=[{"value":[None]},2,None]) as call:
            live.reconcile(self.db,self.row(),c)
            self.assertEqual(call.call_count,3)
        self.assertEqual(self.row()["pending"],"expired")
        self.assertTrue(self.row()["error"])

    def test_plan_is_reusable(self):
        self.db.execute("DELETE FROM campaigns")
        live.command(self.db,1,"إعداد شراء حقيقي")
        live.command(self.db,1,"0.75 100")
        settings = self.db.execute("SELECT * FROM live_settings").fetchone()
        self.assertEqual((settings["budget"],settings["target"]),(750000000,10000))
        self.assertIn("عقد",live.command(self.db,1,"شراء حقيقي"))
        live.command(self.db,1,"/unarm")
        self.assertIsNone(self.db.execute("SELECT prompt FROM live_settings").fetchone()[0])


class SignedBuilderTests(unittest.TestCase):
    def test_recovery_and_batch_packet_limits(self):
        import solana_live as c
        from solders.transaction import Transaction
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,COIN_CONFIG_DIR=folder):
            master, children = c.keys(create=True)
            self.assertEqual([str(k.pubkey()) for k in c.keys()[1]], [str(k.pubkey()) for k in children])
            self.assertEqual(len(set(str(k.pubkey()) for k in children)),50)
            mint = children[10].pubkey()
            for collect in (False,True):
                ix = []
                for child in children[:4]:
                    p, u = master.pubkey(),child.pubkey()
                    if collect:
                        ix += [c.transfer(u,p,mint,c.TOKEN22,42,6),c.close_ata(u,p,mint,c.TOKEN22)]
                    else:
                        ix += [c.create_ata(p,u,mint,c.TOKEN22),c.transfer(p,u,mint,c.TOKEN22,42,6)]
                with patch.object(bot,"rpc",return_value={"value":{"blockhash":str(c.Hash.default()),"lastValidBlockHeight":10}}):
                    _, raw, _ = c.prepare(ix,[master,*children[:4]] if collect else [master])
                tx = Transaction.from_bytes(base64.b64decode(raw))
                tx.verify()

    def test_all_child_signers_offline(self):
        import solana_live as chain
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, COIN_CONFIG_DIR=folder):
            master, children = chain.keys(create=True)
            with patch.object(bot, "rpc", side_effect=AssertionError("offline check used RPC")):
                self.assertEqual(chain.check_child_signers(str(master.pubkey()), live.BATCH), 50)
                with self.assertRaises(ValueError):
                    chain.check_child_signers(str(children[0].pubkey()), live.BATCH)
