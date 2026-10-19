"""Where a configured service URL may point, and which secret goes with it.

Two rules for the operator-supplied endpoints (Plex, the arrs, Ollama,
SoulSync), shared by the setup wizard and the library panel (2026-10-10 audit):

* ``validate_service_url`` - a base URL is http(s), carries no credentials,
  query or fragment, and is not a link-local / cloud-metadata / multicast /
  unspecified address. A '#' or '?' would cut off or swallow the fixed API
  path the clients append ("http://10.0.0.1/secret#" + "/identity" fetches
  /secret), turning "Test connection" into a fetch-anything. LAN and loopback
  stay legal - that is where Plex and the arrs live.

* ``secret_for_target`` - "Test connection" with a blank key field uses the
  saved key, but only against the saved address. Otherwise a typed URL and an
  empty key field sent the stored Plex token or arr key to any host, undoing
  the write-only masking of the integrations panel.
"""
import ipaddress
import logging
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Never a media server, always an SSRF target: link-local (where the
# AWS/GCP/Azure metadata services answer), AWS's IPv6 metadata address and
# Alibaba's. A link-local Plex is not a setup anyone runs.
_REFUSED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "169.254.0.0/16", "fe80::/10", "fd00:ec2::254/128", "100.100.100.200/32",
))
_DEFAULT_PORTS = {"http": 80, "https": 443}
# Every spelling inet_aton accepts: 1-4 dot-separated decimal, octal or hex
# parts (the last may be empty only before a trailing dot, stripped first).
_NUMERIC_HOST = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)(?:\.(?:0x[0-9a-f]*|[0-9]+)){0,3}",
                           re.IGNORECASE)


def validate_service_url(raw: Optional[str]) -> Optional[str]:
    """The normalised base URL (no trailing slash), or ValueError naming what
    is wrong. None and blank pass through unchanged - they mean "unset" or
    "keep the saved value" to the callers."""
    if raw is None or not raw.strip():
        return raw
    raw = raw.strip()
    try:
        u = httpx.URL(raw)
    except Exception:
        raise ValueError("not a valid URL")
    if u.scheme not in ("http", "https"):
        raise ValueError("only http:// and https:// addresses are supported")
    if not u.host:
        raise ValueError("the URL has no host")
    if u.userinfo:
        raise ValueError("put credentials in the key field, not in the URL")
    if u.query or u.fragment or "?" in raw or "#" in raw:
        raise ValueError("a service address cannot carry a query string or fragment")
    # A trailing dot is a legal absolute name, and "169.254.169.254." still
    # connects to the metadata service: check what is left without it.
    host = u.host.rstrip(".")
    # inet_aton - what the connect underneath resolves with - also takes
    # 2852039166, 0xa9fea9fe, 0251.0376.0251.0376, 169.254.43518 and "0".
    # None of those parse as an ip_address below, so each one walked past
    # every refusal (audit 2026-10-15). A media server's address is written
    # as four decimal numbers; any other all-numeric host is refused.
    if _NUMERIC_HOST.fullmatch(host):
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise ValueError(f"write {u.host} as a plain IPv4 address (four "
                             "decimal numbers) or a host name")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        why = _refusal_for_ip(ip)
        if why:
            raise ValueError(f"{u.host} is {why}")
    return str(u).rstrip("/")


def _refusal_for_ip(ip) -> Optional[str]:
    """Why a service may not live at ``ip``, or None. LAN and loopback pass."""
    if ip.version == 6 and ip.ipv4_mapped:
        # [::ffff:169.254.169.254] is IPv4 on the wire, and an IPv4Network
        # never "contains" an IPv6 address - judge the address it maps to.
        ip = ip.ipv4_mapped
    # Order matters: link-local and 0.0.0.0 both count as is_private,
    # and ::1 sits in a reserved block - so the refusals come first and
    # only the reserved check gives way to loopback/LAN.
    if any(ip in n for n in _REFUSED_NETS) or ip.is_link_local:
        return "a link-local / metadata address, not a media service"
    if ip.is_unspecified or ip.is_multicast or (
            ip.is_reserved and not (ip.is_loopback or ip.is_private)):
        return "not an address a service can be reached at"
    return None


# Resolving is bounded: an address that does not resolve fails the connect
# by itself, and a slow resolver must not hold a connection test for long.
_RESOLVE_TIMEOUT_S = 3.0


async def resolved_refusal(url: str) -> Optional[str]:
    """Why ``url``'s host NAME must not be connected to, or None.

    ``validate_service_url`` sees only literal addresses: a name such as
    ``metadata.example`` that resolves to 169.254.169.254 passed it (the
    2026-10-10 audit left this open). Called right before a connection test
    connects; every address the name resolves to must be acceptable. A name
    that does not resolve is left to fail the connect on its own. A lookup
    that times out or errors any other way is a refusal: answering "fine"
    there let a slow resolver walk past the check."""
    import asyncio
    import socket
    try:
        host = (httpx.URL(url).host or "").rstrip(".")
    except (httpx.InvalidURL, ValueError, TypeError):
        return None                       # validate_service_url refused it already
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return None                       # a literal - the validator judged it
    except ValueError:
        pass
    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, None), _RESOLVE_TIMEOUT_S)
    except socket.gaierror:
        return None                       # does not resolve: the connect fails by itself
    except asyncio.TimeoutError:
        logger.warning("[endpoint] %s did not resolve within %.0f s - refused", host,
                       _RESOLVE_TIMEOUT_S)
        return f"{host} could not be resolved in time - try again, or use its IP address"
    except Exception as e:
        logger.warning("[endpoint] resolving %s failed - refused: %s", host, e)
        return f"{host} could not be resolved ({type(e).__name__}) - use its IP address"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except ValueError:
            continue
        why = _refusal_for_ip(ip)
        if why:
            return f"{host} resolves to {ip}, {why}"
    return None


def _origin(url: str) -> tuple:
    u = httpx.URL(url)
    return (u.scheme, (u.host or "").lower(), u.port or _DEFAULT_PORTS.get(u.scheme))


def same_origin(a: str, b: str) -> bool:
    try:
        return bool(a and b) and _origin(a) == _origin(b)
    except Exception:  # noqa: BLE001 - unparseable is simply "not the same"
        return False


# The refusal. The routers answer with this constant, never str(exc): any
# exception text in a response is what CodeQL's py/stack-trace-exposure flags.
NEW_ADDRESS_NEEDS_KEY = ("Enter the key for this new address - the saved key is "
                         "only ever sent to the saved address")


def secret_for_target(supplied_url: Optional[str], supplied_secret: Optional[str],
                      stored_url: Optional[str], stored_secret: Optional[str]) -> str:
    """The secret to send with a connection test. A typed secret goes wherever
    the admin points it; the SAVED one only to the saved address (scheme, host
    and port). ValueError when a new address comes without its own secret."""
    if supplied_secret:
        return supplied_secret
    if not supplied_url or same_origin(supplied_url, stored_url or ""):
        return stored_secret or ""
    raise ValueError(NEW_ADDRESS_NEEDS_KEY)
