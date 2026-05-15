# Security Notes: Gemini Authentication in Kim

## Token Security

### Storage Strategy

Kim uses a **tiered token storage approach** to maximize security:

1. **Refresh Token** (Long-lived, sensitive)
   - **Where:** Stored exclusively in OS secure storage
     - macOS: Keychain
     - Windows: Windows Credential Manager
     - Linux: Secret Service / libsecret (if available)
   - **Access:** Only Rust code can read it
   - **Python:** Never receives refresh token
   - **Why:** Refresh tokens grant indefinite access; they must be protected as highly as passwords

2. **Access Token** (Short-lived, less sensitive)
   - **Where:** Passed only via environment variables to Python subprocess
   - **Lifetime:** Typically 1 hour; may be shorter
   - **Python:** Receives via `KIM_GOOGLE_ACCESS_TOKEN`; used only for current request
   - **After Request:** Environment is cleaned; token is never persisted
   - **Why:** Short lifetime limits exposure window

3. **Project ID** (Non-secret metadata)
   - **Where:** Local configuration file, not secure storage
   - **Example:** `kim-gemini-abc123`
   - **Risk Level:** Low — project IDs are not secret
   - **Why:** Needed for `x-goog-user-project` header; no secrecy required

### No Token Logging

Kim explicitly avoids logging tokens:

```python
# ✓ SAFE: HTTP error message does not include bearer token
logger.error("Gemini OAuth API error: HTTP %s: %s", exc.code, _truncate(raw, 2000))

# ✗ UNSAFE (never done in Kim):
# logger.error("Request headers: %s", headers)  # Would include Authorization header
```

### No Token in Error Messages

User-facing errors never include token values:

```python
# ✓ SAFE: User sees clear error without secrets
raise RuntimeError("Google access token is expired. Please reconnect Google for Kim.")

# ✗ UNSAFE (never done in Kim):
# raise RuntimeError(f"Token refresh failed: {token}")
```

## OAuth Configuration

### PKCE (Proof Key for Code Exchange)

Kim uses PKCE for desktop OAuth flow:

- **Why:** PKCE is the OAuth 2.0 standard for public clients (desktop apps) and doesn't require a client secret
- **How:** Rust generates a random code verifier, hashes it, and includes both in the OAuth flow
- **Benefit:** Client secret never leaves Kim; even if intercepted, the auth code is useless

### Client ID vs Client Secret

Kim's deployment:

1. **Client ID** (`KIM_GOOGLE_OAUTH_CLIENT_ID`)
   - Shipped with Kim binary
   - Not secret; safe to expose
   - Identifies Kim as the OAuth application

2. **Client Secret** (`KIM_GOOGLE_OAUTH_CLIENT_SECRET`)
   - Optional (not required with PKCE)
   - If provided at build time, used only in Rust (never passed to Python)
   - Example: For server-to-server flows or internal CI/CD
   - **Never committed to source control**

### Scope Management

Kim requests only the minimum scopes needed:

1. **Base Identity Scopes** (always)
   - `openid` — User identity
   - `email` — User email address
   - `profile` — User profile information

2. **Gemini API Scope** (for API access)
   - `https://www.googleapis.com/auth/generative-language.retriever`
   - Grants access only to Gemini models, not other Google APIs

3. **Cloud Platform Scope** (for project creation only, if needed)
   - `https://www.googleapis.com/auth/cloud-platform`
   - Requested only when user chooses "Use Google free tier"
   - Allows Kim to create a project and enable Gemini API

**Principle:** Scopes are incremental and explicit. Kim never requests broad access.

## Project Isolation

### User-Owned Project Mode

When Kim creates a Google Cloud project in your account:

1. **Project Creation**
   - Kim uses your OAuth token and Google Cloud APIs
   - Kim generates a project ID: `kim-gemini-<random-suffix>`
   - Google Cloud creates the project in your Google account
   - Kim stores only the project ID (not secret)

2. **API Enablement**
   - Kim enables Generative Language API on your project
   - No data is transferred to Kim's account

3. **Billing**
   - Usage is charged to **your project**, not Kim's
   - Your free-tier quota (if applicable) applies
   - Kim never charges your account directly

4. **Project Access**
   - Kim can only access Gemini API (scoped to that specific API)
   - Kim cannot access other services (Cloud Storage, Cloud SQL, etc.)
   - You can revoke access anytime via Google Cloud Console

### API Key Mode

If you use an API key:

1. **API Key Ownership**
   - You own and manage the key
   - The key is tied to your Google Cloud project

2. **Scope of Access**
   - The key grants access to Gemini API
   - No access to other resources

3. **Revocation**
   - You can revoke the key anytime in Google Cloud Console

## What Kim Does NOT Do

### ✗ Does Not Implement Leaked/Internal APIs

- No Antigravity access
- No Gemini CLI endpoints
- No internal Google client spoofing
- No reverse-engineered Google code

### ✗ Does Not Access User Credentials Beyond Scope

- Does **not** fetch user's Gemini API keys
- Does **not** read user's existing Google Cloud projects without permission
- Does **not** access user's Gmail, Drive, or other Google services

### ✗ Does Not Bypass Billing

- Does **not** use Kim's shared project as fallback quota
- Does **not** hide costs by using company credentials
- If your free tier is exceeded, usage is blocked (no silent upgrade)

### ✗ Does Not Store Secrets in Plain Text

- No API keys in config files
- No refresh tokens in unencrypted storage
- No secrets in logs or error messages

## Compliance

### Required Consents

Kim shows consent screens for:

1. **Google Sign-In**
   - Standard Google OAuth consent
   - Shows scopes: identity, email, profile, Gemini API access

2. **Google Cloud Project Creation** (free-tier mode only)
   - Additional scope: Cloud Platform
   - Clearly explains: "Kim needs permission to create a Google Cloud project and enable the Gemini API for you."

### Transparency

- Kim discloses all scopes upfront
- Users can review project access in Google Cloud Console
- Users can revoke access anytime

## Incident Response

### Token Compromise

If your refresh token is compromised (e.g., keychain is stolen):

1. **Immediate:** Disconnect Google for Kim in Settings
2. **Google Account:** Visit [myaccount.google.com/security](https://myaccount.google.com/security)
3. **Confirm:** Check "Security event" notifications for unauthorized Kim access
4. **Reset:** Sign back in to Kim to generate a new refresh token

### Project Abuse

If your free-tier project is used suspiciously:

1. **Google Cloud Console:** Visit [console.cloud.google.com](https://console.cloud.google.com)
2. **Audit:** Check "Cloud Audit Logs" for unexpected API calls
3. **Revoke:** Go to Settings, disconnect Gemini, or delete the project
4. **Report:** Contact Google Cloud support if needed

## Development & Testing

### Testing Without Real Credentials

Kim includes test mocks in `tests/test_gemini_oauth_provider.py`:

```python
def test_requires_exactly_one_auth_path():
    """Ensure user cannot accidentally enable multiple auth modes."""
    config_both = {"api_key": "key", "oauth_access_token": "token"}
    with pytest.raises(EnvironmentError, match="ambiguous"):
        GeminiProvider(config_both)
```

### Local Development

For development, use the API key mode:

```bash
export GOOGLE_API_KEY=<your-key>
```

Never commit API keys. Use `.env` or `.envrc` locally.

### CI/CD

For GitHub Actions or other CI systems:

1. Store secrets as GitHub Secrets (never in code)
2. Use `export KIM_GOOGLE_OAUTH_CLIENT_SECRET=...` at build time
3. PKCE flow ensures desktop OAuth still works in CI

## Audit Checklist

- [ ] No tokens in logs
- [ ] No secrets in source code
- [ ] Refresh token stored in OS secure storage only
- [ ] Access token passed via environment only
- [ ] Project ID stored as plaintext (not secret)
- [ ] PKCE enabled for desktop OAuth
- [ ] Scopes are minimal
- [ ] User-owned project mode does not fall back to Kim's quota
- [ ] Error messages do not expose token values
- [ ] Token expiry is checked before use

## References

- [OAuth 2.0 Security Best Practices](https://tools.ietf.org/html/draft-ietf-oauth-security-topics)
- [PKCE RFC 7636](https://tools.ietf.org/html/rfc7636)
- [Google OAuth 2.0 Documentation](https://developers.google.com/identity/protocols/oauth2)
- [Keyring Token Storage (macOS Keychain)](https://developer.apple.com/documentation/foundation/keychain)
