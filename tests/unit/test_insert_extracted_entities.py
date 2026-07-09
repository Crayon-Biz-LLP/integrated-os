from core.pulse.graph import insert_extracted_entities

def test_insert_extracted_entities_routes_correctly(monkeypatch):
    calls = []
    
    def mock_audit_log_sync(subsystem, level, msg, metadata=None, **kwargs):
        calls.append({"msg": msg, "metadata": metadata})
        
    monkeypatch.setattr("core.pulse.graph.audit_log_sync", mock_audit_log_sync)
    monkeypatch.setattr("core.lib.graph_rules.audit_log_sync", mock_audit_log_sync)

    class MockData:
        def __init__(self, data):
            self.data = data
            
    class MockBuilder:
        def __init__(self, existing=None):
            self.existing = existing
        def select(self, *args, **kwargs): return self
        def ilike(self, *args, **kwargs): return self
        def eq(self, *args, **kwargs): return self
        def filter(self, *args, **kwargs): return self
        def limit(self, *args, **kwargs): return self
        def maybe_single(self, *args, **kwargs): return self
        def insert(self, data, **kwargs): return self
        def update(self, data, **kwargs): return self
        def execute(self): return MockData(self.existing or [])

    class MockSupabase:
        def table(self, name):
            if name == "graph_nodes":
                return MockBuilder([{"id": "11111111-1111-1111-1111-111111111111", "label": "SourceNode", "type": "task"}])
            return MockBuilder([])

    monkeypatch.setattr("core.pulse.graph.supabase", MockSupabase())
    monkeypatch.setattr("core.lib.graph_rules.supabase", MockSupabase())

    insert_extracted_entities(
        nodes=[{"label": "father, my wife", "type": "person"}],
        edges=[],
        source_id="123",
        source_type="task"
    )
    
    discard_logs = [m for m in calls if m["msg"] == "Routing entity candidate" and m.get("metadata") and m["metadata"].get("route") == "discard"]
    assert len(discard_logs) == 1, f"Expected discard log, got {calls}"


def test_discarded_node_no_downstream_edge(monkeypatch):
    calls = []
    def mock_audit_log_sync(subsystem, level, msg, metadata=None, **kwargs): calls.append(msg)
    monkeypatch.setattr("core.pulse.graph.audit_log_sync", mock_audit_log_sync)
    monkeypatch.setattr("core.lib.graph_rules.audit_log_sync", mock_audit_log_sync)

    class MockData:
        def __init__(self, data): self.data = data
    class MockBuilder:
        def select(self, *args, **kwargs): return self
        def ilike(self, *args, **kwargs): return self
        def eq(self, *args, **kwargs): return self
        def filter(self, *args, **kwargs): return self
        def limit(self, *args, **kwargs): return self
        def maybe_single(self, *args, **kwargs): return self
        def insert(self, data, **kwargs): return self
        def update(self, data, **kwargs): return self
        def execute(self): return MockData([])

    class MockSupabase:
        def table(self, name): return MockBuilder()

    monkeypatch.setattr("core.pulse.graph.supabase", MockSupabase())
    monkeypatch.setattr("core.lib.graph_rules.supabase", MockSupabase())
    
    # Mock insert_pending_edge correctly
    edge_inserts = []
    def mock_insert_pending_edge(*args, **kwargs):
        edge_inserts.append(args)
    monkeypatch.setattr("core.lib.graph_rules.insert_pending_edge", mock_insert_pending_edge)

    insert_extracted_entities(
        nodes=[{"label": "valid concept", "type": "concept"}, {"label": "father, my wife", "type": "person"}],
        edges=[{"source": "valid concept", "target": "father, my wife", "relationship": "RELATES_TO"}],
        source_id="123",
        source_type="task"
    )
    
    # Edge to discarded node should not be created
    assert len(edge_inserts) == 0

