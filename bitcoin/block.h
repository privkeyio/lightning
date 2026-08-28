#ifndef LIGHTNING_BITCOIN_BLOCK_H
#define LIGHTNING_BITCOIN_BLOCK_H
#include "config.h"
#include "bitcoin/shadouble.h"
#include <ccan/endian/endian.h>
#include <ccan/short_types/short_types.h>
#include <ccan/structeq/structeq.h>

struct chainparams;

enum dynafed_params_type {
	DYNAFED_PARAMS_NULL,
	DYNAFED_PARAMS_COMPACT,
	DYNAFED_PARAMS_FULL,
};

struct bitcoin_blkid {
	struct sha256_double shad;
};
/* Define bitcoin_blkid_eq (no padding) */
STRUCTEQ_DEF(bitcoin_blkid, 0, shad.sha.u);

/* Number of extra bytes a v2 (BLAKE2b) header carries after the 80 byte
 * v1 layout. */
#define BLOCK_HEADER_V2_EXTRA_LEN 84

struct bitcoin_block_hdr {
	le32 version;
	struct bitcoin_blkid prev_hash;
	struct sha256_double merkle_hash;
	le32 timestamp;
	le32 target;
	le32 nonce;
	struct bitcoin_blkid hash;

	/* A v2 header is signalled by the top bit of the version, and is
	 * identified by BLAKE2b rather than SHA256d.  The fields below are
	 * only meaningful when header_v2 is set. */
	bool header_v2;
	/* timestamp as it appears on the wire: nTime is this plus
	 * time_offset when BLOCK_HEADER_FLAG_USE_TIME_OFFSET is set. */
	u32 time_on_wire;
	u32 nonce2, nonce3;
	u8 extranonce[16];
	u32 time_offset;
	u16 txcount;
	u8 flags;
	u8 xor_key_mask_clear_bits;
	u8 xor_key[16];
	s32 height;
	u8 mm_rhs[32];
};

struct bitcoin_block {
	struct bitcoin_block_hdr hdr;
	/* tal_count shows now many */
	struct bitcoin_tx **tx;
	struct bitcoin_txid *txids;
};

struct bitcoin_block *
bitcoin_block_from_hex(const tal_t *ctx, const struct chainparams *chainparams,
		       const char *hex, size_t hexlen);

/* Compute the double SHA block ID from the block header. */
void bitcoin_block_blkid(const struct bitcoin_block *block,
			 struct bitcoin_blkid *out);

/* Marshalling/unmarshaling over the wire */
void towire_bitcoin_blkid(u8 **pptr, const struct bitcoin_blkid *blkid);
void fromwire_bitcoin_blkid(const u8 **cursor, size_t *max,
			   struct bitcoin_blkid *blkid);
void fromwire_chainparams(const u8 **cursor, size_t *max,
			  const struct chainparams **chainparams);
void towire_chainparams(u8 **cursor, const struct chainparams *chainparams);

bool bitcoin_blkid_from_hex(const char *hexstr, size_t hexstr_len,
			    struct bitcoin_blkid *blkid);
bool bitcoin_blkid_to_hex(const struct bitcoin_blkid *blkid,
			  char *hexstr, size_t hexstr_len);

char *fmt_bitcoin_blkid(const tal_t *ctx,
			const struct bitcoin_blkid *blkid);

#endif /* LIGHTNING_BITCOIN_BLOCK_H */
