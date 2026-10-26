#include "config.h"
#include <assert.h>
#include <bitcoin/feerate.h>

u32 feerate_from_style(u32 feerate, enum feerate_style style)
{
	/* Make sure it's called somewhere! */
	assert(feerate_floor_check() == FEERATE_FLOOR);

	switch (style) {
	case FEERATE_PER_KSIPA:
		return feerate;
	case FEERATE_PER_KBYTE:
		/* Everyone uses satoshi per kbyte, but we use satoshi per ksipa
		 * (don't round down to zero though)!
		 *
		 * Widen before rounding up: on a u32 the +3 wraps for the top
		 * three values, turning an absurd feerate into 0 or 1 perkw.
		 * That is the dangerous direction (it underpays our unilateral
		 * close), and it slips under every bound we check afterwards,
		 * since those are applied to the converted value.  The result
		 * always fits a u32: (UINT_MAX + 3) / 4 < UINT_MAX. */
		return ((u64)feerate + 3) / 4;
	}
	abort();
}

u32 feerate_relay_floor(u32 floor_perkw)
{
	u64 rate;

	if (floor_perkw <= BITCOIND_MINRELAYTXFEE_PER_KW)
		return FEERATE_FLOOR;

	rate = ((u64)floor_perkw * (MINIMUM_TX_WEIGHT + 3)
		+ MINIMUM_TX_WEIGHT - 1) / MINIMUM_TX_WEIGHT
		+ (2000 + MINIMUM_TX_WEIGHT - 1) / MINIMUM_TX_WEIGHT;
	if (rate > UINT_MAX)
		return UINT_MAX;
	return rate;
}

u32 feerate_to_style(u32 feerate_perkw, enum feerate_style style)
{
	switch (style) {
	case FEERATE_PER_KSIPA:
		return feerate_perkw;
	case FEERATE_PER_KBYTE:
		if ((u64)feerate_perkw * 4 > UINT_MAX)
			return UINT_MAX;
		return feerate_perkw * 4;
	}
	abort();
}

bool next_funding_feerate(u32 last_feerate, u32 *next_feerate)
{
	u64 next;

	/* Not a feerate we could ever have proposed, and 25/24 of it is
	 * still 0. */
	if (last_feerate == 0)
		return false;

	/* Widen: anything above UINT_MAX/25 overflows a u32 here, and that
	 * is exactly the range a broken fee estimator can leave in the db. */
	next = (u64)last_feerate * 25 / 24;
	if (next > UINT_MAX)
		return false;

	/* Rounding down maps feerates below 24 back onto themselves, which
	 * the rule allows but which can't replace anything: take one more,
	 * or a funding tx made at one of these could never be replaced. */
	if (next <= last_feerate)
		next = last_feerate + 1;

	*next_feerate = next;
	return true;
}

const char *feerate_style_name(enum feerate_style style)
{
	switch (style) {
	case FEERATE_PER_KBYTE:
		return "perkb";
	case FEERATE_PER_KSIPA:
		return "perkw";
	}
	abort();
}
