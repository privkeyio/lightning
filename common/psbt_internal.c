#include "config.h"
#include <assert.h>
#include <bitcoin/psbt.h>
#include <bitcoin/pubkey.h>
#include <bitcoin/script.h>
#include <bitcoin/signature.h>
#include <bitcoin/unified_sighash.h>
#include <ccan/mem/mem.h>
#include <ccan/tal/str/str.h>
#include <common/psbt_internal.h>
#include <common/psbt_open.h>
#include <common/utils.h>
#include <secp256k1_extrakeys.h>
#include <secp256k1_schnorrsig.h>
#include <wally_map.h>
#include <wally_transaction.h>
#include <wire/peer_wire.h>


static bool next_size(const u8 **cursor, size_t *max, size_t *size)
{
	size_t len;
	varint_t varint;

	if (*max < 1)
		return false;

	len = varint_get(*cursor, *max, &varint);

	if (len < 1)
		return false;

	if (*max < len) {
		*max = 0;
		return false;
	}

	*cursor += len;
	*max -= len;
	*size = varint;
	return true;
}

/* If @elements is non-NULL it must hold at least as many slots as the
 * stack claims. A failure may have written a prefix; ignore it. */
static bool parse_witness_stack(const u8 *witness, size_t *num_items,
				struct witness_element *elements)
{
	const u8 *cursor = witness;
	size_t max = tal_bytelen(witness);
	size_t n, item_len;

	if (!next_size(&cursor, &max, &n))
		return false;

	for (size_t i = 0; i < n; i++) {
		if (!next_size(&cursor, &max, &item_len))
			return false;
		if (max < item_len)
			return false;
		if (elements) {
			elements[i].data = cursor;
			elements[i].len = item_len;
		}
		cursor += item_len;
		max -= item_len;
	}

	/* Encoding ends here; anything left is not a witness. */
	if (max != 0)
		return false;

	*num_items = n;
	return true;
}

bool witness_stack_items(const u8 *witness, size_t *num_items)
{
	return parse_witness_stack(witness, num_items, NULL);
}

const struct witness_element *witness_stack_elements(const tal_t *ctx,
						     const u8 *witness)
{
	size_t n;
	struct witness_element *elements;

	if (!parse_witness_stack(witness, &n, NULL))
		return NULL;

	/* Each item costs at least its length byte, so n fits the blob. */
	elements = tal_arr(ctx, struct witness_element, n);
	if (!parse_witness_stack(witness, &n, elements))
		return tal_free(elements);
	return elements;
}

static u8 *next_script(const tal_t *ctx, const u8 **cursor, size_t *max)
{
	const u8 *p;
	size_t size;
	u8 *ret;

	if (!next_size(cursor, max, &size))
		return NULL;

	if (*max < size) {
		*max = 0;
		return NULL;
	}

	p = *cursor;
	*max -= size;
	*cursor += size;

	ret = tal_arr(ctx, u8, size);
	memcpy(ret, p, size);
	return ret;
}

static void
psbt_input_set_final_witness_stack(const tal_t *ctx,
				   struct wally_psbt_input *in,
				   const struct witness *witness)
{
	u8 *script, *sctx;
	const u8 *data = witness->witness_data;
	size_t size, max = tal_count(data);
	bool ok;

	wally_psbt_input_set_final_witness(in, NULL);

	/* FIXME: return an error?? */
	if (!next_size(&data, &max, &size))
		return;

	tal_wally_start();
	sctx = tal(NULL, u8);

	wally_tx_witness_stack_init_alloc(size, &in->final_witness);

	while ((script = next_script(sctx, &data, &max)) && script != NULL) {
		ok = (wally_tx_witness_stack_add(in->final_witness,
					   script, tal_count(script)) == WALLY_OK);
		assert(ok);
	}

	tal_wally_end(ctx);
	tal_free(sctx);
}

void psbt_finalize_input(const tal_t *ctx,
			 struct wally_psbt_input *in,
			 const struct witness *witness)
{
	const struct wally_map_item *redeem_script;
	psbt_input_set_final_witness_stack(ctx, in, witness);

	/* There's this horrible edgecase where we set the final_witnesses
	 * directly onto the PSBT, but the input is a P2SH-wrapped input
	 * (which has redeemscripts that belong in the scriptsig). Because
	 * of how the internal libwally stuff works calling 'finalize'
	 * on these just .. ignores it!? Murder. Anyway, here we do a final
	 * scriptsig check -- if there's a redeemscript field still around we
	 * just go ahead and mush it into the final_scriptsig field. */
	redeem_script = wally_map_get_integer(&in->psbt_fields, /* PSBT_IN_REDEEM_SCRIPT */ 0x04);
	if (redeem_script) {
		u8 *redeemscript = tal_dup_arr(NULL, u8,
					       redeem_script->value,
					       redeem_script->value_len, 0);
		u8 *final_scriptsig =
			bitcoin_scriptsig_redeem(ctx,
						 take(redeemscript));
		tal_wally_start();
		wally_psbt_input_set_final_scriptsig(in, final_scriptsig, tal_bytelen(final_scriptsig));
		wally_psbt_input_set_redeem_script(in, tal_arr(in, u8, 0), 0);
		tal_wally_end(ctx);
	}
}

const struct witness **
psbt_to_witnesses(const tal_t *ctx,
		  const struct wally_psbt *psbt,
		  enum tx_role side_to_stack,
		  int input_index_to_ignore)
{
	u64 serial_id;
	const struct witness **witnesses =
		tal_arr(ctx, const struct witness *, 0);

	for (size_t i = 0; i < psbt->num_inputs; i++) {
		struct wally_tx_witness_stack *wtx_s =
			psbt->inputs[i].final_witness;
		if (!psbt_get_serial_id(&psbt->inputs[i].unknowns,
					&serial_id))
			/* FIXME: throw an error ? */
			return tal_free(witnesses);

		if (input_index_to_ignore == i)
			continue;

		/* BOLT #2:
		 * - if is the *initiator*:
		 *   - MUST send even `serial_id`s
		 */
		if (wtx_s && serial_id % 2 == side_to_stack) {

			/* BOLT #2:
			 *
			 * The `witness_data` is encoded as per bitcoin's
			 * wire protocol (a CompactSize number of elements,
			 * with each element a CompactSize length and that
			 * many bytes following).
			 */
			struct witness *wit = tal(witnesses, struct witness);
			wit->witness_data = tal_arr(wit, u8, 0);
			add_varint(&wit->witness_data, wtx_s->num_items);
			for (size_t j = 0; j < wtx_s->num_items; j++) {
				add_varint(&wit->witness_data, wtx_s->items[j].witness_len);
				if (wtx_s->items[j].witness_len) {
					assert(wtx_s->items[j].witness);
					tal_expand(&wit->witness_data,
						   wtx_s->items[j].witness,
						   wtx_s->items[j].witness_len);
				}
			}

			tal_arr_expand(&witnesses, wit);
		}

	}

	return witnesses;
}

static const struct wally_tx_output *spent_output(const struct wally_psbt *psbt,
						  size_t i)
{
	const struct wally_psbt_input *in = &psbt->inputs[i];

	if (in->utxo) {
		if (in->index >= in->utxo->num_outputs)
			return NULL;
		return &in->utxo->outputs[in->index];
	}
	return in->witness_utxo;
}

static bool bip341_keypath_digest(const struct bitcoin_tx *tx, size_t i,
				  u32 sighash, u8 digest[SHA256_LEN])
{
	struct wally_map scripts, values;
	bool ok = false;

	tal_wally_start();
	if (wally_map_init(tx->psbt->num_inputs, NULL, &scripts) != WALLY_OK)
		abort();
	if (wally_map_init(tx->psbt->num_inputs, NULL, &values) != WALLY_OK)
		abort();
	for (size_t j = 0; j < tx->psbt->num_inputs; j++) {
		const struct wally_tx_output *out = spent_output(tx->psbt, j);
		u64 sat;

		if (!out)
			goto done;
		sat = out->satoshi;
		if (wally_map_add_integer(&scripts, j, out->script,
					  out->script_len) != WALLY_OK
		    || wally_map_add_integer(&values, j, (u8 *)&sat,
					     sizeof(sat)) != WALLY_OK)
			goto done;
	}
	ok = wally_tx_get_input_signature_hash(tx->wtx, i, &scripts, NULL,
					       &values, NULL, 0, 0,
					       WALLY_NO_CODESEPARATOR,
					       NULL, 0, NULL, 0, sighash,
					       WALLY_SIGTYPE_SW_V1, NULL,
					       digest, SHA256_LEN) == WALLY_OK;
done:
	wally_map_clear(&scripts);
	wally_map_clear(&values);
	tal_wally_end(tmpctx);
	return ok;
}

static const char *check_ecdsa(const tal_t *ctx, const struct bitcoin_tx *tx,
			       size_t i, const struct wally_tx_witness_item *item,
			       const u8 *script_code, const struct pubkey *key,
			       bool unified)
{
	struct bitcoin_signature sig;

	if (!signature_from_der(item->witness, item->witness_len, &sig))
		return tal_fmt(ctx, "input %zu has a bad signature encoding",
			       i);
	if (sig.sighash_type != SIGHASH_ALL
	    && !(unified && sig.sighash_type == (SIGHASH_ALL|SIGHASH_UNIFIED)))
		return tal_fmt(ctx, "input %zu uses hash type 0x%02x", i,
			       sig.sighash_type);
	if (!check_tx_sig(tx, i, NULL, script_code, key, &sig))
		return tal_fmt(ctx, "input %zu has an invalid signature", i);
	return NULL;
}

/* A funding output's 2-of-2, as when the peer splices another channel in. */
static bool is_2of2_script(const u8 *script, struct pubkey *k1,
			   struct pubkey *k2)
{
	u8 *expected;

	if (tal_bytelen(script) != 71
	    || !pubkey_from_der(script + 2, 33, k1)
	    || !pubkey_from_der(script + 36, 33, k2))
		return false;
	expected = bitcoin_redeem_2of2(tmpctx, k1, k2);
	return memeq(expected, tal_bytelen(expected),
		     script, tal_bytelen(script));
}

static const char *check_p2wsh_2of2(const tal_t *ctx,
				    const struct bitcoin_tx *tx,
				    size_t i,
				    const struct wally_tx_witness_stack *stack,
				    const u8 *script, bool unified)
{
	struct pubkey k1, k2;
	const char *bad;

	if (stack->num_items != 4 || stack->items[0].witness_len != 0
	    || !is_2of2_script(script, &k1, &k2))
		return tal_fmt(ctx, "P2WSH input %zu is not a 2-of-2 spend", i);

	/* OP_CHECKMULTISIG takes the signatures in the keys' order. */
	bad = check_ecdsa(ctx, tx, i, &stack->items[1], script, &k1, unified);
	if (!bad)
		bad = check_ecdsa(ctx, tx, i, &stack->items[2], script, &k2,
				  unified);
	return bad;
}

/* An anchor channel's to_remote output, as addpsbtinput may use. */
static const char *check_p2wsh_to_remote(const tal_t *ctx,
					 const struct bitcoin_tx *tx, size_t i,
					 const struct wally_tx_witness_stack *stack,
					 const u8 *script, bool unified)
{
	size_t len = tal_bytelen(script);
	struct pubkey key;
	u32 csv, seq;
	u8 *expected;

	if (stack->num_items != 2 || len < 37
	    || !pubkey_from_der(script + 1, 33, &key))
		return tal_fmt(ctx, "P2WSH input %zu is not a to_remote spend",
			       i);
	/* Between OP_CHECKSIGVERIFY and OP_CHECKSEQUENCEVERIFY: OP_1 to
	 * OP_16, or a push of up to 3 bytes. */
	if (len == 37 && script[35] >= 0x51 && script[35] <= 0x60)
		csv = script[35] - 0x50;
	else if (len > 37 && script[35] == len - 37 && len - 37 <= 3) {
		csv = 0;
		for (size_t b = 0; b < len - 37; b++)
			csv |= (u32)script[36 + b] << (8 * b);
	} else
		return tal_fmt(ctx, "P2WSH input %zu is not a to_remote spend",
			       i);
	expected = bitcoin_wscript_to_remote_anchored(tmpctx, &key, csv);
	if (!memeq(expected, tal_bytelen(expected), script, len))
		return tal_fmt(ctx, "P2WSH input %zu is not a to_remote spend",
			       i);

	/* BIP68: a block based relative lock of at least csv. */
	seq = tx->wtx->inputs[i].sequence;
	if ((seq & 0x80000000) || (seq & 0x00400000) || (seq & 0xffff) < csv)
		return tal_fmt(ctx, "P2WSH input %zu sequence %u does not"
			       " satisfy its CSV of %u", i, seq, csv);

	return check_ecdsa(ctx, tx, i, &stack->items[0], script, &key, unified);
}

const char *psbt_input_witness_invalid(const tal_t *ctx,
				       const struct bitcoin_tx *tx, size_t i,
				       bool unified)
{
	const struct wally_psbt *psbt = tx->psbt;
	const struct wally_tx_witness_stack *stack;
	const struct wally_tx_output *spent;
	struct sha256 wsh;
	u8 xonly[32];

	if (i >= psbt->num_inputs)
		return tal_fmt(ctx, "no input %zu", i);
	stack = psbt->inputs[i].final_witness;
	spent = spent_output(psbt, i);
	if (!spent)
		return tal_fmt(ctx, "input %zu has no spent output", i);
	if (!stack)
		return tal_fmt(ctx, "input %zu has no witness", i);

	if (is_p2wpkh(spent->script, spent->script_len, NULL)) {
		const struct wally_tx_witness_item *sig_item, *key_item;
		struct pubkey key;
		u8 *expected;

		if (stack->num_items != 2)
			return tal_fmt(ctx, "P2WPKH input %zu has %zu witness"
				       " items", i, stack->num_items);
		sig_item = &stack->items[0];
		key_item = &stack->items[1];
		if (!pubkey_from_der(key_item->witness, key_item->witness_len,
				     &key))
			return tal_fmt(ctx, "P2WPKH input %zu has a bad pubkey",
				       i);
		expected = scriptpubkey_p2wpkh(tmpctx, &key);
		if (!memeq(expected, tal_bytelen(expected),
			   spent->script, spent->script_len))
			return tal_fmt(ctx, "P2WPKH input %zu pubkey does not"
				       " match its output", i);
		return check_ecdsa(ctx, tx, i, sig_item,
				   p2wpkh_scriptcode(tmpctx, &key), &key,
				   unified);
	}

	if (is_p2wsh(spent->script, spent->script_len, &wsh)) {
		const struct wally_tx_witness_item *last;
		struct sha256 h;
		u8 *script;

		if (stack->num_items < 2)
			return tal_fmt(ctx, "P2WSH input %zu has %zu witness"
				       " items", i, stack->num_items);
		last = &stack->items[stack->num_items - 1];
		script = tal_dup_arr(tmpctx, u8, last->witness,
				     last->witness_len, 0);
		sha256(&h, script, tal_bytelen(script));
		if (!sha256_eq(&h, &wsh))
			return tal_fmt(ctx, "P2WSH input %zu witness script does"
				       " not match its output", i);
		if (is_to_remote_anchored_witness_script(script,
							 tal_bytelen(script)))
			return check_p2wsh_to_remote(ctx, tx, i, stack, script,
						     unified);
		return check_p2wsh_2of2(ctx, tx, i, stack, script, unified);
	}

	if (is_p2tr(spent->script, spent->script_len, xonly)) {
		const struct wally_tx_witness_item *sig_item;
		secp256k1_xonly_pubkey key;
		u8 digest[SHA256_LEN];
		u32 sighash;

		if (stack->num_items != 1)
			return tal_fmt(ctx, "P2TR input %zu is not a key path"
				       " spend", i);
		sig_item = &stack->items[0];
		if (sig_item->witness_len == 64)
			sighash = WALLY_SIGHASH_DEFAULT;
		else if (sig_item->witness_len == 65)
			sighash = sig_item->witness[64];
		else
			return tal_fmt(ctx, "P2TR input %zu has a %zu byte"
				       " signature", i, sig_item->witness_len);

		/* BIP341: a 65-byte signature never carries 0x00. */
		if (sig_item->witness_len == 64 || sighash == SIGHASH_ALL) {
			if (!bip341_keypath_digest(tx, i, sighash, digest))
				return tal_fmt(ctx, "P2TR input %zu digest", i);
		} else if (unified && sighash == (SIGHASH_ALL|SIGHASH_UNIFIED)) {
			struct unified_sighash_input exec = { .script_type = 2 };
			struct sha256 d;
			if (!bitcoin_tx_unified_sighash(tx, i, sighash, &exec, &d))
				return tal_fmt(ctx, "P2TR input %zu digest", i);
			memcpy(digest, d.u.u8, sizeof(digest));
		} else
			return tal_fmt(ctx, "P2TR input %zu uses hash type"
				       " 0x%02x", i, sighash);

		if (!secp256k1_xonly_pubkey_parse(secp256k1_ctx, &key, xonly)
		    || secp256k1_schnorrsig_verify(secp256k1_ctx,
						   sig_item->witness, digest,
						   sizeof(digest), &key) != 1)
			return tal_fmt(ctx, "P2TR input %zu has an invalid"
				       " signature", i);
		return NULL;
	}

	return tal_fmt(ctx, "input %zu spends none of P2WPKH, P2WSH or P2TR", i);
}

bool psbt_input_witness_known(const struct bitcoin_tx *tx, size_t i)
{
	const struct wally_psbt *psbt = tx->psbt;
	const struct wally_tx_witness_stack *stack;
	const struct wally_tx_output *spent;
	const struct wally_tx_witness_item *last;
	struct sha256 wsh, h;
	struct pubkey k1, k2;
	u8 *script;

	if (i >= psbt->num_inputs)
		return true;
	stack = psbt->inputs[i].final_witness;
	spent = spent_output(psbt, i);
	if (!spent || !stack || stack->num_items == 0)
		return true;

	if (is_p2wpkh(spent->script, spent->script_len, NULL))
		return true;
	last = &stack->items[stack->num_items - 1];
	/* BIP341: a last item starting 0x50 is an annex, which is
	 * non-standard, so it's judged (and fails). */
	if (is_p2tr(spent->script, spent->script_len, NULL))
		return stack->num_items == 1
			|| (last->witness_len && last->witness[0] == 0x50);
	/* Spending any other witness program is non-standard, but for P2A
	 * with an empty witness, which BOLT 2 doesn't allow either. */
	if (!is_p2wsh(spent->script, spent->script_len, &wsh))
		return true;

	script = tal_dup_arr(tmpctx, u8, last->witness, last->witness_len, 0);
	sha256(&h, script, tal_bytelen(script));
	return stack->num_items < 2
		|| !sha256_eq(&h, &wsh)
		|| is_to_remote_anchored_witness_script(script,
							tal_bytelen(script))
		|| is_2of2_script(script, &k1, &k2);
}

struct bitcoin_tx *psbt_tx_with_spent_outputs(const tal_t *ctx,
					      struct wally_psbt *psbt TAKES)
{
	if (!taken(psbt))
		psbt = clone_psbt(tmpctx, psbt);
	for (size_t i = 0; i < psbt->num_inputs; i++) {
		const struct wally_psbt_input *in = &psbt->inputs[i];
		const struct wally_tx_output *out;

		/* Each digest then reads the spent output here, rather than
		 * hashing the whole prevtx again for every input. */
		if (!in->utxo || in->index >= in->utxo->num_outputs)
			continue;
		out = &in->utxo->outputs[in->index];
		psbt_input_set_wit_utxo(psbt, i,
					tal_dup_arr(tmpctx, u8, out->script,
						    out->script_len, 0),
					amount_sat(out->satoshi));
	}
	return bitcoin_tx_with_psbt(ctx, take(psbt));
}

const char *psbt_their_witnesses_invalid(const tal_t *ctx,
					 const struct bitcoin_tx *tx,
					 enum tx_role their_role,
					 int input_index_to_ignore,
					 bool unified, bool strict)
{
	for (size_t i = 0; i < tx->psbt->num_inputs; i++) {
		u64 serial;
		const char *bad;

		if (i == input_index_to_ignore
		    || !psbt_get_serial_id(&tx->psbt->inputs[i].unknowns,
					   &serial)
		    || serial % 2 != their_role)
			continue;
		if (!strict && !psbt_input_witness_known(tx, i))
			continue;
		bad = psbt_input_witness_invalid(ctx, tx, i, unified);
		if (bad)
			return bad;
	}
	return NULL;
}

/* Bitcoin Core src/policy/policy.h. Applied only to the type they belong to. */
#define MAX_STANDARD_P2WSH_STACK_ITEMS 100
#define MAX_STANDARD_P2WSH_STACK_ITEM_SIZE 80
#define MAX_STANDARD_P2WSH_SCRIPT_SIZE 3600
#define MAX_STANDARD_TAPSCRIPT_STACK_ITEM_SIZE 80

/* BIP341 annex tag. BIP342 leaf version is the low 7 bits of 0xc0. */
#define WITNESS_ANNEX_TAG 0x50
#define TAPROOT_LEAF_MASK 0xfe
#define TAPROOT_LEAF_TAPSCRIPT 0xc0

/* A P2WPKH witness is always a signature and a compressed pubkey.
 * No byte cap: the shape is fixed. */
static const char *check_p2wpkh_witness(const tal_t *ctx,
					const struct witness_element *elems,
					bool unified)
{
	size_t n = tal_count(elems);

	if (n != 2)
		return tal_fmt(ctx,
			       "witness is non-standard: "
			       "P2WPKH witness has %zu items, "
			       "expected a signature and a public key",
			       n);
	if (elems[1].len != PUBKEY_CMPR_LEN)
		return tal_fmt(ctx,
			       "witness is non-standard: "
			       "P2WPKH public key is %zu bytes",
			       elems[1].len);
	/* Sighash byte sits after the DER signature. No interpreter. */
	if (elems[0].len == 0
	    || (elems[0].data[elems[0].len - 1] != SIGHASH_ALL
		&& !(unified && elems[0].data[elems[0].len - 1]
		     == (SIGHASH_ALL|SIGHASH_UNIFIED))))
		return tal_fmt(ctx,
			       "witness is non-standard: "
			       "P2WPKH signature is not SIGHASH_ALL");
	return NULL;
}

/* Items below the witness script, not the script itself. */
static const char *check_p2wsh_witness(const tal_t *ctx,
				       const struct witness_element *elems)
{
	size_t n = tal_count(elems);
	size_t stack_items = n - 1;

	if (elems[n - 1].len > MAX_STANDARD_P2WSH_SCRIPT_SIZE)
		return tal_fmt(ctx,
			       "witness is non-standard: "
			       "witness script is %zu bytes",
			       elems[n - 1].len);

	if (stack_items > MAX_STANDARD_P2WSH_STACK_ITEMS)
		return tal_fmt(ctx,
			       "witness is non-standard: "
			       "%zu stack items",
			       stack_items);

	for (size_t i = 0; i < stack_items; i++) {
		if (elems[i].len > MAX_STANDARD_P2WSH_STACK_ITEM_SIZE)
			return tal_fmt(ctx,
				       "witness is non-standard: "
				       "stack item %zu is %zu bytes",
				       i, elems[i].len);
	}
	/* FIXME: no SIGHASH_ALL check. Which stack items are signatures
	 * depends on the witness script, so this needs an interpreter. */
	return NULL;
}

/* Key path has no size policy. Tapscript limits the items under the
 * script; an annex is non-standard. */
static const char *check_p2tr_witness(const tal_t *ctx,
				      const struct witness_element *elems,
				      bool unified)
{
	size_t n = tal_count(elems);
	const struct witness_element *control;

	if (n >= 2 && elems[n - 1].len > 0
	    && elems[n - 1].data[0] == WITNESS_ANNEX_TAG)
		return tal_fmt(ctx, "witness is non-standard: taproot annex");

	/* Key path. 64 bytes omits the sighash: that is SIGHASH_DEFAULT,
	 * which is SIGHASH_ALL. 65 bytes must end in SIGHASH_ALL. */
	if (n == 1) {
		const struct witness_element *sig = &elems[0];

		if (sig->len == 64
		    || (sig->len == 65 && sig->data[64] == SIGHASH_ALL)
		    || (unified && sig->len == 65
			&& sig->data[64] == (SIGHASH_ALL|SIGHASH_UNIFIED)))
			return NULL;
		return tal_fmt(ctx,
			       "witness is non-standard: "
			       "P2TR key path signature is not SIGHASH_ALL");
	}

	if (n < 2)
		return NULL;

	/* FIXME: no SIGHASH_ALL check on a script-path spend. Which stack
	 * items are signatures depends on the leaf script. */

	control = &elems[n - 1];
	if (control->len == 0)
		return tal_fmt(ctx,
			       "witness is non-standard: "
			       "empty taproot control block");
	if ((control->data[0] & TAPROOT_LEAF_MASK) != TAPROOT_LEAF_TAPSCRIPT)
		return NULL;

	for (size_t i = 0; i + 2 < n; i++) {
		if (elems[i].len > MAX_STANDARD_TAPSCRIPT_STACK_ITEM_SIZE)
			return tal_fmt(ctx,
				       "witness is non-standard: "
				       "tapscript stack item %zu is %zu bytes",
				       i, elems[i].len);
	}
	return NULL;
}

/* Our inputs carry witness_utxo. Peer inputs carry the prevtx in utxo. */
static const u8 *spent_scriptpubkey(const struct wally_psbt_input *in,
				    size_t *script_len)
{
	if (in->witness_utxo && in->witness_utxo->script) {
		*script_len = in->witness_utxo->script_len;
		return in->witness_utxo->script;
	}
	if (in->utxo && in->index < in->utxo->num_outputs
	    && in->utxo->outputs[in->index].script) {
		*script_len = in->utxo->outputs[in->index].script_len;
		return in->utxo->outputs[in->index].script;
	}
	*script_len = 0;
	return NULL;
}

const char *check_tx_signatures_witness(const tal_t *ctx,
					const struct wally_psbt_input *in,
					const struct witness *witness,
					bool unified)
{
	const struct witness_element *elems;
	const u8 *script;
	size_t script_len;

	if (!witness || !witness->witness_data ||
	    tal_count(witness->witness_data) == 0)
		return tal_fmt(ctx, "witness is empty");

	elems = witness_stack_elements(ctx, witness->witness_data);
	if (!elems)
		return tal_fmt(ctx, "witness is truncated or has trailing bytes");

	if (tal_count(elems) == 0)
		return tal_fmt(ctx, "witness is empty");

	script = spent_scriptpubkey(in, &script_len);
	if (script && is_p2wpkh(script, script_len, NULL))
		return check_p2wpkh_witness(ctx, elems, unified);
	if (script && is_p2wsh(script, script_len, NULL))
		return check_p2wsh_witness(ctx, elems);
	if (script && is_p2tr(script, script_len, NULL))
		return check_p2tr_witness(ctx, elems, unified);

	return tal_fmt(ctx,
		       "witness is non-standard: "
		       "input is not P2WPKH, P2WSH or P2TR");
}

const char *check_tx_signatures_witness_count(const tal_t *ctx,
					     const struct wally_psbt *psbt,
					     enum tx_role their_role,
					     size_t num_witnesses,
					     int input_index_to_ignore)
{
	size_t peer_inputs = 0;

	for (size_t i = 0; i < psbt->num_inputs; i++) {
		u64 in_serial;

		if (input_index_to_ignore >= 0
		    && i == (size_t)input_index_to_ignore)
			continue;
		if (!psbt_get_serial_id(&psbt->inputs[i].unknowns, &in_serial))
			continue;
		if (in_serial % 2 != their_role)
			continue;
		peer_inputs++;
	}

	if (peer_inputs != num_witnesses)
		return tal_fmt(ctx,
			       "Mismatched witness stack count, expecting %zu, "
			       "got %zu",
			       peer_inputs, num_witnesses);
	return NULL;
}
