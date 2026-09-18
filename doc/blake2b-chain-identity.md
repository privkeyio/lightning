# Lightning on the Bitcoin BLAKE2b chain: identity constants

This is the set of values a Lightning implementation needs to run on the
Bitcoin BLAKE2b chain without ever being mistaken for, or mistaking a peer
for, a node on the Bitcoin (SHA256d) chain. This build of Core Lightning
implements it, and so does Lightning Fork
(github.com/paulscode/lightning-fork, an LND port) running on mainnet. The
text is kept identical between the two repositories, so that the two agree
byte for byte and so that the values stay open to change while only two
implementations have channels.

The chain is Bitcoin Knots' hard fork that replaced SHA256d proof of work
with BLAKE2b at block 961,640 on 2026-08-30. Everything below the fork
height, including the genesis block, is shared with Bitcoin, which is the
whole problem: BOLT 1, 2, 7 and 11 identify a chain by its genesis hash or
a prefix derived from it, and on this chain those are Bitcoin's.

> **This document changed substantially on 2026-09-17.** Until then it
> specified a `chain_hash` of this chain's own, derived from the activation
> block or from a tagged hash, and said that `chain_hash` was what kept the
> two chains apart. That design has been withdrawn in favour of keeping the
> shared genesis hash and separating the chains in the four specific places
> it actually matters. Section 8 says why, and what to do if you implemented
> the old version. The spec discussion is
> [lightning-blake2b/bolts#1](https://github.com/lightning-blake2b/bolts/pull/1).

## 1. `chain_hash`

`chain_hash` is **the genesis hash, unchanged**:
`000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f` on
mainnet, and each test network's own genesis on the others. It is the same
value the chain that did not upgrade uses.

This is deliberate. A change of proof of work is not a change of chain, and
giving this chain a `chain_hash` of its own would ask every node, wallet and
tool to agree a new 32-byte identifier for something that only ever mattered
in a few specific places. Those places are addressed directly instead, in
sections 2 to 5.

The consequence to keep in mind while reading the rest: **`chain_hash` does
not separate the two chains anywhere.** Two nodes on different chains agree
on it in `init`, in `open_channel`, and in every channel announcement. Any
reasoning of the form "that case is already covered, because `chain_hash`
differs" is wrong here, and was the source of more than one bug.

## 2. `init`: `option_blake2b`, bit 68

Every node on this chain sets feature bit **68**, the even form of
`option_blake2b`, in `init` and in `node_announcement`. It does not set 69.

Even is the point. BOLT 1 obliges a peer that does not know an even feature
bit to close the connection, so a node that has not been updated for this
chain hangs up by itself, without knowing why and without this node having to
decide anything. The separation is symmetric and needs no cooperation from
the other side. Measured: a stock lnd on the SHA256d chain refuses a
Lightning Fork node with `feature vector contains unknown required features:
[68]`.

This is what `chain_hash` used to do, and it is the one place where an even
bit is right despite BOLT 9's usual direction of travel. The usual argument
against starting at the compulsory end is that it refuses peers before there
is anything to be compatible with. Here refusing them is the entire purpose.

**Where this bit stands, as of 2026-09-17.** It is what both implementations
do: privkeyio's released build sets 68 and not 69, and Lightning Fork now
matches it. It is not, at the moment, written down in a specification.
`lightning-blake2b/bolts#1` carried it as a MUST for part of that day and was
then narrowed to `option_unified_sigs` and the gossip rule, dropping it. A
second implementation should set 68 anyway, because without it nothing at
`init` separates the two chains, but should expect the number and the wording
to be settled somewhere before it is relied on.

The `networks` TLV still carries `chain_hash`, and a peer that lists chains
not including ours is still disconnected. That check no longer distinguishes
the two chains, since both send the same value; it distinguishes both of them
from a node on some other chain entirely.

A peer that sends no `networks` TLV at all is kept. Lightning Fork used to
disconnect it, on the reasoning that lnd never sent the field so a silent peer
was probably a stock lnd node on the other chain. That is a heuristic standing
in for a mechanism, and once bit 68 is the mechanism it costs more than it
protects: sending the TLV is optional in BOLT 1, so refusing silence drops
anything that simply omits an optional field, including client applications
that speak the wire protocol only to reach a node's RPC.
How strictly to treat a silent peer is a local policy rather than an identity
constant, and a second implementation does not need to match it to
interoperate. This series does not change what Core Lightning already does
here.

## 3. Gossip: a floor at the activation height

A node ignores any `channel_announcement` whose `short_channel_id` names a
block height **below 961,640**. At the activation height and above is
ordinary.

A funding output from before the change of proof of work exists for nodes
that did not upgrade too, and its spend may happen where this node cannot
see it, so a channel announced against one would sit in the graph forever. A
channel funded after the activation elsewhere fails its funding output lookup
here anyway; one funded before it would not.

The rule applies to this node's own announcements as well, which is intended:
a channel of ours funded before the activation is in the same position.
The rule applies to announcements as they arrive. It does not remove entries a
node already holds: both implementations load their graph from a local store
without re-checking it, so a pre-activation channel accepted before the rule
existed stays until it is pruned as a zombie. An operator who wants it gone
sooner can delete the gossip store and resync. This is worth knowing rather
than worth engineering around, since the store is rebuilt from the network
anyway.


## 4. Channels: `option_unified_sigs`, bit 70 in `channel_type`

The chain's `SIGHASH_UNIFIED` (hash type bit `0x20`) binds a signature to
this chain: it commits to `TaggedHash("UnifiedSighash", message)` over a
BIP 341-shaped message that covers every spent output's value and
scriptPubKey.

Signatures a node makes **alone** opt in past the activation: on-chain sends,
funding inputs it contributes, sweeps of its own outputs, anchor spends, and
its own half of any second-level transaction it broadcasts. These are
`ALL | UNIFIED` (`0x21`), and `0x21` also for taproot key-path spends, which
use a 65-byte signature because `SIGHASH_DEFAULT` cannot carry the bit. No peer
verifies these, so no peer has to agree.

One exception, and it is deliberate: the justice transaction handed to a
watchtower keeps the pre-activation hash type, because the tower reconstructs
the witness from a fixed-size blob with no room for the byte. That is safe
because such a transaction spends an output created by a commitment
transaction already signed with the opt-in, so the output does not exist on the
chain that did not upgrade and there is nothing to replay against. An
implementation with a different tower protocol may opt these in too; nothing
here depends on the choice. The same reasoning covers a sweep of a
second-level HTLC output, which this node does opt in.

Signatures a **peer** verifies are governed by `channel_type`. A channel that
negotiated `option_unified_sigs` signs them with the hash type BOLT 3 already
specifies for that signature, plus `SIGHASH_UNIFIED`:

| Signature | BOLT 3 | With the opt-in |
| --- | --- | --- |
| Commitment transaction | `SIGHASH_ALL` | `0x21` |
| Cooperative close | `SIGHASH_ALL` | `0x21` |
| Second-level HTLC sent to the peer, channel without `option_anchors` | `SIGHASH_ALL` | `0x21` |
| Second-level HTLC sent to the peer, channel with `option_anchors` | `SIGHASH_SINGLE\|SIGHASH_ANYONECANPAY` | `0xa3` |
| Second-level HTLC, the broadcaster's own half | `SIGHASH_ALL` | `0x21` |

The last two rows are one transaction. An HTLC-timeout or HTLC-success
transaction is spent by a 2-of-2 and carries two signatures, and on an anchor
channel they are not the same hash type: only the half pre-signed by the peer
is `SIGHASH_SINGLE|SIGHASH_ANYONECANPAY`, because that is what lets the
broadcaster attach fees to a transaction someone else signed. Its witness is

    <> <remotehtlcsig 0xa3> <localhtlcsig 0x21> <> <witness script>

and both values are confirmed on chain between Lightning Fork and an
unmodified Core Lightning. A single value quoted for "the second-level HTLC
signature" is wrong, and an implementation that signs its own half `0xa3`
produces a transaction that is valid and malleable by a third party.

The opt-in is not a property of the commitment type, and is not the
operator's to choose: it is added to whatever channel type is negotiated,
named or implicit, whenever both peers support it. Taproot channels are the
exception and are refused, because there the commitment signature is a MuSig2
partial signature over a BIP341 digest, so opting in would be a wire change
rather than a hash type, and two sides would sign different digests.

A channel funded from coins that existed before the fork, on a channel type
without the opt-in, remains replayable through its commitment transactions.
Prefer funding from coins received after the activation.

## 5. Invoices: the BOLT 11 prefix

BOLT 11's human-readable part is `ln` followed by a network prefix. On this
chain:

| Network | Prefix | Example |
| --- | --- | --- |
| mainnet | `blake` | `lnblake10n1...` |
| testnet4 | `tblake` | `lntblake...` |
| signet | `tbsblake` | `lntbsblake...` |
| simnet | `sblake` | `lnsblake...` |
| regtest | `blakert` | `lnblakert...` |

An invoice with Bitcoin's prefix (`lnbc`, `lntb`, `lntbs`, `lnbcrt`) is
refused here, and a node on the other chain refuses these. That is the whole
of what keeps an invoice for one chain from being paid on the other: an
invoice carries `chain_hash` nowhere, and it is the last thing a user sees
before paying. Fallback on-chain addresses keep Bitcoin's address formats,
since the address formats are shared.

**This is not settled between implementations.** privkeyio's Core Lightning
keeps `bc` as its `lightning_hrp` and mints `lnbc` invoices. The two builds
therefore peer, agree a channel type, gossip and close, and cannot pay each
other: each refuses the other's invoice on the prefix before a route is
considered. Until it is settled, that is the state of interoperability, and
it is the open question on
[lightning-blake2b/bolts#1](https://github.com/lightning-blake2b/bolts/pull/1).

## 6. Offers: a known gap

There is no equivalent of the invoice prefix for BOLT 12, and as things
stand an offer cannot say which of the two chains it is for.

An offer names chains with `offer_chains`, whose values are `chain_hash`, and
an offer that omits the field means Bitcoin mainnet by the spec's default.
Both chains now answer to that same value. Two BOLT 12 nodes, one following
each chain, each mint an offer and read the other's as valid and for their
own chain, naming the same hash, with no warning from either.

Nothing is malfunctioning. Two implementations following the spec exactly
cannot tell these offers apart, because as of the `chain_hash` change there
is nothing in an offer that distinguishes them. The failure that costs money
needs a merchant with channels on both chains, which is plausible precisely
because the chains share addresses and all their pre-fork history: the offer
is fetchable from either side, the invoice comes back, and the payer pays on
whichever chain they were on.

No fix is implemented, because minting offers that name a chain the other
implementation does not recognise would break fetching between them and that
is not a thing to do unilaterally. The proposal on the table is to put the
activation block's id in `offer_chains`.

## 7. Feature bits

| Bit | Name | Where |
| --- | --- | --- |
| 68 | `option_blake2b` | `init`, `node_announcement`, even only |
| 70 / 71 | `option_unified_sigs` | 70 inside `channel_type`; 71 in `init` and `node_announcement` |

Core Lightning's port of this chain assigned both and ships them at these
numbers. A number already on the wire is the number, whatever it should have
been, so interoperating with it beats being right about it alone.

**Both are provisional and both implementations say so.** The
`v26.06.7-blake2b.4` release notes are explicit that bits 68 and 70 "are not
registered BOLT allocations and are expected to move", and that channels
opened under them may have to be closed and reopened once the numbers settle.
This document takes the same position. What is written here is what is on the
wire today, not a claim that it is right. The highest pair BOLT 9 has
assigned is 66/67, so 68 and 70 are the next numbers the spec will hand out
rather than spare ones; a pair in the custom range would have been the
conservative choice, and both implementations would have to move together.

Neither bit is set in invoices or offers. A payer that cannot read a bit must
still be refused, and for BOLT 11 the prefix does that. For BOLT 12 nothing
does, which is section 6.

### Downgrading after opening a unified channel

A channel that negotiated `option_unified_sigs` records it in its own channel
type, and every signature on it is made under `0x21` or `0xa3`. A build that
does not know the bit reads the channel type without complaint, finds nothing
it recognises, and signs `0x01` instead. The peer then rejects every
signature, and the channel can neither update nor close cooperatively.

So a node that has opened a unified channel must not be downgraded to a build
from before this feature. That is the same rule that already applies to any
negotiated channel type, taproot included, and there is no automatic guard
against it: the channel type is a bitfield, and an older build cannot warn
about a bit it has never heard of.

Existing channels are unaffected either way. The type is fixed when the
channel is opened and is never renegotiated, so upgrading does not change a
channel that is already open, and a peer that does not signal the bit is
offered an ordinary channel rather than refused.

## 8. What changed on 2026-09-17, and what to do about it

Until this date, this document specified a distinct `chain_hash` per network:
the activation block's id on mainnet, and
`TaggedHash("Lightning Fork chain_hash", genesis)` elsewhere. Lightning Fork
shipped it, and a patch series implementing it for Core Lightning was
proposed.

It is withdrawn. Isolating at `chain_hash` asked everything that touches a
chain identifier to agree a new value, to solve problems that live in four
specific places, and those places are better addressed where they are: bit 68
for peering, the height floor for gossip, `channel_type` for channels, and the
invoice prefix for BOLT 11. BOLT 12 remains open, and is the one thing the
old design covered that the new one does not.

If you implemented the old version:

- **`chain_hash` goes back to the genesis hash.** A build still using a
  distinct value will not peer with one that has changed: the two disagree on
  the `networks` TLV and hang up with "no common chain".
- **Move `option_blake2b` to the even bit** if you were sending the odd one.
  With the shared `chain_hash`, the odd bit separates nothing, because a peer
  that cannot read it ignores it by definition.
- **Check what your database keys by chain hash.** Core Lightning stamps the
  wallet with it in `vars`, and a wallet stamped with the old value refuses to
  start against a build using the new one: "Wallet blockchain hash does not
  match". Lightning Fork has it worse, because lnd keys channels by chain hash
  and the server fails with `no chain bucket exists`; it ships a channeldb
  migration to move them. Whatever your storage does, a node with channels
  opened under the old value needs a path forward that is not "restore from a
  static channel backup", since that force-closes every channel by design.
- **Upgrading is a flag day for whoever is on the other end of a channel.**
  While one end has upgraded and the other has not, the two cannot peer at all:
  they disagree about `chain_hash`, and the upgraded one sends an even feature
  bit the other does not know. The channel is intact and unusable until both
  move, and resumes once they have. Measured, both halves, in the lab's
  chain-hash-migration scenario. There is nothing a migration can do about
  this; it is what changing a chain identifier costs.
- **Decide what to do with backups written under the old value.** Lightning
  Fork accepts the legacy form per network alongside the current one, so a
  backup taken the day before an upgrade restores after it, and only its own
  network's legacy value, so a mainnet backup does not restore onto regtest.
- **Wallet checks that keyed off `chain_hash` no longer fire.** Core
  Lightning's restamp path, for instance, is reached only when the wallet is
  stamped with block 0 and the chain's `chain_hash` is not; with the two equal
  it is unreachable. If you were relying on it to catch a wallet carried
  between chains, it will not.

The check that does still catch a node pointed at the wrong chain is not a
Lightning check at all: read the block header at the activation height and
refuse it if it is 80 bytes rather than 164. That touches `chain_hash`
nowhere and came through this change unaltered.

## 9. Block header

Not a Lightning constant, but every node on this chain has to parse it: from
block 961,640, block headers are 164 bytes (the 80-byte layout plus a second
section), the block id is a BLAKE2b digest of the header rather than SHA256d,
and the header's time field is offset. A node that reads block headers itself
(rather than through a Bitcoin node's RPC) needs the Knots definition. Core
Lightning reads blocks through the backend and does not parse headers itself;
Lightning Fork's parser is in its btcd fork's `wire` package.

## 10. What is deliberately unchanged

- Address formats (`bc1...`, `1...`, `3...`) and the derivation paths, so a
  seed restores the same wallet.
- `chain_hash`, as of the change above.
- The genesis hash as the wallet backend's notion of "which network is this
  node on": the chain-identity check (reading the header at the activation
  height) does the work the genesis hash cannot.
- The Lightning protocol messages, feature bits and channel types, apart from
  the values above.

## Status

Implemented in Lightning Fork (`github.com/paulscode/lightning-fork`), running
on mainnet, and in this build of Core Lightning.

Measured in a regtest lab, against `blake2b-unified` at `24d027310`:

- **Without this series**, the two peer, agree `channel_type [12,22,70]`,
  exchange gossip, and close both cooperatively and by force with `0x21` in
  both witnesses, their node having computed half of each of those signatures.
  An HTLC held across a force close produces an HTLC-timeout transaction
  carrying `0xa3` from their node and `0x21` from ours. What they cannot do is
  pay each other: each refuses the other's BOLT 11 invoice on the prefix,
  before a route is considered.
- **With it**, the same run pays in both directions, and both closes still
  carry `0x21`. That is the whole of what this series is for.

Open to change until more than two implementations have mainnet channels;
changes after that would strand channels. Discussion: the spec PR linked above,
or an issue on either repository.
