# BLAKE2b block headers

Core Lightning support for the redesigned block header introduced by the
BLAKE2b hardfork ([Bitcoin Knots PR 359][pr359]).

## What changes

Past the activation height a block header is 164 bytes rather than 80, and its
block id is BLAKE2b rather than SHA256d. Nothing else about Lightning is
affected: Core Lightning does not validate proof of work, it trusts its chain
backend for that. What it does do is compute block ids itself while parsing, in
`bitcoin_block_from_hex()`, and every one of those ids has to keep matching the
backend's.

That matching is load bearing. `lightningd/chaintopology.c` compares the id it
computed for the previous block against the `prev_hash` of the next one, and a
mismatch is read as a reorg. Get the id wrong and chain tracking, channel
confirmation and close detection all fail from the activation block onward.

## Detecting a v2 header

The top bit of the version, `0x80000000`, marks a v2 header. This is
self-describing: no activation height is compiled in, and no chain parameter
gates it. That matches how Knots itself serializes the header, so a node cannot
disagree with its backend about which shape a given header has.

Elements has its own header shape and also uses the top version bit, for
dynafed. The two are kept apart: the Elements branch computes its own block id
and never reaches the v2 path.

## Layout

The first 80 bytes are unchanged. A v2 header carries 84 more:

| offset | size | field |
| --- | --- | --- |
| 80 | 4 | `nonce2` |
| 84 | 4 | `nonce3` |
| 88 | 16 | `extranonce` |
| 104 | 4 | `time_offset` |
| 108 | 2 | `txcount` |
| 110 | 1 | `flags` |
| 111 | 1 | `xor_key_mask_clear_bits` |
| 112 | 16 | `xor_key` |
| 128 | 4 | `height` |
| 132 | 32 | `mm_rhs` |

Two of these are easy to get wrong. `txcount` is a real header field and must
match the block's actual transaction count. And the timestamp on the wire is
`nTime` minus `time_offset` when bit 2 of `flags` is set, so the block time has
to be reconstructed rather than read.

## The block id

`block_hdr_v2_blkid()` mirrors `CBlockHeader::GetHash()`: a tree of BIP340
tagged SHA256 hashes feeding two BLAKE2b passes, the result XORed with a mask
derived from the miner's XOR key and finally byte-reversed.

The second BLAKE2b pass takes one of four layouts selected by `flags & 3`,
which is what the mining hardware sees. Only layout 0 has been seen in blocks so far; the other three are covered by test vectors.

## Testing

`bitcoin/test/run-block_header_v2.c` runs the five cross-implementation vectors
from `src/test/data/block_header_v2.json` in the Knots tree, covering all four
layouts and the XOR mask boundaries, including the partial-byte case.

Beyond the vectors, this has been checked against live chains:

- **testnet4**, which activated at height 150027. Core Lightning synced across
  the activation and computed the same id as the node for every block, including
  blocks carrying a non-null XOR key and a non-zero `time_offset`.
- **regtest** against a node run with `-testactivationheight=blake2b@N`,
  covering a channel opened before activation and force-closed after it, reorgs
  within v2 blocks and across the activation boundary, and restart on a v2 tip.

A node without this change stops at the last SHA256d block and aborts: the 84
extra bytes are left over after the header, the block fails to parse, and the
chain backend plugin treats that as fatal.

## What this does not address

**`chain_hash` does not distinguish the two rule sets.** BOLT identifies a
network by its genesis block hash, and a hard fork does not change genesis. Nodes
that have adopted the fork and nodes still on the old rules therefore advertise
the same network identity, so they connect to each other and their gossip merges,
even though after the activation height they no longer agree on the chain. A peer
cannot tell from the handshake which rules the other side follows. Header parsing
cannot fix that; it takes a decision about what `chain_hash` should be, made once
rather than per implementation.

**Channels funded before activation are valid under both rule sets.** Their
funding output exists on either side of the divergence, so commitment
transactions signed before it can be replayed against a node that did not adopt
the fork. The unified opt-in signature hash is the fix, but commitment
transactions are signed by both parties, so both peers must support it.

[pr359]: https://github.com/bitcoinknots/bitcoin/pull/359
