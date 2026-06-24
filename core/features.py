import os

def is_org_routing_enabled() -> bool:
    """
    Feature flag for the new organization-aware project routing.
    Set ORG_ROUTING_ENABLED=1 in environment to enable Phase 3 routing.
    """
    return os.getenv("ORG_ROUTING_ENABLED", "0").lower() in ("1", "true", "yes")
