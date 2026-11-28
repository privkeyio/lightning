from fixtures import *  # noqa: F401,F403
import pytest
import unittest
import time
from pyln.testing.utils import EXPERIMENTAL_DUAL_FUND
from pyln.client import RpcError
from utils import (
    TEST_NETWORK, TIMEOUT, only_one, wait_for
)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_disconnect_sig(node_factory, bitcoind):
    # Dual open and splicing both use tx_sig messages. If we have dual enabled, ignore the first one.
    disconnect = ['-WIRE_TX_SIGNATURES']
    if EXPERIMENTAL_DUAL_FUND:
        disconnect = ['=WIRE_TX_SIGNATURES'] + disconnect

    l1 = node_factory.get_node(disconnect=disconnect,
                               options={'dev-no-reconnect': None},
                               may_reconnect=True)
    l2 = node_factory.get_node(may_reconnect=True)
    l1.openchannel(l2, 1000000)

    chan_id = l1.get_channel_id(l2)

    # add extra sats to pay fee
    funds_result = l1.rpc.fundpsbt("107527sat", 0, 0, excess_as_change=True)

    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is False)
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is True)
    result = l1.rpc.signpsbt(result['psbt'])
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])

    l1.daemon.wait_for_log(r'dev_disconnect: \-WIRE_TX_SIGNATURES')
    time.sleep(.2)

    print("Killing l1 without sending WIRE_TX_SIGNATURES")
    l1.daemon.kill()

    psbt_query = "SELECT funding_psbt FROM channel_funding_inflights;"
    psbt = l2.db_query(psbt_query)[0]['funding_psbt']

    # Restart l1, without disconnect stuff.
    del l1.daemon.opts['dev-no-reconnect']
    del l1.daemon.opts['dev-disconnect']

    # Should reconnect, and reestablish the splice.
    l1.start()

    # Wait until nodes are reconnected
    l1.daemon.wait_for_log(r'peer_in WIRE_CHANNEL_REESTABLISH')
    l2.daemon.wait_for_log(r'peer_in WIRE_CHANNEL_REESTABLISH')

    # l2 only receives l1's signatures now, but it still sent its own.
    wait_for(lambda: l2.db_query(psbt_query)[0]['funding_psbt'] != psbt)
    assert l2.db_query("SELECT i_sent_sigs FROM channel_funding_inflights;")[0]['i_sent_sigs'] == 1

    bitcoind.generate_block(6, wait_for_mempool=1)

    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')

    inv = l2.rpc.invoice(10**2, '3', 'no_3')
    l1.rpc.xpay(inv['bolt11'])

    # Check that the splice doesn't generate a unilateral close transaction
    time.sleep(5)
    assert l1.db_query("SELECT count(*) as c FROM channeltxs;")[0]['c'] == 0


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_bad_shared_input_signature_reconnect(node_factory, bitcoind):
    """A bad shared_input_signature retransmitted on reconnect is caught too."""
    disconnect = ['-WIRE_TX_SIGNATURES']
    if EXPERIMENTAL_DUAL_FUND:
        disconnect = ['=WIRE_TX_SIGNATURES'] + disconnect

    l1 = node_factory.get_node(disconnect=disconnect,
                               options={'dev-no-reconnect': None,
                                        'dev-splice-corrupt-shared-sig': None},
                               may_reconnect=True)
    l2 = node_factory.get_node(may_reconnect=True, allow_warning=True)
    l1.openchannel(l2, 1000000)

    chan_id = l1.get_channel_id(l2)

    funds_result = l1.rpc.fundpsbt("107527sat", 0, 0, excess_as_change=True)

    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is True)
    result = l1.rpc.signpsbt(result['psbt'])
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])

    l1.daemon.wait_for_log(r'dev_disconnect: \-WIRE_TX_SIGNATURES')
    time.sleep(.2)
    l1.daemon.kill()

    del l1.daemon.opts['dev-no-reconnect']
    del l1.daemon.opts['dev-disconnect']
    l1.start()

    l2.daemon.wait_for_log(r'peer_in WIRE_CHANNEL_REESTABLISH')
    l2.daemon.wait_for_log(r'Bad shared_input_signature')
    l2.daemon.wait_for_log(r'Peer permanent failure in CHANNELD_')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_disconnect_commit(node_factory, bitcoind, executor):
    l1 = node_factory.get_node(options={'dev-no-reconnect': None},
                               may_reconnect=True)
    # Note: for dual-fund, there's a COMMITMENT_SIGNED for the initial tx, before splicing!
    if EXPERIMENTAL_DUAL_FUND:
        disconnects = ['+WIRE_COMMITMENT_SIGNED*2']
    else:
        disconnects = ['+WIRE_COMMITMENT_SIGNED']
    l2 = node_factory.get_node(disconnect=disconnects,
                               options={'dev-no-reconnect': None},
                               may_reconnect=True)
    l1.openchannel(l2, 1000000)

    chan_id = l1.get_channel_id(l2)

    # add extra sats to pay fee
    funds_result = l1.rpc.fundpsbt("107527sat", 0, 0, excess_as_change=True)

    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert(result['commitments_secured'] is False)

    executor.submit(l1.rpc.splice_update, chan_id, result['psbt'])

    print("l2 waiting for dev_disconnect msg")

    l2.daemon.wait_for_log(r'dev_disconnect: \+WIRE_COMMITMENT_SIGNED')

    l1.daemon.kill()

    # Restart l1, should reconnect
    del l1.daemon.opts['dev-no-reconnect']

    # Should reconnect, and reestablish the splice.
    l1.start()

    # Splice should be abandoned via tx_abort

    # Wait until nodes are reconnected
    l1.daemon.wait_for_log(r'billboard: Channel ready for use.')
    l2.daemon.wait_for_log(r'billboard: Channel ready for use.')

    # Check that the splice doesn't generate a unilateral close transaction
    time.sleep(5)
    assert l1.db_query("SELECT count(*) as c FROM channeltxs;")[0]['c'] == 0


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_init_fails_on_disconnect(node_factory, bitcoind, executor):
    """splice_init returns an error, rather than waiting for ever, if the
    peer drops the connection instead of answering."""
    l1 = node_factory.get_node(options={'dev-no-reconnect': None},
                               may_reconnect=True)
    l2 = node_factory.get_node(disconnect=['-WIRE_SPLICE_ACK'],
                               options={'dev-no-reconnect': None},
                               may_reconnect=True)
    l1.openchannel(l2, 1000000)
    chan_id = l1.get_channel_id(l2)

    funds_result = l1.rpc.fundpsbt("107527sat", 0, 0, excess_as_change=True)
    fut = executor.submit(l1.rpc.splice_init, chan_id, 100000, funds_result['psbt'])

    l2.daemon.wait_for_log(r'dev_disconnect: \-WIRE_SPLICE_ACK')
    l1.daemon.wait_for_log('Peer connection lost')
    with pytest.raises(RpcError, match='Splice command failed'):
        fut.result(TIMEOUT)

    # The channel is untouched, and a splice can be started again.
    del l2.daemon.opts['dev-disconnect']
    l2.restart()
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    wait_for(lambda: only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])['state'] == 'CHANNELD_NORMAL')
    assert only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])['peer_connected']

    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])
    l1.rpc.splice_signed(chan_id, result['signed_psbt'])

    bitcoind.generate_block(6, wait_for_mempool=1)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_init_fails_when_lightningd_stops_channeld(node_factory, bitcoind, executor):
    """splice_init returns an error when lightningd stops channeld itself
    (here a forced disconnect), and a later splice isn't refused as busy."""
    l1 = node_factory.get_node(options={'dev-no-reconnect': None},
                               may_reconnect=True)
    # l2's answer to the splice is lost.
    l2 = node_factory.get_node(disconnect=['$WIRE_SPLICE_ACK'],
                               options={'dev-no-reconnect': None},
                               may_reconnect=True)
    l1.openchannel(l2, 1000000)
    chan_id = l1.get_channel_id(l2)

    funds_result = l1.rpc.fundpsbt("107527sat", 0, 0, excess_as_change=True)
    fut = executor.submit(l1.rpc.splice_init, chan_id, 100000, funds_result['psbt'])
    l2.daemon.wait_for_log(r'dev_disconnect: \$WIRE_SPLICE_ACK')

    l1.rpc.disconnect(l2.info['id'], force=True)
    with pytest.raises(RpcError, match='Splice command failed'):
        fut.result(TIMEOUT)

    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    wait_for(lambda: only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])['peer_connected'])
    assert only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])['state'] == 'CHANNELD_NORMAL'

    # The same splice goes through now.
    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])
    l1.rpc.splice_signed(chan_id, result['signed_psbt'])

    bitcoind.generate_block(6, wait_for_mempool=1)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')


def splice_lost_sigs_nodes(node_factory, l1_opts=None, drops=1):
    """l1 signs a splice first, as it does when l2 adds more than l1 (a
    CLN peer adds nothing, so it signs first, and channeld waits for its
    tx_signatures before splice_update returns).  l1's tx_signatures are
    lost (the first `drops` times it sends them), so l2's never come."""
    # Dual open and splicing both use tx_sig messages. If we have dual
    # enabled, ignore the first one.
    disconnect = ['$WIRE_TX_SIGNATURES'] * drops
    if EXPERIMENTAL_DUAL_FUND:
        disconnect = ['=WIRE_TX_SIGNATURES'] + disconnect
    l1 = node_factory.get_node(disconnect=disconnect,
                               options=dict({'dev-no-reconnect': None,
                                             'dev-splice-sign-first': None},
                                            **(l1_opts or {})),
                               may_reconnect=True)
    l2 = node_factory.get_node(options={'dev-no-reconnect': None,
                                        'dev-splice-sign-second': None},
                               may_reconnect=True)
    l1.openchannel(l2, 1000000)
    return l1, l2


def splice_signed_without_their_sigs(l1, l2, executor):
    """l1 calls splice_signed: channeld saves our signatures, sends them
    (lost) and waits for l2's."""
    chan_id = l1.get_channel_id(l2)
    funds_result = l1.rpc.fundpsbt("107527sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])

    fut = executor.submit(l1.rpc.splice_signed, chan_id, result['signed_psbt'])
    l1.daemon.wait_for_logs([r'dev_disconnect: \$WIRE_TX_SIGNATURES',
                             'Splice: Awaiting signature message'])
    return fut


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_signed_waits_through_disconnect(node_factory, bitcoind, executor):
    """A splice_signed whose signatures channeld has saved isn't failed when
    channeld goes before the peer's tx_signatures arrive: a reconnect resumes
    the splice, and our broadcast answers the command."""
    l1, l2 = splice_lost_sigs_nodes(node_factory)
    fut = splice_signed_without_their_sigs(l1, l2, executor)

    # l2 drops the connection: channeld goes, and the command still waits.
    l2.rpc.disconnect(l1.info['id'], force=True)
    wait_for(lambda: not only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])['peer_connected'])
    time.sleep(1)
    assert not fut.done()
    assert bitcoind.rpc.getrawmempool() == []

    # The reconnect resumes the splice: l1 sends its tx_signatures again, l2
    # sends its own, and our broadcast answers the command.
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    txid = fut.result(TIMEOUT)['txid']
    assert txid in bitcoind.rpc.getrawmempool()

    bitcoind.generate_block(6, wait_for_mempool=txid)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')


SPLICE_MAY_BE_COMMITTED = 364


def splice_signed_may_be_committed(fut, match):
    """splice_signed failed after saving our signatures: code 364, and the
    splice's txid in the data."""
    with pytest.raises(RpcError, match=match) as err:
        fut.result(TIMEOUT)
    assert err.value.error['code'] == SPLICE_MAY_BE_COMMITTED
    txid = err.value.error['data']['txid']
    assert ('the peer may hold our signatures for splice ' + txid
            + ', so it may still confirm') in err.value.error['message']
    return txid


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_signed_sigs_wait_ends(node_factory, bitcoind, executor):
    """If the peer's tx_signatures don't come, that splice_signed fails
    splice-sigs-wait seconds after it saved our signatures, saying the
    splice may still confirm (and it does, when the peer reconnects).  The
    peer stays connected: the bound doesn't wait for channeld to go."""
    l1, l2 = splice_lost_sigs_nodes(node_factory,
                                    {'splice-sigs-wait': 5})
    fut = splice_signed_without_their_sigs(l1, l2, executor)

    txid = splice_signed_may_be_committed(
        fut, r"the peer's signatures have not come in the 5 seconds since"
        r" ours were saved")
    assert only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])['peer_connected']
    assert bitcoind.rpc.getrawmempool() == []

    # The reconnect resumes the splice, and it confirms.
    l1.rpc.disconnect(l2.info['id'], force=True)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    bitcoind.generate_block(6, wait_for_mempool=txid)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_signed_sigs_wait_not_restarted(node_factory, bitcoind, executor):
    """The bound counts from when our signatures were saved: a disconnect
    and reconnect in between don't restart it."""
    # Our tx_signatures are lost again on the reconnect, so the splice
    # doesn't complete before the bound.
    l1, l2 = splice_lost_sigs_nodes(node_factory,
                                    {'splice-sigs-wait': 20}, drops=2)
    fut = splice_signed_without_their_sigs(l1, l2, executor)
    start = time.time()

    time.sleep(12)
    assert not fut.done()
    l1.rpc.disconnect(l2.info['id'], force=True)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l1.daemon.wait_for_log(r'dev_disconnect: \$WIRE_TX_SIGNATURES')

    # Restarted at the reconnect, it would end 32 seconds after the start.
    splice_signed_may_be_committed(fut, r"in the 20 seconds since ours were saved")
    assert time.time() - start < 28


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_signed_fails_may_be_committed(node_factory, bitcoind, executor):
    """A failure no reconnect resumes from, with our signatures saved, says
    the peer may hold them, with code 364 and the txid."""
    l1, l2 = splice_lost_sigs_nodes(node_factory)
    fut = splice_signed_without_their_sigs(l1, l2, executor)

    l1.rpc.dev_fail(l2.info['id'])
    splice_signed_may_be_committed(fut, r'Failing due to dev-fail command')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_signed_answered_by_chain(node_factory, bitcoind, executor):
    """We sign first, and the peer broadcasts the splice without sending us
    its tx_signatures: the splice on chain answers splice_signed."""
    disconnect = ['$WIRE_TX_SIGNATURES']
    if EXPERIMENTAL_DUAL_FUND:
        disconnect = ['=WIRE_TX_SIGNATURES'] + disconnect
    l1 = node_factory.get_node(options={'dev-splice-sign-first': None})
    l2 = node_factory.get_node(disconnect=disconnect,
                               options={'dev-splice-sign-second': None})
    l1.openchannel(l2, 1000000)

    chan_id = l1.get_channel_id(l2)
    funds_result = l1.rpc.fundpsbt("107527sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])

    fut = executor.submit(l1.rpc.splice_signed, chan_id, result['signed_psbt'])
    l2.daemon.wait_for_log(r'dev_disconnect: \$WIRE_TX_SIGNATURES')
    l1.daemon.wait_for_log('Splice: Awaiting signature message')

    # l2 broadcasts; l1 never sees its tx_signatures.
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 1)
    txid = bitcoind.rpc.getrawmempool()[0]
    time.sleep(1)
    assert not fut.done()

    bitcoind.generate_block(1, wait_for_mempool=txid)
    res = fut.result(TIMEOUT)
    assert res['txid'] == txid
    assert bitcoind.rpc.getrawtransaction(txid) == res['tx']


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_signed_peer_aborts_after_warning(node_factory, bitcoind, executor):
    """We sign first, and the peer answers with a warning, which ends our
    wait for its tx_signatures, then with tx_abort, which reaches us outside
    the negotiation.  The peer holds our signatures, so we refuse the abort
    (failing the channel, as within a negotiation), keep the inflight, and
    splice_signed fails with 364, not 362."""
    l1 = node_factory.get_node(options={'dev-splice-sign-first': None},
                               allow_warning=True)
    l2 = node_factory.get_node(options={'dev-splice-sign-second': None,
                                        'dev-splice-abort-instead-of-sigs': None},
                               allow_warning=True)
    l1.openchannel(l2, 1000000)

    chan_id = l1.get_channel_id(l2)
    funds_result = l1.rpc.fundpsbt("107527sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])

    fut = executor.submit(l1.rpc.splice_signed, chan_id, result['signed_psbt'])
    l1.daemon.wait_for_log(r'Received WARNING .*dev-splice-warn-instead-of-sigs')
    splice_signed_may_be_committed(fut, r'tx_abort is not allowed after I have'
                                   r' sent my signature')
    assert l1.db_query("SELECT count(*) as c FROM channel_funding_inflights;")[0]['c'] == 1


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_peer_aborts_before_signing_first(node_factory, bitcoind, executor):
    """The peer signs first, but answers with a warning and then tx_abort
    instead of its tx_signatures.  We sign second, so none of ours went out:
    we take the abort, rather than failing the channel as if they had, and
    drop the splice with it."""
    l1 = node_factory.get_node(options={'dev-splice-sign-second': None},
                               allow_warning=True)
    l2 = node_factory.get_node(options={'dev-splice-sign-first': None,
                                        'dev-splice-abort-instead-of-sigs': None},
                               allow_warning=True)
    l2.openchannel(l1, 1000000)

    chan_id = l2.get_channel_id(l1)
    funds_result = l2.rpc.fundpsbt("107527sat", 0, 0, excess_as_change=True)
    result = l2.rpc.splice_init(chan_id, 100000, funds_result['psbt'])
    result = l2.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is False
    result = l2.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l2.rpc.signpsbt(result['psbt'])

    fut = executor.submit(l2.rpc.splice_signed, chan_id, result['signed_psbt'])
    l1.daemon.wait_for_log(r'Received WARNING .*dev-splice-warn-instead-of-sigs')
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort')
    assert not l1.daemon.is_in_log(r'tx_abort is not allowed')
    with pytest.raises(RpcError):
        fut.result(TIMEOUT)
    assert bitcoind.rpc.getrawmempool() == []

    # l2 takes l1's tx_abort as the ack of its own, not as a new one to
    # ack: a second tx_abort would reach l1's restarted channeld, and
    # connectd would force that one closed.
    l2.daemon.wait_for_log(r'We got TX_ABORT ack')
    l2.daemon.wait_for_log(r'Restarting channeld after tx_abort')
    assert not l2.daemon.is_in_log(r'Send ack of tx_abort')
    for n in (l1, l2):
        wait_for(lambda: only_one(n.rpc.listpeerchannels()['channels'])['peer_connected'])
        assert only_one(n.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL'
    # Neither side's signatures went out, so both drop the splice: if l1
    # kept it, its next commitment_signed would not match l2's.
    for n in (l1, l2):
        assert n.db_query("SELECT count(*) as c FROM channel_funding_inflights;")[0]['c'] == 0
    l2.rpc.xpay(l1.rpc.invoice(10000, 'after-abort', 'after-abort')['bolt11'])
    assert not l1.daemon.is_in_log(r'Activating for message WIRE_TX_ABORT')


@unittest.skipIf(TEST_NETWORK != 'regtest', 'elementsd doesnt yet support PSBT features we need')
def test_splice_sigs_wait_at_least_one(node_factory):
    """A wait of 0 seconds would fail splice_signed before anything could
    answer it, so lightningd refuses it."""
    l1 = node_factory.get_node(start=False)
    l1.daemon.opts['splice-sigs-wait'] = 0
    l1.daemon.start(wait_for_initialized=False, stderr_redir=True)
    assert l1.daemon.wait() == 1
    assert l1.daemon.is_in_stderr(r"'0' must be at least 1")

    l1.daemon.opts['splice-sigs-wait'] = 1
    l1.start()
