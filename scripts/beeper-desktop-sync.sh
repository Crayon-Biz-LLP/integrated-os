#!/bin/bash
# Beeper Desktop sync — one tick (launchd runs this every 60s while the
# Mac is on). Reads the Desktop API token from .env (BEEPER_DESKTOP_TOKEN,
# falling back to BEEPER_MATRIX_TOKEN which today holds the bdapi_ token).
#
# Install (see infra/com.rhodey.beeper-desktop-sync.plist):
#   launchctl load ~/Library/LaunchAgents/com.rhodey.beeper-desktop-sync.plist
# Run once now:
#   bash scripts/beeper-desktop-sync.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Overlap guard: cold-start ticks can run minutes while launchd fires every
# 300s. A mkdir lock (portable — no flock on macOS) makes concurrent ticks
# exit early instead of double-processing + double-LLM-ing.
LOCK_DIR="${TMPDIR:-/tmp}/rhodey-beeper-desktop-sync.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  # Lock exists: take over ONLY when the holder is dead (stale from a
  # killed tick) — otherwise another tick is running, exit quietly.
  if [ -f "$LOCK_DIR/pid" ] && kill -0 "$(cat "$LOCK_DIR/pid")" 2>/dev/null; then
    exit 0
  fi
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
fi
echo "$$" > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

# Load .env into the environment (export so child python sees the vars).
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# NOTE: no `exec` here — an EXIT trap must run after python finishes to
# release the lock (exec would replace the shell and orphan the lock).
# Prefer the project venv when present (VPS deploys Python 3.11 there;
# the Mac uses system python3).
if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  PY="$REPO_ROOT/.venv/bin/python"
else
  PY=python3
fi
"$PY" -m core.skills.beeper_desktop
