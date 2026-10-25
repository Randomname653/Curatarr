"""Curatarr — nanogenres: titles whose themes sit together, named once.

The facet index (services/facet_index) keeps about six theme phrases per
title in the vector store ("school setting with supernatural threat"),
nothing aggregated them. Clustering the PHRASES gave broad themes ("urban
life", "humor") with music and video mixed; clustering the TITLES (each the
mean of its own phrases, video and music apart) gave what Letterboxd calls
nanogenres: coming-of-age teen comedies (Superbad, Clueless), serial-killer
thrillers (Zodiac), horrorcore rap (2026-10-25, measured on the real store:
29,630 titles, 340 MB peak, the clustering itself seconds).

* ``run_nanogenres`` (custodian, no model): the first run and every new
  ``VERSION`` or embedding model build the clusters: spherical k-means with
  a fixed seed, about one cluster per ``_PER_CLUSTER`` titles. Every later
  run only assigns titles that are new to the index to the nearest centre,
  so clusters and names stay put.
* ``run_nanogenre_names`` (custodian, the summarizer): names each cluster
  once from its central titles and commonest phrases.
* ``wrapup_sections.nanogenres`` counts the period's played hours per named
  nanogenre.
"""
import json
import logging
from collections import Counter
from datetime import datetime
from typing import Optional

import numpy as np

from src.database.connection import get_db_session

logger = logging.getLogger(__name__)

VERSION = 1
VIDEO = ("movie", "show", "anime", "tv")
_PAGE = 2000
_PER_CLUSTER = 80
_K_MIN, _K_MAX = 8, 250
_SEED = 0
_ITERS = 30
_TOP = 8
_NAME_BUDGET = 40


def group_of(domain: Optional[str]) -> Optional[str]:
    d = (domain or "").lower()
    if d == "music":
        return "music"
    return "video" if d in VIDEO else None


# ── reading the facet index ─────────────────────────────────────────────────

def _fold(rows, sums: dict, meta: list, vecs: list) -> None:
    """Add facet points (doc, metadata, embedding) to their title's sum."""
    for doc, md, emb in rows:
        md = md or {}
        parent = md.get("parent")
        if not parent or group_of(md.get("domain")) is None or emb is None:
            continue
        v = np.asarray(emb, dtype=np.float32)
        n = float(np.linalg.norm(v))
        if n == 0:
            continue
        if parent not in sums:
            sums[parent] = len(meta)
            meta.append({"parent": parent, "title": md.get("title"), "domain": md.get("domain"),
                         "model": md.get("emb_model"), "phrases": [],
                         "genres": [g.strip() for g in str(md.get("genres") or "").split(",") if g.strip()]})
            vecs.append(np.zeros_like(v))
        i = sums[parent]
        vecs[i] += v / n
        meta[i]["phrases"].append((doc or "").strip())


def read_titles(chroma, only_new: Optional[set] = None) -> tuple:
    """(X, meta, model): one unit vector per title (the mean of its phrases),
    {parent, title, domain, model, phrases}, and the embedding model most of
    the index is in. ``only_new``: the parents already assigned — then only
    the others are read with embeddings. Blocking (Chroma) — call via
    ``run_chroma``."""
    sums: dict = {}
    meta: list = []
    vecs: list = []
    offset = 0
    want_ids: list = []
    models: Counter = Counter()
    while True:
        got = chroma.facets_page(offset, _PAGE, embeddings=only_new is None) or {}
        ids = got.get("ids") or []
        if not ids:
            break
        models.update((md or {}).get("emb_model") for md in got.get("metadatas") or [])
        if only_new is None:
            _fold(zip(got.get("documents") or [], got.get("metadatas") or [], got.get("embeddings")),
                  sums, meta, vecs)
        else:
            want_ids += [i for i, md in zip(ids, got.get("metadatas") or [])
                         if (md or {}).get("parent") and md["parent"] not in only_new]
        offset += len(ids)
        if len(ids) < _PAGE:
            break
    for i in range(0, len(want_ids), 1000):
        got = chroma.facets_by_ids(want_ids[i:i + 1000]) or {}
        _fold(zip(got.get("documents") or [], got.get("metadatas") or [], got.get("embeddings")),
              sums, meta, vecs)
    models.pop(None, None)
    model = models.most_common(1)[0][0] if models else None
    if not vecs:
        return np.zeros((0, 0), dtype=np.float32), [], model
    X = np.stack(vecs)
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
    return X, meta, model


def kmeans(X: np.ndarray, k: int, seed: int = _SEED, iters: int = _ITERS) -> tuple:
    """Spherical k-means (cosine): k-means++ start on a sample, then Lloyd.
    Returns (unit centroids, labels). The same input gives the same output."""
    rng = np.random.default_rng(seed)
    k = max(1, min(k, len(X)))
    sample = X[rng.choice(len(X), size=min(20000, len(X)), replace=False)]
    C = [sample[rng.integers(len(sample))]]
    d = 1 - sample @ C[0]
    for _ in range(k - 1):
        p = np.maximum(d, 0) ** 2
        if p.sum() <= 0:
            break
        C.append(sample[rng.choice(len(sample), p=p / p.sum())])
        d = np.minimum(d, 1 - sample @ C[-1])
    C = np.stack(C)
    for _ in range(iters):
        lab = (X @ C.T).argmax(axis=1)
        new = np.zeros_like(C)
        np.add.at(new, lab, X)
        empty = np.bincount(lab, minlength=len(C)) == 0
        new[empty] = C[empty]
        new /= np.linalg.norm(new, axis=1, keepdims=True) + 1e-9
        moved = float(np.abs(new - C).max())
        C = new
        if moved < 1e-4:
            break
    return C, (X @ C.T).argmax(axis=1)


def k_for(n: int) -> int:
    return max(_K_MIN, min(_K_MAX, round(n / _PER_CLUSTER)))


def build(chroma) -> dict:
    """The clusters of every title, per group. Blocking (Chroma, NumPy)."""
    X, meta, model = read_titles(chroma)
    plan: dict = {"groups": {}, "model": model, "titles": len(meta)}
    if not meta:
        return plan
    for grp in ("video", "music"):
        idx = np.array([i for i, m in enumerate(meta) if group_of(m["domain"]) == grp], dtype=int)
        if len(idx) < _K_MIN:
            continue
        C, lab = kmeans(X[idx], k_for(len(idx)))
        clusters = []
        for c in range(len(C)):
            members = idx[lab == c]
            if not len(members):
                continue
            sims = X[members] @ C[c]
            central = members[np.argsort(-sims)][:_TOP]
            phrases = Counter(p.lower() for m in members for p in meta[m]["phrases"] if p)
            genres = Counter(g.lower() for m in members for g in meta[m]["genres"])
            clusters.append({
                "idx": c, "centroid": C[c].astype(np.float32).tobytes(),
                "size": int(len(members)),
                "top_titles": [meta[m]["title"] for m in central if meta[m]["title"]],
                "top_phrases": [p for p, _ in phrases.most_common(_TOP)],
                "top_genres": [g for g, _ in genres.most_common(5)],
                "members": [(meta[m]["parent"], meta[m]["title"], meta[m]["domain"], float(s))
                            for m, s in zip(members, sims)],
            })
        plan["groups"][grp] = clusters
    return plan


def persist_build(plan: dict, now: Optional[datetime] = None) -> int:
    """Replace every nanogenre and assignment with this build. Blocking —
    call via ``run_write``. Returns the clusters written."""
    from src.database.models import Nanogenre as N, TitleNanogenre as T
    now = now or datetime.utcnow()
    n = 0
    with get_db_session() as db:
        db.query(T).delete(synchronize_session=False)
        db.query(N).delete(synchronize_session=False)
        for grp, clusters in plan["groups"].items():
            for c in clusters:
                row = N(grp=grp, idx=c["idx"], size=c["size"], centroid=c["centroid"],
                        top_titles=json.dumps(c["top_titles"], ensure_ascii=False),
                        top_phrases=json.dumps(c["top_phrases"], ensure_ascii=False),
                        top_genres=json.dumps(c.get("top_genres") or [], ensure_ascii=False),
                        embed_model=plan.get("model"), version=VERSION, built_at=now)
                db.add(row)
                db.flush()
                db.bulk_insert_mappings(T, [
                    {"parent": p, "title": (t or "")[:512] or None, "domain": d,
                     "nanogenre_id": row.id, "sim": s, "assigned_at": now}
                    for p, t, d, s in c["members"]])
                n += 1
        db.commit()
    return n


# ── keeping it current ──────────────────────────────────────────────────────

def _state() -> dict:
    """What the stored build is: version, model, assigned parents, centres.
    Blocking."""
    from src.database.models import Nanogenre as N, TitleNanogenre as T
    with get_db_session() as db:
        rows = db.query(N.id, N.grp, N.centroid, N.version, N.embed_model).all()
        parents = {p for (p,) in db.query(T.parent)}
    return {"version": max((r[3] for r in rows), default=None),
            "model": next((r[4] for r in rows if r[4]), None),
            "centres": {g: [(r[0], np.frombuffer(r[2], dtype=np.float32)) for r in rows if r[1] == g]
                        for g in ("video", "music")},
            "parents": parents}


def assign(X: np.ndarray, meta: list, centres: dict) -> list:
    """[(parent, title, domain, nanogenre id, sim)] for new titles: the
    nearest centre of their group."""
    out = []
    for i, m in enumerate(meta):
        grp = group_of(m["domain"])
        cs = centres.get(grp) or []
        if not cs:
            continue
        sims = np.stack([c for _, c in cs]) @ X[i]
        j = int(sims.argmax())
        out.append((m["parent"], m["title"], m["domain"], cs[j][0], float(sims[j])))
    return out


def save_assignments(rows: list, now: Optional[datetime] = None) -> int:
    from src.database.models import Nanogenre as N, TitleNanogenre as T
    now = now or datetime.utcnow()
    with get_db_session() as db:
        db.bulk_insert_mappings(T, [{"parent": p, "title": (t or "")[:512] or None, "domain": d,
                                     "nanogenre_id": nid, "sim": s, "assigned_at": now}
                                    for p, t, d, nid, s in rows])
        for nid, k in Counter(r[3] for r in rows).items():
            db.query(N).filter(N.id == nid).update({N.size: N.size + k}, synchronize_session=False)
        db.commit()
    return len(rows)


async def run_nanogenres(task=None) -> bool:
    """The custodian job: build once (and on a new version or embedding
    model), then assign the titles new to the facet index."""
    from src.database.runner import run_read, run_write
    from src.vector_store.chromadb_wrapper import get_chroma_db, run_chroma
    st = await run_read(_state)
    chroma = get_chroma_db()
    X = meta = model = None
    if st["version"] == VERSION:
        X, meta, model = await run_chroma(read_titles, chroma, st["parents"])
    # A new embedding model re-embeds the facet index: the old centres point
    # into another space, so the clusters are built again.
    if st["version"] != VERSION or (model and st["model"] and model != st["model"]):
        plan = await run_chroma(build, chroma)
        if not plan["groups"]:
            logger.info("[nanogenres] the facet index holds too few titles to cluster")
            return True
        n = await run_write(persist_build, plan)
        logger.info("[nanogenres] built %d nanogenre(s) from %d title(s)", n, plan["titles"])
        return True
    if meta:
        rows = assign(X, meta, st["centres"])
        await run_write(save_assignments, rows)
        logger.info("[nanogenres] %d new title(s) assigned", len(rows))
    return True


# ── naming ──────────────────────────────────────────────────────────────────

_NAME_SYSTEM = (
    "You name nanogenres for a film, TV and music library, the way Letterboxd or a record "
    "shop would (\"slow-burn folk horror\", \"coming-of-age teen comedy\", \"german street rap\"). "
    "You get the titles nearest the centre of one cluster, their genres and shared themes. "
    "Answer with one natural name of 2 to 4 lowercase words, a genre phrase rather than a "
    "list of themes or moods, no title names. JSON: {\"name\": \"...\"}")
_NAME_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}


def clean_name(raw: str) -> Optional[str]:
    name = " ".join(str(raw or "").replace('"', "").replace("_", " ").strip().lower().split())
    words = name.split()
    if not 1 <= len(words) <= 6 or len(name) > 60:
        return None
    return name


def _pending(limit: int) -> list:
    from src.database.models import Nanogenre as N
    with get_db_session() as db:
        return [{"id": r.id, "grp": r.grp, "titles": json.loads(r.top_titles or "[]"),
                 "phrases": json.loads(r.top_phrases or "[]"),
                 "genres": json.loads(r.top_genres or "[]")}
                for r in db.query(N).filter(N.name.is_(None)).order_by(N.size.desc()).limit(limit)]


def _save_name(nid: int, name: str) -> None:
    from src.database.models import Nanogenre as N
    with get_db_session() as db:
        db.query(N).filter(N.id == nid).update({N.name: name, N.named_at: datetime.utcnow()},
                                                synchronize_session=False)
        db.commit()


async def _ask_name(c: dict, model: Optional[str] = None) -> Optional[str]:
    from src.config import settings
    from src.services.llm_utils import SUMMARIZER_KEEP_ALIVE, ollama_options, strip_think_tags
    from src.services.ollama_hosts import ollama_for
    from src.services.pc_guard import ollama_client
    kind = "music artists" if c["grp"] == "music" else "films and series"
    prompt = (f"{kind.capitalize()}: {', '.join(c['titles'][:_TOP])}\n"
              f"Their genres: {', '.join(c.get('genres') or []) or 'unknown'}\n"
              f"Shared themes: {', '.join(c['phrases'][:_TOP])}")
    try:
        async with ollama_client(timeout=120) as client:
            r = await client.post(f"{ollama_for('enrich')}/api/chat", json={
                "model": model or settings.SUMMARIZER_MODEL or settings.BASE_SUMMARIZER_MODEL,
                "stream": False, "format": _NAME_SCHEMA, "keep_alive": SUMMARIZER_KEEP_ALIVE,
                "messages": [{"role": "system", "content": _NAME_SYSTEM},
                             {"role": "user", "content": prompt}],
                **ollama_options(temperature=0.3, num_predict=40)})
        if r.status_code != 200:
            return None
        got = json.loads(strip_think_tags(r.json().get("message", {}).get("content", "") or ""))
        return clean_name(got.get("name"))
    except Exception as e:  # noqa: BLE001 - this cluster is asked again next run
        logger.warning("[nanogenres] naming #%s failed: %s", c["id"], e)
        return None


async def run_nanogenre_names(task=None) -> bool:
    """The custodian job: name a budget of clusters with the summarizer.
    True when none is left unnamed."""
    from src.database.runner import run_read, run_write
    from src.services.task_monitor import task_monitor
    todo = await run_read(_pending, _NAME_BUDGET)
    named = 0
    for i, c in enumerate(todo):
        if task is not None:
            task_monitor.update(task, processed=i, total=len(todo), message="Naming nanogenres…")
        name = await _ask_name(c)
        if name:
            await run_write(_save_name, c["id"], name)
            named += 1
    if todo:
        logger.info("[nanogenres] named %d of %d cluster(s)", named, len(todo))
    return len(todo) < _NAME_BUDGET and named == len(todo)
