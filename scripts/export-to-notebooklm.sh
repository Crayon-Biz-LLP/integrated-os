#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_DIR="$REPO_ROOT/notebooklm"
RCLONE="$(which rclone 2>/dev/null || echo "/opt/homebrew/bin/rclone")"
RCLONE_DEST="rhodey-calls:Crayon/Rhodey OS/NotebookLM Sources"
START=$(date +%s)

echo "=== Notebook LM Export ==="
echo "Repo:  $REPO_ROOT"
echo ""

# Step 1: Generate markdown sources
python3 "$REPO_ROOT/scripts/generate_notebooklm_sources.py" \
    --output-dir "$OUTPUT_DIR" \
    --repo-root "$REPO_ROOT"

echo ""

# Step 2: Sync to Google Drive
echo "Syncing to Google Drive ($RCLONE_DEST)..."
"$RCLONE" sync "$OUTPUT_DIR" "$RCLONE_DEST" \
    --progress \
    --checksum \
    --delete-excluded \
    --exclude ".DS_Store"
echo ""

ELAPSED=$(($(date +%s) - START))
echo "=== Done in ${ELAPSED}s ==="
echo "Sync complete. Add the Google Drive folder 'NotebookLM Sources'"
echo "as a source in Notebook LM to query the codebase."
