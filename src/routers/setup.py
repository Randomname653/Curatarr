"""
Curatarr - Setup Router

Handles the first-run setup wizard.

Auth model:
  - /status, /fields are public (the frontend wizard reads them before any
    user account exists).
  - /test, /complete, /build-models are gated by ``require_admin_or_first_run``:
    callable WITHOUT auth while no admin User exists (initial onboarding),
    admin-only thereafter (prevents post-setup SSRF + .env-overwrite by
    arbitrary callers).
"""

import asyncio
import logging
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from typing import Optional

from src.config import settings
from src.routers.auth import require_admin_or_first_run, require_admin
from src.services.endpoint_policy import (
    NEW_ADDRESS_NEEDS_KEY, resolved_refusal, secret_for_target, validate_service_url)
from src.services.setup_wizard import (
    test_plex, test_jellyfin, test_ollama, test_arr, test_tmdb, test_lastfm, test_spotify,
    write_env, build_ollama_models, SETUP_FIELDS,
    endpoint_privacy_note,   # Pass 97
)

logger = logging.getLogger(__name__)

_URL_FIELDS = ("plex_url", "jellyfin_url", "ollama_endpoint", "radarr_url", "sonarr_url",
               "lidarr_url", "soulsync_url")


async def _refuse_resolved(values: dict) -> None:
    """422 when a configured address NAME resolves to a link-local /
    metadata address. The validators judge literal addresses only; saving
    such a name would point every later background call (model pulls and
    builds, syncs) at the metadata service."""
    for field in _URL_FIELDS:
        url = (values.get(field) or "").strip()
        if url:
            why = await resolved_refusal(url)
            if why:
                logger.warning("[setup] %s refused: %s", field, why)
                raise HTTPException(status_code=422, detail=f"{field}: {why}")
router = APIRouter()


def _write_env_or_400(cfg: dict) -> None:
    """write_env refuses values it must not persist (a line break that would
    inject another .env key, a masked secret placeholder) with ValueError -
    that is the caller's bad input, not a server fault."""
    try:
        write_env(cfg)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/status")
def setup_status():
    """Return whether setup is complete and what's configured."""
    return {
        "first_run": settings.FIRST_RUN,
        "configured": settings.is_configured,
        "has_plex": bool(settings.effective_plex_url and settings.effective_plex_token),
        "media_server_type": settings.MEDIA_SERVER_TYPE or "plex",
        "has_jellyfin": bool(settings.JELLYFIN_URL and settings.JELLYFIN_API_KEY),
        "has_ollama": bool(settings.effective_ollama),
        "has_tmdb": bool(settings.TMDB_API_KEY),
        "has_lastfm": bool(settings.LASTFM_API_KEY),
        "has_spotify": bool(settings.SPOTIFY_CLIENT_ID and settings.SPOTIFY_CLIENT_SECRET),
        "has_radarr": bool(settings.RADARR_URL and settings.RADARR_API_KEY),
        "has_sonarr": bool(settings.SONARR_URL and settings.SONARR_API_KEY),
        "has_lidarr": bool(settings.LIDARR_URL and settings.LIDARR_API_KEY),
    }


@router.get("/fields")
def get_setup_fields():
    """Return setup field definitions for the wizard UI."""
    return {"fields": SETUP_FIELDS}


class TestRequest(BaseModel):
    service: str       # plex / ollama / radarr / sonarr / lidarr / tmdb / lastfm / spotify
    url: Optional[str] = None
    token: Optional[str] = None
    api_key: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None

    # Every service address a request carries passes the endpoint policy
    # (http(s), no query/fragment/credentials, no link-local or metadata
    # address) before anything connects to it or writes it to .env.
    _url = field_validator("url")(validate_service_url)


@router.post("/test")
async def test_connection(
    req: TestRequest,
    _gate=Depends(require_admin_or_first_run),
):
    """Test a service connection during setup.

    Pass 97: when the service is one of the endpoint-style ones (plex /
    ollama / *arr), check whether the URL points at a private address
    and attach a ``privacy_warning`` if not. Frontend renders this as a
    yellow banner. We never block — the user might legitimately point
    Ollama at a cloud GPU box — but we never go silent about it either.
    """
    # Post-setup panel: blank secrets mean "use what is saved" - the
    # browser never receives them, so it cannot echo them back. A saved
    # secret only ever travels to its saved address (endpoint_policy): a
    # new URL with a blank key field used to send it to any host.
    def _stored(name: str) -> str:
        return getattr(settings, name, None) or ""

    def _secret(supplied, stored_url, stored_secret):
        try:
            return secret_for_target(req.url, supplied, stored_url, stored_secret)
        except ValueError:
            raise HTTPException(status_code=400, detail=NEW_ADDRESS_NEEDS_KEY)
    if req.service == "jellyfin":
        stored_url = str(settings.JELLYFIN_URL) if settings.JELLYFIN_URL else ""
        stored_key = (settings.JELLYFIN_API_KEY.get_secret_value()
                      if settings.JELLYFIN_API_KEY else "")
        result = await test_jellyfin(req.url or stored_url,
                                     _secret(req.api_key or req.token, stored_url, stored_key))
    elif req.service == "plex":
        result = await test_plex(req.url or settings.effective_plex_url,
                                 _secret(req.token, settings.effective_plex_url,
                                         settings.effective_plex_token))
    elif req.service == "ollama":
        result = await test_ollama(req.url or settings.effective_ollama)
    elif req.service in ("radarr", "sonarr", "lidarr"):
        up = req.service.upper()
        result = await test_arr(req.url or _stored(f"{up}_URL"),
                                _secret(req.api_key, _stored(f"{up}_URL"),
                                        _stored(f"{up}_API_KEY")),
                                req.service)
    elif req.service == "tmdb":
        result = await test_tmdb(req.api_key or _stored("TMDB_API_KEY"))
    elif req.service == "lastfm":
        result = await test_lastfm(req.api_key or _stored("LASTFM_API_KEY"))
    elif req.service == "spotify":
        result = await test_spotify(req.client_id or _stored("SPOTIFY_CLIENT_ID"),
                                    req.client_secret or _stored("SPOTIFY_CLIENT_SECRET"))
    else:
        raise HTTPException(status_code=400, detail=f"Unknown service: {req.service}")

    # Only the endpoint-style services have a URL to classify. TMDB /
    # Last.fm / Spotify go to fixed public hostnames by design — no
    # warning makes sense there.
    if req.service in ("plex", "jellyfin", "ollama", "radarr", "sonarr", "lidarr"):
        url = req.url or (settings.effective_ollama if req.service == "ollama" else "")
        warn = endpoint_privacy_note(url)
        if warn and isinstance(result, dict):
            result["privacy_warning"] = warn
    return result


@router.get("/gpu")
async def gpu_probe(_gate=Depends(require_admin_or_first_run)):
    """User-triggered nvidia-smi probe of the Curatarr host (wizard button)."""
    from src.services.setup_wizard import detect_gpu
    return await detect_gpu()


class RecommendRequest(BaseModel):
    ollama_endpoint: Optional[str] = None
    vram_gb: Optional[float] = None
    # The detected card's name: scales the bench's measured speed by memory
    # bandwidth for the hardware package (model_catalog.hardware_package).
    gpu_name: Optional[str] = Field(default=None, max_length=120)
    # The host's system RAM: a package that offloads most of its model needs
    # the rest there (model_catalog.hardware_package falls back when short).
    ram_gb: Optional[float] = Field(default=None, ge=0, le=4096)

    _url = field_validator("ollama_endpoint")(validate_service_url)


@router.post("/recommend")
async def recommend(req: RecommendRequest,
                    _gate=Depends(require_admin_or_first_run)):
    """VRAM-aware model recommendations from the bench-verified catalog.

    Also returns the Ollama server's installed tags so the wizard can mark
    zero-download choices and still offer everything already pulled.
    """
    from src.services.model_catalog import recommend_models
    installed: set = set()
    endpoint = req.ollama_endpoint or settings.effective_ollama
    tags = await test_ollama(endpoint)
    if tags.get("ok"):
        installed = set(tags.get("models") or [])
    result = recommend_models(req.vram_gb, installed, req.gpu_name, req.ram_gb)
    result["installed"] = sorted(installed)
    result["ollama_ok"] = bool(tags.get("ok"))
    return result


class WarmupRequest(BaseModel):
    ollama_endpoint: Optional[str] = None
    model: str

    _url = field_validator("ollama_endpoint")(validate_service_url)


@router.post("/warmup")
async def warmup(req: WarmupRequest,
                 _gate=Depends(require_admin_or_first_run)):
    """Post-bake reality check: GPU residency + generation speed."""
    from src.services.setup_wizard import warmup_check
    endpoint = req.ollama_endpoint or settings.effective_ollama
    await _refuse_resolved({"ollama_endpoint": endpoint})
    return await warmup_check(endpoint, req.model)


class MinibenchRequest(BaseModel):
    ollama_endpoint: Optional[str] = None
    vram_gb: Optional[float] = None
    gpu_name: Optional[str] = Field(default=None, max_length=120)

    _url = field_validator("ollama_endpoint")(validate_service_url)


@router.post("/minibench")
async def minibench(req: MinibenchRequest,
                    _gate=Depends(require_admin_or_first_run)):
    """About a minute of production calls on this machine (warm-up, two
    deletion verdicts, one card note) for a fictional owner: does the baked
    curator run as the bench measured? When the hardware matrix has a package
    for this card and it names the same base model, the bench runs with the
    package's context and GPU split and returns its numbers to compare."""
    from src.services.model_catalog import hardware_package
    from src.services.setup_minibench import run_minibench
    endpoint = req.ollama_endpoint or settings.effective_ollama
    await _refuse_resolved({"ollama_endpoint": endpoint})
    pkg = hardware_package(req.vram_gb, req.gpu_name) if req.vram_gb else None
    rec = (pkg or {}).get("recommended") or {}
    same = rec.get("model") == settings.BASE_CURATOR_MODEL
    result = await run_minibench(endpoint, settings.CURATOR_MODEL,
                                 num_ctx=rec.get("num_ctx") if same else None,
                                 num_gpu=rec.get("num_gpu") if same else None,
                                 expected_gen_tok_s=((pkg.get("estimate") or {}).get("gen_tok_s")
                                                     or (rec.get("expected") or {}).get("gen_tok_s"))
                                 if same else None)
    if same:
        result["package"] = {"tier_gb": pkg.get("tier_gb"), "model": rec.get("model"),
                             "expected": rec.get("expected"), "estimate": pkg.get("estimate"),
                             "server": {k: v for k, v in (rec.get("server") or {}).items()
                                        if k in ("OLLAMA_KV_CACHE_TYPE", "OLLAMA_FLASH_ATTENTION")}}
    return result


class SetupCompleteRequest(BaseModel):
    # One media server per install: Plex (the default) or Jellyfin. The
    # chosen one's address and key are required; the other's may stay blank.
    media_server_type: str = "plex"
    plex_url: str = ""
    plex_token: str = ""
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    # A finished Jellyfin connect (POST /jellyfin/connect...): the address
    # and the key Curatarr made stay on the server under this ticket.
    jellyfin_ticket: str = Field(default="", max_length=128)
    ollama_endpoint: str = "http://localhost:11434"
    base_curator_model: str = "gemma4:31b"
    base_summarizer_model: str = "granite4.1:8b"
    embedding_model: str = "nomic-embed-text-v2-moe"
    tmdb_api_key: str = ""
    # omdb was in SETUP_FIELDS and in the .env template from the start, but
    # never in this request model — pydantic silently dropped the value, so
    # the wizard could not actually save an OMDb key. The schema lives in
    # three places (SETUP_FIELDS, this model, the frontend forms); a test
    # now asserts the three agree.
    omdb_api_key: str = ""
    lastfm_api_key: str = ""
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    radarr_url: str = ""
    radarr_api_key: str = ""
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    lidarr_url: str = ""
    lidarr_api_key: str = ""
    soulsync_url: str = ""
    soulsync_api_key: str = ""
    listenbrainz_token: str = ""
    # Two-bake split: a dedicated deletion-judge bake (benchmarked better
    # at pitches than the chat curator). Off unless the wizard turns it on.
    enable_pitcher: bool = False
    base_pitcher_model: str = "qwen3.8:27b"
    # Sharing the card with another program (src/services/llm_lane.py).
    gpu_pressure_gate: bool = True
    llm_cpu_lane: bool = True
    llm_cpu_threads: int = 6
    llm_cpu_min_free_mb: int = 12000

    _urls = field_validator("plex_url", "jellyfin_url", "ollama_endpoint", "radarr_url", "sonarr_url",
                           "lidarr_url", "soulsync_url")(validate_service_url)

    @field_validator("media_server_type")
    @classmethod
    def _known_server(cls, v):
        v = (v or "plex").strip().lower()
        if v not in ("plex", "jellyfin"):
            raise ValueError("media_server_type must be plex or jellyfin")
        return v


class ReconfigureRequest(BaseModel):
    """Partial post-setup change: every field optional, None = unchanged,
    "" = clear. Same keys the wizard writes, so one write_env serves both."""
    plex_url: Optional[str] = None
    plex_token: Optional[str] = None
    jellyfin_url: Optional[str] = None
    jellyfin_api_key: Optional[str] = None
    ollama_endpoint: Optional[str] = None
    base_curator_model: Optional[str] = None
    base_summarizer_model: Optional[str] = None
    embedding_model: Optional[str] = None
    enable_pitcher: Optional[bool] = None
    base_pitcher_model: Optional[str] = None
    tmdb_api_key: Optional[str] = None
    omdb_api_key: Optional[str] = None
    mal_client_id: Optional[str] = None
    lastfm_api_key: Optional[str] = None
    spotify_client_id: Optional[str] = None
    spotify_client_secret: Optional[str] = None
    soulsync_url: Optional[str] = None
    soulsync_api_key: Optional[str] = None
    listenbrainz_token: Optional[str] = None
    opensubtitles_api_key: Optional[str] = None
    opensubtitles_username: Optional[str] = None
    opensubtitles_password: Optional[str] = None
    opensubtitles_daily_budget: Optional[int] = None
    radarr_url: Optional[str] = None
    radarr_api_key: Optional[str] = None
    sonarr_url: Optional[str] = None
    sonarr_api_key: Optional[str] = None
    lidarr_url: Optional[str] = None
    lidarr_api_key: Optional[str] = None
    gpu_pressure_gate: Optional[bool] = None
    llm_cpu_lane: Optional[bool] = None
    llm_cpu_threads: Optional[int] = None
    llm_cpu_min_free_mb: Optional[int] = None

    _urls = field_validator("plex_url", "jellyfin_url", "ollama_endpoint", "radarr_url", "sonarr_url",
                           "lidarr_url", "soulsync_url")(validate_service_url)


@router.post("/complete")
async def complete_setup(
    req: SetupCompleteRequest,
    background_tasks: BackgroundTasks,
    _gate=Depends(require_admin_or_first_run),
):
    """
    Finalize setup: write .env and kick off model building.
    The app needs a restart after this to reload settings.
    """
    # Blank Plex fields used to flip FIRST_RUN=false anyway, leaving an
    # install the UI could never route back into the wizard.
    ticket = req.jellyfin_ticket
    if req.media_server_type == "jellyfin" and ticket:
        from src.services.media_server.jellyfin_connect import held
        got = held(ticket)
        if got is None:
            raise HTTPException(status_code=422,
                                detail="That Jellyfin connection expired - connect again")
        req.jellyfin_url, req.jellyfin_api_key = got
    if req.media_server_type == "jellyfin":
        if not req.jellyfin_url.strip() or not req.jellyfin_api_key.strip():
            raise HTTPException(status_code=422,
                                detail="Jellyfin URL and API key are required to complete setup")
    elif not req.plex_url.strip() or not req.plex_token.strip():
        raise HTTPException(status_code=422,
                            detail="Plex URL and token are required to complete setup")
    # Write .env - enable_pitcher becomes the baked name the runtime keys on
    cfg = req.dict()
    cfg.pop("jellyfin_ticket", None)
    # Blank = plex in .env, the same as every install before Jellyfin support.
    cfg["media_server_type"] = "" if req.media_server_type == "plex" else req.media_server_type
    await _refuse_resolved(cfg)
    cfg["pitcher_model"] = "curatarr-pitcher" if req.enable_pitcher else ""
    # On a worker: write_env does file I/O and, on Windows, two icacls
    # subprocesses (up to 10 s each). The 400 mapping travels with it.
    await asyncio.to_thread(_write_env_or_400, cfg)
    if ticket:
        from src.services.media_server.jellyfin_connect import release
        release(ticket)

    # Build Ollama models in background (takes a moment). The pitcher bake
    # used to be unreachable from here - only the post-setup rebuild built it.
    background_tasks.add_task(
        build_ollama_models,
        req.ollama_endpoint,
        req.base_curator_model,
        req.base_summarizer_model,
        req.base_pitcher_model if req.enable_pitcher else None,
    )

    return {
        "status": "ok",
        "message": "Configuration saved. Building Ollama models in background. Please restart Curatarr.",
        "restart_required": True,
    }


@router.post("/build-models")
async def build_models_endpoint(
    background_tasks: BackgroundTasks,
    _gate=Depends(require_admin_or_first_run),
):
    """Build curatarr-curator and curatarr-summarizer Ollama models
    (plus curatarr-pitcher when the two-bake split is enabled) on THIS
    install's Ollama - the plan build_models.py --host main follows: on a
    split install no pitcher here (it lives on the gaming PC), and the
    host's own curator window."""
    from src.services.model_check import host_plan
    plan = await asyncio.to_thread(host_plan, "main")
    results = await build_ollama_models(
        plan["url"], plan["curator"], plan["summarizer"],
        base_pitcher=plan["pitcher"], num_ctx=plan["num_ctx"],
        with_embedding=bool(plan["embedding"]), embed_model=plan["embedding"])
    return {
        "curator": results.get("curator", False),
        "summarizer": results.get("summarizer", False),
        "pitcher": results.get("pitcher"),
    }


# ── post-setup: see and change what the wizard configured ───────────────────

_MODEL_KEYS = ("base_curator_model", "base_summarizer_model", "embedding_model",
               "pitcher_model", "base_pitcher_model")


@router.get("/integrations")
def integrations_status(_admin=Depends(require_admin)):
    """Every integration the wizard knows, as the live settings hold it -
    plain values for URLs/models, ``{"set": bool}`` for secrets, and the
    JWT secret not at all. This is what Settings -> Integrations renders."""
    from src.services.setup_wizard import current_env_config, mask_secrets
    return {"config": mask_secrets(current_env_config()),
            "pitcher_enabled": bool(settings.PITCHER_MODEL)}


@router.post("/reconfigure")
async def reconfigure(req: ReconfigureRequest, _admin=Depends(require_admin)):
    """Change any subset of integration settings after setup.

    Until this existed, Plex URL/token, the Ollama endpoint, the model
    choices and every metadata key could be set exactly once - in the wizard
    - and never again except by hand-editing .env. Merges the change on the
    live config, writes atomically, reloads the process settings. Model
    changes additionally need a rebuild (POST /build-models) and a restart;
    the response says so.
    """
    from src.services.setup_wizard import current_env_config, merge_env_config
    changes = req.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to change")
    if "plex_url" in changes and not changes["plex_url"].strip():
        raise HTTPException(status_code=422, detail="Plex URL cannot be empty")
    if "plex_token" in changes and not changes["plex_token"].strip():
        raise HTTPException(status_code=422, detail="Plex token cannot be empty")
    if settings.MEDIA_SERVER_TYPE == "jellyfin":
        for key in ("jellyfin_url", "jellyfin_api_key"):
            if key in changes and not changes[key].strip():
                raise HTTPException(status_code=422, detail=f"{key} cannot be empty on a Jellyfin install")
    await _refuse_resolved(changes)
    before = current_env_config()
    merged = merge_env_config(before, changes)
    await asyncio.to_thread(_write_env_or_400, merged)   # file I/O + icacls, as above
    settings.__init__()   # live reload, same as the library panel does
    models_changed = any(before.get(k) != merged.get(k) for k in _MODEL_KEYS)
    endpoints_changed = any(before.get(k) != merged.get(k)
                            for k in ("plex_url", "jellyfin_url", "ollama_endpoint"))
    logger.info("[setup] integrations reconfigured by admin: %s",
                ", ".join(sorted(changes)))
    return {"ok": True, "changed": sorted(changes),
            "models_changed": models_changed,
            "restart_recommended": models_changed or endpoints_changed}


# ── Jellyfin: found and connected without copying a key ─────────────────────
# (src/services/media_server/jellyfin_connect.py). Same gate as /test: open
# while no admin exists, the admin's afterwards.

SETUP_POLLS_PER_IP_PER_MINUTE = 30
SETUP_PW_TRIES_PER_IP = 10
SETUP_PW_WINDOW_S = 15 * 60


def _ip(request: Request) -> str:
    return (request.client.host if request.client else "") or "?"


@router.get("/jellyfin/servers")
async def jellyfin_servers(request: Request, _gate=Depends(require_admin_or_first_run)):
    """The Jellyfin servers on the LAN, for the wizard's pick list. The host
    this page was loaded from is asked too (on a NAS, Jellyfin's likely
    home); it passes the endpoint policy like every other candidate."""
    from dataclasses import asdict
    from src.services.media_server.jellyfin_connect import find_servers
    found = await find_servers(via_host=request.url.hostname)
    return {"servers": [asdict(f) for f in found]}


class JellyfinConnectRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    _url = field_validator("url")(validate_service_url)


async def _connect_target(url: str) -> str:
    url = url.strip().rstrip("/")
    await _refuse_resolved({"jellyfin_url": url})
    return url


async def _connected(url: str, who, api_key: str) -> dict:
    """The finished connect: the key checked like a pasted one, then held
    under a ticket - the browser never sees it."""
    from src.services.media_server.jellyfin_connect import hold
    result = await test_jellyfin(url, api_key)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error") or "Jellyfin did not answer")
    logger.info("[setup] Jellyfin connected by its admin %s (%s %s)",
                who.name, result.get("server_name"), result.get("version"))
    return {"status": "ok", "ticket": hold(url, api_key), "url": url, "admin": who.name,
            "server_name": result.get("server_name"), "version": result.get("version")}


@router.post("/jellyfin/connect")
async def jellyfin_connect_start(req: JellyfinConnectRequest, request: Request,
                                 _gate=Depends(require_admin_or_first_run)):
    """Step 1 - a Quick Connect code for the Jellyfin admin to approve."""
    from src.routers.auth import _enforce_pin_create_rate_limit
    from src.services.media_server.jellyfin_auth import (JellyfinAuth, JellyfinAuthError,
                                                         JellyfinAuthRefused)
    from src.services.media_server.jellyfin_connect import PENDING_TTL_S, bind_pending
    _enforce_pin_create_rate_limit(_ip(request))
    url = await _connect_target(req.url)
    auth = JellyfinAuth(url)
    try:
        secret, code = await auth.initiate()
    except JellyfinAuthRefused:
        raise HTTPException(status_code=409, detail="Quick Connect is off on this Jellyfin server - "
                                                    "connect with the admin password instead, or turn "
                                                    "it on (Dashboard -> General)")
    except JellyfinAuthError as e:
        logger.warning("[setup] Jellyfin Quick Connect not started: %s", e)
        raise HTTPException(status_code=502, detail="Jellyfin did not answer at that address")
    login_id, nonce = bind_pending(secret, auth.device_id, url)
    return {"login_id": login_id, "code": code, "nonce": nonce, "expires_in": PENDING_TTL_S}


@router.get("/jellyfin/connect/{login_id}")
async def jellyfin_connect_poll(login_id: str, request: Request,
                                _gate=Depends(require_admin_or_first_run)):
    """Step 2 - poll until the code is approved; then Curatarr's API key
    is made (or found again) and held under a ticket."""
    from src.services import rate_limit
    from src.services.media_server.jellyfin_auth import (JellyfinAuth, JellyfinAuthError,
                                                         JellyfinAuthRefused)
    from src.services.media_server.jellyfin_connect import check_pending, drop_pending
    rate_limit.enforce("jellyfin-setup-poll-ip", _ip(request), SETUP_POLLS_PER_IP_PER_MINUTE, 60,
                       detail="Too many requests")
    pending = check_pending(login_id, request.headers.get("X-Jellyfin-Login-Nonce", ""))
    if pending is None:
        raise HTTPException(status_code=403, detail="Unknown or expired code - start Quick Connect again")
    secret, device_id, url = pending
    auth = JellyfinAuth(url, device_id=device_id)
    try:
        if not await auth.approved(secret):
            return {"status": "pending"}
        who, api_key = await auth.admin_api_key(secret=secret)
    except JellyfinAuthRefused as e:
        drop_pending(login_id)
        raise HTTPException(status_code=403, detail=(
            "That Jellyfin account is no administrator - approve the code with an admin account"
            if "administrator" in str(e) else "Jellyfin refused this code - start Quick Connect again"))
    except JellyfinAuthError as e:
        logger.warning("[setup] Jellyfin Quick Connect poll failed: %s", e)
        raise HTTPException(status_code=502, detail="Jellyfin did not answer")
    drop_pending(login_id)
    return await _connected(url, who, api_key)


class JellyfinPasswordConnect(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(default="", max_length=1024)   # Jellyfin allows an empty password
    _url = field_validator("url")(validate_service_url)


@router.post("/jellyfin/connect-password")
async def jellyfin_connect_password(req: JellyfinPasswordConnect, request: Request,
                                    _gate=Depends(require_admin_or_first_run)):
    """The same connect with the admin's name and password, for a server
    whose Quick Connect is off. The password goes to Jellyfin once and is
    never stored or logged."""
    from src.services import rate_limit
    from src.services.media_server.jellyfin_auth import (JellyfinAuth, JellyfinAuthError,
                                                         JellyfinAuthRefused)
    rate_limit.enforce("jellyfin-setup-pw-ip", _ip(request), SETUP_PW_TRIES_PER_IP, SETUP_PW_WINDOW_S,
                       detail="Too many tries - wait 15 minutes, or use Quick Connect")
    url = await _connect_target(req.url)
    try:
        who, api_key = await JellyfinAuth(url).admin_api_key(username=req.username,
                                                             password=req.password)
    except JellyfinAuthRefused as e:
        raise HTTPException(status_code=403, detail=(
            "That Jellyfin account is no administrator" if "administrator" in str(e)
            else "Wrong name or password"))
    except JellyfinAuthError as e:
        logger.warning("[setup] Jellyfin password connect failed: %s", e)
        raise HTTPException(status_code=502, detail="Jellyfin did not answer at that address")
    return await _connected(url, who, api_key)


class JellyfinSaveRequest(BaseModel):
    ticket: str = Field(min_length=1, max_length=128)


@router.post("/jellyfin/save")
async def jellyfin_save(req: JellyfinSaveRequest, _admin=Depends(require_admin)):
    """After setup: a finished connect's address and key into the settings.
    Which server signs people in does not change here - that is the media
    server switch's job."""
    from src.services.media_server.jellyfin_connect import held, release
    from src.services.setup_wizard import current_env_config, merge_env_config
    got = held(req.ticket)
    if got is None:
        raise HTTPException(status_code=422, detail="That Jellyfin connection expired - connect again")
    url, api_key = got
    merged = merge_env_config(current_env_config(), {"jellyfin_url": url, "jellyfin_api_key": api_key})
    await asyncio.to_thread(_write_env_or_400, merged)
    settings.__init__()
    release(req.ticket)
    logger.info("[setup] Jellyfin address and key saved by admin")
    return {"ok": True, "url": url}
