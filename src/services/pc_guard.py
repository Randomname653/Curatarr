"""Abort a call to the gaming PC the moment it stops taking work (2026-10-22,
NAS move phase 2, PR 4b).

A call to the PC can outlive the PC: it shuts down, loses power, or a game
starts and the card is the game's. Without this, a hard power-off left the
request hanging until its read timeout - 5 minutes for a verdict, 10 for a
chat reply - and a game shared the card with the reply it interrupted.

``ollama_client()`` is the httpx client for model calls. Its transport
watches every request that goes to OLLAMA_PC_ENDPOINT: when services/pc_host
reports the PC lost (a new "generation" - gone, draining or busy since the
request started), the request is cancelled - headers or body, mid-stream
included - and ``PcLost`` is raised. PcLost IS an httpx.ConnectError, so
every caller already handles it as "Ollama not reachable": a verdict fails
closed, an enrichment item stays due, a chat turn may retry on the NAS.
Requests to any other host pass straight through.
"""
from __future__ import annotations

import asyncio
import contextlib

import httpx

from src.services.http_client import tls_context

_WATCH_S = 0.5


class PcLost(httpx.ConnectError):
    """The gaming PC stopped taking work while this call was on it."""


def _pc_origin():
    from src.services.ollama_hosts import pc_host
    h = pc_host()
    if h is None:
        return None
    u = httpx.URL(h.url)
    return (u.scheme, u.host, u.port)


def is_pc(url: httpx.URL) -> bool:
    o = _pc_origin()
    return o is not None and (url.scheme, url.host, url.port) == o


async def _until_lost(gen: int) -> None:
    from src.services import pc_host
    while pc_host.generation() == gen:
        await asyncio.sleep(_WATCH_S)


async def _race(coro, gen: int, request: httpx.Request):
    """``coro``'s result, unless the PC is lost first: then it is cancelled
    and PcLost raised."""
    task = asyncio.ensure_future(coro)
    watch = asyncio.ensure_future(_until_lost(gen))
    try:
        done, _ = await asyncio.wait({task, watch}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        task.cancel()
        watch.cancel()
        raise
    if task in done:
        watch.cancel()
        return task.result()
    task.cancel()
    with contextlib.suppress(BaseException):
        await task
    raise PcLost("the gaming PC stopped taking work mid-call", request=request)


async def _next(it):
    try:
        return False, await it.__anext__()
    except StopAsyncIteration:
        return True, None


class _GuardedStream(httpx.AsyncByteStream):
    """The response body, each read raced against the PC's loss."""

    def __init__(self, inner, gen: int, request: httpx.Request):
        self._inner, self._gen, self._request = inner, gen, request

    async def __aiter__(self):
        it = self._inner.__aiter__()
        while True:
            done, chunk = await _race(_next(it), self._gen, self._request)
            if done:
                return
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class GuardedTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not is_pc(request.url):
            return await super().handle_async_request(request)
        from src.services import pc_host
        gen = pc_host.generation()
        response = await _race(super().handle_async_request(request), gen, request)
        response.stream = _GuardedStream(response.stream, gen, request)
        return response


def ollama_client(timeout, **kwargs) -> httpx.AsyncClient:
    """The client for model calls: like httpx.AsyncClient(verify=tls_context(),
    timeout=...), with calls to the gaming PC cut off when it leaves."""
    return httpx.AsyncClient(verify=tls_context(), timeout=timeout,
                             transport=GuardedTransport(verify=tls_context()), **kwargs)
