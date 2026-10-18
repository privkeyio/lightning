"""option_independent_secrets: a peer's per-commitment secrets need not come
from a shachain.  They still go in one for as long as they fit it, as they
always do from a peer which uses one; from the first which doesn't, we store
every one in the db.

Nodes signal it with --independent-secrets, which is off by default, and it
joins the channel type whenever both peers have it.
--dev-independent-secrets-sender makes a node generate its own
secrets without a shachain, which is how these tests give the receiving side
something no shachain would accept: any first secret fits a shachain, so with
it the db holds every secret from the second on.
"""
from fixtures import *  # noqa: F401,F403
from pyln.client import RpcError
from utils import (
    TEST_NETWORK, only_one, wait_for, sync_blockheight, first_scid,
    expected_peer_features
)

import pytest
import re
import time


pytestmark = pytest.mark.skipif(TEST_NETWORK != 'regtest',
                                reason='option_independent_secrets is for the BLAKE2b network')

OPT_INDEPENDENT_SECRETS = 266

# Offers option_independent_secrets.
ON = {'independent-secrets': None}
# Also generates our own secrets without a shachain.
SENDER = {**ON, 'dev-independent-secrets-sender': None}

# The cheater's own onchaind can't believe what it sees.
CHEATER_OPTS = {'may_fail': True,
                'broken_log': r"onchaind-chan#[0-9]*: Could not find resolution for output .*: did \*we\* cheat\?"}


def channel_type(node, peer):
    return only_one(node.rpc.listpeerchannels(peer.info['id'])['channels'])['channel_type']


def has_independent_secrets(node, peer):
    ctype = channel_type(node, peer)
    has_bit = OPT_INDEPENDENT_SECRETS in ctype['bits']
    assert has_bit == ('independent_secrets/even' in ctype['names'])
    return has_bit


def stored_secrets(node):
    """The numbers of the commitments whose secrets node holds in its db, in
    order: those from the first which didn't fit the shachain."""
    return [r['commitnum'] for r in node.db_query(
        'SELECT commitnum FROM channel_revocation_secrets ORDER BY commitnum;')]


def reported_stored(node, peer):
    """What listpeerchannels says node stores of peer's secrets."""
    return only_one(node.rpc.listpeerchannels(peer.info['id'])['channels']).get('independent_secrets_stored')


def their_revocations(node, peer):
    """How many of peer's commitments node holds the secret for, once they
    outgrew the shachain: what node tells peer in channel_reestablish as
    next_revocation_number."""
    nums = stored_secrets(node)
    # From the first which didn't fit, every one, without gaps.
    assert nums == list(range(nums[0], nums[0] + len(nums)))
    return nums[-1] + 1


def settle(l1, l2):
    """Until both sides have every revocation: then the counts are final."""
    wait_for(lambda: all([only_one(n.rpc.listpeerchannels()['channels'])['htlcs'] == [] for n in (l1, l2)]))


def pay_back_and_forth(l1, l2, rounds):
    """Each payment revokes commitments on both sides."""
    for _ in range(rounds):
        l1.pay(l2, 50_000_000)
        l2.pay(l1, 10_000_000)
    settle(l1, l2)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_negotiated(node_factory, bitcoind):
    """It joins the channel type when both peers signal it, which only
    --independent-secrets does, and not otherwise."""
    l1, l2, l3 = node_factory.get_nodes(3, opts=[ON, ON, {}])

    # Signalled in init and node_announcement with the option, odd.
    assert l1.rpc.getinfo()['our_features']['init'] == expected_peer_features([OPT_INDEPENDENT_SECRETS + 1])
    assert l3.rpc.getinfo()['our_features']['init'] == expected_peer_features()
    for node, offered in ((l1, True), (l3, False)):
        for place in ('init', 'node'):
            bits = int(node.rpc.getinfo()['our_features'][place], 16)
            assert (bits >> (OPT_INDEPENDENT_SECRETS + 1)) & 1 == offered
            assert (bits >> OPT_INDEPENDENT_SECRETS) & 1 == 0
    assert l1.rpc.listconfigs('independent-secrets')['configs']['independent-secrets']['set'] is True
    assert l3.rpc.listconfigs('independent-secrets')['configs']['independent-secrets']['set'] is False

    node_factory.join_nodes([l1, l2])
    node_factory.join_nodes([l1, l3])
    assert has_independent_secrets(l1, l2)
    assert has_independent_secrets(l2, l1)
    assert not has_independent_secrets(l1, l3)
    assert not has_independent_secrets(l3, l1)

    # A caller-supplied channel_type gets it too: it changes nothing but
    # which secrets the receiver accepts.
    l2.fundwallet(10**7)
    ret = l2.rpc.fundchannel(l1.info['id'], 10**6, channel_type=[12, 22])
    assert OPT_INDEPENDENT_SECRETS in ret['channel_type']['bits']
    # Confirm its change, for the next.
    bitcoind.generate_block(1, wait_for_mempool=1)
    sync_blockheight(bitcoind, [l2])

    # And a node which doesn't offer it refuses it (fundchannel uses
    # openchannel_init with dual-funding, fundchannel_start without).
    l2.rpc.connect(l3.info['id'], 'localhost', l3.port)
    with pytest.raises(RpcError, match=r'Did not support channel_type'):
        l2.rpc.fundchannel(l3.info['id'], 10**6,
                           channel_type=[12, 22, 514, OPT_INDEPENDENT_SECRETS])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_shachain_channel_unchanged(node_factory):
    """Without the feature, the peer's secrets go in the shachain as before,
    and none in the new table."""
    l1, l2 = node_factory.line_graph(2)
    assert not has_independent_secrets(l1, l2)
    pay_back_and_forth(l1, l2, 3)
    assert stored_secrets(l1) == []
    assert stored_secrets(l2) == []
    # Not reported without the add-on.
    assert reported_stored(l1, l2) is None


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_shachain_peer(node_factory):
    """A peer which uses a shachain, as every node does unless it has a
    reason not to, sends secrets which fit one: on a channel with the
    add-on they stay in the shachain, so nothing is stored individually and
    a static channel backup still holds them all
    (test_emergencyrecoverpenaltytxn[True] punishes with one)."""
    l1, l2 = node_factory.line_graph(2, opts={**ON, 'may_reconnect': True})
    assert has_independent_secrets(l1, l2)
    pay_back_and_forth(l1, l2, 3)
    l1.restart()
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL')
    pay_back_and_forth(l1, l2, 1)
    assert stored_secrets(l1) == []
    assert stored_secrets(l2) == []
    assert reported_stored(l1, l2) == 0
    assert reported_stored(l2, l1) == 0


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_stored(node_factory):
    """Once the peer's secrets outgrow the shachain, each one it reveals is
    stored, and they survive a restart and reconnection, both ways round."""
    l1, l2 = node_factory.line_graph(2, opts={**SENDER, 'may_reconnect': True})
    assert has_independent_secrets(l1, l2)

    pay_back_and_forth(l1, l2, 3)
    # The first secret fits a shachain, the second can't.
    assert stored_secrets(l1)[0] == 1
    assert stored_secrets(l2)[0] == 1
    n1, n2 = their_revocations(l1, l2), their_revocations(l2, l1)
    # listpeerchannels counts them: all but the first, which fit.
    assert reported_stored(l1, l2) == len(stored_secrets(l1)) == n1 - 1
    assert reported_stored(l2, l1) == len(stored_secrets(l2)) == n2 - 1

    # channel_reestablish carries the count and the last secret from the
    # db; each side checks the last secret is the one it revealed.
    l1.restart()
    l2.restart()
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL')
    wait_for(lambda: only_one(l2.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL')
    assert not l1.daemon.is_in_log('bad reestablish')
    assert not l2.daemon.is_in_log('bad reestablish')

    l2.rpc.disconnect(l1.info['id'], force=True)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    pay_back_and_forth(l1, l2, 2)
    assert their_revocations(l1, l2) > n1
    assert their_revocations(l2, l1) > n2
    # Including after a restart.
    assert reported_stored(l1, l2) == their_revocations(l1, l2) - 1
    assert reported_stored(l2, l1) == their_revocations(l2, l1) - 1


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_kept_without_the_option(node_factory, bitcoind):
    """A channel opened with the add-on keeps it, and keeps storing and
    punishing with the peer's secrets, after the node restarts without
    --independent-secrets, which then only stops new channels using it."""
    l1, l2 = node_factory.line_graph(2, opts=[{**CHEATER_OPTS, **SENDER, 'may_reconnect': True},
                                              {**ON, 'may_reconnect': True}])
    pay_back_and_forth(l1, l2, 2)
    n = their_revocations(l2, l1)

    del l2.daemon.opts['independent-secrets']
    l2.restart()
    assert (int(l2.rpc.getinfo()['our_features']['init'], 16) >> (OPT_INDEPENDENT_SECRETS + 1)) & 1 == 0
    l2.rpc.connect(l1.info['id'], 'localhost', l1.port)
    wait_for(lambda: only_one(l2.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL')
    assert has_independent_secrets(l2, l1)

    l1.pay(l2, 100_000_000)
    settle(l1, l2)
    old_tx = l1.rpc.dev_sign_last_tx(l2.info['id'])['tx']
    old_num = their_revocations(l2, l1)
    pay_back_and_forth(l1, l2, 2)
    assert old_num > n
    assert old_num in stored_secrets(l2)
    assert reported_stored(l2, l1) == their_revocations(l2, l1) - 1

    bitcoind.rpc.sendrawtransaction(old_tx)
    bitcoind.generate_block(1)
    l2.daemon.wait_for_log(' to ONCHAIN')
    ((_, txid, blocks),) = l2.wait_for_onchaind_txs(
        ('OUR_PENALTY_TX', 'THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM'))
    assert blocks == 0
    bitcoind.generate_block(1, wait_for_mempool=txid)
    l2.daemon.wait_for_log(r'Resolved THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM by our proposal OUR_PENALTY_TX')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_need_the_feature(node_factory, executor):
    """A sender whose secrets don't form a shachain fails a channel without
    option_independent_secrets: that is what the feature is for, and it shows
    the other tests really do give the receiver unrelated secrets."""
    l1, l2 = node_factory.line_graph(2, opts=[SENDER, {}])
    assert not has_independent_secrets(l2, l1)

    # Any first secret starts a shachain, but the second has to derive it.
    # A payment revokes two of l1's commitments.
    inv = l2.rpc.invoice(10_000_000, 'test', 'test')['bolt11']
    executor.submit(l1.rpc.xpay, inv)
    l2.daemon.wait_for_log(r'Bad per_commitment_secret [0-9a-f]{64} for 1')
    wait_for(lambda: only_one(l2.rpc.listpeerchannels()['channels'])['state'] == 'AWAITING_UNILATERAL')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_cap(node_factory, bitcoind):
    """Once a channel stores max-independent-secrets of the peer's secrets,
    we start a mutual close, after a warning at 90%.  A payment from l1
    revokes two of its commitments, and l2 stores those from commitment 1 on,
    so after n payments it stores 2n - 1."""
    l1, l2 = node_factory.line_graph(2, opts=[SENDER,
                                              {**ON, 'max-independent-secrets': 20}])
    assert l2.rpc.listconfigs()['configs']['max-independent-secrets']['value_int'] == 20
    assert l1.rpc.listconfigs()['configs']['max-independent-secrets']['value_int'] == 1000000

    for _ in range(10):
        l1.pay(l2, 10_000_000)
    settle(l1, l2)
    assert reported_stored(l2, l1) == 19
    # Once, at 18.
    l2.daemon.wait_for_log(r'Stored 18 of the peer.s per-commitment secrets:'
                           r' at max-independent-secrets=20 we will close the channel')
    assert not l2.daemon.is_in_log(r'Stored 19 of the peer.s per-commitment secrets:')
    assert only_one(l2.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL'

    # The 20th starts the close, and the payment in flight still completes:
    # settling it stores one more, within the 10% we allow for that.
    l1.pay(l2, 10_000_000)
    l2.daemon.wait_for_logs([r'Stored 20 of the peer.s per-commitment secrets,'
                             r' max-independent-secrets=20: closing the channel',
                             r'State changed from CHANNELD_NORMAL to CHANNELD_SHUTTING_DOWN'])
    bitcoind.generate_block(1, wait_for_mempool=1)
    for n in (l1, l2):
        wait_for(lambda: only_one(n.rpc.listpeerchannels()['channels'])['state'] == 'ONCHAIN')
    closed = only_one(l2.rpc.listpeerchannels()['channels'])
    assert closed['independent_secrets_stored'] == 21
    assert 'Reached max-independent-secrets' in [s['message'] for s in closed['state_changes']]
    assert not l2.daemon.is_in_log('closing it unilaterally')


def test_independent_secrets_cap_unilateral(node_factory, bitcoind, executor):
    """A peer which keeps a closing channel updating has it closed
    unilaterally once we store 10% more than max-independent-secrets (at
    least one more).  With a cap of 10, settling the payment in flight when
    the close starts is already one too many."""
    l1, l2 = node_factory.line_graph(2, opts=[{**SENDER, 'may_reconnect': True},
                                              {**ON, 'max-independent-secrets': 10,
                                               'may_reconnect': True}])
    for _ in range(5):
        l1.pay(l2, 10_000_000)
    settle(l1, l2)
    assert reported_stored(l2, l1) == 9

    # It never settles cleanly: the channel closes with it on.
    inv = l2.rpc.invoice(10_000_000, 'last', 'last')['bolt11']
    executor.submit(l1.rpc.xpay, inv)
    l2.daemon.wait_for_logs([r'Stored 10 of the peer.s per-commitment secrets,'
                             r' max-independent-secrets=10: closing the channel',
                             r'Stored 11 of the peer.s per-commitment secrets,'
                             r' max-independent-secrets=10, and the channel still'
                             r" isn't closed: closing it unilaterally"])
    l2.wait_for_channel_onchain(l1.info['id'])
    closed = only_one(l2.rpc.listpeerchannels()['channels'])
    assert 'Stored too many per-commitment secrets' in [s['message'] for s in closed['state_changes']]


def test_independent_secrets_refused_options(node_factory):
    """--independent-secrets can't be used with an external signer, which
    checks the peer's secrets against a shachain, and the dev sender needs it,
    since only a peer with the add-on accepts its secrets."""
    for opts, err in (({**ON, 'subdaemon': 'hsmd:lightning_hsmd'},
                       r"--independent-secrets can't be used with an external signer"),
                      ({'dev-independent-secrets-sender': None},
                       r'--dev-independent-secrets-sender needs --independent-secrets')):
        node = node_factory.get_node(start=False, expect_fail=True, options=opts)
        node.daemon.start(wait_for_initialized=False, stderr_redir=True)
        assert node.daemon.wait() == 1
        assert node.daemon.is_in_stderr(err)

    # Without the option, replacing hsmd is fine.
    node_factory.get_node(options={'subdaemon': 'hsmd:lightning_hsmd'})


def test_independent_secrets_external_signer_channels(node_factory):
    """A channel keeps the add-on after --independent-secrets is removed, so
    an external signer is refused while it is open, even without the option."""
    l1, l2 = node_factory.line_graph(2, opts=[{**ON, 'may_fail': True}, ON])
    assert has_independent_secrets(l1, l2)
    l1.stop()
    del l1.daemon.opts['independent-secrets']
    l1.daemon.opts['subdaemon'] = 'hsmd:lightning_hsmd'
    l1.daemon.start(wait_for_initialized=False, stderr_redir=True)
    assert l1.daemon.wait() == 1
    assert l1.daemon.is_in_stderr(r'Channel [0-9a-f]{64} has option_independent_secrets,'
                                  r" which can't be used with an external signer")


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_sender_cannot_flip(node_factory):
    """--dev-independent-secrets-sender changes how hsmd derives every
    channel's secrets, so a node with a channel won't start with it flipped:
    the channel would fail, and our own unilateral close couldn't be swept."""
    l1, l2 = node_factory.line_graph(2, opts=[{**ON, 'may_fail': True, 'may_reconnect': True},
                                              {**ON, 'may_reconnect': True}])
    l1.stop()
    l1.daemon.opts['dev-independent-secrets-sender'] = None
    l1.daemon.start(wait_for_initialized=False, stderr_redir=True)
    assert l1.daemon.wait() == 1
    assert l1.daemon.is_in_stderr(r'Our channels were made with'
                                  r' --dev-independent-secrets-sender unset')

    del l1.daemon.opts['dev-independent-secrets-sender']
    l1.start()
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL')
    l1.pay(l2, 10_000_000)


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@pytest.mark.parametrize("restart", [False, True])
def test_independent_secrets_penalty(node_factory, bitcoind, executor, restart):
    """When a peer broadcasts a revoked commitment, we take everything,
    using the secret it revealed for that commitment, from the db.

    l1 cheats, so l1 is the one whose secrets l2 stores."""
    l1, l2 = node_factory.line_graph(2, opts=[{**CHEATER_OPTS, **SENDER}, ON])
    assert has_independent_secrets(l2, l1)
    scid = first_scid(l1, l2)

    l1.pay(l2, 100_000_000)
    settle(l1, l2)
    old_tx = l1.rpc.dev_sign_last_tx(l2.info['id'])['tx']
    old_num = their_revocations(l2, l1)

    # Revoke that one, and plenty after it.
    pay_back_and_forth(l1, l2, 4)
    revocations = their_revocations(l2, l1)
    assert revocations > old_num + 4
    # So its secret is one of those in the db.
    assert old_num in stored_secrets(l2)

    if restart:
        l2.restart()

    bitcoind.rpc.sendrawtransaction(old_tx)
    bitcoind.generate_block(1)
    l2.daemon.wait_for_log(' to ONCHAIN')
    wait_for(lambda: l2.is_local_channel_active(scid) is False)
    wait_for(lambda: l2.daemon.is_in_log(f'commitnum = {old_num}, revocations_received = {revocations}'))

    if restart:
        # onchaind is started again from the db, and gets the secret again.
        l2.restart()

    ((_, txid, blocks),) = l2.wait_for_onchaind_txs(
        ('OUR_PENALTY_TX', 'THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM'))
    assert blocks == 0
    bitcoind.generate_block(1, wait_for_mempool=txid)
    l2.daemon.wait_for_log(r'Resolved THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM by our proposal OUR_PENALTY_TX')

    # Once that is irrevocable, the channel is forgotten, and we keep only
    # the last secret, for channel_reestablish.
    last = stored_secrets(l2)[-1]
    bitcoind.generate_block(100)
    sync_blockheight(bitcoind, [l2])
    wait_for(lambda: l2.rpc.listpeerchannels()['channels'] == [])
    assert stored_secrets(l2) == [last]

    closed = only_one(l2.rpc.listclosedchannels()['closedchannels'])
    assert OPT_INDEPENDENT_SECRETS in closed['channel_type']['bits']


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_penalty_from_shachain(node_factory, bitcoind):
    """Once a peer's secrets outgrow the shachain, those before stay in it:
    a revoked commitment whose secret is one of them is punished from the
    shachain.  With the dev sender only the first fits, so l1 broadcasts
    commitment 0."""
    l1, l2 = node_factory.line_graph(2, opts=[{**CHEATER_OPTS, **SENDER}, ON])
    commitment_0 = l1.rpc.dev_sign_last_tx(l2.info['id'])['tx']
    pay_back_and_forth(l1, l2, 2)
    revocations = their_revocations(l2, l1)
    assert stored_secrets(l2)[0] == 1

    bitcoind.rpc.sendrawtransaction(commitment_0)
    bitcoind.generate_block(1)
    l2.daemon.wait_for_log(' to ONCHAIN')
    wait_for(lambda: l2.daemon.is_in_log(f'commitnum = 0, revocations_received = {revocations}'))
    ((_, txid, blocks),) = l2.wait_for_onchaind_txs(
        ('OUR_PENALTY_TX', 'THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM'))
    assert blocks == 0
    bitcoind.generate_block(1, wait_for_mempool=txid)
    l2.daemon.wait_for_log(r'Resolved THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM by our proposal OUR_PENALTY_TX')


def test_independent_secrets_penalty_htlc(node_factory, bitcoind, executor):
    """As test_penalty_inhtlc: the revoked commitment has an HTLC on it, which
    we take as well."""
    opts = {'dev-disable-commit-after': 1, **SENDER}
    l1, l2 = node_factory.line_graph(2, opts=[{**opts, **CHEATER_OPTS,
                                               'feerates': (7500, 7500, 7500, 7500)},
                                              opts])
    assert has_independent_secrets(l2, l1)

    t = executor.submit(l1.pay, l2, 100000000)
    l1.daemon.wait_for_log('dev-disable-commit-after: disabling')
    l2.daemon.wait_for_log('dev-disable-commit-after: disabling')
    l1.daemon.wait_for_log('got commitsig')
    old_tx = l1.rpc.dev_sign_last_tx(l2.info['id'])['tx']

    l1.rpc.dev_reenable_commit(l2.info['id'])
    l2.rpc.dev_reenable_commit(l1.info['id'])
    t.result(timeout=30)
    settle(l1, l2)

    bitcoind.rpc.sendrawtransaction(old_tx)
    bitcoind.generate_block(1)
    l2.daemon.wait_for_log(' to ONCHAIN')

    ((_, txid1, blocks1), (_, txid2, blocks2)) = \
        l2.wait_for_onchaind_txs(('OUR_PENALTY_TX',
                                  'THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM'),
                                 ('OUR_PENALTY_TX',
                                  'THEIR_REVOKED_UNILATERAL/THEIR_HTLC'))
    assert blocks1 == 0
    assert blocks2 == 0
    bitcoind.generate_block(1, wait_for_mempool=[txid1, txid2])
    l2.daemon.wait_for_logs([r'Resolved THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM by our proposal OUR_PENALTY_TX',
                             r'Resolved THEIR_REVOKED_UNILATERAL/THEIR_HTLC by our proposal OUR_PENALTY_TX'])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_their_unilateral(node_factory, bitcoind):
    """Their latest commitment isn't revoked: no penalty, just our output,
    and no complaint about a missing secret."""
    l1, l2 = node_factory.line_graph(2, opts=SENDER)
    pay_back_and_forth(l1, l2, 2)

    l1.rpc.dev_fail(l2.info['id'])
    l1.wait_for_channel_onchain(l2.info['id'])
    bitcoind.generate_block(1)
    l2.daemon.wait_for_log(r'Their unilateral tx, (old|new) commit point')
    assert not l2.daemon.is_in_log('No per-commitment secret stored')
    assert not l2.daemon.is_in_log('OUR_PENALTY_TX')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_closed_channel_reestablish(node_factory, bitcoind):
    """As test_reestablish_closed_channels: long after l1 has forgotten the
    channel, it still answers l2's reestablish with the last secret l2
    revealed, which is all it keeps of them, and l2 checks it."""
    l1, l2 = node_factory.line_graph(2, opts=[{**ON,
                                               'may_reconnect': True,
                                               'dev-no-reconnect': None},
                                              {**SENDER,
                                               'may_reconnect': True,
                                               'dev-no-reconnect': None}])

    # We block l2 from seeing close, so it will try to reestablish.
    def no_new_blocks(req):
        return {"error": {"code": -8, "message": "Block height out of range"}}
    l2.daemon.rpcproxy.mock_rpc('getblockhash', no_new_blocks)

    pay_back_and_forth(l1, l2, 2)
    n = their_revocations(l1, l2)

    l1.rpc.disconnect(l2.info['id'], force=True)
    l1.rpc.close(l2.info['id'], unilateraltimeout=1)
    bitcoind.generate_block(5, wait_for_mempool=1)
    bitcoind.generate_block(100, wait_for_mempool=1)
    wait_for(lambda: l1.rpc.listclosedchannels()['closedchannels'] != [])
    assert stored_secrets(l1) == [n - 1]

    # Closed channels are loaded at startup too.
    l1.restart()

    try:
        l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    except RpcError as err:
        assert "disconnected during connection" in err.error['message']
    l1.daemon.wait_for_log('Responded to reestablish for long-closed channel')
    l2.daemon.wait_for_log('peer_in WIRE_CHANNEL_REESTABLISH')
    l2.daemon.wait_for_log('peer_in WIRE_ERROR')
    assert not l2.daemon.is_in_log('bad reestablish')
    assert not l1.daemon.is_in_log('cannot get')

    l2.daemon.rpcproxy.mock_rpc('getblockhash', None)


def test_independent_secrets_cap_lowered(node_factory, bitcoind):
    """A limit lowered below what a channel already stores starts a mutual
    close at the channel's next update, and the 10% we allow before closing
    unilaterally counts from there, so the payment in flight still settles."""
    l1, l2 = node_factory.line_graph(2, opts=[{**SENDER, 'may_reconnect': True},
                                              {**ON, 'may_reconnect': True}])
    for _ in range(15):
        l1.pay(l2, 10_000_000)
    settle(l1, l2)
    assert reported_stored(l2, l1) == 29

    l2.stop()
    l2.daemon.opts['max-independent-secrets'] = 20
    l2.start()
    for n, peer in ((l1, l2), (l2, l1)):
        wait_for(lambda: only_one(n.rpc.listpeerchannels(peer.info['id'])['channels'])['peer_connected'])
        wait_for(lambda: only_one(n.rpc.listpeerchannels(peer.info['id'])['channels'])['state'] == 'CHANNELD_NORMAL')

    # 30 starts the close, and settling the payment stores 31: within the
    # slack of 2, counted from 30 rather than from 20.
    l1.pay(l2, 10_000_000)
    l2.daemon.wait_for_logs([r'Stored 30 of the peer.s per-commitment secrets,'
                             r' max-independent-secrets=20: closing the channel',
                             r'State changed from CHANNELD_NORMAL to CHANNELD_SHUTTING_DOWN'])
    bitcoind.generate_block(1, wait_for_mempool=1)
    for n in (l1, l2):
        wait_for(lambda: only_one(n.rpc.listpeerchannels()['channels'])['state'] == 'ONCHAIN')
    assert only_one(l2.rpc.listpeerchannels()['channels'])['independent_secrets_stored'] == 31
    assert not l2.daemon.is_in_log('closing it unilaterally')


def test_independent_secrets_cap_close_timeout(node_factory, executor):
    """If the mutual close started at the limit doesn't finish in time, we
    close the channel unilaterally, as `close` does.  Here the peer
    disconnects rather than answer our shutdown, and doesn't come back."""
    l1, l2 = node_factory.line_graph(2, opts=[{**SENDER, 'may_reconnect': True,
                                               'dev-no-reconnect': None,
                                               'disconnect': ['-WIRE_SHUTDOWN']},
                                              {**ON, 'max-independent-secrets': 20,
                                               'dev-independent-secrets-close-timeout': 10,
                                               'may_reconnect': True,
                                               'dev-no-reconnect': None}])
    for _ in range(10):
        l1.pay(l2, 10_000_000)
    settle(l1, l2)
    assert reported_stored(l2, l1) == 19

    # It never settles: the peer is gone.
    executor.submit(l1.pay, l2, 10_000_000)
    l2.daemon.wait_for_logs([r'Stored 20 of the peer.s per-commitment secrets,'
                             r' max-independent-secrets=20: closing the channel',
                             r'The mutual close started at max-independent-secrets'
                             r" hasn't finished in 10 seconds: closing the channel"
                             r' unilaterally'])
    l2.wait_for_channel_onchain(l1.info['id'])
    closed = only_one(l2.rpc.listpeerchannels()['channels'])
    assert 'Mutual close at max-independent-secrets timed out' in [s['message'] for s in closed['state_changes']]


@pytest.mark.openchannel('v2')
def test_independent_secrets_cap_lease(node_factory, bitcoind, executor):
    """We don't start a mutual close of a channel the peer leased from us
    before the lease expires, but the backstop still applies: once the peer
    has made us store 10% more than the limit, we close unilaterally."""
    lease = {'funder-policy': 'match', 'funder-policy-mod': 100,
             'lease-fee-base-sat': '100sat', 'lease-fee-basis': 100,
             'may_reconnect': True}
    l1, l2 = node_factory.get_nodes(2, opts=[{**SENDER, **lease},
                                             {**ON, **lease, 'max-independent-secrets': 10}])
    amount = 500000
    l1.fundwallet(20000000)
    l2.fundwallet(20000000)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    rates = l1.rpc.dev_queryrates(l2.info['id'], amount, amount)
    # l1 leases a channel from l2.
    l1.rpc.fundchannel(l2.info['id'], amount, request_amt=amount,
                       feerate='2000perkw', compact_lease=rates['compact_lease'])
    bitcoind.generate_block(6)
    for n, peer in ((l1, l2), (l2, l1)):
        wait_for(lambda: only_one(n.rpc.listpeerchannels(peer.info['id'])['channels'])['state'] == 'CHANNELD_NORMAL')
    assert has_independent_secrets(l2, l1)

    # On this dual-funded channel, l2 stores 2n of l1's secrets after n
    # payments.
    for _ in range(4):
        l1.pay(l2, 10_000_000)
    settle(l1, l2)
    assert reported_stored(l2, l1) == 8

    # 10 reaches the limit, but the lease holds off the close.
    l1.pay(l2, 10_000_000)
    settle(l1, l2)
    l2.daemon.wait_for_log(r'Reached max-independent-secrets=10, but the peer'
                           r' leased this channel until block [0-9]+: closing'
                           r' after that, or unilaterally if it has us store'
                           r' another 1 first')
    assert reported_stored(l2, l1) == 10
    assert only_one(l2.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL'

    # With a limit of 10 the slack is 1, so the next one closes it.
    executor.submit(l1.pay, l2, 10_000_000)
    l2.daemon.wait_for_log(r'Stored 11 of the peer.s per-commitment secrets,'
                           r' max-independent-secrets=10, and the channel still'
                           r" isn't closed: closing it unilaterally")
    assert not l2.daemon.is_in_log(r'max-independent-secrets=10: closing the channel')
    l2.wait_for_channel_onchain(l1.info['id'])


def test_independent_secrets_cap_close_restart(node_factory, executor):
    """A restart resets neither the backstop nor the timeout of the mutual
    close we started at the limit: we save how many we stored when it
    started, and when it started is in the channel's state changes."""
    l1, l2 = node_factory.line_graph(2, opts=[{**SENDER, 'may_reconnect': True,
                                               'dev-no-reconnect': None,
                                               'disconnect': ['-WIRE_SHUTDOWN']},
                                              {**ON, 'max-independent-secrets': 20,
                                               'dev-independent-secrets-close-timeout': 30,
                                               'may_reconnect': True,
                                               'dev-no-reconnect': None}])
    for _ in range(10):
        l1.pay(l2, 10_000_000)
    settle(l1, l2)
    executor.submit(l1.pay, l2, 10_000_000)
    l2.daemon.wait_for_log(r'Stored 20 of the peer.s per-commitment secrets,'
                           r' max-independent-secrets=20: closing the channel')
    assert only_one(l2.db_query('SELECT independent_secrets_close_at FROM channels'))['independent_secrets_close_at'] == 20

    time.sleep(10)
    l2.restart()
    # Logged while starting up, before the log search begins.
    line = l2.daemon.is_in_log(r'The mutual close started at max-independent-secrets'
                               r' has [0-9]+ seconds left')
    assert int(re.search(r'has ([0-9]+) seconds left', line).group(1)) <= 20
    l2.daemon.wait_for_log(r"hasn't finished in 30 seconds: closing the channel unilaterally")
    l2.wait_for_channel_onchain(l1.info['id'])


def test_independent_secrets_cap_not_our_close(node_factory, executor):
    """Only a close we started at the limit is timed out by it.  Here the
    user closes the channel with no timeout while the peer is away, and the
    limit is lowered below what the channel stores before a restart: the
    close keeps waiting."""
    l1, l2 = node_factory.line_graph(2, opts=[{**SENDER, 'may_reconnect': True},
                                              {**ON, 'may_reconnect': True,
                                               'dev-no-reconnect': None,
                                               'dev-independent-secrets-close-timeout': 5}])
    for _ in range(5):
        l1.pay(l2, 10_000_000)
    settle(l1, l2)
    assert reported_stored(l2, l1) == 9

    l1.stop()
    executor.submit(l2.rpc.close, l1.info['id'], 0)
    wait_for(lambda: only_one(l2.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_SHUTTING_DOWN')

    l2.stop()
    l2.daemon.opts['max-independent-secrets'] = 5
    l2.start()
    time.sleep(10)
    assert only_one(l2.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_SHUTTING_DOWN'
    assert not l2.daemon.is_in_log('seconds left')
    assert not l2.daemon.is_in_log('closing the channel unilaterally')
