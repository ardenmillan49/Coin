import unittest
import bot
import live
import telegram_bot as tg
import wallet_trades as trades


class ChildSettlementTests(unittest.TestCase):
    def test_funding_buy_and_sell_receipts(self):
        db = bot.open_db(":memory:")
        tg.init_chat(db)
        db.execute("INSERT INTO telegram(id,owner) VALUES(1,2)")
        db.execute("""INSERT INTO campaigns(bot,wallet,mint,budget,target,stage,pending)
                      VALUES(1,'payer','mint',2000000000,10000,'fund50','fund')""")
        db.execute("INSERT INTO child_plans(campaign,allocation,order_size,next_cursor) VALUES(1,39960000,30000000,50)")
        db.executemany("INSERT INTO child_trades(campaign,idx,owner) VALUES(1,?,?)",[(i,str(i)) for i in range(50)])
        def settle(pre,post,tokens_pre,tokens_post,fee):
            def balances(amounts):
                return [{"mint":"mint","owner":str(i+48),"uiTokenAmount":{"amount":str(n)}} for i,n in enumerate(amounts)]
            tx = {"transaction":{"message":{"accountKeys":[{"pubkey":k} for k in ("payer","48","49")]}},
                  "meta":{"err":None,"fee":fee,"preBalances":pre,"postBalances":post,
                          "preTokenBalances":balances(tokens_pre),"postTokenBalances":balances(tokens_post)}}
            with db:
                live.settle_transaction(db,live.active(db,1),tx)
        settle([1000,0,0],[795,100,100],[],[],5)
        self.assertEqual(live.active(db,1)["spent"],5)
        self.assertEqual(live.active(db,1)["stage"],"buy50")
        db.execute("UPDATE campaigns SET cursor=48,pending='buy'")
        settle([795,100,100],[780,30,30],[],[101,98],15)
        self.assertEqual(live.active(db,1)["stage"],"hold50")
        self.assertEqual([r[0] for r in db.execute("SELECT units FROM child_trades WHERE idx>=48 ORDER BY idx")],[101,98])
        db.execute("UPDATE campaigns SET stage='sell50',cursor=48,pending='sell'")
        settle([780,30,30],[765,230,220],[101,98],[],15)
        self.assertIsNone(live.active(db,1))
        self.assertEqual(db.execute("SELECT SUM(units) FROM child_trades").fetchone()[0],0)
        self.assertEqual(db.execute("SELECT spent FROM campaigns").fetchone()[0],-215)
        db.close()


class ChildTransactionTests(unittest.TestCase):
    def test_actual_group_builders_use_the_child_traders(self):
        from unittest.mock import patch
        import solana_live as chain
        from solders.compute_budget import set_compute_unit_limit
        from solders.transaction import Transaction
        import base64
        payer, a, b = (chain.Keypair() for _ in range(3))
        mint,curve = chain.Keypair().pubkey(),chain.Keypair().pubkey()
        venue = ("curve",curve,10**15,30*10**9,8*10**14)
        for buy in (True,False):
            ix = [set_compute_unit_limit(600000)]
            for child in (a,b):
                owner = child.pubkey()
                trade = chain.trade(owner,mint,chain.TOKEN22,venue,buy,10000,1)
                self.assertEqual(trade.accounts[8].pubkey,owner)
                self.assertTrue(trade.accounts[8].is_signer)
                self.assertEqual(trade.accounts[9].pubkey,chain.ata(owner,mint,chain.TOKEN22))
                if buy:
                    ix.append(chain.create_ata(owner,owner,mint,chain.TOKEN22))
                ix.append(trade)
                if not buy:
                    ix.append(chain.close_ata(owner,owner,mint,chain.TOKEN22))
            with patch.object(bot,"rpc",return_value={"value":{"blockhash":str(chain.Hash.default()),"lastValidBlockHeight":1}}):
                _,raw,_ = chain.prepare(ix,[payer,a,b])
            tx = Transaction.from_bytes(base64.b64decode(raw))
            tx.verify()
            self.assertEqual(len(tx.signatures),3)
