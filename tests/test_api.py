from fastapi.testclient import TestClient
from api.index import app

client = TestClient(app)

def test_health_check():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "Integrated OS API is running on Python 🐍"}
