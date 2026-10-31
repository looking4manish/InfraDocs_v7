"""First-run setup — detection, the wizard's completion, and joining an existing replica set.

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

import os
import secrets
import threading
from pathlib import Path

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


# ---- detection -----------------------------------------------------------

def _setup_code() -> str:
    return os.environ.get("INFRADOCS_SETUP_CODE", "")


def _setup_open(db: DBManager) -> bool:
    return not _settings(db).get("setup_complete")


def _require_open_with_code(db: DBManager, code: Optional[str]) -> None:
    if not _setup_open(db):
        raise HTTPException(status_code=409, detail="setup is already complete")
    expected = _setup_code()
    if not expected:
        raise HTTPException(status_code=403, detail="installer did not set a setup code")
    if not code or not secrets.compare_digest(code.strip(), expected):
        raise HTTPException(status_code=403, detail="setup code does not match")


def _web_port() -> int:
    try:
        return int(os.environ.get("INFRADOCS_WEB_PORT", "8081"))
    except ValueError:
        return 8081


def _projects_root() -> str:
    from app.core.config_loader import get_config
    return get_config().paths.projects_root


def _count_projects(root: str) -> Optional[int]:
    base = Path(os.environ.get("INFRADOCS_HOST_ROOT", "/") + root) if os.environ.get("INFRADOCS_HOST_ROOT") else Path(root)
    for b in (Path(root), base):
        try:
            return sum(1 for c in b.iterdir() if c.is_dir() and not c.name.startswith("."))
        except OSError:
            continue
    return None


def _docker_version() -> Optional[str]:
    try:
        import docker
        return docker.from_env().version().get("Version")
    except Exception:  # noqa: BLE001
        return None


def _mongo_ok(db: DBManager) -> bool:
    try:
        db.db.command("ping")
        return True
    except Exception:  # noqa: BLE001
        return False


def detect(db: DBManager) -> dict:
    from app.cluster_runtime import host_facts
    ifaces = _interfaces()
    ts = next((i["ip"] for i in ifaces if i["kind"] == "tailscale"), None) or os.environ.get("INFRADOCS_TAILSCALE_IP")
    ts_dns = os.environ.get("INFRADOCS_TAILSCALE_DNS") or None
    public_ip = os.environ.get("INFRADOCS_PUBLIC_IP") or None
    host = host_facts()
    root = _projects_root()
    found = _count_projects(root)
    docker_v = _docker_version()
    port = _web_port()
    host_mounted = os.path.isdir(os.path.join(os.environ.get("INFRADOCS_HOST_ROOT", "/"), "etc"))
    checks = [
        {"id": "port", "ok": True, "label": f"Port {port} is serving"},
        {"id": "docker", "ok": bool(docker_v), "label": "Docker socket readable" if docker_v else "Docker socket not readable"},
        {"id": "host", "ok": host_mounted, "label": "Host filesystem mounted read-only" if host_mounted else "Host filesystem not mounted"},
        {"id": "mongo", "ok": _mongo_ok(db), "label": "MongoDB started"},
        {"id": "projects", "ok": found is not None, "label": f"{found} project folders found" if found is not None else f"{root} not readable"},
    ]
    return {"hostname": _server_id(), "os": host.get("os"), "cpu_count": host.get("cpu_count"),
            "mem_gb": host.get("mem_gb"), "tailscale_ip": ts, "tailscale_dns": ts_dns, "public_ip": public_ip,
            "docker_version": docker_v, "port": port, "projects_folder": root, "projects_found": found,
            "checks": checks}


def _suggested_address(d: dict) -> str:
    host = d.get("tailscale_dns") or d.get("tailscale_ip") or d.get("public_ip") or "localhost"
    return f"http://{host}:{d['port']}"


@router.get("/status")
def status(db: DBManager = Depends(get_db)):
    s = _settings(db)
    out = {"setup_complete": bool(s.get("setup_complete")), "server_name": s.get("server_name"),
           "role": s.get("role"), "needs_setup_code": bool(_setup_code())}
    if not out["setup_complete"]:
        d = detect(db)
        out["detected"] = d
        out["suggested_address"] = _suggested_address(d)
    return out


class AddressCheck(BaseModel):
    address: str


@router.post("/check-address")
def check_address(req: AddressCheck, db: DBManager = Depends(get_db)):
    """Is this address really THIS server? Other servers will use it to reach the primary."""
    from app.federation import _get_json, probe_failure_reason
    if not _setup_open(db):
        raise HTTPException(status_code=409, detail="setup is already complete")
    addr = req.address.strip().rstrip("/")
    if not addr.startswith(("http://", "https://")):
        return {"reachable": False, "reason": "address must start with http:// or https://"}
    try:
        who = _get_json(addr + "/api/cluster/rpc/ping", timeout=6)
    except Exception as e:  # noqa: BLE001
        return {"reachable": False, "reason": probe_failure_reason(addr, e)}
    if who.get("node_id") != _server_id():
        return {"reachable": False, "reason": f"that address answers as {who.get('node_id')}"}
    return {"reachable": True, "reason": None}


class Admin(BaseModel):
    username: str
    password: str


class Access(BaseModel):
    mode: str = "tailscale"
    address: str


class CompleteRequest(BaseModel):
    setup_code: Optional[str] = None
    admin: Admin
    server_name: Optional[str] = None
    projects_folder: Optional[str] = None
    access: Access


def _validate_admin(a: Admin) -> None:
    import re
    if not re.fullmatch(r"[a-z][a-z0-9._-]{1,31}", a.username or ""):
        raise HTTPException(status_code=400, detail="username: lower-case letters, digits, . _ -")
    if len(a.password or "") < 12:
        raise HTTPException(status_code=400, detail="password: at least 12 characters")


def _start_first_scan(cfg_server_id: str) -> None:
    def run():
        try:
            from app.api.routers.scans import run_scan_now
            run_scan_now()
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger("app.setup").warning("first scan failed: %s", e)
    threading.Thread(target=run, name="first-scan", daemon=True).start()


@router.post("/complete")
def complete(req: CompleteRequest, db: DBManager = Depends(get_db)):
    """First server: create the administrator, record how it is reached, form a one-member
    replica set with this server as primary, and start the first scan."""
    from app import cluster_runtime as RT
    _require_open_with_code(db, req.setup_code)
    _validate_admin(req.admin)
    if req.access.mode not in ("tailscale", "domain", "cloudflare"):
        raise HTTPException(status_code=400, detail="access mode must be tailscale, domain or cloudflare")
    addr = req.access.address.strip().rstrip("/")
    if not addr.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="address must start with http:// or https://")
    node_id = _server_id()
    db.db.users.delete_many({})
    db.db.users.insert_one({"username": req.admin.username, "password_hash": A.hash_password(req.admin.password),
                            "must_change_password": False})
    _save_settings(db, {"setup_complete": True, "server_name": (req.server_name or node_id).strip(),
                        "role": "primary", "exposure": req.access.mode, "advertise_url": addr,
                        "projects_folder": req.projects_folder or _projects_root(),
                        "completed_by": req.admin.username})
    RT.bootstrap_first_server(db, node_id, addr)
    RT.start(db, node_id)
    _start_first_scan(node_id)
    return {"ok": True, "token": A.create_session(db, req.admin.username), "scan_started": True}


class JoinRequest(BaseModel):
    setup_code: Optional[str] = None
    primary_url: str
    code: str
    address: str
    server_name: Optional[str] = None


@router.post("/join")
def join(req: JoinRequest, db: DBManager = Depends(get_db)):
    """Later servers: called by the installer on this server. Presents the join code to the
    primary, which connects back and adds this server; then this server follows the primary."""
    from app import cluster_runtime as RT
    from app.federation import _post_json, probe_failure_reason
    from app.fleet_auth import store_fleet_config
    import urllib.error
    import json as _json
    _require_open_with_code(db, req.setup_code)
    node_id = _server_id()
    addr = req.address.strip().rstrip("/")
    primary = req.primary_url.strip().rstrip("/")
    try:
        res = _post_json(primary + "/api/cluster/rpc/join",
                         {"code": req.code, "node_id": node_id, "address": addr, "version": RT.VERSION}, timeout=30)
    except urllib.error.HTTPError as e:
        try:
            detail = _json.loads(e.read().decode()).get("detail")
        except Exception:  # noqa: BLE001
            detail = f"HTTP {e.code}"
        raise HTTPException(status_code=400, detail=f"primary refused: {detail}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="cannot reach the primary: " + probe_failure_reason(primary, e))
    store_fleet_config(db, primary, "", res["fleet_secret"])
    db.db.users.delete_many({})
    for u in res.get("users") or []:
        if u.get("username") and u.get("password_hash"):
            db.db.users.insert_one(u)
    RT.adopt_joined_state(db, res["term"], res["config"], res.get("leader_id"))
    _save_settings(db, {"setup_complete": True, "server_name": (req.server_name or node_id).strip(),
                        "role": "secondary", "advertise_url": addr, "joined_primary": primary})
    RT.start(db, node_id)
    _start_first_scan(node_id)
    from app import election as E
    cfg = E.Config.from_dict(res["config"])
    mode, why = E.failover_mode(cfg)
    me = cfg.member(node_id)
    return {"ok": True, "node_id": node_id, "votes": me.votes if me else None, "mode": mode, "mode_reason": why}


def _server_id() -> str:
    from app.core.config_loader import get_config
    return get_config().server.id
