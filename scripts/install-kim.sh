#!/usr/bin/env sh
set -eu

# Override KIM_RELEASE_REPO to point at a different fork (#66).
REPO="${KIM_RELEASE_REPO:-AdamMagued/kim}"
VERSION="${KIM_VERSION:-latest}"
INSTALL_DIR="${KIM_INSTALL_DIR:-$HOME/.kim/bin}"

os="$(uname -s | tr '[:upper:]' '[:lower:]')"
arch="$(uname -m)"

case "$os" in
  darwin) platform="apple-darwin" ;;
  linux) platform="unknown-linux-gnu" ;;
  mingw*|msys*|cygwin*) platform="pc-windows-msvc" ;;
  *) echo "Unsupported OS: $os" >&2; exit 1 ;;
esac

case "$arch" in
  arm64|aarch64) cpu="aarch64" ;;
  x86_64|amd64) cpu="x86_64" ;;
  *) echo "Unsupported architecture: $arch" >&2; exit 1 ;;
esac

asset="kim-${cpu}-${platform}.tar.gz"
if [ "$platform" = "pc-windows-msvc" ]; then
  asset="kim-${cpu}-${platform}.zip"
fi

mkdir -p "$INSTALL_DIR"
tmp="$(mktemp -d)"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT INT TERM

if [ "$VERSION" = "latest" ]; then
  url="https://github.com/${REPO}/releases/latest/download/${asset}"
else
  url="https://github.com/${REPO}/releases/download/${VERSION}/${asset}"
fi

echo "Downloading Kim CLI: $url"
if [ -n "${GITHUB_TOKEN:-}" ]; then
  curl -fL -H "Authorization: Bearer ${GITHUB_TOKEN}" "$url" -o "$tmp/$asset"
else
  curl -fL "$url" -o "$tmp/$asset"
fi

# Checksum verification (#57): download and verify SHA256SUMS if available.
# Set KIM_SKIP_CHECKSUM=1 to bypass (not recommended).
if [ "${KIM_SKIP_CHECKSUM:-0}" != "1" ]; then
  sha_url="${url%.tar.gz}.sha256"
  sha_url="${sha_url%.zip}.sha256"
  if curl -fsSL "$sha_url" -o "$tmp/SHA256SUMS" 2>/dev/null; then
    expected=$(grep "$asset" "$tmp/SHA256SUMS" | awk '{print $1}')
    if [ -n "$expected" ]; then
      if command -v sha256sum >/dev/null 2>&1; then
        actual=$(sha256sum "$tmp/$asset" | awk '{print $1}')
      elif command -v shasum >/dev/null 2>&1; then
        actual=$(shasum -a 256 "$tmp/$asset" | awk '{print $1}')
      else
        echo "WARNING: no sha256sum/shasum found; skipping checksum verification" >&2
        actual="$expected"
      fi
      if [ "$actual" != "$expected" ]; then
        echo "ERROR: checksum mismatch (expected $expected, got $actual). Aborting." >&2
        exit 1
      fi
      echo "Checksum verified: $expected"
    else
      echo "WARNING: no checksum entry found for $asset in SHA256SUMS" >&2
    fi
  else
    echo "WARNING: SHA256SUMS not available for this release; skipping verification" >&2
  fi
fi

case "$asset" in
  *.zip) unzip -q "$tmp/$asset" -d "$tmp/out" ;;
  *.tar.gz) mkdir -p "$tmp/out"; tar -xzf "$tmp/$asset" -C "$tmp/out" ;;
esac

bin="$tmp/out/kim"
[ -f "$bin.exe" ] && bin="$bin.exe"
if [ ! -f "$bin" ]; then
  echo "Release asset did not contain kim binary." >&2
  exit 1
fi

dest="$INSTALL_DIR/kim"
if [ "$platform" = "pc-windows-msvc" ]; then
  dest="$INSTALL_DIR/kim.exe"
fi

cp "$bin" "$dest"
if [ "$platform" != "pc-windows-msvc" ]; then
  chmod +x "$dest"
fi

case ":$PATH:" in
  *":$INSTALL_DIR:"*) ;;
  *) echo "Add this to your shell profile: export PATH=\"$INSTALL_DIR:\$PATH\"" ;;
esac

echo "Kim installed at $dest"
echo "Next: run 'kim', then type /login."
