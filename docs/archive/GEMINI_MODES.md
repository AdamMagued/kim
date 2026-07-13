> **Archived** — historical document retained for provenance; not maintained. For current plans and repo structure see ROADMAP.md and the living docs at the repo root.

# Gemini Provider Modes

Kim supports three authentication modes for the Gemini API:

## 1. Google Free-Tier Project (Recommended)

**Mode:** `oauth_user_project`

Kim creates and owns a Google Cloud project in your Google account. All Gemini API usage counts against your free-tier quota, not Kim's shared project.

### Setup
1. Open Kim Settings → Account → Gemini → "Use Google free tier"
2. Click "Continue with Google"
3. Approve Kim's request to create a Google Cloud project in your account
4. Kim automatically creates a project named `kim-gemini-<random>` and enables the Gemini API
5. Start using Gemini without further configuration

### How It Works
- **Authentication:** OAuth 2.0 with PKCE (no client secret required)
- **Token Storage:** Refresh token securely stored in your OS keychain; access tokens are short-lived
- **Quota Project:** Kim stores your generated project ID locally and includes it in every Gemini API request
- **Billing:** Your free-tier quota applies; no charges to Kim's account

### Requirements
- An active Google Account
- Internet access to authenticate with Google
- Permission to create Google Cloud projects (available to most Google accounts)

## 2. API Key (Development)

**Mode:** `api_key`

You provide a Gemini API key directly. Useful for development or if you prefer managing your own project quota explicitly.

### Setup
1. Get an API key from [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Set environment variable: `export GOOGLE_API_KEY=<your-key>`
3. (Or configure Kim settings if available)

### How It Works
- **Authentication:** Bearer token with API key
- **Quota Project:** Uses whatever Google Cloud project is associated with your API key
- **Billing:** Charged to the project that owns the API key

### Notes
- API keys are less secure than OAuth and should only be used in development
- Never commit API keys to version control
- API keys grant access to all resources in the associated project

## 3. Advanced: Existing Google Cloud Project

**Mode:** `oauth_user_project` with manual project setup

If you already have a Google Cloud project and want Kim to use it, you can manually configure the project ID after signing in with Google.

### Setup
1. Create or select a Google Cloud project at [Google Cloud Console](https://console.cloud.google.com)
2. Enable the Generative Language API on that project
3. Set up billing (required for most models)
4. Get the project ID
5. Configure Kim: `export KIM_GOOGLE_USER_PROJECT_ID=<your-project-id>`
6. Sign in with Google in Kim Settings

### How It Works
- Same as free-tier mode, but uses your existing project
- Kim includes your project ID in the `x-goog-user-project` header for every Gemini request
- Usage counts against your project's quota

## Comparison Table

| Feature | Free-Tier | API Key | Advanced Project |
|---------|-----------|---------|------------------|
| Setup Effort | Very Easy | Easy | Medium |
| Secure Token Storage | ✓ Keychain | ✗ Plaintext | ✓ Keychain |
| Free Tier Eligible | ✓ Yes | Depends | Depends |
| Requires Credentials Entry | ✗ No | ✓ Paste key | ✗ No |
| Support Multiple Projects | ✗ One per account | ✗ One per key | ✓ Yes |
| Quota Isolation | ✓ Isolated | ✓ Isolated | ✓ Isolated |

## Troubleshooting

### "Google access token is expired"
Reconnect your Google account in Settings → Account → Gemini.

### "oauth_user_project mode requires a valid Google Cloud project ID"
Ensure the Gemini API is enabled on your project and Kim has permissions.

### "Your Google Gemini free-tier quota has been exceeded"
Wait for your quota to reset (typically monthly), upgrade to a paid plan, or use an API key.

### "Your Google Cloud project is not properly configured"
- Verify the Gemini API is enabled on your project
- Ensure billing is set up (required for some models)
- Check project permissions

## Migration

To switch modes:
1. Go to Kim Settings → Account → Gemini
2. Disconnect current authentication
3. Select a new mode and follow its setup instructions

**Note:** Each mode is independent. Switching modes does not affect your other projects or API keys.

## Security Best Practices

1. **Never share your API key** (if using API key mode)
2. **Refresh tokens are stored securely** in your OS keychain; don't manually delete them
3. **Access tokens are short-lived** and never logged by Kim
4. **OAuth scopes are minimal** — Kim only requests access to Gemini and Cloud project management when necessary
5. **Your project is isolated** — switching off Kim doesn't affect other Google Cloud services

## For More Information

- [Google Gemini API Documentation](https://ai.google.dev/)
- [Google Cloud OAuth Documentation](https://cloud.google.com/docs/authentication/oauth2)
- [Keyring Token Storage Security](https://en.wikipedia.org/wiki/Keyring_(software))
