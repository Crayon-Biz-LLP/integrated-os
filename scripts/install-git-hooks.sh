#!/bin/bash
# Install git hooks for Notebook LM codebase export.
# Run ONCE after cloning the repo.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK_SRC="$REPO_ROOT/scripts/git-hooks/pre-push"
HOOK_DST="$REPO_ROOT/.git/hooks/pre-push"

if [ ! -f "$HOOK_SRC" ]; then
    echo "Error: hook source not found at $HOOK_SRC"
    exit 1
fi

cp "$HOOK_SRC" "$HOOK_DST"
chmod +x "$HOOK_DST"

echo "Installed pre-push hook: $HOOK_DST"
echo ""
echo "The hook will run 'scripts/export-to-notebooklm.sh' before every push,"
echo "generating Notebook LM source files and syncing them to Google Drive."
