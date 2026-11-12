import contextlib
import io
import sys
import tempfile
import unittest
from unittest.mock import patch
import bot


class PaperTests(unittest.TestCase):
    reserves = (10**15, 30*10**9, 8*10**14, 0)

    curve = bot.Curve(reserves, "curve", "creator", 10)

    def test_round_trip(self):
        budget, tx = 100000000, 10000
        units, net, cost = bot.entry(self.reserves, budget, tx, 125)
        self.assertLessEqual(cost + tx, budget)
        proceeds = bot.exit_quote(self.reserves, units, net, tx, 125)
        self.assertLess(proceeds, cost)
        self.assertGreater(proceeds, net * 0.98)

    def test_migrated(self):
        with self.assertRaisesRegex(ValueError, "اكتمل"):
            bot.decode(bot.TAG + bytes(40) + bytes([1]))

    def test_wait_hold_restart_sell(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()) as output:
            argv = ["bot", "--db", folder + "/state"]
            with patch.object(sys, "argv", argv + ["watch", "mint", "--budget-sol", "0.1"]), patch.object(
                    bot, "snapshot", side_effect=[None, self.curve]), patch.object(bot, "holder_report", return_value={
                        "creator_pct": 5, "largest_external_account_pct": 5}), patch.object(bot.time, "sleep"):
                bot.main()
                with self.assertRaisesRegex(ValueError, "already open"):
                    bot.main()
            with patch.object(sys, "argv", argv + ["sell"]), patch.object(bot, "snapshot", return_value=self.curve):
                bot.main()
                with self.assertRaisesRegex(ValueError, "No open"):
                    bot.main()
            with patch.object(sys, "argv", argv + ["status"]):
                bot.main()
            self.assertIn("profit_lamports", output.getvalue())

    def test_holder_report_excludes_curve(self):
        def account(owner, amount):
            info = {"owner": owner, "mint": "mint", "tokenAmount": {"amount": str(amount)}}
            return {"data": {"parsed": {"info": info}}}
        responses = [
            {"context": {"slot": 11}, "value": [{"address": "a"}, {"address": "b"}]},
            {"value": [account("curve", 900), account("other", 65)]},
            {"value": {"amount": "1000", "decimals": 6}, "context": {"slot": 11}},
            {"value": [{"account": account("creator", 30)}, {"account": account("creator", 20)}]},
        ]
        with patch.object(bot, "rpc", side_effect=responses):
            report = bot.holder_report("mint", self.curve)
        self.assertEqual(report["creator_pct"], 5)
        self.assertEqual(report["largest_external_account_pct"], 6.5)
        bot.check_limits(report, 5, 7)
        with self.assertRaises(ValueError):
            bot.check_limits(report, 4, 7)
        with self.assertRaises(ValueError):
            bot.check_limits(dict(report, creator_pct=None), 5, None)

    def test_rpc_curve_decoding_with_sdk(self):
        from solders.pubkey import Pubkey
        from struct import pack
        from base64 import b64encode
        creator = Pubkey.new_unique()
        data = bot.TAG + pack("<5Q?", *self.reserves, 10**15, False) + bytes(creator)
        reply = {"context": {"slot": 91}, "value": {
            "owner": bot.PROGRAM, "data": [b64encode(data).decode(), "base64"]}}
        mint = Pubkey.new_unique()
        with patch.object(bot, "rpc", return_value=reply) as rpc:
            curve = bot.snapshot(str(mint))
        self.assertEqual(curve.creator, str(creator))
        self.assertEqual(curve.reserves, self.reserves)
        self.assertEqual(curve.slot, 91)
        self.assertEqual(rpc.call_args.args[1][0], curve.address)

    def test_public_holder_fallback(self):
        answer = {"context":{"slot":20},"value":[{"address":"holder"}]}
        with patch.object(bot,"rpc",side_effect=[OSError("restricted"),answer]) as rpc:
            self.assertEqual(bot.largest_accounts("mint",{}),answer)
            self.assertEqual(rpc.call_args.args[0],"getTokenLargestAccounts")
            self.assertIn("endpoint",rpc.call_args.kwargs)

    def test_filtered_account_scan(self):
        info = {"mint":"mint","tokenAmount":{"amount":"80"}}
        account = {"pubkey":"a","account":{"data":{"parsed":{"type":"account","info":info}}}}
        mint = {"value":{"owner":"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}}
        with patch.object(bot,"rpc",side_effect=[OSError(),OSError(),mint,{"context":{"slot":20},"value":[account]}]) as rpc:
            self.assertEqual(bot.largest_accounts("mint",{})["value"],[{"address":"a"}])
            self.assertEqual(rpc.call_args.args[1][1]["filters"][0]["memcmp"]["bytes"],"mint")
