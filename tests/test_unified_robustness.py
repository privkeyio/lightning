"""Wallet signing must reject foreign sighashes without taking the node down."""
from fixtures import *  # noqa: F401,F403
from utils import TEST_NETWORK
from psbt_patch import set_input0_sighash, set_input0_nonwitness_utxo
import pytest

pytestmark = pytest.mark.skipif(TEST_NETWORK != 'regtest', reason='Blake2b regtest only')


# SIGHASH_ALL is what every existing PSBT tool writes; the rest are legal too.
@pytest.mark.parametrize("sighash", [0x01, 0x02, 0x03, 0x81, 0x83, 0x21 | 0x80])
def test_signpsbt_foreign_sighash_is_rejected_not_fatal(node_factory, bitcoind, sighash):
    l1 = node_factory.get_node(broken_log='.*')
    a = l1.rpc.newaddr()
    bitcoind.rpc.sendtoaddress(a.get('bech32') or list(a.values())[0], 0.01)
    bitcoind.generate_block(1)
    l1.daemon.wait_for_log('Owning output')

    funded = l1.rpc.fundpsbt(satoshi=100000, feerate='253perkw', startweight=250)
    tampered = set_input0_sighash(funded['psbt'], sighash)

    with pytest.raises(Exception):
        l1.rpc.signpsbt(tampered)

    # The node must still be answering RPC, and still able to sign normally.
    assert l1.rpc.getinfo()['id'] is not None
    ok = l1.rpc.signpsbt(funded['psbt'])
    assert ok['signed_psbt'] != funded['psbt']


def test_signpsbt_nonwitness_utxo_only_is_not_fatal(node_factory, bitcoind):
    """BIP174 lets an input carry only a non_witness_utxo.  We need the
    witness_utxo to authenticate the prevout, so lightningd must fill it in
    rather than hand the signer a request it can only answer by dying."""
    l1 = node_factory.get_node()
    a = l1.rpc.newaddr()
    bitcoind.rpc.sendtoaddress(a.get('bech32') or list(a.values())[0], 0.01)
    bitcoind.generate_block(1)
    l1.daemon.wait_for_log('Owning output')

    funded = l1.rpc.fundpsbt(satoshi=100000, feerate='253perkw', startweight=250)
    tx = bitcoind.rpc.decodepsbt(funded['psbt'])['tx']
    vin = [{'txid': v['txid'], 'vout': v['vout']} for v in tx['vin']]
    vout = [{bitcoind.rpc.getnewaddress(): 0.0001}]
    # converttopsbt leaves every input map empty, so the only prevout record
    # is the one we add.
    bare = bitcoind.rpc.converttopsbt(bitcoind.rpc.createrawtransaction(vin, vout))
    prev = bitcoind.rpc.getrawtransaction(vin[0]['txid'])
    patched = set_input0_nonwitness_utxo(bare, prev)

    inp = bitcoind.rpc.decodepsbt(patched)['inputs'][0]
    assert 'non_witness_utxo' in inp and 'witness_utxo' not in inp

    l1.rpc.signpsbt(patched)
    assert l1.rpc.getinfo()['id'] is not None
