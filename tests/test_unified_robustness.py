"""Wallet signing must reject foreign sighashes without taking the node down."""
from fixtures import *  # noqa: F401,F403
from pyln.client import RpcError
from utils import TEST_NETWORK, only_one, wait_for
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

    # A change output: signpsbt refuses a PSBT with none before it looks
    # at the sighash.
    funded = l1.rpc.fundpsbt(satoshi=100000, feerate='253perkw', startweight=250,
                             excess_as_change=True)
    tampered = set_input0_sighash(funded['psbt'], sighash)

    with pytest.raises(RpcError, match=r'wallet inputs must use SIGHASH_ALL\|SIGHASH_UNIFIED'):
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


# Keeps option_blake2b but drops option_unified_sigs, as builds before
# v26.06.7-blake2b.4 did.
NO_UNIFIED = '-514'


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@pytest.mark.parametrize('opener_lacks_it', [False, True])
def test_new_channel_requires_unified_sigs(node_factory, opener_lacks_it):
    """Neither side opens a channel without option_unified_sigs."""
    unified, plain = node_factory.get_nodes(2, opts=[{'allow_warning': True},
                                                     {'dev-force-features': NO_UNIFIED,
                                                      'allow_warning': True}])
    opener, fundee = (plain, unified) if opener_lacks_it else (unified, plain)
    opener.rpc.connect(fundee.info['id'], 'localhost', fundee.port)
    opener.fundwallet(2000000)

    with pytest.raises(RpcError, match='channel_type'):
        opener.rpc.fundchannel(fundee.info['id'], 500000)
    for n in (unified, plain):
        assert not [c for c in n.rpc.listpeerchannels()['channels']
                    if c['state'] in ('CHANNELD_AWAITING_LOCKIN', 'DUALOPEND_AWAITING_LOCKIN', 'CHANNELD_NORMAL')]


def test_no_splice_without_unified_sigs(node_factory, bitcoind, executor):
    """A channel opened without option_unified_sigs is closed and reopened, never spliced."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     opts={'dev-force-features': NO_UNIFIED,
                                           'may_reconnect': True,
                                           'allow_warning': True})
    chan_id = l1.get_channel_id(l2)
    names = only_one(l1.rpc.listpeerchannels()['channels'])['channel_type']['names']
    assert 'unified_sigs/even' not in names

    # Both upgrade.  The channel keeps the type it was opened with.
    for n in (l1, l2):
        del n.daemon.opts['dev-force-features']
        n.restart()
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL')
    assert only_one(l1.rpc.listpeerchannels()['channels'])['channel_type']['names'] == names

    funds = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
    with pytest.raises(RpcError, match='does not use option_unified_sigs'):
        l1.rpc.splice_init(chan_id, 100000, funds['psbt'])

    # The peer refuses it too, when our own check is skipped.
    l1.daemon.opts['dev-splice-without-unified-sigs'] = None
    l1.restart()
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL')
    # The peer warns and hangs up, which leaves our splice_init waiting, so
    # do not wait on it: what matters is that no splice starts.
    executor.submit(l1.rpc.splice_init, chan_id, 100000, funds['psbt'])
    l2.daemon.wait_for_log('Splice refused: this channel does not use option_unified_sigs')
    assert not l2.daemon.is_in_log('peer_out WIRE_SPLICE_ACK')
