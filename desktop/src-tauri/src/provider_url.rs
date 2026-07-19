// provider_url.rs — provider/site identity, URL classification, and meta-write helpers.
// Extracted from lib.rs (file-split restructure) — behavior unchanged.

use crate::*;
use std::path::Path;

pub(crate) fn normalize_site(site: &str) -> String {
    match site.trim().to_lowercase().as_str() {
        "chatgpt" | "openai" | "gpt" => "chatgpt".to_string(),
        "gemini" | "google" => "gemini".to_string(),
        "deepseek" | "deepseek-browser" => "deepseek".to_string(),
        other if !other.is_empty() => other.to_string(),
        _ => "chatgpt".to_string(),
    }
}

pub(crate) fn host_matches_site(host: &str, site: &str) -> bool {
    let host = host.trim().trim_start_matches("www.").to_ascii_lowercase();
    match normalize_site(site).as_str() {
        "chatgpt" => {
            host == "chatgpt.com" || host == "chat.openai.com" || host.ends_with(".chatgpt.com")
        }
        "gemini" => host == "gemini.google.com",
        "deepseek" => host == "chat.deepseek.com" || host.ends_with(".deepseek.com"),
        _ => false,
    }
}

pub(crate) fn browser_url_site(url: &str) -> Option<String> {
    let parsed = tauri::Url::parse(url).ok()?;
    let host = parsed.host_str()?.to_ascii_lowercase();
    for site in ["chatgpt", "gemini", "deepseek"] {
        if host_matches_site(&host, site) {
            return Some(site.to_string());
        }
    }
    None
}

pub(crate) fn browser_url_is_bad_for_commit(url: &str, site: &str) -> bool {
    let trimmed = url.trim();
    if trimmed.is_empty() {
        return true;
    }
    let Ok(parsed) = tauri::Url::parse(trimmed) else {
        return true;
    };
    if !matches!(parsed.scheme(), "https" | "http") {
        return true;
    }
    let host = parsed.host_str().unwrap_or("").to_ascii_lowercase();
    if !host_matches_site(&host, site) {
        return true;
    }

    let lower = trimmed.to_ascii_lowercase();
    if lower.contains("accounts.google.com")
        || lower.contains("/login")
        || lower.contains("signin")
        || lower.contains("sign-in")
        || lower.contains("servicelogin")
        || lower.contains("signoutoptions")
        || lower.contains("/auth")
        || lower.contains("oauth")
    {
        return true;
    }

    let normalized = lower.trim_end_matches('/');
    let site_norm = normalize_site(site);
    match site_norm.as_str() {
        "chatgpt" => normalized == "https://chatgpt.com" || normalized == "https://chat.openai.com",
        "gemini" => {
            normalized == "https://gemini.google.com"
                || normalized == "https://gemini.google.com/app"
        }
        "deepseek" => normalized == "https://chat.deepseek.com",
        _ => true,
    }
}

pub(crate) fn browser_url_allowed_for_restore(url: &str, site: &str) -> bool {
    // Restore is deliberately stricter than "same host": do not navigate to
    // arbitrary URLs, login/auth pages, or provider home/new-chat pages stored
    // by mistake. Fallback home navigation is controlled separately.
    !browser_url_is_bad_for_commit(url, site)
}

pub(crate) fn last_llm_provider_allowed(p: &str) -> bool {
    if p.is_empty() || p.len() > 64 {
        return false;
    }
    matches!(
        p,
        "browser"
            | "browser:chatgpt"
            | "browser:gemini"
            | "browser:deepseek"
            | "browser:custom"
            | "claude"
            | "openai"
            | "gemini"
            | "deepseek"
            | "ollama"
    )
}

pub(crate) fn default_site_url(site: &str) -> &'static str {
    match normalize_site(site).as_str() {
        "gemini" => "https://gemini.google.com/app",
        "deepseek" => "https://chat.deepseek.com",
        _ => "https://chatgpt.com",
    }
}

pub(crate) fn gemini_site_url(authuser: Option<u32>) -> String {
    match authuser {
        Some(index) => format!("https://gemini.google.com/app?authuser={index}"),
        None => "https://gemini.google.com/app".to_string(),
    }
}

pub(crate) fn fresh_site_url(site: &str, authuser: Option<u32>) -> String {
    if normalize_site(site) == "gemini" {
        gemini_site_url(authuser)
    } else {
        default_site_url(site).to_string()
    }
}

pub(crate) fn apply_browser_meta_writes(
    meta: &mut BrowserSessionMeta,
    browser_last_site: Option<String>,
    site: Option<String>,
    url: Option<String>,
    last_llm_provider: Option<String>,
) -> Result<(), String> {
    if let Some(last) = browser_last_site
        .as_deref()
        .map(normalize_site)
        .filter(|s| !s.is_empty())
    {
        meta.browser_last_site = Some(last);
    }

    if let (Some(site_raw), Some(url_raw)) = (site.as_deref(), url.as_deref()) {
        let site_norm = normalize_site(site_raw);
        if browser_url_is_bad_for_commit(url_raw, &site_norm) {
            return Err(format!(
                "Refusing to store non-conversation/login URL for {}: {}",
                site_norm, url_raw
            ));
        }
        meta.browser_threads
            .insert(site_norm.clone(), url_raw.trim().to_string());
        meta.browser_last_site = Some(site_norm);
    }

    if let Some(p) = last_llm_provider {
        let t = p.trim();
        if last_llm_provider_allowed(t) {
            meta.last_llm_provider = Some(t.to_string());
        }
    }

    meta.browser_threads_updated_at_ms = Some(now_ms());
    Ok(())
}

pub(crate) fn browser_restore_status_for_session(
    session_dir: &Path,
    session_id: Option<&str>,
    provider_arg: &str,
) -> String {
    let Some(session_id) = session_id.map(str::trim).filter(|s| !s.is_empty()) else {
        return "new_or_unknown".to_string();
    };
    if validate_session_id(session_id).is_err() {
        return "new_or_unknown".to_string();
    }

    let site = if provider_arg.starts_with("browser:") {
        normalize_site(provider_arg.trim_start_matches("browser:"))
    } else if provider_arg == "browser" {
        // The UI stores browser_last_site in the sidecar before send. If the
        // provider is the generic "browser", read that hint below.
        "".to_string()
    } else {
        return "not_browser".to_string();
    };

    let date_dir = match resolve_session_date_dir(session_dir, session_id, None) {
        Ok(v) => v,
        Err(_) => return "new_or_unknown".to_string(),
    };
    let meta = read_browser_session_meta_from_dir(&date_dir, session_id).unwrap_or_default();
    let resolved_site = if site.is_empty() {
        meta.browser_last_site
            .clone()
            .unwrap_or_else(|| "chatgpt".to_string())
    } else {
        site
    };

    match meta.browser_threads.get(&resolved_site) {
        Some(url) if browser_url_allowed_for_restore(url, &resolved_site) => {
            "stored_thread".to_string()
        }
        Some(_) => "stored_url_rejected".to_string(),
        None => "no_stored_url".to_string(),
    }
}
