"""GALAHAD bridge — exposes the galahad_comms tool to the voice assistant. See galahad/bridge.py."""
from galahad import bridge

PLUGIN, run = bridge.make("galahad_comms")
