import datetime
from fastapi.testclient import TestClient
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.main import app
from src.database.connection import get_db
from src.routers.auth import get_current_user
from src.database.models import User, WatchHistoryEntry

class MockQuery:
    def __init__(self, data):
        self.data = data

    def filter(self, *expressions):
        filtered_data = self.data
        for expr in expressions:
            expr_str = str(expr)
            if "watch_history_media_type = " in expr_str or "watch_history.media_type = " in expr_str:
                if hasattr(expr, 'right'):
                    val = expr.right.value
                    filtered_data = [d for d in filtered_data if getattr(d, 'media_type') == val]
        return MockQuery(filtered_data)

    def order_by(self, *args):
        return self

    def offset(self, val):
        self.data = self.data[val:]
        return self

    def limit(self, val):
        self.data = self.data[:val]
        return self

    def all(self):
        return self.data


class MockSession:
    def __init__(self, data=None):
        self.data = data or []

    def query(self, model):
        return MockQuery(self.data)


def get_mock_user():
    return User(id=1, plex_username="test_user")


def get_mock_db_with_entries():
    now = datetime.datetime.now(datetime.timezone.utc)
    entries = [
        WatchHistoryEntry(
            id=1,
            user_id=1,
            title="The Matrix",
            media_type="movie",
            viewed_at=now,
            completed=True,
            genres="Action",
            season=None,
            episode=None,
            series_title=None
        ),
        WatchHistoryEntry(
            id=2,
            user_id=1,
            title="Breaking Bad",
            media_type="show",
            viewed_at=now,
            completed=True,
            genres="Drama",
            season=1,
            episode=1,
            series_title="Breaking Bad"
        ),
        WatchHistoryEntry(
            id=3,
            user_id=1,
            title="Inception",
            media_type="movie",
            viewed_at=now,
            completed=False,
            genres="Sci-Fi",
            season=None,
            episode=None,
            series_title=None
        )
    ]
    return MockSession(entries)


client = TestClient(app)

def test_recent_history_default():
    app.dependency_overrides[get_current_user] = get_mock_user
    app.dependency_overrides[get_db] = get_mock_db_with_entries

    try:
        response = client.get("/api/history/recent")
        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 100
        assert data["offset"] == 0
        assert len(data["entries"]) == 3
        assert data["entries"][0]["title"] == "The Matrix"
        assert data["entries"][1]["title"] == "Breaking Bad"
        assert data["entries"][2]["title"] == "Inception"
    finally:
        app.dependency_overrides.clear()

def test_recent_history_category_filter():
    app.dependency_overrides[get_current_user] = get_mock_user
    app.dependency_overrides[get_db] = get_mock_db_with_entries

    try:
        response = client.get("/api/history/recent?category=movie")
        assert response.status_code == 200
        data = response.json()
        assert len(data["entries"]) == 2
        assert data["entries"][0]["title"] == "The Matrix"
        assert data["entries"][1]["title"] == "Inception"
    finally:
        app.dependency_overrides.clear()

def test_recent_history_category_all():
    app.dependency_overrides[get_current_user] = get_mock_user
    app.dependency_overrides[get_db] = get_mock_db_with_entries

    try:
        response = client.get("/api/history/recent?category=all")
        assert response.status_code == 200
        data = response.json()
        assert len(data["entries"]) == 3
    finally:
        app.dependency_overrides.clear()

def test_recent_history_pagination():
    app.dependency_overrides[get_current_user] = get_mock_user
    app.dependency_overrides[get_db] = get_mock_db_with_entries

    try:
        response = client.get("/api/history/recent?limit=1&offset=1")
        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 1
        assert data["offset"] == 1
        assert len(data["entries"]) == 1
        assert data["entries"][0]["title"] == "Breaking Bad"
    finally:
        app.dependency_overrides.clear()

def test_recent_history_invalid_limit():
    app.dependency_overrides[get_current_user] = get_mock_user
    app.dependency_overrides[get_db] = get_mock_db_with_entries

    try:
        response = client.get("/api/history/recent?limit=501")
        assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()

def test_recent_history_invalid_offset():
    app.dependency_overrides[get_current_user] = get_mock_user
    app.dependency_overrides[get_db] = get_mock_db_with_entries

    try:
        response = client.get("/api/history/recent?offset=-1")
        assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()
