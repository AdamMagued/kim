//! ssrf.rs — host-based SSRF classification for webview navigations.
//!
//! F-D-1 / F-C-4 (desktop half): `/v1/open` (and every other caller of
//! `open_browser_signin_window_inner`) used to navigate the app webview to ANY
//! http/https URL after only a scheme check — no private-range block. A local
//! process holding the bridge token could `POST /v1/open
//! {"url":"http://169.254.169.254/…"}` and the app's own webview would dial
//! cloud-metadata / RFC-1918 / loopback targets, then run the persistent bridge
//! JS + title-pull payload channel over the fetched content.
//!
//! This is the top-level-navigation sibling of Team C's F-C-4 (subresource/XHR
//! guard on the Playwright browser). We mirror the SAME classification the
//! Python guard uses (`mcp_server/tools/web/browser.py::_is_ssrf_target`),
//! including the numeric-IP encodings a browser will actually dial
//! (`2130706433`, `0x7f000001`, `0177.0.0.1`, `127.1`), so the block the user
//! sees is consistent across the two browsers Kim drives.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

/// Parse one WHATWG IPv4 host part. Base is chosen by prefix: `0x`/`0X` → hex,
/// a leading `0` (len>1) → octal, otherwise decimal. (Plain integer parsing
/// rejects leading-zero octal like `0177` — the gap that let `0177.0.0.1` slip
/// past a naive check — so the base is chosen explicitly here.)
fn whatwg_ipv4_part(tok: &str) -> Option<u64> {
    if tok.is_empty() {
        return None;
    }
    let (radix, digits) = if let Some(rest) = tok.strip_prefix("0x").or_else(|| tok.strip_prefix("0X")) {
        (16u32, rest)
    } else if tok.len() > 1 && tok.starts_with('0') {
        (8u32, &tok[1..])
    } else {
        (10u32, tok)
    };
    if digits.is_empty() {
        // Only reachable for a bare "0x"/"0X" prefix with no hex digits — not a
        // valid IPv4 part. (Lone "0" parses as decimal zero via the path below;
        // the octal branch requires len>1 so it never yields empty digits.)
        return None;
    }
    u64::from_str_radix(digits, radix).ok()
}

/// Try to parse *host* as a numeric IP literal in any encoding browsers accept
/// (mirrors the WHATWG URL IPv4 parser). Returns None when the host is a DNS
/// domain name rather than a numeric literal.
fn parse_host_as_ip(host: &str) -> Option<IpAddr> {
    // Fast path: standard dotted-decimal IPv4 or IPv6 literal.
    if let Ok(ip) = host.parse::<IpAddr>() {
        return Some(ip);
    }

    // WHATWG IPv4: 1-4 dot-separated parts, each in any base; a single trailing
    // empty part (host ending in '.') is tolerated.
    let mut parts: Vec<&str> = host.split('.').collect();
    if parts.last() == Some(&"") {
        parts.pop();
    }
    if parts.is_empty() || parts.len() > 4 {
        return None;
    }
    let nums: Vec<u64> = parts.iter().map(|p| whatwg_ipv4_part(p)).collect::<Option<Vec<_>>>()?;

    let n = nums.len();
    // Every part except the last is a single octet (<256).
    if nums[..n - 1].iter().any(|&x| x > 255) {
        return None;
    }
    // The last part holds the remaining (4 - (n-1)) bytes.
    let last_max: u128 = 1u128 << (8 * (4 - (n - 1)));
    if (nums[n - 1] as u128) >= last_max {
        return None;
    }
    let mut value: u64 = nums[n - 1];
    for (i, &octet) in nums[..n - 1].iter().enumerate() {
        value += octet << (8 * (3 - i));
    }
    if value > 0xFFFF_FFFF {
        return None;
    }
    Some(IpAddr::V4(Ipv4Addr::from(value as u32)))
}

fn ipv4_is_internal(a: Ipv4Addr) -> bool {
    if a.is_loopback()
        || a.is_private()
        || a.is_link_local()
        || a.is_broadcast()
        || a.is_unspecified()
        || a.is_multicast()
        || a.is_documentation()
    {
        return true;
    }
    let o = a.octets();
    // Reserved 240.0.0.0/4 (future use).
    if o[0] >= 240 {
        return true;
    }
    // Carrier-grade NAT / shared address space 100.64.0.0/10.
    if o[0] == 100 && (o[1] & 0xC0) == 64 {
        return true;
    }
    // IETF protocol assignments 192.0.0.0/24 (incl. 192.0.0.0/29).
    if o[0] == 192 && o[1] == 0 && o[2] == 0 {
        return true;
    }
    // Benchmarking 198.18.0.0/15.
    if o[0] == 198 && (o[1] & 0xFE) == 18 {
        return true;
    }
    false
}

fn ipv6_is_internal(a: Ipv6Addr) -> bool {
    if a.is_loopback() || a.is_multicast() || a.is_unspecified() {
        return true;
    }
    // IPv4-mapped (::ffff:0:0/96) and IPv4-compatible: classify the embedded v4.
    if let Some(v4) = a.to_ipv4() {
        return ipv4_is_internal(v4);
    }
    let seg = a.segments();
    // Link-local unicast fe80::/10.
    if (seg[0] & 0xffc0) == 0xfe80 {
        return true;
    }
    // Unique local fc00::/7.
    if (seg[0] & 0xfe00) == 0xfc00 {
        return true;
    }
    false
}

fn ip_is_internal(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(a) => ipv4_is_internal(a),
        IpAddr::V6(a) => ipv6_is_internal(a),
    }
}

/// Return true when *host* denotes a loopback / private / link-local / reserved
/// address (or the `localhost` / cloud-metadata spellings) that a webview must
/// not be navigated to. A plain DNS domain name returns false (DNS-rebind is
/// out of scope here, same as the Python guard). An empty host default-denies.
pub(crate) fn host_is_ssrf_target(host: &str) -> bool {
    let h = host.trim().trim_start_matches('[').trim_end_matches(']');
    let lowered = h.to_ascii_lowercase();
    let lowered = lowered.trim_end_matches('.');
    if lowered.is_empty() {
        // No host on an http(s) URL is malformed — default-deny.
        return true;
    }
    // `localhost` (and RFC 6761 `*.localhost`) always resolves to loopback but
    // is not a numeric IP, so it must be blocked by name.
    if lowered == "localhost" || lowered.ends_with(".localhost") {
        return true;
    }
    // GCP link-local metadata alias.
    if lowered == "metadata.google.internal" || lowered.ends_with(".metadata.google.internal") {
        return true;
    }
    match parse_host_as_ip(lowered) {
        Some(ip) => ip_is_internal(ip),
        None => false, // DNS domain name — allowed.
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blocks_loopback_forms() {
        for h in [
            "127.0.0.1",
            "127.1",           // short form -> 127.0.0.1
            "127.0.1",         // short form -> 127.0.0.1
            "2130706433",      // decimal integer 127.0.0.1
            "0x7f000001",      // hex integer
            "0x7f.0.0.1",      // dotted hex octet
            "0177.0.0.1",      // dotted octal octet (the classic bypass)
            "017700000001",    // bare octal integer 127.0.0.1
            "localhost",
            "foo.localhost",
            "::1",
            "[::1]",
            "::ffff:127.0.0.1",
        ] {
            assert!(host_is_ssrf_target(h), "should block loopback host {h:?}");
        }
    }

    #[test]
    fn blocks_cloud_metadata_and_private_ranges() {
        for h in [
            "169.254.169.254",          // AWS/GCP IMDS (link-local)
            "metadata.google.internal", // GCP alias
            "10.0.0.5",                 // RFC-1918
            "172.16.0.1",               // RFC-1918
            "192.168.1.1",              // RFC-1918
            "100.64.0.1",               // CGNAT
            "0.0.0.0",                  // unspecified
            "fd00::1",                  // unique local
            "fe80::1",                  // link-local
            "0xA9FEA9FE",               // 169.254.169.254 in hex
        ] {
            assert!(host_is_ssrf_target(h), "should block internal host {h:?}");
        }
    }

    #[test]
    fn allows_public_provider_hosts() {
        for h in [
            "claude.ai",
            "chatgpt.com",
            "gemini.google.com",
            "accounts.google.com",
            "grok.com",
            "chat.deepseek.com",
            "8.8.8.8",
            "1.1.1.1",
            "example.com",
        ] {
            assert!(
                !host_is_ssrf_target(h),
                "should ALLOW public host {h:?}"
            );
        }
    }

    #[test]
    fn empty_host_default_denies() {
        assert!(host_is_ssrf_target(""));
        assert!(host_is_ssrf_target("   "));
    }
}
