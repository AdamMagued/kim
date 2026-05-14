//! Google OAuth for Kim's official Gemini API provider.
//!
//! Contract with Python:
//! - Rust owns the refresh token and stores it in OS secure storage.
//! - Before spawning the Python agent with provider=gemini, Rust refreshes and
//!   injects only short-lived env vars:
//!     KIM_GEMINI_AUTH_MODE=oauth
//!     KIM_GOOGLE_ACCESS_TOKEN=<redacted in logs>
//!     KIM_GOOGLE_ACCESS_TOKEN_EXPIRES_AT=<epoch seconds>
//!     KIM_GOOGLE_CLOUD_PROJECT=<optional quota project>
//! - Python never receives refresh tokens or client secrets.

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use keyring::Entry;
use rand::{distributions::Alphanumeric, Rng};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::time::{SystemTime, UNIX_EPOCH};
use url::Url;

const KEYRING_SERVICE: &str = "kim.google.oauth";
const KEYRING_ACCOUNT: &str = "default";
const GOOGLE_AUTH_URL: &str = "https://accounts.google.com/o/oauth2/v2/auth";
const GOOGLE_TOKEN_URL: &str = "https://oauth2.googleapis.com/token";
const GOOGLE_USERINFO_URL: &str = "https://openidconnect.googleapis.com/v1/userinfo";
const GEMINI_MODELS_URL: &str = "https://generativelanguage.googleapis.com/v1beta/models";
const GEMINI_SCOPE: &str = "https://www.googleapis.com/auth/generative-language.retriever";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GoogleOAuthStatus {
    pub connected: bool,
    #[serde(default)]
    pub email: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project_id: Option<String>,
    #[serde(default)]
    pub needs_reauth: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct GoogleOAuthSecret {
    refresh_token: String,
    email: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    project_id: Option<String>,
    #[serde(default)]
    scopes: Vec<String>,
    connected_at: u64,
}

#[derive(Debug, Deserialize)]
struct TokenResponse {
    access_token: String,
    #[serde(default)]
    refresh_token: Option<String>,
    #[serde(default)]
    expires_in: Option<u64>,
    #[serde(default)]
    scope: Option<String>,
}

#[derive(Debug, Deserialize)]
struct UserInfoResponse {
    #[serde(default)]
    email: String,
}

#[derive(Debug, Clone)]
pub struct AgentGoogleOAuthEnv {
    pub access_token: String,
    pub expires_at: u64,
    pub project_id: Option<String>,
}

impl AgentGoogleOAuthEnv {
    pub fn as_env_pairs(&self) -> Vec<(String, String)> {
        // Determine auth mode based on whether user project is configured
        let auth_mode = if self.project_id.is_some() {
            "oauth_user_project"
        } else {
            "oauth"
        };
        
        let mut env = vec![
            ("KIM_GEMINI_AUTH_MODE".to_string(), auth_mode.to_string()),
            ("KIM_GOOGLE_ACCESS_TOKEN".to_string(), self.access_token.clone()),
            ("KIM_GOOGLE_ACCESS_TOKEN_EXPIRES_AT".to_string(), self.expires_at.to_string()),
        ];
        if let Some(project_id) = &self.project_id {
            env.push(("KIM_GOOGLE_USER_PROJECT_ID".to_string(), project_id.clone()));
        }
        env
    }
}

fn now_epoch() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn oauth_client_id() -> Result<String, String> {
    option_env!("KIM_GOOGLE_OAUTH_CLIENT_ID")
        .map(str::to_string)
        .or_else(|| std::env::var("KIM_GOOGLE_OAUTH_CLIENT_ID").ok())
        .filter(|s| !s.trim().is_empty())
        .ok_or_else(|| "KIM_GOOGLE_OAUTH_CLIENT_ID is not configured for this Kim build.".to_string())
}

fn oauth_client_secret() -> Option<String> {
    option_env!("KIM_GOOGLE_OAUTH_CLIENT_SECRET")
        .map(str::to_string)
        .or_else(|| std::env::var("KIM_GOOGLE_OAUTH_CLIENT_SECRET").ok())
        .filter(|s| !s.trim().is_empty())
}

fn quota_project() -> Option<String> {
    option_env!("KIM_GOOGLE_CLOUD_PROJECT")
        .map(str::to_string)
        .or_else(|| std::env::var("KIM_GOOGLE_CLOUD_PROJECT").ok())
        .or_else(|| std::env::var("GOOGLE_CLOUD_PROJECT").ok())
        .filter(|s| !s.trim().is_empty())
}

fn oauth_scopes() -> Vec<String> {
    std::env::var("KIM_GOOGLE_OAUTH_SCOPES")
        .ok()
        .map(|raw| raw.split_whitespace().map(str::to_string).collect())
        .unwrap_or_else(|| vec![
            "openid".to_string(),
            "email".to_string(),
            "profile".to_string(),
            GEMINI_SCOPE.to_string(),
        ])
}

fn keyring_entry() -> Result<Entry, String> {
    Entry::new(KEYRING_SERVICE, KEYRING_ACCOUNT).map_err(|e| format!("Could not open OS secure storage: {e}"))
}

fn read_secret() -> Result<Option<GoogleOAuthSecret>, String> {
    let entry = keyring_entry()?;
    match entry.get_password() {
        Ok(raw) => serde_json::from_str(&raw)
            .map(Some)
            .map_err(|e| format!("Stored Google token metadata is invalid: {e}")),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(format!("Could not read Google token from OS secure storage: {e}")),
    }
}

fn write_secret(secret: &GoogleOAuthSecret) -> Result<(), String> {
    let entry = keyring_entry()?;
    let raw = serde_json::to_string(secret).map_err(|e| e.to_string())?;
    entry.set_password(&raw).map_err(|e| format!("Could not save Google token to OS secure storage: {e}"))
}

fn delete_secret() -> Result<(), String> {
    let entry = keyring_entry()?;
    match entry.delete_password() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(e) => Err(format!("Could not remove Google token from OS secure storage: {e}")),
    }
}

fn random_string(len: usize) -> String {
    rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(len)
        .map(char::from)
        .collect()
}

fn code_challenge(verifier: &str) -> String {
    let digest = Sha256::digest(verifier.as_bytes());
    URL_SAFE_NO_PAD.encode(digest)
}

fn system_open_url(url: &str) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    let mut cmd = std::process::Command::new("open");
    #[cfg(target_os = "windows")]
    let mut cmd = {
        let mut c = std::process::Command::new("cmd");
        c.arg("/C").arg("start").arg("");
        c
    };
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    let mut cmd = std::process::Command::new("xdg-open");

    cmd.arg(url)
        .status()
        .map_err(|e| format!("Could not open Google sign-in in your browser: {e}"))?
        .success()
        .then_some(())
        .ok_or_else(|| "Could not open Google sign-in in your browser.".to_string())
}

fn wait_for_loopback_callback(listener: TcpListener, expected_state: String) -> Result<String, String> {
    let (mut stream, _) = listener.accept().map_err(|e| format!("OAuth callback failed: {e}"))?;
    let mut buf = [0u8; 8192];
    let n = stream.read(&mut buf).map_err(|e| format!("OAuth callback read failed: {e}"))?;
    let request = String::from_utf8_lossy(&buf[..n]);
    let first_line = request.lines().next().unwrap_or_default();
    let path = first_line
        .strip_prefix("GET ")
        .and_then(|s| s.split_whitespace().next())
        .ok_or_else(|| "Invalid OAuth callback.".to_string())?;
    let callback_url = Url::parse(&format!("http://127.0.0.1{path}"))
        .map_err(|e| format!("Invalid OAuth callback URL: {e}"))?;
    let params: HashMap<String, String> = callback_url.query_pairs().into_owned().collect();
    let body = "<html><body><h3>Google connected. You can close this tab and return to Kim.</h3></body></html>";
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body,
    );
    let _ = stream.write_all(response.as_bytes());

    if let Some(err) = params.get("error") {
        return Err(format!("Google sign-in failed: {err}"));
    }
    if params.get("state") != Some(&expected_state) {
        return Err("Google sign-in state mismatch. Please try again.".to_string());
    }
    params
        .get("code")
        .cloned()
        .ok_or_else(|| "Google sign-in did not return an authorization code.".to_string())
}

async fn exchange_code_for_token(code: &str, redirect_uri: &str, verifier: &str) -> Result<TokenResponse, String> {
    let client_id = oauth_client_id()?;
    let mut form = vec![
        ("client_id", client_id),
        ("code", code.to_string()),
        ("code_verifier", verifier.to_string()),
        ("redirect_uri", redirect_uri.to_string()),
        ("grant_type", "authorization_code".to_string()),
    ];
    if let Some(secret) = oauth_client_secret() {
        form.push(("client_secret", secret));
    }
    Client::new()
        .post(GOOGLE_TOKEN_URL)
        .form(&form)
        .send()
        .await
        .map_err(|e| format!("Google token exchange failed: {e}"))?
        .error_for_status()
        .map_err(|e| format!("Google token exchange failed: {e}"))?
        .json::<TokenResponse>()
        .await
        .map_err(|e| format!("Could not parse Google token response: {e}"))
}

async fn refresh_access_token(secret: &GoogleOAuthSecret) -> Result<TokenResponse, String> {
    let client_id = oauth_client_id()?;
    let mut form = vec![
        ("client_id", client_id),
        ("refresh_token", secret.refresh_token.clone()),
        ("grant_type", "refresh_token".to_string()),
    ];
    if let Some(client_secret) = oauth_client_secret() {
        form.push(("client_secret", client_secret));
    }
    Client::new()
        .post(GOOGLE_TOKEN_URL)
        .form(&form)
        .send()
        .await
        .map_err(|e| format!("Google token refresh failed: {e}"))?
        .error_for_status()
        .map_err(|e| format!("Google token refresh failed; reconnect Google for Kim. {e}"))?
        .json::<TokenResponse>()
        .await
        .map_err(|e| format!("Could not parse Google refresh response: {e}"))
}

async fn fetch_userinfo(access_token: &str) -> Result<UserInfoResponse, String> {
    Client::new()
        .get(GOOGLE_USERINFO_URL)
        .bearer_auth(access_token)
        .send()
        .await
        .map_err(|e| format!("Could not read Google account email: {e}"))?
        .error_for_status()
        .map_err(|e| format!("Could not read Google account email: {e}"))?
        .json::<UserInfoResponse>()
        .await
        .map_err(|e| format!("Could not parse Google account email: {e}"))
}

#[derive(Debug, Deserialize)]
struct GcpProject {
    name: Option<String>,
    project_id: Option<String>,
    state: Option<String>,
}

#[derive(Debug, Serialize)]
struct CreateProjectRequest {
    project_id: String,
    name: String,
}

#[derive(Debug, Deserialize)]
struct CreateProjectResponse {
    name: String,
}

#[derive(Debug, Serialize)]
struct ServiceEnablementRequest {
    service_ids: Vec<String>,
}

async fn create_gcp_project(access_token: &str, project_id: &str) -> Result<(), String> {
    // https://cloud.google.com/resource-manager/reference/rest/v1/projects/create
    let body = CreateProjectRequest {
        project_id: project_id.to_string(),
        name: "Kim Gemini".to_string(),
    };
    let client = Client::new();
    let resp = client
        .post("https://cloudresourcemanager.googleapis.com/v1/projects")
        .bearer_auth(access_token)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("Failed to create GCP project: {e}"))?;
    
    if resp.status().is_success() {
        Ok(())
    } else {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_else(|_| "unknown error".to_string());
        Err(format!("Failed to create GCP project (HTTP {status}): {body}"))
    }
}

async fn enable_gemini_api(access_token: &str, project_id: &str) -> Result<(), String> {
    // https://cloud.google.com/service-usage/docs/reference/rest/v1/services/enable
    let url = format!(
        "https://serviceusage.googleapis.com/v1/projects/{}/services/generativelanguage.googleapis.com:enable",
        project_id
    );
    let client = Client::new();
    let resp = client
        .post(&url)
        .bearer_auth(access_token)
        .json(&serde_json::json!({}))
        .send()
        .await
        .map_err(|e| format!("Failed to enable Gemini API: {e}"))?;
    
    if resp.status().is_success() {
        Ok(())
    } else {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_else(|_| "unknown error".to_string());
        Err(format!("Failed to enable Gemini API (HTTP {status}): {body}"))
    }
}

pub async fn google_oauth_env_for_agent() -> Result<AgentGoogleOAuthEnv, String> {
    let secret = read_secret()?.ok_or_else(|| "Google for Kim is not connected.".to_string())?;
    let refreshed = refresh_access_token(&secret).await?;
    let expires_at = now_epoch() + refreshed.expires_in.unwrap_or(3600);
    Ok(AgentGoogleOAuthEnv {
        access_token: refreshed.access_token,
        expires_at,
        project_id: secret.project_id.clone(),
    })
}

#[tauri::command]
pub async fn google_oauth_status() -> Result<GoogleOAuthStatus, String> {
    match read_secret()? {
        Some(secret) => Ok(GoogleOAuthStatus {
            connected: true,
            email: secret.email,
            expires_at: None,
            project_id: secret.project_id,
            needs_reauth: false,
            error: None,
        }),
        None => Ok(GoogleOAuthStatus {
            connected: false,
            email: String::new(),
            expires_at: None,
            project_id: quota_project(),
            needs_reauth: false,
            error: None,
        }),
    }
}

#[tauri::command]
pub async fn google_oauth_start() -> Result<GoogleOAuthStatus, String> {
    let client_id = oauth_client_id()?;
    let verifier = random_string(96);
    let challenge = code_challenge(&verifier);
    let state = random_string(48);
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| format!("Could not start OAuth callback server: {e}"))?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    let redirect_uri = format!("http://127.0.0.1:{port}/callback");
    let scopes = oauth_scopes();

    let mut auth_url = Url::parse(GOOGLE_AUTH_URL).map_err(|e| e.to_string())?;
    auth_url.query_pairs_mut()
        .append_pair("client_id", &client_id)
        .append_pair("redirect_uri", &redirect_uri)
        .append_pair("response_type", "code")
        .append_pair("scope", &scopes.join(" "))
        .append_pair("state", &state)
        .append_pair("code_challenge", &challenge)
        .append_pair("code_challenge_method", "S256")
        .append_pair("access_type", "offline")
        .append_pair("prompt", "consent");

    system_open_url(auth_url.as_str())?;
    let code = tauri::async_runtime::spawn_blocking(move || wait_for_loopback_callback(listener, state))
        .await
        .map_err(|e| format!("OAuth callback task failed: {e}"))??;
    let token = exchange_code_for_token(&code, &redirect_uri, &verifier).await?;
    let refresh_token = token.refresh_token.clone().or_else(|| read_secret().ok().flatten().map(|s| s.refresh_token))
        .ok_or_else(|| "Google did not return a refresh token. Disconnect/reconnect and approve offline access.".to_string())?;
    let userinfo = fetch_userinfo(&token.access_token).await?;
    let email = userinfo.email;
    if email.trim().is_empty() {
        return Err("Google sign-in succeeded but no email was returned.".to_string());
    }

    let secret = GoogleOAuthSecret {
        refresh_token,
        email: email.clone(),
        project_id: quota_project(),
        scopes: token.scope.unwrap_or_else(|| scopes.join(" ")).split_whitespace().map(str::to_string).collect(),
        connected_at: now_epoch(),
    };
    write_secret(&secret)?;

    Ok(GoogleOAuthStatus {
        connected: true,
        email,
        expires_at: Some(now_epoch() + token.expires_in.unwrap_or(3600)),
        project_id: secret.project_id,
        needs_reauth: false,
        error: None,
    })
}

#[tauri::command]
pub async fn google_oauth_disconnect() -> Result<(), String> {
    delete_secret()
}

#[tauri::command]
pub async fn google_oauth_test() -> Result<GoogleOAuthStatus, String> {
    let secret = read_secret()?.ok_or_else(|| "Google for Kim is not connected.".to_string())?;
    let env = google_oauth_env_for_agent().await?;
    let mut req = Client::new().get(GEMINI_MODELS_URL).bearer_auth(&env.access_token);
    if let Some(project) = &env.project_id {
        req = req.header("x-goog-user-project", project);
    }
    req.send()
        .await
        .map_err(|e| format!("Could not reach Gemini API: {e}"))?
        .error_for_status()
        .map_err(|e| format!("Gemini API rejected Google OAuth. Reconnect Google for Kim or verify project/API access. {e}"))?;

    Ok(GoogleOAuthStatus {
        connected: true,
        email: secret.email,
        expires_at: Some(env.expires_at),
        project_id: env.project_id,
        needs_reauth: false,
        error: None,
    })
}

#[tauri::command]
pub async fn google_oauth_setup_free_tier_project() -> Result<GoogleOAuthStatus, String> {
    let mut secret = read_secret()?.ok_or_else(|| "Google for Kim is not connected.".to_string())?;
    
    // If already has a project, skip creation
    if secret.project_id.is_some() {
        return Ok(GoogleOAuthStatus {
            connected: true,
            email: secret.email.clone(),
            expires_at: None,
            project_id: secret.project_id.clone(),
            needs_reauth: false,
            error: None,
        });
    }
    
    // Generate a new project ID
    let random_suffix = random_string(6).to_lowercase();
    let project_id = format!("kim-gemini-{}", random_suffix);
    
    // Refresh token to get a fresh access token
    let token_resp = refresh_access_token(&secret).await?;
    let access_token = &token_resp.access_token;
    
    // Create the GCP project
    create_gcp_project(access_token, &project_id).await?;
    
    // Enable Gemini API on the project
    enable_gemini_api(access_token, &project_id).await?;
    
    // Update secret with the project ID
    secret.project_id = Some(project_id.clone());
    write_secret(&secret)?;
    
    Ok(GoogleOAuthStatus {
        connected: true,
        email: secret.email,
        expires_at: None,
        project_id: Some(project_id),
        needs_reauth: false,
        error: None,
    })
}
