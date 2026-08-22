"""
Curatarr - Encrypted Taste Vector Service

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
    """Decrypt an AES-256-GCM blob. Returns None on failure.

    v0 note (eval 1.9): the blobs the app ACTUALLY writes in Phase A store
    their fields top-level ({"embedding": …, "version": 0}) — only the
    dev-mode encrypt fallback ever produced the {"plaintext": …} wrapper.
    Parse both, and never let a shape mismatch raise instead of the
    documented None."""
    if blob.get("version") == 0:
        try:
            if "plaintext" in blob:
                return json.loads(blob["plaintext"])
            return blob
        except Exception as e:
            logger.warning("v0 blob parse failed: %s", e)
            return None
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


# ── TASTE BLOB CONCURRENCY ────────────────────────────────────────────────────
# (The old aspirational "schema_version 2" template — never produced by any
# writer, never consumed by any reader — is gone; the REAL blob shape is
# whatever taste_engine's rebuild writes plus the feedback fields merged in.)

def cas_update_blob(db, etv_id: int, old_rev: int, blob: dict) -> bool:
    """Optimistic write for the taste blob (eval 1.4). The blob is a
    read-modify-write shared by the minutes-long taste rebuild and the
    chat-feedback mergers — SQLite serialises the WRITES, not our RMW
    cycle, so a late writer could silently clobber the other side's work.
    Writes only if the stored ``rev`` still matches what the caller read;
    on a miss the caller re-reads and re-merges. Caller commits."""
    from sqlalchemy import func

    from src.database.models import EncryptedTasteVector
    blob = dict(blob)
    blob["rev"] = int(old_rev) + 1
    n = (db.query(EncryptedTasteVector)
         .filter(EncryptedTasteVector.id == etv_id,
                 func.coalesce(func.json_extract(
                     EncryptedTasteVector.encrypted_blob, "$.rev"), 0)
                 == int(old_rev))
         .update({"encrypted_blob": json.dumps(blob)},
                 synchronize_session=False))
    return bool(n)


# ── VECTOR COMPUTATION ────────────────────────────────────────────────────────

def compute_temporal_patterns(entries: list) -> dict:
    """Compute time-of-day and day-of-week viewing distributions."""
    time_buckets = {"night": 0, "morning": 0, "afternoon": 0, "evening": 0}
    day_buckets = {str(i): 0 for i in range(7)}
    total = len(entries)

    for e in entries:
        # accepts both ORM rows and the plain dicts taste_engine works with
        viewed_at = e.get("viewed_at") if isinstance(e, dict) \
            else getattr(e, "viewed_at", None)
        if not viewed_at:
            continue
        h = viewed_at.hour
        if h < 6:
            time_buckets["night"] += 1
        elif h < 12:
            time_buckets["morning"] += 1
        elif h < 18:
            time_buckets["afternoon"] += 1
        else:
            time_buckets["evening"] += 1
        day_buckets[str(viewed_at.weekday())] += 1

    if total > 0:
        time_dist = {k: round(v / total, 3) for k, v in time_buckets.items()}
        day_dist = {k: round(v / total, 3) for k, v in day_buckets.items()}
    else:
        time_dist = {k: 0.0 for k in time_buckets}
        day_dist = {k: 0.0 for k in day_buckets}

    return {"time_of_day_dist": time_dist, "day_of_week_dist": day_dist}


# (detect_binges_from_history is gone — it was a dead, already-diverged
# duplicate of the inline binge detection in taste_engine, eval 1.11.)


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
    bump = 0.15 * min(weight, 2.0)
    if feedback.get("sentiment") == "negative":
        aversion = vector.get("theme_aversion", {})
        for aspect in feedback.get("genre_aspects", []):
            aversion[aspect] = min(1.0, aversion.get(aspect, 0) + bump)
        # Key cap: free-text keys accumulated unbounded (unlike the [-100:]
        # and [-50:] lists right here) — keep the strongest 20.
        vector["theme_aversion"] = dict(
            sorted(aversion.items(), key=lambda x: -x[1])[:20])
        if feedback.get("title"):
            disliked = vector.get("disliked_titles", [])
            if feedback["title"] not in disliked:
                disliked.append(feedback["title"])
            vector["disliked_titles"] = disliked[-50:]
    elif feedback.get("sentiment") == "positive":
        # Latest statement wins BOTH ways: praise takes the title off the
        # dislike list and walks the named aversions back down — before
        # this, a dislike was a one-way ratchet no amount of later praise
        # could undo.
        title = feedback.get("title")
        if title:
            vector["disliked_titles"] = [t for t in vector.get("disliked_titles", [])
                                         if t != title]
        aversion = vector.get("theme_aversion", {})
        for aspect in feedback.get("genre_aspects", []):
            if aspect in aversion:
                aversion[aspect] = round(aversion[aspect] - bump, 4)
                if aversion[aspect] <= 0.01:
                    del aversion[aspect]
        vector["theme_aversion"] = aversion

    return vector
