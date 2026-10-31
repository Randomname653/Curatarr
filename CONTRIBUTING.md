# Contributing

Curatarr is a personal project that went public — issues and PRs are
welcome, but the maintainer curates changes the same way the app curates
media: deliberately.

## Dev setup

You need Python 3.12, Git, and a running [Ollama](https://ollama.com)
with enough VRAM for the configured base models (the LLM features need
it; the app degrades gracefully without the optional metadata keys).

```bash
git clone https://github.com/Randomname653/Curatarr.git curatarr
cd curatarr
python -m venv venv                  # Windows: py -3.12 -m venv venv
source venv/bin/activate             # Windows: venv\Scripts\activate
pip install -r requirements.txt
python build_models.py               # pulls the base models, builds the curatarr-* tags
python -m uvicorn src.main:app --reload --reload-dir src
```

Open `http://localhost:8000` and the setup wizard writes `.env` for you.
To configure by hand instead, copy `.env.example` to `.env` and fill in
the Plex and Ollama sections before the first start. On Windows,
`start.bat` does all of the above in one step.

This command listens on `127.0.0.1` only. Developing on a headless machine,
a VM or WSL2? Add `--host 0.0.0.0` to reach it from another browser, and
enter the setup code from its console when the wizard asks.

Backend changes reload the server; the frontend is served as static
files, so a browser refresh picks up changes in `frontend/`.

## Tests

No pytest — every suite is a plain script, run with the venv's Python:

```bash
python tests/run_all.py          # the whole battery, one suite per CPU (CI runs exactly this)
python tests/run_all.py --serial # one at a time (-j4 / CURATARR_TEST_JOBS=4 to choose)
python tests/test_<name>.py      # one suite
python tests/run_all.py --for wrapup_facts          # every suite that exercises a module
python tests/run_all.py --for wrapup_facts --list   # ...or just name them
```

The suites from the 2026 audits carry the audit's name (`loop_free_g3`,
`fail_closed_s2_g1`, `silent_failures_r3_*`) because the sweep docs cite
them as evidence; `--for` answers "where is this module tested" across them.

No suite touches your `data/` or `.env`: the runner gives each suite its own
temp database, vector store and caches, and fails the run if one still
writes to `data/` (`--serial` names it). So the battery runs while the app
is running.

Policy: new functionality ships with tests for it, and a bug fix ships
with the test that would have caught it. New tests follow the stdlib
pattern — `from _harness import app_db, check` as the first import (temp
stores; `app_db()` is the app's own migrated database), `check(name, cond)`
/ `check.table(...)`, `check.done()` at the end — see
`tests/test_vector_reconcile_live.py`. Prefer calling the code over grepping
its source: `tests/test_source_grep_ratchet.py` only lets the number of
source-text checks go down. In-memory databases come from
`_checks.memory_engine()` (foreign keys on, loop guard installed), never a
bare `create_engine("sqlite://")`. Wait for a condition with `wait_until`,
not a guessed sleep. Regressions caught live become fixtures.

## Dependencies

Two files describe the install:

- `requirements.txt` — the direct dependencies, each pinned to one
  version. Edit this file to change a dependency.
- `lock/requirements.txt` — the tested install, generated: every package
  the pins pull in, with platform markers and the sha256 of every
  distribution file (`uv pip compile --universal --generate-hashes`). CI
  installs it with `--require-hashes`, and the security scanners and
  Dependabot read it. **Never edit it by hand.**

The launchers keep an install in step with both at every start: they
install missing or outdated pins, raise any package that fell below the
lock (through the same hash check), and let the lock follow a newer
version, so they never lower anything. The server itself never installs
anything; it reports the comparison in Settings → Maintenance. To
reproduce the exact tested set in a fresh environment:

```bash
pip install --require-hashes -r lock/requirements.txt
```

To change a dependency on purpose, bump its version in `requirements.txt`
(or a tool version in `lock/requirements-*.in`) and regenerate the lock.
This needs `uv` once (`pip install uv`):

```bash
python -m src.deps_lock --compile
```

That keeps every version the lock already holds and changes only what
has to change. To move everything to the newest versions the pins allow,
add `--upgrade`.

## Style

- Comments explain **why** (constraints, traps, history), not what the
  next line does — match the density you see around your change.
- UI/app knowledge lives in `src/services/app_context.py` blocks, never
  inline in routers (`tests/test_app_context_drift.py` enforces it).
- Frontend: design tokens/classes only, amber is the accent — rules live
  in the header of `frontend/css/app.css`.

## License

Contributions are accepted under the project license (AGPL-3.0). Ported
third-party code keeps its origin — see `THIRD_PARTY_LICENSES.md`.
