#include "config.h"
#include <bitcoin/psbt.h>
#include <bitcoin/unified_sighash.h>
#include <ccan/crypto/sha256/sha256.h>
#include <string.h>
#include <wally_psbt.h>

static void unified_script(struct sha256_ctx *ctx, const u8 *script, size_t len)
{
	u8 compact[9];
	size_t n = varint_put(compact, len);
	sha256_update(ctx, compact, n);
	sha256_update(ctx, script, len);
}

static void unified_output(struct sha256_ctx *ctx, u64 amount,
			   const u8 *script, size_t len)
{
	sha256_le64(ctx, amount);
	unified_script(ctx, script, len);
}

static void unified_prevout(struct sha256_ctx *ctx,
			    const struct wally_tx_input *input)
{
	sha256_update(ctx, input->txhash, sizeof(input->txhash));
	sha256_le32(ctx, input->index);
}

static void unified_append_hash(struct sha256_ctx *msg, struct sha256_ctx *part)
{
	struct sha256 hash;
	sha256_done(part, &hash);
	sha256_update(msg, &hash, sizeof(hash));
}

bool bitcoin_unified_sighash(const struct wally_tx *tx, size_t input,
			    u8 hash_type,
			    const struct bitcoin_tx_output *spent,
			    size_t num_spent,
			    const struct unified_sighash_input *exec,
			    struct sha256 *digest)
{
	struct sha256_ctx msg, part;
	struct sha256 tag;
	u8 base = hash_type & 0x1f;
	bool acp = (hash_type & 0x80) != 0;
	if (!tx || !exec || !digest || !spent || !(hash_type & 0x20)
	    || input >= tx->num_inputs || num_spent != tx->num_inputs
	    || exec->script_type > 3
	    || (base == 3 && input >= tx->num_outputs))
		return false;
	if (exec->script_type >= 2
	    && ((hash_type & ~(0x80 | 0x20 | 3)) || base < 1 || base > 3))
		return false;
	if (exec->script_type == 3 && !exec->tapleaf)
		return false;
	if (exec->script_type < 2 && (exec->annex || exec->tapleaf))
		return false;
	if (exec->annex
	    && (!tal_bytelen(exec->annex) || exec->annex[0] != 0x50))
		return false;

	sha256(&tag, "UnifiedSighash", strlen("UnifiedSighash"));
	sha256_init(&msg);
	sha256_update(&msg, &tag, sizeof(tag));
	sha256_update(&msg, &tag, sizeof(tag));
	sha256_u8(&msg, 0);
	sha256_u8(&msg, hash_type);
	sha256_le32(&msg, tx->version);
	sha256_le32(&msg, tx->locktime);
	sha256_u8(&msg, 0);
	if (!acp) {
		sha256_init(&part);
		for (size_t i = 0; i < tx->num_inputs; i++)
			unified_prevout(&part, &tx->inputs[i]);
		unified_append_hash(&msg, &part);
		sha256_init(&part);
		for (size_t i = 0; i < num_spent; i++)
			sha256_le64(&part, spent[i].amount.satoshis); /* Raw: digest serialization. */
		unified_append_hash(&msg, &part);
		sha256_init(&part);
		for (size_t i = 0; i < num_spent; i++)
			unified_script(&part, spent[i].script, tal_bytelen(spent[i].script));
		unified_append_hash(&msg, &part);
		sha256_init(&part);
		for (size_t i = 0; i < tx->num_inputs; i++)
			sha256_le32(&part, tx->inputs[i].sequence);
		unified_append_hash(&msg, &part);
	}
	if (base != 2 && base != 3) {
		sha256_init(&part);
		for (size_t i = 0; i < tx->num_outputs; i++)
			unified_output(&part, tx->outputs[i].satoshi,
				       tx->outputs[i].script, tx->outputs[i].script_len);
		unified_append_hash(&msg, &part);
	}
	sha256_u8(&msg, exec->script_type);
	if (acp) {
		unified_prevout(&msg, &tx->inputs[input]);
		unified_output(&msg, spent[input].amount.satoshis, /* Raw: digest serialization. */
			       spent[input].script, tal_bytelen(spent[input].script));
		sha256_le32(&msg, tx->inputs[input].sequence);
	} else
		sha256_le32(&msg, input);
	if (exec->script_type < 2)
		unified_script(&msg, exec->script_code, tal_bytelen(exec->script_code));
	else {
		sha256_u8(&msg, exec->annex != NULL);
		if (exec->annex) {
			sha256_init(&part);
			unified_script(&part, exec->annex, tal_bytelen(exec->annex));
			unified_append_hash(&msg, &part);
		}
	}
	if (base == 3) {
		sha256_init(&part);
		unified_output(&part, tx->outputs[input].satoshi,
			       tx->outputs[input].script, tx->outputs[input].script_len);
		unified_append_hash(&msg, &part);
	}
	if (exec->script_type == 3) {
		sha256_update(&msg, exec->tapleaf, sizeof(*exec->tapleaf));
		sha256_u8(&msg, 0);
		sha256_le32(&msg, exec->codeseparator);
	}
	sha256_done(&msg, digest);
	return true;
}

bool bitcoin_tx_unified_sighash(const struct bitcoin_tx *tx, size_t input,
			       u8 hash_type,
			       const struct unified_sighash_input *exec,
			       struct sha256 *digest)
{
	struct bitcoin_tx_output *spent;
	bool ok = false;
	size_t elements;
	if (!tx || !tx->psbt || !tx->wtx
	    || wally_psbt_is_elements(tx->psbt, &elements) != WALLY_OK || elements
	    || tx->psbt->num_inputs != tx->wtx->num_inputs
	    || input >= tx->wtx->num_inputs)
		return false;
	spent = tal_arrz(NULL, struct bitcoin_tx_output, tx->wtx->num_inputs);
	for (size_t i = 0; i < tx->wtx->num_inputs; i++) {
		const struct wally_psbt_input *p = &tx->psbt->inputs[i];
		const struct wally_tx_output *out = p->witness_utxo;
		/* ANYONECANPAY does not commit to unrelated prevout metadata. */
		if ((hash_type & 0x80) && i != input)
			continue;
		if (p->utxo) {
			const struct wally_tx_output *full;

			if (tx->wtx->inputs[i].index >= p->utxo->num_outputs)
				goto done;
			/* Only the txid costs anything here: deriving it means
			 * hashing the whole previous transaction, and this loop
			 * runs for every input of every signature, so doing it
			 * where `witness_utxo` already answers is quadratic in
			 * whole transactions.  Check the input being signed,
			 * which is every input across a complete signing pass,
			 * and otherwise only where there is nothing else to
			 * read the output from.  The comparison below is cheap,
			 * so it stays for every input. */
			if (i == input || !out) {
				struct bitcoin_txid prev;
				wally_txid(p->utxo, &prev);
				if (memcmp(&prev, tx->wtx->inputs[i].txhash,
					   sizeof(prev)))
					goto done;
			}
			full = &p->utxo->outputs[tx->wtx->inputs[i].index];
			if (out && (out->satoshi != full->satoshi
				    || out->script_len != full->script_len
				    || memcmp(out->script, full->script, out->script_len)))
				goto done;
			out = full;
		}
		if (!out)
			goto done;
		spent[i].amount = amount_sat(out->satoshi);
		spent[i].script = tal_dup_arr(spent, u8, out->script, out->script_len, 0);
	}
	ok = bitcoin_unified_sighash(tx->wtx, input, hash_type, spent,
				    tx->wtx->num_inputs, exec, digest);
done:
	tal_free(spent);
	return ok;
}
