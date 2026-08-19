"""One-off migration: legacy domain='tv' chroma docs → 'show' / 'anime'.

The old sync epoch let TMDB's raw media_type 'tv' become the chroma
quarantine key: 995 series docs — including everything the household
actually watched — lived outside every domain='show' filter (taste
calibration ran on the 289 arr-era docs only; external eval catch).
The owner's call (2026-08-18): resolve TODAY's correct assignment and
write it, instead of union-reading the historical value forever.

Classification: live Sonarr entry via classify_sonarr_category (the
single source of truth: seriesType OR Anime genre tag); docs whose
series left Sonarr fall back to the same genre heuristic on the doc's
stored genres. Facet points inherit their parent's new domain.

MUST run with the Curatarr app STOPPED (chroma process lock).

    python scripts/migrate_tv_domain.py
"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.arr_client import classify_sonarr_category  # noqa: E402


def _sonarr_series() -> dict:
    env = {}
    with open(Path(__file__).resolve().parents[1] / ".env", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    req = urllib.request.Request(
        f"{env['SONARR_URL'].rstrip('/')}/api/v3/series",
        headers={"X-Api-Key": env["SONARR_API_KEY"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        return {s["id"]: s for s in json.loads(r.read())}


def _fallback_domain(genres_str: str) -> str:
    return "anime" if "anime" in (genres_str or "").lower() else "show"


def main() -> int:
    try:
        from src.vector_store.chromadb_wrapper import get_chroma_db
        chroma = get_chroma_db()
    except Exception as e:
        print(f"ABORT: cannot open chroma — is the app still running? ({e})")
        return 1

    series = _sonarr_series()
    print(f"live sonarr series: {len(series)}")
    if len(series) < 50:
        print("ABORT: suspiciously small Sonarr list — refusing to classify against it.")
        return 1

    # ── parents ──────────────────────────────────────────────────────────────
    moved = {"show": 0, "anime": 0}
    via = {"sonarr": 0, "genre-fallback": 0}
    parent_domain: dict = {}
    _last_first = None
    while True:
        # Self-consuming pagination: updated docs stop matching the where,
        # so each get() returns the next still-'tv' batch.
        page = chroma.collection.get(where={"domain": "tv"},
                                     include=["metadatas"], limit=500)
        ids = page.get("ids") or []
        if not ids:
            break
        if ids[0] == _last_first:
            print("ABORT: update did not take effect (same page twice) — "
                  "stopping to avoid an infinite loop.")
            return 2
        _last_first = ids[0]
        for doc_id, md in zip(ids, page.get("metadatas") or []):
            md = dict(md or {})
            sid = None
            if str(doc_id).startswith("sonarr:"):
                tail = str(doc_id).split(":", 1)[1]
                sid = int(tail) if tail.isdigit() else None
            if sid is not None and sid in series:
                new_dom = classify_sonarr_category(series[sid])
                via["sonarr"] += 1
            else:
                new_dom = _fallback_domain(md.get("genres", ""))
                via["genre-fallback"] += 1
            md["domain"] = new_dom
            md["media_type"] = new_dom
            chroma.collection.update(ids=[doc_id], metadatas=[md])
            parent_domain[str(doc_id)] = new_dom
            moved[new_dom] += 1
        print(f"  migrated {sum(moved.values())} parents so far …")

    # ── facet points inherit the parent's new domain ─────────────────────────
    facets_moved = 0
    fc = chroma._facets_collection()
    _last_first = None
    while True:
        page = fc.get(where={"domain": "tv"}, include=["metadatas"], limit=500)
        ids = page.get("ids") or []
        if not ids:
            break
        if ids[0] == _last_first:
            print("ABORT: facet update did not take effect — stopping.")
            return 2
        _last_first = ids[0]
        for fid, md in zip(ids, page.get("metadatas") or []):
            md = dict(md or {})
            parent = str(md.get("parent") or "")
            md["domain"] = parent_domain.get(
                parent, _fallback_domain(md.get("genres", "")))
            fc.update(ids=[fid], metadatas=[md])
            facets_moved += 1
        print(f"  migrated {facets_moved} facet points so far …")

    remaining = len((chroma.collection.get(where={"domain": "tv"},
                                           include=[], limit=5).get("ids")) or [])
    f_remaining = len((fc.get(where={"domain": "tv"},
                              include=[], limit=5).get("ids")) or [])
    print(f"\nDONE: parents -> show={moved['show']} anime={moved['anime']} "
          f"(via sonarr={via['sonarr']}, genre-fallback={via['genre-fallback']}); "
          f"facets moved={facets_moved}")
    print(f"remaining domain='tv': parents={remaining} facets={f_remaining}")
    return 0 if remaining == 0 and f_remaining == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
