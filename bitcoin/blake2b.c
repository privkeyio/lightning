/*
   BLAKE2 reference source code package - reference C implementations

   Copyright 2012, Samuel Neves <sneves@dei.uc.pt>.  You may use this under the
   terms of the CC0, the OpenSSL Licence, or the Apache Public License 2.0, at
   your option.  The terms of these licenses can be found at:

   - CC0 1.0 Universal : http://creativecommons.org/publicdomain/zero/1.0
   - OpenSSL license   : https://www.openssl.org/source/license.html
   - Apache 2.0        : http://www.apache.org/licenses/LICENSE-2.0

   More information about the BLAKE2 hash function can be found at
   https://blake2.net.
*/
#include "config.h"
#include <assert.h>
#include <bitcoin/blake2b.h>
#include <string.h>

static const uint64_t blake2b_IV[8] = {
	0x6a09e667f3bcc908ULL, 0xbb67ae8584caa73bULL,
	0x3c6ef372fe94f82bULL, 0xa54ff53a5f1d36f1ULL,
	0x510e527fade682d1ULL, 0x9b05688c2b3e6c1fULL,
	0x1f83d9abfb41bd6bULL, 0x5be0cd19137e2179ULL
};

static const uint8_t blake2b_sigma[12][16] = {
	{  0,  1,  2,  3,  4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 15 },
	{ 14, 10,  4,  8,  9, 15, 13,  6,  1, 12,  0,  2, 11,  7,  5,  3 },
	{ 11,  8, 12,  0,  5,  2, 15, 13, 10, 14,  3,  6,  7,  1,  9,  4 },
	{  7,  9,  3,  1, 13, 12, 11, 14,  2,  6,  5, 10,  4,  0, 15,  8 },
	{  9,  0,  5,  7,  2,  4, 10, 15, 14,  1, 11, 12,  6,  8,  3, 13 },
	{  2, 12,  6, 10,  0, 11,  8,  3,  4, 13,  7,  5, 15, 14,  1,  9 },
	{ 12,  5,  1, 15, 14, 13,  4, 10,  0,  7,  6,  3,  9,  2,  8, 11 },
	{ 13, 11,  7, 14, 12,  1,  3,  9,  5,  0, 15,  4,  8,  6,  2, 10 },
	{  6, 15, 14,  9, 11,  3,  0,  8, 12,  2, 13,  7,  1,  4, 10,  5 },
	{ 10,  2,  8,  4,  7,  6,  1,  5, 15, 11,  9, 14,  3, 12, 13,  0 },
	{  0,  1,  2,  3,  4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 15 },
	{ 14, 10,  4,  8,  9, 15, 13,  6,  1, 12,  0,  2, 11,  7,  5,  3 }
};

static uint64_t load_le64(const uint8_t *p)
{
	return (uint64_t)p[0] | ((uint64_t)p[1] << 8) | ((uint64_t)p[2] << 16)
		| ((uint64_t)p[3] << 24) | ((uint64_t)p[4] << 32)
		| ((uint64_t)p[5] << 40) | ((uint64_t)p[6] << 48)
		| ((uint64_t)p[7] << 56);
}

static void store_le64(uint8_t *p, uint64_t v)
{
	for (size_t i = 0; i < 8; i++)
		p[i] = (uint8_t)(v >> (8 * i));
}

static uint64_t rotr64(uint64_t v, unsigned int n)
{
	return (v >> n) | (v << (64 - n));
}

static void blake2b_increment_counter(struct blake2b_state *S, uint64_t inc)
{
	S->t[0] += inc;
	S->t[1] += (S->t[0] < inc);
}

#define G(r, i, a, b, c, d)					\
	do {							\
		a = a + b + m[blake2b_sigma[r][2 * i + 0]];	\
		d = rotr64(d ^ a, 32);				\
		c = c + d;					\
		b = rotr64(b ^ c, 24);				\
		a = a + b + m[blake2b_sigma[r][2 * i + 1]];	\
		d = rotr64(d ^ a, 16);				\
		c = c + d;					\
		b = rotr64(b ^ c, 63);				\
	} while (0)

#define ROUND(r)					\
	do {						\
		G(r, 0, v[ 0], v[ 4], v[ 8], v[12]);	\
		G(r, 1, v[ 1], v[ 5], v[ 9], v[13]);	\
		G(r, 2, v[ 2], v[ 6], v[10], v[14]);	\
		G(r, 3, v[ 3], v[ 7], v[11], v[15]);	\
		G(r, 4, v[ 0], v[ 5], v[10], v[15]);	\
		G(r, 5, v[ 1], v[ 6], v[11], v[12]);	\
		G(r, 6, v[ 2], v[ 7], v[ 8], v[13]);	\
		G(r, 7, v[ 3], v[ 4], v[ 9], v[14]);	\
	} while (0)

static void blake2b_compress(struct blake2b_state *S,
			     const uint8_t block[BLAKE2B_BLOCKBYTES])
{
	uint64_t m[16], v[16];
	size_t i;

	for (i = 0; i < 16; i++)
		m[i] = load_le64(block + i * 8);

	for (i = 0; i < 8; i++)
		v[i] = S->h[i];

	v[ 8] = blake2b_IV[0];
	v[ 9] = blake2b_IV[1];
	v[10] = blake2b_IV[2];
	v[11] = blake2b_IV[3];
	v[12] = blake2b_IV[4] ^ S->t[0];
	v[13] = blake2b_IV[5] ^ S->t[1];
	v[14] = blake2b_IV[6] ^ S->f[0];
	v[15] = blake2b_IV[7] ^ S->f[1];

	ROUND(0); ROUND(1); ROUND(2); ROUND(3);
	ROUND(4); ROUND(5); ROUND(6); ROUND(7);
	ROUND(8); ROUND(9); ROUND(10); ROUND(11);

	for (i = 0; i < 8; i++)
		S->h[i] = S->h[i] ^ v[i] ^ v[i + 8];
}

#undef G
#undef ROUND

void blake2b_init(struct blake2b_state *S, size_t outlen)
{
	assert(outlen > 0 && outlen <= BLAKE2B_OUTBYTES);

	memset(S, 0, sizeof(*S));
	for (size_t i = 0; i < 8; i++)
		S->h[i] = blake2b_IV[i];

	/* Parameter block: digest_length, key_length 0, fanout 1, depth 1,
	 * everything else zero. */
	S->h[0] ^= 0x01010000ULL ^ (uint64_t)outlen;
	S->outlen = outlen;
}

void blake2b_update(struct blake2b_state *S, const void *pin, size_t inlen)
{
	const uint8_t *in = pin;

	if (inlen == 0)
		return;

	if (inlen > BLAKE2B_BLOCKBYTES - S->buflen) {
		size_t fill = BLAKE2B_BLOCKBYTES - S->buflen;

		memcpy(S->buf + S->buflen, in, fill);
		S->buflen = 0;
		blake2b_increment_counter(S, BLAKE2B_BLOCKBYTES);
		blake2b_compress(S, S->buf);
		in += fill;
		inlen -= fill;

		while (inlen > BLAKE2B_BLOCKBYTES) {
			blake2b_increment_counter(S, BLAKE2B_BLOCKBYTES);
			blake2b_compress(S, in);
			in += BLAKE2B_BLOCKBYTES;
			inlen -= BLAKE2B_BLOCKBYTES;
		}
	}
	memcpy(S->buf + S->buflen, in, inlen);
	S->buflen += inlen;
}

void blake2b_done(struct blake2b_state *S, void *out)
{
	uint8_t buffer[BLAKE2B_OUTBYTES] = {0};

	blake2b_increment_counter(S, S->buflen);
	/* Last block flag. */
	S->f[0] = (uint64_t)-1;
	memset(S->buf + S->buflen, 0, BLAKE2B_BLOCKBYTES - S->buflen);
	blake2b_compress(S, S->buf);

	for (size_t i = 0; i < 8; i++)
		store_le64(buffer + i * 8, S->h[i]);

	memcpy(out, buffer, S->outlen);
}

void blake2b(void *out, size_t outlen, const void *in, size_t inlen)
{
	struct blake2b_state S;

	blake2b_init(&S, outlen);
	blake2b_update(&S, in, inlen);
	blake2b_done(&S, out);
}
