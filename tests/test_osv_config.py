"""The OSV exception list stays small, reasoned, documented and identical
for both scan jobs.

2026-09-21: the first OSV run that actually scanned (the 2.5.1 reusable
workflow had been failing on --skip-git and reporting "No issues found"
from a missing results file) listed the four chromadb server advisories
that SECURITY.md already explains and Dependabot has dismissed. They are
excepted in osv-scanner.toml; this suite keeps every excepted id explained
in SECURITY.md, with a reason of its own, and keeps the scan-args of the
push and PR jobs the same so the two scans cannot drift apart.

    python tests/test_osv_config.py
"""
import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TOML = _ROOT / "osv-scanner.toml"
_SECURITY = _ROOT / "SECURITY.md"
_WORKFLOW = _ROOT / ".github" / "workflows" / "osv-scanner.yml"
_ID = re.compile(r"^(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}|PYSEC-\d{4}-\d+|CVE-\d{4}-\d+)$")


def _config():
    try:
        import tomllib
    except ImportError:  # Python < 3.11 — CI runs 3.12, say so instead of passing silently
        print("  (tomllib missing - osv-scanner.toml not parsed here)")
        return None
    with open(_TOML, "rb") as fh:
        return tomllib.load(fh)


def test_every_exception_has_a_well_formed_id_and_a_reason():
    cfg = _config()
    if cfg is None:
        return
    entries = cfg.get("IgnoredVulns", [])
    assert entries, "osv-scanner.toml lists no IgnoredVulns"
    assert not cfg.get("PackageOverrides"), "package-wide ignores hide new advisories; except ids instead"
    for e in entries:
        assert _ID.match(e.get("id", "")), f"odd id: {e.get('id')!r}"
        assert len(e.get("reason", "").strip()) >= 60, f"{e['id']}: the reason must say why the code is unreachable"
    assert len(entries) <= 8, f"{len(entries)} exceptions — that is a policy, not a list; revisit"


def test_every_excepted_id_is_explained_in_security_md():
    cfg = _config()
    if cfg is None:
        return
    text = _SECURITY.read_text(encoding="utf-8")
    missing = [e["id"] for e in cfg.get("IgnoredVulns", []) if e["id"] not in text]
    assert not missing, f"excepted in osv-scanner.toml but absent from SECURITY.md: {missing}"


def _scan_arg_blocks(text):
    """The lines of every `scan-args: |-` block: those indented deeper than the key."""
    lines = text.splitlines()
    blocks = []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)scan-args:\s*\|-?\s*$", line)
        if not m:
            continue
        depth = len(m.group(1))
        body = []
        for nxt in lines[i + 1:]:
            if not nxt.strip():
                continue
            if len(nxt) - len(nxt.lstrip()) <= depth:
                break
            body.append(nxt.strip())
        blocks.append(tuple(body))
    return blocks


def test_both_scan_jobs_pass_the_same_scan_args():
    blocks = _scan_arg_blocks(_WORKFLOW.read_text(encoding="utf-8"))
    assert len(blocks) == 2, f"expected the push and the PR job to declare scan-args, found {len(blocks)}"
    assert blocks[0] == blocks[1], f"scan-args differ between the jobs: {blocks}"
    assert "--no-resolve" in blocks[0], "requirements.txt pins direct deps only; without --no-resolve the resolver reports floor versions no install gets"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
