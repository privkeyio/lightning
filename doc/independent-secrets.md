# Independent per-commitment secrets

This version of Core Lightning supports `option_independent_secrets` (feature
bits 266/267, proposed in a draft bLIP). On a
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
  opens the normal way.
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
- **Breaches.** When the funding output is spent, lightningd works out which
  commitment the transaction is from its locktime and sequence. If it is a
  commitment of the peer's which they revoked, and its secret is in the
  database rather than the shachain, lightningd hands it to onchaind, which
  then takes every output, as it would on any other channel.
- **Existing channels** keep their type and behave exactly as before.

`listpeerchannels` and `listclosedchannels` show the add-on in `channel_type`
as bit 266, `independent_secrets/even`.

## Turning it on

```
independent-secrets
```

Without it, new channels keep requiring shachain secrets. A channel opened with
the add-on keeps it for its whole life, even if the option is later removed.

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
node produces the same secrets. It applies to every channel the node has, so
only a peer with `option_independent_secrets` accepts them: any other fails the
channel at the second revocation. For the same reason lightningd won't start
with the option changed while any channel is open or still being resolved,
since that channel's secrets, and the keys for our own unilateral close, would
change under it. It is not available outside developer mode: a single signer
gains nothing from it.
