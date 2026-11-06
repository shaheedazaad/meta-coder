#!/usr/bin/env bash
# MetaCoder installer (macOS/Linux) — todo.md step 20 / plan.md "A1. Shape,
# install, and local-only security". Downloads a versioned release bundle
# (source + pixi.lock, built by build_release.sh), installs its locked
# environment via Pixi, and drops a `meta-coder` launcher on PATH. Re-running
# this script installs the newest version and repoints the launcher — nothing
# needs to be manually removed first.
#
# Usage:
#   curl -fsSL https://github.com/shaheedazaad/meta-coder/releases/latest/download/install.sh | bash
# Configuration (env vars):
#   META_CODER_RELEASE_BASE_URL  Where release bundles + latest.txt live.
#                                 Optional override for mirrors/testing.
#   META_CODER_VERSION           Install this exact version instead of latest.
set -euo pipefail

app_name="MetaCoder"
app_slug="meta-coder"
data_dir_name="Meta-Coder"

# --- 1. Public GitHub releases (or an explicitly configured mirror) --------
base_url="${META_CODER_RELEASE_BASE_URL:-https://github.com/shaheedazaad/meta-coder/releases/latest/download}"
base_url="${base_url%/}"
download_asset() {
  local name="$1" output="$2" download_base="$base_url"
  if [ -z "${META_CODER_RELEASE_BASE_URL:-}" ] && [ -n "${version:-}" ]; then
    download_base="https://github.com/shaheedazaad/meta-coder/releases/download/v$version"
  fi
  curl -fsSL "$download_base/$name" -o "$output"
}

# --- 2. Which version? ---------------------------------------------------
version="${META_CODER_VERSION:-}"
if [ -z "$version" ]; then
  version="$(download_asset latest.txt -)" || {
    echo "error: could not resolve the latest release. Check your connection and whether a stable release has been published." >&2
    exit 1
  }
fi
version="$(printf '%s' "$version" | tr -d '[:space:]')"
if ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "error: expected a stable version like 0.1.0." >&2
  exit 1
fi
echo "Installing $app_name $version..."

# --- 3. Where does it go? (mirrors meta_coder/paths.py's app_data_dir) --
if [ "$(uname -s)" = "Darwin" ]; then
  data_root="$HOME/Library/Application Support/$data_dir_name"
else
  data_root="${XDG_DATA_HOME:-$HOME/.local/share}/$app_slug"
fi
app_root="$data_root/app"
install_dir="$app_root/$version"
bin_dir="$HOME/.local/bin"
launcher="$bin_dir/meta-coder"

# --- 4. Download + extract -----------------------------------------------
mkdir -p "$install_dir" "$bin_dir"
tmp_tarball="$(mktemp)"
trap 'rm -f "$tmp_tarball"' EXIT
download_asset "meta-coder-$version.tar.gz" "$tmp_tarball" || {
  echo "error: could not download meta-coder-$version.tar.gz" >&2
  exit 1
}
tar -xzf "$tmp_tarball" -C "$install_dir" --strip-components=1

# --- 5. Bootstrap Pixi if this machine doesn't have it yet ---------------
if ! command -v pixi >/dev/null 2>&1; then
  echo "Pixi not found — installing it (see https://pixi.sh)..."
  curl -fsSL https://pixi.sh/install.sh | sh
  # The official installer places pixi here; add it to this script's PATH so
  # the `pixi install` call below can find it without a new shell.
  export PATH="$HOME/.pixi/bin:$PATH"
fi
if ! command -v pixi >/dev/null 2>&1; then
  echo "error: pixi installation did not put 'pixi' on PATH. Open a new shell and re-run." >&2
  exit 1
fi

# --- 6. Materialize the locked environment --------------------------------
# --locked (not --frozen) so a bundle whose pixi.lock doesn't actually match
# its own manifest fails loudly here rather than silently resolving fresh.
pixi install --manifest-path "$install_dir/pyproject.toml" --locked

# --- 7. Launcher shim ------------------------------------------------------
# The marker line lets uninstall.sh recognise launchers it may remove.
cat > "$launcher" <<EOF
#!/usr/bin/env bash
# meta-coder launcher
exec "$(command -v pixi)" run --locked --manifest-path "$install_dir/pyproject.toml" start "\$@"
EOF
chmod +x "$launcher"

# --- 8. Drop stale versions — "re-running the installer updates in place" -
for existing in "$app_root"/*/; do
  existing="${existing%/}"
  if [ "$(basename "$existing")" != "$version" ]; then
    rm -rf "$existing"
  fi
done

echo ""
echo "$app_name $version installed."
if ! command -v meta-coder >/dev/null 2>&1; then
  echo "Add $bin_dir to your PATH (e.g. in ~/.zshrc or ~/.bashrc), then open a new shell."
fi
echo "Run it with: meta-coder"
