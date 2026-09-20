"""Losing the database must still leave recovered funds spendable."""
from fixtures import *  # noqa: F401,F403
from utils import only_one, wait_for, sync_blockheight, TEST_NETWORK
import os
import pytest

pytestmark = [
    pytest.mark.skipif(TEST_NETWORK != 'regtest', reason='Blake2b regtest only'),
    pytest.mark.skipif(os.getenv('TEST_DB_PROVIDER', 'sqlite3') != 'sqlite3',
                       reason='deletes database, which is assumed sqlite3'),
]


def unified_witness_count(bitcoind, txid):
    tx = bitcoind.rpc.getrawtransaction(txid, True)
    sigs = []
    for vin in tx['vin']:
        for item in vin.get('txinwitness', []):
            raw = bytes.fromhex(item)
            if len(raw) == 65 or (69 <= len(raw) <= 73 and raw[0] == 0x30):
                sigs.append(raw)
    assert sigs, 'no signatures found'
    assert all(s[-1] & 0x20 for s in sigs), 'a signature lacked SIGHASH_UNIFIED'
    return len(sigs)


def test_unified_emergencyrecover(node_factory, bitcoind):
    broken = ('ERROR: Unknown commitment #.*, recovering our funds'
              '|plugin-bookkeeper: Cannot find the open_event for ')
    l1, l2 = node_factory.get_nodes(2, opts=[{'may_reconnect': True, 'broken_log': broken},
                                             {'may_reconnect': True, 'broken_log': '.*'}])
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    c12, _ = l1.fundchannel(l2)
    assert 'unified_sigs/even' in only_one(l1.rpc.listpeerchannels()['channels'])['channel_type']['names']

    l1.stop()
    os.unlink(os.path.join(l1.daemon.lightning_dir, TEST_NETWORK, "lightningd.sqlite3"))
    l1.start()
    assert l1.daemon.is_in_log('Server started with public key')

    stubs = l1.rpc.emergencyrecover()["stubs"]
    assert len(stubs) == 1

    l1.daemon.wait_for_log('Sending a bogus channel_reestablish message')
    l2.daemon.wait_for_log('State changed from CHANNELD_NORMAL to AWAITING_UNILATERAL')

    bitcoind.generate_block(5, wait_for_mempool=1)
    sync_blockheight(bitcoind, [l1, l2])
    l1.daemon.wait_for_log(r'All outputs resolved.*')
    wait_for(lambda: l1.rpc.listfunds()["channels"][0]["state"] == "ONCHAIN")

    # The recovered funds must actually be spendable, with unified signing.
    withdraw = l1.rpc.withdraw(l2.rpc.newaddr('bech32')['bech32'], 'all')
    bitcoind.generate_block(1, wait_for_mempool=withdraw['txid'])
    unified_witness_count(bitcoind, withdraw['txid'])
    assert bitcoind.rpc.getrawtransaction(withdraw['txid'], True)['confirmations'] >= 1
