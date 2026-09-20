"""Run only against isolated Knots regtest with Blake2b active from height 1."""
from fixtures import *  # noqa: F401,F403
from utils import only_one, wait_for, sync_blockheight, mine_funding_to_announce, TEST_NETWORK, TIMEOUT
import queue
import threading
from shutil import copyfile
import os
import pytest

pytestmark = pytest.mark.skipif(TEST_NETWORK != 'regtest', reason='Blake2b consensus tests require Knots regtest')


def assert_unified_witnesses(bitcoind, txid, minimum):
    tx = bitcoind.rpc.getrawtransaction(txid, True)
    signatures = []
    for vin in tx['vin']:
        for item in vin.get('txinwitness', []):
            raw = bytes.fromhex(item)
            if len(raw) == 65 or (69 <= len(raw) <= 73 and raw[0] == 0x30):
                signatures.append(raw)
    assert len(signatures) >= minimum
    # SIGHASH_UNIFIED is added to the hash type that would otherwise apply,
    # so SIGHASH_ALL becomes 0x21, and the anchors HTLC form 0xa3.
    for sig in signatures:
        assert sig[-1] in (0x21, 0xa3), '0x%02x' % sig[-1]
    return tx


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_unified_channel_pay_restart_close_withdraw(node_factory, bitcoind):
    a, b = node_factory.line_graph(2, opts={'may_reconnect': True})
    channel = only_one(a.rpc.listpeerchannels()['channels'])
    assert 'unified_sigs/even' in channel['channel_type']['names']
    assert_unified_witnesses(bitcoind, channel['funding_txid'], 1)
    a.pay(b, 1000000)
    # Reopening the DB must not revert the negotiated signing type.
    a.restart()
    wait_for(lambda: only_one(a.rpc.listpeerchannels()['channels'])['peer_connected'])
    a.pay(b, 1000000)
    a.rpc.close(b.info['id'])
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 1)
    txid = only_one(bitcoind.rpc.getrawmempool())
    assert_unified_witnesses(bitcoind, txid, 2)
    bitcoind.generate_block(6)
    sync_blockheight(bitcoind, [a, b])
    wait_for(lambda: any(o['status'] == 'confirmed' for o in a.rpc.listfunds()['outputs']))
    closed_output = next(o for o in a.rpc.listfunds()['outputs'] if o['txid'] == txid)
    result = a.rpc.withdraw(bitcoind.rpc.getnewaddress(), 10000,
                            utxos=[f"{txid}:{closed_output['output']}"])
    assert_unified_witnesses(bitcoind, result['txid'], 1)
    bitcoind.generate_block(1)
    assert bitcoind.rpc.getrawtransaction(result['txid'], True)['confirmations'] >= 1


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_unified_unilateral_close(node_factory, bitcoind):
    a, b = node_factory.line_graph(2, opts={'may_reconnect': True, 'allow_warning': True})
    funding = only_one(a.rpc.listpeerchannels()['channels'])['funding_txid']
    a.pay(b, 1000000)
    # Close using signatures loaded from disk, without replacing them by paying.
    a.restart()
    wait_for(lambda: only_one(a.rpc.listpeerchannels()['channels'])['peer_connected'])
    assert 'unified_sigs/even' in only_one(a.rpc.listpeerchannels()['channels'])['channel_type']['names']
    b.stop()
    a.rpc.close(b.info['id'], unilateraltimeout=1)
    a.wait_for_channel_onchain(b.info['id'])
    matches = [t for t in bitcoind.rpc.getrawmempool()
               if any(v['txid'] == funding for v in bitcoind.rpc.getrawtransaction(t, True)['vin'])]
    txid = only_one(matches)
    assert_unified_witnesses(bitcoind, txid, 2)
    bitcoind.generate_block(1)
    assert bitcoind.rpc.getrawtransaction(txid, True)['confirmations'] >= 1


@pytest.mark.openchannel('v1')
@pytest.mark.skipif(os.getenv('TEST_DB_PROVIDER', 'sqlite3') != 'sqlite3',
                    reason='copies database, which is assumed sqlite3')
def test_unified_penalty(node_factory, bitcoind, chainparams):
    """A revoked commitment must still be punishable under unified signing."""
    a, b = node_factory.line_graph(2, opts=[{'may_reconnect': True},
                                            {'may_reconnect': True,
                                             'broken_log': '.*'}])
    assert 'unified_sigs/even' in only_one(a.rpc.listpeerchannels()['channels'])['channel_type']['names']
    a.pay(b, 1000000)
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [a, b])

    # Snapshot b at a state it will later be punished for broadcasting.
    b.stop()
    db = os.path.join(b.daemon.lightning_dir, chainparams['name'], 'lightningd.sqlite3')
    copyfile(db, db + '.bak')
    b.start(wait_for_bitcoind_sync=True)
    a.rpc.connect(b.info['id'], 'localhost', b.port)
    wait_for(lambda: only_one(a.rpc.listpeerchannels()['channels'])['peer_connected'])

    # Advance the commitment so the snapshot becomes revoked.
    a.pay(b, 1000000)
    b.stop()
    a.stop()
    copyfile(db + '.bak', db)

    b.start()
    sync_blockheight(bitcoind, [b])
    b.rpc.close(a.info['id'], 1)
    bitcoind.generate_block(1, wait_for_mempool=1)

    a.start()
    sync_blockheight(bitcoind, [a])
    _, txid, _ = a.wait_for_onchaind_tx('OUR_PENALTY_TX',
                                        'THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM')
    # The punishment itself must be signed with the opt-in hash type.
    mined = a.mine_txid_or_rbf(txid, numblocks=1)
    assert_unified_witnesses(bitcoind, mined, 1)
    bitcoind.generate_block(100)
    sync_blockheight(bitcoind, [a])
    wait_for(lambda: a.rpc.listpeerchannels()['channels'] == []
             or only_one(a.rpc.listpeerchannels()['channels'])['state'] == 'ONCHAIN')
    assert a.daemon.is_in_log('All outputs resolved')


@pytest.mark.openchannel('v1')
def test_unified_onchain_htlc_timeout(node_factory, bitcoind, executor):
    """An HTLC left in flight must still resolve on chain under unified signing."""
    disconnects = ['+WIRE_REVOKE_AND_ACK*3', 'permfail']
    a, b = node_factory.line_graph(2,
                                   opts=[{'disconnect': disconnects,
                                          'feerates': (7500, 7500, 7500, 7500),
                                          'broken_log': '.*'},
                                         {'broken_log': '.*'}])
    assert 'unified_sigs/even' in only_one(a.rpc.listpeerchannels()['channels'])['channel_type']['names']

    inv = b.rpc.invoice(10**8, 'unified_timeout', 'desc')
    routestep = {'amount_msat': 10**8 - 1, 'id': b.info['id'], 'delay': 5,
                 'channel': only_one(a.rpc.listpeerchannels()['channels'])['short_channel_id']}
    # Underpaid, so b will never settle it.
    a.rpc.sendpay([routestep], inv['payment_hash'],
                  payment_secret=inv['payment_secret'], groupid=1)
    with pytest.raises(Exception):
        a.rpc.waitsendpay(inv['payment_hash'])
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [a])

    a.rpc.sendpay([routestep], inv['payment_hash'],
                  payment_secret=inv['payment_secret'], groupid=2)
    payfuture = executor.submit(a.rpc.waitsendpay, inv['payment_hash'])

    a.daemon.wait_for_log('permfail')
    a.wait_for_channel_onchain(b.info['id'])
    bitcoind.generate_block(1)
    a.daemon.wait_for_log(' to ONCHAIN')
    b.daemon.wait_for_log(' to ONCHAIN')

    ((_, txid1, _), (_, txid2, _)) = a.wait_for_onchaind_txs(
        ('OUR_DELAYED_RETURN_TO_WALLET', 'OUR_UNILATERAL/DELAYED_OUTPUT_TO_US'),
        ('OUR_HTLC_TIMEOUT_TX', 'OUR_UNILATERAL/OUR_HTLC'))

    bitcoind.generate_block(4)
    bitcoind.generate_block(1, wait_for_mempool=txid1)
    # Sweeping our delayed output must carry the opt-in hash type.
    assert_unified_witnesses(bitcoind, txid1, 1)
    # An RBF changes the txid, so assert against the one actually mined.
    mined = a.mine_txid_or_rbf(txid2)
    # So must timing the HTLC out.
    assert_unified_witnesses(bitcoind, mined, 1)

    _, txid3, _ = a.wait_for_onchaind_tx('OUR_DELAYED_RETURN_TO_WALLET',
                                         'OUR_HTLC_TIMEOUT_TX/DELAYED_OUTPUT_TO_US')
    bitcoind.generate_block(4)
    bitcoind.generate_block(1, wait_for_mempool=txid3)
    assert_unified_witnesses(bitcoind, txid3, 1)
    payfuture.exception(TIMEOUT)
    bitcoind.generate_block(100)
    sync_blockheight(bitcoind, [a])
    assert a.daemon.is_in_log('All outputs resolved')


@pytest.mark.openchannel('v1')
def test_unified_onchain_htlc_success(node_factory, bitcoind):
    """Claiming an HTLC on chain with the preimage must use unified signing."""
    # a -> b -> c, and b drops to chain after learning the preimage from c.
    disconnects = ['-WIRE_UPDATE_FULFILL_HTLC', 'permfail']
    a, b, c = node_factory.get_nodes(3, opts=[{'broken_log': '.*'},
                                              {'disconnect': disconnects,
                                               'broken_log': '.*'},
                                              {'broken_log': '.*'}])
    b.rpc.connect(a.info['id'], 'localhost', a.port)
    b.rpc.connect(c.info['id'], 'localhost', c.port)
    b.fundchannel(a, 10**6)
    c_scid, _ = b.fundchannel(c, 10**6)
    mine_funding_to_announce(bitcoind, [a, b, c])
    a.wait_channel_active(c_scid)
    assert 'unified_sigs/even' in only_one(b.rpc.listpeerchannels(a.info['id'])['channels'])['channel_type']['names']

    b.pay(a, 2 * 10**8)
    inv = c.rpc.invoice(10**8, 'unified_success', 'desc')
    route = a.single_route(c.info['id'], 10**8)

    q = queue.Queue()

    def try_pay():
        try:
            a.rpc.sendpay(route, inv['payment_hash'], payment_secret=inv['payment_secret'])
            a.rpc.waitsendpay(inv['payment_hash'])
            q.put(None)
        except Exception as err:
            q.put(err)

    t = threading.Thread(target=try_pay)
    t.daemon = True
    t.start()

    b.daemon.wait_for_log('sendrawtx exit 0')
    bitcoind.generate_block(1, wait_for_mempool=1)
    b.daemon.wait_for_log(' to ONCHAIN')
    a.daemon.wait_for_log(' to ONCHAIN')

    ((_, txid1, _), (_, txid2, _)) = b.wait_for_onchaind_txs(
        ('OUR_HTLC_SUCCESS_TX', 'OUR_UNILATERAL/THEIR_HTLC'),
        ('OUR_DELAYED_RETURN_TO_WALLET', 'OUR_UNILATERAL/DELAYED_OUTPUT_TO_US'))

    # An RBF changes the txid, so assert against the one actually mined.
    mined1 = b.mine_txid_or_rbf(txid1)
    # Claiming with the preimage must carry the opt-in hash type.
    assert_unified_witnesses(bitcoind, mined1, 1)
    a.daemon.wait_for_log('THEIR_UNILATERAL/OUR_HTLC gave us preimage')
    err = q.get(timeout=TIMEOUT)
    assert err is None
    t.join(timeout=1)

    _, txid3, _ = b.wait_for_onchaind_tx('OUR_DELAYED_RETURN_TO_WALLET',
                                         'OUR_HTLC_SUCCESS_TX/DELAYED_OUTPUT_TO_US')
    bitcoind.generate_block(3)
    mined2 = b.mine_txid_or_rbf(txid2)
    assert_unified_witnesses(bitcoind, mined2, 1)
    bitcoind.generate_block(3)
    mined = b.mine_txid_or_rbf(txid3)
    assert_unified_witnesses(bitcoind, mined, 1)
    bitcoind.generate_block(100)
    sync_blockheight(bitcoind, [b])
    assert b.daemon.is_in_log('All outputs resolved')


@pytest.mark.openchannel('v1')
def test_unified_their_unilateral_htlc_to_us(node_factory, bitcoind):
    """Sweeping our HTLC off their unilateral commitment must use unified signing."""
    disconnects = ['-WIRE_UPDATE_FAIL_HTLC', 'permfail']
    a, b = node_factory.line_graph(2, opts=[{'broken_log': '.*'},
                                            {'disconnect': disconnects,
                                             'broken_log': '.*'}])
    assert 'unified_sigs/even' in only_one(a.rpc.listpeerchannels()['channels'])['channel_type']['names']
    route = a.single_route(b.info['id'], 10**8)

    q = queue.Queue()

    def try_pay():
        try:
            rhash = 'B1' * 32
            a.rpc.sendpay(route, rhash, payment_secret=rhash)
            q.put(None)
        except Exception as err:
            q.put(err)

    t = threading.Thread(target=try_pay)
    t.daemon = True
    t.start()

    b.daemon.wait_for_log(' to AWAITING_UNILATERAL')
    b.daemon.wait_for_log('sendrawtx exit 0')
    bitcoind.generate_block(1)
    a.daemon.wait_for_log(' to ONCHAIN')
    b.daemon.wait_for_log(' to ONCHAIN')
    a.daemon.wait_for_log('THEIR_UNILATERAL/OUR_HTLC')

    _, txid, _ = a.wait_for_onchaind_tx('OUR_HTLC_TIMEOUT_TO_US',
                                        'THEIR_UNILATERAL/OUR_HTLC')
    bitcoind.generate_block(9)
    q.get(timeout=TIMEOUT)
    t.join(timeout=1)
    mined = a.mine_txid_or_rbf(txid)
    assert_unified_witnesses(bitcoind, mined, 1)
    bitcoind.generate_block(100)
    sync_blockheight(bitcoind, [a])
    assert a.daemon.is_in_log('All outputs resolved')


@pytest.mark.openchannel('v1')
def test_unified_their_unilateral_fulfill_to_us(node_factory, bitcoind):
    """Claiming an incoming HTLC off their unilateral must use unified signing."""
    # a drops to chain once b has the preimage, so b claims it from a's commitment.
    a_disconnects = ['=WIRE_UPDATE_FULFILL_HTLC', 'permfail']
    b_disconnects = ['-WIRE_UPDATE_FULFILL_HTLC']
    a, b, c = node_factory.get_nodes(3, opts=[{'disconnect': a_disconnects,
                                               'broken_log': '.*'},
                                              {'disconnect': b_disconnects,
                                               'broken_log': '.*'},
                                              {'broken_log': '.*'}])
    b.rpc.connect(a.info['id'], 'localhost', a.port)
    b.rpc.connect(c.info['id'], 'localhost', c.port)
    c_ab, _ = b.fundchannel(a, 10**6)
    c_bc, _ = b.fundchannel(c, 10**6)
    mine_funding_to_announce(bitcoind, [a, b, c])
    a.wait_channel_active(c_bc)
    wait_for(lambda: c.rpc.listchannels(c_ab)['channels'] != [])
    assert 'unified_sigs/even' in only_one(b.rpc.listpeerchannels(a.info['id'])['channels'])['channel_type']['names']

    b.pay(a, 2 * 10**8)
    inv = c.rpc.invoice(10**8, 'unified_fulfill', 'desc')
    route = a.single_route(c.info['id'], 10**8)

    q = queue.Queue()

    def try_pay():
        try:
            a.rpc.sendpay(route, inv['payment_hash'], payment_secret=inv['payment_secret'])
            a.rpc.waitsendpay(inv['payment_hash'])
            q.put(None)
        except Exception as err:
            q.put(err)

    t = threading.Thread(target=try_pay)
    t.daemon = True
    t.start()

    a.daemon.wait_for_log('sendrawtx exit 0')
    bitcoind.generate_block(1, wait_for_mempool=1)
    a.daemon.wait_for_log(' to ONCHAIN')
    b.daemon.wait_for_log(' to ONCHAIN')

    _, txid, _ = b.wait_for_onchaind_tx('THEIR_HTLC_FULFILL_TO_US',
                                        'THEIR_UNILATERAL/THEIR_HTLC')
    mined = b.mine_txid_or_rbf(txid)
    assert_unified_witnesses(bitcoind, mined, 1)
    bitcoind.generate_block(100)
    sync_blockheight(bitcoind, [b])
    assert b.daemon.is_in_log('All outputs resolved')
