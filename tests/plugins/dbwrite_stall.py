#!/usr/bin/env python3
"""Once armed with `dbwrite-stall-arm`, holds lightningd for `delay` seconds
on the first database transaction whose writes contain `pattern`.

lightningd waits for the db_write hook before it sends what it has queued
for its subdaemons in that transaction, so this stands in for a slow disk
or a backup plugin.
"""
from pyln.client import Plugin
import time

plugin = Plugin()
plugin.pattern = None
plugin.delay = 0


@plugin.method('dbwrite-stall-arm')
def arm(plugin, pattern, delay):
    """Stall once, for delay seconds, on a transaction with pattern."""
    plugin.pattern = pattern
    plugin.delay = float(delay)
    return {}


@plugin.hook('db_write')
def db_write(plugin, writes, **kwargs):
    if plugin.pattern and any(plugin.pattern in w for w in writes):
        pattern, plugin.pattern = plugin.pattern, None
        plugin.log("dbwrite-stall: stalling {}s on {}".format(plugin.delay,
                                                              pattern))
        time.sleep(plugin.delay)
        plugin.log("dbwrite-stall: done")
    return {"result": "continue"}


plugin.run()
