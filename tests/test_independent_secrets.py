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
    TEST_NETWORK, TIMEOUT, only_one, wait_for, sync_blockheight, first_scid,
    expected_peer_features
)

from pathlib import Path
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
    the other tests really do give the receiver unrelated secrets.  The dev
    sender only opens such a channel with --dev-independent-secrets-any-channel
    (see test_independent_secrets_sender_needs_the_add_on)."""
    l1, l2 = node_factory.line_graph(2, opts=[{**SENDER, 'dev-independent-secrets-any-channel': None}, {}])
    assert not has_independent_secrets(l2, l1)

    # Any first secret starts a shachain, but the second has to derive it.
    # A payment revokes two of l1's commitments.
    inv = l2.rpc.invoice(10_000_000, 'test', 'test')['bolt11']
    executor.submit(l1.rpc.xpay, inv)
    l2.daemon.wait_for_log(r'Bad per_commitment_secret [0-9a-f]{64} for 1')
    wait_for(lambda: only_one(l2.rpc.listpeerchannels()['channels'])['state'] == 'AWAITING_UNILATERAL')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_sender_needs_the_add_on(node_factory):
    """BOLT 3: a node whose secrets don't come from the seed MUST include
    option_independent_secrets in the channel_type it proposes, and MUST NOT
    accept a channel without it.  The dev sender keeps to that, both ways."""
    l1, l2, l3 = node_factory.get_nodes(3, opts=[SENDER, {}, ON])
    for n in (l2, l3):
        l1.fundwallet(10**7)
        n.fundwallet(10**7)
        l1.rpc.connect(n.info['id'], 'localhost', n.port)

    # It doesn't open a channel the peer can't give the add-on, and `check`
    # of the command that would open it says so too.
    if l1.config('experimental-dual-fund'):
        psbt = l1.rpc.fundpsbt(10**6, 'normal', 0, reserve=0)['psbt']
        to_check = {'command_to_check': 'openchannel_init',
                    'id': l2.info['id'], 'amount': 10**6,
                    'initialpsbt': psbt}
    else:
        to_check = {'command_to_check': 'fundchannel_start',
                    'id': l2.info['id'], 'amount': 10**6}
    with pytest.raises(RpcError, match=r"a channel needs option_independent_secrets"):
        l1.rpc.check(**to_check)
    with pytest.raises(RpcError, match=r"a channel needs option_independent_secrets"):
        l1.rpc.fundchannel(l2.info['id'], 10**6)

    # Nor accept one without it.
    with pytest.raises(RpcError):
        l2.rpc.fundchannel(l1.info['id'], 10**6)
    l1.daemon.wait_for_log(r"a channel needs option_independent_secrets")

    # With a peer that has it, the add-on joins, so it opens.
    l1.rpc.fundchannel(l3.info['id'], 10**6)
    assert has_independent_secrets(l1, l3)


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
    # Below what BOLT 3 recommends, so we say so at startup.
    assert l2.daemon.is_in_log(r'max-independent-secrets=20 is below 1,000,000,'
                               r' the lowest limit BOLT 3 recommends: an honest peer'
                               r' may reach it, and have its channel closed'
                               r' unilaterally once it has us store another 2')
    assert not l1.daemon.is_in_log(r'is below 1,000,000')

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


def test_independent_secrets_cap_withheld(node_factory, bitcoind, executor):
    """The backstop on a channel whose funding we withheld: failing it
    forgets the channel outright (there is nothing to broadcast), so nothing
    may touch it afterwards.  It stays forgotten across a restart."""
    plugin_path = Path(__file__).parent / "plugins" / "zeroconf-selective.py"
    l1, l2 = node_factory.get_nodes(2, opts=[{**ON, 'max-independent-secrets': 10,
                                              'may_reconnect': True},
                                             {**SENDER, 'plugin': str(plugin_path),
                                              'zeroconf_allow': 'any',
                                              'may_reconnect': True}])
    l1.fundwallet(10**7)
    l1.connect(l2)
    amount = 1000000
    funding_addr = l1.rpc.fundchannel_start(l2.info['id'], f"{amount}sat", mindepth=0)['funding_address']
    psbt = l1.rpc.fundpsbt(amount, "1000perkw", 1000, excess_as_change=True)['psbt']
    psbt = l1.rpc.addpsbtoutput(amount, psbt, destination=funding_addr)['psbt']
    assert l1.rpc.fundchannel_complete(l2.info['id'], psbt, withhold=True)['commitments_secured']
    assert only_one(l1.rpc.listpeerchannels()['channels'])['funding']['withheld'] is True
    assert has_independent_secrets(l1, l2)
    wait_for(lambda: 'remote' in only_one(l1.rpc.listpeerchannels()['channels'])['updates'])

    # A payment has l2 revoke two commitments, and l1 stores those from
    # commitment 1 on.
    for i in range(5):
        l1.rpc.xpay(l2.rpc.invoice(10_000_000, f'w{i}', f'w{i}')['bolt11'])
    settle(l1, l2)
    assert reported_stored(l1, l2) == 9

    # The 10th starts the close, and settling the payment in flight is one
    # too many: the channel is failed, and as it was withheld, forgotten.
    inv = l2.rpc.invoice(10_000_000, 'last', 'last')['bolt11']
    executor.submit(l1.rpc.xpay, inv)
    l1.daemon.wait_for_logs([r'Stored 10 of the peer.s per-commitment secrets,'
                             r' max-independent-secrets=10: closing the channel',
                             r"and the channel still isn't closed: closing it unilaterally",
                             r'Withheld channel: not sending a close transaction'])
    wait_for(lambda: l1.rpc.listpeerchannels()['channels'] == [])
    assert bitcoind.rpc.getrawmempool() == []

    # Nothing wrote the forgotten channel back.
    l1.restart()
    assert l1.rpc.listpeerchannels()['channels'] == []


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
def test_independent_secrets_penalty_we_opened(node_factory, bitcoind):
    """The same with the roles swapped: l1 opens the channel and stores
    l2's secrets, and l2 cheats.  The commitment number is unmasked with
    the opener's payment basepoint first, which only a channel we opened
    tells apart from one we accepted."""
    l1, l2 = node_factory.line_graph(2, opts=[ON, {**CHEATER_OPTS, **SENDER}])
    assert only_one(l1.rpc.listpeerchannels()['channels'])['opener'] == 'local'
    assert has_independent_secrets(l1, l2)

    # Give the cheater a balance, and a revoked state to broadcast.
    l1.pay(l2, 200_000_000)
    settle(l1, l2)
    old_tx = l2.rpc.dev_sign_last_tx(l1.info['id'])['tx']
    old_num = their_revocations(l1, l2)

    pay_back_and_forth(l1, l2, 4)
    revocations = their_revocations(l1, l2)
    assert revocations > old_num + 4
    assert old_num in stored_secrets(l1)

    bitcoind.rpc.sendrawtransaction(old_tx)
    bitcoind.generate_block(1)
    l1.daemon.wait_for_log(' to ONCHAIN')
    wait_for(lambda: l1.daemon.is_in_log(f'commitnum = {old_num}, revocations_received = {revocations}'))
    ((_, txid, blocks),) = l1.wait_for_onchaind_txs(
        ('OUR_PENALTY_TX', 'THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM'))
    assert blocks == 0
    bitcoind.generate_block(1, wait_for_mempool=txid)
    l1.daemon.wait_for_log(r'Resolved THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM by our proposal OUR_PENALTY_TX')


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


@pytest.mark.parametrize("how", ["overdue", "clock_back"])
def test_independent_secrets_cap_close_restart_late(node_factory, executor, how):
    """After a restart, the mutual close we started at the limit gets a
    tenth of its timeout, up to 10 minutes (so 10 minutes by default), to
    finish, even if we were down for longer than the whole timeout, so the
    peer can reconnect;
    and a clock which has gone back counts as no time having passed."""
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

    l2.stop()
    if how == "overdue":
        time.sleep(32)
        expect = 3
    else:
        l2.daemon.env['CLN_DEV_SET_TIME'] = str(int(time.time()) - 3600)
        expect = 30
    l2.start()
    assert l2.daemon.is_in_log(r'The mutual close started at max-independent-secrets'
                               r' has {} seconds left'.format(expect))
    l2.daemon.wait_for_log(r"hasn't finished in 30 seconds: closing the channel unilaterally")
    l2.wait_for_channel_onchain(l1.info['id'])


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_cap_lowered_during_splice(node_factory, bitcoind):
    """A limit lowered below what a channel stores while it can't close
    mutually (here, a splice is pending) counts the backstop from where the
    channel first reached it, rather than failing the channel at its next
    update; the mutual close starts once the splice locks."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True,
                                     opts=[{**ON, 'may_reconnect': True},
                                           {**SENDER, 'may_reconnect': True}])
    while (reported_stored(l1, l2) or 0) < 29:
        pay_direct(l1, l2, 10_000_000)
        settle(l1, l2)
    assert reported_stored(l1, l2) == 29

    splice_txid = splice_in(l1, l2)
    l1.stop()
    l1.daemon.opts['max-independent-secrets'] = 20
    l1.start()
    wait_for(lambda: only_one(l1.rpc.listpeers()['peers'])['connected'])
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['peer_connected'])

    # Two more: the first finds the limit reached, and the slack (2) counts
    # from there.
    pay_direct(l1, l2, 10_000_000)
    settle(l1, l2)
    assert reported_stored(l1, l2) == 31
    l1.daemon.wait_for_log('Reached max-independent-secrets=20, but a splice is pending')
    assert only_one(l1.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_AWAITING_SPLICE'
    assert not l1.daemon.is_in_log('closing it unilaterally')

    bitcoind.generate_block(6, wait_for_mempool=splice_txid)
    l1.daemon.wait_for_logs([r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL',
                             r'max-independent-secrets=20: closing the channel'])
    l1.daemon.wait_for_log(r'to CLOSINGD_COMPLETE')
    assert not l1.daemon.is_in_log('closing it unilaterally')


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


@pytest.mark.parametrize("offered", [True, False])
def test_independent_secrets_even_bit_in_init(node_factory, offered):
    """lightning-blake2b/bolts#17, BOLT 3: a receiver of bit 266 in init MUST
    close the connection, whether or not it offers the option (when it
    does, it knows the bit, so the generic check would let it through)."""
    l1, l2 = node_factory.get_nodes(2, opts=[
        ON if offered else {},
        {'dev-force-features': '+266', 'may_fail': True},
    ])

    with pytest.raises(RpcError):
        l2.rpc.connect(l1.info['id'], 'localhost', l1.port)

    if offered:
        l1.daemon.wait_for_log('Feature 266 is only valid in channel_type')
    else:
        l1.daemon.wait_for_log('Unsupported feature 266')
    assert l1.rpc.listpeers(l2.info['id'])['peers'] == []

    # A peer setting only the odd bit connects.
    l3 = node_factory.get_node(options=ON)
    l3.rpc.connect(l1.info['id'], 'localhost', l1.port)
    wait_for(lambda: only_one(l1.rpc.listpeers(l3.info['id'])['peers'])['connected'])


def pay_direct(src, dst, amt):
    """Like LightningNode.pay, but also while a splice is pending, when the
    channel is in CHANNELD_AWAITING_SPLICE rather than CHANNELD_NORMAL."""
    label = 'direct-{}'.format(time.time())
    inv = dst.rpc.invoice(amt, label, label)
    scid = only_one(src.rpc.listpeerchannels(dst.info['id'])['channels'])['short_channel_id']
    route = [{'amount_msat': amt, 'id': dst.info['id'], 'delay': 5, 'channel': scid}]
    src.rpc.sendpay(route, inv['payment_hash'], payment_secret=inv['payment_secret'])
    # A payment caught in a channel that closes would wait for ever.
    src.rpc.waitsendpay(inv['payment_hash'], timeout=TIMEOUT)


def splice_in(l1, l2, amount=100000):
    """l1 splices amount into its channel with l2, leaving the splice
    pending: its transaction is in the mempool, unconfirmed."""
    chan_id = l1.get_channel_id(l2)
    funds = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
    result = l1.rpc.splice_init(chan_id, amount, funds['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    result = l1.rpc.splice_update(chan_id, result['psbt'])
    assert result['commitments_secured'] is True
    result = l1.rpc.signpsbt(result['psbt'])
    result = l1.rpc.splice_signed(chan_id, result['signed_psbt'])
    for n in (l1, l2):
        n.daemon.wait_for_log(r'CHANNELD_NORMAL to CHANNELD_AWAITING_SPLICE')
    return result['txid']


def closed_on(bitcoind):
    """The funding transaction whose output the last block's one
    transaction spends: what a mutual close just mined closed."""
    block = bitcoind.rpc.getblock(bitcoind.rpc.getbestblockhash(), 2)
    close = only_one(block['tx'][1:])
    return only_one(close['vin'])['txid']


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
@pytest.mark.parametrize("locked", [False, True])
def test_independent_secrets_penalty_spliced(node_factory, bitcoind, locked):
    """A revoked commitment on a splice's funding output is punished with
    the secret we stored for its number: one secret revokes that number on
    every funding output.  Either while the splice is pending (the cheater
    broadcasts its commitment on the new funding, spending the splice
    transaction) or after it locked."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True,
                                     opts=[{**CHEATER_OPTS, **SENDER}, ON])
    assert has_independent_secrets(l2, l1)
    l1.pay(l2, 100_000_000)
    settle(l1, l2)

    splice_txid = splice_in(l1, l2)
    if locked:
        bitcoind.generate_block(6, wait_for_mempool=splice_txid)
        for n in (l1, l2):
            n.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
        l1.pay(l2, 10_000_000)
        settle(l1, l2)
        old_tx = l1.rpc.dev_sign_last_tx(l2.info['id'])['tx']
    else:
        pay_direct(l1, l2, 10_000_000)
        settle(l1, l2)
        # Its commitment on the splice's funding output.
        old_tx = only_one(l1.rpc.dev_sign_last_tx(l2.info['id'])['inflights'])['tx']
    old_num = their_revocations(l2, l1)
    # The cheater's balance in that commitment: while the splice is pending,
    # listpeerchannels still counts the old funding, without the splice-in.
    cheater_sat = int(only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])['to_us_msat']) // 1000
    if not locked:
        cheater_sat += 100000
    # Either way it is a commitment on the splice's funding output.
    decoded = bitcoind.rpc.decoderawtransaction(old_tx)
    assert only_one(decoded['vin'])['txid'] == splice_txid

    # Revoke it, and some after it.
    for _ in range(3):
        pay_direct(l1, l2, 50_000_000)
        pay_direct(l2, l1, 10_000_000)
    settle(l1, l2)
    assert old_num in stored_secrets(l2)
    revocations = their_revocations(l2, l1)

    bitcoind.rpc.sendrawtransaction(old_tx)
    bitcoind.generate_block(1, wait_for_mempool=bitcoind.rpc.decoderawtransaction(old_tx)['txid'])
    l2.daemon.wait_for_log(' to ONCHAIN')
    wait_for(lambda: l2.daemon.is_in_log(f'commitnum = {old_num}, revocations_received = {revocations}'))
    ((_, txid, blocks),) = l2.wait_for_onchaind_txs(
        ('OUR_PENALTY_TX', 'THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM'))
    assert blocks == 0

    # The penalty takes the cheater's to_local: its balance, less what it
    # pays for the commitment as opener (the fee and the anchors).
    penalty = bitcoind.rpc.getrawtransaction(txid, True)
    spent = only_one(penalty['vin'])
    assert spent['txid'] == decoded['txid']
    to_local_sat = round(decoded['vout'][spent['vout']]['value'] * 10**8)
    assert cheater_sat - 10_000 < to_local_sat <= cheater_sat
    swept_sat = sum(round(o['value'] * 10**8) for o in penalty['vout'])
    assert to_local_sat - 10_000 < swept_sat < to_local_sat

    bitcoind.generate_block(1, wait_for_mempool=txid)
    l2.daemon.wait_for_log(r'Resolved THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM by our proposal OUR_PENALTY_TX')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_cap_during_splice(node_factory, bitcoind):
    """Reaching max-independent-secrets while a splice is pending doesn't
    send shutdown, which BOLT 2 doesn't allow then: the mutual close starts
    once the splice locks."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True,
                                     opts=[{**ON, 'max-independent-secrets': 20},
                                           SENDER])
    splice_txid = splice_in(l1, l2)

    # Each payment has l2 revoke a commitment or two.
    while (reported_stored(l1, l2) or 0) < 20:
        pay_direct(l1, l2, 10_000_000)
        settle(l1, l2)
    # Within the backstop's 10% (2 here).
    assert reported_stored(l1, l2) < 22

    l1.daemon.wait_for_log('Reached max-independent-secrets=20, but a splice'
                           ' is pending')
    chan = only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])
    assert chan['state'] == 'CHANNELD_AWAITING_SPLICE'
    assert not l1.daemon.is_in_log('closing the channel')

    bitcoind.generate_block(6, wait_for_mempool=splice_txid)
    l1.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l1.daemon.wait_for_logs([r'CHANNELD_NORMAL to CHANNELD_SHUTTING_DOWN',
                             r'max-independent-secrets=20: closing the channel'])
    l1.daemon.wait_for_log(r'to CLOSINGD_COMPLETE')
    bitcoind.generate_block(1, wait_for_mempool=1)
    l1.daemon.wait_for_log(r'to ONCHAIN')
    assert not l1.daemon.is_in_log('closing it unilaterally')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_cap_splice_aborted(node_factory, bitcoind):
    """An aborted splice has the limit checked again, so a close it held up
    starts straight away rather than at the next revocation.  Here the limit
    was lowered while l1 was down, so nothing has checked it when l1 starts a
    splice and aborts it."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True,
                                     opts=[{**ON, 'may_reconnect': True},
                                           {**SENDER, 'may_reconnect': True}])
    chan_id = l1.get_channel_id(l2)
    while (reported_stored(l1, l2) or 0) < 21:
        pay_direct(l1, l2, 10_000_000)
        settle(l1, l2)

    l1.stop()
    l1.daemon.opts['max-independent-secrets'] = 20
    l1.start()
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['peer_connected'])
    assert not l1.daemon.is_in_log('closing the channel')

    funds = l1.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
    l1.rpc.splice_init(chan_id, 100000, funds['psbt'])
    l1.rpc.abort_channels([chan_id])
    l1.daemon.wait_for_logs([r'Restarting channeld after tx_abort on CHANNELD_NORMAL',
                             r'max-independent-secrets=20: closing the channel',
                             r'CHANNELD_NORMAL to CHANNELD_SHUTTING_DOWN'])
    l1.daemon.wait_for_log(r'to CLOSINGD_COMPLETE')
    assert not l1.daemon.is_in_log('closing it unilaterally')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_cap_splice_rbf_aborted(node_factory, bitcoind):
    """Aborting an RBF attempt drops only the attempt: the splice it would
    have replaced is still pending, so a close waiting at the limit keeps
    waiting until that splice locks."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True,
                                     opts=[{**ON, 'max-independent-secrets': 20},
                                           SENDER])
    chan_id = l1.get_channel_id(l2)
    # A second confirmed output, to fund the RBF attempt with.
    l1.fundwallet(200000)
    splice_txid = splice_in(l1, l2)
    while (reported_stored(l1, l2) or 0) < 20:
        pay_direct(l1, l2, 10_000_000)
        settle(l1, l2)
    l1.daemon.wait_for_log('Reached max-independent-secrets=20, but a splice'
                           ' is pending')

    funds = l1.rpc.fundpsbt("109000sat", 0, 0, excess_as_change=True)
    l1.rpc.splice_init(chan_id, 100000, funds['psbt'])
    l2.daemon.wait_for_log(r'peer_in WIRE_TX_INIT_RBF')
    l1.rpc.abort_channels([chan_id])
    l1.daemon.wait_for_log(r'Restarting channeld after tx_abort on CHANNELD_AWAITING_SPLICE')
    l1.rpc.unreserveinputs(funds['psbt'])

    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['peer_connected'])
    chan = only_one(l1.rpc.listpeerchannels()['channels'])
    assert chan['state'] == 'CHANNELD_AWAITING_SPLICE'
    assert [i['funding_txid'] for i in chan['inflight']] == [splice_txid]
    assert not l1.daemon.is_in_log('closing the channel')

    bitcoind.generate_block(6, wait_for_mempool=splice_txid)
    l1.daemon.wait_for_logs([r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL',
                             r'max-independent-secrets=20: closing the channel'])
    l1.daemon.wait_for_log(r'to CLOSINGD_COMPLETE')
    assert not l1.daemon.is_in_log('closing it unilaterally')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_cap_refuses_splice(node_factory, bitcoind, executor):
    """A peer that starts a splice just as a revocation reaches
    max-independent-secrets has it refused: BOLT 2 doesn't allow the
    shutdown we're about to send while a splice is unlocked.  The close
    then completes on the funding we had.  (commit-time on l1 holds its
    shutdown back, so the peer's stfu comes first.)  It checks what
    happens rather than the order of channeld's messages."""
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     wait_for_announce=True,
                                     opts=[{**ON, 'may_reconnect': True,
                                            'max-independent-secrets': 11,
                                            'commit-time': 4000,
                                            'allow_warning': True},
                                           {**SENDER, 'may_reconnect': True,
                                            'allow_warning': True}])
    l2.fundwallet(500000)
    while (reported_stored(l1, l2) or 0) < 9:
        l1.pay(l2, 10_000_000)
        settle(l1, l2)
    assert reported_stored(l1, l2) == 9
    funding_txid = only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])['funding_txid']

    # l2 asks to splice as soon as it has fulfilled this payment, so its
    # stfu follows the revoke_and_ack that reaches the limit.
    chan_id = l2.get_channel_id(l1)
    funds = l2.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
    l2.daemon.logs_catchup()
    l2.daemon.logsearch_start = len(l2.daemon.logs)
    fut = executor.submit(l1.pay, l2, 10_000_000)
    l2.daemon.wait_for_log('peer_out WIRE_UPDATE_FULFILL_HTLC')
    # The splice_init command doesn't return once the peer disconnects, so
    # we don't wait for it.
    executor.submit(l2.rpc.splice_init, chan_id, 100000, funds['psbt'])
    fut.result(TIMEOUT)

    l1.daemon.wait_for_logs([r'max-independent-secrets=11: closing the channel',
                             r'peer_in WIRE_STFU',
                             r'Splice refused: the channel is closing'])
    l2.daemon.wait_for_log(r'Received WARNING .*Splice refused: the channel is closing')
    for n in (l1, l2):
        n.daemon.wait_for_log(r'to CLOSINGD_COMPLETE')
    bitcoind.generate_block(1, wait_for_mempool=1)
    l1.daemon.wait_for_log(r'to ONCHAIN')
    # The close spent the funding we had: no splice was agreed.
    assert closed_on(bitcoind) == funding_txid
    assert not l1.daemon.is_in_log(r'peer_out WIRE_SPLICE_ACK')
    assert not l1.daemon.is_in_log(r'CHANNELD_SHUTTING_DOWN to CHANNELD_AWAITING_SPLICE')
    assert not l1.daemon.is_in_log(r'unexpected value CHANNELD_SHUTTING_DOWN')
    assert not l1.daemon.is_in_log('closing it unilaterally')


@pytest.mark.openchannel('v1')
@pytest.mark.openchannel('v2')
def test_independent_secrets_cap_refuses_splice_at_lock(node_factory, bitcoind, executor):
    """The same when the close starts at a splice lock, having waited for
    it: a peer that starts a new splice as soon as the splice locks has it
    refused, and the close completes on the funding that locked.  The
    plugin holds l1's lightningd at the lock, as a slow disk or a backup
    plugin would, so the peer's new splice reaches channeld before
    lightningd has handled the lock: channeld waits for it."""
    stall = str(Path(__file__).parent / 'plugins' / 'dbwrite_stall.py')
    l1, l2 = node_factory.line_graph(2, fundamount=1000000,
                                     announce_channels=False,
                                     opts=[{**ON, 'max-independent-secrets': 20,
                                            'may_reconnect': True,
                                            'allow_warning': True,
                                            'plugin': stall},
                                           {**SENDER, 'may_reconnect': True,
                                            'allow_warning': True}])
    l2.fundwallet(500000)
    splice_txid = splice_in(l1, l2)
    while (reported_stored(l1, l2) or 0) < 20:
        pay_direct(l1, l2, 10_000_000)
        settle(l1, l2)
    l1.daemon.wait_for_log('Reached max-independent-secrets=20, but a splice'
                           ' is pending')

    chan_id = l2.get_channel_id(l1)
    funds = l2.rpc.fundpsbt("111722sat", 0, 0, excess_as_change=True)
    l1.rpc.call('dbwrite-stall-arm', {'pattern': 'DELETE FROM channel_funding_inflights',
                                      'delay': 8})
    l1.daemon.logs_catchup()
    start = len(l1.daemon.logs)

    bitcoind.generate_block(6, wait_for_mempool=splice_txid)
    l2.daemon.wait_for_log(r'CHANNELD_AWAITING_SPLICE to CHANNELD_NORMAL')
    l1.daemon.wait_for_log(r'dbwrite-stall: stalling')
    # l2 starts a new splice while l1's lightningd is still handling the
    # lock.  splice_init doesn't return once the peer disconnects, so we
    # don't wait for it.
    executor.submit(l2.rpc.splice_init, chan_id, 100000, funds['psbt'])

    # The close started at the lock, before the stall.
    l1.daemon.wait_for_log(r'dbwrite-stall: done')
    assert l1.daemon.is_in_log(r'max-independent-secrets=20: closing the channel',
                               start=start)
    # The new splice is refused, not accepted (or, if l2's stfu came after
    # our shutdown, l2 cancels it) ...
    wait_for(lambda: l1.daemon.is_in_log(r'Splice refused: the channel is closing'
                                         r'|peer_out WIRE_SPLICE_ACK', start=start)
             or l2.daemon.is_in_log(r'Splice cancelled: the channel is closing'))
    assert not l1.daemon.is_in_log(r'peer_out WIRE_SPLICE_ACK', start=start)
    for n in (l1, l2):
        n.daemon.wait_for_log(r'to CLOSINGD_COMPLETE')
    assert not l1.daemon.is_in_log(r'CHANNELD_SHUTTING_DOWN to CHANNELD_AWAITING_SPLICE')
    assert not l1.daemon.is_in_log(r'unexpected value CHANNELD_SHUTTING_DOWN')
    # ... and the close spent the funding that locked.
    bitcoind.generate_block(1, wait_for_mempool=1)
    l1.daemon.wait_for_log(r'to ONCHAIN')
    assert closed_on(bitcoind) == splice_txid
    assert not l1.daemon.is_in_log('closing it unilaterally')
