"""tv-domain epoch: normalized writes + unioned reads.

External eval catch: the old sync let TMDB's raw media_type 'tv' become
the chroma quarantine key — 995 series docs (all watched history) lived
outside every domain='show' filter; show taste calibrated against the
289 arr-era docs only. Owner call 2026-08-18: assign correctly TODAY
instead of union-reading history forever → scripts/migrate_tv_domain.py
migrated 995 parents (922 show / 73 anime; 480 via live Sonarr, 515 via
genre fallback) + 5,900 facet points, 0 remaining. This suite guards
the DURABLE parts: no writer can mint 'tv' again, and domain filters
keep the 'show'+'tv' union as a safety net.

    python tests/test_tv_domain.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


from src.services.media_enricher import _domain_for_write
from src.vector_store.chromadb_wrapper import domain_where

# ── writer normalization ─────────────────────────────────────────────────────

check("'tv' + anime genres -> anime",
      _domain_for_write("tv", ["Animation", "Anime", "Action"]) == "anime")
check("'tv' + western genres -> show (the Batman Beyond shape)",
      _domain_for_write("tv", "Animation, Action & Adventure, Sci-Fi & Fantasy") == "show")
check("real domains pass through untouched",
      _domain_for_write("movie", []) == "movie"
      and _domain_for_write("music", None) == "music"
      and _domain_for_write("anime", []) == "anime"
      and _domain_for_write(None, None) == "movie")

# ── read-side union (safety net after the migration) ─────────────────────────

check("show unions the legacy tv epoch",
      domain_where("show") == {"domain": {"$in": ["show", "tv"]}})
check("other domains filter plainly; None stays None",
      domain_where("anime") == {"domain": "anime"}
      and domain_where(None) is None)

# ── wiring ───────────────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
me = (root / "src/services/media_enricher.py").read_text(encoding="utf-8")
check("BOTH chroma writers derive domain via _domain_for_write "
      "(raw media_type never reaches the quarantine key again)",
      me.count("_domain_for_write(") >= 3)   # def + two writer sites

ss = (root / "src/services/semantic_search.py").read_text(encoding="utf-8")
check("all semantic-search domain filters route through domain_where",
      ss.count("where=_domain_where(domain)") == 4
      and '{"domain": domain}' not in ss)

fi = (root / "src/services/facet_index.py").read_text(encoding="utf-8")
check("facet probe routes through domain_where",
      "where=domain_where(domain)" in fi)

cw = (root / "src/vector_store/chromadb_wrapper.py").read_text(encoding="utf-8")
check("taste calibration (embeddings_for_domain) uses domain_where",
      "self.collection.get(where=domain_where(domain)" in cw)

check("migration script exists for provenance",
      (root / "scripts/migrate_tv_domain.py").exists())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
