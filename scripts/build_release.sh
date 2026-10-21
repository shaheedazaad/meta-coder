#!/usr/bin/env bash
# Builds a versioned release bundle (todo.md step 20 / plan.md Architecture):
# a source archive of one git ref, including pixi.lock, that install.sh/
# install.ps1 later download and unpack. Bootstrapping the environment
# (`pixi install`) happens on the end user's machine at install time, not
# here. MkDocs must be installed in PYTHON (defaults to python3).
#
# Usage: scripts/build_release.sh [ref]
#   ref defaults to HEAD. The version number is read from pyproject.toml's
#   [project].version, not derived from the ref name, so it stays correct
#   even when building from a branch rather than a tag.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

ref="${1:-HEAD}"

version="$(git show "$ref:pyproject.toml" | sed -n 's/^version = "\(.*\)"$/\1/p' | head -n1)"
if [ -z "$version" ]; then
  echo "error: could not read [project].version from pyproject.toml at $ref" >&2
  exit 1
fi

if ! git cat-file -e "$ref:pixi.lock" 2>/dev/null; then
  echo "error: $ref has no pixi.lock — run 'pixi install' and commit it before releasing." >&2
  exit 1
fi

mkdir -p dist
bundle_name="meta-coder-${version}"
out_path="dist/${bundle_name}.tar.gz"

# Build documentation from the selected ref, never from unrelated working changes.
build_dir="$(mktemp -d)"
trap 'rm -rf "$build_dir"' EXIT
mkdir -p "$build_dir/$bundle_name"
git archive "$ref" | tar -x -C "$build_dir/$bundle_name"
"${PYTHON:-python3}" -m mkdocs build --strict --config-file "$build_dir/$bundle_name/mkdocs.yml"
tar -czf "$out_path" -C "$build_dir" "$bundle_name"
cp "$build_dir/$bundle_name/scripts/install.sh" dist/install.sh
cp "$build_dir/$bundle_name/scripts/install.ps1" dist/install.ps1
cp "$build_dir/$bundle_name/scripts/uninstall.sh" dist/uninstall.sh
cp "$build_dir/$bundle_name/scripts/uninstall.ps1" dist/uninstall.ps1
printf '%s\n' "$version" > dist/latest.txt
"${PYTHON:-python3}" - "$out_path" <<'CHECKSUM'
import hashlib
from pathlib import Path
import sys
files = [Path(sys.argv[1])] + [Path("dist", name) for name in (
    "install.sh", "install.ps1", "uninstall.sh", "uninstall.ps1", "latest.txt")]
Path("dist/SHA256SUMS").write_text("".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in files))
CHECKSUM

echo "Built $out_path (version $version, from $ref)"
