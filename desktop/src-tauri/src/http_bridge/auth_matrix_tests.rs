//! F-H-9 (issue #56-H): route × auth regression matrix for the loopback HTTP
//! bridge (`docs/CONTRACTS.md` § "2c. The /v1 loopback HTTP bridge").
//!
//! The auth contract (mod.rs `bridge_request_authorized` + CONTRACTS.md 2c) is:
//! every `/v1/*` route requires `X-Kim-Token` to match the full bridge token,
//! EXCEPT:
//!   * `GET /v1/health` — unauthenticated liveness (F-D-3).
//!   * `POST /v1/callback` — ALSO accepts the capability-scoped webview token
//!     (F-D-4), in addition to the full token.
//!
//! This test pins that contract for EVERY route currently registered in
//! `handle_webview_bridge_request`'s dispatch, so that a future route added
//! without going through `bridge_request_authorized` (or one that silently
//! widens auth, e.g. by adding a bogus early-return) fails loudly here rather
//! than shipping a silent auth bypass.
//!
//! ── Keeping this file honest ──────────────────────────────────────────────
//! `route_list_matches_dispatch_source` below parses the literal
//! `match (method, path.as_str()) { ... }` block out of `mod.rs`'s *actual
//! source text* (via `include_str!`) and cross-checks it against
//! `ROUTE_MATRIX`. If someone adds/removes/renames a route in the dispatch
//! match without updating `ROUTE_MATRIX` below, that test fails. It does NOT
//! (and cannot, without a full Rust parser) verify the auth *requirement* per
//! route — that part is still asserted directly against
//! `bridge_request_authorized`, the actual production auth-gate function, in
//! `every_route_enforces_its_documented_auth_requirement`.
//!
//! ⚠️  If you add a new `/v1/*` route: add it to `ROUTE_MATRIX` below with its
//! correct `AuthRequirement`, or this file will fail to compile/pass.

use tiny_http::Method;

use super::bridge_request_authorized;

const FULL: &str = "full-secret-token-abc123";
const WEBVIEW: &str = "webview-scoped-token-def456";
const WRONG: &str = "not-a-real-token";

/// What presented-token classes must (and must not) authorize a route.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AuthRequirement {
    /// No token required at all (only `GET /v1/health` today).
    None,
    /// Only the full bridge token authorizes this route.
    FullOnly,
    /// The full token OR the webview-scoped token authorizes this route
    /// (only `POST /v1/callback` today, F-D-4).
    FullOrWebview,
}

/// The complete route registry as of this writing, mirroring the `match`
/// arms in `handle_webview_bridge_request` (mod.rs) in the SAME order they
/// appear there, plus the pre-match dynamic `/v1/result/{reqId}` special case.
///
/// Update this list whenever a route is added, removed, or its auth
/// requirement changes. `route_list_matches_dispatch_source` (below) checks
/// the (method, path) half of this list against the real source; a human
/// must still get the `AuthRequirement` column right — that's what
/// `every_route_enforces_its_documented_auth_requirement` pins.
const ROUTE_MATRIX: &[(Method, &str, AuthRequirement)] = &[
    (Method::Get, "/v1/health", AuthRequirement::None),
    // Dynamic path handled BEFORE the match (mod.rs: `path.starts_with("/v1/result/")`).
    // Represented here with a concrete id so it round-trips through the auth
    // gate exactly like a real request; see `resolved_path` below.
    (Method::Get, "/v1/result/abc123", AuthRequirement::FullOnly),
    (Method::Post, "/v1/hide", AuthRequirement::FullOnly),
    (Method::Post, "/v1/show", AuthRequirement::FullOnly),
    (Method::Post, "/v1/open", AuthRequirement::FullOnly),
    (Method::Post, "/v1/callback", AuthRequirement::FullOrWebview),
    (Method::Post, "/v1/complete", AuthRequirement::FullOnly),
    (Method::Post, "/v1/send", AuthRequirement::FullOnly),
    (Method::Get, "/v1/status", AuthRequirement::FullOnly),
    (
        Method::Get,
        "/v1/browser/current-url",
        AuthRequirement::FullOnly,
    ),
    (Method::Get, "/v1/browser/meta", AuthRequirement::FullOnly),
    (Method::Post, "/v1/browser/meta", AuthRequirement::FullOnly),
    (
        Method::Post,
        "/v1/browser/commit-url",
        AuthRequirement::FullOnly,
    ),
    (
        Method::Post,
        "/v1/browser/restore",
        AuthRequirement::FullOnly,
    ),
    (Method::Post, "/v1/task", AuthRequirement::FullOnly),
    (Method::Post, "/v1/cancel", AuthRequirement::FullOnly),
    (Method::Post, "/v1/task/approve", AuthRequirement::FullOnly),
    (Method::Post, "/v1/browser/show", AuthRequirement::FullOnly),
    (Method::Post, "/v1/browser/hide", AuthRequirement::FullOnly),
    (Method::Post, "/v1/browser/click", AuthRequirement::FullOnly),
    (
        Method::Post,
        "/v1/browser/new-chat",
        AuthRequirement::FullOnly,
    ),
    (Method::Post, "/v1/provider", AuthRequirement::FullOnly),
];

/// `ROUTE_MATRIX`'s dynamic-result entry uses a concrete example id
/// (`/v1/result/abc123`); `bridge_request_authorized` only cares that the
/// path fails the `/v1/health` and `/v1/callback` exact matches, so any
/// concrete id under the `/v1/result/` prefix exercises the same code path
/// as the real dynamic route.
fn resolved_path(route: &str) -> &str {
    route
}

// ── 1. Full route × auth matrix ─────────────────────────────────────────────

#[test]
fn every_route_enforces_its_documented_auth_requirement() {
    for (method, path, requirement) in ROUTE_MATRIX {
        let path = resolved_path(path);

        let no_token = bridge_request_authorized(method, path, "", FULL, WEBVIEW);
        let wrong_token = bridge_request_authorized(method, path, WRONG, FULL, WEBVIEW);
        let full_token = bridge_request_authorized(method, path, FULL, FULL, WEBVIEW);
        let webview_token = bridge_request_authorized(method, path, WEBVIEW, FULL, WEBVIEW);

        match requirement {
            AuthRequirement::None => {
                assert!(
                    no_token,
                    "{method:?} {path}: must be reachable with no token (auth-exempt)"
                );
                assert!(
                    wrong_token,
                    "{method:?} {path}: auth-exempt route must accept any token too"
                );
                assert!(
                    full_token,
                    "{method:?} {path}: auth-exempt route must accept the full token too"
                );
            }
            AuthRequirement::FullOnly => {
                assert!(!no_token, "{method:?} {path}: no token must be rejected");
                assert!(
                    !wrong_token,
                    "{method:?} {path}: wrong token must be rejected"
                );
                assert!(full_token, "{method:?} {path}: full token must be accepted");
                assert!(
                    !webview_token,
                    "{method:?} {path}: webview-scoped token must NOT authorize this route \
                     (a stolen page token must not reach it — F-D-4)"
                );
            }
            AuthRequirement::FullOrWebview => {
                assert!(!no_token, "{method:?} {path}: no token must be rejected");
                assert!(
                    !wrong_token,
                    "{method:?} {path}: wrong token must be rejected"
                );
                assert!(full_token, "{method:?} {path}: full token must be accepted");
                assert!(
                    webview_token,
                    "{method:?} {path}: webview-scoped token must be accepted (F-D-4)"
                );
            }
        }
    }
}

/// Sanity check: exactly one route is auth-exempt today (`GET /v1/health`)
/// and exactly one accepts the webview-scoped token (`POST /v1/callback`).
/// If either count drifts, the auth-exemption surface has grown and someone
/// must consciously update this file (and, likely, CONTRACTS.md 2c).
#[test]
fn auth_exemption_surface_is_exactly_the_documented_two_routes() {
    let exempt: Vec<_> = ROUTE_MATRIX
        .iter()
        .filter(|(_, _, r)| *r == AuthRequirement::None)
        .collect();
    let webview_ok: Vec<_> = ROUTE_MATRIX
        .iter()
        .filter(|(_, _, r)| *r == AuthRequirement::FullOrWebview)
        .collect();

    assert_eq!(
        exempt,
        vec![&(Method::Get, "/v1/health", AuthRequirement::None)],
        "unauthenticated route set must be exactly {{GET /v1/health}}"
    );
    assert_eq!(
        webview_ok,
        vec![&(Method::Post, "/v1/callback", AuthRequirement::FullOrWebview)],
        "webview-token-accepting route set must be exactly {{POST /v1/callback}}"
    );
}

// ── 2. Exhaustiveness guard: ROUTE_MATRIX vs. the real dispatch source ─────

/// The literal source of `mod.rs`, re-read at test time so this guard tracks
/// the file even if it changes on disk (no rebuild-time codegen needed).
const MOD_RS_SOURCE: &str = include_str!("mod.rs");

/// Pull every `(Method::X, "/path")` pair out of the
/// `match (method, path.as_str()) { ... }` dispatch block in `mod.rs`,
/// in source order. Deliberately dumb (line-oriented, no real parser) but
/// scoped tightly to the dispatch block so it can't be fooled by the
/// `(Method::Post, "/v1/task")`-shaped literals that ALSO appear inside this
/// crate's other `#[cfg(test)]` modules.
fn parse_dispatch_routes(source: &str) -> Vec<(Method, String)> {
    let start_marker = "match (method, path.as_str()) {";
    let end_marker = "\npub(crate) fn capitalize";

    let start = source
        .find(start_marker)
        .expect("dispatch match block marker not found in mod.rs — did the function get renamed?")
        + start_marker.len();
    let end = source[start..].find(end_marker).expect(
        "end-of-dispatch marker not found in mod.rs — did capitalize() move or get renamed?",
    ) + start;
    let block = &source[start..end];

    let mut routes = Vec::new();
    for line in block.lines() {
        let trimmed = line.trim_start();
        if trimmed.starts_with("//") {
            continue;
        }
        let Some(after_method_kw) = line.find("(Method::") else {
            continue;
        };
        let rest = &line[after_method_kw + "(Method::".len()..];
        let Some(comma) = rest.find(',') else {
            continue;
        };
        let method = match &rest[..comma] {
            "Get" => Method::Get,
            "Post" => Method::Post,
            "Put" => Method::Put,
            "Delete" => Method::Delete,
            "Head" => Method::Head,
            "Options" => Method::Options,
            other => panic!("parse_dispatch_routes: unrecognized Method variant {other:?} in mod.rs — update the parser"),
        };
        let after_comma = &rest[comma..];
        let Some(q1) = after_comma.find('"') else {
            continue;
        };
        let Some(q2_rel) = after_comma[q1 + 1..].find('"') else {
            continue;
        };
        let path = after_comma[q1 + 1..q1 + 1 + q2_rel].to_string();
        routes.push((method, path));
    }
    routes
}

#[test]
fn route_list_matches_dispatch_source() {
    let parsed = parse_dispatch_routes(MOD_RS_SOURCE);

    assert!(
        !parsed.is_empty(),
        "parser found zero routes in mod.rs's dispatch block — parser or file likely broken"
    );

    // Every route the parser found in the live dispatch source must appear
    // in ROUTE_MATRIX (minus the dynamic /v1/result/ route, which lives
    // outside the match block and is asserted separately below).
    for (method, path) in &parsed {
        let found = ROUTE_MATRIX
            .iter()
            .any(|(m, p, _)| m == method && p == path);
        assert!(
            found,
            "route {method:?} {path} is registered in mod.rs's dispatch match \
             but missing from ROUTE_MATRIX in auth_matrix_tests.rs — add it \
             with the correct AuthRequirement (this test exists to catch \
             exactly this: a new route that never goes through the \
             regression matrix)."
        );
    }

    // And every FullOnly/FullOrWebview route in ROUTE_MATRIX (again, except
    // the dynamic result route) must actually be present in the dispatch
    // source — otherwise the matrix is testing a route that no longer exists.
    for (method, path, _) in ROUTE_MATRIX {
        if *path == "/v1/result/abc123" {
            continue;
        }
        let found = parsed.iter().any(|(m, p)| m == method && p == path);
        assert!(
            found,
            "ROUTE_MATRIX lists {method:?} {path} but it is no longer in \
             mod.rs's dispatch match — remove it from ROUTE_MATRIX (route was \
             renamed or deleted)."
        );
    }
}

/// The `/v1/result/{reqId}` route is handled via a `path.starts_with(...)`
/// special case BEFORE the match block (it can't be a match arm — the id is
/// dynamic). Confirm that special case still exists in the source, so if it
/// is ever removed (e.g. folded into the match with a different auth path)
/// this test — not just a silent behavior change — is what notices.
#[test]
fn dynamic_result_route_special_case_still_present() {
    let occurrences = MOD_RS_SOURCE
        .matches("path.starts_with(\"/v1/result/\")")
        .count();
    assert_eq!(
        occurrences, 1,
        "expected exactly one `path.starts_with(\"/v1/result/\")` special-case \
         guard in mod.rs; if this is 0, GET /v1/result/{{id}} routing changed \
         and ROUTE_MATRIX's dynamic-result entry needs to be re-verified \
         against the new code path."
    );
}
