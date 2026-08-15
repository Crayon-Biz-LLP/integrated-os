#!/usr/bin/env python3
"""tag_aspects.py — Phase-1 tagging tool (plans/75 §3/§5).

Adds a module-level `pytestmark = pytest.mark.<aspect>` to every test file,
keyed off the plan's §3 "Primary suites today" column (refined by reading
each file's actual content). One primary aspect per file = exclusive-primary
semantics (`pytest -m pulse` selects everything tagged pulse; coverage counts
per-primary-aspect only).

Ops surfaces (rate limiter, providers/failover) deliberately carry NO primary
aspect (plan §3: "covered by per-layer floors and tag with the layer only").

Run:  python scripts/tag_aspects.py --dry-run   # show what would change
      python scripts/tag_aspects.py              # apply
Idempotent: never double-tags; safe to re-run.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# (rel_path, primary_aspect) — refined from plans/75 §3 by reading file content.
FILE_ASPECT: dict[str, str] = {
    # root
    "tests/test_api.py": "pulse",
    "tests/test_classify_project_update.py": "ingest",
    "tests/test_dispatch_heuristics.py": "ingest",
    "tests/test_memory_wiring.py": "retrieval",
    "tests/test_retrieval.py": "retrieval",
    "tests/test_webhook_utils.py": "webhook",
    # unit
    "tests/unit/test_action_models.py": "decision",
    "tests/unit/test_actions.py": "decision",
    "tests/unit/test_audit_logger.py": "decision",
    "tests/unit/test_auth_provision.py": "auth",
    "tests/unit/test_awaiting_reply.py": "decision",
    "tests/unit/test_backfill_graph.py": "graph",
    "tests/unit/test_batch_concurrency.py": "decision",
    "tests/unit/test_beeper_desktop.py": "ingest",
    "tests/unit/test_beeper_ingest.py": "ingest",
    "tests/unit/test_beeper_send.py": "ingest",
    "tests/unit/test_briefing_refresh.py": "pulse",
    "tests/unit/test_chat_sieve_detector.py": "ingest",
    "tests/unit/test_chunk_enrichment.py": "retrieval",
    "tests/unit/test_context_registry.py": "retrieval",
    "tests/unit/test_curated_people.py": "graph",
    "tests/unit/test_decision_undo.py": "decision",
    "tests/unit/test_email_classify_prompt.py": "email",
    "tests/unit/test_email_learning.py": "email",
    "tests/unit/test_entity_hardening.py": "graph",
    "tests/unit/test_eval_harness.py": "retrieval",
    "tests/unit/test_executor_acks.py": "decision",
    "tests/unit/test_executor_patch.py": "decision",
    "tests/unit/test_graph_pipeline.py": "graph",
    "tests/unit/test_health_fixes.py": "briefing",
    "tests/unit/test_inbox_feed.py": "decision",
    "tests/unit/test_insert_extracted_entities.py": "graph",
    "tests/unit/test_learning_hints.py": "learning",
    "tests/unit/test_mentions_provenance.py": "graph",
    "tests/unit/test_message_voice.py": "briefing",
    "tests/unit/test_neighbor_context.py": "retrieval",
    "tests/unit/test_pattern_extractor.py": "learning",
    "tests/unit/test_persona_api.py": "briefing",
    "tests/unit/test_persona_guard.py": "briefing",
    "tests/unit/test_persona_l3_context.py": "briefing",
    "tests/unit/test_persona_verifier.py": "briefing",
    "tests/unit/test_sentinel_provenance.py": "sentinel",
    "tests/unit/test_suggest_mode.py": "decision",
    "tests/unit/test_teams_ingest.py": "ingest",
    "tests/unit/test_telemetry.py": "learning",
    "tests/unit/test_tenant_scope.py": "auth",
    "tests/unit/test_time_utils.py": "decision",
    "tests/unit/test_tsvector_search.py": "retrieval",
    "tests/unit/test_url_shortcut.py": "ingest",
    "tests/unit/test_user_settings.py": "auth",
    "tests/unit/test_whatsapp_golden.py": "ingest",
    "tests/unit/test_why.py": "decision",
    "tests/unit/test_workflow_clarification.py": "decision",
    # sim
    "tests/sim/test_context_registry.py": "retrieval",
    "tests/sim/test_index_queue.py": "retrieval",
    "tests/sim/test_preflight_context.py": "retrieval",
    "tests/sim/test_simulated_flows.py": "decision",
    "tests/sim/test_suite1_positive.py": "ingest",
    "tests/sim/test_suite2_cognitive.py": "ingest",
    "tests/sim/test_suite3_boundary.py": "ingest",
    "tests/sim/test_suite4_idempotent.py": "ingest",
    "tests/sim/test_suite5_failure.py": "ingest",
    "tests/sim/test_thread_classification.py": "ingest",
    "tests/sim/test_validation_refactor.py": "decision",
    "tests/sim/test_why.py": "decision",
    # clusters
    "tests/clusters/test_completion_misclassify.py": "ingest",
    "tests/clusters/test_cross_system.py": "decision",
    "tests/clusters/test_deletion_cancellation.py": "decision",
    "tests/clusters/test_lineage_integrity.py": "graph",
    "tests/clusters/test_merge_dedup.py": "graph",
    "tests/clusters/test_metadata_priority.py": "graph",
    "tests/clusters/test_note_capture_and_persistent_memory.py": "graph",
    "tests/clusters/test_recurrence.py": "decision",
    "tests/clusters/test_timing_scheduling.py": "pulse",
    "tests/clusters/test_workflows.py": "ingest",
    # tenants
    "tests/tenants/test_cross_tenant_ngrams.py": "auth",
    "tests/tenants/test_db_isolation.py": "auth",
    "tests/tenants/test_settings_fallback.py": "auth",
}

# Ops surfaces — no primary aspect by design (plan §3); exempt from lint.
OPS_EXEMPT = {
    "tests/test_rate_limiter.py",
    "tests/unit/test_providers_shape.py",
}


def _is_docstring(node: ast.AST) -> bool:
    return (isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def _insert_point(tree: ast.Module) -> int:
    """Line to insert after: module docstring + import section (0 if none)."""
    end = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            end = node.end_lineno or node.lineno
        elif _is_docstring(node) and end == 0:
            # docstring comes before imports; start scanning after it
            end = node.end_lineno or node.lineno
        else:
            break
    return end


def _pytest_imported(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Import):
            if any(a.name == "pytest" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "pytest":
                return True
    return False


def tag_file(path: Path, aspect: str) -> bool:
    """Add/refresh the module-level pytestmark. Returns True if changed."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Already tagged with this aspect?
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            return False
    lines = src.splitlines(keepends=True)
    insert_at = _insert_point(tree)
    new_lines: list[str] = []
    if not _pytest_imported(tree):
        new_lines.append("import pytest\n")
    new_lines.append(f"pytestmark = pytest.mark.{aspect}\n\n")
    # insert after the module docstring + imports (both handled by _insert_point)
    lines[insert_at:insert_at] = new_lines
    path.write_text("".join(lines), encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="show changes without writing")
    args = parser.parse_args()

    changed: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    missing: list[tuple[str, str]] = []
    for rel, aspect in sorted(FILE_ASPECT.items()):
        path = ROOT / rel
        if not path.exists():
            missing.append((rel, aspect))
            continue
        src = path.read_text(encoding="utf-8")
        if f"pytestmark = pytest.mark.{aspect}" in src:
            skipped.append((rel, aspect))
            continue
        if args.dry_run:
            changed.append((rel, aspect))
            continue
        if tag_file(path, aspect):
            changed.append((rel, aspect))
        else:
            skipped.append((rel, aspect))

    print(f"{len(changed)} to tag, {len(skipped)} already tagged, {len(missing)} MISSING files")
    for rel, aspect in changed:
        print(f"  {rel:<60} → {aspect}")
    for rel, aspect in missing:
        print(f"  !! MISSING FILE: {rel} (aspect {aspect})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
