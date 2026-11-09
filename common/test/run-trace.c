#include "config.h"
#include <ccan/str/str.h>
#include <ccan/tal/tal.h>
#include <common/setup.h>
#include <common/trace.h>

/* This is mostly a benchmark to see how much overhead the tracing
 * introduces. */

int main(int argx, char *argv[])
{
	/* Just some context objects to hang spans off of. */
	int a = 0, b = 0, c = 0, d = 0;

	common_setup(argv[0]);

	/* Create a bunch of nested spans to emit. */
	for(int i=0; i<2500000; i++) {
		trace_span_start("a", &a);
		trace_span_tag(&a, "method", "getrawblockbyheight");
		trace_span_start("b", &b);
		trace_span_tag(&b, "method", "getrawblockbyheight");

		trace_span_start("c", &c);
		trace_span_tag(&c, "method", "getrawblockbyheight");
		trace_span_end(&c);

		trace_span_end(&b);

		trace_span_start("d", &d);
		trace_span_tag(&d, "method", "getrawblockbyheight");
		trace_span_end(&d);

		trace_span_end(&a);
	}

	/* Freeing a suspended span's object inside another span ends the
	 * suspended one and leaves the other current. */
	char *p = tal(NULL, char);
	trace_span_start("p", p);
	trace_span_suspend_may_free(p);
	trace_span_start("b", &b);
	tal_free(p);
	trace_span_end(&b);

	trace_cleanup();
	common_shutdown();
}
