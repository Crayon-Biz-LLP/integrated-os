"""API contract — pinned surface + OpenAPI spec validity (ops, no aspect).

Two contracts, one file:

1. **Route inventory pin.** The exact (path → methods) surface of the
   FastAPI app, committed here like a golden. Adding OR removing a route
   fails the gate until this pin is updated deliberately (the failure
   message shows the diff). This is what turns an accidental route rename
   (e.g. `/api/tasks` → `/api/tasks2`, which silently breaks the app) into
   a red CI.

2. **OpenAPI spec validity.** FastAPI auto-generates `/openapi.json`; this
   pins the meta (title/version/OpenAPI version) and the hard validity
   rule: **every operationId is unique**. That rule was VIOLATED until
   v2.9: the eight `@app.api_route(path, methods=["GET","POST"])` routes
   produced one operationId per route (FastAPI's generate_unique_id uses
   `list(route.methods)[0]`), so GET and POST shared an id — an invalid
   spec for strict consumers (SDK generators, API tooling). The routes were
   split into explicit `@app.get` + `@app.post` decorators to fix it.

Regenerate the pin (after an intentional route change) with:

    python -c "import sys,warnings;sys.path.insert(0,'.');warnings.filterwarnings('ignore');\
from api.index import app;spec=app.openapi();import json;\
print(json.dumps({p:sorted(o) for p,o in sorted(spec['paths'].items())},indent=2))"

Ops surface — exempt from the aspect-marker lint by design (plan §3).
"""

from api.index import app


PINNED_ROUTES = {
    "/": ["get"],
    "/api/admin/spend": ["get", "post"],
    "/api/aliases": ["delete", "get", "post"],
    "/api/app-version": ["get"],
    "/api/auth/google/exchange": ["post"],
    "/api/auth/google/start": ["get"],
    "/api/auth/otp/send": ["post"],
    "/api/auth/otp/verify": ["post"],
    "/api/auto-decisions": ["get"],
    "/api/auto-decisions/confirm": ["post"],
    "/api/auto-decisions/undo": ["post"],
    "/api/beeper-send": ["post"],
    "/api/beeper-sync": ["get", "post"],
    "/api/briefing": ["get"],
    "/api/calendar-events": ["get"],
    "/api/call-action": ["post"],
    "/api/call-action/batch": ["post"],
    "/api/captures": ["get"],
    "/api/conversation-history": ["get"],
    "/api/decision-pulse": ["get", "post"],
    "/api/decisions/undo": ["post"],
    "/api/demo/cleanup": ["post"],
    "/api/document/confirm": ["post"],
    "/api/demo/message": ["post"],
    "/api/draft-action": ["post"],
    "/api/drive-webhook": ["post"],
    "/api/email-action": ["post"],
    "/api/email-action/batch": ["post"],
    "/api/email-search/sent": ["post"],
    "/api/focal-action": ["post"],
    "/api/fyi-action": ["post"],
    "/api/fyi-action/batch": ["post"],
    "/api/graph-edge-action": ["post"],
    "/api/graph-edge-action/batch": ["post"],
    "/api/graph-edges/similar": ["get"],
    "/api/graph-merge-action": ["post"],
    "/api/graph-node-action": ["post"],
    "/api/graph-node-action/batch": ["post"],
    "/api/graph-node-merge": ["post"],
    "/api/graph-node/{node_id}/enrichment": ["patch"],
    "/api/org-relationship": ["post"],
    "/api/graph-node/{pending_id}": ["delete", "put"],
    "/api/graph-node/{pending_id}/type": ["patch"],
    "/api/graph-nodes/live": ["get"],
    "/api/graph-nodes/search": ["get"],
    "/api/graph-nodes/similar": ["get"],
    "/api/health": ["get", "post"],
    "/api/home-feed": ["get"],
    "/api/home-mode-switch": ["post"],
    "/api/inbox": ["get"],
    "/api/maintenance": ["get", "post"],
    "/api/messages": ["get"],
    "/api/multimodal-input": ["post"],
    "/api/oauth/callback": ["get"],
    "/api/oauth/exchange": ["post"],
    "/api/oauth/start": ["get"],
    "/api/onboarding/complete": ["post"],
    "/api/onboarding/presets": ["get"],
    "/api/onboarding/status": ["get"],
    "/api/pending-graph-edges": ["get"],
    "/api/pending-graph-nodes": ["get"],
    "/api/pending-merges": ["get"],
    "/api/people/{person_id}/tasks": ["get"],
    "/api/persona": ["get"],
    "/api/pulse": ["post"],
    "/api/pulse-cron": ["get", "post"],
    "/api/register-device": ["post"],
    "/api/roundup": ["get", "post"],
    "/api/send-draft": ["post"],
    "/api/send-message": ["post"],
    "/api/sentinel": ["get", "post"],
    "/api/tasks": ["get"],
    "/api/tasks/{task_id}/status": ["patch"],
    "/api/teams-action": ["post"],
    "/api/teams-action/batch": ["post"],
    "/api/vault-action": ["post"],
    "/api/webhook": ["post"],
    "/api/whatsapp-action": ["post"],
    "/api/whatsapp-action/batch": ["post"],
    "/api/whatsapp-ingest": ["post"],
}


def _live_surface(spec):
    return {path: sorted(ops.keys()) for path, ops in spec["paths"].items()}


# ── 1. Route inventory pin ────────────────────────────────────────────────

def test_route_surface_matches_pin():
    """The app's OpenAPI path surface must EXACTLY match the committed pin —
    a route added or removed is an intentional, reviewed change."""
    spec = app.openapi()
    live = _live_surface(spec)
    missing = {p: m for p, m in PINNED_ROUTES.items() if p not in live}
    added = {p: m for p, m in live.items() if p not in PINNED_ROUTES}
    assert not missing, (
        "Route(s) REMOVED from the API — update tests/unit/test_api_contract.py "
        f"PINNED_ROUTES if intentional: {missing}"
    )
    assert not added, (
        "Route(s) ADDED to the API — update tests/unit/test_api_contract.py "
        f"PINNED_ROUTES: {added}"
    )
    # method drift on a pinned path
    method_drift = {
        p: {"pinned": PINNED_ROUTES[p], "live": live[p]}
        for p in PINNED_ROUTES
        if p in live and live[p] != PINNED_ROUTES[p]
    }
    assert not method_drift, f"Method set drifted: {method_drift}"


def test_pin_operation_count_is_stable():
    """Sanity guard so the pin can't silently shrink while paths stay equal."""
    total = sum(len(m) for m in PINNED_ROUTES.values())
    assert total == 91
    assert len(PINNED_ROUTES) == 80


# ── 2. OpenAPI spec validity ──────────────────────────────────────────────

def test_spec_metadata_present():
    spec = app.openapi()
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "Integrated-OS"
    assert spec["info"]["version"]


def test_operation_ids_are_unique():
    """Hard validity rule — was VIOLATED (8 duplicates) until v2.9: the
    GET+POST `api_route` pairs shared one operationId each. Duplicate ids
    break strict OpenAPI consumers."""
    spec = app.openapi()
    ids = [op.get("operationId") for ops in spec["paths"].values() for op in ops.values()]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"Duplicate operationIds — split the multi-method route(s): {dupes}"


def test_every_route_is_documented():
    """Every registered APIRoute (incl. the split GET/POST pairs) appears in
    the spec with its method."""
    spec = app.openapi()
    documented = _live_surface(spec)
    # FastAPI-managed doc routes are framework metadata, not product contract
    framework_routes = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    for route in app.routes:
        if not hasattr(route, "methods"):
            continue  # non-API routes (static mounts, middleware internals)
        path = getattr(route, "path", None)
        if path in framework_routes:
            continue
        assert path in documented, f"Route {path} missing from the OpenAPI spec"
        for method in route.methods:
            assert method.lower() in documented[path], f"{method} {path} missing from spec"
