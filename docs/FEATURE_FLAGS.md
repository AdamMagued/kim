# Dormant feature flags

## Relay

The relay server is deployable, but its desktop settings surface is intentionally
hidden by default. `RELAY_ENABLED` in
`desktop/src/components/kim-ui/RevampSettings.tsx` controls that surface and must
default to `false`. For local relay UI work, change it to `true`, run the desktop
app locally, and restore it to `false` before committing.

## Voice

`VOICE_ENABLED` in `mcp_server/config.py` gates the retained voice configuration
and IPC plumbing and defaults to `False`. The agent TTS runtime was intentionally
removed, so enabling this flag exposes configuration only; it does not make the
agent speak. Restoring voice requires a deliberate runtime implementation in
addition to enabling the flag.
