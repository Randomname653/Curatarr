import sys
from unittest.mock import MagicMock

# Mock missing dependencies
sys.modules['httpx'] = MagicMock()
sys.modules['sqlalchemy'] = MagicMock()
sys.modules['sqlalchemy.orm'] = MagicMock()
sys.modules['src.database.connection'] = MagicMock()
sys.modules['src.database.models'] = MagicMock()
sys.modules['src.config'] = MagicMock()

from datetime import datetime, timedelta
from src.services.proactive_messages import detect_night_owl

def test_detect_night_owl_happy_path():
    now = datetime(2024, 1, 15, 12, 0)
    # Entry at 3 AM within 7 days
    entries = [
        {"title": "Late Night Show", "last_viewed_at": datetime(2024, 1, 14, 3, 0)},
    ]
    result = detect_night_owl(entries, now)
    assert result is not None
    assert result["trigger_type"] == "night_owl"
    assert result["media_title"] == "Late Night Show"
    assert "Up late watching Late Night Show" in result["context"]

def test_detect_night_owl_outside_window_hour():
    now = datetime(2024, 1, 15, 12, 0)
    # Entry at 1 AM (too early, window is 2-5)
    # Entry at 6 AM (too late)
    entries = [
        {"title": "Too Early", "last_viewed_at": datetime(2024, 1, 14, 1, 0)},
        {"title": "Too Late", "last_viewed_at": datetime(2024, 1, 14, 6, 0)},
    ]
    result = detect_night_owl(entries, now)
    assert result is None

def test_detect_night_owl_outside_window_days():
    now = datetime(2024, 1, 15, 12, 0)
    # Entry at 3 AM but 8 days ago
    entries = [
        {"title": "Too Old", "last_viewed_at": datetime(2024, 1, 7, 3, 0)},
    ]
    result = detect_night_owl(entries, now)
    assert result is None

def test_detect_night_owl_edge_hours():
    now = datetime(2024, 1, 15, 12, 0)
    # 2 AM and 5 AM are inclusive in the current logic (2 <= hour <= 5)
    entries_2am = [{"title": "Edge 2AM", "last_viewed_at": datetime(2024, 1, 14, 2, 0)}]
    entries_5am = [{"title": "Edge 5AM", "last_viewed_at": datetime(2024, 1, 14, 5, 0)}]

    assert detect_night_owl(entries_2am, now) is not None
    assert detect_night_owl(entries_5am, now) is not None

def test_detect_night_owl_missing_timestamp():
    now = datetime(2024, 1, 15, 12, 0)
    entries = [{"title": "No Time"}] # missing last_viewed_at
    result = detect_night_owl(entries, now)
    assert result is None
