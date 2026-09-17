# Core Lightning with BLAKE2b proof of work

This is an unofficial fork of [Core Lightning](https://github.com/ElementsProject/lightning) that follows the BLAKE2b proof-of-work hardfork of Bitcoin. It is not affiliated with the Core Lightning project. Upstream is still on the pre-fork rules, so use it instead if that is what you want.

> **Not audited. Use at your own risk, and no warranty of any kind, see the [BSD-MIT license](LICENSE).** It holds keys and funds, it changes how transactions are signed, and the feature numbers it uses on the wire are provisional. Read *Before opening channels* below. Everything under the divider is upstream's documentation and describes Core Lightning rather than this fork.

## What differs from upstream

- **BLAKE2b block headers.** Parses the 164 byte v2 header and takes its BLAKE2b hash as the block id. A header announces itself through the top bit of its version word, so no activation height is compiled in and nothing has to be configured per network. Without this a node cannot parse the activation block and stops there.
- **Unified signatures.** Wallet transactions and new channels are signed with the fork's opt-in `SIGHASH_UNIFIED` digest, so a channel funded past activation from post-activation coins is signed in a way the pre-fork rules reject, and cannot be replayed on the SHA256d chain. Built on [connorslab's](https://github.com/connorslab/lightning) unified-sigs work.
- **A required peer feature bit.** The node advertises `option_blake2b` as compulsory, so it will not connect to a Lightning node still on the pre-fork rules.
- **Downgrades are refused.** A build without unified signing computes a different signature hash and could not close the channels this one opens, so `lightning-downgrade` stops before touching the database.

## Before opening channels

The required feature bit means you **cannot cooperatively close a channel opened before activation** with a counterparty still on the pre-fork rules.

The feature numbers are provisional. Bits 68 and 70 are not registered BOLT allocations and are expected to move; an alternative proposal signals odd in `init` with numbers at or above 32768, and the two are mutually exclusive. Channels opened under the current numbering may have to be closed and reopened once the numbers are settled.

Fund channels only from coins received past activation. A channel funded from a pre-fork UTXO has a funding transaction valid under both rule sets, which reopens the exposure unified signing exists to close.

## Activation

| Network | Height |
| --- | --- |
| mainnet | 961,640 |
| testnet4 | 150,308 |

These are the activation heights. Block parsing does not use them: it keys off the header itself, so it needs no update if the heights move. They are compiled in for gossip alone. Announcements for channels funded before activation are ignored, since that funding output exists under the pre-fork rules too and its spend may happen where this node cannot see it. The same height bounds where the seeker probes for short channel ids.

## Building

Unchanged from upstream, see [Getting Started](#getting-started) below. Clone this repository rather than upstream's:

```bash
git clone https://github.com/privkeyio/lightning.git
```

## Releases

Published under [Releases](https://github.com/privkeyio/lightning/releases) with signed checksum files covering every artifact:

```bash
gpg --import privkeyio-signing-key.asc
gpg --verify SHA256SUMS-*.asc SHA256SUMS-*
sha256sum -c SHA256SUMS-* --ignore-missing
```

The signing key is `A47D 99B6 DB0D 715D 40C5 9A20 23AE 8A8E A7E2 4E38`.

For StartOS, the packaged build lives at [privkeyio/cln-startos](https://github.com/privkeyio/cln-startos).

---

# Core Lightning (CLN): A specification compliant Lightning Network implementation in C

Core Lightning (previously c-lightning) is a lightweight, highly customizable and [standard compliant][std] implementation of the Lightning Network protocol.

* [Getting Started](#getting-started)
    * [Installation](#installation)
    * [Starting lightningd](#starting-lightningd)
    * [Using the JSON-RPC Interface](#using-the-json-rpc-interface)
    * [Care And Feeding Of Your New Lightning Node](#care-and-feeding-of-your-new-lightning-node)
    * [Opening A Channel](#opening-a-channel)
	* [Sending and Receiving Payments](#sending-and-receiving-payments)
	* [Configuration File](#configuration-file)
* [Further Information](#further-information)
    * [FAQ](doc/FAQ.md)
    * [Pruning](#pruning)
    * [HD wallet encryption](#hd-wallet-encryption)
	* [Developers](#developers)
* [Documentation](https://docs.corelightning.org/docs)

## Project Status

[![Continuous Integration][actions-badge]][actions]
[![Pull Requests Welcome][prs-badge]][prs]
[![Documentation Status][docs-badge]][docs]
[![Telegram][telegram-badge]][telegram]
[![Discord][discord-badge]][discord]
[![Irc][IRC-badge]][IRC]

This implementation has been in production use on the Bitcoin mainnet since early 2018, with the launch of the [Blockstream Store][blockstream-store-blog].
We recommend getting started by experimenting on `testnet` (`testnet4` or `regtest`), but the implementation is considered stable and can be safely used on mainnet.

## Reach Out to Us

Any help testing the implementation, reporting bugs, or helping with outstanding issues is very welcome.
Don't hesitate to reach out to us on the implementation-specific [mailing list][ml1], or on [CLN Discord][discord], or on [CLN Telegram][telegram], or on IRC at [dev][irc1]/[gen][irc2] channel.

## Getting Started

Core Lightning only works on Linux and macOS, and requires a locally (or remotely) running `bitcoind` (version 25.0 or above) that is fully caught up with the network you're running on, and relays transactions (ie with `blocksonly=0`).
Pruning (`prune=n` option in `bitcoin.conf`) is partially supported, see [here](#pruning) for more details.

### Installation

There are 3 supported installation options:

 - Installation of a pre-compiled binary from the [release page][releases] on GitHub.
 - Using one of the [provided docker images][dockerhub] on the Docker Hub.
 - Compiling the source code yourself as described in the [installation documentation](doc/getting-started/getting-started/installation.md).

### Starting `lightningd`

#### Regtest (local, fast-start) Option
If you want to experiment with `lightningd`, there's a script to set
up a `bitcoind` regtest test network of two local lightning nodes,
which provides a convenient `start_ln` helper. See the notes at the top
of the `startup_regtest.sh` file for details on how to use it.

```bash
. contrib/startup_regtest.sh
```

#### Mainnet Option
To test with real bitcoin,  you will need to have a local `bitcoind` node running:

```bash
bitcoind -daemon
```

Wait until `bitcoind` has synchronized with the network.

Make sure that you do not have `walletbroadcast=0` in your `~/.bitcoin/bitcoin.conf`, or you may run into trouble.
Notice that running `lightningd` against a pruned node may cause some issues if not managed carefully, see [below](#pruning) for more information.

You can start `lightningd` with the following command:

```bash
lightningd --network=bitcoin --log-level=debug
```

This creates a `.lightning/` subdirectory in your home directory: see `man -l doc/lightningd.8` (or https://docs.corelightning.org/docs) for more runtime options.

### Using The JSON-RPC Interface

Core Lightning exposes a [JSON-RPC 2.0][jsonrpcspec] interface over a Unix Domain socket; the `lightning-cli` tool can be used to access it, or there is a [python client library](contrib/pyln-client).

You can use `lightning-cli help` to print a table of RPC methods; `lightning-cli help <command>`
will offer specific information on that command.

Useful commands:

* [newaddr](https://docs.corelightning.org/reference/newaddr): get a bitcoin address to deposit funds into your lightning node.
* [listfunds](https://docs.corelightning.org/reference/listfunds): see where your funds are.
* [connect](https://docs.corelightning.org/reference/connect): connect to another lightning node.
* [fundchannel](https://docs.corelightning.org/reference/fundchannel): create a channel to another connected node.
* [invoice](https://docs.corelightning.org/reference/invoice): create an invoice to get paid by another node.
* [pay](https://docs.corelightning.org/reference/pay): pay someone else's invoice.
* [plugin](https://docs.corelightning.org/reference/plugin): commands to control extensions.

### Care And Feeding Of Your New Lightning Node

Once you've started for the first time, there's a script called
`contrib/bootstrap-node.sh` which will connect you to other nodes on
the lightning network.

There are also numerous plugins available for Core Lightning which add
capabilities: in particular there's a collection at: https://github.com/lightningd/plugins

For a less reckless experience, you can encrypt the HD wallet seed:
 see [HD wallet encryption](#hd-wallet-encryption).

You can also chat to other users at Discord [core-lightning][discord];
we are always happy to help you get started!


### Opening A Channel

First you need to transfer some funds to `lightningd` so that it can
open a channel:

```bash
# Returns an address <address>
lightning-cli newaddr
```

`lightningd` will register the funds once the transaction is confirmed.

Alternatively you can generate a taproot address should your source of funds support it:

```bash
# Return a taproot address
lightning-cli newaddr p2tr
```

Confirm `lightningd` got funds by:

```bash
# Returns an array of on-chain funds.
lightning-cli listfunds
```

Once `lightningd` has funds, we can connect to a node and open a channel.
Let's assume the **remote** node is accepting connections at `<ip>`
(and optional `<port>`, if not 9735) and has the node ID `<node_id>`:

```bash
lightning-cli connect <node_id> <ip> [<port>]
lightning-cli fundchannel <node_id> <amount_in_satoshis>
```

This opens a connection and, on top of that connection, then opens a channel.
The funding transaction needs 3 confirmation in order for the channel to be usable, and 6 to be announced for others to use.
You can check the status of the channel using `lightning-cli listpeers`, which after 3 confirmations (1 on testnet) should say that `state` is `CHANNELD_NORMAL`; after 6 confirmations you can use `lightning-cli listchannels` to verify that the `public` field is now `true`.

### Sending and Receiving Payments

Payments in Lightning are invoice based.
The recipient creates an invoice with the expected `<amount>` in
millisatoshi (or `"any"` for a donation), a unique `<label>` and a
`<description>` the payer will see:

```bash
lightning-cli invoice <amount> <label> <description>
```

This returns some internal details, and a standard invoice string called `bolt11` (named after the [BOLT #11 lightning spec][BOLT11]).

[BOLT11]: https://github.com/lightning/bolts/blob/master/11-payment-encoding.md

The sender can feed this `bolt11` string to the `decode` command to see what it is, and pay it simply using the `pay` command:

```bash
lightning-cli pay <bolt11>
```

Note that there are lower-level interfaces (and more options to these
interfaces) for more sophisticated use.

## Configuration File

`lightningd` can be configured either by passing options via the command line, or via a configuration file.
Command line options will always override the values in the configuration file.

To use a configuration file, create a file named `config` within your top-level lightning directory or network subdirectory
(eg. `~/.lightning/config` or `~/.lightning/bitcoin/config`).  See `man -l doc/lightningd-config.5`.

A sample configuration file is available at `contrib/config-example`.

## Further information

### Pruning

Core Lightning requires JSON-RPC access to a fully synchronized `bitcoind` in order to synchronize with the Bitcoin network.
Access to ZeroMQ is not required and `bitcoind` does not need to be run with `txindex` like other implementations.
The lightning daemon will poll `bitcoind` for new blocks that it hasn't processed yet, thus synchronizing itself with `bitcoind`.
If `bitcoind` prunes a block that Core Lightning has not processed yet, e.g., Core Lightning was not running for a prolonged period, then `bitcoind` will not be able to serve the missing blocks, hence Core Lightning will not be able to synchronize anymore and will be stuck.
In order to avoid this situation you should be monitoring the gap between Core Lightning's blockheight using `lightning-cli getinfo` and `bitcoind`'s blockheight using `bitcoin-cli getblockchaininfo`.
If the two blockheights drift apart it might be necessary to intervene.

### HD wallet encryption

You can encrypt the `hsm_secret` content (which is used to derive the HD wallet's master key) by passing the `--encrypted-hsm` startup argument, or by using the `lightning-hsmtool` (which you can find in the `tool/` directory at the root of this repo) with the `encrypt` method. You can unencrypt an encrypted `hsm_secret` using the `lightning-hsmtool` with the `decrypt` method.

If you encrypt your `hsm_secret`, you will have to pass the `--encrypted-hsm` startup option to `lightningd`. Once your `hsm_secret` is encrypted, you __will not__ be able to access your funds without your password, so please beware with your password management. Also, beware of not feeling too safe with an encrypted `hsm_secret`: unlike for `bitcoind` where the wallet encryption can restrict the usage of some RPC command, `lightningd` always needs to access keys from the wallet which is thus __not locked__ (yet), even with an encrypted BIP32 master seed.

### Developers

Developers wishing to contribute should start with the developer guide [here](doc/contribute-to-core-lightning/coding-style-guidelines.md).

[blockstream-store-blog]: https://blockstream.com/2018/01/16/en-lightning-charge/
[std]: https://github.com/lightning/bolts
[prs-badge]: https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=flat
[prs]: http://makeapullrequest.com
[ml1]: https://lists.ozlabs.org/listinfo/c-lightning
[discord-badge]: https://badgen.net/badge/Discord/chat/blue
[discord]: https://discord.gg/mE9s4rc5un
[telegram-badge]: https://badgen.net/badge/Telegram/chat/blue
[telegram]: https://t.me/lightningd
[IRC-badge]: https://img.shields.io/badge/IRC-chat-blue.svg
[IRC]: https://web.libera.chat/#c-lightning
[irc1]: https://web.libera.chat/#lightning-dev
[irc2]: https://web.libera.chat/#c-lightning
[docs-badge]: https://readthedocs.org/projects/lightning/badge/?version=docs
[docs]: https://docs.corelightning.org/docs
[releases]: https://github.com/ElementsProject/lightning/releases
[dockerhub]: https://hub.docker.com/r/elementsproject/lightningd/
[jsonrpcspec]: https://www.jsonrpc.org/specification
[helpme-github]: https://github.com/lightningd/plugins/tree/master/helpme
[actions-badge]: https://github.com/ElementsProject/lightning/workflows/Continuous%20Integration/badge.svg
[actions]: https://github.com/ElementsProject/lightning/actions
