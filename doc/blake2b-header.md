# BLAKE2b block headers

Core Lightning support for the redesigned block header introduced with the BLAKE2b proof of work ([Bitcoin Knots PR 359][pr359]).

## What changes

Past the activation height a block header is 164 bytes rather than 80, and its block id is BLAKE2b rather than SHA256d. Nothing else about Lightning is affected: Core Lightning does not validate proof of work, it trusts its chain backend for that. What it does do is compute block ids itself while parsing, in `bitcoin_block_from_hex()`, and every one of those ids has to keep matching the backend's.

That matching is load bearing. `lightningd/chaintopology.c` compares the id it computed for the previous block against the `prev_hash` of the next one, and a mismatch is read as a reorg. Get the id wrong and chain tracking, channel confirmation and close detection all fail from the activation block onward.

## Detecting a v2 header

The top bit of the version, `0x80000000`, marks a v2 header. This is self-describing: no activation height is compiled in, and no chain parameter gates it. That matches how Knots itself serializes the header, so a node cannot disagree with its backend about which shape a given header has.

Elements has its own header shape and also uses the top version bit, for dynafed. The two are kept apart: the Elements branch computes its own block id and never reaches the v2 path.

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

Two of these are easy to get wrong. `txcount` is a real header field and must match the block's actual transaction count. And the timestamp on the wire is `nTime` minus `time_offset` when bit 2 of `flags` is set, so the block time has to be reconstructed rather than read.

## The block id

`block_hdr_v2_blkid()` mirrors `CBlockHeader::GetHash()`: a tree of BIP340 tagged SHA256 hashes feeding two BLAKE2b passes, the result XORed with a mask derived from the miner's XOR key and finally byte-reversed.

The second BLAKE2b pass takes one of four layouts selected by `flags & 3`, which is what the mining hardware sees. Only layout 0 has been seen on mainnet or testnet4; the other three are covered by test vectors.

## Testing

`bitcoin/test/run-block_header_v2.c` runs the five cross-implementation vectors from `src/test/data/block_header_v2.json` in the Knots tree, covering all four layouts and the XOR mask boundaries, including the partial-byte case.

Beyond the vectors, this has been checked against live chains:

- **mainnet**, which activated at height 961640. Core Lightning synced across the activation and computed the same id as the node for all 254 blocks either side of it, including two carrying a non-null XOR key with 46 and 47 mask bits cleared, the partial-byte case.
- **testnet4**, over 543 blocks across its activation. Note the height moved during the release candidates: the chain checked here activated at 150027, which is rc3's value, while rc4 and the final release set `Blake2bHeight = 150308` for testnet4. Check the height against the node you are running rather than against this document.
- **regtest** against a node run with `-testactivationheight=blake2b@N`, covering a channel opened before activation and force-closed after it, reorgs within v2 blocks and across the activation boundary, and restart on a v2 tip.

A node without this change stops at the last SHA256d block and dies: the 84 extra bytes are left over after the header, so the transaction count is read from header bytes and parsing runs off the end. Against a real block it segfaults in `bitcoin_block_from_hex()`; on regtest it aborts instead, with the chain backend reporting a bad block.

## What this does not address

**`chain_hash` is unchanged.** BOLT identifies a network by its genesis block hash, and a change of proof of work does not change genesis, because it is the same chain. A node that has upgraded and one that has not advertise the same `chain_hash`; they are told apart by the even `option_blake2b` feature bit, which a node that has not upgraded refuses. See [blake2b-upgrade.md](blake2b-upgrade.md).

**Channels funded before activation.** Their funding output predates the change of proof of work, so a node that has not upgraded sees it too, and a commitment transaction signed without the unified opt-in signature hash can be replayed to it. The unified opt-in signature hash is the fix, but commitment transactions are signed by both parties, so both peers must support it.

[pr359]: https://github.com/bitcoinknots/bitcoin/pull/359
