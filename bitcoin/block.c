#include "config.h"
#include <assert.h>
#include <bitcoin/blake2b.h>
#include <bitcoin/block.h>
#include <bitcoin/tx.h>
#include <ccan/mem/mem.h>
#include <ccan/str/hex/hex.h>
#include <common/utils.h>

/* Top bit of the version marks the redesigned block header introduced by the
 * BLAKE2b hardfork. */
#define BLOCK_HEADER_V2_VERSION_FLAG 0x80000000

/* Bit in hdr->flags meaning nTime is time_on_wire plus time_offset. */
#define BLOCK_HEADER_FLAG_USE_TIME_OFFSET 4

/* Sets *cursor to NULL and returns NULL when a pull fails. */
static const u8 *pull(const u8 **cursor, size_t *max, void *copy, size_t n)
{
	const u8 *p = *cursor;

	if (*max < n) {
		*cursor = NULL;
		*max = 0;
		/* Just make sure we don't leak uninitialized mem! */
		if (copy)
			memset(copy, 0, n);
		return NULL;
	}
	*cursor += n;
	*max -= n;
	assert(p);
	if (copy)
		memcpy(copy, p, n);
	return memcheck(p, n);
}

static u32 pull_le32(const u8 **cursor, size_t *max)
{
	le32 ret;

	if (!pull(cursor, max, &ret, sizeof(ret)))
		return 0;
	return le32_to_cpu(ret);
}

static u16 pull_le16(const u8 **cursor, size_t *max)
{
	le16 ret;

	if (!pull(cursor, max, &ret, sizeof(ret)))
		return 0;
	return le16_to_cpu(ret);
}

static u8 pull_u8(const u8 **cursor, size_t *max)
{
	u8 ret;

	if (!pull(cursor, max, &ret, sizeof(ret)))
		return 0;
	return ret;
}

static u64 pull_varint(const u8 **cursor, size_t *max)
{
	u64 ret;
	size_t len;

	len = varint_get(*cursor, *max, &ret);
	if (len == 0) {
		*cursor = NULL;
		*max = 0;
		return 0;
	}
	pull(cursor, max, NULL, len);
	return ret;
}


static void sha256_varint(struct sha256_ctx *ctx, u64 val)
{
	u8 vt[VARINT_MAX_LEN];
	size_t vtlen;
	vtlen = varint_put(vt, val);
	sha256_update(ctx, vt, vtlen);
}

static void bitcoin_block_pull_dynafed_params(const u8 **cursor, size_t *len, struct sha256_ctx *shactx)
{
	u8 type;
	u64 l1, l2;
	pull(cursor, len, &type, 1);
	sha256_update(shactx, &type, 1);
	switch ((enum dynafed_params_type)type) {
	case DYNAFED_PARAMS_NULL:
		break;
	case DYNAFED_PARAMS_COMPACT:
		/* "scriptPubKey" used for block signing */
		l1 = pull_varint(cursor, len);
		sha256_varint(shactx, l1);
		sha256_update(shactx, *cursor, l1);
		pull(cursor, len, NULL, l1);

		/* signblock_witness_limit */
		sha256_update(shactx, *cursor, 4);
		pull(cursor, len, NULL, 4);

		/* Skip elided_root */
		sha256_update(shactx, *cursor, 32);
		pull(cursor, len, NULL, 32);
		break;

	case DYNAFED_PARAMS_FULL:
		/* "scriptPubKey" used for block signing */
		l1 = pull_varint(cursor, len);
		sha256_varint(shactx, l1);
		sha256_update(shactx, *cursor, l1);
		pull(cursor, len, NULL, l1);

		/* signblock_witness_limit */
		sha256_update(shactx, *cursor, 4);
		pull(cursor, len, NULL, 4);

		/* fedpeg_program */
		l1 = pull_varint(cursor, len);
		sha256_varint(shactx, l1);
		sha256_update(shactx, *cursor, l1);
		pull(cursor, len, NULL, l1);

		/* fedpegscript */
		l1 = pull_varint(cursor, len);
		sha256_varint(shactx, l1);
		sha256_update(shactx, *cursor, l1);
		pull(cursor, len, NULL, l1);

		/* extension space */
		l2 = pull_varint(cursor, len);
		sha256_varint(shactx, l2);
		for (size_t i = 0; i < l2; i++) {
			l1 = pull_varint(cursor, len);
			sha256_varint(shactx, l1);
			sha256_update(shactx, *cursor, l1);
			pull(cursor, len, NULL, l1);
		}
		break;
	}
}

static void bitcoin_block_pull_dynafed_details(const u8 **cursor, size_t *len, struct sha256_ctx *shactx)
{
	bitcoin_block_pull_dynafed_params(cursor, len, shactx);
	bitcoin_block_pull_dynafed_params(cursor, len, shactx);

	/* Consume the signblock_witness */
	u64 numwitnesses = pull_varint(cursor, len);
	for (size_t i=0; i<numwitnesses; i++) {
		u64 witsize = pull_varint(cursor, len);
		pull(cursor, len, NULL, witsize);
	}
}

static size_t push_le32(u8 *dest, u32 v)
{
	le32 l = cpu_to_le32(v);

	memcpy(dest, &l, sizeof(l));
	return sizeof(l);
}

static size_t push_zeros(u8 *dest, size_t n)
{
	memset(dest, 0, n);
	return n;
}

/* BIP340-style tagged hash, as the v2 block id uses throughout. */
static void tagged_hash_init(struct sha256_ctx *ctx, const char *tag)
{
	struct sha256 taghash;

	sha256(&taghash, tag, strlen(tag));
	sha256_init(ctx);
	sha256_update(ctx, &taghash, sizeof(taghash));
	sha256_update(ctx, &taghash, sizeof(taghash));
}

/* The 84 bytes a v2 header carries beyond the v1 layout. */
static void pull_block_hdr_v2(const u8 **cursor, size_t *len,
			      struct bitcoin_block_hdr *hdr)
{
	hdr->nonce2 = pull_le32(cursor, len);
	hdr->nonce3 = pull_le32(cursor, len);
	pull(cursor, len, hdr->extranonce, sizeof(hdr->extranonce));
	hdr->time_offset = pull_le32(cursor, len);
	hdr->txcount = pull_le16(cursor, len);
	hdr->flags = pull_u8(cursor, len);
	hdr->xor_key_mask_clear_bits = pull_u8(cursor, len);
	pull(cursor, len, hdr->xor_key, sizeof(hdr->xor_key));
	hdr->height = (s32)pull_le32(cursor, len);
	pull(cursor, len, hdr->mm_rhs, sizeof(hdr->mm_rhs));

	/* The wire carries nTime minus the offset, so that a miner can grind
	 * the offset without the timestamp appearing to move. */
	if (hdr->flags & BLOCK_HEADER_FLAG_USE_TIME_OFFSET)
		hdr->timestamp = hdr->time_on_wire + hdr->time_offset;
	else
		hdr->timestamp = hdr->time_on_wire;
}

/* The v2 block id: two BLAKE2b passes over a tree of tagged SHA256 hashes,
 * finally XORed with a mask derived from the miner's XOR key.  This mirrors
 * CBlockHeader::GetHash() in the BLAKE2b hardfork. */
static void block_hdr_v2_blkid(const struct bitcoin_block_hdr *hdr,
			       struct bitcoin_blkid *out)
{
	static const u8 zeros[32];
	struct sha256_ctx shactx;
	struct sha256 xor_key_hash, xor_key_mask, prevblock_hidden, h1, h2;
	u8 prev_sane[32], buf[160], hash[32];
	size_t n = 0;

	/* uint256::ReversedBytes(): display order, not internal order. */
	for (size_t i = 0; i < 32; i++)
		prev_sane[i] = hdr->prev_hash.shad.sha.u.u8[31 - i];

	/* A pooled miner only learns the XOR key once it finds a block, so the
	 * header commits to its hash rather than to the key. */
	tagged_hash_init(&shactx, "Bitcoin block hash PoW XOR key");
	sha256_update(&shactx, hdr->xor_key, sizeof(hdr->xor_key));
	sha256_done(&shactx, &xor_key_hash);

	memset(&xor_key_mask, 0, sizeof(xor_key_mask));
	if (!memeqzero(hdr->xor_key, sizeof(hdr->xor_key))) {
		size_t clear_bytes = hdr->xor_key_mask_clear_bits / 8;

		tagged_hash_init(&shactx, "Bitcoin block hash PoW XOR mask");
		sha256_update(&shactx, hdr->xor_key, sizeof(hdr->xor_key));
		sha256_done(&shactx, &xor_key_mask);

		if (clear_bytes > sizeof(xor_key_mask))
			clear_bytes = sizeof(xor_key_mask);
		memset(xor_key_mask.u.u8, 0, clear_bytes);
		if (clear_bytes < sizeof(xor_key_mask))
			xor_key_mask.u.u8[clear_bytes]
				&= 0xff >> (hdr->xor_key_mask_clear_bits % 8);
	}

	tagged_hash_init(&shactx, "Bitcoin prevblock header, hashed");
	sha256_update(&shactx, prev_sane, sizeof(prev_sane));
	sha256_done(&shactx, &prevblock_hidden);

	/* These fields are invisible to the mining machine, so that it cannot
	 * brick itself at some future block version, time or difficulty. */
	tagged_hash_init(&shactx, "Bitcoin block header 1");
	sha256_le32(&shactx, hdr->version);
	sha256_update(&shactx, prev_sane, sizeof(prev_sane));
	sha256_le32(&shactx, (u32)hdr->height);
	sha256_update(&shactx, &hdr->merkle_hash, sizeof(hdr->merkle_hash));
	sha256_le32(&shactx, hdr->time_on_wire);
	sha256_u8(&shactx, 0); /* Reserved for extended 40-bit time */
	sha256_le32(&shactx, hdr->target);
	sha256_le32(&shactx, hdr->txcount);
	sha256_u8(&shactx, hdr->flags);
	sha256_u8(&shactx, hdr->xor_key_mask_clear_bits);
	sha256_update(&shactx, &xor_key_hash, sizeof(xor_key_hash));
	sha256_done(&shactx, &h1);

	tagged_hash_init(&shactx, "Merge-mining hook");
	sha256_update(&shactx, &h1, sizeof(h1));
	sha256_update(&shactx, zeros, 32);
	sha256_update(&shactx, hdr->mm_rhs, sizeof(hdr->mm_rhs));
	sha256_done(&shactx, &h2);

	/* What gets sent to mining machines over Sv1: 52 bytes of coinb1 plus
	 * extranonce. */
	memset(buf, 0, 4);
	memcpy(buf + 4, &h2, sizeof(h2));
	memcpy(buf + 4 + sizeof(h2), hdr->extranonce, sizeof(hdr->extranonce));
	blake2b(hash, sizeof(hash), buf, 4 + sizeof(h2) + sizeof(hdr->extranonce));

	/* What the mining ASIC itself sees, in one of four layouts. */
	switch (hdr->flags & 3) {
	case 3:
		n += push_zeros(buf + n, 32);
		/* fallthrough */
	case 2:
		n += push_zeros(buf + n, 48);
		memcpy(buf + n, &h2, sizeof(h2));
		n += sizeof(h2);
		n += push_le32(buf + n, hdr->nonce);
		n += push_le32(buf + n, hdr->nonce2);
		n += push_le32(buf + n, hdr->time_offset);
		n += push_le32(buf + n, hdr->nonce3);
		memcpy(buf + n, hash, sizeof(hash));
		n += sizeof(hash);
		break;
	case 0:
		/* The top six bytes are dropped, so the machine cannot see
		 * which chain it is extending. */
		memset(prevblock_hidden.u.u8, 0, 6);
		memcpy(buf + n, &prevblock_hidden, sizeof(prevblock_hidden));
		n += sizeof(prevblock_hidden);
		n += push_le32(buf + n, hdr->nonce);
		n += push_le32(buf + n, hdr->nonce2);
		n += push_le32(buf + n, hdr->time_offset);
		n += push_le32(buf + n, hdr->nonce3);
		memcpy(buf + n, hash, sizeof(hash));
		n += sizeof(hash);
		break;
	case 1:
		n += push_le32(buf + n, hdr->nonce);
		n += push_le32(buf + n, hdr->nonce2);
		n += push_le32(buf + n, hdr->nonce3);
		n += push_le32(buf + n, hdr->time_offset);
		memcpy(buf + n, hash, sizeof(hash));
		n += sizeof(hash);
		memcpy(buf + n, &h2, sizeof(h2));
		n += sizeof(h2);
		break;
	}
	assert(n <= sizeof(buf));
	blake2b(hash, sizeof(hash), buf, n);

	/* Reversed, so that the id prints as the BLAKE2b digest reads. */
	for (size_t i = 0; i < sizeof(hash); i++)
		out->shad.sha.u.u8[31 - i] = hash[i] ^ xor_key_mask.u.u8[i];
}

/* Encoding is <blockhdr> <varint-num-txs> <tx>... */
struct bitcoin_block *
bitcoin_block_from_hex(const tal_t *ctx, const struct chainparams *chainparams,
		       const char *hex, size_t hexlen)
{
	struct bitcoin_block *b;
	u8 *linear_tx;
	const u8 *p;
	size_t len, i, num, templen;
	struct sha256_ctx shactx;
	bool is_dynafed;
	u32 height;

	if (hexlen && hex[hexlen-1] == '\n')
		hexlen--;

	/* Set up the block for success. */
	b = tal(ctx, struct bitcoin_block);

	/* De-hex the array. */
	len = hex_data_size(hexlen);
	p = linear_tx = tal_arr(ctx, u8, len);
	if (!hex_decode(hex, hexlen, linear_tx, len))
		return tal_free(b);

	sha256_init(&shactx);
	b->hdr.header_v2 = false;

	b->hdr.version = pull_le32(&p, &len);
	sha256_le32(&shactx, b->hdr.version);

	pull(&p, &len, &b->hdr.prev_hash, sizeof(b->hdr.prev_hash));
	sha256_update(&shactx, &b->hdr.prev_hash, sizeof(b->hdr.prev_hash));

	pull(&p, &len, &b->hdr.merkle_hash, sizeof(b->hdr.merkle_hash));
	sha256_update(&shactx, &b->hdr.merkle_hash, sizeof(b->hdr.merkle_hash));

	b->hdr.time_on_wire = pull_le32(&p, &len);
	b->hdr.timestamp = b->hdr.time_on_wire;
	sha256_le32(&shactx, b->hdr.time_on_wire);

	if (is_elements(chainparams)) {
		/* A dynafed block is signalled by setting the MSB of the version. */
		is_dynafed = (b->hdr.version >> 31 == 1);

		/* elements_header.height */
		height = pull_le32(&p, &len);
		sha256_le32(&shactx, height);

		if (is_dynafed) {
			bitcoin_block_pull_dynafed_details(&p, &len, &shactx);
		} else {
			/* elemens_header.challenge */
			templen = pull_varint(&p, &len);
			sha256_varint(&shactx, templen);
			sha256_update(&shactx, p, templen);
			pull(&p, &len, NULL, templen);

			/* elements_header.solution. Not hashed since it'd be
			 * a circular dependency. */
			templen = pull_varint(&p, &len);
			pull(&p, &len, NULL, templen);
		}

	} else {
		b->hdr.target = pull_le32(&p, &len);
		sha256_le32(&shactx, b->hdr.target);

		b->hdr.nonce = pull_le32(&p, &len);
		sha256_le32(&shactx, b->hdr.nonce);

		/* The BLAKE2b hardfork marks its redesigned header by setting
		 * the top bit of the version.  It carries 84 further bytes,
		 * and is identified by BLAKE2b rather than SHA256d. */
		b->hdr.header_v2
			= (b->hdr.version & BLOCK_HEADER_V2_VERSION_FLAG) != 0;
	}

	if (b->hdr.header_v2) {
		pull_block_hdr_v2(&p, &len, &b->hdr);
		if (!p)
			return tal_free(b);
		block_hdr_v2_blkid(&b->hdr, &b->hdr.hash);
	} else
		sha256_double_done(&shactx, &b->hdr.hash.shad);

	num = pull_varint(&p, &len);
	b->tx = tal_arr(b, struct bitcoin_tx *, num);
	b->txids = tal_arr(b, struct bitcoin_txid, num);
	for (i = 0; i < num; i++) {
		b->tx[i] = pull_bitcoin_tx_only(b->tx, &p, &len);
		b->tx[i]->chainparams = chainparams;
		bitcoin_txid(b->tx[i], &b->txids[i]);
	}

	/* We should end up not overrunning, nor have extra */
	if (!p || len)
		return tal_free(b);

	tal_free(linear_tx);
	return b;
}

void bitcoin_block_blkid(const struct bitcoin_block *b,
			 struct bitcoin_blkid *out)
{
	*out = b->hdr.hash;
}

bool bitcoin_blkid_from_hex(const char *hexstr, size_t hexstr_len,
			   struct bitcoin_blkid *blkid)
{
	if (!hex_decode(hexstr, hexstr_len, blkid, sizeof(*blkid)))
		return false;
	reverse_bytes(blkid->shad.sha.u.u8, sizeof(blkid->shad.sha.u.u8));
	return true;
}

bool bitcoin_blkid_to_hex(const struct bitcoin_blkid *blkid,
			 char *hexstr, size_t hexstr_len)
{
	struct sha256_double rev = blkid->shad;
	reverse_bytes(rev.sha.u.u8, sizeof(rev.sha.u.u8));
	return hex_encode(&rev, sizeof(rev), hexstr, hexstr_len);
}

char *fmt_bitcoin_blkid(const tal_t *ctx, const struct bitcoin_blkid *blkid)
{
	char *hexstr = tal_arr(ctx, char, hex_str_size(sizeof(*blkid)));

	bitcoin_blkid_to_hex(blkid, hexstr, hex_str_size(sizeof(*blkid)));
	return hexstr;
}

void fromwire_bitcoin_blkid(const u8 **cursor, size_t *max,
			    struct bitcoin_blkid *blkid)
{
	fromwire_sha256_double(cursor, max, &blkid->shad);
}

void towire_bitcoin_blkid(u8 **pptr, const struct bitcoin_blkid *blkid)
{
	towire_sha256_double(pptr, &blkid->shad);
}


void towire_chainparams(u8 **cursor, const struct chainparams *chainparams)
{
	towire_bitcoin_blkid(cursor, &chainparams->genesis_blockhash);
}

void fromwire_chainparams(const u8 **cursor, size_t *max,
			  const struct chainparams **chainparams)
{
	struct bitcoin_blkid genesis;
	fromwire_bitcoin_blkid(cursor, max, &genesis);
	*chainparams = chainparams_by_chainhash(&genesis);
}
