#!/bin/bash
# Export codebase to Notebook LM via Google Docs (Drive + Docs API).
# Called by CI workflow (.github/workflows/notebooklm-sync.yml) on push to main.
# Can also be run locally for testing.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
START=$(date +%s)

echo "=== Notebook LM Export ==="
echo "Repo: $REPO_ROOT"
echo ""

# Step 1: Generate .md bundles from git-tracked files
python3 "$REPO_ROOT/scripts/generate_notebooklm_sources.py"

echo ""

# Step 2: Sync to Google Docs (Drive API create + Docs API update)
echo "Syncing to Google Docs..."
python3 "$REPO_ROOT/scripts/sync_notebooklm_docs.py"

ELAPSED=$(($(date +%s) - START))
echo ""
echo "=== Done in ${ELAPSED}s ==="
echo "Google Docs updated in drive://NotebookLM Codebase Sources/"
echo "Notebook LM auto-syncs these sources within minutes."
