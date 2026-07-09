from core.lib.graph_rules import validate_label, TYPE_TO_DANNY_EDGE

def test_validate_label_rejects_phrases():
    # Commas
    res = validate_label("father, my wife")
    assert res["verdict"] == "reject"
    assert "comma" in res["reason"]
    
    # Possessives / relationship markers
    res = validate_label("my wife")
    assert res["verdict"] == "reject"
    
    res = validate_label("his car")
    assert res["verdict"] == "reject"
    
    res = validate_label("Danny's project")
    assert res["verdict"] == "reject"

def test_validate_label_flags_fused():
    hints = {
        "people": {"vignesh sankaran", "danny"},
        "orgs": {"saafe", "crayon"}
    }
    
    # Fused
    res = validate_label("Vignesh Sankaran Saafe", hints=hints)
    assert res["verdict"] == "flag"
    assert "fused" in res["reason"]
    
    # Long phrase without exact match
    res = validate_label("this is a very long name", hints=hints)
    assert res["verdict"] == "flag"
    
    # Known exact match despite being long
    hints["exact_matches"] = {"this is a very long name"}
    res = validate_label("this is a very long name", hints=hints)
    assert res["verdict"] == "pass"

def test_canonical_type_mapping():
    assert TYPE_TO_DANNY_EDGE["organization"] == "WORKS_WITH"
    assert TYPE_TO_DANNY_EDGE["concept"] == "INTERESTED_IN"
    assert TYPE_TO_DANNY_EDGE["project"] == "OWNS"

def test_insert_pending_edge_dedup_ci(monkeypatch):
    from core.lib.graph_rules import insert_pending_edge
    
    # We will mock supabase calls to test the CI dedup behavior
    class MockData:
        def __init__(self, data):
            self.data = data
            
    class MockBuilder:
        def __init__(self, existing=None):
            self.existing = existing
            self.inserted = []
            self.updated = []
        def select(self, *args, **kwargs): return self
        def ilike(self, *args, **kwargs): return self
        def eq(self, *args, **kwargs): return self
        def update(self, data, **kwargs):
            self.updated.append(data)
            return self
        def insert(self, data, **kwargs):
            self.inserted.append(data)
            return self
        def execute(self):
            if hasattr(self, 'updated') and self.updated:
                return MockData([{'id': 1}])
            if hasattr(self, 'inserted') and self.inserted:
                return MockData([{'id': 2}])
            return MockData(self.existing or [])

    mock_db = MockBuilder([{'id': 1, 'source_text': 'old_source'}])
    
    class MockSupabase:
        def table(self, name):
            return mock_db

    monkeypatch.setattr("core.lib.graph_rules.supabase", MockSupabase())
    
    # Test dedup branch
    res = insert_pending_edge("Danny", "Alpha Youth retreat", "ASSOCIATED_WITH", {"source_text": "new_source", "source_type": "person", "target_type": "concept"})
    assert res["status"] == "deduped"
    assert res["id"] == 1
    assert len(mock_db.updated) == 1
    assert mock_db.updated[0]["source_text"] == "old_source, new_source"

