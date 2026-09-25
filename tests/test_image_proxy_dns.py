"""The image proxy refuses a whitelisted name that resolves to a private,
loopback or link-local address, before the first connection and before
every redirect hop (DNS rebinding, 2026-09-25).

    python tests/test_image_proxy_dns.py
"""
import asyncio
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import src.routers.image_proxy as ip  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def resolves(host, resolver):
    ip._resolved.clear()
    return asyncio.run(ip._host_resolves_public(host, resolver=resolver))


check("public addresses pass", resolves("image.tmdb.org", lambda h: ["151.101.1.91", "2a04:4e42::347"]))
for bad in ("192.168.1.100", "10.0.0.7", "172.16.4.4", "127.0.0.1", "::1", "169.254.169.254",
            "fe80::1%eth0", "0.0.0.0", "224.0.0.1"):
    check(f"{bad} is refused", not resolves("evil.example", lambda h, b=bad: ["151.101.1.91", b]))
check("a name that does not resolve is refused", not resolves("nope.example", lambda h: []))


def boom(h):
    raise OSError("resolver down")


check("a resolver error is refused, not trusted", not resolves("nope.example", boom))

calls = []


def counting(h):
    calls.append(h)
    return ["151.101.1.91"]


ip._resolved.clear()
asyncio.run(ip._host_resolves_public("image.tmdb.org", resolver=counting))
asyncio.run(ip._host_resolves_public("image.tmdb.org", resolver=counting))
check("verdicts are cached per host", calls == ["image.tmdb.org"])
check("garbage is not an address", not ip._ip_is_public("not-an-ip"))

src = (_ROOT / "src/routers/image_proxy.py").read_text(encoding="utf-8")
fetch = src[src.index("async def proxy_image"):]
check("wiring: the initial host and every redirect target are resolved before connecting",
      fetch.count("await _host_resolves_public(") == 2
      and fetch.index("await _host_resolves_public(parsed.host") < fetch.index("httpx.AsyncClient(")
      and fetch.index("_host_allowed(target_host)") < fetch.index("await _host_resolves_public(target_host)"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
