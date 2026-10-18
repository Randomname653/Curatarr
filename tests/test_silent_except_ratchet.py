"""No silent broad excepts in src/ (silent-failure sweep, 2026-10).

A broad handler (``except:``, ``except Exception``, ``except BaseException``)
whose whole body is ``pass``, or one ``logger.debug(...)`` line (optionally
followed by ``pass`` / ``continue`` / ``return ...``), turns a bug into "no
data" without a trace a normal log level shows. That is how the 2026-10
taste-cluster DetachedInstanceError hid for months.

This began as a ratchet (342 such handlers, each file allowed only to go
down) and reached zero; now it is a rule. A handler that must stay silent
says why on its ``except`` line, concretely:

    except Exception:  # noqa: BLE001 - progress card only; the work is unaffected

Anything else either narrows the exception, logs at WARNING, or - when it
guards a decision - fails closed.

    python tests/test_silent_except_ratchet.py
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
_BROAD = {"Exception", "BaseException"}
# A reason, not a shrug: at least a few words after the marker.
_REASON = re.compile(r"#\s*noqa:\s*BLE001\s*[-\u2013\u2014]\s*(\S.{11,})")
_VAGUE = {"best effort", "best-effort", "ignore", "ignored", "not important", "harmless"}


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


def _annotation(line: str):
    """(annotated, reason_ok) for an except line."""
    if "noqa: BLE001" not in line:
        return False, False
    m = _REASON.search(line)
    reason = (m.group(1).strip().lower() if m else "")
    return True, bool(m) and reason.rstrip(".") not in _VAGUE


def silent_sites(path: Path) -> list:
    """[(line, problem)] for one file: unannotated silent handlers, and
    annotations whose reason is missing or a shrug."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    out = []
    for node in ast.walk(ast.parse(text, filename=str(path))):
        if isinstance(node, ast.ExceptHandler) and _is_broad(node) and _is_silent(node.body):
            annotated, reason_ok = _annotation(lines[node.lineno - 1])
            if not annotated:
                out.append((node.lineno, "silent broad except"))
            elif not reason_ok:
                out.append((node.lineno, "noqa: BLE001 without a concrete reason"))
    return out


def count_silent(path: Path) -> int:
    """Unannotated silent handlers in one file (kept for tooling)."""
    return sum(1 for _, why in silent_sites(path) if why == "silent broad except")


def main() -> int:
    problems = []
    for p in sorted(SRC.rglob("*.py")):
        for line, why in silent_sites(p):
            problems.append(f"{p.relative_to(ROOT).as_posix()}:{line}: {why}")
    for msg in problems:
        print(f"  FAIL  {msg} - log at WARNING, narrow the except, fail closed, "
              "or say why with '# noqa: BLE001 - <reason>'")
    print(f"\nsilent broad excepts: {len(problems)}")
    print(f"{0 if problems else 1} passed, {1 if problems else 0} failed")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
