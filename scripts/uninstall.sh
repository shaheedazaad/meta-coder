#!/usr/bin/env bash
# Uninstall a release installed by install.sh. User data and Pixi are kept.
# Usage:
#   curl -fsSL https://github.com/shaheedazaad/meta-coder/releases/latest/download/uninstall.sh | bash
# or, from a source checkout: bash scripts/uninstall.sh
set -euo pipefail

if [ "$#" -ne 0 ]; then
  echo "Usage: bash scripts/uninstall.sh (preserves user data and Pixi)" >&2
  exit 1
fi

: "${HOME:?HOME must be set}"
if [ "$(uname -s)" = "Darwin" ]; then
  data_root="$HOME/Library/Application Support/Meta-Coder"
else
  data_root="${XDG_DATA_HOME:-$HOME/.local/share}/meta-coder"
fi
app_root="$data_root/app"
launcher="$HOME/.local/bin/meta-coder"

# A launcher is ours if it runs a manifest under $app_root, either behind
# install.sh's marker line or as the `exec <pixi> run ...` line written before
# the marker existed. Anything else (e.g. a pip install) is left alone.
launcher_is_ours() {
  local line marked=false runs_app=false
  while IFS= read -r line || [ -n "$line" ]; do
    [ "$line" = "# meta-coder launcher" ] && marked=true
    if [[ "$line" == *"--manifest-path \"$app_root/"* ]]; then
      [[ "$line" == "exec "*" run "* ]] && return 0
      runs_app=true
    fi
  done < "$launcher"
  [ "$marked" = true ] && [ "$runs_app" = true ]
}

if [ -f "$launcher" ] && launcher_is_ours; then
  rm -f -- "$launcher"
elif [ -e "$launcher" ] || [ -L "$launcher" ]; then
  echo "Keeping launcher not recognized as this release installation: $launcher"
fi
rm -rf -- "$app_root"

echo "MetaCoder release installation removed."
echo "Projects, settings, saved API keys, and Pixi have been preserved."
echo "User data location: $data_root (or your META_CODER_HOME override)."
