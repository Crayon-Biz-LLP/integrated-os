# Session 77: Unified Entity Extraction & Dual-Pipeline Architecture
**Date:** August 21, 2026

## Objective
Fix orphan tasks and fragmented entity extraction by consolidating 5 legacy extraction paths into a single `extract_context_from_source` pipeline. Ensure guaranteed organization linkage for all tasks and memories, with a fallback to the 'Personal' organization. Fix the Suggestion Card UX for active ingestion channels.

## Root Causes Addressed
1. **Fragmented Extraction:** Entity linking ran asynchronously *after* task creation via 5 different `extract_and_link_entities` call sites, leading to race conditions and orphan tasks.
2. **Double Extraction:** The app's Suggestion Card (`extract_suggestions`) and the backend ran independent LLM passes for entities, causing duplicate costs and conflicting resolutions.
3. **Broken Primary UX:** The Suggestion Card for app chat messages never fired because of an unreachable `source == 'app'` check (source is always `'web'`).
4. **State Drift on Confirm:** Confirming a Suggestion Card threw away the card's entity context, forcing the backend to blindly re-extract entities from truncated text.

## What We Built
- **`extract_context_from_source` (Single Source of Truth):**
  - Replaces all legacy entity linking.
  - Runs *synchronously* before task creation.
  - Phase 1: Deterministic (fast n-gram match).
  - Phase 2: LLM org & person extraction.
  - Phase 3: Reconciliation & pending node creation.
  - Phase 4: 'Personal' org fallback.
- **Dual-Pipeline Architecture:**
  - **Passive Channels (WhatsApp/Email/Teams/Calls):** Route directly to `plan_actions` → execute → `extract_context_from_source`.
  - **Active Channels (App Chat & Documents):** Route to `extract_suggestions` (actions only) + `extract_context_from_source` (entities). Yields a Suggestion Card.
- **Smart Threshold:** Cards only show if `new_entities` are found or multiple tasks are extracted. Known entities auto-execute with zero friction.
- **100% WYSIWYG Confirm:** `EntityContext` is persisted in `raw_dumps` metadata. When the user confirms the card, the backend natively merges their UI choices (like `merge_with`) into the `EntityContext` before creating the task.
- **Cleaned Tech Debt:** Deleted the dead `/api/document/confirm` endpoint. Removed redundant `org_hint` from the Flutter app UI and LLM prompts.

## Testing & Verification
- **Fast Tier (Python):** All 62 test cases pass. Residue gate is clean.
- **Flutter Widget Tests:** Pass.
- **Device Emulator (Pixel 8 Pro):** Integration test (`app_flow_test.dart`) confirmed onboarding and API service stability.
- **E2E UAT Script:** Built `uat_e2e_active_ingestion.py` mimicking the Flutter HTTP boundary. Verified the threshold intercepts, suggestion cards generate, metadata persists, and task linking strictly follows the UI payload.
