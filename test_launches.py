import time
import unittest
from unittest.mock import patch
import launches
from launches import chain


class DiscoveryTests(unittest.TestCase):
    def test_skips_graduated_and_returns_verified_mint(self):
        authority = str(chain.pda(chain.PUMP,b"mint-authority"))
        ids = [str(chain.Keypair().pubkey()) for _ in range(2)]
        sigs = [dict(signature=m, err=None, blockTime=time.time()) for m in ids]
        def rpc(method, args, **kwargs):
            if method == "getSignaturesForAddress":
                return sigs
            self.assertEqual(method,"getTransaction")
            ix = dict(programId=str(chain.TOKEN22), parsed=dict(type="initializeMint2",
                      info=dict(mintAuthority=authority, mint=args[0])))
            return dict(meta=dict(err=None, innerInstructions=[dict(instructions=[ix])]),
                        transaction=dict(message=dict(instructions=[])))
        with patch.object(launches.bot,"rpc",side_effect=rpc), patch.object(chain,"market",side_effect=[
                ("pool",), ("curve",None,100,100,10)]), patch.object(chain,"mint_info",return_value=(chain.TOKEN22,6)):
            result = launches.find()
        self.assertIn(ids[1],result)
        self.assertNotIn(ids[0],result)

    def test_no_recent_launch(self):
        with patch.object(launches,"from_listing",return_value=[]), patch.object(launches.bot,"rpc",return_value=[dict(err=None,blockTime=1)]):
            self.assertIn("ما لقيت",launches.find())

    def test_history_blocked_listing_with_rpc_verification(self):
        address = str(chain.Keypair().pubkey())
        with patch.object(launches.bot,"rpc",side_effect=OSError), patch.object(launches,"from_listing",return_value=[address]), patch.object(chain,"market",return_value=("curve",None,1,1,1)), patch.object(chain,"mint_info",return_value=(chain.SPL,6)):
            self.assertIn(address,launches.find())

    def test_error_reports_method_without_endpoint_secret(self):
        with patch.object(launches.bot,"rpc",side_effect=OSError("private-url")), patch.object(launches,"from_listing",side_effect=ValueError):
            text = launches.find()
            self.assertIn("getSignaturesForAddress",text)
            self.assertNotIn("private-url",text)

    def test_backup_reads_history_and_verifies_curve(self):
        address = str(chain.Keypair().pubkey())
        def history(read):
            read("getSignaturesForAddress", [])
            yield address
        def rpc(method, params, endpoint):
            if endpoint == "primary":
                raise OSError()
            return {}
        def market(mint, *, reader, entry_only):
            reader("getAccountInfo", [])
            return ("curve",None,1,1,1)
        with patch.dict(launches.os.environ, SOLANA_RPC_URL="primary",SOLANA_HOLDERS_RPC_URL="backup"), patch.object(launches,"from_history",side_effect=history), patch.object(launches,"from_listing",return_value=[]), patch.object(launches.bot,"rpc",side_effect=rpc), patch.object(chain,"market",side_effect=market), patch.object(chain,"mint_info",return_value=(chain.SPL,6)):
            self.assertIn(address,launches.find())
