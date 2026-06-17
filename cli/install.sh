#!/usr/bin/env bash
set -euo pipefail

# Kim CLI installer.
#
# Works in two modes:
#   1. From a local checkout: ./cli/install.sh
#   2. One-line remote install:
#        curl -fsSL https://raw.githubusercontent.com/AdamMagued/kim/main/cli/install.sh | bash
#
# What works WITHOUT Python: `kim chat` (direct provider APIs / ollama) and the
# whole slash-command shell. What NEEDS Python (orchestrator + deps at the source
# root): `kim code` with browser providers (the codex browser bridge). The
# installer offers to provision a Python venv for that — see KIM_SETUP_PYTHON.
#
# Environment overrides:
#   KIM_REPO_URL       Git repository to clone when not run from a checkout.
#   KIM_RELEASE_REPO   owner/repo to fetch prebuilt release binaries from.
#   KIM_INSTALL_BRANCH Branch/ref to checkout when cloning/updating.
#   KIM_SOURCE_DIR     Where the source checkout lives for remote installs.
#   KIM_BIN_DIR        Where the kim executable is installed.
#   KIM_FORCE_BUILD=1  Skip the prebuilt download and build from source.
#   KIM_SETUP_PYTHON=1 Provision the Python venv non-interactively (0 to skip).

KIM_REPO_URL="${KIM_REPO_URL:-https://github.com/AdamMagued/kim.git}"
KIM_RELEASE_REPO="${KIM_RELEASE_REPO:-AdamMagued/kim}"
KIM_INSTALL_BRANCH="${KIM_INSTALL_BRANCH:-main}"
KIM_SOURCE_DIR="${KIM_SOURCE_DIR:-${HOME}/.kim/source}"
KIM_BIN_DIR="${KIM_BIN_DIR:-${HOME}/.local/bin}"

say() {
  printf '%s\n' "$*"
}

say_err() {
  printf '%s\n' "$*" >&2
}

die() {
  printf 'kim installer error: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command '$1'"
}

script_dir() {
  if [[ -n "${BASH_SOURCE[0]:-}" ]] && [[ "${BASH_SOURCE[0]}" != "bash" ]] && [[ -f "${BASH_SOURCE[0]}" ]]; then
    cd "$(dirname "${BASH_SOURCE[0]}")" && pwd
  else
    return 1
  fi
}

checkout_is_usable() {
  local root="$1"
  [[ -f "${root}/cli/Cargo.toml" && -d "${root}/orchestrator" ]]
}

resolve_source_root() {
  local local_script_dir
  if local_script_dir="$(script_dir 2>/dev/null)"; then
    local local_root
    local_root="$(cd "${local_script_dir}/.." && pwd)"
    if checkout_is_usable "${local_root}"; then
      printf '%s\n' "${local_root}"
      return 0
    fi
  fi

  need_cmd git
  mkdir -p "$(dirname "${KIM_SOURCE_DIR}")"

  if [[ -d "${KIM_SOURCE_DIR}/.git" ]]; then
    say_err "Updating Kim source at ${KIM_SOURCE_DIR}"
    if ! git -C "${KIM_SOURCE_DIR}" diff --quiet || ! git -C "${KIM_SOURCE_DIR}" diff --cached --quiet; then
      die "${KIM_SOURCE_DIR} has local changes. Set KIM_SOURCE_DIR to another path or clean it first."
    fi
    git -C "${KIM_SOURCE_DIR}" fetch --depth 1 origin "${KIM_INSTALL_BRANCH}"
    git -C "${KIM_SOURCE_DIR}" checkout -q "${KIM_INSTALL_BRANCH}" 2>/dev/null \
      || git -C "${KIM_SOURCE_DIR}" checkout -q -B "${KIM_INSTALL_BRANCH}" FETCH_HEAD
    git -C "${KIM_SOURCE_DIR}" reset --hard -q FETCH_HEAD
  elif [[ -e "${KIM_SOURCE_DIR}" ]]; then
    die "${KIM_SOURCE_DIR} exists but is not a git checkout. Set KIM_SOURCE_DIR to another path."
  else
    say_err "Cloning Kim source into ${KIM_SOURCE_DIR}"
    git clone --depth 1 --branch "${KIM_INSTALL_BRANCH}" "${KIM_REPO_URL}" "${KIM_SOURCE_DIR}"
  fi

  checkout_is_usable "${KIM_SOURCE_DIR}" || die "source checkout is missing cli/Cargo.toml or orchestrator/"
  printf '%s\n' "${KIM_SOURCE_DIR}"
}

# Map uname OS/arch to the release asset suffix produced by release.yml.
detect_asset_suffix() {
  local os arch
  os="$(uname -s 2>/dev/null || printf 'unknown')"
  arch="$(uname -m 2>/dev/null || printf 'unknown')"
  case "${os}" in
    Darwin)
      case "${arch}" in
        arm64|aarch64) printf 'macos-aarch64' ;;
        x86_64)        printf 'macos-x86_64' ;;
        *) return 1 ;;
      esac ;;
    Linux)
      case "${arch}" in
        x86_64) printf 'linux-x86_64' ;;
        *) return 1 ;;
      esac ;;
    *) return 1 ;;
  esac
}

# A14: try to install a prebuilt `kim` from the latest GitHub release.
# Echoes nothing; returns 0 on success (binary installed + runs), non-zero to
# signal the caller should fall back to building from source.
try_install_prebuilt() {
  [[ "${KIM_FORCE_BUILD:-0}" == "1" ]] && return 1
  command -v curl >/dev/null 2>&1 || return 1

  local suffix
  suffix="$(detect_asset_suffix)" || return 1

  local api="https://api.github.com/repos/${KIM_RELEASE_REPO}/releases/latest"
  local url
  # Asset names are kim-cli-<version>-<suffix>[.exe]; pick the one for this host.
  url="$(curl -fsSL "${api}" 2>/dev/null \
    | grep -oE "https://[^\"]*kim-cli-[^\"]*${suffix}(\.exe)?" \
    | head -n1)" || return 1
  [[ -n "${url}" ]] || return 1

  local tmp
  tmp="$(mktemp -d)" || return 1
  say_err "Downloading prebuilt kim (${suffix}) from ${url}"
  if ! curl -fsSL "${url}" -o "${tmp}/kim"; then
    rm -rf "${tmp}"
    return 1
  fi
  chmod +x "${tmp}/kim" 2>/dev/null || true
  # Verify it actually runs on this machine before trusting it.
  if ! "${tmp}/kim" --version >/dev/null 2>&1; then
    say_err "Prebuilt binary did not run; falling back to source build."
    rm -rf "${tmp}"
    return 1
  fi
  mkdir -p "${KIM_BIN_DIR}"
  cp "${tmp}/kim" "${KIM_BIN_DIR}/kim"
  chmod +x "${KIM_BIN_DIR}/kim"
  rm -rf "${tmp}"
  say "Installed prebuilt kim -> ${KIM_BIN_DIR}/kim"
  return 0
}

# C1: warn loudly before repointing an existing ~/.kim_root somewhere else.
write_kim_root() {
  local new_root="$1"
  local marker="${HOME}/.kim_root"
  if [[ -f "${marker}" ]]; then
    local old_root
    old_root="$(cat "${marker}" 2>/dev/null | head -n1)"
    if [[ -n "${old_root}" && "${old_root}" != "${new_root}" ]]; then
      say_err ""
      say_err "⚠  ~/.kim_root already points to a different checkout:"
      say_err "     old: ${old_root}"
      say_err "     new: ${new_root}"
      say_err "   The Kim desktop app and CLI share this marker — repointing it"
      say_err "   changes what 'kim code' / the browser bridge runs."
      say_err ""
    fi
  fi
  mkdir -p "${HOME}/.kim"
  printf '%s\n' "${new_root}" > "${marker}"
  say "Wrote repo root to ${marker}"
}

# A15: offer to provision the Python side (needed only for `kim code` browser
# bridge). Interactive unless KIM_SETUP_PYTHON is set (1 = yes, 0 = skip).
maybe_setup_python() {
  local root="$1"
  if ! command -v python3 >/dev/null 2>&1; then
    say "python3 not found — skipping Python setup. 'kim chat' works without it;"
    say "  'kim code' with browser providers needs python3 + 'pip install -r requirements.txt'."
    return 0
  fi

  local do_setup="${KIM_SETUP_PYTHON:-}"
  if [[ -z "${do_setup}" ]]; then
    if [[ -t 0 ]]; then
      printf 'Set up the Python venv for code-mode browser bridge now? [y/N] ' >&2
      local reply=""
      read -r reply || reply=""
      case "${reply}" in y|Y|yes|YES) do_setup=1 ;; *) do_setup=0 ;; esac
    else
      do_setup=0
    fi
  fi

  if [[ "${do_setup}" != "1" ]]; then
    say "Skipped Python setup. To enable code-mode browser bridge later:"
    say "  python3 -m venv \"${root}/venv\" && \"${root}/venv/bin/pip\" install -r \"${root}/requirements.txt\""
    return 0
  fi

  say "Creating Python venv at ${root}/venv"
  if ! python3 -m venv "${root}/venv"; then
    say_err "venv creation failed — set it up manually later."
    return 0
  fi
  if [[ -f "${root}/requirements.txt" ]]; then
    say "Installing Python dependencies (this may take a minute)…"
    "${root}/venv/bin/pip" install -q -r "${root}/requirements.txt" \
      || say_err "pip install reported errors — 'kim code' browser bridge may not work."
  fi
}

main() {
  local root cli_dir

  # A14: prefer a prebuilt binary; only fall back to a source build (which needs
  # cargo) when no asset matches or the download/verify fails.
  if try_install_prebuilt; then
    # For a prebuilt install we still want a source checkout for code mode, but
    # only if one is already available locally; don't force a cargo toolchain.
    root="$(resolve_source_root 2>/dev/null || true)"
  else
    need_cmd cargo
    root="$(resolve_source_root)"
    cli_dir="${root}/cli"
    say "Building Kim CLI from ${cli_dir}"
    cargo build --manifest-path "${cli_dir}/Cargo.toml" --release
    say "Installing kim -> ${KIM_BIN_DIR}/kim"
    mkdir -p "${KIM_BIN_DIR}"
    cp "${cli_dir}/target/release/kim" "${KIM_BIN_DIR}/kim"
    chmod +x "${KIM_BIN_DIR}/kim"
  fi

  if [[ -n "${root:-}" ]]; then
    write_kim_root "${root}"
    maybe_setup_python "${root}"
  else
    say "No local source checkout found; 'kim chat' works now. For 'kim code'"
    say "  run this installer from a Kim checkout or set KIM_SOURCE_DIR."
  fi

  say ""
  say "Done. Run: kim doctor   (checks install & provider readiness)"
  say ""
  say "One-shot commands (no interactive session):"
  say "  kim chat \"explain this\"   run a single chat prompt and exit"
  say "  kim code \"fix the bug\"    run a single coding-agent prompt and exit"
  say ""
  say "Interactive mode (no arguments):"
  say "  /chat                    switch to chat mode"
  say "  /code                    switch to code mode"
  say "  /login ollama            local models, no key"
  say "  /login browser:claude    Claude via Kim desktop bridge, no API key"
  say "  /login browser:chatgpt   ChatGPT via Kim desktop bridge, no API key"
  say "  /login browser:gemini    Gemini via Kim desktop bridge, no API key"
  say ""
  say "Browser providers require the Kim desktop app to be running."
  say "Chat mode routes through /v1/task; code mode uses the browser codex bridge."

  case ":${PATH}:" in
    *":${KIM_BIN_DIR}:"*) ;;
    *)
      say ""
      say "Add this to your shell profile if 'kim' is not found:"
      say "  export PATH=\"${KIM_BIN_DIR}:\$PATH\""
      ;;
  esac
}

main "$@"
