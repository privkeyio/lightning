from fixtures import *  # noqa: F401,F403
from pyln.client import LightningRpc, RpcError
from pyln.testing.utils import EXPERIMENTAL_DUAL_FUND, SLOW_MACHINE, VALGRIND
import os
import pytest
import threading
import unittest
import time
from utils import (
    sync_blockheight, wait_for, TEST_NETWORK, first_scid, only_one, TIMEOUT
)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice(node_factory, bitcoind):
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True)

    chan_id = l1.get_channel_id(l2)

    # add extra sats to pay fee
    funds_result = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)

    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is False)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is True)
    result = l1.rpc.signpsbt(result['psbt'])
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])

    l2.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    l1.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')

    wait_for(lambda: len(list(bitcoind.rpc.getrawmempool(True).keys())) == 1)
    mempool = bitcoind.rpc.getrawmempool(True)
    assert result['txid'] in list(mempool.keys())

    bitcoind.generate_block(6, wait_for_mempool=1)

    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    inv = l2.rpc.invoice(10**2, '3', 'no_3')
    l1.rpc.xpay(inv['bolt11'])

    # Check that the splice doesn't generate a unilateral close transaction
    time.sleep(5)
    assert l1.db_query("SELECT count(*) as c FROM channeltxs;")[0]['c'] == 0


def _splice_to_inflight(l1, chan_id, amount=100000):
    """Drive a splice as far as an inflight in the db, and return its txid."""
    funds_result = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, amount, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])
    l1.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    return result['txid']


@pytest.mark.openchannel('v1')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
@unittest.skipIf(os.getenv('TEST_DB_PROVIDER', 'sqlite3') != 'sqlite3', "modifies database, which is assumed sqlite3")
# -1 is how lightningd itself stored a u32 above INT_MAX (db_bind_int); the
# positive form is what you get writing the same value by hand.
@pytest.mark.parametrize("poison,repaired", [(4294967295, 1000000),
                                             (-1, 1000000),
                                             (2000000, 1000000),
                                             (0, 253)])
def test_splice_stored_feerate_repaired_on_upgrade(node_factory, bitcoind,
                                                   poison, repaired):
    """An out-of-range stored funding feerate is repaired when we upgrade.

    Nothing used to bound what got written to
    channel_funding_inflights.funding_feerate, and json_add_channel then
    asserted on it: the BOLT #2 25/24 RBF bump overflows a u32 above
    UINT_MAX/25, and 0 tripped the assert right above it.  Since plugins call
    listpeerchannels at startup, a single bad row crash-looped the node with
    no RPC left to repair it with, which is what the migration is for.
    """
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True)
    chan_id = l1.get_channel_id(l2)
    _splice_to_inflight(l1, chan_id)

    l1.stop()
    l1.db_manip("UPDATE channel_funding_inflights"
                " SET funding_feerate = {}".format(poison))

    # Rewind past the two clamping migrations so they run again over the row
    # we just planted, which is the upgrade an attacked node goes through.
    # They are plain idempotent UPDATEs, so re-running them is safe.  The
    # count has to reach back past both of them, so it grows whenever a
    # migration is appended after them, and what those create has to go
    # first: here, option_independent_secrets' table and column.
    l1.db_manip("DROP TABLE channel_revocation_secrets")
    l1.db_manip("ALTER TABLE channels DROP COLUMN independent_secrets_close_at")
    l1.db_manip("UPDATE version SET version = version - 6")
    l1.daemon.opts['database-upgrade'] = 'true'
    l1.start()

    assert l1.daemon.is_in_log(r'Updating database from version')

    row = l1.db_query("SELECT funding_feerate AS f"
                      " FROM channel_funding_inflights;")[0]
    assert row['f'] == repaired

    # And the read path, which used to abort here, agrees.
    chan = only_one(l1.rpc.listpeerchannels()['channels'])
    assert chan['last_feerate'] == '{}perkw'.format(repaired)
    assert chan['next_feerate'] == '{}perkw'.format(repaired * 25 // 24)


@pytest.mark.openchannel('v1')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_feerate_too_high(node_factory, bitcoind):
    """We refuse a splice a peer proposes at an absurd feerate.

    The fee comes out of the initiator's balance, so we gain nothing by
    signing it; they lose the difference to a broken fee estimator of theirs.
    Before this there was no upper bound on the accepter side at all.
    """
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True,
                                     opts={'allow_warning': True,
                                           'may_reconnect': True})
    chan_id = l1.get_channel_id(l2)

    # Both sides agree feerate_max is 15000 * max_fee_multiplier.
    assert l2.rpc.feerates('perkw')['perkw']['max_acceptable'] == 150000

    # force_feerate gets us past *our* check on what we're willing to pay,
    # which leaves l2's bound as the thing under test.
    funds_result = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)

    # l2 refuses on receipt of splice_init, so the splice_ack l1 is waiting
    # for never arrives: run it in a daemon thread so the test can proceed.
    def _splice():
        try:
            # force_feerate isn't in the pyln-client wrapper, so call directly.
            l1.rpc.call('splice_init',
                        {'channel_id': chan_id,
                         'relative_amount': 100000,
                         'initialpsbt': funds_result['psbt'],
                         # param_feerate reads a bare number as perkb, and
                         # the schema only allows a bare number here:
                         # 800000perkb == 200000perkw.
                         'feerate_per_kw': 800000,
                         'force_feerate': True})
        except Exception:
            pass

    threading.Thread(target=_splice, daemon=True).start()

    l2.daemon.wait_for_log(r'Splice feerate_perkw 200000 is above our maximum 150000')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_two_chan_splice_in(node_factory, bitcoind):
    l1, l2, l3 = node_factory.line_graph(3, fundamount=1000000, wait_for_announce=True)

    # l2 will splice funds into the channels with l1 and l3 at the same time

    chan_id1 = l2.get_channel_id(l1)
    chan_id2 = l2.get_channel_id(l3)

    # add extra sats to pay fee
    funds_result = l2.rpc.fundpsbt("211722sat", 0, 0, excess_as_change=True)

    # Intiate splices to both channels
    result = l2.rpc.splice_init(chan_id1, 100000, funds_result['psbt'])
    result = l2.rpc.splice_init(chan_id2, 100000, result['psbt'])  # start with psbt from first channel

    done1 = False
    done2 = False
    sigs1 = False
    sigs2 = False

    while not done1 or not done2:
        if not done1:
            result = l2.rpc.splice_update(chan_id1, result['psbt'])
            done1 = result['commitments_secured']
            sigs1 = result['signatures_secured']
            print("chan 1 " + result['psbt'])
        if not done2:
            result = l2.rpc.splice_update(chan_id2, result['psbt'])
            done2 = result['commitments_secured']
            sigs2 = result['signatures_secured']
            print("chan 2 " + result['psbt'])

    # Due to splice signing order, we may or may not have signatures
    # from all peers, but we must have them from one.
    print("Sigs1 " + str(sigs1) + ", Sigs2 " + str(sigs2))
    assert(sigs1 or sigs2)

    # Sign the inputs provided by `fundpsbt`
    result = l2.rpc.signpsbt(result['psbt'])
    result['psbt'] = result['signed_psbt']

    if sigs2:
        # If chan2 gave us sigs, start with chan1
        result = l2.rpc.splice_signed(chan_id1, result['psbt'])
        result = l2.rpc.splice_signed(chan_id2, result['psbt'])
    else:
        # If chan1 gave us sigs, start with chan2
        result = l2.rpc.splice_signed(chan_id2, result['psbt'])
        result = l2.rpc.splice_signed(chan_id1, result['psbt'])

    l3.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    l2.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    l1.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')

    wait_for(lambda: len(list(bitcoind.rpc.getrawmempool(True).keys())) == 1)
    assert result['txid'] in list(bitcoind.rpc.getrawmempool(True).keys())

    bitcoind.generate_block(6, wait_for_mempool=1)

    l3.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    inv = l2.rpc.invoice(10**2, '1', 'no_1')
    l1.rpc.xpay(inv['bolt11'])

    inv = l3.rpc.invoice(10**2, '2', 'no_2')
    l2.rpc.xpay(inv['bolt11'])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_rbf(node_factory, bitcoind):
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True)

    chan_id = l1.get_channel_id(l2)

    funds_result = l1.rpc.addpsbtoutput(100000)

    # Pay with fee by subtracting 5801 from channel balance
    result = l1.rpc.splice_init(chan_id, -105801, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is False)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is True)
    result = l1.rpc.splice_signed(chan_id, result['psbt'])

    l2.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    l1.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')

    wait_for(lambda: len(list(bitcoind.rpc.getrawmempool(True).keys())) == 1)
    mempool = bitcoind.rpc.getrawmempool(True)
    assert result['txid'] in list(mempool.keys())

    inv = l2.rpc.invoice(10**2, '1', 'no_1')
    l1.rpc.xpay(inv['bolt11'])

    funds_result = l1.rpc.addpsbtoutput(100000)

    # Pay with fee by subtracting 5790 from channel balance
    result = l1.rpc.splice_init(chan_id, -111722, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is False)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is True)
    result = l1.rpc.splice_signed(chan_id, result['psbt'])

    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_AWAITING_SPLICE')
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_AWAITING_SPLICE')

    inv = l2.rpc.invoice(10**2, '2', 'no_2')
    l1.rpc.xpay(inv['bolt11'])

    # Make sure l1 doesn't unilateral close if HTLC hasn't completely settled before deadline.
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['htlcs'] == [])

    bitcoind.generate_block(6, wait_for_mempool=1)

    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    inv = l2.rpc.invoice(10**2, '3', 'no_3')
    l1.rpc.xpay(inv['bolt11'])

    # Check that the splice doesn't generate a unilateral close transaction
    time.sleep(5)
    assert l1.db_query("SELECT count(*) as c FROM channeltxs;")[0]['c'] == 0


@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_rbf_htlc_sigs(node_factory, bitcoind, executor):
    """Once a splice-RBF candidate confirms, only its HTLC signatures become
    the active set, even when the other candidates place the funding output
    at the same index."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True)
    chan_id = l1.get_channel_id(l2)

    def htlc_sig_rows(node):
        return node.db_query("SELECT inflight_tx_id, inflight_tx_outnum"
                             " FROM htlc_sigs;")

    def fee_bump_splice(fee):
        # No inputs and no extra outputs: the new funding output is the only
        # output, so every candidate puts it at index 0.
        result = l1.rpc.splice_init(chan_id, -fee)
        result = l1.rpc.splice_update(chan_id, result['psbt'])
        assert result['commitments_secured'] is False
        result = l1.rpc.splice_update(chan_id, result['psbt'])
        assert result['commitments_secured'] is True
        return l1.rpc.splice_signed(chan_id, result['psbt'])

    first = fee_bump_splice(5801)
    l1.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    l2.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    wait_for(lambda: bitcoind.rpc.getrawmempool() == [first['txid']])

    second = fee_bump_splice(11722)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_AWAITING_SPLICE')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_AWAITING_SPLICE')
    wait_for(lambda: bitcoind.rpc.getrawmempool() == [second['txid']])

    # The losing candidate differs from the winner only by txid.
    assert first['txid'] != second['txid']
    assert first['outnum'] == second['outnum'] == 0

    # Leave one HTLC stuck on the channel. The commitment that adds it
    # carries an HTLC signature for the active funding and for each
    # candidate, which is what gets stored in htlc_sigs.
    l2.rpc.dev_ignore_htlcs(id=l1.info['id'], ignore=True)
    inv = l2.rpc.invoice(10000000, '1', 'no_1')
    executor.submit(l1.rpc.xpay, inv['bolt11'])
    l2.daemon.wait_for_log('their htlc 0 dev_ignore_htlcs')

    # One active row plus one per candidate, on both sides.
    for node in (l1, l2):
        wait_for(lambda: len(htlc_sig_rows(node)) == 3)
        rows = htlc_sig_rows(node)
        assert len([r for r in rows if r['inflight_tx_id'] is None]) == 1
        assert [r['inflight_tx_outnum'] for r in rows
                if r['inflight_tx_id'] is not None] == [0, 0]

    bitcoind.generate_block(6, wait_for_mempool=1)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    # Only the confirmed candidate's signature survives, now active.  The
    # state change is logged before the transaction that promotes it commits.
    for node in (l1, l2):
        wait_for(lambda: len(htlc_sig_rows(node)) == 1)
        assert htlc_sig_rows(node)[0]['inflight_tx_id'] is None


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_nosign(node_factory, bitcoind):
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True)

    chan_id = l1.get_channel_id(l2)

    # add extra sats to pay fee
    funds_result = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)

    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is False)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is True)
    try:
        l1.rpc.splice_signed(chan_id, result['psbt'])
        assert(False)
    except RpcError as e:
        assert(e.error['code'] == 358)
        assert(e.error['message'] == "The PSBT is missing a signature. Have you signed it with `signpsbt`?")


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_gossip(node_factory, bitcoind):
    l1, l2, l3 = node_factory.line_graph(3, fundamount=1000000, wait_for_announce=True)

    chan_id = l1.get_channel_id(l2)
    pre_splice_scid = first_scid(l1, l2)

    # add extra sats to pay fee
    funds_result = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)

    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is False)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is True)
    result = l1.rpc.signpsbt(result['psbt'])
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])

    wait_for(lambda: only_one(l2.rpc.listpeerchannels(l1.info['id'])['channels'])['state'] == 'CHANNELD_AWAITING_SPLICE')
    wait_for(lambda: only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])['state'] == 'CHANNELD_AWAITING_SPLICE')

    bitcoind.generate_block(5, wait_for_mempool=result['txid'])

    # l3 will see channel dying, but still consider it OK for 72 blocks.
    l3.daemon.wait_for_log(f'gossipd: channel {pre_splice_scid} closing soon due to the funding outpoint being spent')
    assert len(l3.rpc.listchannels(short_channel_id=pre_splice_scid)['channels']) == 2
    assert len(l3.rpc.listchannels(source=l1.info['id'])['channels']) == 1

    # Final one will allow splice announcement to proceed.
    bitcoind.generate_block(1)
    wait_for(lambda: only_one(l2.rpc.listpeerchannels(l1.info['id'])['channels'])['state'] == 'CHANNELD_NORMAL')
    wait_for(lambda: only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])['state'] == 'CHANNELD_NORMAL')

    post_splice_scid = first_scid(l1, l2)
    assert post_splice_scid != pre_splice_scid

    # l3 should see the new channel now.
    wait_for(lambda: len(l3.rpc.listchannels(short_channel_id=post_splice_scid)['channels']) == 2)
    assert len(l3.rpc.listchannels(short_channel_id=pre_splice_scid)['channels']) == 2

    bitcoind.generate_block(67)

    # The old channel should fall off l3's perspective
    wait_for(lambda: l3.rpc.listchannels(short_channel_id=pre_splice_scid)['channels'] == [])
    assert len(l3.rpc.listchannels(short_channel_id=post_splice_scid)['channels']) == 2

    # Check that the splice doesn't generate a unilateral close transaction
    time.sleep(5)
    assert l1.db_query("SELECT count(*) as c FROM channeltxs;")[0]['c'] == 0

    # Still looks normal from both sides
    assert only_one(l1.rpc.listpeerchannels()['channels'])['short_channel_id'] == post_splice_scid
    assert only_one(l1.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL'
    assert only_one(l2.rpc.listpeerchannels(l1.info['id'])['channels'])['short_channel_id'] == post_splice_scid
    assert only_one(l2.rpc.listpeerchannels(l1.info['id'])['channels'])['state'] == 'CHANNELD_NORMAL'

    # Check for channel announcement failure
    assert not l1.daemon.is_in_log("invalid local_channel_announcement")
    assert not l2.daemon.is_in_log("invalid local_channel_announcement")


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_invoice_routehint_after_lock(node_factory, bitcoind):
    l1, l2, l3 = node_factory.line_graph(3, fundamount=1000000, wait_for_announce=True)
    chan_id = l1.get_channel_id(l2)
    pre_splice_scid = first_scid(l1, l2)

    funds_result = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.signpsbt(result['psbt'])
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])
    bitcoind.generate_block(6, wait_for_mempool=result['txid'])
    wait_for(lambda: only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])['state'] == 'CHANNELD_NORMAL')
    post_splice_scid = first_scid(l1, l2)

    # Gossip keeps the old scid for 72 blocks, so listincoming has both.
    wait_for(lambda: {pre_splice_scid, post_splice_scid}
             <= {c['short_channel_id'] for c in l1.rpc.listincoming()['incoming']})

    # l2 has nothing to send l1, so the invoice falls back to MPP routehints.
    inv = l1.rpc.invoice(100000, 'after-splice', 'after-splice')
    routes = l1.rpc.decode(inv['bolt11'])['routes']
    assert [r[0]['short_channel_id'] for r in routes] == [post_splice_scid]

    # A hint naming the old scid still selects the channel.
    inv = l1.rpc.invoice(100000, 'old-scid-hint', 'old-scid-hint',
                         exposeprivatechannels=[pre_splice_scid])
    routes = l1.rpc.decode(inv['bolt11'])['routes']
    assert [r[0]['short_channel_id'] for r in routes] == [post_splice_scid]


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_listnodes(node_factory, bitcoind):
    # Here we do a splice but underfund it purposefully
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True)

    chan_id = l1.get_channel_id(l2)

    # add extra sats to pay fee
    funds_result = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)

    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is False)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is True)
    result = l1.rpc.signpsbt(result['psbt'])
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])

    l2.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    l1.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')

    assert len(l1.rpc.listnodes()['nodes']) == 2
    assert len(l2.rpc.listnodes()['nodes']) == 2

    bitcoind.generate_block(6, wait_for_mempool=1)

    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    bitcoind.generate_block(7)

    wait_for(lambda: len(l1.rpc.listnodes()['nodes']) == 2)
    wait_for(lambda: len(l2.rpc.listnodes()['nodes']) == 2)


@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_inputs_prefer_p2wpkh_p2tr(node_factory, bitcoind):
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     opts=[{'may_reconnect': True,
                                            'allow_warning': True},
                                           {'may_reconnect': True}])

    # l2's force close leaves l1 a P2WSH to_remote output.
    l1.stop()
    l2.rpc.close(l1.info['id'], unilateraltimeout=1)
    l2.wait_for_channel_onchain(l1.info['id'])
    bitcoind.generate_block(100)
    l1.start()
    l1.daemon.wait_for_log('onchaind complete, forgetting peer')
    wait_for(lambda: len(l1.rpc.listfunds()['outputs']) == 2)
    outputs = l1.rpc.listfunds()['outputs']
    p2wsh = only_one([o for o in outputs if o['scriptpubkey'].startswith('0020')])
    others = sum(o['amount_msat'] for o in outputs if o is not p2wsh) // 1000

    def selected(amount):
        res = l1.rpc.addpsbtinput(satoshi=amount, reserve=0)['reservations']
        return [(r['txid'], r['vout']) for r in res]

    for _ in range(20):
        assert (p2wsh['txid'], p2wsh['output']) not in selected(100000)

    assert not l1.daemon.is_in_log('addpsbtinput: using P2WSH close outputs')

    # It is still used when the others are not enough.
    assert (p2wsh['txid'], p2wsh['output']) in selected(others + 10000)
    l1.daemon.wait_for_log('addpsbtinput: using P2WSH close outputs')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_out(node_factory, bitcoind):
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True)

    chan_id = l1.get_channel_id(l2)

    funds_result = l1.rpc.addpsbtoutput(100000)

    # Pay with fee by subjtracting 5801 from channel balance
    result = l1.rpc.splice_init(chan_id, -105801, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is False)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is True)
    result = l1.rpc.splice_signed(chan_id, result['psbt'])

    l2.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    l1.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')

    wait_for(lambda: len(list(bitcoind.rpc.getrawmempool(True).keys())) == 1)
    mempool = bitcoind.rpc.getrawmempool(True)
    assert result['txid'] in list(mempool.keys())

    bitcoind.generate_block(6, wait_for_mempool=1)

    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    inv = l2.rpc.invoice(10**2, '3', 'no_3')
    l1.rpc.xpay(inv['bolt11'])

    # Check that the splice doesn't generate a unilateral close transaction
    time.sleep(5)
    assert l1.db_query("SELECT count(*) as c FROM channeltxs;")[0]['c'] == 0


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_invalid_splice(node_factory, bitcoind):
    # Here we do a splice but underfund it purposefully
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True, opts={'may_reconnect': True,
                                                                                          'allow_warning': True})

    chan_id = l1.get_channel_id(l2)

    # We claim to add 100000 but in fact add nothing
    result = l1.rpc.splice_init(chan_id, 100000)

    with pytest.raises(RpcError) as rpc_error:
        result = l1.rpc.splice_update(chan_id, result['psbt'])
        assert(result['commitments_secured'] is False)
        result = l1.rpc.splice_update(chan_id, result['psbt'])
        assert(result['commitments_secured'] is True)

    assert rpc_error.value.error["code"] == 357
    assert rpc_error.value.error["message"] == "You provided 1000000000msat but committed to 1100000000msat."

    # The splicing inflight should not have been left pending in the DB
    assert l1.db_query("SELECT count(*) as c FROM channel_funding_inflights;")[0]['c'] == 0

    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_NORMAL channel')

    assert l1.db_query("SELECT count(*) as c FROM channel_funding_inflights;")[0]['c'] == 0

    # Now we do a real splice to confirm everything works after restart
    funds_result = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)

    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is False)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is True)
    result = l1.rpc.signpsbt(result['psbt'])
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])

    wait_for(lambda: len(list(bitcoind.rpc.getrawmempool(True).keys())) == 1)
    mempool = bitcoind.rpc.getrawmempool(True)
    assert result['txid'] in list(mempool.keys())

    bitcoind.generate_block(6, wait_for_mempool=1)

    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    inv = l2.rpc.invoice(10**2, '3', 'no_3')
    l1.rpc.xpay(inv['bolt11'])

    # Check that the splice doesn't generate a unilateral close transaction
    time.sleep(5)
    assert l1.db_query("SELECT count(*) as c FROM channeltxs;")[0]['c'] == 0


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_commit_crash_splice(node_factory, bitcoind):
    # Here we do a normal splice out but force a restart after commiting.
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True, opts={'may_reconnect': True})

    chan_id = l1.get_channel_id(l2)

    result = l1.rpc.splice_init(chan_id, -105801, l1.rpc.addpsbtoutput(100000)['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is False)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is True)

    l1.daemon.wait_for_log(r"Splice initiator: we commit")

    l1.restart()

    # The splicing inflight should have been left pending in the DB
    assert l1.db_query("SELECT count(*) as c FROM channel_funding_inflights;")[0]['c'] == 1

    l1.daemon.wait_for_log(r'peer_out WIRE_CHANNEL_REESTABLISH')
    l1.daemon.wait_for_log(r'Got reestablish commit=1 revoke=0 inflights: 1, active splices: 1')
    l1.daemon.wait_for_log(r'Splice resume check with local_next_funding: sent, remote_next_funding: received, inflights: 1')
    l1.daemon.wait_for_log(r'Splice negotation, will not send commit, not recv commit, send signature, recv signature as initiator')

    assert l1.db_query("SELECT count(*) as c FROM channel_funding_inflights;")[0]['c'] == 1

    l2.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    l1.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')

    wait_for(lambda: len(list(bitcoind.rpc.getrawmempool(True).keys())) == 1)

    bitcoind.generate_block(6, wait_for_mempool=1)

    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    time.sleep(5)

    assert l1.db_query("SELECT count(*) as c FROM channel_funding_inflights;")[0]['c'] == 0

    inv = l2.rpc.invoice(10**2, '3', 'no_3')
    l1.rpc.xpay(inv['bolt11'])

    # Check that the splice doesn't generate a unilateral close transaction
    time.sleep(5)
    assert l1.db_query("SELECT count(*) as c FROM channeltxs;")[0]['c'] == 0


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_stuck_htlc(node_factory, bitcoind, executor):
    l1, l2, l3 = node_factory.line_graph(3, wait_for_announce=True)

    l3.rpc.dev_ignore_htlcs(id=l2.info['id'], ignore=True)

    inv = l3.rpc.invoice(10000000, '1', 'no_1')
    executor.submit(l1.rpc.xpay, inv['bolt11'])
    l3.daemon.wait_for_log('their htlc 0 dev_ignore_htlcs')

    # Now we should have a stuck invoice between l1 -> l2

    chan_id = l1.get_channel_id(l2)

    # add extra sats to pay fee
    funds_result = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)

    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is False)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is True)
    result = l1.rpc.signpsbt(result['psbt'])
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])

    l2.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    l1.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')

    wait_for(lambda: len(list(bitcoind.rpc.getrawmempool(True).keys())) == 1)
    mempool = bitcoind.rpc.getrawmempool(True)
    assert result['txid'] in list(mempool.keys())

    bitcoind.generate_block(1, wait_for_mempool=1)
    # Don't have l2, l3 reject channel_announcement as too far in future.
    sync_blockheight(bitcoind, [l1, l2, l3])
    bitcoind.generate_block(5)

    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    # Check that the splice doesn't generate a unilateral close transaction
    time.sleep(5)
    assert l1.db_query("SELECT count(*) as c FROM channeltxs;")[0]['c'] == 0


@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_route_by_old_scid(node_factory, bitcoind):
    opts = {'may_reconnect': True}
    # l1 sometimes talks about pre-splice channels.  l2 (being part of the splice) immediately forgets
    # the old scid and uses the new one, then complains when l1 talks about it.  Which is fine, but
    # breaks CI.
    l1opts = opts.copy()
    l1opts['allow_warning'] = True
    l1, l2, l3 = node_factory.line_graph(3, wait_for_announce=True, opts=[l1opts, opts, opts])

    # Get pre-splice route.
    inv = l3.rpc.invoice(10000000, 'test_route_by_old_scid', 'test_route_by_old_scid')
    inv2 = l3.rpc.invoice(10000000, 'test_route_by_old_scid2', 'test_route_by_old_scid2')
    route = l1.single_route(l3.info['id'], 10000000, cltv=16)

    # Do a splice
    funds_result = l2.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
    chan_id = l2.get_channel_id(l3)
    result = l2.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l2.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is False)
    result = l2.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is True)
    result = l2.rpc.signpsbt(result['psbt'])
    result = l2.rpc.splice_signed(chan_id, result['signed_psbt'])

    wait_for(lambda: only_one(l2.rpc.listpeerchannels(l3.info['id'])['channels'])['state'] == 'CHANNELD_AWAITING_SPLICE')
    bitcoind.generate_block(6, wait_for_mempool=1)
    wait_for(lambda: only_one(l2.rpc.listpeerchannels(l3.info['id'])['channels'])['state'] == 'CHANNELD_NORMAL')

    # Now l1 tries to send using old scid: should work
    l1.rpc.sendpay(route, inv['payment_hash'], payment_secret=inv['payment_secret'])
    l1.rpc.waitsendpay(inv['payment_hash'])

    # Let's splice again, so the original scid is two behind the times.
    l3.fundwallet(200000)
    funds_result = l3.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
    chan_id = l3.get_channel_id(l2)
    result = l3.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l3.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is False)
    result = l3.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is True)
    result = l3.rpc.signpsbt(result['psbt'])
    result = l3.rpc.splice_signed(chan_id, result['signed_psbt'])

    wait_for(lambda: only_one(l2.rpc.listpeerchannels(l3.info['id'])['channels'])['state'] == 'CHANNELD_AWAITING_SPLICE')
    bitcoind.generate_block(6, wait_for_mempool=1)
    wait_for(lambda: only_one(l2.rpc.listpeerchannels(l3.info['id'])['channels'])['state'] == 'CHANNELD_NORMAL')

    # Now restart l2, make sure it remembers the original!
    l2.restart()
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l2.rpc.connect(l3.info['id'], 'localhost', l3.port)

    wait_for(lambda: only_one(l1.rpc.listpeers()['peers'])['connected'] is True)
    l1.rpc.sendpay(route, inv2['payment_hash'], payment_secret=inv2['payment_secret'])
    l1.rpc.waitsendpay(inv2['payment_hash'])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_sent_sigs_flag(node_factory, bitcoind):
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True,
                                     opts={'may_reconnect': True,
                                           'allow_warning': True})

    chan_id = l1.get_channel_id(l2)

    funds_result = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)

    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True

    # l2 contributed nothing so it signs first.  These two lines bracket that:
    # it has sent tx_signatures and is now waiting for l1's.
    l2.daemon.wait_for_logs([r'peer_out WIRE_TX_SIGNATURES',
                             r'Splice: Awaiting signature message'])
    assert l2.db_query("SELECT count(*) as c FROM channel_funding_inflights;")[0]['c'] == 1

    assert l1.db_query("SELECT i_sent_sigs FROM channel_funding_inflights;")[0]['i_sent_sigs'] == 0
    assert l2.db_query("SELECT i_sent_sigs FROM channel_funding_inflights;")[0]['i_sent_sigs'] == 1


@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_splice_unannounced(node_factory, bitcoind):
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=False)

    chan_id = l1.get_channel_id(l2)

    # add extra sats to pay fee
    funds_result = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is False)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is True)
    result = l1.rpc.signpsbt(result['psbt'])
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])

    l2.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    l1.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')

    bitcoind.generate_block(1, wait_for_mempool=1)

    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_abort_after_sigs_sent(node_factory, bitcoind):
    """tx_abort must not tear down an inflight we have already signed.

    Whoever contributes less to the splice sends tx_signatures first, so with
    l1 contributing every input it is l2 that signs first.  Once l2 has sent
    tx_signatures the shared funding input carries its signature, and the
    inflight -- along with its last_tx/last_sig pair and the splice watcher --
    has to stay put until the splice resolves one way or the other.

    splice_abort() already enforces this for aborts we initiate; check that
    the remote-initiated path agrees.
    """
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True,
                                     opts={'may_reconnect': True,
                                           'allow_warning': True})

    chan_id = l1.get_channel_id(l2)

    funds_result = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)

    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True

    # l2 contributed nothing so it signs first.  These two lines bracket that:
    # it has sent tx_signatures and is now waiting for l1's.
    l2.daemon.wait_for_logs([r'peer_out WIRE_TX_SIGNATURES',
                             r'Splice: Awaiting signature message'])
    assert l2.db_query("SELECT count(*) as c FROM channel_funding_inflights;")[0]['c'] == 1

    # l1 hasn't signed, so its own abort guard lets this through; l2 is the
    # side that has to refuse it.
    l1.rpc.abort_channels([chan_id])

    # Either way l2's channeld goes away here: it either refuses the abort, or
    # it honours it and lightningd tears the inflight down first.  Both lines
    # land after that decision, so the count below isn't racing it.
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort'
                           r'|Peer permanent failure')

    assert l2.db_query("SELECT count(*) as c FROM channel_funding_inflights;")[0]['c'] == 1, \
        "inflight dropped by tx_abort after we had already sent our signature"


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_bad_shared_input_signature(node_factory, bitcoind):
    """A bad shared_input_signature fails the channel when it arrives.

    l2 contributes nothing, so it sends tx_signatures first, here with a
    corrupted shared_input_signature.  Unchecked, l1 only found out when its
    splice tx failed to broadcast.
    """
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True,
                                     opts=[{'may_reconnect': True,
                                            'allow_warning': True},
                                           {'may_reconnect': True,
                                            'allow_warning': True,
                                            'dev-splice-corrupt-shared-sig': None}])

    chan_id = l1.get_channel_id(l2)

    funds_result = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)

    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    with pytest.raises(RpcError, match='Bad shared_input_signature'):
        l1.rpc.splice_update(chan_id, result['psbt'])

    l1.daemon.wait_for_log(r'Peer permanent failure in CHANNELD_NORMAL')
    assert not l1.daemon.is_in_log(r'Broadcasting splice tx')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_bad_shared_input_signature_after_ours(node_factory, bitcoind):
    """As above, but the bad signature arrives after we sent ours.

    l1 sends it this time, so l2 has already signed.  l2 fails the channel.
    l1 holds both good signatures and broadcasts the splice, so the channel
    closes through the commitment that spends it.
    """
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True,
                                     opts=[{'may_reconnect': True,
                                            'allow_warning': True,
                                            'dev-splice-corrupt-shared-sig': None},
                                           {'may_reconnect': True,
                                            'allow_warning': True}])

    chan_id = l1.get_channel_id(l2)

    funds_result = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)

    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])
    # l2's error and l1's broadcast race back to the command.
    try:
        l1.rpc.splice_signed(chan_id, result['signed_psbt'])
    except RpcError:
        pass

    l2.daemon.wait_for_log(r'Peer permanent failure in CHANNELD_.*Bad shared_input_signature')
    assert not l2.daemon.is_in_log(r'Broadcasting splice tx')
    line = l1.daemon.wait_for_log(r'Broadcasting splice tx')
    splice_txid = bitcoind.rpc.decoderawtransaction(line.split('Broadcasting splice tx ')[1].split()[0])['txid']

    # The splice and the commitments on the old funding output conflict, so
    # the one which reached the mempool first confirms.  After the splice, a
    # commitment spending it follows.
    bitcoind.generate_block(1, wait_for_mempool=1)
    block = bitcoind.rpc.getblock(bitcoind.rpc.getbestblockhash(), 2)
    if (splice_txid in [tx['txid'] for tx in block['tx']]
            and not any(i.get('txid') == splice_txid for tx in block['tx'] for i in tx['vin'])):
        bitcoind.generate_block(1, wait_for_mempool=1)
    for n in (l1, l2):
        wait_for(lambda: only_one(n.rpc.listpeerchannels()['channels'])['state'] == 'ONCHAIN')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
@pytest.mark.parametrize("l2_signs_second", [True, False])
def test_splice_bad_input_witness(node_factory, bitcoind, l2_signs_second):
    """A bad witness on the peer's input fails the channel.

    l1 sends its tx_signatures first, with a corrupted witness for its
    wallet input.  If l2 signs second it finds out before sending its own,
    so l1 never gets the shared input signature it needs to complete the
    splice.
    """
    l2_opts = {'may_reconnect': True, 'allow_warning': True}
    if l2_signs_second:
        l2_opts['dev-splice-sign-second'] = None
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True,
                                     opts=[{'may_reconnect': True,
                                            'allow_warning': True,
                                            'dev-splice-corrupt-input-witness': None},
                                           l2_opts])
    chan_id = l1.get_channel_id(l2)

    funds_result = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])
    try:
        l1.rpc.splice_signed(chan_id, result['signed_psbt'])
    except RpcError:
        pass

    l2.daemon.wait_for_log(r'Peer permanent failure in CHANNELD_.*Bad witness for splice tx')
    assert l2.daemon.is_in_log(r'channeld-chan#[0-9]+: peer_out WIRE_TX_SIGNATURES') != l2_signs_second
    assert not l2.daemon.is_in_log(r'Broadcasting splice tx')
    if l2_signs_second:
        return

    # l1 holds a valid splice, and l2's commitments on the old funding
    # output conflict with it, so the one which reached the mempool first
    # confirms.  After the splice, a commitment spending it follows.
    line = l1.daemon.wait_for_log(r'Broadcasting splice tx')
    splice_txid = bitcoind.rpc.decoderawtransaction(line.split('Broadcasting splice tx ')[1].split()[0])['txid']
    bitcoind.generate_block(1, wait_for_mempool=1)
    block = bitcoind.rpc.getblock(bitcoind.rpc.getbestblockhash(), 2)
    if (splice_txid in [tx['txid'] for tx in block['tx']]
            and not any(i.get('txid') == splice_txid for tx in block['tx'] for i in tx['vin'])):
        bitcoind.generate_block(1, wait_for_mempool=1)
    for n in (l1, l2):
        wait_for(lambda: only_one(n.rpc.listpeerchannels()['channels'])['state'] == 'ONCHAIN')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
@pytest.mark.parametrize("corrupt", [False, True])
def test_splice_peer_p2wsh_to_remote_input(node_factory, bitcoind, corrupt):
    """The peer checks the witness of a P2WSH to_remote input."""
    l1_opts = {'may_reconnect': True, 'allow_warning': True}
    l2_opts = {'may_reconnect': True, 'allow_warning': True}
    if corrupt:
        l1_opts['dev-splice-corrupt-input-witness'] = None
        l2_opts['dev-splice-sign-second'] = None
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True,
                                     opts=[l1_opts, l2_opts])
    l3 = node_factory.get_node()
    chan_id = l1.get_channel_id(l2)

    # l3's force close leaves l1 a P2WSH to_remote output.
    node_factory.join_nodes([l1, l3], fundamount=500000)
    l1.stop()
    l3.rpc.close(l1.info['id'], unilateraltimeout=1)
    l3.wait_for_channel_onchain(l1.info['id'])
    bitcoind.generate_block(100)
    l1.start()
    l1.daemon.wait_for_log('onchaind complete, forgetting peer')
    p2wsh = only_one([o for o in l1.rpc.listfunds()['outputs']
                      if o['scriptpubkey'].startswith('0020')])
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    wait_for(lambda: only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])['state'] == 'CHANNELD_NORMAL')

    psbt = l1.rpc.utxopsbt("115000sat", 0, 0,
                           [f"{p2wsh['txid']}:{p2wsh['output']}"],
                           excess_as_change=True)['psbt']
    result = l1.rpc.splice_init(chan_id, 100000, psbt)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])
    if corrupt:
        try:
            l1.rpc.splice_signed(chan_id, result['signed_psbt'])
        except RpcError:
            pass
        l2.daemon.wait_for_log(r'Peer permanent failure in CHANNELD_.*Bad witness for splice tx .*: input [0-9]+ has an invalid signature')
        assert not l2.daemon.is_in_log(r'channeld-chan#[0-9]+: peer_out WIRE_TX_SIGNATURES')
        return
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])

    bitcoind.generate_block(6, wait_for_mempool=result['txid'])
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    assert not l2.daemon.is_in_log(r'Bad witness')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_peer_p2tr_input(node_factory, bitcoind):
    """The peer checks a P2TR key path witness on our input, and accepts it."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True)
    chan_id = l1.get_channel_id(l2)

    # Leave l1 only P2TR outputs to splice in (withdraw keeps the
    # emergency reserve back, as P2TR change).
    l1.rpc.withdraw(l1.rpc.newaddr('p2tr')['p2tr'], 'all')
    bitcoind.generate_block(1, wait_for_mempool=1)
    sync_blockheight(bitcoind, [l1])
    wait_for(lambda: {o['scriptpubkey'][:4] for o in l1.rpc.listfunds()['outputs']
                      if o['status'] == 'confirmed'} == {'5120'})

    # Room for the fee of two P2TR inputs, should coin selection take both.
    funds_result = l1.rpc.fundpsbt("115000sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])

    bitcoind.generate_block(6, wait_for_mempool=result['txid'])
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    assert not l2.daemon.is_in_log(r'Bad witness')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
@pytest.mark.parametrize("closer", ["initiator", "accepter"])
def test_splice_locked_then_force_close(node_factory, bitcoind, closer):
    """A force close after a locked splice must drop our current commitment.

    Once the splice locked, the channel's last_tx moved on with every
    commitment update, but the inflight kept the commitment from the moment
    of the lock.  A unilateral close broadcast that stale inflight commitment
    instead of channel->last_tx, and after any post-lock update it has been
    revoked: our peer would sweep it with a penalty, and our own onchaind,
    not recognising it, took it for a revoked commitment of theirs.
    """
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True,
                                     opts={'allow_warning': True})
    victim, peer = (l1, l2) if closer == "initiator" else (l2, l1)

    chan_id = l1.get_channel_id(l2)

    funds_result = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])

    l2.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    l1.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')

    bitcoind.generate_block(6, wait_for_mempool=result['txid'])

    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    lock_time = bitcoind.rpc.decoderawtransaction(
        victim.rpc.dev_sign_last_tx(peer.info['id'])['tx'])['txid']

    # Any commitment update after the lock revokes the lock-time commitment.
    inv = l2.rpc.invoice(10**7, 'post-lock', 'post-lock')
    l1.rpc.xpay(inv['bolt11'])
    for n in (l1, l2):
        wait_for(lambda: only_one(n.rpc.listpeerchannels()['channels'])['htlcs'] == [])

    current = bitcoind.rpc.decoderawtransaction(
        victim.rpc.dev_sign_last_tx(peer.info['id'])['tx'])['txid']

    # Keep the peer from dropping its own commitment on our error, so ours is
    # the only one that can confirm.
    victim.rpc.disconnect(peer.info['id'], force=True)
    victim.rpc.dev_fail(peer.info['id'])
    victim.daemon.wait_for_log('Peer permanent failure in CHANNELD_NORMAL')
    broadcast = victim.daemon.wait_for_log(r'Broadcasting txid [0-9a-f]{64}')
    assert current != lock_time
    assert lock_time not in broadcast, "broadcast the revoked lock-time commitment"
    assert current in broadcast

    bitcoind.generate_block(1, wait_for_mempool=current)

    victim.daemon.wait_for_log(r'Resolved FUNDING_TRANSACTION/FUNDING_OUTPUT by OUR_UNILATERAL')
    peer.daemon.wait_for_log(r'Resolved FUNDING_TRANSACTION/FUNDING_OUTPUT by THEIR_UNILATERAL')
    assert not victim.daemon.is_in_log('THEIR_REVOKED_UNILATERAL')
    assert not peer.daemon.is_in_log('THEIR_REVOKED_UNILATERAL')
    assert not peer.daemon.is_in_log('OUR_PENALTY_TX')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_unconfirmed_force_close_publishes_current(node_factory, bitcoind):
    """A force close with an unconfirmed splice must publish the live commitment too.

    Until the splice confirms, the channel's own commitment (spending the
    original funding) is the only one that can go on chain; the inflight's
    commitment spends the splice output.  drop_to_chain used to publish the
    inflight's alone whenever an inflight existed, leaving the live channel
    unclosed if the splice never confirmed.
    """
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True,
                                     opts={'may_reconnect': True,
                                           'allow_warning': True})

    chan_id = l1.get_channel_id(l2)
    funding_txid = only_one(l1.rpc.listpeerchannels()['channels'])['funding_txid']

    funds_result = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])
    splice_txid = result['txid']
    l1.daemon.wait_for_log(' to CHANNELD_AWAITING_SPLICE')
    wait_for(lambda: splice_txid in bitcoind.rpc.getrawmempool())

    # The live commitment spends the original funding; the inflight's spends
    # the (unconfirmed) splice output.
    current = bitcoind.rpc.decoderawtransaction(
        l1.rpc.dev_sign_last_tx(l2.info['id'])['tx'])
    assert only_one(current['vin'])['txid'] == funding_txid
    inflight = only_one(only_one(l1.rpc.listpeerchannels()['channels'])['inflight'])
    assert inflight['funding_txid'] == splice_txid

    l1.rpc.dev_fail(l2.info['id'])
    l1.daemon.wait_for_log('Peer permanent failure in CHANNELD_AWAITING_SPLICE')

    # Both commitments are published, not just the inflight's.
    l1.daemon.wait_for_logs([r'Broadcasting txid {}'.format(current['txid']),
                             r'Broadcasting txid {}'.format(inflight['scratch_txid'])])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
@unittest.skipIf(os.getenv('TEST_DB_PROVIDER', 'sqlite3') != 'sqlite3', "modifies database, which is assumed sqlite3")
def test_splice_force_close_uses_splice_remote_fundingkey(node_factory, bitcoind):
    """A force close must sign a splice commitment with the splice's funding key.

    A peer may give a new funding key in splice_init/splice_ack.  Until the
    splice locks, the channel still holds the peer's old key, and signing the
    splice's commitment with it gives a witness that can't spend the splice
    output: a peer that withholds splice_locked leaves us no valid close.
    """
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     announce_channels=False,
                                     opts={'may_reconnect': True,
                                           'allow_warning': True})
    chan_id = l1.get_channel_id(l2)

    funds_result = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])
    splice_txid = l1.rpc.splice_signed(chan_id, result['signed_psbt'])['txid']
    l1.daemon.wait_for_log(' to CHANNELD_AWAITING_SPLICE')
    l2.daemon.wait_for_log(' to CHANNELD_AWAITING_SPLICE')

    # l2 withholds splice_locked while the splice confirms.
    l2.stop()
    bitcoind.generate_block(6, wait_for_mempool=splice_txid)

    # CLN doesn't rotate its funding key, so make the channel's remote key
    # differ from the splice's as if l2 had given a new one for the splice.
    l1.stop()
    l1.db_manip("UPDATE channels SET fundingkey_remote="
                "X'0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798'")
    l1.start()

    inflight = only_one(l1.rpc.dev_sign_last_tx(l2.info['id'])['inflights'])
    assert inflight['funding_txid'] == splice_txid
    commit_txid = bitcoind.rpc.decoderawtransaction(inflight['tx'])['txid']

    l1.rpc.dev_fail(l2.info['id'])
    l1.daemon.wait_for_log('Peer permanent failure')
    bitcoind.generate_block(1, wait_for_mempool=commit_txid)
    l1.daemon.wait_for_log(r'Resolved FUNDING_TRANSACTION/FUNDING_OUTPUT by OUR_UNILATERAL')


@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_candidate_spent_before_lock(node_factory, bitcoind):
    """A confirmed splice output spent before splice_locked must be adopted.

    splice_locked needs both sides; a peer that never sends it and closes
    on the confirmed splice output must still send us to onchaind on that
    output, not on the funding the splice already spent.
    """
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     opts={'may_reconnect': True})
    chan_id = l1.get_channel_id(l2)

    # Fee-bump splice: no inputs, no payout, so the funding is output 0.
    result = l1.rpc.splice_init(chan_id, -5801)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.splice_signed(chan_id, result['psbt'])
    splice_txid = result['txid']
    l1.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    l2.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    wait_for(lambda: bitcoind.rpc.getrawmempool() == [splice_txid])

    # l1 is away while the splice confirms, so it never gets splice_locked.
    l1.stop()
    bitcoind.generate_block(1, wait_for_mempool=splice_txid)

    # l2 closes on the confirmed splice output.
    l2.rpc.close(l1.info['id'], unilateraltimeout=1)
    l2.daemon.wait_for_log(r'to AWAITING_UNILATERAL')
    bitcoind.generate_block(1, wait_for_mempool=1)

    # l1 must go to onchaind on the splice output, not on the old funding.
    l1.start()
    l1.daemon.wait_for_log('Resolved FUNDING_TRANSACTION/FUNDING_OUTPUT by THEIR_UNILATERAL')
    chan = only_one(l1.rpc.listpeerchannels()['channels'])
    assert chan['state'] == 'ONCHAIN'
    assert chan['funding_txid'] == splice_txid


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_restart_watches_funding_spend(node_factory, bitcoind):
    """A restart with a splice unconfirmed must still watch the funding.

    Until the splice confirms, a commitment spending the current funding can
    be mined instead, so after a restart that output needs a spend watch as
    well as the splice candidates.
    """
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     opts=[{'may_reconnect': True},
                                           {'may_reconnect': True,
                                            # We try to cheat!
                                            'broken_log': r"onchaind-chan#[0-9]*: Could not find resolution for output .*: did \*we\* cheat\?"}])
    chan_id = l1.get_channel_id(l2)

    l1.pay(l2, 200000000)
    revoked = l2.rpc.dev_sign_last_tx(l1.info['id'])['tx']
    l2.pay(l1, 100000000)

    # Fee-bump splice: no inputs, no payout.
    result = l1.rpc.splice_init(chan_id, -5801)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    splice_txid = l1.rpc.splice_signed(chan_id, result['psbt'])['txid']
    l1.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    l2.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    wait_for(lambda: bitcoind.rpc.getrawmempool() == [splice_txid])

    l1.restart()

    # The revoked commitment is mined in place of the splice.
    bitcoind.rpc.generateblock(bitcoind.rpc.getnewaddress(), [revoked])
    assert splice_txid not in bitcoind.rpc.getrawmempool()

    l1.daemon.wait_for_log('Resolved FUNDING_TRANSACTION/FUNDING_OUTPUT by THEIR_REVOKED_UNILATERAL')
    l1.wait_for_onchaind_txs(('OUR_PENALTY_TX',
                              'THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM'))


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_peer_aborts_after_tx_complete(node_factory, bitcoind):
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts=[{'may_reconnect': True,
                                            'allow_warning': True},
                                           {'dev-splice-abort-at-tx-complete': None,
                                            'may_reconnect': True,
                                            'allow_warning': True}])
    chan_id = l1.get_channel_id(l2)

    # l2 sends tx_abort after tx_complete, when l1 has signed its side of
    # the splice tx but not sent tx_signatures: that is still allowed.
    funds_result = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    with pytest.raises(RpcError, match='dev-splice-abort-at-tx-complete'):
        result = l1.rpc.splice_update(chan_id, result['psbt'])
        l1.rpc.splice_update(chan_id, result['psbt'])
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_NORMAL channel')
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_NORMAL channel')

    assert not l1.daemon.is_in_log('Peer permanent failure')
    assert l1.db_query("SELECT count(*) as c FROM channel_funding_inflights;")[0]['c'] == 0
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['peer_connected'])
    assert only_one(l1.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL'
    l1.rpc.xpay(l2.rpc.invoice(10000, 'after-abort', 'after-abort')['bolt11'])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_peer_aborts_unsigned_after_reconnect(node_factory, bitcoind, executor):
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts={'may_reconnect': True,
                                           'allow_warning': True})
    chan_id = l1.get_channel_id(l2)

    # l2 never gets l1's last tx_complete, so only l1 stores the inflight,
    # with its signature on the shared input but no tx_signatures sent.
    l1.stop()
    l1.disconnect = ['$WIRE_TX_COMPLETE']
    l1.daemon.opts['dev-disconnect'] = os.path.join(l1.daemon.lightning_dir,
                                                    TEST_NETWORK, 'dev-disconnect')
    l1.start()
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['peer_connected'])
    l1.daemon.wait_for_log(r'billboard: Channel ready for use.')

    funds_result = l1.rpc.addpsbtoutput(100000)
    result = l1.rpc.splice_init(chan_id, -105801, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    executor.submit(l1.rpc.splice_update, chan_id, result['psbt'])
    l1.daemon.wait_for_log(r'dev_disconnect: \$WIRE_TX_COMPLETE')
    wait_for(lambda: l1.db_query("SELECT count(*) as c FROM channel_funding_inflights;")[0]['c'] == 1)

    assert l1.db_query("SELECT i_sent_sigs FROM channel_funding_inflights;")[0]['i_sent_sigs'] == 0
    assert l2.db_query("SELECT count(*) as c FROM channel_funding_inflights;")[0]['c'] == 0

    # On reconnect l2 does not know the splice and answers with tx_abort.
    l1.rpc.disconnect(l2.info['id'], force=True)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l1.daemon.wait_for_log(r'peer_in WIRE_TX_ABORT')
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_NORMAL channel'
                           r'|Peer permanent failure')
    assert not l1.daemon.is_in_log('Peer permanent failure')
    assert not l2.daemon.is_in_log('Peer permanent failure')

    wait_for(lambda: l1.db_query("SELECT count(*) as c FROM channel_funding_inflights;")[0]['c'] == 0)
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['peer_connected'])
    assert only_one(l1.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL'
    l1.rpc.xpay(l2.rpc.invoice(10000, 'after-abort', 'after-abort')['bolt11'])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
@unittest.skipIf(os.getenv('TEST_DB_PROVIDER', 'sqlite3') != 'sqlite3', "modifies database, which is assumed sqlite3")
def test_splice_tx_abort_refused_with_their_sigs(node_factory, bitcoind):
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts={'may_reconnect': True,
                                           'allow_warning': True})
    chan_id = l1.get_channel_id(l2)

    funds_result = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])
    splice_txid = l1.rpc.splice_signed(chan_id, result['signed_psbt'])['txid']
    l1.daemon.wait_for_log(' to CHANNELD_AWAITING_SPLICE')
    l2.daemon.wait_for_log(' to CHANNELD_AWAITING_SPLICE')
    wait_for(lambda: splice_txid in bitcoind.rpc.getrawmempool())

    # l1 holds both signatures, but its inflight says it neither sent nor
    # received tx_signatures, as an older build could leave it.  l2 has
    # forgotten the splice, so it answers l1's next_funding with tx_abort.
    l1.stop()
    l2.stop()
    l1.db_manip("UPDATE channel_funding_inflights SET i_sent_sigs=0,"
                " funding_tx_remote_sigs_received=0")
    l2.db_manip("DELETE FROM htlc_sigs WHERE inflight_tx_id IS NOT NULL")
    l2.db_manip("DELETE FROM channel_funding_inflights")
    l2.db_manip("UPDATE channels SET state=3 WHERE state=13")
    l2.start()
    l1.start()

    l1.daemon.wait_for_log(r'peer_in WIRE_TX_ABORT')
    l1.daemon.wait_for_log(r'tx_abort is not allowed after I have sent my signature'
                           r'|Restarting channeld after tx_abort')
    assert l1.daemon.is_in_log(r'tx_abort is not allowed after I have sent my signature')
    assert only_one(l1.db_query("SELECT count(*) as c FROM channel_funding_inflights;"))['c'] == 1

    # l2 has no record of the splice left to follow it onchain.
    l2.stop()

    # l1 keeps watching the splice, so its close of the splice output
    # resolves as its own.
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 2)
    assert splice_txid in bitcoind.rpc.getrawmempool()
    bitcoind.generate_block(1)
    l1.daemon.wait_for_log(r'Splice candidate {}:[0-9]+ spent before splice_locked; adopting it'.format(splice_txid))
    l1.daemon.wait_for_log(r'Resolved FUNDING_TRANSACTION/FUNDING_OUTPUT by OUR_UNILATERAL')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
@unittest.skipIf(os.getenv('TEST_DB_PROVIDER', 'sqlite3') != 'sqlite3', "modifies database, which is assumed sqlite3")
def test_splice_sent_sigs_marked_on_upgrade(node_factory, bitcoind, executor):
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts={'may_reconnect': True,
                                           'allow_warning': True,
                                           'dev-no-reconnect': None})
    chan_id = l1.get_channel_id(l2)

    # l2 withholds its tx_signatures, so l1 holds no signature of l2's.
    l2.stop()
    l2.disconnect = ['-WIRE_TX_SIGNATURES']
    l2.daemon.opts['dev-disconnect'] = os.path.join(l2.daemon.lightning_dir,
                                                    TEST_NETWORK, 'dev-disconnect')
    l2.start()
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['peer_connected'])

    funds_result = l1.rpc.addpsbtoutput(100000)
    result = l1.rpc.splice_init(chan_id, -105801, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    executor.submit(l1.rpc.splice_update, chan_id, result['psbt'])
    l2.daemon.wait_for_log(r'dev_disconnect: -WIRE_TX_SIGNATURES')
    wait_for(lambda: l1.db_query("SELECT count(*) as c FROM channel_funding_inflights"
                                 " WHERE last_tx IS NOT NULL;")[0]['c'] == 1)
    l1.stop()
    l2.stop()

    # An older release that had sent our tx_signatures here stored the same
    # row, with i_sent_sigs still 0.  Rewind so the upgrade marks it.
    row = only_one(l1.db_query("SELECT i_am_initiator, i_sent_sigs"
                               " FROM channel_funding_inflights;"))
    assert row['i_am_initiator'] == 1 and row['i_sent_sigs'] == 0
    # Migrations appended after it go first: option_independent_secrets'
    # table and column.
    l1.db_manip("DROP TABLE channel_revocation_secrets")
    l1.db_manip("ALTER TABLE channels DROP COLUMN independent_secrets_close_at")
    l1.db_manip("UPDATE version SET version = version - 3")
    l1.daemon.opts['database-upgrade'] = 'true'

    # l2 forgets the splice, so it answers l1's next_funding with tx_abort.
    l2.db_manip("DELETE FROM htlc_sigs WHERE inflight_tx_id IS NOT NULL")
    l2.db_manip("DELETE FROM channel_funding_inflights")
    l2.db_manip("UPDATE channels SET state=3 WHERE state=13")
    del l2.daemon.opts['dev-disconnect']
    l2.start()
    l1.start()
    assert l1.daemon.is_in_log(r'Updating database from version')
    assert only_one(l1.db_query("SELECT i_sent_sigs"
                                " FROM channel_funding_inflights;"))['i_sent_sigs'] == 1
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)

    l1.daemon.wait_for_log(r'peer_in WIRE_TX_ABORT')
    l1.daemon.wait_for_log(r'tx_abort is not allowed after I have sent my signature'
                           r'|Restarting channeld after tx_abort')
    assert l1.daemon.is_in_log(r'tx_abort is not allowed after I have sent my signature')
    assert only_one(l1.db_query("SELECT count(*) as c FROM channel_funding_inflights;"))['c'] == 1


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
@pytest.mark.parametrize("l1_initiates", [True, False])
def test_splice_sigs_sent_after_commit(node_factory, bitcoind, executor, l1_initiates):
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts=[{'may_reconnect': True,
                                            'may_fail': True,
                                            'dev-splice-exit-before-sending-sigs': None},
                                           {'may_reconnect': True}])
    chan_id = l1.get_channel_id(l2)

    # The initiator adds the shared input, so the accepter sends
    # tx_signatures first: l1 sends second when it initiates, first when not.
    if l1_initiates:
        l2_log_start = len(l2.daemon.logs)
        funds_result = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
        result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
        result = l1.rpc.splice_update(chan_id, result['psbt'])
        result = l1.rpc.splice_update(chan_id, result['psbt'])
        assert result['commitments_secured'] is True
        result = l1.rpc.signpsbt(result['psbt'])
        executor.submit(l1.rpc.splice_signed, chan_id, result['signed_psbt'])
    else:
        l1.rpc.xpay(l2.rpc.invoice(300000000, 'push', 'push')['bolt11'])
        l2_log_start = len(l2.daemon.logs)
        funds_result = l2.rpc.addpsbtoutput(100000)
        result = l2.rpc.splice_init(chan_id, -105801, funds_result['psbt'])
        result = l2.rpc.splice_update(chan_id, result['psbt'])
        executor.submit(l2.rpc.splice_update, chan_id, result['psbt'])

    # lightningd exits before replying, so channeld must not send.
    l1.daemon.wait_for_log(r'dev-splice-exit-before-sending-sigs')
    wait_for(lambda: l1.daemon.proc.poll() is not None)
    time.sleep(1)
    assert not l2.daemon.is_in_log(r'channeld-chan#[0-9]*: peer_in WIRE_TX_SIGNATURES',
                                   start=l2_log_start)

    del l1.daemon.opts['dev-splice-exit-before-sending-sigs']
    l1.start()
    l1.daemon.wait_for_log(r'peer_in WIRE_CHANNEL_REESTABLISH')
    l2.daemon.wait_for_log(r'peer_in WIRE_CHANNEL_REESTABLISH')

    bitcoind.generate_block(6, wait_for_mempool=1)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    assert not l1.daemon.is_in_log('Peer permanent failure')
    assert not l2.daemon.is_in_log('Peer permanent failure')
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['peer_connected'])
    assert only_one(l1.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL'
    l1.rpc.xpay(l2.rpc.invoice(10000, 'after-restart', 'after-restart')['bolt11'])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_reserve_changes_at_reestablish(node_factory, bitcoind):
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts={'may_reconnect': True,
                                           'allow_warning': True})
    chan_id = l1.get_channel_id(l2)
    scid = first_scid(l1, l2)
    labels = iter(range(10))

    def l2_pays_l1(amount_msat):
        inv = l1.dev_invoice(amount_msat=amount_msat, label=f'reserve-{next(labels)}',
                             description='reserve', dev_routes=[])
        route = [{'id': l1.info['id'], 'channel': scid, 'amount_msat': amount_msat,
                  'delay': 18, 'style': 'tlv'}]
        l2.rpc.sendpay(route, inv['payment_hash'],
                       payment_secret=inv['payment_secret'],
                       amount_msat=amount_msat)
        return l2.rpc.waitsendpay(inv['payment_hash'], timeout=TIMEOUT)

    l1.rpc.xpay(l2.rpc.invoice(30000000, 'push', 'push')['bolt11'])

    # Splicing in 500,000 sat raises the reserves from 10,000 to 15,000 sat.
    funds_result = l1.rpc.fundpsbt("511722sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 500000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.signpsbt(result['psbt'])
    l1.rpc.splice_signed(chan_id, result['signed_psbt'])
    bitcoind.generate_block(6, wait_for_mempool=1)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    scid = first_scid(l1, l2)

    # Leave l2 with 13,000 sat: above the old reserve, below the new one.
    l2_pays_l1(17000000)

    # A tx_abort restarts channeld without a channel_reestablish.
    result = l1.rpc.splice_init(chan_id, 100000)
    with pytest.raises(RpcError):
        result = l1.rpc.splice_update(chan_id, result['psbt'])
        l1.rpc.splice_update(chan_id, result['psbt'])
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_NORMAL channel')
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_NORMAL channel')
    wait_for(lambda: only_one(l2.rpc.listpeerchannels()['channels'])['peer_connected'])

    # Both nodes still use the old reserves.
    l2_pays_l1(1000000)

    # The new reserves apply from the next channel_reestablish.
    l2.rpc.disconnect(l1.info['id'], force=True)
    l2.rpc.connect(l1.info['id'], 'localhost', l1.port)
    l1.daemon.wait_for_log(r'peer_in WIRE_CHANNEL_REESTABLISH')
    l2.daemon.wait_for_log(r'peer_in WIRE_CHANNEL_REESTABLISH')
    with pytest.raises(RpcError, match=r'Capacity exceeded'):
        l2_pays_l1(1000000)

    # And stay after a later tx_abort.
    result = l1.rpc.splice_init(chan_id, 100000)
    with pytest.raises(RpcError):
        result = l1.rpc.splice_update(chan_id, result['psbt'])
        l1.rpc.splice_update(chan_id, result['psbt'])
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_NORMAL channel')
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_NORMAL channel')
    wait_for(lambda: only_one(l2.rpc.listpeerchannels()['channels'])['peer_connected'])
    with pytest.raises(RpcError, match=r'Capacity exceeded'):
        l2_pays_l1(1000000)


def mature_coinbase(node, bitcoind):
    addr = node.rpc.newaddr('bech32')['bech32']
    coinbase = bitcoind.rpc.getblock(bitcoind.rpc.generatetoaddress(1, addr)[0])['tx'][0]
    bitcoind.generate_block(100)
    sync_blockheight(bitcoind, [node])
    wait_for(lambda: [o for o in node.rpc.listfunds()['outputs'] if o['txid'] == coinbase])
    output = only_one([o for o in node.rpc.listfunds()['outputs'] if o['txid'] == coinbase])
    return f"{coinbase}:{output['output']}"


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_coinbase_input_refused(node_factory, bitcoind):
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True)
    l3 = node_factory.get_node()
    chan_id = l1.get_channel_id(l2)

    outpoint = mature_coinbase(l1, bitcoind)
    psbt = l1.rpc.utxopsbt("111722sat", 0, 0, [outpoint], excess_as_change=True)['psbt']
    with pytest.raises(RpcError, match='coinbase'):
        l1.rpc.splice_init(chan_id, 100000, psbt)

    # Nor in a later update.
    l1.rpc.splice_init(chan_id, 100000)
    with pytest.raises(RpcError, match='coinbase'):
        l1.rpc.splice_update(chan_id, psbt)

    # splicein funds with addpsbtinput, which leaves coinbase outputs out.
    mature_coinbase(l3, bitcoind)
    with pytest.raises(RpcError, match='Could not afford'):
        l3.rpc.addpsbtinput(100000)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_peer_coinbase_input_aborted(node_factory, bitcoind):
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts=[{'dev-splice-coinbase-input': None,
                                            'may_reconnect': True,
                                            'allow_warning': True},
                                           {'may_reconnect': True,
                                            'allow_warning': True}])
    chan_id = l1.get_channel_id(l2)

    outpoint = mature_coinbase(l1, bitcoind)
    psbt = l1.rpc.utxopsbt("111722sat", 0, 0, [outpoint], excess_as_change=True)['psbt']
    result = l1.rpc.splice_init(chan_id, 100000, psbt)
    with pytest.raises(RpcError, match='coinbase'):
        result = l1.rpc.splice_update(chan_id, result['psbt'])
        l1.rpc.splice_update(chan_id, result['psbt'])
    l2.daemon.wait_for_log(r'We are initiating tx_abort for reason: .*coinbase')
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_NORMAL channel')
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_NORMAL channel')

    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['peer_connected'])
    l1.rpc.xpay(l2.rpc.invoice(10000, 'after-abort', 'after-abort')['bolt11'])


def splice_feerate_of(node, txid):
    inflight = only_one([i for i in only_one(node.rpc.listpeerchannels()['channels'])['inflight']
                         if i['funding_txid'] == txid])
    assert inflight['feerate'].endswith('perkw')
    return int(inflight['feerate'][:-len('perkw')])


def peer_splices_in(peer, node, chan_id, bitcoind):
    """peer splices in 100k sats: signed by both sides and in the mempool."""
    # p2tr, like node's wallet: an RBF that is no lighter can then clear
    # bitcoind's absolute fee bump at 25/24 of the feerate.
    peer.fundwallet(500000, addrtype="p2tr")
    txid = peer.rpc.splicein(chan_id, "100000")['txid']
    peer.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    node.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    wait_for(lambda: txid in bitcoind.rpc.getrawmempool())
    return txid


def check_splice_survives(l1, l2, bitcoind, txid):
    """Channel stays up on the one signed splice, which then confirms."""
    assert not l1.daemon.is_in_log('Peer permanent failure')
    assert not l2.daemon.is_in_log('Peer permanent failure')
    for n in (l1, l2):
        wait_for(lambda: only_one(n.rpc.listpeerchannels()['channels'])['peer_connected'])
        chan = only_one(n.rpc.listpeerchannels()['channels'])
        assert chan['state'] == 'CHANNELD_AWAITING_SPLICE'
        assert [i['funding_txid'] for i in chan['inflight']] == [txid]
    l1.rpc.xpay(l2.rpc.invoice(10000, 'after-abort', 'after-abort')['bolt11'])

    bitcoind.generate_block(6, wait_for_mempool=txid)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    for n in (l1, l2):
        chan = only_one(n.rpc.listpeerchannels()['channels'])
        assert chan['funding_txid'] == txid
    l1.rpc.xpay(l2.rpc.invoice(10000, 'after-lock', 'after-lock')['bolt11'])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splicein_peer_refuses_rbf(node_factory, bitcoind):
    """splicein on top of the peer's pending splice is an RBF; when the peer
    refuses it, the abort that follows must leave the peer's splice alone."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts=[{'may_reconnect': True,
                                            'allow_warning': True},
                                           {'dev-splice-abort-at-tx-complete': None,
                                            'may_reconnect': True,
                                            'allow_warning': True}])
    chan_id = l1.get_channel_id(l2)
    splice_txid = peer_splices_in(l2, l1, chan_id, bitcoind)

    with pytest.raises(RpcError, match='dev-splice-abort-at-tx-complete'):
        l1.rpc.splicein(chan_id, "50000")
    l1.daemon.wait_for_log(r'Abort requested with no splice in progress'
                           r'|Peer permanent failure')

    check_splice_survives(l1, l2, bitcoind, splice_txid)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_rbf_user_abort_keeps_signed_splice(node_factory, bitcoind):
    """Aborting an RBF attempt aborts the attempt, not the splice under it."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts={'may_reconnect': True,
                                           'allow_warning': True})
    chan_id = l1.get_channel_id(l2)
    splice_txid = peer_splices_in(l2, l1, chan_id, bitcoind)

    funds = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    l1.rpc.splice_init(chan_id, 100000, funds['psbt'])
    l1.rpc.abort_channels([chan_id])
    l1.daemon.wait_for_log(r'We are initiating tx_abort for reason: requested by user'
                           r'|Peer permanent failure')
    assert not l1.daemon.is_in_log('Peer permanent failure')
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l1.rpc.unreserveinputs(funds['psbt'])

    check_splice_survives(l1, l2, bitcoind, splice_txid)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splicein_feerate_units(node_factory, bitcoind):
    """splicein announces the feerate it pays for, in the right units."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True)
    chan_id = l1.get_channel_id(l2)
    feerate = l1.rpc.feerates('perkw')['perkw']['splice']

    txid = l1.rpc.splicein(chan_id, "100000")['txid']
    for n in (l1, l2):
        inflight = only_one(only_one(n.rpc.listpeerchannels()['channels'])['inflight'])
        assert inflight['funding_txid'] == txid
        assert inflight['feerate'] == '{}perkw'.format(feerate)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_script_rbf_feerate_too_low(node_factory, bitcoind):
    """A script feerate too low to replace the pending splice is refused
    after the plugin has quiesced the channel: its abort only leaves STFU."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts={'may_reconnect': True,
                                           'allow_warning': True})
    chan_id = l1.get_channel_id(l2)
    splice_txid = peer_splices_in(l2, l1, chan_id, bitcoind)
    last_feerate = splice_feerate_of(l1, splice_txid)

    with pytest.raises(RpcError, match='at least {}'.format(last_feerate * 25 // 24)):
        l1.rpc.splice('wallet -> 50000 + fee@{}; 50000 -> {}'.format(last_feerate, chan_id))
    l1.daemon.wait_for_log(r'We are initiating tx_abort for reason: requested by user'
                           r'|Peer permanent failure')
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')

    check_splice_survives(l1, l2, bitcoind, splice_txid)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splicein_rbf_feerate(node_factory, bitcoind):
    """splicein on top of the peer's pending splice bids a valid RBF feerate."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True)
    chan_id = l1.get_channel_id(l2)
    splice_txid = peer_splices_in(l2, l1, chan_id, bitcoind)
    last_feerate = splice_feerate_of(l1, splice_txid)

    next_feerate = only_one(l1.rpc.listpeerchannels()['channels'])['next_feerate']
    assert next_feerate == '{}perkw'.format(last_feerate * 25 // 24)

    rbf_txid = l1.rpc.splicein(chan_id, "50000")['txid']
    l1.daemon.wait_for_log(r'Raising feerate from [0-9]+ to {} perkw to replace'.format(last_feerate * 25 // 24))

    # At least 25/24 of the previous feerate, rounded down.
    for n in (l1, l2):
        assert splice_feerate_of(n, rbf_txid) >= last_feerate * 25 // 24
    wait_for(lambda: bitcoind.rpc.getrawmempool() == [rbf_txid])

    bitcoind.generate_block(6, wait_for_mempool=rbf_txid)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    for n in (l1, l2):
        assert only_one(n.rpc.listpeerchannels()['channels'])['funding_txid'] == rbf_txid


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_rbf_feerate_too_low(node_factory, bitcoind):
    """An RBF below 25/24 of the last splice feerate is refused before we
    contact the peer; without a feerate we pick one that qualifies."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts={'may_reconnect': True,
                                           'allow_warning': True})
    chan_id = l1.get_channel_id(l2)
    splice_txid = peer_splices_in(l2, l1, chan_id, bitcoind)
    last_feerate = splice_feerate_of(l1, splice_txid)
    min_feerate = last_feerate * 25 // 24

    funds = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    l2.daemon.logs_catchup()
    start = len(l2.daemon.logs)
    with pytest.raises(RpcError, match='at least {}'.format(min_feerate)):
        # A bare number is perkb.
        l1.rpc.splice_init(chan_id, 100000, funds['psbt'],
                           feerate_per_kw=(min_feerate - 1) * 4)
    assert not l2.daemon.is_in_log(r'peer_in WIRE_(STFU|TX_INIT_RBF)', start=start)
    assert only_one(l1.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_AWAITING_SPLICE'

    l1.rpc.splice_init(chan_id, 100000, funds['psbt'])
    l1.daemon.wait_for_log(r'splice_init input PSBT version [0-9]+, feerate: {}'.format(min_feerate))
    l2.daemon.wait_for_log(r'peer_in WIRE_TX_INIT_RBF')
    l1.rpc.abort_channels([chan_id])
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l1.rpc.unreserveinputs(funds['psbt'])

    check_splice_survives(l1, l2, bitcoind, splice_txid)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_rbf_feerate_too_low_peer_refuses(node_factory, bitcoind):
    """We answer an RBF below 25/24 of the last splice feerate with tx_abort."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts=[{'dev-splice-rbf-any-feerate': None,
                                            'may_reconnect': True,
                                            'allow_warning': True},
                                           {'may_reconnect': True,
                                            'allow_warning': True}])
    chan_id = l1.get_channel_id(l2)
    splice_txid = peer_splices_in(l2, l1, chan_id, bitcoind)
    last_feerate = splice_feerate_of(l2, splice_txid)

    funds = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    with pytest.raises(RpcError, match='below 25/24 of the last splice feerate_perkw {}'.format(last_feerate)):
        # A bare number is perkb.
        l1.rpc.splice_init(chan_id, 100000, funds['psbt'],
                           feerate_per_kw=last_feerate * 4)
    l2.daemon.wait_for_log(r'We are initiating tx_abort for reason: RBF feerate_perkw')
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l1.rpc.unreserveinputs(funds['psbt'])

    check_splice_survives(l1, l2, bitcoind, splice_txid)


def splice_we_signed_first(node, initiator, bitcoind):
    """initiator splices in; node contributes nothing, so it signs first.
    initiator (run with dev-splice-warn-instead-of-sigs) then sends a warning
    instead of its tx_signatures, leaving node waiting for them."""
    chan_id = initiator.get_channel_id(node)
    initiator.fundwallet(500000, addrtype="p2tr")
    funds = initiator.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    result = initiator.rpc.splice_init(chan_id, 100000, funds['psbt'])
    result = initiator.rpc.splice_update(chan_id, result['psbt'])
    result = initiator.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    node.daemon.wait_for_logs([r'peer_out WIRE_TX_SIGNATURES',
                               r'Splice: Awaiting signature message'])
    result = initiator.rpc.signpsbt(result['psbt'])
    txid = initiator.rpc.splice_signed(chan_id, result['signed_psbt'])['txid']
    node.daemon.wait_for_log(r'Received WARNING .*dev-splice-warn-instead-of-sigs')
    return txid


def wait_splice_resumed(node, peer):
    """After a reconnect, node gets the peer's tx_signatures."""
    wait_for(lambda: only_one(node.rpc.listpeerchannels(peer.info['id'])['channels'])['peer_connected'])
    node.daemon.wait_for_logs([r'Splice negotation, will not send commit, not recv commit,'
                               r' not send signature, recv signature as accepter',
                               r'peer_in WIRE_TX_SIGNATURES'])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_abort_after_we_signed_first(node_factory, bitcoind):
    """Once we have sent tx_signatures we may not tx_abort: the user is told
    so, and a reconnect resumes the signature exchange, after which the
    splice can be aborted (a no-op) or replaced as usual."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts=[{'may_reconnect': True,
                                            'allow_warning': True},
                                           {'dev-splice-warn-instead-of-sigs': None,
                                            'may_reconnect': True,
                                            'allow_warning': True}])
    chan_id = l1.get_channel_id(l2)
    txid = splice_we_signed_first(l1, l2, bitcoind)

    with pytest.raises(RpcError, match='we already sent tx_signatures.*`close` the channel'):
        l1.rpc.abort_channels([chan_id])
    assert not l1.daemon.is_in_log('Peer permanent failure')
    assert not l2.daemon.is_in_log('Peer permanent failure')

    l1.rpc.disconnect(l2.info['id'], force=True)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    wait_splice_resumed(l1, l2)
    l1.rpc.xpay(l2.rpc.invoice(10000, 'resumed', 'resumed')['bolt11'])

    # lightningd knows it has their signatures now, so this isn't refused.
    assert 'refused' not in l1.rpc.abort_channels([chan_id])
    l1.daemon.wait_for_log(r'Abort requested with no splice in progress')
    funds = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    l1.rpc.splice_init(chan_id, 100000, funds['psbt'])
    l2.daemon.wait_for_log(r'peer_out WIRE_TX_ACK_RBF')
    l1.rpc.abort_channels([chan_id])
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l1.rpc.unreserveinputs(funds['psbt'])

    check_splice_survives(l1, l2, bitcoind, txid)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_abort_after_we_signed_first_channeld(node_factory, bitcoind):
    """If an abort reaches channeld after we sent tx_signatures anyway, it
    reconnects to resume the splice instead of failing the channel."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts=[{'dev-splice-abort-after-sigs': None,
                                            'may_reconnect': True,
                                            'allow_warning': True},
                                           {'dev-splice-warn-instead-of-sigs': None,
                                            'may_reconnect': True,
                                            'allow_warning': True}])
    txid = splice_we_signed_first(l1, l2, bitcoind)

    l1.rpc.abort_channels([l1.get_channel_id(l2)])
    l1.daemon.wait_for_log(r'Not aborting splice .*: we already sent tx_signatures,'
                           r' reconnecting to resume it'
                           r'|Peer permanent failure')
    assert not l1.daemon.is_in_log('Peer permanent failure')
    wait_splice_resumed(l1, l2)
    wait_for(lambda: l1.db_query("SELECT funding_tx_remote_sigs_received AS r"
                                 " FROM channel_funding_inflights;") == [{'r': 1}])

    check_splice_survives(l1, l2, bitcoind, txid)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_abort_channels_refuses_one(node_factory, bitcoind):
    """abort_channels skips (and reports) a channel it can't abort, and still
    aborts the others: the splice plugin relies on that to clean up."""
    l1, l2, l3 = node_factory.line_graph(3, fundamount=1000000, wait_for_announce=True,
                                         opts=[{'dev-splice-warn-instead-of-sigs': None,
                                                'may_reconnect': True,
                                                'allow_warning': True},
                                               {'may_reconnect': True,
                                                'allow_warning': True},
                                               {'may_reconnect': True,
                                                'allow_warning': True}])
    chan12 = l2.get_channel_id(l1)
    chan23 = l2.get_channel_id(l3)
    # Before chan12 gets stuck: a block would make l1 send splice_locked,
    # which l2 can't take in STFU, so it would reconnect and resume.
    l2.fundwallet(500000, addrtype="p2tr")
    splice_we_signed_first(l2, l1, bitcoind)

    l2.rpc.stfu_channels([chan23])
    result = l2.rpc.abort_channels([chan12, chan23])
    assert len(result['refused']) == 1
    assert chan12 in result['refused'][0]
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_NORMAL')
    l3.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_NORMAL')

    # A splice script over both fails at stfu (chan12 is mid-splice): the
    # plugin's cleanup aborts chan23 and still reports the stfu failure.
    wait_for(lambda: only_one(l2.rpc.listpeerchannels(l3.info['id'])['channels'])['peer_connected'])
    with pytest.raises(RpcError, match='stfu_channels'):
        l2.rpc.splice('wallet -> 20000; 10000 -> {}; 10000 -> {}'.format(chan12, chan23))
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_NORMAL')
    assert not l2.daemon.is_in_log(r'abort_channels failed')
    assert not l2.daemon.is_in_log('Peer permanent failure')
    wait_for(lambda: only_one(l2.rpc.listpeerchannels(l3.info['id'])['channels'])['peer_connected'])
    l2.rpc.xpay(l3.rpc.invoice(10000, 'after', 'after')['bolt11'])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
@unittest.skipIf(os.getenv('TEST_DB_PROVIDER', 'sqlite3') != 'sqlite3', "modifies database, which is assumed sqlite3")
def test_splice_rbf_tiny_feerate(node_factory, bitcoind):
    """Below 24 perkw, 25/24 rounds back to the same feerate: the next
    one is one more, so such a splice can still be replaced."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts={'may_reconnect': True,
                                           'allow_warning': True})
    chan_id = l1.get_channel_id(l2)
    splice_txid = peer_splices_in(l2, l1, chan_id, bitcoind)

    # As if negotiated with ignore_fee_limits, which accepts down to 1.
    for n in (l1, l2):
        n.stop()
        n.db_manip("UPDATE channel_funding_inflights SET funding_feerate=20;")
    for n in (l1, l2):
        n.start()
    l1.connect(l2)
    for n in (l1, l2):
        wait_for(lambda: only_one(n.rpc.listpeerchannels()['channels'])['peer_connected'])
        assert only_one(n.rpc.listpeerchannels()['channels'])['next_feerate'] == '21perkw'

    funds = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    l1.rpc.splice_init(chan_id, 100000, funds['psbt'])
    l2.daemon.wait_for_log(r'peer_out WIRE_TX_ACK_RBF')
    l1.rpc.abort_channels([chan_id])
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l1.rpc.unreserveinputs(funds['psbt'])

    check_splice_survives(l1, l2, bitcoind, splice_txid)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_init_refused_leaves_no_splice(node_factory, bitcoind):
    """A splice_init channeld refuses leaves nothing behind to abort, and
    doesn't block the next splice."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts={'may_reconnect': True,
                                           'allow_warning': True})
    chan_id = l1.get_channel_id(l2)

    funds = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    with pytest.raises(RpcError, match='Feerate 120000 is too high'):
        # A bare number is perkb.
        l1.rpc.splice_init(chan_id, 100000, funds['psbt'], feerate_per_kw=120000 * 4)

    l1.rpc.abort_channels([chan_id])
    l1.daemon.wait_for_log(r'Abort requested with no splice in progress'
                           r'|We are initiating tx_abort')
    assert not l1.daemon.is_in_log(r'We are initiating tx_abort')

    result = l1.rpc.splice_init(chan_id, 100000, funds['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])
    txid = l1.rpc.splice_signed(chan_id, result['signed_psbt'])['txid']

    bitcoind.generate_block(6, wait_for_mempool=txid)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    assert not l1.daemon.is_in_log('Peer permanent failure')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_rbf_feerate_above_our_max(node_factory, bitcoind):
    """When 25/24 of the pending splice's feerate is more than we'll pay,
    splice_init (and its check) says so before contacting the peer."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts={'may_reconnect': True,
                                           'allow_warning': True})
    chan_id = l1.get_channel_id(l2)
    l2.fundwallet(500000, addrtype="p2tr")
    splice_txid = l2.rpc.splice('wallet -> 100000 + fee@100000; 100000 -> {}'.format(chan_id),
                                force_feerate=True)['txid']
    l1.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    wait_for(lambda: splice_txid in bitcoind.rpc.getrawmempool())
    assert splice_feerate_of(l1, splice_txid) == 100000

    l2.daemon.logs_catchup()
    start = len(l2.daemon.logs)
    with pytest.raises(RpcError, match='Replacing the pending splice needs a feerate of at least 104166'):
        l1.rpc.check(command_to_check='splice_init', channel_id=chan_id, relative_amount=100000)
    funds = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    with pytest.raises(RpcError, match='Replacing the pending splice needs a feerate of at least 104166'):
        l1.rpc.splice_init(chan_id, 100000, funds['psbt'])
    assert not l2.daemon.is_in_log(r'peer_in WIRE_(STFU|TX_INIT_RBF)', start=start)

    l1.rpc.call('splice_init', {'channel_id': chan_id, 'relative_amount': 100000,
                                'initialpsbt': funds['psbt'], 'force_feerate': True})
    l1.daemon.wait_for_log(r'splice_init input PSBT version [0-9]+, feerate: 104166')
    l2.daemon.wait_for_log(r'peer_out WIRE_TX_ACK_RBF')
    l1.rpc.abort_channels([chan_id])
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l1.rpc.unreserveinputs(funds['psbt'])

    check_splice_survives(l1, l2, bitcoind, splice_txid)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_locks_while_peer_awaits_sigs(node_factory, bitcoind):
    """l1's splice confirms while l2 still waits for l1's tx_signatures:
    l2 can't take splice_locked then, so they reconnect, l1 resends its
    signatures, updating the inflight it has already locked, and the splice
    completes."""
    disconnect = ['$WIRE_TX_SIGNATURES']
    # A dual-funded open sends tx_signatures too.
    if EXPERIMENTAL_DUAL_FUND:
        disconnect = ['=WIRE_TX_SIGNATURES'] + disconnect
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts=[{'disconnect': disconnect,
                                            'may_reconnect': True,
                                            'allow_warning': True},
                                           {'may_reconnect': True,
                                            'allow_warning': True}])
    chan_id = l1.get_channel_id(l2)
    l1.fundwallet(500000, addrtype="p2tr")

    # l2 contributes nothing, so signs first; l1's tx_signatures are dropped,
    # leaving l2 waiting for them (too busy to see the splice confirm).
    funds = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 100000, funds['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    l2.daemon.wait_for_logs([r'peer_out WIRE_TX_SIGNATURES',
                             r'Splice: Awaiting signature message'])
    result = l1.rpc.signpsbt(result['psbt'])
    txid = l1.rpc.splice_signed(chan_id, result['signed_psbt'])['txid']

    bitcoind.generate_block(1, wait_for_mempool=txid)
    l1.daemon.wait_for_log(r'peer_out WIRE_SPLICE_LOCKED')
    l2.daemon.wait_for_log(r'Splicing got incorrect message from peer: WIRE_SPLICE_LOCKED')

    # Don't wait for the reconnect backoff.
    def connected():
        if only_one(l2.rpc.listpeerchannels(l1.info['id'])['channels'])['peer_connected']:
            return True
        try:
            l2.rpc.connect(l1.info['id'], 'localhost', l1.port)
        except RpcError:
            pass
        return False
    wait_for(connected)
    l2.daemon.wait_for_logs([r'Splice negotation, will not send commit, not recv commit,'
                             r' not send signature, recv signature as accepter',
                             r'peer_in WIRE_TX_SIGNATURES'])
    if not VALGRIND:
        for n in (l1, l2):
            assert n.rpc.dev_memleak()['leaks'] == []

    bitcoind.generate_block(5)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    for n in (l1, l2):
        assert only_one(n.rpc.listpeerchannels()['channels'])['funding_txid'] == txid
    assert not l1.daemon.is_in_log('Peer permanent failure')
    assert not l2.daemon.is_in_log('Peer permanent failure')
    l1.rpc.xpay(l2.rpc.invoice(10000, 'after', 'after')['bolt11'])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_stale_confirmed_stfu(node_factory, bitcoind):
    """stfu_channels fails on one channel (it is mid-splice) before another
    reports STFU, and a splice_init on that other channel gets in first.
    The late STFU report belongs to nobody: a skip_stfu splice_init uses the
    STFU, and anything else is refused and the STFU ended."""
    # Long enough for the test to get its splice_init in first.
    delay = 10 if VALGRIND or SLOW_MACHINE or os.getenv('ASAN', '0') == '1' else 3
    l1, l2, l3 = node_factory.line_graph(3, fundamount=1000000, wait_for_announce=True,
                                         opts=[{'dev-splice-warn-instead-of-sigs': None,
                                                'may_reconnect': True,
                                                'allow_warning': True},
                                               {'dev-delay-confirmed-stfu': delay,
                                                'may_reconnect': True,
                                                'allow_warning': True},
                                               {'may_reconnect': True,
                                                'allow_warning': True}])
    chan12 = l2.get_channel_id(l1)
    chan23 = l2.get_channel_id(l3)
    l2.fundwallet(500000, addrtype="p2tr")
    splice_we_signed_first(l2, l1, bitcoind)
    funds = l2.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    quiescent = r'{}-channeld-chan#[0-9]+: STFU complete: we are quiescent'.format(l3.info['id'])

    def stfu_fails_on_chan12():
        with pytest.raises(RpcError, match='splice is active'):
            l2.rpc.stfu_channels([chan23, chan12])
        # chan23 is quiescent now, but channeld hasn't said so yet.
        l2.daemon.wait_for_log(quiescent)

    # skip_stfu: this splice_init uses chan23's STFU.  (skip_stfu isn't in
    # the schema, so don't validate against it.)
    stfu_fails_on_chan12()
    LightningRpc.call(l2.rpc, 'splice_init',
                      {'channel_id': chan23, 'relative_amount': 100000,
                       'initialpsbt': funds['psbt'], 'skip_stfu': True})
    l2.daemon.wait_for_log(r'confirmed_stfu for a command which has gone:'
                           r' a skip_stfu splice_init has the channel')
    l2.rpc.abort_channels([chan23])
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_NORMAL')
    wait_for(lambda: only_one(l2.rpc.listpeerchannels(l3.info['id'])['channels'])['peer_connected'])

    # Without it, channeld refuses the splice_init while in STFU, so we
    # end the STFU ourselves.
    stfu_fails_on_chan12()
    with pytest.raises(RpcError, match="Can't begin a splice while currently in STFU"):
        l2.rpc.splice_init(chan23, 100000, funds['psbt'])
    l2.daemon.wait_for_log(r'confirmed_stfu for a command which has gone:'
                           r' a splice command without skip_stfu has the channel,'
                           r' aborting it')
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_NORMAL')

    l2.rpc.unreserveinputs(funds['psbt'])
    assert not l2.daemon.is_in_log('Peer permanent failure')
    wait_for(lambda: only_one(l2.rpc.listpeerchannels(l3.info['id'])['channels'])['peer_connected'])
    l2.rpc.xpay(l3.rpc.invoice(10000, 'after', 'after')['bolt11'])


def hold_splice_broadcast(node):
    """Holds node's next sendrawtransaction until the returned Event is set."""
    release = threading.Event()

    def held_sendraw(r):
        release.wait(TIMEOUT)
        return None

    node.daemon.rpcproxy.mock_rpc('sendrawtransaction', held_sendraw)
    return release


def release_splice_broadcast(node, release):
    """Lets the held broadcast through and waits until lightningd has its result."""
    release.set()
    node.daemon.wait_for_log(r'plugin-bcli: sendrawtx exit 0')
    node.daemon.rpcproxy.mock_rpc('sendrawtransaction', None)
    # The bcli reply follows its log line on the same pipe.
    node.rpc.getinfo()


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_late_broadcast_skips_splice_init(node_factory, bitcoind, executor):
    """Our broadcast of a splice we accepted completes while our own
    splice_init RBF of it waits on the peer: the broadcast must not answer
    the splice_init."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts=[{'dev-splice-rbf-any-feerate': None,
                                            'may_reconnect': True,
                                            'allow_warning': True},
                                           {'disconnect': ['$WIRE_TX_ABORT'],
                                            'may_reconnect': True,
                                            'allow_warning': True}])
    chan_id = l1.get_channel_id(l2)
    release = hold_splice_broadcast(l1)
    splice_txid = peer_splices_in(l2, l1, chan_id, bitcoind)
    last_feerate = splice_feerate_of(l2, splice_txid)

    # l2 refuses the RBF, but its tx_abort is dropped: the splice_init waits.
    funds = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    fut = executor.submit(l1.rpc.splice_init, chan_id, 100000, funds['psbt'],
                          feerate_per_kw=last_feerate * 4)
    l1.daemon.wait_for_log(r'peer_out WIRE_TX_INIT_RBF')
    l2.daemon.wait_for_log(r'We are initiating tx_abort for reason: RBF feerate_perkw')

    release_splice_broadcast(l1, release)
    assert not fut.done()

    # Nothing else answers it while the tx_abort is lost; a restart ends it.
    l1.restart()
    with pytest.raises(Exception):
        fut.result(TIMEOUT)
    l1.rpc.unreserveinputs(funds['psbt'])

    check_splice_survives(l1, l2, bitcoind, splice_txid)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_late_broadcast_skips_stfu_channels(node_factory, bitcoind, executor):
    """Our broadcast of a splice we accepted completes while the splice
    plugin's stfu_channels waits: the broadcast must not answer it, which
    crashed the plugin and with it lightningd."""
    delay = 10 if VALGRIND or SLOW_MACHINE or os.getenv('ASAN', '0') == '1' else 3
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts=[{'dev-delay-confirmed-stfu': delay,
                                            'may_reconnect': True,
                                            'allow_warning': True},
                                           {'may_reconnect': True,
                                            'allow_warning': True}])
    chan_id = l1.get_channel_id(l2)
    release = hold_splice_broadcast(l1)
    splice_txid = peer_splices_in(l2, l1, chan_id, bitcoind)
    last_feerate = splice_feerate_of(l1, splice_txid)

    # channeld holds its confirmed_stfu for `delay` seconds once quiescent.
    fut = executor.submit(l1.rpc.splice,
                          'wallet -> 50000 + fee@{}; 50000 -> {}'.format(last_feerate, chan_id))
    l1.daemon.wait_for_logs([r'STFU initiator local',
                             r'STFU complete: we are quiescent'])

    release_splice_broadcast(l1, release)
    assert not fut.done()

    with pytest.raises(RpcError, match='at least {}'.format(last_feerate * 25 // 24)):
        fut.result(TIMEOUT)
    l1.rpc.getinfo()
    assert [p['active'] for p in l1.rpc.plugin_list()['plugins']
            if p['name'].endswith('/spenderp')] == [True]
    l1.daemon.wait_for_log(r'We are initiating tx_abort for reason: requested by user'
                           r'|Peer permanent failure')
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')

    check_splice_survives(l1, l2, bitcoind, splice_txid)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_broadcast_skips_cmd_awaiting_sigs(node_factory, bitcoind, executor):
    """l2 starts a splice while its channeld waits for l1's tx_signatures:
    l2's broadcast of l1's splice must not answer it."""
    # Keeps l2's stfu_channels pending until its broadcast has completed.
    delay = 10 if VALGRIND or SLOW_MACHINE or os.getenv('ASAN', '0') == '1' else 3
    l1, l2 = node_factory.line_graph(2, fundamount=1000000, wait_for_announce=True,
                                     opts=[{'may_reconnect': True,
                                            'allow_warning': True},
                                           {'dev-delay-confirmed-stfu': delay,
                                            'log-level': ['debug', 'io:jsonrpc'],
                                            'may_reconnect': True,
                                            'allow_warning': True}])
    chan_id = l1.get_channel_id(l2)
    l1.fundwallet(500000, addrtype="p2tr")
    l2.fundwallet(500000, addrtype="p2tr")

    funds = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 100000, funds['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    l2.daemon.wait_for_logs([r'peer_out WIRE_TX_SIGNATURES',
                             r'Splice: Awaiting signature message'])
    feerate = only_one(only_one(l2.rpc.listpeerchannels()['channels'])['inflight'])['feerate']
    last_feerate = int(feerate[:-len('perkw')])

    # Its stfu_channels waits: channeld only reads it after the signatures.
    fut = executor.submit(l2.rpc.splice,
                          'wallet -> 50000 + fee@{}; 50000 -> {}'.format(last_feerate, chan_id))
    l2.daemon.wait_for_log(r'stfu_channels#')
    result = l1.rpc.signpsbt(result['psbt'])
    txid = l1.rpc.splice_signed(chan_id, result['signed_psbt'])['txid']
    wait_for(lambda: txid in bitcoind.rpc.getrawmempool())

    with pytest.raises(RpcError, match='at least {}'.format(last_feerate * 25 // 24)):
        fut.result(TIMEOUT)
    l2.rpc.getinfo()
    assert [p['active'] for p in l2.rpc.plugin_list()['plugins']
            if p['name'].endswith('/spenderp')] == [True]
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')

    check_splice_survives(l1, l2, bitcoind, txid)
