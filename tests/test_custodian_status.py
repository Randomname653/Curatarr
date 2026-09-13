"""A failed custodian step reports one line, never a SQL statement.

2026-09-13: the Data custodian pill in Settings showed the audit's full
IntegrityError - the INSERT statement with all fifteen parameters - because
the report carried str(e). The class and the first line of the message are
what a status line can hold; the traceback belongs to the log.

    python tests/test_custodian_status.py
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.services.data_custodian import _short_error  # noqa: E402


class _FakeIntegrityError(Exception):
    pass


def test_sqlalchemy_style_messages_lose_the_statement_and_parameters():
    e = _FakeIntegrityError(
        "(sqlite3.IntegrityError) UNIQUE constraint failed: enrichment_findings.plex_rating_key, "
        "enrichment_findings.kind\n[SQL: INSERT INTO enrichment_findings (plex_rating_key, kind) VALUES (?, ?)]\n"
        "[parameters: ('radarr:1027', 'wrong_entity:year')]\n(Background on this error at: https://sqlalche.me/e/20/gkpj)")
    s = _short_error(e)
    assert s.startswith("_FakeIntegrityError: (sqlite3.IntegrityError) UNIQUE constraint failed"), s
    assert "[SQL:" not in s and "parameters" not in s and "\n" not in s and len(s) <= 180, s


def test_plain_errors_keep_class_and_message():
    assert _short_error(ValueError("boom")) == "ValueError: boom"
    assert _short_error(RuntimeError("")) == "RuntimeError"
    assert _short_error(ValueError("x" * 500)).endswith("x") and len(_short_error(ValueError("x" * 500))) <= 160


def test_the_custodian_uses_it_for_the_pill_and_the_report():
    src = (_ROOT / "src/services/data_custodian.py").read_text(encoding="utf-8")
    assert 'task_monitor.error(mon, _short_error(e))' in src
    assert '"result": f"error: {_short_error(e)}"' in src
    assert 'f"error: {e}"' not in src, "the raw exception text is back in the report"


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
