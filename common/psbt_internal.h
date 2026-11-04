#ifndef LIGHTNING_COMMON_PSBT_INTERNAL_H
#define LIGHTNING_COMMON_PSBT_INTERNAL_H

#include "config.h"
#include <ccan/short_types/short_types.h>
#include <ccan/tal/tal.h>
#include <common/tx_roles.h>

struct wally_psbt;
struct wally_psbt_input;
struct witness;

/* One element of a witness stack. @data points into the witness blob. */
struct witness_element {
	const u8 *data;
	size_t len;
};

/* True iff witness is a complete witness stack: item count, that many
 * items, no trailing bytes. @num_items is set only on success. */
bool witness_stack_items(const u8 *witness, size_t *num_items);

/* Elements of a complete witness stack, pointing into @witness.
 * NULL if truncated or trailing bytes remain. An empty stack is a
 * zero-length array, not NULL. */
const struct witness_element *witness_stack_elements(const tal_t *ctx,
						     const u8 *witness);

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

/* Minimal tx_signatures witness check, shared by dual open and splice.
 * @unified also accepts SIGHASH_ALL|SIGHASH_UNIFIED.  NULL if acceptable. */
const char *check_tx_signatures_witness(const tal_t *ctx,
					const struct wally_psbt_input *in,
					const struct witness *witness,
					bool unified);

/* The number of witnesses must equal the inputs added by the sending
 * node. @input_index_to_ignore is not counted (the splice funding
 * input, whose signature is not in the witness array). Pass -1 to
 * count every input of @their_role. */
const char *check_tx_signatures_witness_count(const tal_t *ctx,
					     const struct wally_psbt *psbt,
					     enum tx_role their_role,
					     size_t num_witnesses,
					     int input_index_to_ignore);

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

/* psbt_input_witness_known - Can psbt_input_witness_invalid judge it?
 *
 * False if input @i of @tx->psbt spends an output it doesn't evaluate: a
 * P2TR output other than by a key path, or a P2WSH output with any other
 * witness script.  A missing or empty witness, one that doesn't match its
 * P2WSH output, or a spend of any other kind of output (non-standard but
 * for P2A with an empty witness) can be judged (invalid).
 */
bool psbt_input_witness_known(const struct bitcoin_tx *tx, size_t i);

/* psbt_tx_with_spent_outputs - The tx of @psbt, with each input's spent
 * output attached from its prevtx, for checking witnesses.  NULL if it
 * can't be built. */
struct bitcoin_tx *psbt_tx_with_spent_outputs(const tal_t *ctx,
					      struct wally_psbt *psbt TAKES);

/* psbt_their_witnesses_invalid - Check the witnesses of a side's inputs
 *
 * Returns NULL if every input of @their_role in @tx (but
 * @input_index_to_ignore, or -1 for none) has a final witness that
 * psbt_input_witness_invalid accepts, otherwise why not.  Unless @strict,
 * inputs psbt_input_witness_known can't judge are skipped.  Bitcoin only.
 */
const char *psbt_their_witnesses_invalid(const tal_t *ctx,
					 const struct bitcoin_tx *tx,
					 enum tx_role their_role,
					 int input_index_to_ignore,
					 bool unified, bool strict);

#endif /* LIGHTNING_COMMON_PSBT_INTERNAL_H */
