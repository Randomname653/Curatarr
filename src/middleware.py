"""Security-header middleware (adapted from Jules Sentinel branch).

Pure ASGI instead of Starlette's BaseHTTPMiddleware: the app serves SSE
streams (chat, task monitor), and BaseHTTPMiddleware wraps every response
in its own send loop — pure ASGI just decorates headers on the first
``http.response.start`` message and stays out of the stream's way.

Deliberately NOT set: Strict-Transport-Security (the app is served over
plain HTTP on the LAN — browsers ignore HSTS on http://, and advertising
it would only bite someone who later fronts a whole domain with TLS) and
X-XSS-Protection (deprecated; modern browsers dropped the auditor).
"""


import logging
import time

logger = logging.getLogger(__name__)

# One number to answer "which pages are actually slow?" — the page-switch
# sluggishness felt in the frontend is either per-endpoint server time
# (this catches it) or fetch-on-every-switch behaviour (the frontend's
# stale-while-revalidate cache addresses that). 300ms is the threshold of
# "a human notices".
SLOW_REQUEST_MS = 300


class SlowRequestLogMiddleware:
    """Log requests whose response START took longer than SLOW_REQUEST_MS.

    Pure ASGI, same reasoning as below: timing stops at the first
    ``http.response.start`` message, so SSE streams (chat, task monitor)
    are measured on time-to-first-byte, not on how long they stay open —
    a stream that runs for minutes is not a slow endpoint.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        t0 = time.perf_counter()

        async def send_timed(message):
            if message["type"] == "http.response.start":
                ms = (time.perf_counter() - t0) * 1000
                if ms >= SLOW_REQUEST_MS:
                    logger.warning("[slow] %s %s — %.0f ms to response start",
                                   scope.get("method", "?"),
                                   scope.get("path", "?"), ms)
            await send(message)

        await self.app(scope, receive, send_timed)


class SecurityHeadersMiddleware:
    HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),  # no iframes anywhere in the frontend
        (b"referrer-policy", b"same-origin"),
    ]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.extend(self.HEADERS)
            await send(message)

        await self.app(scope, receive, send_with_headers)
