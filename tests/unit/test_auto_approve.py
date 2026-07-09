import pytest
from core.pulse.auto_approve import auto_approve_concepts_and_evokes

@pytest.mark.asyncio
async def test_auto_approve_routes_to_canonical_promotion(monkeypatch):
    calls = []
    
    async def mock_process_pending_edge_decision(pending_id, decision, new_source=None, new_target=None, new_rel=None, auto_decided=False):
        calls.append((pending_id, decision, new_rel, auto_decided))
        return {"success": True}
        
    monkeypatch.setattr("core.pulse.auto_approve.process_pending_edge_decision", mock_process_pending_edge_decision)
    
    class MockData:
        def __init__(self, data): self.data = data
        
    class MockBuilder:
        def select(self, *args, **kwargs): return self
        def eq(self, *args, **kwargs): return self
        def or_(self, *args, **kwargs): return self
        def update(self, *args, **kwargs): return self
        def execute(self): 
            # Depending on context we return different data
            return MockData([{"id": 1, "source_label": "Danny", "target_label": "Concept", "status": "pending", "relationship": "EVOKES"}])

    class MockSupabase:
        def table(self, name):
            if name == "graph_nodes":
                class GNM:
                    def select(self, *args, **kwargs): return self
                    def eq(self, *args, **kwargs): return self
                    def execute(self): return MockData([{"id": "uuid-123"}])
                return GNM()
            elif name == "pending_graph_edges":
                class PGEM:
                    def select(self, *args, **kwargs): return self
                    def eq(self, *args, **kwargs): return self
                    def or_(self, *args, **kwargs): return self
                    def execute(self): 
                        return MockData([{"id": 999, "source_label": "Danny", "target_label": "Concept", "relationship": "EVOKES"}])
                return PGEM()
            return MockBuilder()

    monkeypatch.setattr("core.pulse.auto_approve.get_supabase", lambda: MockSupabase())

    # Mock maybe_single_safe
    def mock_mss(builder):
        return MockData(builder.execute().data[0] if builder.execute().data else None)
    monkeypatch.setattr("core.pulse.auto_approve.maybe_single_safe", mock_mss)
    
    # Mock create_graph_node_with_db_record
    async def mock_cgn(*args, **kwargs): return {"success": True, "node_id": "uuid-456"}
    monkeypatch.setattr("core.pulse.auto_approve.create_graph_node_with_db_record", mock_cgn)

    await auto_approve_concepts_and_evokes("Danny")
    
    assert len(calls) == 1
    assert calls[0][0] == 999
    assert calls[0][1] == 'approve'
    assert calls[0][3] is True # auto_decided

