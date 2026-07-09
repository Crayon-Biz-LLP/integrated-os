from core.skills.backfill_graph import get_or_create_node

def test_backfill_suspicious_concept_routes_to_pending(monkeypatch):
    calls = []
    
    def mock_audit_log_sync(subsystem, level, msg, metadata=None, **kwargs):
        if metadata:
            calls.append((msg, metadata))
        else:
            calls.append(msg)
            
    monkeypatch.setattr("core.skills.backfill_graph.audit_log_sync", mock_audit_log_sync)
    monkeypatch.setattr("core.lib.graph_rules.audit_log_sync", mock_audit_log_sync)

    class MockData:
        def __init__(self, data):
            self.data = data
            
    class MockBuilder:
        def select(self, *args, **kwargs): return self
        def ilike(self, *args, **kwargs): return self
        def eq(self, *args, **kwargs): return self
        def filter(self, *args, **kwargs): return self
        def limit(self, *args, **kwargs): return self
        def maybe_single(self, *args, **kwargs): return self
        def insert(self, data, **kwargs):
            return self
        def update(self, data, **kwargs):
            return self
        def execute(self):
            return MockData([])

    class MockSupabase:
        def table(self, name):
            return MockBuilder()

    monkeypatch.setattr("core.skills.backfill_graph.supabase", MockSupabase())
    monkeypatch.setattr("core.lib.graph_rules.supabase", MockSupabase())

    # We mock out evaluate_node so it doesn't throw errors
    def mock_evaluate_node(*args, **kwargs): pass
    monkeypatch.setattr("core.clarifier.evaluate_node", mock_evaluate_node)

    # Calling with a fused concept-like label
    get_or_create_node("This is an extremely long name that should be flagged", "concept", {}, {}, "memory_123")
    
    # We should see a route=pending or route=discard log, not route=direct
    routing_logs = [m for m in calls if isinstance(m, tuple) and m[0] == "Routing entity candidate"]
    assert len(routing_logs) == 1
    
    route = routing_logs[0][1]["route"]
    assert route in ["pending", "discard"]

