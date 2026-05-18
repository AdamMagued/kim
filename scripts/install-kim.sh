#!/usr/bin/env sh
set -eu

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

cp "$bin" "$INSTALL_DIR/kim"
chmod +x "$INSTALL_DIR/kim"

case ":$PATH:" in
  *":$INSTALL_DIR:"*) ;;
  *) echo "Add this to your shell profile: export PATH=\"$INSTALL_DIR:\$PATH\"" ;;
esac

echo "Kim installed at $INSTALL_DIR/kim"
echo "Next: run 'kim', then type /login."
