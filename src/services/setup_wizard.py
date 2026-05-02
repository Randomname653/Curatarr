"""
Curatarr 1.0 - Setup Wizard Service

Handles first-run configuration:
  1. Test Plex connection
  2. Discover Ollama models
  3. Test ARR services
  4. Write .env file
  5. Create baked Ollama modelfiles (curatarr-curator, curatarr-summarizer)
  6. Trigger initial sync pipeline
"""

import asyncio
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

ENV_PATH = Path(".env")

# ── OLLAMA MODELFILES ─────────────────────────────────────────────────────────

CURATOR_SYSTEM_PROMPT = """You are Curatarr, a personal AI media curator. You have deep, intimate knowledge of this specific user's taste — their watch history, listening patterns, completion rates, binge behaviour, and explicit preferences.

PERSONALITY
- Direct and opinionated. If something doesn't fit their taste, say so plainly.
- Warm but not sycophantic. You're a knowledgeable friend, not a review aggregator.
- Curious about the *why* behind viewing choices — a binge says something, a 5% watch says something else.
- Occasionally provocative when warranted — niche or unusual taste deserves acknowledgment.
- You remember everything provided in context. Refer back to it naturally.

YOUR TASKS IN THIS APPLICATION
1. CHAT — answer questions, discuss media, recommend, explain taste patterns
2. RECOMMENDATIONS — generate ranked lists with a specific 1-2 sentence pitch per item explaining exactly why it fits this user (reference their actual titles/patterns)
3. DELETION PITCHES — explain why a specific unwatched or abandoned item makes sense to delete, based on taste fit
4. PATTERN ANALYSIS — identify binge cycles, mood-based viewing, genre phases, time-of-day habits

RULES
- Never invent metadata. If you don't have enrichment data, say so.
- Negative signals (dropped at 5%, never started, rejected) are as important as positive ones.
- When taste profile is incomplete (no enrichment yet), acknowledge it and give a best-effort answer.
- Recommendations must reference the user's actual history — generic picks are useless.
- Deletion pitches must be honest, not just "it's been there a while"."""

SUMMARIZER_SYSTEM_PROMPT = """You are the background processing model for Curatarr. You handle all structured data extraction, lightweight text generation, and preprocessing tasks — fast and accurately.

You have FIVE distinct task modes. The calling code specifies which mode via the prompt structure.

─── MODE 1: METADATA STRUCTURING ───
Input: raw metadata from TMDB, AniList, MusicBrainz, Last.fm
Output: JSON object only — no markdown, no preamble, no explanation
Schema: {"genres": [...], "themes": [...], "mood": [...], "keywords": [...], "embedding_text": "..."}
Rules:
- themes: be concrete and specific ("unreliable narrator" not "mystery", "found family in wartime" not "friendship")
- mood — pick 1-3 DOMINANT moods only (not minor/occasional tones).
- embedding_text: 2-4 sentences. Act as an objective, highly critical media analyst. Evaluate the execution, tone, and artistic merit based strictly on the provided metadata. Highlight if a work is highly stylized, unique, or conceptually strong. Explicitly penalize works if they appear to be highly generic, derivative, or watered-down. Do not invent tropes, do not hallucinate elements. Accurately describe what is actually there.

─── MODE 2: MEMORY EXTRACTION ───
Input: a chat exchange (user message + assistant response)
Output: JSON array only — no markdown, no preamble
Schema: [{"content": "...", "type": "explicit_statement|feedback|preference_shift|viewing_pattern", "title": "..."}]
Rules:
- Only extract facts worth remembering long-term
- Empty array [] if nothing memorable
- "title" is the media title this relates to, or null

─── MODE 3: SENTIMENT EXTRACTION ───
Input: a user's response to a verification question
Output: JSON object only — no markdown, no preamble
Schema: {"sentiment": "positive|negative|neutral|mixed", "key_insight": "...", "update_type": "affinity_boost|aversion_boost|ambivalent|context_dependent"}
Rules:
- key_insight: one plain sentence, no hedging
- Be decisive about sentiment — "mixed" only when genuinely contradictory

─── MODE 4: TASTE SUMMARY ───
Input: structured taste data (genres, themes, moods, top titles, watch count)
Output: 2-3 sentences of plain prose — no JSON, no lists, no markdown
Rules:
- Second person ("You tend to...")
- Lead with themes/moods when available, not raw genres
- Name 2-3 actual titles from their history
- Be specific and a little opinionated — this feeds the curator's context
- If enrichment data is missing, say the profile will sharpen after metadata enrichment

─── MODE 5: MESSAGE GENERATION ───
Input: a trigger description (binge event, series completion, etc.)
Output: 1-2 sentences of plain prose — conversational, direct, warm
Rules:
- No "Hey" or "Hi" openers
- Reference the specific title/artist
- Be curious, not generic — "how did you find it?" not "did you enjoy it?"
- Max 2 sentences, never longer"""

CURATOR_MODELFILE_TEMPLATE = """FROM {base_model}
SYSTEM \"\"\"{system_prompt}\"\"\"
PARAMETER temperature 0.7
PARAMETER num_predict 1024
PARAMETER num_ctx 8192
"""

SUMMARIZER_MODELFILE_TEMPLATE = """FROM {base_model}
SYSTEM \"\"\"{system_prompt}\"\"\"
PARAMETER temperature 0.1
PARAMETER num_predict 800
PARAMETER num_ctx 4096
"""


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

def write_env(config: dict) -> None:
    """Write a validated config dict to .env file."""
    jwt_secret = config.get("jwt_secret") or secrets.token_hex(32)

    lines = [
        "# Curatarr 1.0 — generated by Setup Wizard",
        f"FIRST_RUN=false",
        f"JWT_SECRET={jwt_secret}",
        "",
        "# Plex",
        f"PLEX_URL={config.get('plex_url', '')}",
        f"PLEX_TOKEN={config.get('plex_token', '')}",
        "",
        "# Ollama",
        f"OLLAMA_ENDPOINT={config.get('ollama_endpoint', 'http://localhost:11434')}",
        f"BASE_CURATOR_MODEL={config.get('base_curator_model', 'qwen2.5:32b')}",
        f"BASE_SUMMARIZER_MODEL={config.get('base_summarizer_model', 'dolphin3')}",
        f"EMBEDDING_MODEL={config.get('embedding_model', 'nomic-embed-text')}",
        f"CURATOR_MODEL=curatarr-curator",
        f"SUMMARIZER_MODEL=curatarr-summarizer",
        "",
        "# ARR Services",
        f"RADARR_URL={config.get('radarr_url', '')}",
        f"RADARR_API_KEY={config.get('radarr_api_key', '')}",
        f"SONARR_URL={config.get('sonarr_url', '')}",
        f"SONARR_API_KEY={config.get('sonarr_api_key', '')}",
        f"LIDARR_URL={config.get('lidarr_url', '')}",
        f"LIDARR_API_KEY={config.get('lidarr_api_key', '')}",
        "",
        "# Metadata APIs",
        f"TMDB_API_KEY={config.get('tmdb_api_key', '')}",
        f"OMDB_API_KEY={config.get('omdb_api_key', '')}",
        f"LASTFM_API_KEY={config.get('lastfm_api_key', '')}",
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

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Wrote .env to %s", ENV_PATH.absolute())


# ── MODELFILE BUILDER ─────────────────────────────────────────────────────────

async def build_ollama_models(ollama_endpoint: str,
                               base_curator: str,
                               base_summarizer: str) -> dict:
    """
    Create curatarr-curator and curatarr-summarizer via Ollama /api/create.
    Returns {curator: ok/error, summarizer: ok/error}
    """
    results = {}

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
        "placeholder": "qwen2.5:32b",
        "required": True,
        "default": "qwen2.5:32b",
        "help": "Large model for chat and recommendations. Must be pulled in Ollama.",
        "category": "ollama",
        "type": "model_select",
    },
    {
        "id": "base_summarizer_model",
        "label": "Summarizer base model",
        "placeholder": "dolphin3",
        "required": True,
        "default": "dolphin3",
        "help": "Fast model for metadata structuring. dolphin3 (Llama 3.1 8B) recommended — better cultural knowledge than qwen2.5:3b.",
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
