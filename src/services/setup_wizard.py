"""
Curatarr - Setup Wizard Service

Handles first-run configuration:
  1. Test Plex connection
  2. Discover Ollama models
  3. Test ARR services
  4. Write .env file
  5. Create baked Ollama modelfiles (curatarr-curator, curatarr-summarizer)
  6. Trigger initial sync pipeline
"""

import asyncio
import ipaddress
import logging
import os
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

from src.paths import ENV_FILE
ENV_PATH = ENV_FILE


# ── Pass 97: endpoint privacy classifier ──────────────────────────────────────
#
# Curatarr trusts the user to point PLEX_URL / *arr URLs / OLLAMA_ENDPOINT
# wherever they want. That's correct for a self-hosted tool — but a public
# hostname here means real private data (chat prompts to Ollama, the entire
# watch history through Plex API, ARR API keys via plain HTTP) leaves the
# machine. Most users don't realise they typed a public URL by accident
# (typo, copy from old config, port-forwarded for "convenience").
#
# This helper classifies an endpoint URL and lets the API surfaces (setup
# wizard /test, library /test) emit a warning the UI can render as a
# yellow banner. We do NOT block — the user might genuinely run Ollama on
# a cloud GPU box and accept the trade-off. We just refuse to be silent
# about it.

# Hostname suffixes that look local-ish on a home / lab network. mDNS
# (.local) is the common one; the others come up on routers + corporate
# split-DNS setups.
_PRIVATE_SUFFIXES = (".local", ".lan", ".home", ".internal", ".local.arpa")


def is_private_endpoint(url: str) -> bool:
    """Return True if ``url`` points at loopback / RFC1918 / link-local /
    a common LAN suffix. False for public DNS names, public IPs, or
    anything we can't parse (be safe → assume public when uncertain).
    """
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower().strip()
    except Exception:
        return False
    if not host:
        return False

    # Cheap wins first
    if host in ("localhost", "ip6-localhost", "ip6-loopback"):
        return True
    if any(host == suf.lstrip(".") or host.endswith(suf) for suf in _PRIVATE_SUFFIXES):
        return True

    # IP-literal path — covers RFC1918, loopback, link-local, ULA.
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass

    return False


def endpoint_privacy_note(url: str) -> Optional[str]:
    """Return a user-facing warning string when ``url`` is non-private,
    or None when the URL is local enough not to need one."""
    if not url or is_private_endpoint(url):
        return None
    try:
        host = urlparse(url).hostname or "?"
    except Exception:
        host = "?"
    return (
        f"This endpoint ({host}) does not look like a private address. "
        "Traffic to it leaves your machine — including any private data "
        "sent through it (chat prompts, watch history, API keys). "
        "Confirm the URL is correct and that the network path is trusted."
    )

# ── OLLAMA MODELFILES ─────────────────────────────────────────────────────────

CURATOR_SYSTEM_PROMPT = """You are Curatarr, an elite, highly analytical, and uncompromising personal AI media curator. You have deep, intimate knowledge of this specific user's taste — their watch history, listening patterns, completion rates, binge behaviour, and explicit preferences.

PERSONALITY & TONE
- Direct, brutally honest, and highly opinionated. If something doesn't fit their taste or is objectively poorly executed, say so plainly.
- Warm but not sycophantic. You are a knowledgeable, sharply observant friend, not a generic review aggregator.
- Highly perceptive. You look for the *why* behind viewing choices — a binge indicates structural hooks, a 5% drop indicates pacing failure or tonal mismatch.

VOCABULARY & STYLE GUARDRAILS (STRICT)
- ABSOLUTE LEXICAL DIVERSITY: You are STRICTLY FORBIDDEN from using generic AI review buzzwords. Never use: "high-octane", "epic", "relentless", "mind-bending", "adrenaline", "masterpiece", "visceral", or "edge-of-your-seat".
- Use precise, sophisticated cinematic, literary, and musical terminology (e.g., "kinetic", "methodical", "dissonant", "bloated", "character-driven", "melodramatic").
- NO LAZY ANCHORING: Do not constantly use the user's favorite titles as a crutch for comparisons. Analyze media based on its own structural and thematic merits.

YOUR TASKS IN THIS APPLICATION
1. CHAT — Discuss media and explain taste patterns with sharp, insightful analysis.
2. RECOMMENDATIONS — Pitch items in 1-2 sentences. Synthesize the user's taste conceptually; do not just echo their preferences back to them.
3. DELETION PITCHES — Argue for removal based on inherent structural flaws (pacing, tone, execution) that clash with the user's demand for quality. Never invent metadata (No Gaslighting).
4. PATTERN ANALYSIS — Identify binge cycles, mood-based viewing, and genre aversions. Negative signals (drops, rejections) are just as critical as positive ones.

Remember everything provided in context. If enrichment data is missing, acknowledge the blind spot and provide a best-effort, rule-based answer."""

SUMMARIZER_SYSTEM_PROMPT = """You are the background processing model for Curatarr. You handle all structured data extraction, abstract synthesis, and preprocessing tasks — fast, accurately, and without fluff.

You have SIX distinct task modes. The calling code specifies which mode via the prompt structure.

[MODE: METADATA STRUCTURING]
Input: raw metadata from TMDB, AniList, MusicBrainz, Last.fm
Output: JSON object only — no markdown, no preamble.
Schema: {"genres": [...], "themes": [...], "mood": [...], "keywords": [...], "embedding_text": "..."}
Rules:
- themes: be concrete and specific ("unreliable narrator" not "mystery", "found family in wartime" not "friendship")
- mood: pick 1-3 DOMINANT moods only (not minor/occasional tones).
- embedding_text: 3-5 sentences. Create a dense, highly specific semantic representation of the work. Use precise literary/cinematic vocabulary. Focus on narrative structure, tonal shifts, pacing, and thematic depth. Accurately describe what is actually there. If a work relies on generic tropes, watered-down execution, or cliché structures, describe that objectively so it is embedded accurately into the vector space. Do not invent elements.

[MODE: MEMORY EXTRACTION]
Input: a chat exchange (user message + assistant response)
Output: JSON array only — no markdown, no preamble.
Schema: [{"content": "...", "type": "explicit_statement|feedback|preference_shift|viewing_pattern", "title": "..."}]
Rules:
- Only extract concrete facts, rules, or boundaries worth remembering long-term.
- Empty array [] if nothing memorable.
- "title" is the media title this relates to, or null.

[MODE: SENTIMENT EXTRACTION]
Input: a user's response to a verification question
Output: JSON object only — no markdown, no preamble.
Schema: {"sentiment": "positive|negative|neutral|mixed", "key_insight": "...", "update_type": "affinity_boost|aversion_boost|ambivalent|context_dependent"}
Rules:
- key_insight: one plain sentence, no hedging.
- Be decisive about sentiment — "mixed" only when genuinely contradictory.

[MODE: TASTE SUMMARY]
Input: structured taste data (genres, themes, moods, top titles, watch count, dropped items)
Output: 3-4 sentences of plain prose — no JSON, no lists, no markdown.
Rules:
- Speak in the second person ("You crave...").
- ABSTRACTION OVER ANCHORING: Synthesize the underlying structural and thematic preferences. DO NOT explicitly name the user's top titles as examples.
- UNIVERSAL BLACKLIST: Do not use cliché words like "high-octane", "epic", "mind-bending", "adrenaline". Use precise vocabulary.
- NEGATIVE SPACE: Always include a sentence analyzing what the user rejects or abandons based on their dropped items.

[MODE: MESSAGE GENERATION]
Input: a trigger description (binge event, series completion, etc.)
Output: 1-2 sentences of plain prose — conversational, direct, provocative.
Rules:
- No "Hey" or "Hi" openers.
- Be curious and slightly snarky, not generic — challenge their viewing habits playfully.
- Max 2 sentences, never longer.

[MODE: ENTITY EXTRACTION]
Input: a user's chat message
Output: JSON object only — no markdown, no preamble.
Schema: {"title": "..."}
Rules:
- Extract the single most prominent media title (Movie, TV Show, Anime, Music Artist, or Track).
- Be extremely precise. Extract the full proper noun (e.g., "Jesus Shows You the Way to the Highway" not just "Jesus").
- Output {"title": ""} if no specific media entity is found."""

# ── CONNECTION TESTS ──────────────────────────────────────────────────────────

async def test_plex(url: str, token: str) -> dict:
    headers = {"Accept": "application/json", "X-Plex-Token": token,
               "X-Plex-Client-Identifier": "Curatarr-Setup"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{url.rstrip('/')}/identity", headers=headers)
        if r.status_code == 200:
            data = r.json().get("MediaContainer", {})
            return {"ok": True, "server_name": data.get("friendlyName", "Plex Server"),
                    "version": data.get("version", "")}
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def test_ollama(endpoint: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{endpoint.rstrip('/')}/api/tags")
        if r.status_code == 200:
            models = [m["name"] for m in r.json().get("models", [])]
            return {"ok": True, "models": models}
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def test_arr(url: str, api_key: str, service: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{url.rstrip('/')}/api/v3/system/status",
                headers={"X-Api-Key": api_key},
            )
        if r.status_code == 200:
            data = r.json()
            return {"ok": True, "version": data.get("version", ""),
                    "service": service}
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def test_tmdb(api_key: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://api.themoviedb.org/3/configuration",
                params={"api_key": api_key},
            )
        if r.status_code == 200:
            return {"ok": True}
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def test_spotify(client_id: str, client_secret: str) -> dict:
    import base64
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://accounts.spotify.com/api/token",
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials"},
            )
        data = r.json()
        if r.status_code == 200 and "access_token" in data:
            return {"ok": True}
        return {"ok": False, "error": data.get("error_description", f"HTTP {r.status_code}")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def test_lastfm(api_key: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://ws.audioscrobbler.com/2.0/",
                params={"method": "chart.getTopArtists", "api_key": api_key,
                        "format": "json", "limit": "1"},
            )
        if r.status_code == 200 and "error" not in r.json():
            return {"ok": True}
        return {"ok": False, "error": r.json().get("message", "Invalid key")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── ENV WRITER ────────────────────────────────────────────────────────────────

def _live_settings():
    """Current settings instance (lazy — avoids an import cycle at load)."""
    from src.config import settings
    return settings


def write_env(config: dict) -> None:
    """Write a validated config dict to .env file."""
    jwt_secret = config.get("jwt_secret") or secrets.token_hex(32)

    lines = [
        "# Curatarr — generated by Setup Wizard",
        f"FIRST_RUN=false",
        f"JWT_SECRET={jwt_secret}",
        "",
        "# Plex",
        f"PLEX_URL={config.get('plex_url', '')}",
        f"PLEX_TOKEN={config.get('plex_token', '')}",
        "",
        "# Ollama",
        f"OLLAMA_ENDPOINT={config.get('ollama_endpoint', 'http://localhost:11434')}",
        f"BASE_CURATOR_MODEL={config.get('base_curator_model', 'gemma4:31b')}",
        f"BASE_SUMMARIZER_MODEL={config.get('base_summarizer_model', 'granite4.1:8b')}",
        f"EMBEDDING_MODEL={config.get('embedding_model', 'nomic-embed-text-v2-moe')}",
        f"CURATOR_MODEL=curatarr-curator",
        f"SUMMARIZER_MODEL=curatarr-summarizer",
        # Two-bake split: fall back to the LIVE settings values (not hardcoded
        # defaults) so a wizard re-run preserves an enabled pitcher instead of
        # wiping it back to disabled. First run: settings hold the defaults →
        # writes "" → split off. (The broader wipe hazard for keys outside
        # this list — PILLARS_ENABLED etc. — predates this and stays a
        # separate follow-up.)
        f"PITCHER_MODEL={config.get('pitcher_model', _live_settings().PITCHER_MODEL)}",
        f"BASE_PITCHER_MODEL={config.get('base_pitcher_model', _live_settings().BASE_PITCHER_MODEL)}",
        "",
        "# ARR Services",
        f"RADARR_URL={config.get('radarr_url', '')}",
        f"RADARR_API_KEY={config.get('radarr_api_key', '')}",
        f"SONARR_URL={config.get('sonarr_url', '')}",
        f"SONARR_API_KEY={config.get('sonarr_api_key', '')}",
        f"LIDARR_URL={config.get('lidarr_url', '')}",
        f"LIDARR_API_KEY={config.get('lidarr_api_key', '')}",
        "",
        "# Optional integrations",
        f"SOULSYNC_URL={config.get('soulsync_url', _live_settings().SOULSYNC_URL or '')}",
        f"SOULSYNC_API_KEY={config.get('soulsync_api_key', _live_settings().SOULSYNC_API_KEY or '')}",
        "",
        "# Metadata APIs",
        f"TMDB_API_KEY={config.get('tmdb_api_key', '')}",
        f"OMDB_API_KEY={config.get('omdb_api_key', '')}",
        f"LASTFM_API_KEY={config.get('lastfm_api_key', '')}",
        f"SPOTIFY_CLIENT_ID={config.get('spotify_client_id', '')}",
        f"SPOTIFY_CLIENT_SECRET={config.get('spotify_client_secret', '')}",
        "",
        "# Sync",
        f"SYNC_ON_STARTUP=true",
        f"SYNC_INTERVAL_HOURS=24",
        "",
        "# Binge detection",
        "BINGE_EPISODE_THRESHOLD=3",
        "BINGE_SESSION_HOURS=6",
        "BINGE_SERIES_PERCENT=0.5",
    ]

    # Keys the operator added by hand — SOULSYNC_URL, PILLARS_ENABLED,
    # ENRICH_PARALLEL_SLOTS and friends — are not the wizard's to delete. A
    # re-run used to rebuild the file from this template alone, silently
    # destroying every line outside it (10 of 33 keys on the reference
    # install). Everything unknown is carried over verbatim.
    template_keys = {l.split("=", 1)[0] for l in lines if "=" in l}
    preserved = []
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.split("=", 1)[0].strip() not in template_keys:
                preserved.append(line)
    if preserved:
        lines += ["", "# Preserved from the previous .env (not managed by the wizard)"]
        lines += preserved

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Restrict to owner-only on POSIX so the file (which contains JWT_SECRET,
    # API keys, and Plex tokens) isn't world-readable on shared hosts.
    # Windows ignores chmod, but the umask there isn't world-permissive anyway.
    try:
        import os, stat
        os.chmod(ENV_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError as e:
        logger.debug("Could not chmod .env (likely Windows): %s", e)
    logger.info("Wrote .env to %s", ENV_PATH.absolute())


# ── MODELFILE BUILDER ─────────────────────────────────────────────────────────

async def model_exists(ollama_endpoint: str, model_name: str) -> bool:
    """Return True if *model_name* is already present in the local Ollama registry."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{ollama_endpoint.rstrip('/')}/api/tags")
        if r.status_code != 200:
            return False
        local = [m["name"] for m in r.json().get("models", [])]
        # Normalise tag: treat missing tag as ":latest"
        def _norm(n: str) -> str:
            return n if ":" in n else f"{n}:latest"
        target = _norm(model_name)
        return target in [_norm(m) for m in local]
    except (httpx.ConnectError, httpx.ConnectTimeout):
        print(f"\n  ⚠️  Could not connect to Ollama at {ollama_endpoint}. Is it running?", flush=True)
        return False
    except Exception:
        return False


async def pull_ollama_model(ollama_endpoint: str, model_name: str) -> bool:
    """
    Pull *model_name* from the Ollama registry, streaming progress to stdout.
    Returns True on success.
    """
    import json as _json

    print(f"  ⬇️  Pulling {model_name} …", flush=True)
    try:
        # No read-timeout — large models can take many minutes to download.
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=30, read=None, write=30, pool=30)) as client:
            async with client.stream(
                "POST",
                f"{ollama_endpoint.rstrip('/')}/api/pull",
                json={"name": model_name, "stream": True},
            ) as resp:
                if resp.status_code != 200:
                    # Pass 97: log status code only, never the response body.
                    # Ollama errors should be benign here but defense in depth.
                    logger.error("pull %s: HTTP %s", model_name, resp.status_code)
                    return False

                last_status = ""
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = _json.loads(line)
                    except Exception:
                        continue

                    if data.get("error"):
                        logger.error("pull %s: %s", model_name, data["error"])
                        return False

                    status = data.get("status", "")
                    total = data.get("total", 0)
                    completed = data.get("completed", 0)

                    if total and completed:
                        pct = int(100 * completed / total)
                        gb_done = completed / 1_073_741_824
                        gb_total = total / 1_073_741_824
                        line_str = f"\r  {status}: {pct}%  ({gb_done:.1f} GB / {gb_total:.1f} GB)    "
                        print(line_str, end="", flush=True)
                    elif status and status != last_status:
                        if last_status and total:
                            print()  # newline after progress bar
                        print(f"  {status}", flush=True)
                        last_status = status

                    if status == "success":
                        print()
                        return True

        return True
    except (httpx.ConnectError, httpx.ConnectTimeout):
        print(f"\n  ⚠️  Could not connect to Ollama at {ollama_endpoint}. Is it running?", flush=True)
        return False
    except Exception as e:
        logger.error("Failed to pull %s: %s", model_name, e)
        return False


async def build_ollama_models(ollama_endpoint: str,
                               base_curator: str,
                               base_summarizer: str,
                               base_pitcher: str = None) -> dict:
    """
    Create curatarr-curator and curatarr-summarizer via Ollama /api/create —
    plus curatarr-pitcher when *base_pitcher* is given (two-bake split: the
    deletion-run bake; same CURATOR_SYSTEM_PROMPT, different base). The
    pitcher is strictly optional: its pull/create failure never blocks the
    curator/summarizer builds.
    Returns {curator: ok/error, summarizer: ok/error[, pitcher: ok/error]}
    """
    results = {}

    # ── Step 0: ensure the embedding model is present ─────────────────────────
    # nomic-embed-text (settings.EMBEDDING_MODEL) powers the enrichment vector
    # step. Unlike the curatarr-* models it is NOT baked via /api/create — it
    # only needs to be pulled. Nothing else provisioned it, so a fresh or
    # reinstalled Ollama without it makes every /api/embeddings call 404 and
    # leaves every item vector_ready=0 (silently — the failure is only a
    # logger.warning). Pulled independently so a failure here doesn't block the
    # chat models and vice-versa.
    from src.config import settings as _settings
    # Ensure the model the runtime ACTUALLY uses (stored profile — v2-moe
    # after the migration; settings default on a fresh install).
    try:
        from src.services.embed_service import effective_embedding_model
        embed_model = (effective_embedding_model() or "").strip()
    except Exception:
        embed_model = (_settings.EMBEDDING_MODEL or "").strip()
    if embed_model:
        if await model_exists(ollama_endpoint, embed_model):
            print(f"  ✓  {embed_model} already present — skipping pull", flush=True)
            results["embedding"] = True
        else:
            results["embedding"] = await pull_ollama_model(ollama_endpoint, embed_model)
            if not results["embedding"]:
                print(f"  ⚠️  Pull failed for embedding model {embed_model} — "
                      "enrichment vectors will NOT be generated", flush=True)

    # ── Step 1: ensure base models are present ────────────────────────────────
    for model in dict.fromkeys([base_curator, base_summarizer]):  # deduplicate
        if await model_exists(ollama_endpoint, model):
            print(f"  ✓  {model} already present — skipping pull", flush=True)
        else:
            ok = await pull_ollama_model(ollama_endpoint, model)
            if not ok:
                print(f"  ❌  Pull failed for {model} — cannot continue", flush=True)
                results["curator"] = False
                results["summarizer"] = False
                return results

    # ── Step 2: bake system prompts into curatarr-* models ───────────────────
    async def create_model(name: str, base_model: str, system_prompt: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream(
                    "POST",
                    f"{ollama_endpoint.rstrip('/')}/api/create",
                    json={
                        "name": name,
                        "from": base_model,
                        "system": system_prompt,
                        "parameters": {
                            "temperature": 0.7,
                            "num_ctx": 8192,
                        },
                    },
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        logger.error("ollama create %s: HTTP %s — %s", name, resp.status_code, body.decode()[:200])
                        return False
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            import json as _json
                            data = _json.loads(line)
                            if data.get("error"):
                                logger.error("ollama create %s error: %s", name, data["error"])
                                return False
                            if data.get("status") == "success":
                                return True
                        except Exception:
                            continue
            return True
        except (httpx.ConnectError, httpx.ConnectTimeout):
            print(f"\n  ⚠️  Could not connect to Ollama at {ollama_endpoint}. Is it running?", flush=True)
            return False
        except Exception as e:
            logger.error("Failed to create %s: %s", name, e)
            return False

    logger.info("Building curatarr-curator from %s...", base_curator)
    results["curator"] = await create_model(
        "curatarr-curator", base_curator, CURATOR_SYSTEM_PROMPT
    )

    logger.info("Building curatarr-summarizer from %s...", base_summarizer)
    results["summarizer"] = await create_model(
        "curatarr-summarizer", base_summarizer, SUMMARIZER_SYSTEM_PROMPT
    )

    # ── Step 3 (optional): the deletion-run pitcher bake ─────────────────────
    # Same persona prompt as the curator ON PURPOSE (one voice, v1 — observe
    # live before adding pitcher-specific rules). Pull + create are isolated:
    # a missing/failed pitcher leaves curator/summarizer results untouched.
    if base_pitcher:
        if await model_exists(ollama_endpoint, base_pitcher):
            print(f"  ✓  {base_pitcher} already present — skipping pull", flush=True)
            pulled = True
        else:
            pulled = await pull_ollama_model(ollama_endpoint, base_pitcher)
            if not pulled:
                print(f"  ⚠️  Pull failed for pitcher base {base_pitcher} — "
                      "deletion runs will fall back to the curator bake", flush=True)
        if pulled:
            logger.info("Building curatarr-pitcher from %s...", base_pitcher)
            results["pitcher"] = await create_model(
                "curatarr-pitcher", base_pitcher, CURATOR_SYSTEM_PROMPT
            )
        else:
            results["pitcher"] = False

    return results


# ── SETUP QUESTIONS SCHEMA ────────────────────────────────────────────────────

SETUP_FIELDS = [
    {
        "id": "plex_url",
        "label": "Plex Server URL",
        "placeholder": "http://192.168.1.100:32400",
        "required": True,
        "help": "The local address of your Plex Media Server.",
        "test": "plex",
        "category": "plex",
    },
    {
        "id": "plex_token",
        "label": "Plex Auth Token",
        "placeholder": "xxxxxxxxxxxxxxxxxxxx",
        "required": True,
        "help": "Find it at plex.tv → Account → Plex Media Server → your server → (i) → XML.",
        "category": "plex",
        "secret": True,
    },
    {
        "id": "ollama_endpoint",
        "label": "Ollama Endpoint",
        "placeholder": "http://localhost:11434",
        "required": True,
        "default": "http://localhost:11434",
        "help": "Where Ollama is running. Usually localhost:11434.",
        "test": "ollama",
        "category": "ollama",
    },
    {
        "id": "base_curator_model",
        "label": "Curator base model",
        "placeholder": "gemma4:31b",
        "required": True,
        "default": "gemma4:31b",
        "help": "Large model for chat and recommendations. Must be pulled in Ollama.",
        "category": "ollama",
        "type": "model_select",
    },
    {
        "id": "base_summarizer_model",
        "label": "Summarizer base model",
        "placeholder": "granite4.1:8b",
        "required": True,
        "default": "granite4.1:8b",
        "help": "Fast model for metadata structuring. granite4.1:8b recommended — see docs/BENCHMARKS.md.",
        "category": "ollama",
        "type": "model_select",
    },
    {
        "id": "tmdb_api_key",
        "label": "TMDB API Key",
        "placeholder": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "required": False,
        "help": "Free at themoviedb.org/settings/api. Needed for movie + series metadata.",
        "used_for": ["movie", "show"],
        "category": "metadata",
        "secret": True,
    },
    {
        "id": "omdb_api_key",
        "label": "OMDB API Key",
        "placeholder": "xxxxxxxx",
        "required": False,
        "help": "Optional. Free at omdbapi.com (1000 req/day). Adds Rotten Tomatoes scores, awards, and richer plot descriptions.",
        "used_for": ["movie", "show"],
        "category": "metadata",
        "secret": True,
    },
    {
        "id": "lastfm_api_key",
        "label": "Last.fm API Key",
        "placeholder": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "required": False,
        "help": "Free at last.fm/api. Needed for music tags and similar artist data. MusicBrainz works without a key.",
        "used_for": ["music"],
        "category": "metadata",
        "secret": True,
    },
    {
        "id": "spotify_client_id",
        "label": "Spotify Client ID",
        "placeholder": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "required": False,
        "help": "Free at developer.spotify.com → Create App. Enables Spotify genre enrichment — no user login needed (Client Credentials flow).",
        "used_for": ["music"],
        "category": "metadata",
        "test": "spotify",
    },
    {
        "id": "spotify_client_secret",
        "label": "Spotify Client Secret",
        "placeholder": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "required": False,
        "help": "From the same Spotify Developer App as the Client ID above.",
        "used_for": ["music"],
        "category": "metadata",
        "secret": True,
    },
    {
        "id": "soulsync_url",
        "label": "SoulSync URL",
        "placeholder": "http://192.168.1.100:12279",
        "required": False,
        "help": "Optional. A SoulSync instance on your LAN adds a second music-metadata opinion. Read-only — Curatarr never triggers its downloads.",
        "used_for": ["music"],
        "category": "metadata",
    },
    {
        "id": "soulsync_api_key",
        "label": "SoulSync API Key",
        "placeholder": "xxxxxxxxxxxxxxxxxxxx",
        "required": False,
        "category": "metadata",
        "secret": True,
    },
    {
        "id": "radarr_url",
        "label": "Radarr URL",
        "placeholder": "http://192.168.1.100:7878",
        "required": False,
        "help": "Only needed for movie library management and deletion proposals.",
        "used_for": ["movie"],
        "category": "arr",
    },
    {
        "id": "radarr_api_key",
        "label": "Radarr API Key",
        "placeholder": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "required": False,
        "category": "arr",
        "secret": True,
    },
    {
        "id": "sonarr_url",
        "label": "Sonarr URL",
        "placeholder": "http://192.168.1.100:8989",
        "required": False,
        "help": "Needed for TV series and anime library management.",
        "used_for": ["show", "anime"],
        "category": "arr",
    },
    {
        "id": "sonarr_api_key",
        "label": "Sonarr API Key",
        "placeholder": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "required": False,
        "category": "arr",
        "secret": True,
    },
    {
        "id": "lidarr_url",
        "label": "Lidarr URL",
        "placeholder": "http://192.168.1.100:8686",
        "required": False,
        "help": "Needed for music library management.",
        "used_for": ["music"],
        "category": "arr",
    },
    {
        "id": "lidarr_api_key",
        "label": "Lidarr API Key",
        "placeholder": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "required": False,
        "category": "arr",
        "secret": True,
    },
]
