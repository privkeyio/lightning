#include "config.h"
#include <assert.h>
#include <ccan/array_size/array_size.h>
#include <ccan/str/str.h>
#include <ccan/tal/grab_file/grab_file.h>
#include <ccan/tal/str/str.h>
#include <ccan/tal/tal.h>
#include <common/setup.h>
#include <common/trace.h>
#include <common/utils.h>
#include <stdlib.h>
#include <unistd.h>

static const char *emitted_field(const char *trace, const char *name,
				 const char *field)
{
	const char *namepat = tal_fmt(tmpctx, "\"name\":\"%s\"", name);
	const char *fieldpat = tal_fmt(tmpctx, "\"%s\":\"", field);
	const char *line = NULL, *p = trace;

	while ((p = strstr(p, "span_emit ")) != NULL) {
		const char *eol = strchr(p, '\n');
		const char *n = strstr(p, namepat);
		if (n && n < eol) {
			assert(!line);
			line = p;
		}
		p = eol;
	}
	assert(line);

	const char *eol = strchr(line, '\n');
	const char *f = strstr(line, fieldpat);
	if (!f || f > eol)
		return NULL;
	f += strlen(fieldpat);
	return tal_strndup(tmpctx, f, strchr(f, '"') - f);
}

/* A suspended child whose parent ends first, then resumes and ends
 * after other spans have taken over the parent's slot and its key. */
static void run_scenario(const char *na, const char *nb, const char *nc,
			 const char *nd, const char *ne, const char *fname)
{
	int a, c, d, e;
	char *b = tal(NULL, char);
	char *trace;

	trace_span_start_(na, &a);
	trace_span_start_(nb, b);
	trace_span_suspend(b);
	trace_span_end(&a);

	trace_span_start_(nc, &c);
	trace_span_suspend(&c);
	trace_span_start("reuse", &a);
	trace_span_suspend(&a);

	trace_span_resume(b);
	trace_span_suspend(b);
	trace_span_start_(nd, &d);
	trace_span_end(&d);

	trace_span_resume(b);
	trace_span_end(b);
	trace_span_start_(ne, &e);
	trace_span_end(&e);

	trace_span_resume(&a);
	trace_span_end(&a);
	trace_span_resume(&c);
	trace_span_end(&c);
	tal_free(b);

	trace = grab_file_str(tmpctx, fname);
	assert(trace);
	assert(streq(emitted_field(trace, nb, "parentId"),
		     emitted_field(trace, na, "id")));
	assert(!emitted_field(trace, nc, "parentId"));
	assert(!emitted_field(trace, nd, "parentId"));
	assert(!emitted_field(trace, ne, "parentId"));
}

/* Freeing a suspended parent's object ends it while its child is
 * current, and the child then ends on its own. */
static void run_destroy(const char *np, const char *nq, const char *nr,
			const char *ns, const char *fname)
{
	char *p = tal(NULL, char);
	int q, r, s;
	char *trace;

	trace_span_start_(np, p);
	trace_span_start_(nq, &q);
	trace_span_suspend(&q);
	trace_span_suspend_may_free(p);
	trace_span_resume(&q);
	tal_free(p);

	trace_span_start_(nr, &r);
	trace_span_end(&r);
	trace_span_end(&q);
	trace_span_start_(ns, &s);
	trace_span_end(&s);

	trace = grab_file_str(tmpctx, fname);
	assert(trace);
	assert(streq(emitted_field(trace, nq, "parentId"),
		     emitted_field(trace, np, "id")));
	assert(streq(emitted_field(trace, nr, "parentId"),
		     emitted_field(trace, nq, "id")));
	assert(!emitted_field(trace, ns, "parentId"));
}

int main(int argc, char *argv[])
{
	char *tmpl, *fname;
	int fillers[8];
	int fd;

	common_setup(argv[0]);

	fd = tmpdir_mkstemp(tmpctx, "run-trace-parent.XXXXXX", &tmpl);
	assert(fd >= 0);
	close(fd);
	setenv("CLN_DEV_TRACE_FILE", tmpl, 1);
	fname = tal_fmt(tmpctx, "%s.%u", tmpl, (unsigned)getpid());

	/* Parent allocated on the heap once the fixed slots are taken. */
	for (size_t i = 0; i < ARRAY_SIZE(fillers); i++) {
		trace_span_start("filler", &fillers[i]);
		trace_span_suspend(&fillers[i]);
	}
	run_scenario("heapA", "heapB", "heapC", "heapD", "heapE", fname);
	run_destroy("heapP", "heapQ", "heapR", "heapS", fname);
	for (size_t i = 0; i < ARRAY_SIZE(fillers); i++) {
		trace_span_resume(&fillers[i]);
		trace_span_end(&fillers[i]);
	}

	/* Parent in a fixed slot, which is reused after it ends. */
	run_scenario("fixedA", "fixedB", "fixedC", "fixedD", "fixedE", fname);
	run_destroy("fixedP", "fixedQ", "fixedR", "fixedS", fname);

	trace_cleanup();
	unlink(tmpl);
	unlink(fname);
	common_shutdown();
}
