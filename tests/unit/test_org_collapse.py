import pytest
from core.lib.entity_context import EntityContext, _collapse_org_duplicates

@pytest.mark.graph
def test_collapse_org_duplicates():
    # Setup test context with duplicates
    ctx = EntityContext()
    ctx.detected_entities = [
        {"type": "organization", "label": "Rhodey OS", "confidence": 0.95},
        {"type": "organization", "label": "Rhodey", "confidence": 0.9},
        {"type": "person", "label": "David Orban", "confidence": 1.0}
    ]
    ctx.pending_org_label = "Rhodey"

    # Run collapse
    _collapse_org_duplicates(ctx)

    # Verify
    orgs = [e for e in ctx.detected_entities if e.get("type") == "organization"]
    assert len(orgs) == 1
    assert orgs[0]["label"] == "Rhodey OS"
    assert orgs[0]["confidence"] == 0.95
    assert ctx.pending_org_label == "Rhodey OS"

@pytest.mark.graph
def test_collapse_org_duplicates_no_suffix():
    # Should not collapse if suffix is not a recognized designator
    ctx = EntityContext()
    ctx.detected_entities = [
        {"type": "organization", "label": "Ashraya Chennai North", "confidence": 0.95},
        {"type": "organization", "label": "Ashraya", "confidence": 0.9},
    ]

    _collapse_org_duplicates(ctx)

    orgs = [e for e in ctx.detected_entities if e.get("type") == "organization"]
    assert len(orgs) == 2  # No collapse because "Chennai North" isn't a known org suffix

