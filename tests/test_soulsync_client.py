import asyncio
from unittest.mock import patch

from src.services.soulsync_client import album_info

def test_album_info_empty_args():
    assert asyncio.run(album_info("", "Album")) is None
    assert asyncio.run(album_info("Artist", "")) is None
    assert asyncio.run(album_info(None, None)) is None

@patch("src.services.soulsync_client.artist_info")
def test_album_info_artist_not_found(mock_artist_info):
    mock_artist_info.return_value = None
    assert asyncio.run(album_info("Unknown", "Album")) is None

@patch("src.services.soulsync_client.artist_info")
def test_album_info_artist_no_id(mock_artist_info):
    mock_artist_info.return_value = {"name": "No ID Artist"}
    assert asyncio.run(album_info("No ID Artist", "Album")) is None

@patch("src.services.soulsync_client._get")
@patch("src.services.soulsync_client.artist_info")
def test_album_info_empty_albums_response(mock_artist_info, mock_get):
    mock_artist_info.return_value = {"id": "123"}

    mock_get.return_value = {}
    assert asyncio.run(album_info("Artist", "Album")) is None

    mock_get.return_value = {"albums": []}
    assert asyncio.run(album_info("Artist", "Album")) is None

    mock_get.return_value = {"albums": None}
    assert asyncio.run(album_info("Artist", "Album")) is None

    mock_get.return_value = {"albums": [{"title": "Different Album"}]}
    assert asyncio.run(album_info("Artist", "Album")) is None

@patch("src.services.soulsync_client._get")
@patch("src.services.soulsync_client.artist_info")
def test_album_info_success(mock_artist_info, mock_get):
    mock_artist_info.return_value = {"id": "123"}
    mock_get.return_value = {
        "albums": [
            {
                "id": "abc",
                "title": "Matched Album",
                "genres": ["Rock"],
                "year": "2023"
            }
        ]
    }
    res = asyncio.run(album_info("Artist", "Matched Album"))
    assert res is not None
    assert res["id"] == "abc"
    assert res["title"] == "Matched Album"
    assert res["year"] == "2023"
