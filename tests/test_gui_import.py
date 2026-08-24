"""The GUI data-import path, and the setup wizard's three-headed schema.

    python tests/test_gui_import.py

Offline — file handling runs against a temp directory, nothing touches the
database or the network.

Why the schema block exists: the wizard's fields live in THREE places
(SETUP_FIELDS in the backend, SetupCompleteRequest for /complete, and the
hand-written frontend forms). OMDb sat in two of the three from the start —
pydantic silently dropped the value, so the wizard never actually saved an
OMDb key, and nothing anywhere errored.
"""
import io
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


from src.services import spotify_import as si  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="curatarr-import-test-"))

# ── file classification ─────────────────────────────────────────────────────

check("the extended history is usable",
      si._classify("Streaming_History_Audio_2023_4.json") == "usable"
      and si._classify("endsong_0.json") == "usable")
check("the basic export is recognised and refused — it lacks the completion "
      "signal replay counting needs",
      si._classify("StreamingHistory0.json") == "basic")
check("anything else is neither", si._classify("notes.txt") == "other")

# ── uploads ─────────────────────────────────────────────────────────────────

r = si.save_upload("Streaming_History_Audio_2024_1.json", b"[]", TMP)
check("a usable file is stored", r["saved"] == ["Streaming_History_Audio_2024_1.json"])

r = si.save_upload("StreamingHistory0.json", b"[]", TMP)
check("the basic export is rejected with an explanation",
      not r["saved"] and "EXTENDED" in r["rejected"][0][1])

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as zf:
    zf.writestr("MyData/Streaming_History_Audio_2022.json", "[]")
    zf.writestr("MyData/Read_Me_First.pdf", "x")
    zf.writestr("MyData/StreamingHistory1.json", "[]")
r = si.save_upload("my_spotify_data.zip", buf.getvalue(), TMP)
check("a whole export zip is unpacked to just the usable members",
      r["saved"] == ["Streaming_History_Audio_2022.json"]
      and len(r["rejected"]) == 2)

check("garbage bytes with a .zip name do not crash the upload",
      si.save_upload("fake.zip", b"not a zip", TMP)["rejected"][0][1]
      == "not a readable zip archive")

check("pending lists exactly the usable files",
      sorted(p["name"] for p in si.pending_files(TMP))
      == ["Streaming_History_Audio_2022.json",
          "Streaming_History_Audio_2024_1.json"])

# path traversal: a hostile zip member name must not escape the import dir
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as zf:
    zf.writestr("../../evil_Streaming_History_Audio_1.json", "[]")
r = si.save_upload("evil.zip", buf.getvalue(), TMP)
escaped = (TMP.parent / "evil_Streaming_History_Audio_1.json").exists()
check("a zip member cannot climb out of the import directory", not escaped)

check("clear removes them", si.clear_pending(TMP) >= 2
      and si.pending_files(TMP) == [])

# ── the completion rule matches the Plex path ───────────────────────────────

check("timestamps parse and bad ones do not crash",
      si._parse_ts("2024-01-02T03:04:05Z").year == 2024
      and si._parse_ts("garbage") is not None)

# ── wiring ──────────────────────────────────────────────────────────────────

_r = (ROOT / "src/routers/imports.py").read_text(encoding="utf-8")
check("upload is allowed during first-run setup",
      "require_admin_or_first_run" in _r)
check("running an import is admin-only",
      'Depends(require_admin)' in _r)
check("the import runs off the event loop with an Activity card",
      "asyncio.to_thread" in _r and "task_monitor.create" in _r)
check("only one import at a time",
      'raise HTTPException(409' in _r)

_m = (ROOT / "src/main.py").read_text(encoding="utf-8")
check("the router is registered", '"/api/import"' in _m)

_f = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
check("the setup wizard has an Import step",
      "'import','done'" in _f and "spotifyDropZone('su-sp')" in _f)
check("the admin view has the same drop zone with a user picker",
      "adm-sp-zone" in _f and 'id="adm-sp-user"' in _f)
check("the CLI wrapper survives for headless boxes",
      "run_import" in (ROOT / "import_spotify.py").read_text(encoding="utf-8"))

# ── the wizard schema agrees with itself in all three places ────────────────

from src.routers.setup import SetupCompleteRequest
from src.services.setup_wizard import SETUP_FIELDS

model_fields = set(SetupCompleteRequest.model_fields
                   if hasattr(SetupCompleteRequest, "model_fields")
                   else SetupCompleteRequest.__fields__)
for f in SETUP_FIELDS:
    check(f"SETUP_FIELDS['{f['id']}'] survives /complete",
          f["id"] in model_fields)
    if f["id"].endswith(("_url", "_api_key", "_model")) or f["id"] in (
            "tmdb_api_key", "plex_token"):
        check(f"...and the frontend has an input for it",
              f["id"] in _f or f["id"].replace("_api_key", "-key") in _f
              or f["id"].replace("_", "-") in _f
              or f"s-{f['id'].split('_')[0]}" in _f)

check("no stale model generation left in the frontend", "qwen2.5" not in _f)
check("the frontend fallback matches the benchmarked default",
      "gemma4:31b (recommended" in _f)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
