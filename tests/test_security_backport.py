"""Regression coverage for the v26.06.9 security backports."""
from fixtures import *  # noqa: F401,F403
from pyln.client import RpcError
from pyln.proto import wire
from utils import only_one, wait_for
import os
import pytest


def test_reestablish_revoked_commitment(node_factory):
    private_key = '12' * 32
    a, b = node_factory.get_nodes(2, opts=[
        {'may_reconnect': True}, {'dev-force-privkey': private_key},
    ])
    a.rpc.connect(b.info['id'], 'localhost', b.port)
    a.fundchannel(b, 10**6)
    a.pay(b, 10**8)
    channel_id = only_one(a.rpc.listpeerchannels()['channels'])['channel_id']
    b.stop()
    wait_for(lambda: not a.rpc.getpeer(b.info['id'])['connected'])
    indexes = a.db_query('SELECT next_index_local, next_index_remote FROM channels')[0]
    # Ask for a commitment already acknowledged by revoke_and_ack.
    connection = wire.connect(wire.PrivateKey(bytes.fromhex(private_key)),
                              wire.PublicKey(bytes.fromhex(a.info['id'])),
                              '127.0.0.1', a.port)
    try:
        init = connection.read_message()
        assert int.from_bytes(init[:2], 'big') == 16
        connection.send_message(init)
        while int.from_bytes(connection.read_message()[:2], 'big') != 136:
            pass
        connection.send_message(bytes.fromhex('0088') + bytes.fromhex(channel_id)
                                + (indexes['next_index_remote'] - 1).to_bytes(8, 'big')
                                + (indexes['next_index_local'] - 1).to_bytes(8, 'big')
                                + bytes(32) + bytes.fromhex(b.info['id']))
        a.daemon.wait_for_log('bad reestablish commitment_number: .*already revoked')
        a.wait_for_channel_onchain(b.info['id'])
    finally:
        connection.connection.close()


def test_restricted_rune_cannot_escape_through_alias(node_factory):
    node = node_factory.get_node()
    rune = node.rpc.createrune(restrictions=[
        ["method/createrune"], ["method/blacklistrune"],
    ])["rune"]
    assert node.rpc.checkrune(rune=rune, method="getinfo", params={})["valid"]
    for method in ("createrune", "invokerune", "blacklistrune", "destroyrune"):
        with pytest.raises(RpcError, match="Not permitted"):
            node.rpc.checkrune(rune=rune, method=method, params={})


def test_restricted_rune_cannot_mint_or_relist(node_factory):
    node = node_factory.get_node()
    rune = node.rpc.createrune(restrictions=[["time>1"]])["rune"]
    for method in ("createrune", "invokerune"):
        for params in ({}, {"rune": None}, [], [None]):
            with pytest.raises(RpcError, match="without restrictions"):
                node.rpc.checkrune(rune=rune, method=method, params=params)
        assert node.rpc.checkrune(rune=rune, method=method,
                                  params={"rune": rune})["valid"]
        assert node.rpc.checkrune(rune=rune, method=method, params=[rune])["valid"]
    for method in ("blacklistrune", "destroyrune"):
        for params in ({"start": 0, "relist": True}, [0, 0, True]):
            with pytest.raises(RpcError, match="without restrictions"):
                node.rpc.checkrune(rune=rune, method=method, params=params)


@pytest.mark.openchannel('v1')
def test_offered_htlc_deadline_during_shutdown(node_factory, bitcoind, executor):
    a, b = node_factory.line_graph(2, opts=[
        {'allow_warning': True},
        {'plugin': os.path.join(os.getcwd(), 'tests/plugins/htlc_accepted-hold.py'),
         'allow_warning': True},
    ])
    invoice = b.rpc.invoice(1000000, 'shutdown-deadline', 'regression')
    channel = only_one(a.rpc.listpeerchannels()['channels'])
    a.rpc.sendpay([{'id': b.info['id'], 'channel': channel['short_channel_id'],
                   'amount_msat': 1000000, 'delay': 18}], invoice['payment_hash'],
                  payment_secret=invoice['payment_secret'])
    b.daemon.wait_for_log('holding htlc')
    executor.submit(a.rpc.close, b.info['id'])
    wait_for(lambda: only_one(a.rpc.listpeerchannels()['channels'])['state']
             == 'CHANNELD_SHUTTING_DOWN')
    htlc = only_one(only_one(a.rpc.listpeerchannels()['channels'])['htlcs'])
    bitcoind.generate_block(htlc['expiry'] + 1 - bitcoind.rpc.getblockcount())
    wait_for(lambda: only_one(a.rpc.listpeerchannels()['channels'])['state']
             in ('AWAITING_UNILATERAL', 'FUNDING_SPEND_SEEN', 'ONCHAIN'))
