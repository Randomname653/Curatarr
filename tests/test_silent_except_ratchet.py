"""Silent broad excepts may only go down (silent-failure sweep, 2026-10-15).

A broad handler (``except:``, ``except Exception``, ``except BaseException``)
whose whole body is ``pass``, or one ``logger.debug(...)`` line (optionally
followed by ``pass`` / ``continue`` / ``return ...``), turns a bug into "no
data" without a trace a normal log level shows. That is how the 2026-10
taste-cluster DetachedInstanceError hid for months.

This suite counts those handlers per file and fails when a file has MORE than
its recorded baseline (tests/silent_except_baseline.json). Lower counts pass
and print a reminder to tighten the baseline:

    python tests/test_silent_except_ratchet.py            # check
    python tests/test_silent_except_ratchet.py --update   # rewrite baseline

A handler that is silent on purpose says why on its ``except`` line:
``except Exception:  # noqa: BLE001 - <reason>`` - it is not counted.
"""
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
BASELINE = ROOT / "tests" / "silent_except_baseline.json"
_BROAD = {"Exception", "BaseException"}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True
    names = t.elts if isinstance(t, ast.Tuple) else [t]
    return any(isinstance(n, ast.Name) and n.id in _BROAD for n in names)


def _is_debug_call(stmt) -> bool:
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute)
            and stmt.value.func.attr == "debug")


def _is_silent(body: list) -> bool:
    if all(isinstance(s, ast.Pass) for s in body):
        return True
    if not _is_debug_call(body[0]):
        return False
    return all(isinstance(s, (ast.Pass, ast.Continue, ast.Return)) for s in body[1:])


def count_silent(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    n = 0
    for node in ast.walk(ast.parse(text, filename=str(path))):
        if isinstance(node, ast.ExceptHandler) and _is_broad(node) and _is_silent(node.body):
            if "noqa: BLE001" in lines[node.lineno - 1]:
                continue
            n += 1
    return n


def scan() -> dict:
    out = {}
    for p in sorted(SRC.rglob("*.py")):
        n = count_silent(p)
        if n:
            out[p.relative_to(ROOT).as_posix()] = n
    return out


def main() -> int:
    current = scan()
    if "--update" in sys.argv:
        BASELINE.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
        print(f"baseline rewritten: {sum(current.values())} silent handlers "
              f"in {len(current)} files")
        return 0
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    grew = {f: (baseline.get(f, 0), n) for f, n in current.items()
            if n > baseline.get(f, 0)}
    shrank = {f: (b, current.get(f, 0)) for f, b in baseline.items()
              if current.get(f, 0) < b}
    for f, (b, n) in sorted(grew.items()):
        print(f"  FAIL  {f}: {n} silent broad excepts (baseline {b}) - log at "
              "WARNING, narrow the except, or say why with '# noqa: BLE001 - <reason>'")
    for f, (b, n) in sorted(shrank.items()):
        print(f"  note  {f}: {n} (baseline {b}) - run with --update to lock it in")
    total_b, total_n = sum(baseline.values()), sum(current.values())
    print(f"\nsilent broad excepts: {total_n} (baseline {total_b})")
    print(f"{0 if grew else 1} passed, {1 if grew else 0} failed")
    return 1 if grew else 0


if __name__ == "__main__":
    sys.exit(main())
