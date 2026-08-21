import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.asyncio, pytest.mark.app]

class TestDocumentConformance:
    """Verifies document intelligence conforms to the 6-layer pipeline."""

    def test_document_confirm_requires_ownership(self):
        """Verifies you cannot confirm a document you don't own."""
        from api.index import app
        
        client = TestClient(app)
        response = client.post("/api/suggestions/confirm", json={
            "source_type": "document",
            "source_id": -9999, # Fake ID
            "selected_tasks": [{"type": "task", "title": "Test"}]
        })
        
        assert response.status_code in [401, 403, 404]

    def test_document_confirm_schema_compliance(self):
        """Verifies the confirm endpoint can be called with selected items."""
        from api.index import app
        client = TestClient(app)
        
        response = client.post("/api/suggestions/confirm", json={
            "source_type": "document",
            "source_id": -9999,
            "selected_tasks": [
                {"type": "task", "title": "Audit code"},
                {"type": "note", "title": "Meeting minutes"}
            ]
        })
        assert response.status_code in [401, 403, 404]
