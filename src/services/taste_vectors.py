"""
Curatarr 1.0 - Encrypted Taste Vector Service

AES-256-GCM encryption of taste vectors per user.
Key derivation: PBKDF2(user_pin + plex_user_id, salt, 200000 iterations, SHA-256)

The PIN never leaves the client — the server only stores the encrypted blob.
Even if the DB is stolen, taste vectors are unreadable without the PIN.

Positive and negative vectors are stored separately.
Binge patterns and temporal patterns (time-of-day, day-of-week) are tracked.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)


# ── KEY DERIVATION ────────────────────────────────────────────────────────────

def derive_key(pin: str, plex_user_id: str, salt: bytes) -> bytes:
    """Derive AES-256 key from PIN + Plex user ID using PBKDF2."""
    material = f"{pin}:{plex_user_id}".encode("utf-8")
    return hashlib.pbkdf2_hmac(
        "sha256",
        material,
        salt,
        settings.PBKDF2_ITERATIONS,
        dklen=32,
    )


# ── AES-256-GCM ENCRYPTION ────────────────────────────────────────────────────

def encrypt_vector(data: dict, key: bytes) -> dict:
    """Encrypt a dict using AES-256-GCM. Returns {ciphertext, nonce, tag} as hex."""
    try:
        from Crypto.Cipher import AES
        nonce = os.urandom(12)
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)
        return {
            "ciphertext": ciphertext.hex(),
            "nonce": nonce.hex(),
            "tag": tag.hex(),
            "version": 1,
        }
    except ImportError:
        logger.warning("pycryptodome not installed — storing unencrypted (dev mode)")
        return {"plaintext": json.dumps(data), "version": 0}


def decrypt_vector(blob: dict, key: bytes) -> Optional[dict]:
    """Decrypt an AES-256-GCM blob. Returns None on failure."""
    if blob.get("version") == 0:
        return json.loads(blob["plaintext"])
    try:
        from Crypto.Cipher import AES
        cipher = AES.new(
            key, AES.MODE_GCM,
            nonce=bytes.fromhex(blob["nonce"]),
        )
        plaintext = cipher.decrypt_and_verify(
            bytes.fromhex(blob["ciphertext"]),
            bytes.fromhex(blob["tag"]),
        )
        return json.loads(plaintext.decode("utf-8"))
    except Exception as e:
        logger.warning("Vector decryption failed: %s", e)
        return None


# ── TASTE VECTOR SCHEMA ───────────────────────────────────────────────────────
# This is what gets encrypted and stored per user per media_type.

def empty_taste_vector() -> dict:
    return {
        # Positive signals (genre/theme/mood → score 0..1)
        "genre_affinity": {},
        "theme_affinity": {},
        "mood_affinity": {},
        "actor_affinity": {},
        "director_affinity": {},
        "similar_to": [],          # list of titles/artists user seems to like

        # Negative signals
        "genre_aversion": {},
        "theme_aversion": {},
        "mood_aversion": {},
        "disliked_titles": [],

        # Viewing patterns
        "binge_series": [],         # [{series, episodes, duration_h, date}]
        "avg_completion": 1.0,
        "session_durations_h": [],  # rolling last 50

        # Temporal patterns
        "time_of_day_dist": {       # fraction of views per period
            "night": 0.0,           # 0-6h
            "morning": 0.0,         # 6-12h
            "afternoon": 0.0,       # 12-18h
            "evening": 0.0,         # 18-24h
        },
        "day_of_week_dist": {str(i): 0.0 for i in range(7)},

        # Feedback from conversations
        "explicit_feedback": [],    # [{title, sentiment, reason, date}]

        # Meta
        "watch_count": 0,
        "computed_at": None,
        "summary_text": "",
        "schema_version": 2,
    }


# ── VECTOR COMPUTATION ────────────────────────────────────────────────────────

def compute_temporal_patterns(entries: list) -> dict:
    """Compute time-of-day and day-of-week viewing distributions."""
    time_buckets = {"night": 0, "morning": 0, "afternoon": 0, "evening": 0}
    day_buckets = {str(i): 0 for i in range(7)}
    total = len(entries)

    for e in entries:
        if not e.viewed_at:
            continue
        h = e.viewed_at.hour
        if h < 6:
            time_buckets["night"] += 1
        elif h < 12:
            time_buckets["morning"] += 1
        elif h < 18:
            time_buckets["afternoon"] += 1
        else:
            time_buckets["evening"] += 1
        day_buckets[str(e.viewed_at.weekday())] += 1

    if total > 0:
        time_dist = {k: round(v / total, 3) for k, v in time_buckets.items()}
        day_dist = {k: round(v / total, 3) for k, v in day_buckets.items()}
    else:
        time_dist = {k: 0.0 for k in time_buckets}
        day_dist = {k: 0.0 for k in day_buckets}

    return {"time_of_day_dist": time_dist, "day_of_week_dist": day_dist}


def detect_binges_from_history(entries: list) -> list:
    """Find all binge sessions in history."""
    show_entries = [e for e in entries if e.media_type in ("show", "anime") and e.viewed_at]
    show_entries.sort(key=lambda e: e.viewed_at)

    binges = []
    by_series: dict = {}
    for e in show_entries:
        key = e.series_title or e.title
        by_series.setdefault(key, []).append(e)

    for series, eps in by_series.items():
        # Sliding window: find runs of 3+ episodes within BINGE_SESSION_HOURS
        eps.sort(key=lambda e: e.viewed_at)
        i = 0
        while i < len(eps):
            window = [eps[i]]
            j = i + 1
            while j < len(eps):
                span_h = (eps[j].viewed_at - eps[i].viewed_at).total_seconds() / 3600
                if span_h <= settings.BINGE_SESSION_HOURS:
                    window.append(eps[j])
                    j += 1
                else:
                    break
            if len(window) >= settings.BINGE_EPISODE_THRESHOLD:
                duration_h = (window[-1].viewed_at - window[0].viewed_at).total_seconds() / 3600
                binges.append({
                    "series": series,
                    "episodes": len(window),
                    "duration_h": round(duration_h, 1),
                    "date": window[0].viewed_at.isoformat(),
                    "media_type": eps[0].media_type,
                })
                i = j  # skip past this binge window
            else:
                i += 1

    return binges[-20:]  # keep last 20 binges


def merge_feedback_into_vector(vector: dict, feedback: dict) -> dict:
    """
    Merge explicit user feedback (from conversation) into the taste vector.
    feedback = {title, sentiment: positive/negative, genre_aspects: [...],
    reason, weight?, source?} — weight > 1.0 marks elevated signals
    (verdicts on titles Curatarr itself recommended); entries written
    before the field existed read as 1.0.
    """
    weight = float(feedback.get("weight") or 1.0)
    entry = {
        "title": feedback.get("title", ""),
        "sentiment": feedback.get("sentiment", "neutral"),
        "aspects": feedback.get("genre_aspects") or feedback.get("aspects") or [],
        "reason": feedback.get("reason", ""),
        "date": datetime.utcnow().isoformat(),
        "weight": weight,
        "source": feedback.get("source", "chat"),
    }
    vector["explicit_feedback"] = vector.get("explicit_feedback", [])
    vector["explicit_feedback"].append(entry)
    vector["explicit_feedback"] = vector["explicit_feedback"][-100:]  # keep last 100

    # Update aversions based on sentiment. Chat aspects are FREE TEXT from
    # the summarizer ("sound quality", "annoying", "dark narrative") — they
    # belong in theme_aversion. genre_aversion is reserved for the taste
    # engine's computed drop-rate per real genre; mixing chat strings into
    # it produced "genres" like "modern Simpson's seasons".
    if feedback.get("sentiment") == "negative":
        for aspect in feedback.get("genre_aspects", []):
            aversion = vector.get("theme_aversion", {})
            aversion[aspect] = min(1.0, aversion.get(aspect, 0) + 0.15 * min(weight, 2.0))
            vector["theme_aversion"] = aversion
        if feedback.get("title"):
            disliked = vector.get("disliked_titles", [])
            if feedback["title"] not in disliked:
                disliked.append(feedback["title"])
            vector["disliked_titles"] = disliked[-50:]

    return vector
