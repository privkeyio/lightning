#include "config.h"
#include <bitcoin/chainparams.h>
#include <common/utils.h>
#include <common/utxo.h>

size_t utxo_spend_weight(const struct utxo *utxo, size_t min_witness_weight)
{
	size_t witness_weight;
	bool p2sh = (utxo->utxotype == UTXO_P2SH_P2WPKH);

	witness_weight = bitcoin_tx_input_witness_weight(utxo->utxotype);

	/* If the min is less than what we'd use for a 'normal' tx,
	 * we return the value with the greater added/calculated */
	if (witness_weight < min_witness_weight)
		return bitcoin_tx_input_weight(p2sh,
					       min_witness_weight);

	return bitcoin_tx_input_weight(p2sh, witness_weight);
}

u32 utxo_is_immature(const struct utxo *utxo, u32 blockheight)
{
	if (utxo->is_in_coinbase) {
		u32 mature_at;

		/* We got this from a block, it must have a known
		 * blockheight. */
		assert(utxo->blockheight);

		/* The depth at which the network will relay a spend, not the
		 * hundred blocks that make one valid. On a chain with a
		 * longer coinbase maturity these differ, and a transaction we
		 * cannot hand to a peer is not spendable whatever a block
		 * would make of it: we would build it, sign it, and have it
		 * refused at broadcast. The two are the same number on every
		 * chain without such a rule. */
		if (chainparams->long_coinbase_maturity_height
		    && *utxo->blockheight >= chainparams->long_coinbase_maturity_height)
			mature_at = *utxo->blockheight
				+ chainparams->long_coinbase_maturity;
		else
			mature_at = *utxo->blockheight
				+ chainparams->relay_coinbase_maturity;

		if (blockheight < mature_at)
			return mature_at - 1 - blockheight;

		else
			return 0;
	} else {
		/* Non-coinbase outputs are always mature. */
		return 0;
	}
}

const char *utxotype_to_str(enum utxotype utxotype)
{
	switch (utxotype) {
	case UTXO_P2SH_P2WPKH:
		return "p2sh_p2wpkh";
	case UTXO_P2WPKH:
		return "p2wpkh";
	case UTXO_P2WSH_FROM_CLOSE:
		return "p2wsh_from_close";
	case UTXO_P2TR:
		return "p2tr";
	}
	abort();
}
