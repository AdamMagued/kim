# Issue #54 — Release trust-chain design (owner decision)

**Status: OWNER-DECISION — design only. No signing or verification change is implemented by this document.**

This document resolves the design question raised by F-I-1 and F-K-6. It does not
authorize a signing scheme, key custody model, updater migration, or fail-closed
policy. SHA-256 sidecars remain useful for detecting accidental corruption, but a
checksum downloaded from the same release as its payload is not proof of publisher
authenticity: an actor able to replace the payload can replace the sidecar too.

## Current state and trust flows

| Product / artifact | Producer and transport | Current consumer / check | Authenticity gap |
|---|---|---|---|
| Bare KimCLI binaries (`kim-cli-*`, including `.exe`) | The tagged GitHub Actions matrix builds and uploads them to a draft GitHub Release. | A keyless `cosign sign-blob` step publishes a `.sig` and signing certificate `.pem` beside each bare binary. Release notes show a generic manual command. | No installer consumes this material, and the documented command does not pin the expected GitHub workflow/repository identity. |
| CLI install archives (`kim-<triple>.tar.gz` / `.zip`) | The same matrix repackages the bare binary and publishes archive plus `.sha256`. | `install-kim.sh` and `install-kim.ps1` download the archive and same-origin sidecar and fail on missing/mismatched checksums unless `KIM_SKIP_CHECKSUM=1`. | Archives—the artifacts actually installed by the one-line flows—are not signed. Their checksum proves corruption detection only, not publisher identity. |
| Desktop bundles/installers (`.app`, `.dmg`, Windows installer, Linux bundles) | `tauri-action` builds them and creates/updates a draft GitHub Release. macOS certificate import is conditional. | Platform mechanisms may validate a build when the corresponding platform signing configuration exists. | There is no uniform project-level signature covering all published desktop artifacts. A missing Apple certificate only emits a notice, so a tag build can continue unsigned/unnotarized. |
| Desktop update discovery | `App.tsx` calls the fixed `AdamMagued/kim` GitHub latest-release API and compares tags. | The UI displays release notes/version. | Discovery metadata is not itself verified by Kim. |
| Desktop “Update Now” | `UpdateModal.tsx` invokes `run_update`; `run_history.rs` validates that `origin` has a `github.com` host, performs `git pull --ff-only`, then updates Python dependencies and restarts. | `git verify-commit HEAD` is best-effort only: failure or missing GPG support produces a warning and continues. | This updates a source checkout, not a downloaded desktop bundle. Host validation does not pin owner/repository, and signature verification is not fail-closed. |
| Tauri-native updater | None. `tauri.conf.json` has bundle configuration but no updater endpoint/public key, and `updater.rs` explicitly says checking is frontend-owned. | None. | No Tauri signed-update channel is configured; do not describe the current UI as one. |

`KIM_RELEASE_REPO` changes the repository from which the CLI installers download.
Today that is a distribution override, not an authenticity policy: the same checksum
logic applies to the selected fork. Any future verifier must define whether the trust
root follows the override or remains pinned to upstream. Silently pinning upstream
identity while downloading a fork would make legitimate forks unverifiable; silently
trusting any selected fork would make the environment variable a trust-root override.

## Threat model and assumptions

The protected property is that a user installs bytes produced by the intended Kim
release process for the requested version and platform. Relevant adversaries include:

- an attacker who can replace GitHub Release assets or their checksum sidecars;
- a compromised repository token, maintainer account, workflow dependency, or runner;
- a malicious fork or local/environment manipulation of `KIM_RELEASE_REPO`;
- a compromised long-lived signing key or OIDC-enabled release workflow;
- network or mirror tampering, including stale/replayed valid releases; and
- local tampering with a source checkout, git configuration, trusted keyring, or updater.

TLS and GitHub availability are operational dependencies, not sufficient authenticity
roots. Keyless signing reduces long-lived key theft but still trusts GitHub OIDC, the
pinned workflow definition/dependencies, Sigstore certificate policy, and verifier
bootstrap. A long-lived key removes the online OIDC/transparency dependency but makes
custody, recovery, rotation, and revocation first-class operational risks. Neither
scheme alone prevents a compromised authorized workflow from signing malicious output;
workflow hardening, protected tags/environments, reproducibility/provenance, and least
privilege remain separate controls.

## Options

| Dimension | A. Sigstore keyless, GitHub OIDC | B. Long-lived minisign key with KMS/hardware custody | C. Split: Tauri-native desktop signing + CLI trust chain |
|---|---|---|---|
| Trust root / custody | Fulcio/Sigstore roots plus GitHub OIDC claims; no Kim private key at rest. Pin workflow identity, repository, ref policy, and OIDC issuer. | Public key ships with verifier/install docs; private key is held in hardware/KMS or an offline signing host. CI receives only narrowly-scoped signing access or signs through an approval service. | Tauri updater public key is embedded in the desktop app and its private key is separately custodied; CLI uses either A or B. OS code-signing identities remain additional platform roots. |
| Identity pinning | Verify certificate issuer and exact repository/workflow identity; avoid an unconstrained certificate-regexp. Decide tag/ref and reusable-workflow semantics. | Pin the exact minisign public key/fingerprint. Repository identity is policy around who may invoke the signer, not an assertion in the signature itself. | Desktop pins the Tauri updater key; CLI pins its chosen identity/key. Release automation must prove both sets refer to the same version/artifact manifest. |
| Verifier bootstrap / availability | Installers need a trusted cosign binary/library or a safe bootstrap path. “Verify when cosign happens to exist” creates two security classes. | A small verifier may be vendored or use minisign already installed, but its binary/bootstrap still needs authentication. Embedded public key is simple and offline-capable. | Existing desktop app supplies the Tauri verifier; first installation still relies on OS signing/manual distribution. CLI retains A/B bootstrap trade-offs. |
| Rekor and offline behavior | Decide whether inclusion proof/timestamp is required and how offline verification uses a bundle. Online-only Rekor makes installation depend on service/network availability; accepting cert+signature alone weakens transparency guarantees. | No Rekor dependency. Offline verification is straightforward, but independent timestamp/transparency evidence must be added if required. | Tauri updates can verify offline cryptographically after metadata/artifact download; CLI follows A/B. Optional transparency/provenance can be layered on both. |
| Rotation / revocation | Rotate by policy/workflow identity changes and Sigstore root updates. Define behavior for compromised workflow identities, historic bundles, and verifier trust-root updates. | Publish an authenticated key-transition statement, overlap old/new keys for a bounded window, and maintain a revocation channel. Lost key recovery must not become “accept unsigned.” | Coordinate Tauri key rotation through an app release trusted by the old key; rotate CLI independently. Losing the desktop key can strand older clients. |
| Fork semantics | Default pin `AdamMagued/kim`; for `KIM_RELEASE_REPO`, either require an explicit fork identity policy or an explicit unsafe/unverified mode. Deriving identity from an untrusted env var is not meaningful pinning. | A fork must supply an explicitly trusted public key, not inherit upstream merely because its repository name changed. | Upstream desktop clients stay pinned to upstream Tauri key. Forks build clients with their own updater key and configure a separate CLI key/identity. |
| Operational burden | Low key custody; medium policy/verifier/transparency complexity and external-service dependence. | High custody, approval, audit, backup, ceremony, and incident-response burden; lower online infrastructure dependence. | Highest implementation/release complexity, but aligns verification with each product and avoids pretending source-git update is a bundle updater. |
| Compromise blast radius | Compromised authorized workflow can sign release outputs during its authorization window; no reusable private key to steal. | Compromised key/signing service can sign arbitrary versions until revocation reaches users. Hardware policy can constrain use but is an ongoing operational control. | Compromise may be contained to CLI or desktop key. Release orchestration becomes a shared point that must not mix or omit signatures. |

### Non-binding recommendation

Prefer the split product model (Option C): use Tauri’s native signed-updater mechanism
if the owner chooses binary desktop updates, and use Sigstore keyless GitHub OIDC for
the CLI archives plus a signed release manifest. This matches the existing products,
avoids introducing a long-lived general-purpose CI secret, and gives the desktop an
embedded verifier. It is non-binding because verifier bootstrap/offline policy,
workflow identity, fork support, and signing-key custody are owner choices. If robust
offline CLI verification and independence from Sigstore services outweigh custody
cost, minisign with hardware/KMS custody is a valid alternative.

## Explicit owner decisions required

1. Choose the scheme and custody model: keyless Sigstore, long-lived minisign, or the
   split scheme; name the accountable key/workflow owners and incident responder.
2. Define the signed set: bare CLI binaries, install archives, checksum sidecars, a
   canonical manifest, desktop bundles/installers, updater metadata, SBOM/provenance.
   Signing a canonical manifest can cover hashes of all assets; direct signatures may
   still be retained for ergonomic verification.
3. Choose fail-closed behavior when signature material, verifier, transparency proof,
   network, or identity policy is unavailable. Decide whether any override exists and
   how prominently it is named, logged, and documented.
4. Define unsigned `workflow_dispatch`/development artifacts: never publish as a
   release, clearly label them non-release, or require the same signing gate. Decide
   whether dry runs exercise verification using an isolated test identity/key.
5. Decide whether tagged macOS releases must fail when signing/notarization secrets are
   absent, and define equivalent Windows/Linux platform-signing requirements.
6. Choose the updater product direction: retain source/git pull, adopt Tauri-native
   binary updates, support both as explicitly separate modes, or remove in-app update.
7. Define `KIM_RELEASE_REPO`: upstream-only verified installs, explicit per-fork trust
   configuration, or a clearly unsafe override. Do not implicitly trust arbitrary fork
   identity from the same environment value that selects the download.

## Phased future implementation (not performed)

### Phase 0 — policy and test fixtures

- Record the owner decisions in this document or an ADR and specify exact certificate
  claims/public-key fingerprints and asset naming.
- Future files: `docs/THREAT_MODEL.md`, `docs/ops/TRIAGE.md`, release/operator docs, and
  dedicated verification fixtures under `tests/` or `scripts/tests/`.
- Create an isolated test identity/key and fixtures for valid, corrupted, missing,
  wrong-repository, wrong-workflow, expired/revoked, replayed, and rotated signatures.

### Phase 1 — make the release a complete signed set

- Future file: `.github/workflows/release.yml`.
- Generate a deterministic manifest containing version, commit, platform, artifact
  name, size, and SHA-256 for every CLI archive/binary and any selected desktop asset.
- Sign the manifest and/or every owner-selected artifact; verify the staged outputs in
  the workflow before upload. Fail a tag release if required signatures or macOS
  signing/notarization are absent. Keep `workflow_dispatch` artifacts explicitly
  non-release unless the owner selects a test-signing policy.
- Preserve old `.sha256` files during migration for corruption checks and older clients.

### Phase 2 — enforce CLI verification

- Future files: `scripts/install-kim.sh`, `scripts/install-kim.ps1`, installer tests,
  and installation documentation.
- Download signature/certificate/bundle or minisign signature, authenticate the exact
  configured identity/key, then verify the archive before extraction. Retain SHA-256 as
  a fast corruption check, not the authenticity decision.
- Introduce explicit trust configuration for forks. During a documented transition,
  older signed releases may use a version-bounded compatibility path; new releases must
  not silently downgrade to checksum-only verification.

### Phase 3 — decide and implement the desktop update product

- If binary updating is selected, future files include
  `desktop/src-tauri/tauri.conf.json`, `desktop/src-tauri/Cargo.toml`, capabilities,
  `desktop/src-tauri/src/updater.rs`, `desktop/src/App.tsx`,
  `desktop/src/components/UpdateModal.tsx`, and `.github/workflows/release.yml`.
  Configure Tauri updater endpoints/public key and signed update metadata; keep update
  discovery and verification in the native updater path.
- If source update remains, future files include `desktop/src-tauri/src/run_history.rs`
  and its tests. Pin repository owner/name and an enforceable commit/tag trust policy;
  do not treat best-effort `git verify-commit` as authentication.
- Migration must present source and binary update modes accurately, preserve a recovery
  route for old clients, and avoid having both modes race or report the other’s version.

### Phase 4 — rotation, telemetry, and rollback readiness

- Publish key/identity rotation and revocation procedures, audit signing events, and
  rehearse compromise recovery. Never include private material in the repository.
- Roll back a faulty release by stopping publication/latest metadata and publishing a
  new, correctly signed higher version; signatures must not be bypassed to downgrade.
  Preserve verified previous installers as an operator recovery channel.

## Required future verification

Positive tests must cover every supported platform, current release, version-pinned
release, offline/bundled proof where supported, explicit trusted fork, and planned key
rotation. Negative tests must prove hard failure for modified archives/manifests,
modified same-origin checksums, missing signatures, wrong repo/workflow/issuer/key,
unsigned tag output, stale/replayed metadata outside policy, unavailable verifier,
invalid Rekor proof when required, and an unconfigured `KIM_RELEASE_REPO` fork.

The release workflow must also perform a clean-room install of the exact uploaded
archive through each installer and verify the installed executable’s version. Desktop
tests must distinguish source/git update behavior from Tauri-native signed updates.

## Evidence references

- `docs/ops/findings/team-i.md` F-I-1
- `docs/ops/findings/team-k.md` F-K-6
- `docs/THREAT_MODEL.md` H1.6 and release-pipeline trust row
- `docs/ops/TRIAGE.md` owner-decision deferral
- `.github/workflows/release.yml`
- `scripts/install-kim.sh`, `scripts/install-kim.ps1`
- `desktop/src-tauri/tauri.conf.json`, `desktop/src-tauri/src/updater.rs`
- `desktop/src/App.tsx`, `desktop/src/components/UpdateModal.tsx`
- `desktop/src-tauri/src/run_history.rs`
