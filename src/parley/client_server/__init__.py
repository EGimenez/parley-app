"""The client-server side — the human↔own-agent channel: HTTP server, CLI, config, popup.

Kept import-light on purpose (no eager server/CLI imports) so importing config
doesn't drag in the HTTP server.
"""
