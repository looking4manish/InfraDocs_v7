"""Server-to-server HTTP helpers: scan push to the primary, plain JSON calls, and readable
reasons for a failed reachability probe."""

import json
import urllib.request
from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit


def normalize_url(url, default_port: int = 8081) -> str:
    """Give an address an explicit port (http://host -> http://host:8081) so a probe never
    lands on 80/443 by accident."""
    if not url:
        return url
    u = url.strip()
    if "://" not in u:
        u = "http://" + u
    parts = urlsplit(u)
    host = parts.hostname
    if host and parts.port is None and parts.scheme == "http":
        netloc = f"[{host}]" if ":" in host else host
        parts = parts._replace(netloc=f"{netloc}:{default_port}")
    return urlunsplit(parts).rstrip("/")


def probe_failure_reason(url, exc) -> str:
    """Translate a reachability-probe exception into a short, named reason."""
    msg = str(exc).lower()
    u = url if "://" in url else "http://" + url
    try:
        parts = urlsplit(u)
        has_port, scheme = parts.port is not None, parts.scheme
    except ValueError:
        has_port, scheme = True, "http"
    if any(t in msg for t in ("tlsv1", "ssl", "wrong version number", "unknown protocol",
                              "alert internal error", "record layer", "certificate", "eof occurred")):
        if (not has_port) or scheme == "https":
            return f"{url} answered with TLS; use http:// and the explicit port, e.g. :8081"
        return f"{url} answered with TLS where plain http was expected"
    if any(t in msg for t in ("refused", "timed out", "timeout", "no route", "unreachable",
                              "could not connect", "reset by peer", "name or service not known",
                              "temporary failure in name resolution")):
        return f"{url} did not respond — check the address and the firewall"
    return f"could not reach {url} ({type(exc).__name__})"


def _post_json(url: str, body: dict, headers: Optional[dict] = None, timeout: int = 25) -> dict:
    data = json.dumps(body, default=str).encode()
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method="POST", headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode())


def _get_json(url: str, timeout: int = 8) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode())


def push_to_primary(primary_url: str, fleet_secret: str, server_id: str, assets: List[dict],
                    applications: List[dict], term: int = 0, timeout: int = 25) -> dict:
    """POST this server's scan to the primary. The term lets a stale primary refuse it."""
    return _post_json(primary_url.rstrip("/") + "/api/federation/ingest",
                      {"server_id": server_id, "assets": assets, "applications": applications, "term": term},
                      headers={"X-Fleet-Auth": fleet_secret or ""}, timeout=timeout)
