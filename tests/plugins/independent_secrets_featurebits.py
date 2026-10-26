#!/usr/bin/env python3
"""Asks, through its manifest's featurebits, for one of
option_independent_secrets' bits (IS_FEATUREBIT, 266 or 267) in init and
node_announcement.  lightningd refuses it: only --independent-secrets turns
the option on.
"""
import os
from pyln.client import Plugin

bit = int(os.environ['IS_FEATUREBIT'])
plugin = Plugin(
    dynamic=False,
    init_features=1 << bit,
    node_features=1 << bit,
)


@plugin.init()
def init(configuration, options, plugin):
    return {}


plugin.run()
