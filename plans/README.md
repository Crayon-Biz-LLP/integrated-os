# Plans

Future implementation plans, migration roadmaps, and test plans.

These are **not** product documentation — they describe work that is planned or
was planned. For completed work, see the corresponding session notes
(`session-notes/`). For current product capabilities, see `product-summary/`.

| File | Type | Status |
|------|------|--------|
| [33-meta-cognitive-learning-layer.md](33-meta-cognitive-learning-layer.md) | Implementation plan | **Implemented** — the learning loop shipped (see product-summary decisions/learning-loop + plans/75 §D4) |
| [63-comprehensive-user-testing-plan.md](63-comprehensive-user-testing-plan.md) | Test plan | Complete |
| [67-modal-migration-plan.md](67-modal-migration-plan.md) | Migration plan | **Completed** — see product-summary/14-infrastructure.md and session-notes/67-modal-migration.md |
| [68-asyncpg-rpc-consolidation-plan.md](68-asyncpg-rpc-consolidation-plan.md) | Implementation plan | **Mostly complete** — Phase 2a/2b/2d deployed (Jul 26); Phase 2c residual |
| [69-multi-tenant-product-plan.md](69-multi-tenant-product-plan.md) | Product plan | **Shipped** — M0–M18 (runtime Aug 6, cutover runbook `docs/cutover-runbook.md`; see session-notes/74) |
| [70-per-tenant-prompt-personalization.md](70-per-tenant-prompt-personalization.md) | Implementation plan | **Shipped** — per-tenant persona M15/M18/2B (Aug 8–9) |
| [71-onboarding-demo.md](71-onboarding-demo.md) | Feature plan | **Shipped** — M10 "Try it now" demo (Aug 8) |
| [72-phase2b-personalized-surfaces.md](72-phase2b-personalized-surfaces.md) | Implementation plan | **Shipped** — Phase 2B per-tenant persona on remaining surfaces (Aug 9) |
| [73-clarifier-rework-queue-native-graph-hitel.md](73-clarifier-rework-queue-native-graph-hitel.md) | Implementation plan | **Shipped** — queue-native clarification, question flow retired (migrations 98–99, Aug 14) |
| [74-question-vs-command-guard.md](74-question-vs-command-guard.md) | Implementation plan | **Shipped** — question-vs-command guard (Aug 14) |
| [75-comprehensive-test-plan.md](75-comprehensive-test-plan.md) | Test plan | **Current (v2.16)** — built end-to-end; see `tests/README.md` + `scripts/run_tests.py` |
| [76-docs-rebaseline.md](76-docs-rebaseline.md) | Docs plan | **Current** — the verified doc re-baseline spec being executed |
