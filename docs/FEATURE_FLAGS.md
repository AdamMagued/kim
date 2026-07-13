# Dormant feature flags

## Voice

`VOICE_ENABLED` in `mcp_server/config.py` gates the retained voice configuration
and IPC plumbing and defaults to `False`. The agent TTS runtime was intentionally
removed, so enabling this flag exposes configuration only; it does not make the
agent speak. Restoring voice requires a deliberate runtime implementation in
addition to enabling the flag.
