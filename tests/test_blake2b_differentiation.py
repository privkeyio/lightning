"""Mandatory peer feature without invoice modifications."""
from fixtures import *  # noqa: F401,F403
from pyln.client import RpcError
from utils import TEST_NETWORK, wait_for
import pytest

pytestmark = pytest.mark.skipif(TEST_NETWORK != 'regtest', reason='Blake2b peer features do not apply to Elements')


@pytest.mark.parametrize('incoming', [False, True])
@pytest.mark.parametrize('peer_features', ['-68', '69/////////'])
def test_blake2b_required_peer_bit(node_factory, incoming, peer_features):
    blake, legacy = node_factory.get_nodes(2, opts=[
        {'may_reconnect': True, 'allow_warning': True},
        {'dev-force-features': peer_features, 'may_reconnect': True, 'allow_warning': True}])
    source, target = (legacy, blake) if incoming else (blake, legacy)
    # The two refuse each other, but by different routes: a peer that withholds
    # bit 68 hangs up on our compulsory even bit, while one that offers only 69
    # is turned away by us.  Only the second reliably fails the caller's connect,
    # since the first can land after the RPC has already replied.  So assert the
    # settled state, which holds either way, rather than racing the hangup.
    try:
        source.rpc.connect(target.info['id'], 'localhost', target.port)
    except RpcError:
        pass
    wait_for(lambda: not any(p['connected'] for p in blake.rpc.listpeers()['peers']))
    # Invoice compatibility is checked with otherwise normal legacy features.
    for source, reader in ([] if peer_features != '-68' else [(blake, legacy), (legacy, blake)]):
        invoice = source.rpc.invoice(1000, 'compat', 'invoice compatibility')['bolt11']
        assert reader.rpc.decode(invoice)['valid']


def test_blake2b_peers_connect(node_factory):
    a, b = node_factory.get_nodes(2)
    a.rpc.connect(b.info['id'], 'localhost', b.port)
    assert a.rpc.listpeers()['peers'][0]['connected']
