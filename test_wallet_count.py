import unittest
from unittest.mock import Mock, patch

import bot
import live
import panel
import solana_live as chain
import telegram_bot
import wallet_trades as trades


class WalletCountTests(unittest.TestCase):
    def setUp(self):
        self.db = bot.open_db(":memory:")
        self.addCleanup(self.db.close)
        telegram_bot.init_chat(self.db)
        self.master = chain.Keypair()
        self.children = [chain.Keypair() for _ in range(50)]
        self.mint = chain.Keypair().pubkey()
        self.db.execute("INSERT INTO live_settings(bot,wallet,budget_bps,target) VALUES(1,?,10000,10000)",
                        (str(self.master.pubkey()),))

    def test_selection(self):
        def saved():
            return self.db.execute("SELECT wallet_count FROM live_settings").fetchone()[0]
        self.assertEqual(saved(),50)
        panel.handle(self.db,1,"count",True)
        panel.handle(self.db,1,"count:pick5",True)
        self.assertEqual(saved(),5)
        for value in ("0","51","5.5","-1","100%"):
            with self.assertRaises(ValueError):
                panel.save(self.db,1,"count",value)
        panel.handle(self.db,1,"count",True)
        panel.handle(self.db,1,"١٧",False)
        self.assertEqual(saved(),17)

    def test_five_wallet_cycle(self):
        panel.save(self.db,1,"count","5")
        live.command(self.db,1,"/buy")
        balance = 60000000
        with patch.object(bot,"rpc",return_value={"value":balance}), patch.object(trades,"overhead",return_value=8000000):
            live.command(self.db,1,str(self.mint))
            row = live.active(self.db,1)
            self.assertEqual(row["wallet_count"],5)
            with self.assertRaises(ValueError):
                panel.save(self.db,1,"count","50")
            venue = ("curve", chain.Keypair().pubkey(), 10**15, 30*10**9, 8*10**14)
            with patch.object(chain,"verify_mainnet"), patch.object(chain,"market",return_value=venue), patch.object(chain,"mint_info",return_value=(chain.TOKEN22,6)), patch.object(bot,"snapshot",return_value=True), patch.object(bot,"holder_report",return_value={"largest_external_account_pct":0}):
                trades.start(self.db,row,chain,self.master,self.children,self.mint,Mock())
        chosen = [str(c.pubkey()) for c in self.children[:5]]
        self.assertEqual([r[0] for r in self.db.execute("SELECT owner FROM child_trades ORDER BY idx")],chosen)
        self.assertEqual(self.db.execute("SELECT allocation FROM child_plans").fetchone()[0],(balance-trades.MASTER_RESERVE)//5)
        for stage,ends in (("fund50",[5]),("buy50",[2,4,5]),("sell50",[2,4,5])):
            if stage=="sell50":
                self.assertEqual(live.active(self.db,1)["stage"],"hold50")
                live.command(self.db,1,"/exit")
                trades.step(self.db,live.active(self.db,1),chain,self.master,self.children,self.mint,Mock())
            for end in ends:
                row=live.active(self.db,1)
                self.assertEqual(row["stage"],stage)
                cursor=row["cursor"]
                with patch.object(trades,"issue") as issue, patch.object(chain,"market",return_value=venue), patch.object(chain,"amount",return_value=100):
                    trades.step(self.db,row,chain,self.master,self.children,self.mint,Mock())
                args=issue.call_args.args
                self.assertEqual(args[-2],end)
                self.assertEqual(args[-1][1:],chosen[cursor:end])
                self.assertEqual(args[-3][1:],[] if stage=="fund50" else self.children[cursor:end])
                self.db.execute("UPDATE child_plans SET next_cursor=?",(end,))
                self.db.execute("UPDATE campaigns SET pending=?",(stage+str(end),))
                tokens=[{"owner":owner,"mint":str(self.mint),"uiTokenAmount":{"amount":"100"}} for owner in chosen[cursor:end]]
                tx={"transaction":{"message":{"accountKeys":[]}},"meta":{"err":None,"fee":5000,
                    "preBalances":[],"postBalances":[],"preTokenBalances":tokens if stage=="sell50" else [],
                    "postTokenBalances":tokens if stage=="buy50" else []}}
                trades.settle(self.db,live.active(self.db,1),tx,Mock(),Mock())
        self.assertIsNone(live.active(self.db,1))
