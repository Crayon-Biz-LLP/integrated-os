# Notebook LM Auto-Sync via Google Docs

Replaced rclone `.md` file sync with Google Docs API for Notebook LM integration. Google Docs auto-sync into Notebook LM; plain markdown files don't.

## Pipeline

1. On push to `main`, `.github/workflows/notebooklm-sync.yml` triggers.
2. `scripts/sync_notebooklm_docs.py` reads markdown sources from `scripts/generate_notebooklm_sources.py`, creates or updates Google Docs in a shared Drive folder.
3. Notebook LM's built-in Drive watcher picks up new/changed Docs automatically.

## Files
- `scripts/sync_notebooklm_docs.py` — Google Docs create/update
- `scripts/update_google_oauth.py` — One-time OAuth scope updater (adds `docs` scope)
- `scripts/export-to-notebooklm.sh` — Updated to use new sync script
- `.github/workflows/notebooklm-sync.yml` — CI workflow
- Removed pre-push git hook (replaced by CI)
