# Independent per-commitment secrets

This version of Core Lightning supports `option_independent_secrets` (feature
bits 266/267, proposed in
[lightning-blake2b/bolts#17](https://github.com/lightning-blake2b/bolts/pull/17)). On a
channel with it, the node accepts per-commitment secrets from its peer that do
not come from a shachain, and keeps each one it needs to punish a breach.

## Why

Each time a channel updates, each side reveals a secret that cancels its old
state. If it later broadcasts that old state anyway, the other side uses the
secret to take the whole channel.

BOLT 3 requires those secrets to come from one seed (a shachain), so the
receiver can store them in 49 entries. A node whose keys are held k-of-n then
has to give that seed to every signer, and any one signer can pass it to the
channel peer, which can then steal the channel the moment the node broadcasts
its current state. Secrets that need not come from one seed can be generated
jointly by the signers, so no single signer ever holds them.

This node's part is to accept such secrets. It does not change how this node
generates its own: they still come from its shachain.

## What it does

- **Off by default.** With `independent-secrets` in the config, the optional
  bit 267 is set in `init` and `node_announcement` (it isn't available on
  Liquid). A new
  channel uses the add-on when both peers signal it, and it is added to a `channel_type` you name to
  `fundchannel` or `openchannel_init` as well, since it changes nothing but
  what the receiver accepts. With a peer that doesn't signal it, the channel
  opens the normal way: a `channel_type` you name with the add-on is refused,
  and so is one with it from such a peer. Outside developer mode, only the
  option sets either bit: a plugin whose `featurebits` include 266 or 267 is
  refused.
- **Storage.** A secret still goes in the channel's shachain if it fits it, as
  it always does from a peer which uses one, so with such a peer nothing
  changes, down to what a static channel backup can do. From the first secret
  which doesn't fit, each goes in the `channel_revocation_secrets` table, in
  the same database transaction as the rest of the revocation: about 60 bytes
  per revoked state in sqlite3. Once the channel has closed and its outputs
  are resolved, only the last of those is kept, which is what
  `channel_reestablish` needs.
- **Checks.** A secret must be a valid private key that generates the point of
  the commitment it revokes. It doesn't have to be related to earlier secrets.
- **The even bit, 266,** is only valid in `channel_type`. A peer that sets it in
  `init` is refused, whether or not this node offers the add-on.
- **Breaches.** When the funding output is spent, lightningd works out which
  commitment the transaction is from its locktime and sequence. If it is a
  commitment of the peer's which they revoked, and its secret is in the
  database rather than the shachain, lightningd hands it to onchaind, which
  then takes every output, as it would on any other channel.
- **Existing channels** keep their type and behave exactly as before.

`listpeerchannels` and `listclosedchannels` show the add-on in `channel_type`
as bit 266, `independent_secrets/even`.
On such a channel `listpeerchannels` also shows `independent_secrets_stored`:
how many of the peer's secrets are stored one by one, because they don't fit a
shachain. It stays 0 with a peer that uses one.

## Turning it on

```
independent-secrets
```

Without it, new channels keep requiring shachain secrets. A channel opened with
the add-on keeps it for its whole life, even if the option is later removed.

## How many secrets a channel may store

```
max-independent-secrets=1000000
```

Each update of such a channel adds a secret, and nothing else bounds them while
the channel is open. So once a channel stores this many of the peer's secrets
one by one (32 MB of secrets at the default, about 60 MB in sqlite3),
lightningd starts a mutual close of it, as `close` would, after logging a
warning at 90%. After `shutdown` the peer only needs as many more as it takes
to settle the HTLCs in flight. A peer can still keep a closing channel updating
while HTLCs remain (with `update_fee`), so once it has made us store 10% more
than the limit (at least one more), lightningd closes the channel
unilaterally. If the limit is lowered below what a channel already stores, the
10% counts from where the channel is at its next update, and the close starts
then if it can. If the mutual close hasn't finished within 48 hours,
lightningd closes the channel unilaterally, as `close` does by default. A
restart resets neither the 10% nor the 48 hours, but after one the peer gets
at least 10 minutes to finish the close, however long lightningd was down, and
a clock that has gone back counts as no time having passed. Like `close`'s own
timeout, that of a close already started stays even if the limit is then set
to 0. A channel the peer
leased from us is closed at its first update after the lease expires, or
unilaterally before then once the peer has made us store 10% more than the
limit: our funds stay locked until the lease expires either way. A channel
with a pending splice is closed once the splice locks, since BOLT 2 doesn't
allow `shutdown` before then, or straight away if the splice is aborted. An
aborted RBF attempt only drops that attempt, so the close then waits for the
splice it would have replaced. The 10% applies meanwhile, and is all that
bounds a splice which never confirms. Once the close has started, neither side
can start a splice: one the peer proposes is refused, and the connection
closed, before it can delay the `shutdown`. (BOLT 2 asks for `tx_abort` there
if the peer hasn't sent `shutdown`, but after one the subdaemon restarts
without knowing whether ours went out, while reconnecting resends it.) That
holds from the moment the
close starts, including at the lock of a splice it waited for, since the
subdaemon waits for lightningd before it reads anything else from the peer.
So the 10% can close a channel unilaterally without a mutual close having been
tried: during a lease, while a splice is pending, or after the 48 hours. 0
means no limit.

The peer can't learn the limit, so BOLT 3 recommends one of at least 1,000,000,
which an ordinary channel never reaches. Below that lightningd logs a warning
at startup: with a small limit the 10% comes down to a single secret, and an
honest peer could have its channel closed unilaterally.

## Limits

- **Static channel backups** carry the peer's shachain, and not the secrets in
  the database. So once a peer sends secrets that don't fit a shachain, a node
  restored with `recoverchannel` or `emergencyrecover` can't punish a state
  revoked after that; recovering its own balance works as for any other
  channel. With a peer that uses a shachain, backups are as before.
- **External signers.** The built-in hsmd doesn't check the peer's secrets
  (`hsmd_validate_revocation`); channeld does. A signer that keeps them in a
  shachain of its own, such as VLS, would refuse them, so lightningd won't
  start with an hsmd replaced with `--subdaemon` if `independent-secrets` is
  set, or while any channel with the add-on is open or being resolved.
- **Downgrading.** An older build refuses to start on a database this one has
  upgraded, and `lightning-downgrade` refuses wallets of this version anyway.
  The new table's own revert also refuses while a channel which hasn't closed
  uses the add-on.

## Testing against it

In developer mode, `--dev-independent-secrets-sender` (which needs
`--independent-secrets`) makes hsmd generate this node's secrets with a key derivation function instead of a shachain:

```
secret(n) = HMAC-SHA256(shaseed, "independent-per-commitment-secret" ||
                                 n as 8 bytes big-endian || counter as 1 byte)
```

The counter starts at 0 and only goes up in the negligible case that the result
is not a valid private key. This is the same function as Lightning Fork's dev
option, and like the shachain it needs nothing but the seed, so a restarted
node produces the same secrets. It applies to every channel the node has, and
only a peer with `option_independent_secrets` accepts such secrets: any other
fails the channel at the second revocation. So, as BOLT 3 requires of a node
whose secrets don't come from the seed, the node then only opens and accepts
channels with the add-on; `--dev-independent-secrets-any-channel` lifts that,
to show such a channel failing. For the same reason lightningd won't start
with the option changed while any channel is open or still being resolved,
since that channel's secrets, and the keys for our own unilateral close, would
change under it. It is not available outside developer mode: a single signer
gains nothing from it.
