#!/usr/bin/env python3
"""Plugin which holds the next call of a command, once `holdcommand` names it.

The held call stays unanswered until `releasecommand`, which lets a test
act while another plugin waits for its reply.
"""
from pyln.client import Plugin


plugin = Plugin()
plugin.hold = None
plugin.held = None


@plugin.async_hook('rpc_command')
def on_rpc_command(rpc_command, request, plugin, **kwargs):
    if plugin.hold == rpc_command['method']:
        plugin.hold = None
        plugin.held = request
        plugin.log("holding {}".format(rpc_command['method']))
        return
    request.set_result({'result': 'continue'})


@plugin.method('holdcommand')
def holdcommand(plugin, method):
    """Hold the next call of {method}."""
    plugin.hold = method
    return {}


@plugin.method('releasecommand')
def releasecommand(plugin):
    """Let the held call through."""
    request = plugin.held
    plugin.held = None
    request.set_result({'result': 'continue'})
    return {}


plugin.run()
