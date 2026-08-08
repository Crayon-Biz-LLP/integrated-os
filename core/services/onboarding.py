"""onboarding.py — in-app onboarding journey (M8).

Backs the three journey endpoints (defined in api/index.py):

  GET  /api/onboarding/status   → new | in_progress | seeded (+ name/google)
  POST /api/onboarding/complete → seeds the tenant's world and returns the
                                  first welcome briefing
  (the Google connect step lives in api/index.py's /api/oauth/* routes)

The journey is a short conversation (key → who you are → people → plate →
life areas → optional Google) whose answers are normalized into the same
"world" dict the admin CLI's seed_user_world.py accepts, then handed to
seed_world(). The welcome briefing is composed deterministically from the
answers — no LLM call, so it is instant and free — and shaped exactly like
the GET /api/briefing payload so the app's existing BriefingResponse UI
renders it with zero model changes.
"""

from datetime import datetime, timezone as _utc
from zoneinfo import ZoneInfo


# ── Status ────────────────────────────────────────────────────

def derive_status(settings_row: dict | None, user_row: dict | None) -> dict:
    """Map the users/user_settings rows onto the journey's state machine.

    'new'          — no settings (or an empty settings row) — journey runs
    'in_progress'  — reserved; the journey is atomic, so this is only a
                     fallback for a settings row that exists but is empty
    'seeded'       — seed_world finished (onboarding_state='seeded') OR the
                     tenant was admin/CLI-seeded outside the app (bootstrap
                     sets context without onboarding_state, e.g. tenant #1) —
                     those users must NOT be pushed through the journey again
    """
    state = (settings_row or {}).get("onboarding_state") or ""
    has_context = bool((settings_row or {}).get("context"))
    if state == "seeded" or has_context:
        status = "seeded"
    elif settings_row:
        status = "in_progress"
    else:
        status = "new"
    return {
        "status": status,
        "name": (user_row or {}).get("name") or "",
        "has_google": bool((user_row or {}).get("google_connected")),
    }


def fetch_status(supabase, uid: str) -> dict:
    """Load the tenant's rows and derive the journey state."""
    settings_row = None
    user_row = None
    try:
        res = (
            supabase.table("user_settings")
            .select("*")
            .eq("user_id", uid)
            .maybe_single()
            .execute()
        )
        if res.data:
            settings_row = dict(res.data)
    except Exception:
        settings_row = None
    try:
        res = (
            supabase.table("users")
            .select("name, google_connected")
            .eq("id", uid)
            .maybe_single()
            .execute()
        )
        if res.data:
            user_row = dict(res.data)
    except Exception:
        user_row = None
    return derive_status(settings_row, user_row)


# ── World normalization ───────────────────────────────────────

def normalize_world(payload: dict, existing_timezone: str | None) -> dict:
    """Map the app's journey payload onto the seed_world 'world' shape.

    The journey UI submits flat-ish answers (people with a role, tasks with
    a priority, domain chips); seed_world expects the CLI world shape.
    """
    people = []
    for p in (payload.get("people") or []):
        name = (p.get("name") or "").strip()
        if not name:
            continue
        role = (p.get("role") or "").strip()
        extra = (p.get("context") or "").strip()
        ctx = " — ".join(x for x in (role, extra) if x).strip()
        people.append({"name": name, "context": ctx})

    organizations = [
        {"name": (o.get("name") or "").strip(),
         "context": (o.get("context") or "").strip()}
        for o in (payload.get("organizations") or [])
        if (o.get("name") or "").strip()
    ]

    domains = []
    for d in (payload.get("domains") or []):
        name = (d.get("name") or "").strip()
        if not name:
            continue
        keywords = d.get("keywords")
        if isinstance(keywords, list):
            keywords = [str(k).strip() for k in keywords if str(k).strip()]
        else:
            keywords = []
        # An area without keywords is a dead label — it can't route anything
        # (routing_rules_text / _resolve_domain match on keywords only). The
        # onboarding chips send no keywords, so default to the area name
        # itself: "Ministry" → keyword "ministry" → messages about ministry
        # route to MINISTRY instead of the catch-all INBOX.
        if not keywords:
            keywords = [name.lower()]
        domains.append({"name": name, "keywords": keywords})

    personal_orgs = payload.get("personal_orgs")
    if not personal_orgs:
        # Derive from the RAW payload — the normalized domain dicts drop
        # non-essential keys like 'kind'.
        personal_orgs = [
            (d.get("name") or "").strip()
            for d in (payload.get("domains") or [])
            if (d.get("name") or "").strip()
            and str(d.get("kind") or "").lower() in ("personal", "life")
        ]

    tasks = []
    for t in (payload.get("tasks") or []):
        title = (t.get("title") or "").strip()
        if not title:
            continue
        task = {
            "title": title,
            "priority": str(t.get("priority") or "important").lower(),
        }
        if t.get("deadline"):
            task["deadline"] = t["deadline"]
        if (t.get("organization") or "").strip():
            task["organization"] = t["organization"].strip()
        tasks.append(task)

    # M9.8: the device timezone must be a REAL IANA name — a garbage string
    # (bad device, tampered payload) would otherwise be stored verbatim in
    # user_settings.timezone and poison every timezone-aware prompt + the
    # briefing gate. Invalid → keep the admin-set value (or the IST default).
    from core.lib.time_utils import is_valid_timezone
    device_tz = (payload.get("timezone") or "").strip()
    timezone = device_tz or existing_timezone or "Asia/Kolkata"
    if device_tz and not is_valid_timezone(timezone):
        timezone = existing_timezone or "Asia/Kolkata"

    # Step 2 is now two mandatory fields (name + write-up). The write-up
    # naturally covers role/designation, so compose the identity context as
    # "Name — write-up". Backward compatible: an old client sending only
    # `context` still works (no name → the raw context is used as-is).
    name = (payload.get("name") or "").strip()
    writeup = (payload.get("context") or "").strip()
    if name:
        context = name + (f" — {writeup}" if writeup else "")
    else:
        context = writeup

    return {
        "context": context,
        # Preserve the admin-set timezone from bootstrap when the journey
        # doesn't ask for one (upsert would otherwise overwrite it with the
        # Asia/Kolkata default). The device timezone can be sent by the app
        # so a fresh tenant's briefing times are in THEIR local time.
        "timezone": timezone,
        "domains": domains,
        "personal_orgs": [str(o).strip() for o in personal_orgs if str(o).strip()],
        "root_label": (payload.get("root_label") or "").strip(),
        # M9.7: briefing schedule preset (classic/balanced/bookends/
        # through_the_day) — seed_world writes the resolved row. Absent →
        # balanced default.
        "briefing_preset": (payload.get("briefing_preset") or "").strip() or None,
        "people": people,
        "organizations": organizations,
        "tasks": tasks,
    }


# ── Journey completion ────────────────────────────────────────

async def run_onboarding(supabase, uid: str, payload: dict) -> dict:
    """Seed the tenant's world from the journey answers.

    Returns the normalized world + the seed summary. The seed itself is
    idempotent (dedup keys, upserts) so a retry after a partial failure is
    safe. Raises nothing — summary['errors'] carries per-section failures.
    """
    from core.services.seeding import seed_world

    existing_timezone = None
    try:
        res = (
            supabase.table("user_settings")
            .select("timezone")
            .eq("user_id", uid)
            .maybe_single()
            .execute()
        )
        if res.data:
            existing_timezone = (res.data or {}).get("timezone")
    except Exception:
        existing_timezone = None

    world = normalize_world(payload, existing_timezone)
    summary = await seed_world(supabase, uid, world)
    return {"world": world, "summary": summary}


# ── First briefing ────────────────────────────────────────────

def _greeting_for(name: str, tz_name: str) -> str:
    """Time-of-day greeting in the tenant's timezone (like the real briefing)."""
    try:
        hour = datetime.now(ZoneInfo(tz_name or "Asia/Kolkata")).hour
    except Exception:
        hour = datetime.now(_utc.utc).hour
    if hour < 12:
        part = "morning"
    elif hour < 17:
        part = "afternoon"
    else:
        part = "evening"
    first = (name or "friend").strip().split()[0] or "friend"
    return f"Good {part}, {first}."


def welcome_briefing(name: str, world: dict, summary: dict) -> dict:
    """Compose the 'first briefing' from the journey's own answers.

    Deterministic (no LLM call → instant + free) and shaped like the real
    /api/briefing payload so the app renders it with BriefingResponse.
    """
    people = [(p.get("name") or "").strip() for p in (world.get("people") or [])]
    orgs = [(o.get("name") or "").strip() for o in (world.get("organizations") or [])]
    domains = [(d.get("name") or "?").strip() for d in (world.get("domains") or [])]
    tasks = [(t.get("title") or "").strip() for t in (world.get("tasks") or [])]

    sections = []
    if people or orgs:
        items = [
            {"icon": "👤", "text": p, "status": "note"} for p in people
        ] + [
            {"icon": "🏢", "text": o, "status": "note"} for o in orgs
        ]
        sections.append({"id": "your_world", "title": "Your world", "items": items})
    if tasks:
        sections.append({
            "id": "your_plate",
            "title": "On your plate",
            "items": [{"icon": "📝", "text": t, "status": "active"} for t in tasks],
        })
    if domains:
        sections.append({
            "id": "your_areas",
            "title": "Areas I'll track",
            "items": [{"icon": "📍", "text": d, "status": "note"} for d in domains],
        })

    if tasks or people or domains:
        voice = ("Here's your world as I see it — the people, the plate, and the "
                 "areas I'll track. We'll sharpen this picture together, day by day.")
    else:
        voice = ("Your world is a blank canvas for now — send me anything and I'll "
                 "start building the picture. We'll sharpen it together.")

    insights = []
    if tasks:
        insights.append(f"{len(tasks)} thing{'s' if len(tasks) != 1 else ''} on your plate")
    if people:
        insights.append(f"{len(people)} person{'s' if len(people) != 1 else ''} in your orbit")
    if domains:
        insights.append(f"{len(domains)} area{'s' if len(domains) != 1 else ''} I'll track")

    summary_errors = (summary or {}).get("errors") or []

    return {
        "greeting": _greeting_for(name, world.get("timezone") or "Asia/Kolkata"),
        "voice_line": voice,
        "context_bar": "Your onboarding is done — this is your world as Rhodey sees it.",
        "home_mode": "proceed",
        "sections": sections,
        "insights": insights,
        "seeded_counts": {
            "people": (summary or {}).get("people", 0),
            "organizations": (summary or {}).get("organizations", 0),
            "tasks": (summary or {}).get("tasks", 0),
        },
        "warnings": summary_errors[:5],
    }
