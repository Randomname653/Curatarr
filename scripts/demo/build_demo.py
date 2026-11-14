"""Build the Jane Doe demo instance and capture the README screenshots.

    python scripts/demo/build_demo.py --source //nas/curatarr
    python scripts/demo/build_demo.py --stages serve,capture      # shoot again, nothing rebuilt
    python scripts/demo/build_demo.py --stages serve --keep-running   # look at it by hand

--source is a live install's data folder (a share, a mount, a folder): the
newest nightly backup there (backups/curatarr_*.db, made with SQLite's
backup API and integrity-checked) becomes the demo's database, so the demo
always has today's library and today's schema, never an old copy that is
missing columns. CURATARR_DEMO_SOURCE may hold it instead of --source.

Stages, in order (--stages picks some):

  code      this checkout's HEAD (git archive) into <dir>/app - the code the
            screenshots show is the code you have, not the live install's
  fetch     the newest backup, the caches (enrichment, music index, posters
            ...) and the vector store into <dir>/app/data; the live install
            is only read, every database is opened as a COPY
  env       <dir>/app/.env: a fresh JWT secret; from --env-from (default this
            checkout's .env) only the model names, Ollama, the metadata keys
            and Radarr/Sonarr (read: the deletion analysis needs them); the
            media server, SoulSync, the node and every other service blank.
            No value is printed.
  seed      scripts/demo/seed.py in the copy: scrub every person's data, the
            accounts become Jane Doe and Sam, an invented 2026 for both
  serve     the copy's server on 127.0.0.1:<port> without its scheduler
  generate  the app's own output, through its own API: nanogenre names,
            taste profiles, recommendations, the household Wrap-Up 2026
            with its story, one chat question, a deletion analysis
            (the GPU works for a while; Ollama must answer)
  capture   scripts/capture_screenshots.py against the copy: the same frames
            as always, into this checkout's docs/screenshots
  (the server stops at the end unless --keep-running)

The copy holds a scrubbed household and is rebuilt from scratch by `code`
and `fetch`; <dir> carries a .curatarr-demo marker and nothing else is ever
deleted. Never point --dir at a live install.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MARKER = ".curatarr-demo"
STAGES = ["code", "fetch", "env", "seed", "serve", "generate", "capture"]

# From --env-from, by name: what the demo needs to think and to look things
# up. Everything else (tokens, the media server, SoulSync, Shoko, the node,
# Cloudflare Access, OpenSubtitles ...) stays out.
ENV_KEEP = ("OLLAMA_ENDPOINT", "CURATOR_MODEL", "SUMMARIZER_MODEL", "PITCHER_MODEL", "LLM_THINK_TAGS",
            "PILLARS_ENABLED", "PRINCIPLES_ENABLED", "TMDB_API_KEY", "OMDB_API_KEY", "LASTFM_API_KEY",
            "MAL_CLIENT_ID", "RADARR_URL", "RADARR_API_KEY", "SONARR_URL", "SONARR_API_KEY")
ENV_FIXED = {"FIRST_RUN": "false", "SYNC_ON_STARTUP": "false", "UPDATE_CHECK": "false",
             "PLEX_URL": "", "PLEX_TOKEN": "", "JELLYFIN_URL": "", "JELLYFIN_API_KEY": "",
             "SOULSYNC_URL": "", "SOULSYNC_API_KEY": "", "LIDARR_URL": "", "LIDARR_API_KEY": "",
             "SHOKO_URL": "", "SHOKO_API_KEY": "", "PC_AGENT_URL": "", "PC_AGENT_TOKEN": "",
             "OLLAMA_PC_ENDPOINT": "", "LISTENBRAINZ_TOKEN": "", "CF_ACCESS_HOSTS": ""}


def say(msg: str) -> None:
    print(f"[demo] {msg}", flush=True)


def _args():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--source", default=os.environ.get("CURATARR_DEMO_SOURCE", ""),
                   help="a live install's data folder (read only); default $CURATARR_DEMO_SOURCE")
    p.add_argument("--dir", default=str(Path(tempfile.gettempdir()) / "curatarr-demo"),
                   help="where the copy lives (default: %%TEMP%%/curatarr-demo)")
    p.add_argument("--env-from", default=str(REPO / ".env"), help="the .env whose model and metadata keys the demo uses")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--stages", default=",".join(STAGES), help="comma-separated, from: " + ", ".join(STAGES))
    p.add_argument("--keep-running", action="store_true", help="leave the server running at the end")
    p.add_argument("--only", default="", help="passed to capture_screenshots.py (a shot or a viewport)")
    return p.parse_args()


# ── code ─────────────────────────────────────────────────────────────────────
def _safe_rmtree(path: Path, base: Path) -> None:
    if not (base / MARKER).exists():
        sys.exit(f"{base} has no {MARKER} marker - refusing to delete anything there")
    if path.exists():
        shutil.rmtree(path)


def stage_code(base: Path) -> None:
    app = base / "app"
    base.mkdir(parents=True, exist_ok=True)
    if any(base.iterdir()) and not (base / MARKER).exists():
        sys.exit(f"{base} is not empty and is not a demo folder - pick another --dir")
    (base / MARKER).write_text("Curatarr demo copy (scripts/demo/build_demo.py)\n", encoding="utf-8")
    _safe_rmtree(app, base)
    app.mkdir()
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    archive = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", "HEAD"],
                             capture_output=True, check=True).stdout
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(app, filter="data")
    (app / MARKER).write_text(f"code {head}\n", encoding="utf-8")
    say(f"code: HEAD {head} -> {app}")


# ── fetch ────────────────────────────────────────────────────────────────────
def _quick_check(db: Path) -> bool:
    try:
        c = sqlite3.connect(db)
        ok = c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        c.close()
        return ok
    except sqlite3.DatabaseError:
        return False


def _copy_sqlite(src: Path, dst: Path) -> None:
    """A database of the running install: the file and its -wal (never -shm),
    checked here; a copy that does not check is taken again without the -wal
    (the last checkpoint's state)."""
    shutil.copy2(src, dst)
    wal = src.with_name(src.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        shutil.copy2(wal, dst.with_name(dst.name + "-wal"))
    if _quick_check(dst):
        return
    say(f"  {src.name}: copy with its -wal does not check, taking the file alone")
    for extra in (dst.with_name(dst.name + "-wal"), dst.with_name(dst.name + "-shm")):
        extra.unlink(missing_ok=True)
    shutil.copy2(src, dst)
    if not _quick_check(dst):
        sys.exit(f"{src.name}: the copy does not pass quick_check - try again when the install is idle")


def _copy_tree(src: Path, dst: Path) -> int:
    n = 0
    for root, _dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.endswith(("-shm", "-wal", "-journal")):
                continue
            s, d = Path(root) / f, dst / rel / f
            if f.endswith((".db", ".sqlite3")):
                _copy_sqlite(s, d)
            else:
                shutil.copy2(s, d)
            n += 1
    return n


def stage_fetch(base: Path, source: str) -> None:
    if not source:
        sys.exit("fetch needs --source (or CURATARR_DEMO_SOURCE): the live install's data folder")
    src = Path(source)
    backups = sorted((src / "backups").glob("curatarr_*.db"))
    if not backups:
        sys.exit(f"no nightly backup in {src / 'backups'} - the live database itself is never copied")
    data = base / "app" / "data"
    _safe_rmtree(data, base)
    data.mkdir(parents=True)
    t = time.time()
    say(f"fetch: database from {backups[-1].name}")
    shutil.copy2(backups[-1], data / "curatarr.db")
    if not _quick_check(data / "curatarr.db"):
        sys.exit(f"{backups[-1].name} does not pass quick_check")
    for sub in ("cache", "chromadb"):
        if (src / sub).is_dir():
            say(f"fetch: {sub}/ ...")
            say(f"  {_copy_tree(src / sub, data / sub)} files")
    say(f"fetch: done in {time.time() - t:.0f} s")


# ── env ──────────────────────────────────────────────────────────────────────
def _read_env(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def stage_env(base: Path, env_from: str) -> None:
    src = _read_env(Path(env_from))
    kept = {k: src[k] for k in ENV_KEEP if src.get(k)}
    env = {**ENV_FIXED, **kept, "JWT_SECRET": secrets.token_urlsafe(48)}
    lines = ["# The demo copy's settings (scripts/demo/build_demo.py). Rebuilt on every run."]
    lines += [f"{k}={v}" for k, v in env.items()]
    (base / "app" / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    missing = [k for k in ("TMDB_API_KEY", "OLLAMA_ENDPOINT") if k not in kept]
    say(f"env: {len(kept)} settings taken by name from {Path(env_from).name}"
        + (f"; missing: {', '.join(missing)}" if missing else ""))
    if "RADARR_URL" not in kept and "SONARR_URL" not in kept:
        say("env: no Radarr/Sonarr - the deletion analysis will have nothing to look at")


# ── seed ─────────────────────────────────────────────────────────────────────
def _run_in_copy(base: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for k in ("DATABASE_URL", "CHROMADB_PATH", "ENRICHMENT_CACHE", "CURATARR_ENV_FILE", "CURATARR_SUITE_TMP"):
        env.pop(k, None)     # the copy's own paths, never this shell's
    return subprocess.run([sys.executable, *args], cwd=base / "app", env=env, check=check)


def stage_seed(base: Path) -> None:
    say("seed: scrub + Jane Doe and Sam")
    _run_in_copy(base, "scripts/demo/seed.py")


# ── serve ────────────────────────────────────────────────────────────────────
def start_server(base: Path, port: int) -> subprocess.Popen:
    import httpx
    log = open(base / "server.log", "w", encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for k in ("DATABASE_URL", "CHROMADB_PATH", "ENRICHMENT_CACHE", "CURATARR_ENV_FILE", "CURATARR_SUITE_TMP"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, "scripts/demo/serve.py", str(port)], cwd=base / "app",
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}/"
    for _ in range(180):
        if proc.poll() is not None:
            sys.exit(f"the demo server stopped at start - see {base / 'server.log'}")
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                say(f"serve: {url} (log: {base / 'server.log'})")
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(1)
    proc.terminate()
    sys.exit(f"the demo server did not answer within 3 minutes - see {base / 'server.log'}")


def tokens(base: Path) -> dict:
    out = {}
    for uid in (1, 2):
        f = base / f"token-{uid}.txt"
        _run_in_copy(base, "scripts/demo/mint_token.py", str(uid), str(f))
        out[uid] = f.read_text(encoding="utf-8").strip()
    return out


# ── generate ─────────────────────────────────────────────────────────────────
class Api:
    def __init__(self, port: int, token: str):
        import httpx
        self.c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60,
                              headers={"Authorization": f"Bearer {token}"})

    def get(self, path, **kw):
        r = self.c.get(path, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path, ok=(200,), **kw):
        r = self.c.post(path, **kw)
        if r.status_code not in ok:
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def idle(self, what: str, limit_s: int = 3600) -> None:
        """Wait until no task runs (two quiet polls in a row)."""
        t, quiet = time.time(), 0
        while time.time() - t < limit_s:
            running = self.get("/api/tasks/running").get("tasks") or []
            quiet = quiet + 1 if not running else 0
            if quiet >= 2:
                say(f"  {what}: done in {time.time() - t:.0f} s")
                return
            time.sleep(5)
        raise RuntimeError(f"{what}: still running after {limit_s} s")


def _unnamed_nanogenres(base: Path) -> int:
    c = sqlite3.connect(f"file:{base / 'app' / 'data' / 'curatarr.db'}?mode=ro", uri=True)
    try:
        return c.execute("select count(*) from nanogenres where name is null").fetchone()[0]
    except sqlite3.OperationalError:
        return -1
    finally:
        c.close()


def stage_generate(base: Path, port: int, tok: dict) -> None:
    jane, sam = Api(port, tok[1]), Api(port, tok[2])
    if _unnamed_nanogenres(base) == -1:
        say("generate: nanogenres")
        jane.post("/api/tasks/scheduled/nanogenres/run", ok=(200, 409))
        jane.idle("nanogenres")
    for _ in range(10):
        if _unnamed_nanogenres(base) <= 0:
            break
        say(f"generate: naming nanogenres ({_unnamed_nanogenres(base)} unnamed)")
        jane.post("/api/tasks/scheduled/nanogenre_names/run", ok=(200, 409))
        jane.idle("nanogenre names")
    for name, api in (("Jane", jane), ("Sam", sam)):
        say(f"generate: taste profile, {name}")
        api.post("/api/history/recompute-taste", ok=(200, 409))
        jane.idle(f"taste {name}")
    for name, api in (("Jane", jane), ("Sam", sam)):
        say(f"generate: recommendations, {name}")
        api.post("/api/recommendations/refresh-cache", ok=(200, 409))
        jane.idle(f"recommendations {name}")
    say("generate: Wrap-Up 2026, household, with its story")
    tid = jane.post("/api/wrapup/generate", json={"scope": "all", "period": "year:2026"})["task_id"]
    t = time.time()
    while time.time() - t < 3600:
        st = jane.c.get(f"/api/wrapup/status/{tid}")
        if st.status_code == 404 or (st.json().get("status") in ("done", "error", "failed")):
            break
        time.sleep(5)
    w = jane.get("/api/wrapup", params={"scope": "all", "period": "year:2026"})
    say(f"  story: {w.get('story_source') or 'none'}" + (" (the curator did not write it)" if w.get("story_source") == "fallback" else ""))
    say("generate: one chat question")
    with jane.c.stream("POST", "/api/chat/message", timeout=900,
                       json={"message": "tell me something about the big bang theory"}) as r:
        for _ in r.iter_bytes():
            pass
    jane.idle("chat")
    say("generate: deletion analysis (the longest step)")
    r = jane.c.get("/api/recommendations/deletions", params={"refresh": "true"}, timeout=7200)
    r.raise_for_status()
    n = len((r.json() or {}).get("proposals") or [])
    say(f"  {n} deletion proposals" + (f" - {r.json().get('message')}" if not n and r.json().get("message") else ""))
    jane.idle("deletions")


# ── capture ──────────────────────────────────────────────────────────────────
def stage_capture(port: int, token: str, only: str) -> None:
    say("capture: the README frames")
    cmd = [sys.executable, str(REPO / "scripts" / "capture_screenshots.py"), "--url", f"http://127.0.0.1:{port}"]
    if only:
        cmd += ["--only", only]
    subprocess.run(cmd, cwd=REPO, env={**os.environ, "CURATARR_TOKEN": token}, check=True)


def main() -> None:
    a = _args()
    base = Path(a.dir).resolve()
    stages = [s.strip() for s in a.stages.split(",") if s.strip()]
    bad = [s for s in stages if s not in STAGES]
    if bad:
        sys.exit(f"unknown stage(s): {', '.join(bad)}")
    if "code" in stages:
        stage_code(base)
    elif not (base / MARKER).exists():
        sys.exit(f"{base} is not a demo copy yet - run the code stage first")
    if "fetch" in stages:
        stage_fetch(base, a.source)
    if "env" in stages:
        stage_env(base, a.env_from)
    if "seed" in stages:
        stage_seed(base)
    if not ({"serve", "generate", "capture"} & set(stages)):
        return
    proc = start_server(base, a.port)
    try:
        tok = tokens(base)
        if "generate" in stages:
            stage_generate(base, a.port, tok)
        if "capture" in stages:
            stage_capture(a.port, tok[1], a.only)
        if a.keep_running:
            say(f"running on http://127.0.0.1:{a.port}/ - token for Jane in {base / 'token-1.txt'}; Ctrl+C stops it")
            proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
        say("server stopped")
    print(json.dumps({"demo": str(base), "stages": stages}))


if __name__ == "__main__":
    main()
