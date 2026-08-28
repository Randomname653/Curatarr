import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.main import app
from src.routers.auth import get_current_user
from src.database.models import User

# Dummy user for dependency override
mock_user = User(id=1, plex_username="test_user", plex_user_id="123", is_active=True)

def override_get_current_user():
    return mock_user

def test_get_tasks_empty():
    app.dependency_overrides[get_current_user] = override_get_current_user
    client = TestClient(app)

    with patch("src.routers.tasks.task_monitor") as mock_monitor:
        mock_monitor.get_all.return_value = []

        response = client.get("/api/tasks/")

        assert response.status_code == 200
        assert response.json() == {"tasks": []}
        mock_monitor.get_all.assert_called_once()

    app.dependency_overrides.clear()

def test_get_tasks_populated():
    app.dependency_overrides[get_current_user] = override_get_current_user
    client = TestClient(app)

    with patch("src.routers.tasks.task_monitor") as mock_monitor:
        mock_tasks = [
            {"id": "task_1", "name": "Sync", "status": "running"},
            {"id": "task_2", "name": "Enrich", "status": "done"}
        ]
        mock_monitor.get_all.return_value = mock_tasks

        response = client.get("/api/tasks/")

        assert response.status_code == 200
        assert response.json() == {"tasks": mock_tasks}
        mock_monitor.get_all.assert_called_once()

    app.dependency_overrides.clear()

def test_get_running_empty():
    app.dependency_overrides[get_current_user] = override_get_current_user
    client = TestClient(app)

    with patch("src.routers.tasks.task_monitor") as mock_monitor:
        mock_monitor.get_running.return_value = []

        response = client.get("/api/tasks/running")

        assert response.status_code == 200
        assert response.json() == {"tasks": []}
        mock_monitor.get_running.assert_called_once()

    app.dependency_overrides.clear()

def test_get_running_populated():
    app.dependency_overrides[get_current_user] = override_get_current_user
    client = TestClient(app)

    with patch("src.routers.tasks.task_monitor") as mock_monitor:
        mock_running = [
            {"id": "task_1", "name": "Sync", "status": "running"}
        ]
        mock_monitor.get_running.return_value = mock_running

        response = client.get("/api/tasks/running")

        assert response.status_code == 200
        assert response.json() == {"tasks": mock_running}
        mock_monitor.get_running.assert_called_once()

    app.dependency_overrides.clear()

def test_get_task_history():
    app.dependency_overrides[get_current_user] = override_get_current_user
    client = TestClient(app)

    with patch("src.routers.tasks.task_monitor") as mock_monitor:
        mock_history = {
            "sync": {"id": "task_0", "status": "done"}
        }
        mock_monitor.last_runs = mock_history

        response = client.get("/api/tasks/history")

        assert response.status_code == 200
        assert response.json() == {"last_runs": mock_history}

    app.dependency_overrides.clear()
