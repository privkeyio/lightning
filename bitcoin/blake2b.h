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
#ifndef LIGHTNING_BITCOIN_BLAKE2B_H
#define LIGHTNING_BITCOIN_BLAKE2B_H
#include "config.h"
#include <stddef.h>
#include <stdint.h>

#define BLAKE2B_BLOCKBYTES 128
#define BLAKE2B_OUTBYTES 64

struct blake2b_state {
	uint64_t h[8];
	uint64_t t[2];
	uint64_t f[2];
	uint8_t buf[BLAKE2B_BLOCKBYTES];
	size_t buflen;
	size_t outlen;
};

/* Unkeyed BLAKE2b.  outlen must be 1..BLAKE2B_OUTBYTES. */
void blake2b_init(struct blake2b_state *S, size_t outlen);
void blake2b_update(struct blake2b_state *S, const void *in, size_t inlen);
void blake2b_done(struct blake2b_state *S, void *out);

/* One-shot form of the above. */
void blake2b(void *out, size_t outlen, const void *in, size_t inlen);

#endif /* LIGHTNING_BITCOIN_BLAKE2B_H */
