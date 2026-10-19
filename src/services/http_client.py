"""One TLS context for every outbound HTTP client.

httpx builds a fresh ``ssl.SSLContext`` for each ``AsyncClient`` it creates,
loading the whole CA bundle: about 53 ms of CPU, on the event loop, and the
app opens a client per request in many places (one per enriched title, per
arr probe). Every ``httpx.AsyncClient(...)`` in src/ passes
``verify=tls_context()`` instead, which builds that context once, the way
httpx builds its default (certifi, or SSL_CERT_FILE / SSL_CERT_DIR), and
shares it; an SSLContext may serve many connections and threads.
tests/test_loop_db_ratchet.py refuses a client without it.
"""
from __future__ import annotations

import ssl
import threading

import httpx

_ctx: ssl.SSLContext | None = None
_lock = threading.Lock()


def tls_context() -> ssl.SSLContext:
    """The shared default-verifying context (built on first use; the
    lifespan builds it before the server takes requests)."""
    global _ctx
    if _ctx is None:
        with _lock:
            if _ctx is None:
                _ctx = httpx.create_ssl_context()
    return _ctx
