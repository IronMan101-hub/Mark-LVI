"""GALAHAD — the command-centre layer built on top of MARK LVI / JARVIS.

Headless by design: nothing in this package imports Qt. The voice session
(`main.py`), the Qt window (`ui.py`) and the web dashboard all talk to the same
`Engine`, so there is exactly one set of plugins, one database and one
permission model no matter how the user reaches them.
"""
VERSION = "1.0.0"
APP_NAME = "GALAHAD"
