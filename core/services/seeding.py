"""seeding.py — M5/M8 tenant world seeding.

``seed_world()`` turns a short structured "world" description into the
tenant's initial knowledge graph + settings, using the same tenant-scoped
primitives the product uses at runtime (create_graph_node_with_db_record /
create_task_direct / user_settings upsert). It is the engine behind BOTH:

  - the admin CLI  (scripts/seed_user_world.py, which re-exports this)
  - the in-app onboarding journey (core/services/onboarding.py →
    POST /api/onboarding/complete)

It runs under the caller's tenant scope and is fail-open per section — a bad
row never aborts the whole seed.

World dict shape (seed_world):
{
  "context": "Priya, COO at Acme, Bengaluru.",
  "timezone": "Asia/Kolkata",
  "domains": [
    {"name": "Acme", "keywords": ["acme", "client", "delivery"]},
    {"name": "Personal", "keywords": ["home", "family", "bills"]}
  ],
  "personal_orgs": ["Personal"],
  "root_label": "Priya",              # optional — their 'me' node label
  "email_archive_label": "",          # optional — Gmail label; '' = INBOX only
  "github_owner": "",                 # optional — Actions dispatch target
  "github_repo": "",                  # optional
  "people": [
    {"name": "Raj", "context": "CTO at Acme"},
    {"name": "Meera", "context": "co-founder"}
  ],
  "organizations": [
    {"name": "Acme", "context": "the company"}
  ],
  "tasks": [
    {"title": "Prep Q3 board deck", "priority": "high",
     "deadline": "2026-08-10T09:00:00+05:30", "organization": "Acme"},
    {"title": "Call Meera about hiring plan", "priority": "important"}
  ]
}
"""

import json


async def seed_world(supabase, uid: str, world: dict) -> dict:
    """Seed a tenant's world (M5). Runs under the caller's tenant scope.

    Returns a summary dict of what was created. Fail-open per section —
    a bad row never aborts the whole seed.
    """
    from core.pulse.graph import create_graph_node_with_db_record
    from core.pulse.tools import create_task_direct

    created = {"people": 0, "organizations": 0, "tasks": 0, "errors": []}

    # ── 1. user_settings (context, user_orgs, timezone) ──
    try:
        from core.lib.time_utils import is_valid_timezone
        timezone = (world.get("timezone") or "").strip()
        if not is_valid_timezone(timezone):
            # Same guard as onboarding's normalize_world: never write a
            # garbage IANA name into user_settings.timezone.
            timezone = "Asia/Kolkata"
        # Write user_orgs (preferred) or fall back to legacy domains+personal_orgs
        user_orgs = world.get("user_orgs") or []
        if not user_orgs and world.get("domains"):
            # Legacy path: convert domains+personal_orgs to user_orgs shape
            personal_orgs_list = world.get("personal_orgs") or []
            user_orgs = [
                {
                    "name": d.get("name", ""),
                    "keywords": d.get("keywords", []),
                    "is_personal": d.get("name", "") in personal_orgs_list,
                }
                for d in world.get("domains", [])
                if d.get("name")
            ]
        settings = {
            "user_id": uid,
            "timezone": timezone,
            "context": world.get("context") or "",
            "user_orgs": json.dumps(user_orgs),
        }
        # M15: role persona — only written when present so a test/old DB
        # without the column (migration 93) is never broken by the upsert.
        if world.get("persona"):
            settings["persona"] = str(world.get("persona")).strip()[:32]
        row = (
            supabase.table("user_settings")
            .upsert(settings, on_conflict="user_id")
            .execute()
        )
        created["settings"] = bool(row.data)
    except Exception as e:
        created["errors"].append(f"settings: {e}")

    # ── 1b. M6 ingest/archive config (per-tenant core_config rows) ──
    # A new tenant gets their OWN archive/ingest config so they never fall
    # back to tenant #1's hardcoded labels/edges. Rows:
    #   archive_person_labels / archive_org_labels — node typing for the
    #       archive ingest (their own people/orgs; empty = generic typing)
    #   archive_edge_rules — custom graph edges from archive text;
    #       seeded empty ([] = neutral, opt-in)
    #   archive_root_label — their own 'me' node label ('' = derive from
    #       user_settings name via resolve_user_name)
    #   email_archive_label — Gmail label to scan past INBOX; '' =
    #       authoritative INBOX-only (the M6 reader treats a present row
    #       as authoritative, empty content = INBOX only)
    #   github_owner / github_repo — Actions dispatch target (optional)
    try:
        people_names = [
            p.get("name", "").strip() for p in (world.get("people") or [])
            if (p.get("name") or "").strip()
        ]
        org_names = [
            o.get("name", "").strip() for o in (world.get("organizations") or [])
            if (o.get("name") or "").strip()
        ]
        root_label = (world.get("root_label") or "").strip()
        # M9.7: the tenant's own briefing schedule (timeslots in THEIR
        # timezone, read by the 30-min pulse heartbeat gate). Default =
        # balanced preset; the onboarding journey may pass briefing_preset.
        from core.services.briefing_schedule import schedule_for_preset
        # M9.3: a new tenant must get the NEUTRAL briefing_sections row — the
        # runtime treats a MISSING row as the tenant-1-era default (Church
        # section + "work, family, and faith" framing), so an app-onboarded
        # tenant without this row would silently inherit tenant-1's briefing.
        from core.services.briefing_sections import neutral_briefing_sections_json
        config_rows = [
            {"key": "archive_person_labels", "content": json.dumps([root_label] + people_names if root_label else people_names)},
            {"key": "archive_org_labels", "content": json.dumps(org_names)},
            {"key": "archive_edge_rules", "content": "[]"},
            {"key": "archive_root_label", "content": root_label},
            {"key": "email_archive_label", "content": (world.get("email_archive_label") or "").strip()},
            {"key": "briefing_sections", "content": neutral_briefing_sections_json()},
            {"key": "briefing_schedule", "content": json.dumps(schedule_for_preset(world.get("briefing_preset")))},
        ]
        if (world.get("github_owner") or "").strip():
            config_rows.append({"key": "github_owner", "content": world["github_owner"].strip()})
        if (world.get("github_repo") or "").strip():
            config_rows.append({"key": "github_repo", "content": world["github_repo"].strip()})
        for row in config_rows:
            supabase.table("core_config").upsert(row, on_conflict="owner_id,key").execute()
        created["config_rows"] = len(config_rows)
    except Exception as e:
        created["errors"].append(f"m6_config: {e}")

    # ── 2. People + organizations (graph nodes via the tenant-scoped path) ──
    for p in world.get("people") or []:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        try:
            res = await create_graph_node_with_db_record(
                label=name,
                node_type="person",
                context=(p.get("context") or "").strip(),
                source_tag="onboarding_seed",
            )
            if res.get("success"):
                created["people"] += 1
            else:
                created["errors"].append(f"person {name}: {res.get('message')}")
        except Exception as e:
            created["errors"].append(f"person {name}: {e}")

    # Build is_personal lookup from user_orgs for tagging graph nodes
    user_orgs_map = {d.get("name"): d.get("is_personal", False) for d in (world.get("user_orgs") or []) if d.get("name")}

    for o in world.get("organizations") or []:
        name = (o.get("name") or "").strip()
        if not name:
            continue
        try:
            res = await create_graph_node_with_db_record(
                label=name,
                node_type="organization",
                context=(o.get("context") or "").strip(),
                source_tag="onboarding_seed",
            )
            if res.get("success"):
                created["organizations"] += 1
                # Tag graph node with is_personal metadata for briefing filter
                is_personal = user_orgs_map.get(name, False)
                if is_personal:
                    try:
                        supabase.table("graph_nodes").update(
                            {"metadata": {"is_personal": True}}
                        ).eq("id", res.get("node_id")).execute()
                    except Exception:
                        pass
            else:
                created["errors"].append(f"org {name}: {res.get('message')}")
        except Exception as e:
            created["errors"].append(f"org {name}: {e}")

    # ── 2b. Personal orgs as graph nodes + user person node + root edges ──
    # Derive personal org names from user_orgs (preferred) or legacy personal_orgs
    user_orgs_data = world.get("user_orgs") or []
    personal_org_names = [
        d.get("name", "") for d in user_orgs_data if d.get("is_personal")
    ]
    if not personal_org_names:
        personal_org_names = world.get("personal_orgs") or []
    root_label = (world.get("root_label") or "").strip()
    # Derive root label from context if not provided
    if not root_label:
        ctx = (world.get("context") or "").strip()
        if ctx:
            root_label = ctx.split(" - ")[0].split(" — ")[0].strip()[:50]
    PERSONAL_ORG_LABELS = {"Personal", "Family"}
    personal_org_ids = {}  # label → node_id
    for org_name in personal_org_names:
        if org_name not in PERSONAL_ORG_LABELS:
            continue
        try:
            # Set is_personal on graph node metadata for briefing filter
            from core.pulse.graph import create_graph_node_with_db_record
            res = await create_graph_node_with_db_record(
                label=org_name,
                node_type="organization",
                context=f"Personal org for {root_label or 'user'}",
                source_tag="onboarding_seed",
            )
            if res.get("success"):
                node_id = res.get("node_id")
                personal_org_ids[org_name] = node_id
                created["organizations"] += 1
                # Tag graph node with is_personal metadata
                try:
                    supabase.table("graph_nodes").update(
                        {"metadata": {"is_personal": True}}
                    ).eq("id", node_id).execute()
                except Exception:
                    pass  # non-critical — briefing falls back to name match
        except Exception as e:
            created["errors"].append(f"personal_org {org_name}: {e}")

    # Create user person node (the root "me" node)
    user_person_id = None
    if root_label:
        try:
            res = await create_graph_node_with_db_record(
                label=root_label,
                node_type="person",
                context="Root person node",
                source_tag="onboarding_seed",
            )
            if res.get("success"):
                user_person_id = res.get("node_id")
                created["people"] += 1
        except Exception as e:
            created["errors"].append(f"user_person {root_label}: {e}")

    # Create WORKS_WITH edges from root person to personal orgs
    if user_person_id:
        from core.lib.graph_rules import insert_pending_edge
        for org_name, org_id in personal_org_ids.items():
            try:
                insert_pending_edge(
                    root_label, org_name, "WORKS_WITH",
                    {"source_text": "onboarding_seed", "source_table": "seed_world"}
                )
            except Exception as e:
                created["errors"].append(f"edge {root_label}→{org_name}: {e}")

    # ── 3. Initial board (tasks via create_task_direct) ──
    for t in world.get("tasks") or []:
        title = (t.get("title") or "").strip()
        if not title:
            continue
        try:
            # Deterministic dedup key (user id + title) so re-running the
            # seed after a partial failure never duplicates tasks.
            dedup_key = f"seed:{uid}:{title.lower().strip()}"[:16]
            res = await create_task_direct(
                title=title,
                organization_name=(t.get("organization") or "").strip() or None,
                priority=(t.get("priority") or "important").lower(),
                deadline=t.get("deadline"),
                notes=f"onboarding_seed: {world.get('context', '')[:200]}",
                dedup_key=dedup_key,
            )
            if res.get("action") in ("created", "skipped"):
                created["tasks"] += 1
            else:
                created["errors"].append(f"task {title}: {res.get('reason')}")
        except Exception as e:
            created["errors"].append(f"task {title}: {e}")

    # ── 4. Onboarding state: seeded ──
    try:
        supabase.table("user_settings").update({"onboarding_state": "seeded"}).eq("user_id", uid).execute()
    except Exception as e:
        created["errors"].append(f"onboarding_state: {e}")

    return created
