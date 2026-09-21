"""Every GitHub Action reference is a full commit SHA, and one action
family rides one commit.

2026-09-21: Dependabot bumps each `uses:` path as its own PR —
codeql-action/analyze, /init and /upload-sarif are three dependencies to
it. Merging the analyze and upload-sarif PRs while the init PR did not
exist yet put init on 4.38.0 and analyze on 4.38.1, and CodeQL failed
with "Loaded a configuration file for version '4.38.0', but running
version '4.38.1'". The OSV reusable workflow and its PR twin drifted the
same way. Neither failure is visible locally; this suite is.

    python tests/test_workflow_pins.py
"""
import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _ROOT / ".github" / "workflows"
_USES = re.compile(r'^\s*(?:-\s*)?uses:\s*"?([^\s"@]+)@([^\s"#]+)', re.M)
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _references():
    """[(workflow file name, action path, ref)] for every `uses:` line."""
    refs = []
    for wf in sorted(_WORKFLOWS.glob("*.yml")):
        text = wf.read_text(encoding="utf-8")
        for m in _USES.finditer(text):
            refs.append((wf.name, m.group(1), m.group(2)))
    return refs


def _family(action_path):
    """owner/repo — the unit GitHub versions; the subpath is just an entry point."""
    return "/".join(action_path.split("/")[:2])


def test_every_action_is_pinned_to_a_full_sha():
    refs = _references()
    assert refs, "no `uses:` lines found under .github/workflows"
    loose = [(wf, path, ref) for wf, path, ref in refs
             if not path.startswith("./") and not _SHA.match(ref)]
    assert not loose, f"floating action refs (pin to a 40-hex commit): {loose}"


def test_one_action_family_one_commit():
    by_family = {}
    for wf, path, ref in _references():
        by_family.setdefault(_family(path), {}).setdefault(ref, []).append(f"{wf}:{path}")
    split = {fam: shas for fam, shas in by_family.items() if len(shas) > 1}
    assert not split, (
        "one action family on several commits - merge the sibling Dependabot PR "
        f"or move the SHA by hand: {split}")


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
