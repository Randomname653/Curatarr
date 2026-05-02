"""
Curatarr 1.0 - Recommendations Engine

Uses taste vectors + LLM to generate personalised recommendations
with a written pitch per item, organised by category.

Features:
  - Cache-Persistenz: Ergebnisse werden in der DB gespeichert.
  - Force Refresh: Manuelle Neugenerierung via UI möglich.
  - Kontext-bewusste Empfehlungen aus Library oder Entdeckung.
"""

import json
import logging
from datetime import datetime
from typing import Optional

import httpx
import numpy as np

from src.config import settings
from src.database.connection import get_db_session
from src.database.models import (
    TasteVectorEntry, WatchHistoryEntry, User, CachedRecommendation,
    EncryptedTasteVector, ProtectedMedia
)
from src.vector_store.chromadb_wrapper import chroma_db

logger = logging.getLogger(__name__)

CATEGORY_LABELS = {
    "music": "🎵 Music",
    "movie": "🎬 Movies",
    "show":  "📺 TV Shows",
    "anime": "⛩️ Anime",
}


async def _call_llm(prompt: str, max_tokens: int = 800) -> Optional[str]:
    """Call curator model, fall back to base model."""
    for model in [settings.CURATOR_MODEL, settings.BASE_CURATOR_MODEL]:
        if not model:
            continue
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(
                    f"{settings.effective_ollama}/api/chat",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        "options": {"temperature": 0.7, "num_predict": max_tokens},
                    },
                )
            if r.status_code == 200:
                content = r.json().get("message", {}).get("content", "").strip()
                logger.debug("LLM response (%d chars) from %s", len(content), model)
                return content
            logger.warning("LLM HTTP %s from model %s", r.status_code, model)
            if r.status_code == 404:
                continue
        except httpx.TimeoutException:
            logger.warning("LLM timeout on model %s", model)
        except Exception as e:
            logger.warning("LLM call failed (%s): %s", type(e).__name__, e)
    return None


async def generate_recommendations(
    user_id: int,
    category: str = None,
    limit: int = 10,
    arr_library: list = None,  # list of {title, genres, year, ...} from ARR
    force_refresh: bool = False # Ermöglicht das Umgehen des Caches
) -> list:
    """
    Generate recommendations with LLM pitch.
    If arr_library is provided, recommend from those items.
    Otherwise ask the LLM to suggest based on taste alone.
    """
    
    # 1. Datenbank-Sitzung für Cache-Check öffnen
    with get_db_session() as db:
        if not force_refresh:
            cache_q = db.query(CachedRecommendation).filter(
                CachedRecommendation.user_id == user_id
            )
            if category:
                cache_q = cache_q.filter(CachedRecommendation.category == category)
            
            cached_items = cache_q.all()
            if cached_items:
                logger.info("Loading recommendations from cache for user %d", user_id)
                return [
                    {
                        "title": c.title,
                        "reason": c.reason,
                        "confidence": c.confidence,
                        "genres": c.genres,
                        "category": c.category,
                        "category_label": CATEGORY_LABELS.get(c.category, c.category)
                    } for c in cached_items
                ]

        # 2. Kein Cache vorhanden oder Refresh erzwungen: Taste Context sammeln
        tv = db.query(TasteVectorEntry).filter(
            TasteVectorEntry.user_id == user_id
        ).first()
        if not tv:
            return []

        type_data = json.loads(tv.genre_affinity or "{}")
        summary_text = tv.summary_text or ""

        watched_q = db.query(WatchHistoryEntry.series_title, WatchHistoryEntry.title).filter(
            WatchHistoryEntry.user_id == user_id
        )
        if category:
            watched_q = watched_q.filter(WatchHistoryEntry.media_type == category)
        watched = {r.series_title or r.title for r in watched_q.all()}

    # --- Generierungs-Logik ---
    cats = [category] if category else list(type_data.keys())
    all_recs = []

    for cat in cats:
        ts = type_data.get(cat)
        if not ts or not isinstance(ts, dict):
            continue

        top_genres = list((ts.get("genre_affinity") or {}).keys())[:6]
        top_themes = list((ts.get("themes") or {}).keys())[:5]
        top_moods = list((ts.get("moods") or {}).keys())[:4]
        top_titles = ts.get("top_titles", [])[:8]

        import re
        match = re.search(rf'\[{cat.upper()}\]([^\[]*)', summary_text)
        cat_summary = match.group(1).strip() if match else ""

        if arr_library:
            unwatched = [
                item for item in arr_library
                if item.get("title") not in watched
            ][:50]

            if not unwatched:
                continue

            items_text = "\n".join(
                f"- {i['title']} ({i.get('year', '?')}) — {i.get('genres', '')}"
                for i in unwatched[:30]
            )

            prompt = f"""You are Curatarr, a personal media curator. Recommend items for this user.

USER'S {cat.upper()} TASTE:
{cat_summary or f"Top genres: {', '.join(top_genres)}. Often watches: {', '.join(top_titles[:5])}."}

AVAILABLE {cat.upper()} LIBRARY (unwatched):
{items_text}

Pick the best {min(limit, 5)} recommendations from the library above.
For each, write ONE sentence explaining specifically why it fits this user's taste.
Reference their actual viewing history when relevant.

Output as JSON array only:
[{{"title": "...", "reason": "...", "confidence": 0.0-1.0}}]"""

        else:
            noun = "tracks/artists" if cat == "music" else "titles"
            prompt = f"""You are Curatarr, a personal media curator.

USER'S {cat.upper()} TASTE:
{cat_summary or f"Top genres: {', '.join(top_genres)}. Often watches: {', '.join(top_titles[:5])}."}

Already watched/listened to: {', '.join(list(watched)[:15])}

Suggest {limit} {cat} {noun} they haven't seen/heard yet.
Each needs a specific 1-sentence pitch referencing what you know about their taste.
Be direct and opinionated.

Output as JSON array only:
[{{"title": "...", "reason": "...", "confidence": 0.0-1.0, "genres": "..."}}]"""

        response = await _call_llm(prompt, max_tokens=600)
        if not response:
            continue

        try:
            text = response
            if "```" in text:
                text = text.split("```")[1].lstrip("json").strip()
            recs = json.loads(text)
            if not isinstance(recs, list):
                continue
            for rec in recs:
                rec["category"] = cat
                rec["category_label"] = CATEGORY_LABELS.get(cat, cat)
                all_recs.append(rec)
        except Exception as e:
            logger.debug("Recommendation parse error: %s", e)

    # 3. Ergebnisse in Cache speichern
    if all_recs:
        with get_db_session() as db:
            # Alten Cache für diese Auswahl löschen
            del_q = db.query(CachedRecommendation).filter(
                CachedRecommendation.user_id == user_id
            )
            if category:
                del_q = del_q.filter(CachedRecommendation.category == category)
            del_q.delete()

            # Neue Einträge speichern
            for r in all_recs:
                db.add(CachedRecommendation(
                    user_id=user_id,
                    category=r.get("category"),
                    title=r.get("title"),
                    reason=r.get("reason"),
                    confidence=r.get("confidence", 0.7),
                    genres=r.get("genres", "")
                ))
            db.commit()

    return all_recs


async def generate_deletion_proposals(
    user_id: int,
    arr_items: list,
    category: str = "movie",
) -> list:
    """
    Surgical Deletion: Identifiziert Müll, schützt Klassiker und beachtet Whitelists.
    """
    with get_db_session() as db:
        # 1. Taste-Vektor laden
        tv = db.query(TasteVectorEntry).filter(TasteVectorEntry.user_id == user_id).first()
        user_vector = None
        taste_blurb = ""
        if tv:
            taste_blurb = (tv.summary_text or "")[:400]
            encrypted = db.query(EncryptedTasteVector).filter(
                EncryptedTasteVector.user_id == user_id,
                EncryptedTasteVector.media_category == category
            ).first()
            if encrypted and encrypted.encrypted_blob:
                user_vector = json.loads(encrypted.encrypted_blob).get("embedding")

        # 2. Whitelist laden
        protected = {
            p.identifier for p in db.query(ProtectedMedia).filter(ProtectedMedia.user_id == user_id).all()
        }

    scored_candidates = []

    for item in arr_items:
        title = item.get("title")
        tmdb_id = str(item.get("tmdb_id")) if item.get("tmdb_id") is not None else ""
        if title in protected or tmdb_id in protected:
            continue

        rating = item.get("ratings", {}).get("value", 0) or item.get("vote_average", 0)
        size_gb = (item.get("size_mb", 0) or 0) / 1024

        doc_id = str(item.get("plex_rating_key") or item.get("tmdb_id") or title)
        item_vector_res = chroma_db.get_by_id(doc_id)

        distance_penalty = 0.5
        if item_vector_res and item_vector_res.get("embedding") and user_vector:
            try:
                item_vec = item_vector_res["embedding"]
                dist = np.dot(user_vector, item_vec)
                distance_penalty = max(0.0, min(1.0, 1.0 - float(dist)))
            except Exception:
                distance_penalty = 0.5

        del_score = (size_gb * 5) + (distance_penalty * 100) - (rating * 5)

        if del_score > 30:
            scored_candidates.append({
                "item": item,
                "score": del_score,
                "mismatch": distance_penalty,
                "rating": rating,
            })

    scored_candidates.sort(key=lambda x: x["score"], reverse=True)
    final_proposals = []

    for cand in scored_candidates[:10]:
        item = cand["item"]
        prompt = f"""[MODE: SURGICAL DELETION PITCH]
You are Curatarr, an uncompromising, elite media curator. Pitch the deletion of this item.

ITEM: {item.get('title')} (Rating: {cand['rating']}/10)
REASON: Mismatch with user taste (mismatch factor: {cand['mismatch']:.2f}/1.0).
{f'USER TASTE: {taste_blurb}' if taste_blurb else ''}

CRITICAL RULES:
1. MAX 2 SENTENCES. Be brutal, concise, and highly opinionated.
2. NEVER start with "Given your..." or "Since you like...". DO NOT narrate the user's taste back to them. Just state why the item fails.
3. If it's a classic with a high rating, acknowledge its status but ruthlessly explain why it still doesn't belong in THIS specific library.
4. DO NOT mention file sizes or gigabytes."""
        pitch = await _call_llm(prompt)
        size_mb = item.get("size_mb") or 0
        final_proposals.append({
            "title": item.get("title"),
            "pitch": pitch,
            "confidence": min(0.99, cand["score"] / 150),
            "size_mb": size_mb,
            "size_gb": round(size_mb / 1024, 1),
            "arr_id": item.get("arr_id"),
            "service": item.get("service", ""),
            "arr_url": item.get("arr_url", ""),
        })

    return final_proposals


async def score_arr_items(user_id: int, category: str, items: list, top_n: int = 50) -> list:
    """Pre-filter ARR items by genre affinity before sending to LLM."""
    if len(items) <= top_n: return items
    with get_db_session() as db:
        tv = db.query(TasteVectorEntry).filter(TasteVectorEntry.user_id == user_id).first()
        if not tv: return items[:top_n]
        type_data = json.loads(tv.genre_affinity or "{}")

    ts = type_data.get(category, {})
    genre_affinity = {g.lower(): s for g, s in (ts.get("genre_affinity") or {}).items()}
    if not genre_affinity: return items[:top_n]

    def score_item(item: dict) -> float:
        genres = [g.strip().lower() for g in (item.get("genres") or "").split(",") if g.strip()]
        return sum(genre_affinity.get(g, 0) for g in genres) + (0.1 if item.get("monitored") else 0)

    return sorted(items, key=score_item, reverse=True)[:top_n]