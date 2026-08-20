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
