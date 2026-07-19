//! FIX 1 (K-EFFORT via webview bridge): the on-page reasoning-effort picker
//! (bridge_effort.js) is concatenated after bridge.js into one eval'd script,
//! so bridge.js's send() can reach bridge_effort.js's `window.__kimApplyEffort`
//! regardless of bridge.js's own top-level IIFE closure. Split out of
//! browser_bridge.rs to keep that file within the Q6 file-size gate.

/// bridge.js + bridge_effort.js concatenated into one persistent init script.
pub(crate) const PERSISTENT_BRIDGE_JS: &str =
    concat!(include_str!("bridge.js"), include_str!("bridge_effort.js"));
