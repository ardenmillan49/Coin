import base64
import unittest
from unittest.mock import patch

import bot
import telegram_bot as tg
import withdrawals as wd


class WithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.db = bot.open_db(":memory:")
        tg.init_chat(self.db)
        with self.db:
            self.db.execute("INSERT INTO telegram(id,owner) VALUES(1,42)")
            self.db.execute("INSERT INTO live_settings(bot,wallet) VALUES(1,'master')")
            self.db.execute("INSERT INTO panels(bot,message,migrated) VALUES(1,70,1)")

    def tearDown(self):
        self.db.close()

    def send(self, number, text, callback=False, sender=42):
        msg = {"chat":{"id":sender,"type":"private"},"message_id":70}
        if callback:
            update = {"callback_query":{"from":{"id":sender},"message":msg,"data":text}}
        else:
            update = {"message":dict(msg,text=text,**{"from":{"id":sender}})}
        tg.receive(self.db,1,"secret",dict(update,update_id=number))

    def review(self):
        self.send(1,"withdraw",True)
        with patch.object(wd,"destination",return_value="dest"):
            self.send(2,"dest")
        def quote(db, key, address, value):
            self.assertEqual((address,value),("dest","all"))
            db.execute("""INSERT INTO withdrawals VALUES
                       ('nonce',1,'dest',95000,5000,'sig','signed',90,'review',0)""")
            return "nonce"
        with patch.object(wd,"quote",side_effect=quote):
            self.send(3,"wd_all",True)

    def test_private_review_cancel_and_replay(self):
        self.review()
        self.send(4,"wd_ok:nonce",True,sender=7)
        self.send(5,"wd_ok:old",True)
        self.assertIsNone(wd.pending(self.db))
        self.send(6,"home",True)
        self.send(7,"wd_ok:nonce",True)
        self.assertIsNone(wd.pending(self.db))
        self.assertFalse(self.db.execute("SELECT 1 FROM withdrawals").fetchone())

    def test_confirm_restart_and_network_receipt(self):
        self.review()
        with patch.object(bot,"rpc",return_value=42):
            self.send(4,"wd_ok:nonce",True)
            self.send(5,"wd_ok:nonce",True)
        self.assertEqual(wd.pending(self.db)["signature"],"sig")
        with patch.object(bot,"rpc",side_effect=[{"value":[None]},45,"sig"]*2) as rpc:
            wd.tick(self.db,1)
            wd.tick(self.db,1)
            self.assertEqual([x.args[1][0] for x in rpc.call_args_list if x.args[0]=="sendTransaction"],["signed","signed"])
        with patch.object(bot,"rpc",return_value={"value":[{"confirmationStatus":"finalized","err":None}]}):
            wd.tick(self.db,1)
            wd.tick(self.db,1)
        self.assertIsNone(wd.pending(self.db))
        self.assertEqual(self.db.execute("SELECT count(*) FROM replies WHERE text LIKE '%solscan.io%'").fetchone()[0],1)

    def test_expired_unknown_never_resigns(self):
        self.review()
        self.db.execute("UPDATE withdrawals SET state='sending',attempted=1")
        with patch.object(bot,"rpc",side_effect=[{"value":[None]},100,None]*2) as rpc:
            wd.tick(self.db,1)
            wd.tick(self.db,1)
            self.assertTrue(all(call.args[0]!="sendTransaction" for call in rpc.call_args_list))
        self.assertEqual(wd.pending(self.db)["state"],"unknown")

    def test_native_sweep_quotes_fee_and_exact_destination(self):
        import solana_live as chain
        from solders.transaction import Transaction
        master, recipient = chain.Keypair(),chain.Keypair().pubkey()
        self.db.execute("UPDATE live_settings SET wallet=?", (str(master.pubkey()),))
        answers = {"getGenesisHash":"5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
                   "getBalance":{"value":1000000}, "getFeeForMessage":{"value":5000},
                   "getLatestBlockhash":{"value":{"blockhash":str(chain.Hash.default()),"lastValidBlockHeight":10}},
                   "simulateTransaction":{"value":{"err":None}}}
        with patch.object(chain,"keys",return_value=(master,[])), patch.object(bot,"rpc",side_effect=lambda m,p:answers[m]):
            identifier = wd.quote(self.db,1,str(recipient),"all")
        row = self.db.execute("SELECT * FROM withdrawals WHERE id=?",(identifier,)).fetchone()
        self.assertEqual((row["amount"],row["fee"]),(995000,5000))
        tx = Transaction.from_bytes(base64.b64decode(row["raw"]))
        tx.verify()
        ix = tx.message.instructions[0]
        self.assertEqual(tx.message.account_keys[ix.accounts[1]],recipient)
        self.assertEqual(int.from_bytes(ix.data[4:],"little"),row["amount"])
