#!/usr/bin/env sh
set -eu

# Installs kimcli — the rebranded, pinned codex-cli fork that backs Kim's
# Code tab (github.com/AdamMagued/codex, branch kim-brand-0.144.3). Mirrors
# scripts/install-kim.sh's conventions (platform/arch detection, hard-fail
# checksum verification, escape hatch, PATH guidance).
#
# Env overrides:
#   KIMCLI_RELEASE_REPO  default: AdamMagued/codex
#   KIMCLI_VERSION        default: kimcli-v0.144.3 (PINNED — never "latest";
#                          kimcli is a version-pinned codex fork, not a
#                          rolling release, so this must always name an
#                          explicit tag)
#   KIM_SKIP_CHECKSUM     set to 1 to bypass sidecar verification (not
#                          recommended; same escape hatch as install-kim.sh)

REPO="${KIMCLI_RELEASE_REPO:-AdamMagued/codex}"
VERSION="${KIMCLI_VERSION:-kimcli-v0.144.3}"
INSTALL_DIR="$HOME/.kim/bin"

if [ "$VERSION" = "latest" ]; then
  echo "ERROR: KIMCLI_VERSION must be a pinned tag (e.g. kimcli-v0.144.3), never 'latest'." >&2
  exit 1
fi

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"

case "$os" in
  darwin)
    case "$arch" in
      arm64|aarch64) target="aarch64-apple-darwin" ;;
      x86_64|amd64)  target="x86_64-apple-darwin" ;;
      *) echo "Unsupported macOS architecture: $arch" >&2; exit 1 ;;
    esac
    ;;
  linux)
    case "$arch" in
      x86_64|amd64) target="x86_64-unknown-linux-musl" ;;
      *) echo "Unsupported Linux architecture: $arch (kimcli ships x86_64-unknown-linux-musl only)" >&2; exit 1 ;;
    esac
    ;;
  mingw*|msys*|cygwin*)
    target="x86_64-pc-windows-msvc"
    ;;
  *)
    echo "Unsupported OS: $os" >&2
    exit 1
    ;;
esac

asset="kimcli-${target}.tar.gz"
if [ "$target" = "x86_64-pc-windows-msvc" ]; then
  asset="kimcli-${target}.zip"
fi

mkdir -p "$INSTALL_DIR"
tmp="$(mktemp -d)"
tmp_dest=""
cleanup() { rm -rf "$tmp"; [ -n "$tmp_dest" ] && rm -f "$tmp_dest"; return 0; }
trap cleanup EXIT INT TERM

url="https://github.com/${REPO}/releases/download/${VERSION}/${asset}"

echo "Downloading kimcli: $url"
if [ -n "${GITHUB_TOKEN:-}" ]; then
  curl -fL -H "Authorization: Bearer ${GITHUB_TOKEN}" "$url" -o "$tmp/$asset"
else
  curl -fL "$url" -o "$tmp/$asset"
fi

# Checksum verification: download and verify the .sha256 sidecar published
# next to each release archive. Missing checksum material is a HARD FAILURE
# — an unverifiable download must never install silently. Set
# KIM_SKIP_CHECKSUM=1 to bypass (not recommended).
if [ "${KIM_SKIP_CHECKSUM:-0}" != "1" ]; then
  case "$url" in
    *.tar.gz) sha_url="${url%.tar.gz}.sha256" ;;
    *.zip)    sha_url="${url%.zip}.sha256" ;;
    *)        sha_url="${url}.sha256" ;;
  esac
  if ! curl -fsSL "$sha_url" -o "$tmp/SHA256SUMS"; then
    echo "ERROR: checksum sidecar not available at $sha_url." >&2
    echo "Refusing to install an unverifiable download. Set KIM_SKIP_CHECKSUM=1 to override (not recommended)." >&2
    exit 1
  fi
  expected=$(grep "$asset" "$tmp/SHA256SUMS" | awk '{print $1}')
  if [ -z "$expected" ]; then
    echo "ERROR: no checksum entry for $asset in $sha_url. Aborting." >&2
    exit 1
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$tmp/$asset" | awk '{print $1}')
  elif command -v shasum >/dev/null 2>&1; then
    actual=$(shasum -a 256 "$tmp/$asset" | awk '{print $1}')
  else
    echo "ERROR: no sha256sum/shasum tool found; cannot verify download." >&2
    echo "Install coreutils, or set KIM_SKIP_CHECKSUM=1 to override (not recommended)." >&2
    exit 1
  fi
  if [ "$actual" != "$expected" ]; then
    echo "ERROR: checksum mismatch (expected $expected, got $actual). Aborting." >&2
    exit 1
  fi
  echo "Checksum verified: $expected"
fi

case "$asset" in
  *.zip) unzip -q "$tmp/$asset" -d "$tmp/out" ;;
  *.tar.gz) mkdir -p "$tmp/out"; tar -xzf "$tmp/$asset" -C "$tmp/out" ;;
esac

bin="$tmp/out/kimcli"
[ -f "$bin.exe" ] && bin="$bin.exe"
if [ ! -f "$bin" ]; then
  echo "Release asset did not contain a kimcli binary." >&2
  exit 1
fi

dest="$INSTALL_DIR/kimcli"
if [ "$target" = "x86_64-pc-windows-msvc" ]; then
  dest="$INSTALL_DIR/kimcli.exe"
fi

# Install atomically: copying straight over $dest with `cp` truncates the
# existing file in place, which fails ETXTBSY on Linux if kimcli is
# self-upgrading while its own binary is still running, and can SIGKILL a
# signed binary in place on macOS. Instead, stage into a temp file in the
# SAME directory as $dest (so the final `mv` is a same-filesystem rename,
# not a cross-device copy) and apply the usual quarantine/chmod steps to
# the staged file, then `mv` it into place — a rename swaps the directory
# entry atomically instead of writing through the old inode.
tmp_dest="$dest.tmp.$$"
cp "$bin" "$tmp_dest"

if [ "$os" = "darwin" ]; then
  # Unsigned binary: strip the quarantine flag so Gatekeeper doesn't block
  # the first launch. Best-effort — absence of the attribute is not an error.
  xattr -d com.apple.quarantine "$tmp_dest" 2>/dev/null || true
fi

if [ "$target" != "x86_64-pc-windows-msvc" ]; then
  chmod +x "$tmp_dest"
fi

mv "$tmp_dest" "$dest"
tmp_dest=""

case ":$PATH:" in
  *":$INSTALL_DIR:"*) ;;
  *) echo "Add this to your shell profile: export PATH=\"$INSTALL_DIR:\$PATH\"" ;;
esac

echo "kimcli installed at $dest"
echo "Next: export CODEX_BIN=\"$dest\"  # points Kim's Code tab at this binary"
echo "Or run it directly: kimcli / kim tui"
