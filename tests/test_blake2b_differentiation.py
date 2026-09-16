"""Peer feature bit advertised alongside the chain_hash."""
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
    if peer_features == '-68':
        # chain_hash identifies the chain now, so the bit is advertised as odd
        # and a peer that does not know it is neither refused nor refuses us.
        source.rpc.connect(target.info['id'], 'localhost', target.port)
        wait_for(lambda: any(p['connected']
                             for p in blake.rpc.listpeers()['peers']))
        return
    with pytest.raises(RpcError):
        source.rpc.connect(target.info['id'], 'localhost', target.port)
    assert not any(p['connected'] for p in blake.rpc.listpeers()['peers'])
    # Invoice compatibility is checked with otherwise normal legacy features.
    for source, reader in ([] if peer_features != '-68' else [(blake, legacy), (legacy, blake)]):
        invoice = source.rpc.invoice(1000, 'compat', 'invoice compatibility')['bolt11']
        assert reader.rpc.decode(invoice)['valid']


def test_blake2b_peers_connect(node_factory):
    a, b = node_factory.get_nodes(2)
    a.rpc.connect(b.info['id'], 'localhost', b.port)
    assert a.rpc.listpeers()['peers'][0]['connected']
