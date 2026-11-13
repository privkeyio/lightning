#include "config.h"
#include <assert.h>
#include <ccan/str/str.h>
#include <ccan/tal/grab_file/grab_file.h>
#include <ccan/tal/str/str.h>
#include <ccan/tal/tal.h>
#include <common/setup.h>
#include <common/trace.h>
#include <common/utils.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* The span_emit line for @name, without its trailing newline. */
static const char *emitted_line(const char *trace, const char *name)
{
	const char *namepat = tal_fmt(tmpctx, "\"name\":\"%s\"", name);
	const char *p = trace;

	while ((p = strstr(p, "span_emit ")) != NULL) {
		const char *eol = strchr(p, '\n');
		const char *n = strstr(p, namepat);
		assert(eol);
		if (n && n < eol)
			return tal_strndup(tmpctx, p, eol - p);
		p = eol;
	}
	abort();
}

static void emit_with_tag(const char *name, const char *value)
{
	int key;

	trace_span_start_(name, &key);
	trace_span_tag(&key, "query", value);
	trace_span_end(&key);
}

int main(int argc, char *argv[])
{
	char *tmpl, *fname, *trace, *value;
	const char *line, *json;
	int fd;

	common_setup(argv[0]);

	fd = tmpdir_mkstemp(tmpctx, "run-trace-long-tag.XXXXXX", &tmpl);
	assert(fd >= 0);
	close(fd);
	setenv("CLN_DEV_TRACE_FILE", tmpl, 1);
	fname = tal_fmt(tmpctx, "%s.%u", tmpl, (unsigned)getpid());

	/* A value far longer than the line. */
	value = tal_arr(tmpctx, char, 3001);
	memset(value, 'x', 3000);
	value[3000] = '\0';
	emit_with_tag("long", value);

	/* One whose cut point lands on backslashes. */
	memset(value, '\\', 3000);
	emit_with_tag("backslash", value);

	/* A short one is untouched. */
	emit_with_tag("short", "SELECT 1");

	trace = grab_file_str(tmpctx, fname);
	assert(trace);

	line = emitted_line(trace, "long");
	json = strchr(line + strlen("span_emit "), ' ') + 1;
	assert(strlen(json) <= 2048);
	assert(strends(json, "\"}]"));
	assert(strstr(json, "\"traceId\":\""));
	assert(strstr(json, "\"query\":\"xxx"));

	line = emitted_line(trace, "backslash");
	json = strchr(line + strlen("span_emit "), ' ') + 1;
	assert(strlen(json) <= 2048);
	assert(strends(json, "\"}]"));
	assert(strstr(json, "\"query\":\"\"},\"traceId\""));

	line = emitted_line(trace, "short");
	assert(strstr(line, "\"query\":\"SELECT 1\"},\"traceId\""));

	trace_cleanup();
	unlink(tmpl);
	unlink(fname);
	common_shutdown();
}
