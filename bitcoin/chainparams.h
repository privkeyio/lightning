#ifndef LIGHTNING_BITCOIN_CHAINPARAMS_H
#define LIGHTNING_BITCOIN_CHAINPARAMS_H

#include "config.h"
#include <bitcoin/block.h>
#include <common/amount.h>
#include <common/bip32.h>

#define ELEMENTS_ASSET_LEN 33

/* The ordinary coinbase maturity, and so the value of
 * relay_coinbase_maturity on every chain that has not lengthened it. */
#define COINBASE_MATURITY 100

struct chainparams {
	const char *network_name;
	/* Unfortunately starting with signet, we now have diverging
	 * conventions for the "BIP173" Human Readable Part (HRP).
	 * On onchain signet, the HRP is `tb` , but on Lightning
	 * signet the HRP is `tbs`.
	 */
	const char *onchain_hrp;
	const char *lightning_hrp;
	/* The height at which Bitcoin's proof of work changed, or 0 on a
	 * network where it did not. A channel funded below it is also visible
	 * to nodes which have not upgraded, and this node does not see what
	 * they do with it: see the gossip rule in gossmap_manage.c. */
	const u32 blake2b_activation_height;
	/*'bip70_name' is corresponding to the 'chain' field of
	 * the API 'getblockchaininfo' */
	const char *bip70_name;
	const struct bitcoin_blkid genesis_blockhash;
	const int rpc_port;
	/**
	 * BOLT 1:
	 *
	 * The default TCP port depends on the network used. The most common networks are:
	 *
	 * - Bitcoin mainet with port number 9735 or the corresponding hexadecimal `0x2607`;
	 * - Bitcoin testnet with port number 19735 (`0x4D17`);
	 * - Bitcoin signet with port number 39735 (`0x9B37`).
	 */
	const int ln_port;
	const char *cli;
	const char *cli_args;
	/* The min numeric version of cli supported */
	const u64 cli_min_supported_version;
	const struct amount_sat dust_limit;
	const struct amount_sat max_funding;
	const struct amount_msat max_payment;
	/* Total coins in network */
	const struct amount_sat max_supply;
	const u32 when_lightning_became_cool;
	/* The depth a coinbase output must reach before this network will
	 * relay a transaction spending it.
	 *
	 * This is a relay rule, not a consensus one, and on a chain that
	 * deploys a longer coinbase maturity the two do not agree: a spend
	 * can be perfectly valid in a block and still be refused by every
	 * node it is offered to. A node that never validates blocks, which
	 * is all of us, only ever wants the relay answer. It is
	 * COINBASE_MATURITY on any chain without such a rule. */
	const u32 relay_coinbase_maturity;
	/* A coinbase mined at or after long_coinbase_maturity_height needs
	 * long_coinbase_maturity instead. Zero height means no such rule. */
	const u32 long_coinbase_maturity_height;
	const u32 long_coinbase_maturity;
	const u8 p2pkh_version;
	const u8 p2sh_version;

	/* Whether this is a test network or not */
	const bool testnet;

	/* Version codes for BIP32 extended keys in libwally-core*/
	const struct bip32_key_version bip32_key_version;
	const bool is_elements;
	const u8 *fee_asset_tag;
};

/**
 * chainparams_for_network - Look up blockchain parameters by its name
 */
const struct chainparams *chainparams_for_network(const char *network_name);

/**
 * chainparams_by_bip173 - Helper to get a network by its bip173 name
 *
 * This lets us decode BOLT11 addresses.
 */
const struct chainparams *chainparams_by_lightning_hrp(const char *lightning_hrp);

/**
 * chainparams_by_chainhash - Helper to get a network by its genesis blockhash
 */
const struct chainparams *chainparams_by_chainhash(const struct bitcoin_blkid *chain_hash);

/**
 * chainparams_get_network_names - Produce a comma-separated list of network names
 */
const char *chainparams_get_network_names(const tal_t *ctx);

/**
 * chainparams_get_ln_port - Return the lightning network default port by
 * network if the chainparams is initialized, otherwise 9735 as mock port
 */
int chainparams_get_ln_port(const struct chainparams *params);
#endif /* LIGHTNING_BITCOIN_CHAINPARAMS_H */
