#!/usr/bin/env bash
set -euo pipefail

# Kim CLI installer.
#
# Works in two modes:
#   1. From a local checkout: ./cli/install.sh
#   2. One-line remote install:
#        curl -fsSL https://raw.githubusercontent.com/AdamMagued/kim/main/cli/install.sh | bash
#
# Environment overrides:
#   KIM_REPO_URL       Git repository to clone when not run from a checkout.
#   KIM_INSTALL_BRANCH Branch/ref to checkout when cloning/updating.
#   KIM_SOURCE_DIR     Where the source checkout lives for remote installs.
#   KIM_BIN_DIR        Where the kim executable is installed.

KIM_REPO_URL="${KIM_REPO_URL:-https://github.com/AdamMagued/kim.git}"
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

main() {
  need_cmd cargo

  local root
  root="$(resolve_source_root)"
  local cli_dir="${root}/cli"

  say "Building Kim CLI from ${cli_dir}"
  cargo build --manifest-path "${cli_dir}/Cargo.toml" --release

  say "Installing kim -> ${KIM_BIN_DIR}/kim"
  mkdir -p "${KIM_BIN_DIR}"
  cp "${cli_dir}/target/release/kim" "${KIM_BIN_DIR}/kim"
  chmod +x "${KIM_BIN_DIR}/kim"

  mkdir -p "${HOME}/.kim"
  printf '%s\n' "${root}" > "${HOME}/.kim_root"
  say "Wrote repo root to ${HOME}/.kim_root"

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
