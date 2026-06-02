#!/usr/bin/env bash
# Refresh per-plugin lockfile(s) + registry.json after manifest changes.
#
# After editing ANY plugin.toml, run this script so the lockfiles and the
# registry stay in sync with the manifests. CI enforces this with two
# checks (lockfiles-in-sync + registry-in-sync); running this script
# locally is the way to make them pass.
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
#   2. ``python scripts/build-registry.py``       — rebuild registry.json from manifests
#   3. ``python scripts/gen_registry.py``         — fold suggestion_descriptors into registry
#
# Order matters: build-registry consumes plugin.toml + requirements.lock;
# gen_registry mutates the registry.json that build-registry just wrote.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ "$#" -ge 1 ]; then
  echo "[refresh] re-locking $1..."
  python scripts/lock-deps.py "$1"
else
  echo "[refresh] re-locking all plugins..."
  python scripts/lock-deps.py
fi

echo "[refresh] rebuilding registry.json..."
python scripts/build-registry.py

echo "[refresh] folding suggestion_descriptors..."
python scripts/gen_registry.py

echo "[refresh] done. Stage the updates:"
echo "  git add plugins/*/requirements.lock registry.json"
