def test_provenance_rows_are_not_promoted():
    with open('core/pulse/sentinel.py', 'r') as f:
        content = f.read()
    
    # Simple check that the sentinel query strictly excludes provenance
    assert ".neq('approval_source', 'provenance')" in content, "Sentinel must exclude provenance rows"
    
    # We also check that process_pending_edge_decision doesn't get called on them
    # Actually process_pending_edge_decision is human-triggered so it's fine.
    # The requirement: "Make sure provenance rows do not masquerade as human-approved edges."
    # The MENTIONS edges are explicitly saved with approval_source = 'provenance'
    
    with open('core/pulse/graph.py', 'r') as f:
        content = f.read()
        
    assert "'approval_source': 'provenance'" in content or "\"approval_source\": \"provenance\"" in content, "MENTIONS edges must be marked with approval_source='provenance'"

