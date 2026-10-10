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
from typing import Optional

import httpx

# Never a media server, always an SSRF target: link-local (where the
# AWS/GCP/Azure metadata services answer), AWS's IPv6 metadata address and
# Alibaba's. A link-local Plex is not a setup anyone runs.
_REFUSED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "169.254.0.0/16", "fe80::/10", "fd00:ec2::254/128", "100.100.100.200/32",
))
_DEFAULT_PORTS = {"http": 80, "https": 443}


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
    try:
        ip = ipaddress.ip_address(u.host)
    except ValueError:
        ip = None
    if ip is not None:
        # Order matters: link-local and 0.0.0.0 both count as is_private,
        # and ::1 sits in a reserved block - so the refusals come first and
        # only the reserved check gives way to loopback/LAN.
        if any(ip in n for n in _REFUSED_NETS):
            raise ValueError(f"{u.host} is a link-local / metadata address, not a media service")
        if ip.is_unspecified or ip.is_multicast or (
                ip.is_reserved and not (ip.is_loopback or ip.is_private)):
            raise ValueError(f"{u.host} is not an address a service can be reached at")
    return str(u).rstrip("/")


def _origin(url: str) -> tuple:
    u = httpx.URL(url)
    return (u.scheme, (u.host or "").lower(), u.port or _DEFAULT_PORTS.get(u.scheme))


def same_origin(a: str, b: str) -> bool:
    try:
        return bool(a and b) and _origin(a) == _origin(b)
    except Exception:  # noqa: BLE001 - unparseable is simply "not the same"
        return False


def secret_for_target(supplied_url: Optional[str], supplied_secret: Optional[str],
                      stored_url: Optional[str], stored_secret: Optional[str]) -> str:
    """The secret to send with a connection test. A typed secret goes wherever
    the admin points it; the SAVED one only to the saved address (scheme, host
    and port). ValueError when a new address comes without its own secret."""
    if supplied_secret:
        return supplied_secret
    if not supplied_url or same_origin(supplied_url, stored_url or ""):
        return stored_secret or ""
    raise ValueError("Enter the key for this new address - the saved key is "
                     "only ever sent to the saved address")
