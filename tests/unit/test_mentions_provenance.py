def test_provenance_rows_are_not_promoted():
    # This is a static code check test to verify the sentinel sweep correctly excludes provenance
    with open('core/pulse/sentinel.py', 'r') as f:
        content = f.read()
    
    assert ".neq('approval_source', 'provenance')" in content, "Sentinel must exclude provenance rows"
    
    with open('core/pulse/graph.py', 'r') as f:
        content = f.read()
        
    assert '"approval_source": "provenance"' in content or "'approval_source': 'provenance'" in content, "MENTIONS edges must be marked with approval_source='provenance'"

