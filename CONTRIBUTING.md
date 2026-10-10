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

Backend changes reload the server; the frontend is served as static
files, so a browser refresh picks up changes in `frontend/`.

## Tests

No pytest — every suite is a plain script, run with the venv's Python:

```bash
python tests/run_all.py          # the whole battery (CI runs exactly this)
python tests/test_<name>.py      # one suite
```

Stop the app before running the full battery: several suites open the
vector store, which admits one process at a time, so they all fail together
while it is running. The runner says so rather than printing the same
traceback eight times.

Policy: new functionality ships with tests for it, and a bug fix ships
with the test that would have caught it. New tests follow the stdlib
pattern (`check(name, cond)` counter, printed summary, non-zero exit on
failure — see `tests/test_stale_guard.py` as a template). Regressions
caught live become fixtures.

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
