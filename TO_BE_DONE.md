# TO BE DONE

## Discord Webhook Security (before open-sourcing)

### Current state
The Discord feedback webhook URL is embedded at compile time via the `KIM_DISCORD_WEBHOOK` env var (`option_env!` in `desktop/src-tauri/src/lib.rs:9598`). This means:
- URL is **not in source** (safe for GitHub)
- URL **is in the compiled binary** (anyone with the `.app` can extract it with `strings kim | grep discord`)
- People could spam your Discord channel if they get the URL from the binary

### How to test right now
```bash
export KIM_DISCORD_WEBHOOK="https://discord.com/api/webhooks/YOUR_URL_HERE"
cd desktop && cargo tauri build   # or cargo tauri dev
```

### What to do before going public

**Option A — Cloudflare Worker proxy (recommended)**
1. Create a free Cloudflare account
2. Deploy a Worker that:
   - Receives POST requests with a shared secret header
   - Validates the secret, then forwards to the real Discord webhook
   - Rate-limits by IP (Cloudflare does this for free)
3. Embed the **Worker URL + secret** in the binary instead of the raw Discord URL
4. Store the real Discord webhook URL only in the Cloudflare dashboard
5. If the Worker URL leaks, you can rotate the secret or delete the Worker without touching Discord

**Option B — Backend relay**
- Route feedback through your own server (Railway, Fly, etc.)
- Server holds the Discord URL, clients just POST to your API
- More control, more infra to maintain

**Option C — Regenerate + rate limit Discord**
- Regenerate the webhook URL regularly (e.g. monthly via a script)
- Discord itself doesn't have built-in rate limiting on webhooks — spamming is possible
- Least effort but most risk

### Recommended next step
Go with **Option A**. Cloudflare Workers are free up to 100k requests/day, take ~15 min to set up, and you never need to touch the binary signing or build pipeline again.
