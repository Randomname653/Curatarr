from unittest.mock import patch
from fastapi.testclient import TestClient

from src.main import app
from src.routers.auth import get_current_user
from src.database.models import User

client = TestClient(app)

def test_get_taste_has_context():
    app.dependency_overrides[get_current_user] = lambda: User(id=1, plex_username="testuser", is_active=True)
    with patch("src.routers.history.get_user_taste_context") as mock_get_context:
        mock_get_context.return_value = "User likes sci-fi and action movies."
        response = client.get("/api/history/taste")
        assert response.status_code == 200
        assert response.json() == {"taste_context": "User likes sci-fi and action movies."}
    app.dependency_overrides.clear()

def test_get_taste_empty_context():
    app.dependency_overrides[get_current_user] = lambda: User(id=1, plex_username="testuser", is_active=True)
    with patch("src.routers.history.get_user_taste_context") as mock_get_context:
        mock_get_context.return_value = ""
        response = client.get("/api/history/taste")
        assert response.status_code == 404
        assert response.json()["detail"] == "No taste vector yet. Run /api/history/sync first."
    app.dependency_overrides.clear()

def test_get_taste_no_context():
    app.dependency_overrides[get_current_user] = lambda: User(id=1, plex_username="testuser", is_active=True)
    with patch("src.routers.history.get_user_taste_context") as mock_get_context:
        mock_get_context.return_value = None
        response = client.get("/api/history/taste")
        assert response.status_code == 404
        assert response.json()["detail"] == "No taste vector yet. Run /api/history/sync first."
    app.dependency_overrides.clear()
