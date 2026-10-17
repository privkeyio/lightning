#ifndef LIGHTNING_COMMON_PSBT_INTERNAL_H
#define LIGHTNING_COMMON_PSBT_INTERNAL_H

#include "config.h"
#include <ccan/tal/tal.h>
#include <common/tx_roles.h>

struct wally_psbt;
struct wally_psbt_input;
struct witness;

/* psbt_finalize_input - Finalize an input with a given witness stack
 *
 * Sets the given witness elements onto the PSBT. Also finalizes
 * the redeem_script, if any.
 * @ctx - the context to allocate onto
 * @in - input to set final_witness for
 * @witness - witness data to add to witness stack
 */
void psbt_finalize_input(const tal_t *ctx,
			 struct wally_psbt_input *in,
			 const struct witness *witness);

/* psbt_to_witnesses - Take a side's sigs from a PSBT and copy to a
 * 			    wire witness
 *
 * @ctx - allocation context
 * @psbt - PSBT to copy sigs from
 * @side_to_stack - which side to stack witnesses of
 * @input_index_to_ignore - which input to not include. Pass -1 to include all.
 */
const struct witness **
psbt_to_witnesses(const tal_t *ctx,
		  const struct wally_psbt *psbt,
		  enum tx_role side_to_stack,
                  int input_index_to_ignore);

/* psbt_input_witness_invalid - Check a peer's witness for its input
 *
 * Returns NULL if the final witness of input @i of @tx->psbt is a valid
 * spend of a P2WPKH output, a P2TR output by its key path, or a P2WSH
 * output which is a 2-of-2 funding output or an anchor channel's
 * to_remote, signed with a hash type the BOLTs allow (0x21 only if
 * @unified), otherwise why not.
 *
 * It checks the witness against the spent output in the PSBT, not the
 * chain: whether that output exists, is confirmed and is unspent, and
 * whether a relative lock it carries is already satisfied, is not known
 * here.
 */
struct bitcoin_tx;
const char *psbt_input_witness_invalid(const tal_t *ctx,
				       const struct bitcoin_tx *tx, size_t i,
				       bool unified);

#endif /* LIGHTNING_COMMON_PSBT_INTERNAL_H */
