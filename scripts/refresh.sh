#!/usr/bin/env bash
# Refresh per-plugin lockfile(s), registry, and immutable version history.
#
# After editing a plugin package, bump its version and stage the complete
# package change before running this script. Generated lockfiles are staged
# automatically before package identity is calculated.
#
# Usage:
#   bash scripts/refresh.sh                  # re-lock all plugins + regen
#   bash scripts/refresh.sh <plugin_dir>     # re-lock one plugin + regen
#
# Example:
#   bash scripts/refresh.sh weixin
#
# What it does (in order):
#   1. ``python scripts/lock-deps.py [plugin]``  — regenerate requirements.lock
#   2. ``python scripts/build-registry.py``       — rebuild registry + immutable
#                                                    package-version history
#
# Order matters: build-registry hashes the complete tracked plugin package,
# including plugin.toml + requirements.lock.
set -euo pipefail

cd "$(dirname "$0")/.."

# Package identity is defined from the staged Git index. Stage only the generated
# lock outputs here so a newly created lock participates in the same snapshot.
if [ "$#" -ge 1 ]; then
  echo "[refresh] re-locking $1..."
  python scripts/lock-deps.py "$1"
  lockfile="plugins/$1/requirements.lock"
  if [ -e "$lockfile" ] \
      || git ls-files --error-unmatch -- "$lockfile" >/dev/null 2>&1; then
    git add -A -- ":(top,literal)$lockfile"
  fi
else
  echo "[refresh] re-locking all plugins..."
  python scripts/lock-deps.py
  git add -A -- ':(glob)plugins/*/requirements.lock'
fi

echo "[refresh] rebuilding registry.json..."
python scripts/build-registry.py

echo "[refresh] done. Stage the updates:"
echo "  git add registry.json version-history.json"
