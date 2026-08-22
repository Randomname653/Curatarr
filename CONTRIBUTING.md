# Contributing

Curatarr is a personal project that went public — issues and PRs are
welcome, but the maintainer curates changes the same way the app curates
media: deliberately.

## Dev setup

```bash
pip install -r requirements.txt
cp .env.example .env        # then fill in Plex/Ollama at minimum
python build_models.py      # bakes the curatarr-* Ollama model tags
python -m src.main          # or start.bat on Windows
```

An [Ollama](https://ollama.com) install with enough VRAM for the
configured base models is required for the LLM features; the app degrades
gracefully without the optional metadata keys.

## Tests

No pytest, no venv — every suite is a plain script:

```bash
python tests/run_all.py          # the whole battery (CI runs exactly this)
python tests/test_<name>.py      # one suite
```

Stop the app before running the full battery: several suites open the
vector store, which admits one process at a time, so they all fail together
while it is running. The runner says so rather than printing the same
traceback eight times.

New tests follow the stdlib pattern (`check(name, cond)` counter, printed
summary, non-zero exit on failure — see `tests/test_stale_guard.py` as a
template). Regressions caught live become fixtures.

## Style

- Comments explain **why** (constraints, traps, history), not what the
  next line does — match the density you see around your change.
- UI/app knowledge lives in `src/services/app_context.py` blocks, never
  inline in routers (`tests/test_app_context_drift.py` enforces it).
- Frontend: design tokens/classes only, amber is the accent — rules live
  in the CSS header of `frontend/index.html`.

## License

Contributions are accepted under the project license (AGPL-3.0). Ported
third-party code keeps its origin — see `THIRD_PARTY_LICENSES.md`.
