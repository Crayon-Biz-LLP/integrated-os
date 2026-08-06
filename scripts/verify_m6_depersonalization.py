#!/usr/bin/env python3
"""M6 de-personalization gate — verify no Danny-specific hardcodes remain in
runtime code paths (everything is per-tenant config with Danny's values as the
documented legacy fallback), while Danny's own behavior is preserved.

Checks (static + behavioral):
  1. archive_ingest: node typing + edge rules are config-driven; root label
     resolves via user_settings (never a hardcoded name at runtime).
  2. email_ingest: Gmail label from core_config (legacy fallback preserved).
  3. GITHUB dispatch: 3 sites use resolve_github_config() (env → config → fallback).
  4. IST timezones: cluster_discovery + resources use get_user_timezone().
  5. Telegram chat id: research_agent + brain_synth use resolve_telegram_chat_id().
  6. graph_rules: OWNS guard compares against the tenant root person, not 'Danny'.
  7. Danny's fallbacks intact: defaults still reproduce his values (M2 gate + here).
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILURES = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "PASS" if ok else "FAIL"
    print(f"  {'✅' if ok else '❌'} [{tag}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    print("M6 de-personalization verification\n")

    # ── 1. archive_ingest ──
    import core.skills.archive_ingest as ai
    src = inspect.getsource(ai)
    check("archive_ingest: node typing config-driven",
          "person_labels()" in src and "org_labels()" in src and "archive_person_labels" in src)
    check("archive_ingest: edge rules config-driven",
          "edge_rules()" in src and "archive_edge_rules" in src)
    check("archive_ingest: entity mappings config-driven at call time",
          "get_entity_mappings()" in inspect.getsource(ai.graphify)
          and "ENTITY_MAPPINGS = get_entity_mappings()" not in src
          and "entity_mappings" in src)
    check("archive_ingest: entity fallback is the RICH mapping (not degraded)",
          "wife's" in str(ai.DEFAULT_ENTITY_MAPPINGS)
          and "production team" in str(ai.DEFAULT_ENTITY_MAPPINGS)
          and "Jeremy" in str(ai.DEFAULT_ENTITY_MAPPINGS)
          and "2.0" in str(ai.DEFAULT_ENTITY_MAPPINGS))
    check("archive_ingest: root label resolves via settings",
          "resolve_root_label()" in src and "resolve_user_name" in src)
    check("archive_ingest: root label override config is read",
          "archive_root_label" in inspect.getsource(ai.resolve_root_label))
    check("archive_ingest: no hardcoded 'Danny' edge creation at runtime",
          'create_edge("Danny"' not in src and "create_edge(\"Danny\"" not in src)
    # Danny fallback preserved (in the DEFAULT_* constants — single source of truth)
    check("archive_ingest: Danny fallback values preserved",
          "Sunju" in str(ai.DEFAULT_ARCHIVE_EDGE_RULES)
          and "Danny" in ai.DEFAULT_ARCHIVE_PERSON_LABELS
          and "Solvstrat" in ai.DEFAULT_ARCHIVE_ORG_LABELS)
    check("archive_ingest: fallback functions use the DEFAULT_* constants",
          "DEFAULT_ARCHIVE_PERSON_LABELS" in inspect.getsource(ai.person_labels)
          and "DEFAULT_ARCHIVE_EDGE_RULES" in inspect.getsource(ai.edge_rules))
    check("archive_ingest: sheet timestamps parsed in tenant tz",
          "get_user_timezone" in inspect.getsource(ai.parse_timestamp) and "timedelta(hours=5, minutes=30)" not in inspect.getsource(ai.parse_timestamp))

    # ── 2. email_ingest Gmail label ──
    import core.skills.email_ingest as ei
    eisrc = inspect.getsource(ei)
    check("email_ingest: Gmail label from core_config",
          "email_archive_label" in eisrc and "Completed/Ashraya" in eisrc)
    check("email_ingest: label fallback preserved",
          ei.DEFAULT_EMAIL_ARCHIVE_LABEL == "Completed/Ashraya")

    # ── 3. GITHUB dispatch sites ──
    import core.lib.constants as const
    csrc = inspect.getsource(const)
    check("constants: resolve_github_config exists",
          "def resolve_github_config" in csrc and "github_owner" in csrc)
    import api.index as idx
    import core.webhook.commands as cmds
    import core.webhook.utils as wu
    check("api/index: uses resolve_github_config",
          "resolve_github_config" in inspect.getsource(idx))
    check("commands: uses resolve_github_config",
          "resolve_github_config" in inspect.getsource(cmds))
    check("webhook/utils: uses resolve_github_config",
          "resolve_github_config" in inspect.getsource(wu))

    # ── 4. timezones ──
    import core.pulse.cluster_discovery as cd
    import core.pulse.resources as res
    check("cluster_discovery: uses get_user_timezone",
          "get_user_timezone" in inspect.getsource(cd))
    check("resources: uses get_user_timezone",
          "get_user_timezone" in inspect.getsource(res))

    # ── 5. telegram chat id ──
    import core.agents.research_agent as ra
    import core.skills.brain_synth_v2 as bs
    check("research_agent: uses resolve_telegram_chat_id",
          "resolve_telegram_chat_id" in inspect.getsource(ra))
    check("brain_synth: uses resolve_telegram_chat_id",
          "resolve_telegram_chat_id" in inspect.getsource(bs))

    # ── 6. graph_rules OWNS guard ──
    import core.lib.graph_rules as gr
    grsrc = inspect.getsource(gr)
    check("graph_rules: OWNS guard uses root person label",
          "_root_person_label" in grsrc and "rel == 'OWNS' and source_label != _root_person_label()" in grsrc)
    check("graph_rules: root fallback is 'Danny' (legacy preserved)",
          'return "Danny"  # legacy / tenant #1 fallback' in grsrc)

    # ── 7. seeding: new tenants are born neutral, never Danny's world ──
    import scripts.seed_user_world as sw
    import scripts.bootstrap_tenant as bt
    check("seed_user_world: writes M6 core_config rows",
          "archive_person_labels" in inspect.getsource(sw)
          and "archive_edge_rules" in inspect.getsource(sw)
          and "email_archive_label" in inspect.getsource(sw))
    check("seed_user_world: neutral edge rules for new tenants",
          "archive_edge_rules\", \"content\": \"[]\"" in inspect.getsource(sw))
    check("bootstrap_tenant: neutral core_config rows on create",
          "archive_person_labels" in inspect.getsource(bt)
          and "on conflict (owner_id, key) do nothing" in inspect.getsource(bt))
    check("bootstrap_tenant: neutral rows ONLY for new tenants (Danny-safe)",
          "existing_uid" in inspect.getsource(bt)
          and "created_now" in inspect.getsource(bt)
          and "existing tenant — M6 rows left untouched" in inspect.getsource(bt))

    # ── 8. tenant #1 seed: Danny's values live in config, not fallback ──
    import scripts.seed_tenant1_m6_config as s1
    s1src = inspect.getsource(s1)
    check("seed_tenant1: script exists + writes all 8 M6 keys",
          all(k in s1src for k in ["email_archive_label", "archive_person_labels",
                                   "archive_org_labels", "archive_edge_rules",
                                   "archive_root_label", "entity_mappings",
                                   "github_owner", "github_repo"]))
    check("seed_tenant1: values imported from code defaults (no drift)",
          "DEFAULT_ARCHIVE_PERSON_LABELS" in s1src
          and "DEFAULT_ARCHIVE_EDGE_RULES" in s1src
          and "DEFAULT_ENTITY_MAPPINGS" in s1src
          and "DEFAULT_EMAIL_ARCHIVE_LABEL" in s1src
          and "DEFAULT_GITHUB_OWNER" in s1src)
    check("seed_tenant1: idempotent upsert (on conflict do update)",
          "on conflict (owner_id, key) do update" in s1src)
    check("seed_tenant1: guarded on db/78 (owner_id + unique key)",
          "core_config.owner_id missing" in s1src
          and "unique (owner_id, key) missing" in s1src)
    check("seed_tenant1: default values are Danny's canonical ones",
          const.DEFAULT_GITHUB_OWNER == "Crayon-Biz-LLP"
          and const.DEFAULT_GITHUB_REPO == "integrated-os"
          and ei.DEFAULT_EMAIL_ARCHIVE_LABEL == "Completed/Ashraya"
          and ai.DEFAULT_ARCHIVE_ROOT_LABEL == "Danny"
          and ai.DEFAULT_ENTITY_MAPPINGS.get("Sunju") == ["sunju", "wife", "wife's", "sunju's"])
    check("migrate script: points to the seed as next step",
          "seed_tenant1_m6_config.py" in inspect.getsource(
              __import__("scripts.migrate_danny_to_tenant1", fromlist=["x"])))

    print()
    if FAILURES:
        print(f"M6 GATE FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("✅ ALL M6 DE-PERSONALIZATION GATES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
