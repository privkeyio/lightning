"""Run only against isolated Knots regtest with Blake2b active from height 1."""
from fixtures import *  # noqa: F401,F403
from bitcoin.rpc import JSONRPCError
from pyln.client import Millisatoshi
from utils import only_one, wait_for, sync_blockheight, mine_funding_to_announce, TEST_NETWORK, TIMEOUT
import queue
import threading
from shutil import copyfile
import os
import pytest

pytestmark = pytest.mark.skipif(TEST_NETWORK != 'regtest', reason='Blake2b consensus tests require Knots regtest')


def is_signature(raw):
    return len(raw) == 65 or (69 <= len(raw) <= 73 and raw[0] == 0x30)


def assert_unified_witnesses(bitcoind, txid, minimum):
    tx = bitcoind.rpc.getrawtransaction(txid, True)
    signatures = []
    # SIGHASH_UNIFIED is added to the hash type that would otherwise apply,
    # so SIGHASH_ALL becomes 0x21, and the anchors HTLC form 0xa3.
    for vin in tx['vin']:
        wit = [bytes.fromhex(item) for item in vin.get('txinwitness', [])]
        # HTLC-timeout/success: <> <remotehtlcsig> <localhtlcsig> <> <wscript>.
        # Only the half the peer pre-signed may be 0xa3: ours is always 0x21.
        if len(wit) == 5 and wit[0] == b'' and is_signature(wit[1]) and is_signature(wit[2]):
            assert wit[1][-1] == 0xa3, '0x%02x' % wit[1][-1]
            assert wit[2][-1] == 0x21, '0x%02x' % wit[2][-1]
            signatures += wit[1:3]
            continue
        for raw in wit:
            if is_signature(raw):
                assert raw[-1] == 0x21, '0x%02x' % raw[-1]
                signatures.append(raw)
    assert len(signatures) >= minimum
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
@pytest.mark.openchannel('v2')
def test_unified_htlc_timeout_after_splice(node_factory, bitcoind, executor):
    """An HTLC in flight across a splice times out on chain with the splice's HTLC sigs."""
    a, b = node_factory.line_graph(2, wait_for_announce=True,
                                   opts=[{'broken_log': '.*'}, {'broken_log': '.*'}])
    assert 'unified_sigs/even' in only_one(a.rpc.listpeerchannels()['channels'])['channel_type']['names']

    b.rpc.dev_ignore_htlcs(id=a.info['id'], ignore=True)
    # Worth enough that timing it out on chain is not refused as uneconomic.
    inv = b.rpc.invoice(10**8, 'stuck_across_splice', 'desc')
    executor.submit(a.rpc.xpay, inv['bolt11'])
    b.daemon.wait_for_log('their htlc 0 dev_ignore_htlcs')

    chan_id = a.get_channel_id(b)
    funds = a.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
    result = a.rpc.splice_init(chan_id, 100000, funds['psbt'])
    result = a.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    result = a.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = a.rpc.signpsbt(result['psbt'])
    result = a.rpc.splice_signed(chan_id, result['signed_psbt'])
    a.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    b.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')

    bitcoind.generate_block(1, wait_for_mempool=result['txid'])
    sync_blockheight(bitcoind, [a, b])
    bitcoind.generate_block(5)
    a.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    b.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    # Close on the splice's first commitment, so timing the HTLC out uses the
    # HTLC sigs promoted from the splice rather than ones from a later update.
    a.rpc.dev_fail(b.info['id'])
    # The anchor spend takes a wallet coin the HTLC timeout tx needs too: left
    # in the mempool, it blocks that tx, so mine it with the commitment.
    a.daemon.wait_for_log('Creating anchor spend for local commit tx ')
    bitcoind.generate_block(1, wait_for_mempool=2)
    a.daemon.wait_for_log(' to ONCHAIN')

    _, txid, blocks = a.wait_for_onchaind_tx('OUR_HTLC_TIMEOUT_TX', 'OUR_UNILATERAL/OUR_HTLC')
    # The timeout is deferred until the HTLC expires.
    bitcoind.generate_block(blocks)
    sync_blockheight(bitcoind, [a])
    mined = a.mine_txid_or_rbf(txid)
    assert_unified_witnesses(bitcoind, mined, 1)


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

    # Mine the commitment together with its anchor spend: the HTLC claim draws
    # on the same wallet coin, so an anchor spend left in the mempool would
    # refuse it until another block came.
    b.daemon.wait_for_log('Creating anchor spend for local commit tx')
    bitcoind.generate_block(1, wait_for_mempool=2)
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


def split_wallet(node, bitcoind, n, sats):
    """Leave node only n coins of sats each, so fees come from several."""
    fundsats = sum(int(Millisatoshi(o['amount_msat']).to_satoshi())
                   for o in node.rpc.listfunds()['outputs'] if o['status'] == 'confirmed')
    psbt = node.rpc.fundpsbt("all", "1000perkw", 1000)['psbt']
    psbt = node.rpc.addpsbtoutput(fundsats - n * sats - 5000, psbt,
                                  destination=bitcoind.rpc.getnewaddress())['psbt']
    for _ in range(n):
        psbt = node.rpc.addpsbtoutput(sats, psbt)['psbt']
    node.rpc.sendpsbt(node.rpc.signpsbt(psbt)['signed_psbt'])
    bitcoind.generate_block(1, wait_for_mempool=1)
    sync_blockheight(bitcoind, [node])
    wait_for(lambda: len([o for o in node.rpc.listfunds()['outputs']
                          if o['status'] == 'confirmed' and o['amount_msat'] == sats * 1000]) == n)


def mempool_spend(bitcoind, txid, witness_items=None):
    """The mempool tx whose first input spends txid, if any."""
    for t in bitcoind.rpc.getrawmempool():
        try:
            tx = bitcoind.rpc.getrawtransaction(t, True)
        except JSONRPCError:
            # Replaced since we listed the mempool.
            continue
        vin0 = tx['vin'][0]
        if vin0.get('txid') == txid and (witness_items is None
                                         or len(vin0.get('txinwitness', [])) == witness_items):
            return tx
    return None


def mempool_htlc_tx(bitcoind, commit):
    return mempool_spend(bitcoind, commit, witness_items=5)


def offer_stuck_htlc(l1, l2):
    """l1 offers l2 an HTLC which l2 never resolves, then l2 goes away."""
    # Ignored rather than failed: l1 may reconnect, and a peer that only
    # disconnects before failing it can still fail it then.
    l2.rpc.dev_ignore_htlcs(id=l1.info['id'], ignore=True)
    inv = l2.rpc.invoice(10**8, 'stuck', 'desc')
    routestep = {'amount_msat': 10**8, 'id': l2.info['id'], 'delay': 12,
                 'channel': only_one(l1.rpc.listpeerchannels()['channels'])['short_channel_id']}
    l1.rpc.sendpay([routestep], inv['payment_hash'], payment_secret=inv['payment_secret'])
    l2.daemon.wait_for_log('their htlc 0 dev_ignore_htlcs')
    wait_for(lambda: only_one(only_one(l1.rpc.listpeerchannels()['channels'])['htlcs'])['state']
             == 'SENT_ADD_ACK_REVOCATION')
    l2.stop()


def htlc_timeout_onchain(l1, bitcoind, funding):
    """The HTLC hits its deadline: l1 goes on chain and times it out."""
    bitcoind.generate_block(14)
    wait_for(lambda: mempool_spend(bitcoind, funding) is not None)
    commit = mempool_spend(bitcoind, funding)['txid']
    bitcoind.generate_block(1, wait_for_mempool=commit)
    l1.daemon.wait_for_log(' to ONCHAIN')
    l1.wait_for_onchaind_tx('OUR_HTLC_TIMEOUT_TX', 'OUR_UNILATERAL/OUR_HTLC')
    wait_for(lambda: mempool_htlc_tx(bitcoind, commit) is not None)
    return commit


def htlc_tx_onchain(node_factory, bitcoind, case):
    """Leave our anchor channel's HTLC-timeout or HTLC-success tx, with
    fee inputs from a wallet of small coins, in the mempool.  Returns the
    node which broadcast it and the commitment tx it spends."""
    opts = {'feerates': (3000,) * 4,
            'min-emergency-msat': 546000,
            'may_reconnect': True,
            'broken_log': 'overgrind: short signature length'}
    if case == 'timeout':
        node, peer = node_factory.line_graph(2, opts=[opts, {'feerates': (3000,) * 4}])
        chan = only_one(node.rpc.listpeerchannels()['channels'])
        split_wallet(node, bitcoind, 12, 20000)
        offer_stuck_htlc(node, peer)
        commit = htlc_timeout_onchain(node, bitcoind, chan['funding_txid'])
    else:
        # a -> node -> c, and node drops to chain after learning the preimage.
        opts['disconnect'] = ['-WIRE_UPDATE_FULFILL_HTLC', 'permfail']
        a, node, c = node_factory.get_nodes(3, opts=[{'may_reconnect': True}, opts,
                                                     {'may_reconnect': True}])
        node.rpc.connect(a.info['id'], 'localhost', a.port)
        node.rpc.connect(c.info['id'], 'localhost', c.port)
        node.fundwallet(10**7)
        node.fundchannel(a, 10**6)
        c_scid, _ = node.fundchannel(c, 10**6)
        mine_funding_to_announce(bitcoind, [a, node, c])
        a.wait_channel_active(c_scid)
        chan = only_one(node.rpc.listpeerchannels(a.info['id'])['channels'])
        split_wallet(node, bitcoind, 12, 20000)
        node.pay(a, 2 * 10**8)
        inv = c.rpc.invoice(10**8, 'success', 'desc')
        route = a.single_route(c.info['id'], 10**8)
        a.rpc.sendpay(route, inv['payment_hash'], payment_secret=inv['payment_secret'])
        wait_for(lambda: mempool_spend(bitcoind, chan['funding_txid']) is not None)
        commit = mempool_spend(bitcoind, chan['funding_txid'])['txid']
        bitcoind.generate_block(1, wait_for_mempool=commit)
        node.daemon.wait_for_log(' to ONCHAIN')
        node.wait_for_onchaind_tx('OUR_HTLC_SUCCESS_TX', 'OUR_UNILATERAL/THEIR_HTLC')
        wait_for(lambda: mempool_htlc_tx(bitcoind, commit) is not None)

    names = chan['channel_type']['names']
    assert 'unified_sigs/even' in names and 'anchors/even' in names
    return node, commit


@pytest.mark.openchannel('v1')
@pytest.mark.parametrize('case', ['timeout', 'success'])
def test_unified_htlc_tx_rbf(node_factory, bitcoind, case):
    """Every fee bump of our HTLC tx signs our half again, SIGHASH_ALL."""
    node, commit = htlc_tx_onchain(node_factory, bitcoind, case)
    # txid -> our signature, for each version seen in the mempool.
    versions = {}

    def check_current():
        tx = mempool_htlc_tx(bitcoind, commit)
        assert len(tx['vin']) > 1
        wit = [bytes.fromhex(w) for w in tx['vin'][0]['txinwitness']]
        assert wit[1][-1] == 0xa3, '0x%02x' % wit[1][-1]
        assert wit[2][-1] == 0x21, '0x%02x' % wit[2][-1]
        assert_unified_witnesses(bitcoind, tx['txid'], 2)
        if tx['txid'] not in versions:
            # SIGHASH_ALL: a different tx needs a different signature.
            assert wit[2] not in versions.values()
            versions[tx['txid']] = wit[2]
        return tx['txid']

    def next_version(oldtxid):
        wait_for(lambda: (mempool_htlc_tx(bitcoind, commit) or {}).get('txid') not in (None, oldtxid))
        known = len(versions)
        txid = check_current()
        assert len(versions) == known + 1, versions
        return txid

    txid = check_current()
    # Each step outbids the last by enough to replace it in the mempool,
    # even after smoothing and even when it uses fewer inputs and no change.
    for rate in (6000, 18000):
        node.set_feerates((rate,) * 4)
        # An empty block makes it rebroadcast now, at the new feerate.
        bitcoind.generate_block(1, needfeerate=10**9)
        txid = next_version(txid)

        # Fees rise while it is down, so only the restarted node can have
        # built and signed the next version.
        node.stop()
        node.set_feerates((rate * 3 // 2,) * 4, wait_for_effect=False)
        logpos = len(node.daemon.logs)
        node.start()
        txid = next_version(txid)
        assert node.daemon.is_in_log(r'RBF HTLC txid .* with txid {}'.format(txid), start=logpos)

    assert len(versions) == 5, versions


@pytest.mark.openchannel('v1')
def test_htlc_tx_rbf_without_unified_sigs(node_factory, bitcoind):
    """A channel without option_unified_sigs signs its HTLC txs without
    SIGHASH_UNIFIED, on a node which offers it."""
    no_unified = {'dev-force-features': '-514', 'may_reconnect': True, 'allow_warning': True}
    l1, l2 = node_factory.line_graph(2, opts=[{**no_unified,
                                               'feerates': (3000,) * 4,
                                               'min-emergency-msat': 546000,
                                               'broken_log': 'overgrind: short signature length'},
                                              {**no_unified,
                                               'feerates': (3000,) * 4}])
    chan = only_one(l1.rpc.listpeerchannels()['channels'])
    names = chan['channel_type']['names']
    assert 'unified_sigs/even' not in names and 'anchors/even' in names

    # Both upgrade.  The channel keeps the type it was opened with.
    for n in (l1, l2):
        del n.daemon.opts['dev-force-features']
        n.restart()
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL')
    assert only_one(l1.rpc.listpeerchannels()['channels'])['channel_type']['names'] == names

    split_wallet(l1, bitcoind, 12, 5000)
    offer_stuck_htlc(l1, l2)
    commit = htlc_timeout_onchain(l1, bitcoind, chan['funding_txid'])

    def our_htlc_sigs():
        tx = mempool_htlc_tx(bitcoind, commit)
        assert len(tx['vin']) > 1
        wit = [bytes.fromhex(w) for w in tx['vin'][0]['txinwitness']]
        # Theirs is SIGHASH_SINGLE|SIGHASH_ANYONECANPAY and ours is
        # SIGHASH_ALL, as BOLT 5 says.  No SIGHASH_UNIFIED on either.
        assert wit[1][-1] == 0x83 and wit[2][-1] == 0x01, (wit[1][-1], wit[2][-1])
        return tx['txid'], wit[2]

    txid, sig = our_htlc_sigs()
    l1.set_feerates((6000,) * 4)
    bitcoind.generate_block(1, needfeerate=10**9)
    wait_for(lambda: (mempool_htlc_tx(bitcoind, commit) or {}).get('txid') not in (None, txid))
    # Ours commits to the fee inputs, so the fee bump signed again.
    newtxid, newsig = our_htlc_sigs()
    assert newtxid != txid and newsig != sig
