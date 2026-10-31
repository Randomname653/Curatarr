"""Setting Curatarr up on Jellyfin (docs/JELLYFIN_DESIGN.md, phase 2).

- the wizard's connection test asks the Jellyfin server itself, says plainly
  when the key is refused or the server is older than 10.9, and never sends
  a saved key to a new address;
- /complete needs the CHOSEN server's address and key, not Plex's, and
  writes MEDIA_SERVER_TYPE / JELLYFIN_URL / JELLYFIN_API_KEY to .env
  (Plex stays the blank default);
- a Jellyfin install counts as configured without Plex settings;
- the library mapping lists the Jellyfin libraries in the Plex sections'
  shape, so the onboarding table and /configure read both.

    python tests/test_jellyfin_setup.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

import httpx  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base  # noqa: E402
Base.metadata.create_all(conn_mod.engine)

from fastapi import BackgroundTasks, HTTPException  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from media_server_fixtures import jellyfin_transport, load  # noqa: E402
from src.config import Settings, settings  # noqa: E402
from src.routers import libraries, setup as st  # noqa: E402
import src.services.media_server as ms  # noqa: E402
import src.services.media_server.jellyfin_adapter as jfa  # noqa: E402
import src.services.setup_wizard as sw  # noqa: E402

from _checks import check  # noqa: E402


URL = "http://192.168.1.50:8096"
_Orig = jfa.JellyfinAdapter
TRANSPORT = {"t": jellyfin_transport()}
jfa.JellyfinAdapter = lambda url, key, **kw: _Orig(url, key, transport=TRANSPORT["t"])

print("\n-- the connection test --")
ok = asyncio.run(sw.test_jellyfin(URL, "jf-key"))
check("a working server and key: its name and version", ok == {"ok": True, "server_name": "nas",
                                                               "version": "10.10.3"})
bad = asyncio.run(sw.test_jellyfin(URL, "nope"))
check("a refused key says where keys live", bad["ok"] is False and "API Keys" in bad["error"])


def old_server(request):
    return httpx.Response(200, json={**load("jellyfin", "System_Info.json"), "Version": "10.8.13"})


TRANSPORT["t"] = httpx.MockTransport(old_server)
old = asyncio.run(sw.test_jellyfin(URL, "jf-key"))
check("10.8 is refused with the version named", old["ok"] is False and "10.8.13" in old["error"]
      and "10.9" in old["error"])
TRANSPORT["t"] = jellyfin_transport()

settings.JELLYFIN_URL = URL
settings.JELLYFIN_API_KEY = SecretStr("jf-key")
try:
    asyncio.run(st.test_connection(st.TestRequest(service="jellyfin", url="http://10.9.9.9:8096"), None))
    leaked = True
except HTTPException as e:
    leaked = e.status_code != 400
check("a blank key with a NEW address is refused - the saved key stays with its server", not leaked)
saved = asyncio.run(st.test_connection(st.TestRequest(service="jellyfin", url=URL), None))
check("a blank key with the saved address uses the saved key", saved["ok"] is True)

print("\n-- /complete --")
calls = {}
real_write, real_build = st.write_env, st.build_ollama_models
st.write_env = lambda cfg: calls.setdefault("cfg", cfg)


async def _no_build(*a, **kw):
    return {}


st.build_ollama_models = _no_build


def complete(**kw):
    try:
        asyncio.run(st.complete_setup(st.SetupCompleteRequest(**kw), BackgroundTasks(), None))
        return 200
    except HTTPException as e:
        return e.status_code


check("Jellyfin chosen: its URL and key are required, Plex's are not",
      complete(media_server_type="jellyfin", jellyfin_url=URL) == 422
      and complete(media_server_type="jellyfin", jellyfin_url=URL, jellyfin_api_key="k") == 200)
check("...and the .env change names the server", calls["cfg"]["media_server_type"] == "jellyfin"
      and calls["cfg"]["jellyfin_url"] == URL)
calls.clear()
check("Plex (the default) still needs its URL and token",
      complete() == 422 and complete(plex_url="http://192.168.1.2:32400", plex_token="t") == 200)
check("...and writes the blank default, like every install before Jellyfin",
      calls["cfg"]["media_server_type"] == "")
try:
    st.SetupCompleteRequest(media_server_type="emby")
    emby = False
except Exception:  # noqa: BLE001 - the refusal IS the assertion
    emby = True
check("an unknown server kind is refused", emby)
st.write_env, st.build_ollama_models = real_write, real_build

print("\n-- .env --")


class _Live:
    effective_jwt_secret = "live-jwt-secret-0123456789-0123456789-01234567"
    effective_plex_url = ""
    effective_plex_token = ""
    PLEX_CLIENT_ID = "curatarr-test"
    PITCHER_MODEL = ""
    BASE_PITCHER_MODEL = "qwen3.8:27b"
    SOULSYNC_URL = SOULSYNC_API_KEY = MAL_CLIENT_ID = LISTENBRAINZ_TOKEN = ""
    OPENSUBTITLES_API_KEY = OPENSUBTITLES_USERNAME = OPENSUBTITLES_PASSWORD = ""
    OPENSUBTITLES_DAILY_BUDGET = 200
    SYNC_ON_STARTUP = True
    SYNC_INTERVAL_HOURS = 24
    BINGE_EPISODE_THRESHOLD, BINGE_SESSION_HOURS, BINGE_SERIES_PERCENT = 3, 6, 50


env = _TMP / ".env"
real = (sw.ENV_PATH, sw._live_settings, sw._restrict_env_acl)
sw.ENV_PATH, sw._live_settings, sw._restrict_env_acl = env, (lambda: _Live()), (lambda p: None)
try:
    sw.write_env({"media_server_type": "jellyfin", "jellyfin_url": URL, "jellyfin_api_key": "jf-key"})
    text = env.read_text(encoding="utf-8")
    check("MEDIA_SERVER_TYPE, JELLYFIN_URL and JELLYFIN_API_KEY are written",
          "MEDIA_SERVER_TYPE=jellyfin" in text and f"JELLYFIN_URL={URL}" in text
          and "JELLYFIN_API_KEY=jf-key" in text)
    loaded = Settings(_env_file=str(env))
    check("...and load back: a Jellyfin install without Plex settings is configured",
          loaded.MEDIA_SERVER_TYPE == "jellyfin" and loaded.is_configured
          and not Settings(_env_file=None, JWT_SECRET="x" * 40).is_configured)
    sw.write_env({})
    check("a stub without the new settings writes them blank (Plex default)",
          "MEDIA_SERVER_TYPE=\n" in env.read_text(encoding="utf-8")
          and Settings(_env_file=str(env)).MEDIA_SERVER_TYPE is None)
finally:
    sw.ENV_PATH, sw._live_settings, sw._restrict_env_acl = real
check("the API key is a secret: masked for the panel, never echoed",
      sw.mask_secrets({"jellyfin_api_key": "k"}) == {"jellyfin_api_key": {"set": True}}
      and "jellyfin_api_key" in st.ReconfigureRequest.model_fields)

print("\n-- the library mapping --")
settings.MEDIA_SERVER_TYPE = "jellyfin"
real_get = ms.get_adapter
ms.get_adapter = lambda settings=None, *, transport=None: _Orig(URL, "jf-key", transport=jellyfin_transport())
try:
    disc = asyncio.run(libraries.discover_libraries(user=SimpleNamespace(id=1, is_admin=True)))
finally:
    ms.get_adapter = real_get
    settings.MEDIA_SERVER_TYPE = None
rows = {s["title"]: s for s in disc["sections"]}
check("movie, show and music libraries, in the Plex sections' shape",
      list(rows) == ["Movies", "Shows", "Music"]
      and [rows[t]["type"] for t in rows] == ["movie", "show", "artist"]
      and [rows[t]["suggested_category"] for t in rows] == ["movie", "show", "music"])
check("with their item counts and folders",
      [rows[t]["plex_item_count"] for t in rows] == [2, 1, 1]
      and rows["Movies"]["locations"] == ["/media/movies"]
      and rows["Movies"]["key"] == "f137a2dd21bbc1b99aa5c0f6bf02a805")

check.done()
