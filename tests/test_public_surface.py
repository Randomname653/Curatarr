"""What an unauthenticated LAN client can see, pinned.

The 2026-09 first-run probe (a fresh clone on 127.0.0.1:8001, no .env)
found the OpenAPI spec public at /openapi.json while SECURITY.md promised
the docs were off - the gate only hid the Swagger UI. Same probe: no CSP,
and the wizard's Build-models step 500'd on a cp1252 console because the
service prints check marks. These tests keep all three closed.

    python tests/test_public_surface.py
"""
import io
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.config import settings
from src.middleware import SecurityHeadersMiddleware
from src.services.setup_wizard import _safe_print


def test_openapi_spec_is_gated_with_the_docs():
    from src.main import app
    if settings.ENABLE_DOCS:
        assert app.openapi_url == "/api/openapi.json"
        assert app.docs_url == "/api/docs"
    else:
        assert app.openapi_url is None, "the spec is the endpoint map - gate it"
        assert app.docs_url is None
    assert app.redoc_url is None


def test_security_headers_include_a_locked_down_csp():
    hdrs = dict(SecurityHeadersMiddleware.HEADERS)
    csp = hdrs[b"content-security-policy"].decode()
    for directive in ("default-src 'self'", "connect-src 'self'",
                      "object-src 'none'", "frame-ancestors 'none'",
                      "base-uri 'self'", "form-action 'self'"):
        assert directive in csp, directive
    # the single-file UI runs on inline handlers - this is the one allowance
    assert "script-src 'self' 'unsafe-inline'" in csp
    assert b"permissions-policy" in hdrs
    assert hdrs[b"x-frame-options"] == b"DENY"


def test_wizard_prints_survive_a_cp1252_console():
    class _Narrow(io.TextIOWrapper):
        pass
    buf = io.BytesIO()
    narrow = io.TextIOWrapper(buf, encoding="cp1252", errors="strict")
    real = sys.stdout
    sys.stdout = narrow
    try:
        _safe_print("  ✓  model already present — skipping pull", flush=True)
    finally:
        sys.stdout = real
    narrow.flush()
    out = buf.getvalue().decode("cp1252")
    assert "model already present" in out          # rendered, not raised
    assert "✓" not in out                       # degraded to ASCII


def test_launchers_preflight_the_dependencies_we_actually_ship():
    bat = (_ROOT / "start.bat").read_text(encoding="utf-8", errors="replace")
    tray = (_ROOT / "src/tray_app.py").read_text(encoding="utf-8")
    assert "jwt," in bat and " jose" not in bat, "start.bat still preflights python-jose"
    assert '"jwt"' in tray
    assert "--no-server-header" in bat
    assert "server_header=False" in tray


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
