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
        def rpc(method, args):
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
        with patch.object(launches.bot,"rpc",return_value=[dict(err=None,blockTime=1)]):
            self.assertIn("ما لقيت",launches.find())
