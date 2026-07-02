"""First-run setup wizard — status, IP detection, and completion.

IP detection is deliberately careful: it reports the public IP as seen from the
internet AND classifies every local interface, loudly flagging Tailscale / VPN /
CGNAT / private-VPC addresses so a user is never told to point DNS at an address
that isn't reachable from the public internet.
"""

import ipaddress
import json
import subprocess
import urllib.request
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import auth as A
from app.api.dependencies import get_db, verify_auth
from app.core.db_manager import DBManager

router = APIRouter()

_SETTINGS_ID = "app"
_PUBLIC_IP_SOURCES = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)


def _settings(db: DBManager) -> dict:
    return db.db.settings.find_one({"_id": _SETTINGS_ID}) or {}


def _save_settings(db: DBManager, patch: dict) -> None:
    db.db.settings.update_one({"_id": _SETTINGS_ID}, {"$set": patch}, upsert=True)


# ---- IP detection -------------------------------------------------------

def _fetch_public_ip() -> Optional[str]:
    for url in _PUBLIC_IP_SOURCES:
        try:
            with urllib.request.urlopen(url, timeout=4) as r:  # noqa: S310 (known hosts)
                ip = r.read().decode().strip()
                ipaddress.ip_address(ip)  # validate
                return ip
        except Exception:
            continue
    return None


def _classify(ip: str, iface: str) -> str:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    il = iface.lower()
    if il.startswith(("tailscale", "ts")):
        return "tailscale"
    if il.startswith(("docker", "br-", "veth", "cni", "flannel", "cali")):
        return "docker"
    if il.startswith(("wg", "tun", "tap", "zt", "vpn")):
        return "vpn"
    if a.is_loopback:
        return "loopback"
    if a in ipaddress.ip_network("100.64.0.0/10"):
        return "cgnat"  # carrier-grade NAT — also Tailscale's range
    if a.is_private:
        return "private"
    if a.is_global:
        return "public"
    return "other"


def _interfaces() -> List[dict]:
    try:
        r = subprocess.run(
            ["ip", "-j", "addr"], capture_output=True, text=True, timeout=5, check=False
        )
        data = json.loads(r.stdout or "[]")
    except Exception:
        return []
    out = []
    for link in data:
        name = link.get("ifname", "")
        for a in link.get("addr_info", []):
            if a.get("family") != "inet":
                continue  # IPv4 for now
            ip = a.get("local")
            if ip:
                out.append({"iface": name, "ip": ip, "kind": _classify(ip, name)})
    return out


_VPN_VPC_LABEL = {
    "tailscale": "Tailscale VPN",
    "vpn": "VPN tunnel",
    "cgnat": "CGNAT / VPN",
    "private": "private / VPC",
    "docker": "Docker bridge",
}


@router.get("/detect-ip")
def detect_ip(_: str = Depends(verify_auth)):
    public_ip = _fetch_public_ip()
    ifaces = _interfaces()
    warnings: List[str] = []
    # Warn only for addresses a user might mistake for their public IP — VPN/VPC.
    # Docker bridges + loopback are classified but not worth a warning each.
    for i in ifaces:
        if i["kind"] in ("tailscale", "vpn", "cgnat", "private"):
            warnings.append(
                f"{i['ip']} ({i['iface']}) is a {_VPN_VPC_LABEL[i['kind']]} address "
                f"— NOT reachable from the public internet; don't point DNS at it."
            )
    if not public_ip:
        warnings.append(
            "No public IP detected — this host is likely behind NAT. Use the "
            "Tailscale or Cloudflare Tunnel option instead of a domain + DNS."
        )
    return {
        "public_ip": public_ip,        # point your DNS A-record at this
        "recommended": public_ip,
        "interfaces": ifaces,
        "warnings": warnings,
    }


# ---- status + completion ------------------------------------------------

@router.get("/status")
def status(db: DBManager = Depends(get_db)):
    s = _settings(db)
    return {
        "setup_complete": bool(s.get("setup_complete")),
        "server_name": s.get("server_name"),
        "role": s.get("role"),
        "exposure": s.get("exposure"),
    }


class CompleteRequest(BaseModel):
    server_name: Optional[str] = None
    role: str = "standalone"             # standalone | primary | secondary
    exposure: str = "domain"             # domain | tailscale | cloudflare
    domain: Optional[str] = None
    primary_url: Optional[str] = None    # for secondary
    join_token: Optional[str] = None     # for secondary
    advertise_url: Optional[str] = None  # this node's own reachable address (any transport)
    priority: Optional[int] = None       # 1-99; secondary picks a free one (primary auto = 1)
    # Optional AI labeling — any OpenAI-compatible endpoint (OpenAI / local Ollama / …).
    ai_endpoint: Optional[str] = None
    ai_key: Optional[str] = None
    ai_model: Optional[str] = None


def enroll_secondary(db: DBManager, node_id: str, primary_url: str, join_token: str,
                     advertise_url: str, priority: int) -> dict:
    """Shared secondary-enroll used by BOTH the installer path (/api/setup/complete) and
    the Admin tab (/api/cluster/join), so the two can NEVER drift. Runs the bidirectional
    reachability handshake against the primary (the canonical enroll API); on success,
    persists this node's cluster self-record as a secondary. Returns {ok, directions, reason}."""
    from app import federation as F
    from app.cli_install import normalize_url
    advertise_url = normalize_url(advertise_url)
    primary_url = normalize_url(primary_url)
    result = F.enroll_with_primary(primary_url, advertise_url, join_token, node_id, priority)
    if result.get("ok"):
        db.db.cluster.update_one(
            {"_id": "self"},
            {"$set": {"node_id": node_id, "priority": int(priority), "address": advertise_url,
                      "is_primary": False, "override": False}},
            upsert=True,
        )
        # Store the shared fleet secret delivered over the trusted enroll channel (+ the
        # creds to re-sync it later), then strip it so it never propagates to the HTTP caller.
        sec = result.pop("fleet_secret", None)
        if sec:
            from app.fleet_auth import store_fleet_config
            store_fleet_config(db, primary_url, join_token, sec)
    return result


@router.post("/complete")
def complete(req: CompleteRequest, actor: str = Depends(verify_auth), db: DBManager = Depends(get_db)):
    node_id = _server_id()
    # The first node (primary/standalone) auto-takes priority 1 and is primary.
    priority = 1 if req.role in ("primary", "standalone") else req.priority

    if req.role == "secondary":
        if not (req.primary_url and req.join_token and req.advertise_url and req.priority):
            raise HTTPException(
                status_code=400,
                detail="secondary needs primary_url, join_token, advertise_url, and a priority (1-99)",
            )
        result = enroll_secondary(db, node_id, req.primary_url, req.join_token,
                                  req.advertise_url, req.priority)
        if not result.get("ok"):
            raise HTTPException(
                status_code=400,
                detail={"message": "enrollment refused",
                        "directions": result.get("directions", {}),
                        "reason": result.get("reason")},
            )
    else:
        # primary / standalone: this node serves directly (its own cluster self-record).
        db.db.cluster.update_one(
            {"_id": "self"},
            {"$set": {"node_id": node_id, "priority": priority, "address": req.advertise_url,
                      "is_primary": True, "override": False}},
            upsert=True,
        )
        db.db.settings.update_one({"_id": "app"}, {"$set": {"primary_node": node_id}}, upsert=True)

    patch = {
        "setup_complete": True,
        "server_name": req.server_name,
        "role": req.role,
        "exposure": req.exposure,
        "domain": req.domain,
        "primary_url": req.primary_url,
        "join_token": req.join_token,
        "advertise_url": req.advertise_url,
        "priority": priority,
        "completed_by": actor,
    }
    # Only overwrite AI config when provided (so re-running setup doesn't wipe it).
    if req.ai_endpoint is not None:
        patch["ai_endpoint"] = req.ai_endpoint
    if req.ai_key:
        patch["ai_key"] = req.ai_key
    if req.ai_model:
        patch["ai_model"] = req.ai_model
    _save_settings(db, patch)
    return {"ok": True, "setup_complete": True, "role": req.role}


def _server_id() -> str:
    from app.core.config_loader import get_config
    return get_config().server.id
