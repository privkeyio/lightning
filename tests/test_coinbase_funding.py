"""Channels funded by a coinbase output.

A miner can pay a block reward straight into a channel's 2-of-2 funding
script. The coinbase transaction is then the funding transaction: nothing is
broadcast, nothing has to relay, and the block itself creates the channel.

We cannot be the funder of one, because libwally cannot represent a coinbase
input in a version 2 PSBT, and that is what the first test pins down. We can
still be asked to be the fundee, and we refuse: the channel is forgotten the
moment its funding is found at transaction 0 of a block.
"""
from fixtures import *  # noqa: F401,F403
from fixtures import TEST_NETWORK
from pyln.client import RpcError
from utils import only_one, wait_for

import base64
import os
import pytest
import unittest


def _varint(n):
    if n < 0xfd:
        return n.to_bytes(1, 'little')
    if n <= 0xffff:
        return b'\xfd' + n.to_bytes(2, 'little')
    if n <= 0xffffffff:
        return b'\xfe' + n.to_bytes(4, 'little')
    return b'\xff' + n.to_bytes(8, 'little')


def _kv(key, value):
    return _varint(len(key)) + key + _varint(len(value)) + value


def coinbase_psbt(decoded):
    """Build a PSBT whose transaction is the given (decoded) coinbase.

    BIP 174's unsigned transaction carries no scriptSigs, and a coinbase's
    txid commits to the one it has, so the scriptSig travels as
    PSBT_IN_FINAL_SCRIPTSIG (0x07) and the extractor puts it back before the
    txid is taken. That is the only thing about this that is unusual; the
    rest is the ordinary serialisation.

    No witness is needed: a txid does not commit to one.
    """
    vin = only_one(decoded['vin'])
    assert 'coinbase' in vin, "not a coinbase transaction"
    script_sig = bytes.fromhex(vin['coinbase'])

    unsigned = b''
    unsigned += int(decoded['version']).to_bytes(4, 'little')
    unsigned += _varint(1)
    unsigned += b'\x00' * 32                       # null previous txid
    unsigned += (0xffffffff).to_bytes(4, 'little')  # null previous index
    unsigned += _varint(0)                          # empty scriptSig
    unsigned += int(vin['sequence']).to_bytes(4, 'little')
    unsigned += _varint(len(decoded['vout']))
    for out in decoded['vout']:
        sats = int(round(float(out['value']) * 10**8))
        script = bytes.fromhex(out['scriptPubKey']['hex'])
        unsigned += sats.to_bytes(8, 'little')
        unsigned += _varint(len(script)) + script
    unsigned += int(decoded['locktime']).to_bytes(4, 'little')

    psbt = b'psbt\xff'
    psbt += _kv(b'\x00', unsigned)   # PSBT_GLOBAL_UNSIGNED_TX
    psbt += b'\x00'                  # end of the global map
    psbt += _kv(b'\x07', script_sig)  # PSBT_IN_FINAL_SCRIPTSIG
    psbt += b'\x00'                  # end of input 0's map
    for _ in decoded['vout']:
        psbt += b'\x00'              # end of each output's map

    return base64.b64encode(psbt).decode('ascii')


def unpublished_coinbase_block(bitcoind, addr):
    """A block paying its whole reward to addr, built but not published.

    A miner owns his template, so he knows his coinbase before anyone else
    sees it. Returns the block, its decoded coinbase and the output to addr.
    """
    block_hex = bitcoind.rpc.generateblock(addr, [], False)['hex']

    # Only the coinbase is in it, so the transaction begins right after the
    # header and the one byte transaction count. The header is 164 bytes on
    # this chain and 80 on a classic one; the top bit of the version word
    # says which.
    version = int.from_bytes(bytes.fromhex(block_hex[:8]), 'little')
    header_len = 164 if version & 0x80000000 else 80
    coinbase = bitcoind.rpc.decoderawtransaction(block_hex[(header_len + 1) * 2:])
    out = only_one([o for o in coinbase['vout']
                    if o['scriptPubKey'].get('address') == addr])
    return block_hex, coinbase, out


@unittest.skipIf(TEST_NETWORK != 'regtest', 'coinbase maturity is a bitcoin rule')
def test_coinbase_funding_psbt_refused_not_fatal(node_factory, bitcoind):
    """A coinbase cannot fund a channel from here, and saying so must not abort.

    libwally has no version 2 representation for a coinbase input: its null
    previous outpoint does not survive the conversion, so
    wally_psbt_get_length fails on the result. Every check
    fundchannel_complete makes passes anyway, because a coinbase input
    carries a final scriptSig and the amounts are right. The PSBT is then
    stored with the channel, and psbt_get_bytes aborts rather than failing.

    So the conversion has to report it, at the point where no is still an
    available answer.
    """
    l1, l2 = node_factory.get_nodes(2, opts={'large-channels': None})
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)

    amount = bitcoind.rpc.getblocktemplate({'rules': ['segwit', 'blake2b']})['coinbasevalue']
    funding_addr = l1.rpc.fundchannel_start(l2.info['id'], amount)['funding_address']

    block_hex, coinbase, out = unpublished_coinbase_block(bitcoind, funding_addr)
    assert int(round(float(out['value']) * 10**8)) == amount

    with pytest.raises(RpcError, match=r'Could not set PSBT version'):
        l1.rpc.fundchannel_complete(l2.info['id'], coinbase_psbt(coinbase),
                                    withhold=True)

    # Still alive, and still willing to work.
    l1.rpc.fundchannel_cancel(l2.info['id'])
    assert l1.rpc.getinfo()['id']
    assert l2.rpc.getinfo()['id']


@unittest.skipIf(TEST_NETWORK != 'regtest', 'coinbase maturity is a bitcoin rule')
def test_addpsbtoutput_coinbase_psbt(node_factory, bitcoind):
    """addpsbtoutput checks a version 0 PSBT's total without converting it,
    which a coinbase input would refuse."""
    l1 = node_factory.get_node()
    _, coinbase, _ = unpublished_coinbase_block(bitcoind, l1.rpc.newaddr('bech32')['bech32'])
    psbt = coinbase_psbt(coinbase)
    total = sum(int(round(float(o['value']) * 10**8)) for o in coinbase['vout'])

    l1.rpc.addpsbtoutput(100000, psbt)
    with pytest.raises(RpcError, match='would exceed max supply'):
        l1.rpc.addpsbtoutput(2100000000000000 - total + 1, psbt)


@unittest.skipIf(TEST_NETWORK != 'regtest', 'coinbase maturity is a bitcoin rule')
def test_ordinary_channel_still_opens_at_minimum_depth(node_factory, bitcoind):
    """The refusal must not catch a channel merely funded FROM a coinbase.

    Spending a mature coinbase into a funding output is the ordinary way a
    miner opens a channel, and the funding output that results is not a
    coinbase output. Nothing about it should be refused.
    """
    l1, l2 = node_factory.line_graph(2, fundchannel=True)

    scid = only_one(l1.rpc.listpeerchannels()['channels'])['short_channel_id']
    assert scid.split('x')[1] != '0'
    assert only_one(l1.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL'


@unittest.skipIf(TEST_NETWORK != 'regtest', 'coinbase maturity is a bitcoin rule')
@unittest.skipIf(os.getenv('TEST_DB_PROVIDER', 'sqlite3') != 'sqlite3', "sqlite3-specific DB manip")
def test_coinbase_funded_channel_refused(node_factory, bitcoind):
    """As the fundee, we forget a channel whose funding is a coinbase.

    We cannot fund one ourselves (see above), so l1 opens an ordinary channel
    and never broadcasts it, and l2's record of it is then pointed at a
    coinbase paying the same 2-of-2 script: what a funder able to build one
    would have told it in funding_created. The rest is real: a real block
    whose coinbase pays the channel, found by l2's own funding watch.
    """
    l1, l2 = node_factory.get_nodes(2)
    l1.fundwallet(10**7)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)

    funding_addr = l1.rpc.fundchannel_start(l2.info['id'], 10**6)['funding_address']
    prep = l1.rpc.txprepare([{funding_addr: 10**6}])
    assert l1.rpc.fundchannel_complete(l2.info['id'], prep['psbt'])['commitments_secured']
    wait_for(lambda: only_one(l2.rpc.listpeerchannels()['channels'])['state']
             == 'CHANNELD_AWAITING_LOCKIN')
    l1.stop()

    block_hex, coinbase, out = unpublished_coinbase_block(bitcoind, funding_addr)
    sats = int(round(float(out['value']) * 10**8))

    l2.stop()
    l2.db_manip("UPDATE channels SET funding_tx_id = X'{}', funding_tx_outnum = {},"
                " funding_satoshi = {};"
                .format(bytes.fromhex(coinbase['txid'])[::-1].hex(), out['n'], sats))
    l2.start()

    assert bitcoind.rpc.submitblock(block_hex) is None
    l2.daemon.wait_for_log(r'Funding transaction {} is a coinbase'
                           .format(coinbase['txid']))
    wait_for(lambda: l2.rpc.listpeerchannels()['channels'] == [])

    # Forgotten before channeld was told of a single confirmation, so it
    # never offered channel_ready on it.
    assert not l2.daemon.is_in_log(r'Funding tx {} depth'.format(coinbase['txid']))
