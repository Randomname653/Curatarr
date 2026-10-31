"""Checks that grep the source instead of running it may only go down.

``check("...", "relevant_cats = [category]" in src)`` pins how a line is
spelled, not what the code does: a rename breaks it, and a behaviour bug
that keeps the spelling passes it. The 2026-10 test audit counted 413 such
checks in 114 suites. Some are the right tool (a router is mounted, a
workflow is pinned, a frontend string has no runtime to call); most could
call the code instead.

This counts, per suite, the comparisons ``"text" in <source>`` /
``"text" not in <source>`` and ``<source>.index("text")`` where <source> is
text read from the repository (``.read_text()``, ``inspect.getsource()``,
or a helper of the suite that returns either), against
``tests/source_grep_baseline.json``. A suite may go down, never up; a suite
not in the baseline must have none. When a suite goes down, lower its
number in the same change (the test says so).

    python tests/test_source_grep_ratchet.py            # check
    python tests/test_source_grep_ratchet.py --update   # rewrite the baseline (only ever lower)
"""
import ast
import json
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
BASELINE = TESTS / "source_grep_baseline.json"
_READERS = ("read_text", "getsource", "read_bytes")


def _reads_source(node: ast.AST, helpers: set) -> bool:
    """True when evaluating ``node`` reads repository text."""
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in _READERS or name in helpers:
                return True
    return False


def _source_helpers(tree: ast.Module) -> set:
    """Module-level functions that return repository text (``_read``,
    ``_function`` over it, ...) — to a fixed point, so a helper calling a
    helper counts too."""
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    helpers: set = set()
    while True:
        new = {name for name, fn in funcs.items() if name not in helpers and any(
            isinstance(r, ast.Return) and r.value is not None and _reads_source(r.value, helpers)
            for r in ast.walk(fn))}
        if not new:
            return helpers
        helpers |= new


def _source_names(tree: ast.Module, helpers: set) -> set:
    names: set = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            if _reads_source(n.value, helpers) or (
                    isinstance(n.value, ast.Name) and n.value.id in names):
                for t in targets:
                    for x in ast.walk(t):
                        if isinstance(x, ast.Name):
                            names.add(x.id)
    return names


def _is_source(node: ast.AST, names: set, helpers: set) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Subscript):              # src[...] / src.split(...)[1]
        return _is_source(node.value, names, helpers)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr in ("split", "lower", "strip", "replace"):
        return _is_source(node.func.value, names, helpers)
    return _reads_source(node, helpers)


def _is_text(node: ast.AST) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)) \
        or isinstance(node, ast.JoinedStr)


def count(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    helpers = _source_helpers(tree)
    names = _source_names(tree, helpers)
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 \
                and isinstance(node.ops[0], (ast.In, ast.NotIn)) \
                and _is_text(node.left) and _is_source(node.comparators[0], names, helpers):
            n += 1
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("index", "find", "count") and node.args \
                and _is_text(node.args[0]) and _is_source(node.func.value, names, helpers):
            n += 1
    return n


def current() -> dict:
    out = {}
    for f in sorted(TESTS.glob("test_*.py")):
        if f.name == Path(__file__).name:
            continue
        n = count(f)
        if n:
            out[f.name] = n
    return out


def _self_check() -> list:
    """The counter itself, on a tiny suite: what counts and what does not."""
    sample = '''
from pathlib import Path
ROOT = Path(".")
def _read(rel):
    return (ROOT / "src" / rel).read_text()
def _function(src, name):
    return src.split(name)[1]
main = (ROOT / "src/main.py").read_text()
fn = _function(_read("x.js"), "f")
ok = 1
check("a", "prefix=" in main)                 # counts
check("b", "x" not in fn)                     # counts (helper of a helper)
check("c", main.index("a") < main.index("b")) # counts twice
check("d", "y" in _read("y.py"))              # counts
check("e", "z" in {"z": 1})                   # data, not source
check("f", ok in [1])                         # not a text probe
'''
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "test_sample.py"
        p.write_text(sample, encoding="utf-8")
        got = count(p)
    return [] if got == 5 else [f"counter self-check: {got} source probes, expected 5"]


def main() -> int:
    errors = _self_check()
    now = current()
    base = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    if "--update" in sys.argv:
        # Only ever lower: a suite over its number (or new) keeps the old one,
        # so --update cannot paper over a regression.
        merged = {f: min(n, base[f]) if f in base else n
                  for f, n in now.items() if f in base or not base}
        BASELINE.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline written: {sum(merged.values())} source probes in {len(merged)} suites")
        return 0
    passed = 0
    for f, n in sorted(now.items()):
        allowed = base.get(f, 0)
        if n > allowed:
            errors.append(f"{f}: {n} checks grep the source (baseline {allowed}) — call the "
                          "code instead, or say in the check why only the text can tell")
    for f, allowed in sorted(base.items()):
        n = now.get(f, 0)
        if n < allowed:
            errors.append(f"{f}: down to {n} from {allowed} - lower it in {BASELINE.name} "
                          f"(python tests/{Path(__file__).name} --update)")
        else:
            passed += 1
    for e in errors:
        print(f"  FAIL  {e}")
    print(f"  {'PASS' if not errors else 'INFO'}  {sum(now.values())} source probes in "
          f"{len(now)} suites (baseline {sum(base.values())})")
    print(f"\n{passed + (not errors)} passed, {len(errors)} failed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
