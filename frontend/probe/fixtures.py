"""Fixture API responses for the screenshot probe: the mockup's sample fleet, shaped like
the real endpoints (app/api/routers/*) and the V7 contract (docs/V7_API_CONTRACT.md)."""
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc)
GB = 1024 ** 3
MB = 1024 ** 2


def ago(**kw):
    return (NOW - timedelta(**kw)).isoformat()


def ahead(**kw):
    return (NOW + timedelta(**kw)).isoformat()


def app(name, srv, *, url=None, via="caddy", containers=(), running=True, units=(), ports=(), volumes=(),
        size=0, certs=(), cmp=True, links=("project_dir", "compose_label")):
    cd = [{"name": f"{name}-{c}", "image": img, "running": running, "restarts": 0, "restart_policy": "unless-stopped",
           "has_health_check": True, "started_at": ago(days=6), "host_ports": [], "compose_service": c}
          for c, img in containers]
    a = {
        "_id": name, "name": name, "type": "project", "source": f"/home/msinha/projects/{name}",
        "application_id": f"{srv}:app:{name}", "server_id": srv,
        "containers": [c["name"] for c in cd], "containers_detail": cd, "images": [i for _, i in containers],
        "compose_file": f"/home/msinha/projects/{name}/docker-compose.yml" if cmp and containers else None,
        "systemd_units": list(units), "listening_ports": list(ports),
        "volumes": [{"name": v, "mountpoint": f"/var/lib/docker/volumes/{v}/_data", "size_bytes": s} for v, s in volumes],
        "urls": [f"https://{url}"] if url else [], "internet_exposed": bool(url),
        "exposure": [{"via": via, "hostname": url, "upstream_port": ports[0] if ports else None}] if url else [],
        "nginx_sites": [url] if url and via == "nginx" else [],
        "nginx_detail": [{"server_name": url, "listen_ports": [443], "upstream_host": "127.0.0.1", "upstream_port": ports[0] if ports else None, "has_ssl": True}] if url and via == "nginx" else [],
        "certificates": [c[0] for c in certs],
        "certificates_detail": [{"name": c[0], "domains": [c[0]], "days_until_expiry": c[1], "issuer": "C=US, O=Let's Encrypt, CN=R11",
                                 "not_after": ahead(days=c[1]), "cert_path": f"/data/caddy/certificates/{c[0]}.crt"} for c in certs],
        "port_mappings": [{"host_port": ports[0], "container": cd[0]["name"], "container_port": ports[0]}] if ports and cd else [],
        "project_dir": f"/home/msinha/projects/{name}", "project_dir_size_bytes": int(size * 0.1),
        "total_size_bytes": int(size), "storage_paths": [f"/home/msinha/projects/{name}"],
        "hygiene": {"exited_restart_always": [] if running else [c["name"] for c in cd], "dangling_images": [], "unused_images": [], "orphaned_volumes": []},
        "resilience": {"reboot_safe": True, "issues": []},
        "links": [{"src_kind": "x", "src": name, "dst_kind": "application", "dst": name, "via": v, "pass": 1} for v in links],
        "env_keys": [], "networks": [], "storage_mounts": [], "cloudflare": via == "cloudflare_tunnel",
    }
    a["components_count"] = len(cd) + len(units) + len(volumes) + len(a["nginx_sites"])
    return a


APPS = [
    app("nextcloud", "n150", url="cloud.sinhahub.in", via="cloudflare_tunnel", running=False,
        containers=[("app", "nextcloud:29"), ("db", "mariadb:11")], volumes=[("nextcloud_data", 180 * GB), ("nextcloud_db", 4 * GB), ("nextcloud_redis", 2 * GB)], size=186 * GB, ports=[8080]),
    app("minio", "oci-p", url="s3.sinhahub.in", containers=[("minio", "minio/minio:latest")], ports=[9000, 9001],
        volumes=[("minio_data", 92 * GB)], size=92 * GB, certs=[("s3.sinhahub.in", 9)]),
    app("immich", "n150", url="photos.sinhahub.in", via="cloudflare_tunnel",
        containers=[("server", "ghcr.io/immich-app/immich-server:v1.120"), ("ml", "ghcr.io/immich-app/immich-machine-learning:v1.120"), ("redis", "redis:7"), ("postgres", "tensorchord/pgvecto-rs:pg16")],
        ports=[2283], volumes=[("immich_upload", 400 * GB), ("immich_pg", 8 * GB), ("immich_ml", 3 * GB), ("immich_redis", 1 * GB)], size=412 * GB),
    app("authentik", "oci-p", url="auth.mdbdemo.in", containers=[("server", "ghcr.io/goauthentik/server:2026.5.4"), ("worker", "ghcr.io/goauthentik/server:2026.5.4"), ("postgresql", "postgres:16-alpine")],
        ports=[9000], volumes=[("database", int(1.4 * GB)), ("media", 412 * MB)], size=1.8 * GB, certs=[("auth.mdbdemo.in", 71)],
        links=("project_dir", "compose_label", "host_port_index", "upstream_port:9000", "nginx_domain")),
    app("mdb-playground", "oci", url="lab.mdbdemo.in", via="nginx", units=["mdbpg-backend.service", "mdbpg-studio.service", "mdbpg-app.service"],
        ports=[8110, 8101, 8102], size=18.4 * GB, certs=[("lab.mdbdemo.in", 64)], links=("project_dir", "unit_path", "host_port_index")),
    app("langfuse", "oci", url="langfuse.mdbdemo.in", containers=[("web", "langfuse/langfuse:3"), ("worker", "langfuse/langfuse-worker:3"), ("clickhouse", "clickhouse/clickhouse-server:24"), ("postgres", "postgres:16")],
        ports=[3000, 3030], volumes=[("langfuse_pg", 2 * GB), ("langfuse_ch", int(4.2 * GB))], size=6.2 * GB, certs=[("langfuse.mdbdemo.in", 58)]),
    app("mongodb-rs0", "oci-p", units=["mongod.service"], ports=[27017], size=142 * GB, links=("unit_path", "host_port_index")),
    app("infradocs", "oci", url="infra.sinhahub.in", containers=[("api", "infradocs-api:7.0.0"), ("mongo", "mongo:8.0"), ("web", "caddy:2")],
        ports=[8004], volumes=[("infradocs_db", 600 * MB)], size=640 * MB, certs=[("infra.sinhahub.in", 80)]),
    app("homepage", "oci", url="home.sinhahub.in", containers=[("homepage", "ghcr.io/gethomepage/homepage:latest")], ports=[3001], volumes=[("homepage_cfg", 40 * MB)], size=48 * MB, certs=[("home.sinhahub.in", 80)]),
    app("mxh", "oci-p", url="mxh.mdbdemo.in", via="nginx", units=["mxh-api.service", "mxh-web.service"], ports=[8200, 8201], size=3.1 * GB, certs=[("mxh.mdbdemo.in", 47)]),
    app("adguard", "n150", containers=[("adguard", "adguard/adguardhome:latest")], ports=[53, 3000], volumes=[("adguard_work", 200 * MB)], size=220 * MB),
    app("jellyfin", "n150", containers=[("jellyfin", "jellyfin/jellyfin:10.10")], ports=[8096], volumes=[("jf_config", 2 * GB), ("jf_cache", 400 * MB)], size=2.4 * GB),
    app("uptime-kuma", "oci", containers=[("kuma", "louislam/uptime-kuma:1")], ports=[3002], volumes=[("kuma_data", 90 * MB)], size=96 * MB),
    app("backup-scripts", "n150", size=12 * MB, links=("project_dir",)),
]
SYSTEM = {"_id": "sys", "name": "System", "type": "system", "application_id": "oci:app:System", "server_id": "oci",
          "containers": [], "containers_detail": [], "systemd_units": [f"svc{i}.service" for i in range(24)], "listening_ports": [22, 5678],
          "volumes": [], "images": ["a", "b", "c", "d", "e"], "components_count": 24, "urls": [], "exposure": [], "links": [],
          "total_size_bytes": 0, "certificates_detail": [], "hygiene": {}}

APPLICATIONS = {"count": len(APPS) + 1, "applications": APPS + [SYSTEM]}


def ep(host, service, srv, via, scope, url=None, recognized=None, ai=False, purpose=None, kind="web"):
    return {"url": url, "host": host, "kind": kind, "server": srv, "service": service, "via": via, "scope": scope,
            "recognized": recognized, "ai": ai, "purpose": purpose, "access": ""}


ENDPOINTS = {"endpoints": [
    ep("auth.mdbdemo.in", "authentik", "oci-p", "caddy", "public", "https://auth.mdbdemo.in", "Identity provider"),
    ep("lab.mdbdemo.in", "mdb-playground", "oci", "nginx", "public", "https://lab.mdbdemo.in", "Studio and labs"),
    ep("langfuse.mdbdemo.in", "langfuse", "oci", "caddy", "public", "https://langfuse.mdbdemo.in", "LLM tracing"),
    ep("mxh.mdbdemo.in", "mxh", "oci-p", "nginx", "public", "https://mxh.mdbdemo.in", "Experience Hub"),
    ep("home.sinhahub.in", "homepage", "oci", "caddy", "public", "https://home.sinhahub.in", "Start page"),
    ep("infra.sinhahub.in", "infradocs", "oci", "caddy", "public", "https://infra.sinhahub.in", "This console"),
    ep("photos.sinhahub.in", "immich", "n150", "cloudflare_tunnel", "public", "https://photos.sinhahub.in", "Photo library"),
    ep("cloud.sinhahub.in", "nextcloud", "n150", "cloudflare_tunnel", "public", "https://cloud.sinhahub.in", "Files"),
    ep("s3.sinhahub.in", "minio", "oci-p", "caddy", "public", "https://s3.sinhahub.in", "Object storage"),
    ep("100.72.146.5:3000", "adguard", "n150", "direct port", "tailnet", "http://100.72.146.5:3000", "DNS filtering"),
    ep("100.70.18.9:27017", "mongodb-rs0", "oci-p", "direct port", "tailnet", None, "MongoDB", kind="database"),
    ep("100.72.146.5:8096", "jellyfin", "n150", "direct port", "tailnet", "http://100.72.146.5:8096", "Media server"),
    ep("100.107.140.36:3002", "uptime-kuma", "oci", "direct port", "tailnet", "http://100.107.140.36:3002", "Status checks"),
    ep("100.70.18.9:9001", "minio", "oci-p", "direct port", "tailnet", "http://100.70.18.9:9001", "Storage console"),
    ep("100.72.146.5:2283", "immich", "n150", "reverse-proxy backend", "tailnet", "http://100.72.146.5:2283", "Direct access"),
    ep("100.107.140.36:5678", "System", "oci", "direct port", "tailnet", "http://100.107.140.36:5678", None, ai=True, purpose="n8n workflow editor"),
    ep("127.0.0.1:8110", "mdb-playground", "oci", "reverse-proxy backend", "localhost", "http://localhost:8110", "Backend API"),
    ep("127.0.0.1:9000", "authentik", "oci-p", "reverse-proxy backend", "localhost", "http://localhost:9000", "Server"),
    ep("127.0.0.1:3030", "langfuse", "oci", "direct port", "localhost", "http://localhost:3030", "Worker"),
    ep("127.0.0.1:8004", "infradocs", "oci", "reverse-proxy backend", "localhost", "http://localhost:8004", "API"),
    ep("127.0.0.1:5432", "authentik", "oci-p", "direct port", "localhost", None, "PostgreSQL", kind="database"),
]}
ENDPOINTS["count"] = len(ENDPOINTS["endpoints"])


def member(nid, role, prio, addr, os_, cpu, mem, disk, hb, scan_min, overdue=False, beats="1111111111111111", votes=1, self_=False, healthy=True):
    return {"node_id": nid, "address": addr, "priority": prio, "votes": votes, "role": role, "healthy": healthy, "is_self": self_,
            "last_heartbeat_age_s": hb, "last_scan_at": ago(minutes=scan_min), "scan_overdue": overdue, "version": "7.0.0",
            "host": {"os": os_, "cpu_count": 4, "cpu_pct": cpu, "mem_pct": mem, "disk_pct": disk},
            "heartbeats": [c == "1" for c in beats]}


CLUSTER = {
    "node_id": "oci", "role": "primary", "term": 14, "leader_id": "oci", "leader_address": "http://oci.tail6a3622.ts.net:8081",
    "mode": "automatic", "mode_reason": "3 voting members", "voting_members": 3, "quorum": 2, "reachable_voters": 3,
    "has_quorum": True, "primary_since": ago(days=3, hours=8), "settings": {"heartbeat_interval_s": 2, "election_timeout_s": 10},
    "members": [
        member("oci", "primary", 1, "http://oci.tail6a3622.ts.net:8081", "Ubuntu 24.04", 18, 46, 62, 0.0, 2, self_=True),
        member("oci-p", "secondary", 2, "http://oci-p.mdbdemo.in:8081", "Ubuntu 24.04", 31, 71, 54, 1.4, 3),
        member("n150", "secondary", 3, "http://n150.tail6a3622.ts.net:8081", "Ubuntu 24.04", 9, 38, 83, 1.8, 18, overdue=True, beats="1111011111111111"),
    ],
    "elections": [
        {"term": 14, "winner": "oci", "votes_for": 3, "voters": 3, "reason": "oci-p stepped down", "at": ago(days=3, hours=8)},
        {"term": 13, "winner": "oci-p", "votes_for": 2, "voters": 3, "reason": "oci missed heartbeats for 10 s", "at": ago(days=4, hours=19)},
        {"term": 12, "winner": "oci", "votes_for": 3, "voters": 3, "reason": "Replica set formed", "at": ago(days=11)},
    ],
}

ACTIONS = {"actions": [
    {"actor": "manish", "asset_id": "a1", "asset_name": "authentik-worker", "category": "docker_container", "project": "authentik", "action": "restart", "status": "success", "return_code": 0, "duration_ms": 2140, "timestamp": ago(minutes=26), "stdout": "restarted"},
    {"actor": "manish", "asset_id": "a2", "asset_name": "nextcloud-app", "category": "docker_container", "project": "nextcloud", "action": "stop", "status": "success", "return_code": 0, "duration_ms": 10400, "timestamp": ago(minutes=47)},
    {"actor": "manish", "asset_id": "a3", "asset_name": "langfuse-web", "category": "docker_container", "project": "langfuse", "action": "logs", "status": "success", "return_code": 0, "duration_ms": 310, "timestamp": ago(hours=1, minutes=5), "stdout": "ready on :3000"},
    {"actor": "manish", "asset_id": "a4", "asset_name": "immich-ml", "category": "docker_container", "project": "immich", "action": "start", "status": "success", "return_code": 0, "duration_ms": 1800, "timestamp": ago(hours=3)},
    {"actor": "manish", "asset_id": "a5", "asset_name": "nginx", "category": "nginx_server_block", "project": "System", "action": "reload", "status": "refused", "refused_reason": "config test failed", "timestamp": ago(hours=5)},
]}
ACTIONS["count"] = len(ACTIONS["actions"])

ALLOWED = {"allowed": {
    "docker_container": ["start", "stop", "restart", "logs", "inspect", "stats", "check_update"],
    "systemd_service": ["status", "logs", "restart", "start", "stop", "enable", "disable"],
    "systemd_timer": ["status", "trigger", "restart"], "nginx_server_block": ["test", "reload"],
    "docker_image": ["check_update", "pull", "prune"], "docker_volume": ["inspect", "prune"], "storage_mount": ["inspect"],
    "docker_compose": ["update", "up", "restart", "down"],
}, "destructive": {"docker_container": ["stop", "restart"], "docker_volume": ["prune"]}}

SCANS = {"scans": [{"scan_id": "s1", "started_at": ago(minutes=2, seconds=40), "finished_at": ago(minutes=2), "status": "success", "total_assets": 412, "applications_built": 15}], "count": 1}


def asset(cat, name, project, srv, status, meta):
    return {"asset_id": f"{srv}:{cat}:{name}", "category": cat, "name": name, "project": project, "server_id": srv, "status": status,
            "metadata": meta, "discovered_at": ago(minutes=2), "updated_at": ago(minutes=2)}


CERTS = [asset("tls_certificate", d, p, s, "valid", {"domains": [d], "days_until_expiry": n, "not_after": ahead(days=n), "issuer": iss,
                "cert_path": f"/data/caddy/certificates/acme-v02/{d}/{d}.crt", "subject": f"CN={d}", "expiring_soon": n <= 14, "expired": False})
         for d, p, s, n, iss in [
             ("s3.sinhahub.in", "minio", "oci-p", 9, "C=US, O=Let's Encrypt, CN=R11"),
             ("auth.mdbdemo.in", "authentik", "oci-p", 71, "C=US, O=Let's Encrypt, CN=R11"),
             ("lab.mdbdemo.in", "mdb-playground", "oci", 64, "C=US, O=Let's Encrypt, CN=R10"),
             ("langfuse.mdbdemo.in", "langfuse", "oci", 58, "C=US, O=Let's Encrypt, CN=R11"),
             ("mxh.mdbdemo.in", "mxh", "oci-p", 47, "C=US, O=Let's Encrypt, CN=R10"),
             ("home.sinhahub.in", "homepage", "oci", 80, "C=US, O=Let's Encrypt, CN=R11"),
             ("infra.sinhahub.in", "infradocs", "oci", 80, "C=US, O=Let's Encrypt, CN=R11"),
             ("*.sinhahub.in", "System", "n150", 5110, "O=CloudFlare, Inc., CN=CloudFlare Origin Certificate"),
             ("oci-p.mdbdemo.in", "mongodb-rs0", "oci-p", 290, "CN=rs0 member CA"),
             ("s3-console.sinhahub.in", "minio", "oci-p", 52, "C=US, O=Let's Encrypt, CN=R11"),
         ]]
CONTAINERS = [asset("docker_container", c["name"], a["name"], a["server_id"], "running" if c["running"] else "exited",
                    {"image": c["image"], "running": c["running"], "restart_policy": "unless-stopped", "started_at": c["started_at"]})
              for a in APPS for c in a["containers_detail"]]
PORTS = [asset("network_port", f"tcp:{p}", a["name"], a["server_id"], "listening", {"port": p, "protocol": "tcp", "local_address": f"0.0.0.0:{p}", "process": "docker-proxy"})
         for a in APPS for p in a["listening_ports"]] + [asset("network_port", "tcp:5678", "System", "oci", "listening", {"port": 5678, "protocol": "tcp", "local_address": "100.107.140.36:5678", "process": "node"})]
MOUNTS = [asset("storage_mount", t, "System", s, "mounted", {"target": t, "fstype": fs, "usage_pct": u, "total_bytes": tot, "used_bytes": int(tot * u / 100), "source": src})
          for t, s, fs, u, tot, src in [("/", "oci", "ext4", 62, 200 * GB, "/dev/sda1"), ("/", "oci-p", "ext4", 54, 200 * GB, "/dev/sda1"),
                                        ("/", "n150", "ext4", 41, 500 * GB, "/dev/nvme0n1p2"), ("/mnt/media", "n150", "ext4", 83, 2000 * GB, "/dev/sdb1"),
                                        ("/data", "oci-p", "xfs", 38, 500 * GB, "/dev/sdb")]]
ASSETS_BY_CAT = {"tls_certificate": CERTS, "docker_container": CONTAINERS, "network_port": PORTS, "storage_mount": MOUNTS}
CATEGORIES = {"categories": [
    {"category": "docker_container", "count": len(CONTAINERS)}, {"category": "systemd_service", "count": 56},
    {"category": "systemd_timer", "count": 14}, {"category": "cron_job", "count": 9}, {"category": "docker_image", "count": 37},
    {"category": "docker_volume", "count": 18}, {"category": "docker_network", "count": 11}, {"category": "docker_compose", "count": 13},
    {"category": "network_port", "count": len(PORTS)}, {"category": "nginx_server_block", "count": 4}, {"category": "caddy_site", "count": 7},
    {"category": "cloudflare_tunnel", "count": 2}, {"category": "tls_certificate", "count": len(CERTS)}, {"category": "storage_mount", "count": len(MOUNTS)},
]}

SETUP_DONE = {"setup_complete": True, "needs_setup_code": False, "detected": {}}
SETUP_OPEN = {"setup_complete": False, "needs_setup_code": True, "suggested_address": "http://oci.tail6a3622.ts.net:8081", "detected": {
    "hostname": "oci", "os": "Ubuntu 24.04", "cpu_count": 4, "mem_gb": 24, "tailscale_ip": "100.107.140.36",
    "tailscale_dns": "oci.tail6a3622.ts.net", "public_ip": "80.225.195.84", "docker_version": "27.3.1", "port": 8081,
    "projects_folder": "/home/msinha/projects", "projects_found": 31,
    "checks": [{"id": "port", "ok": True, "label": "Port 8081 is free"}, {"id": "docker", "ok": True, "label": "Docker socket readable"},
               {"id": "host", "ok": True, "label": "Host filesystem mounted read-only"}, {"id": "mongo", "ok": True, "label": "MongoDB 8.0 started"}]}}

JOIN = {"code": "ifd7_J4Q9MZK27TXR", "expires_at": ahead(minutes=30),
        "command": "curl -fsSL http://oci.tail6a3622.ts.net:8081/join.sh | bash -s -- ifd7_J4Q9MZK27TXR",
        "voting_after": 3, "mode_after": "automatic", "mode_after_reason": "3 voting members"}
JOIN_STATUS = {"state": "waiting", "node_id": None, "reason": None, "directions": {"new_to_primary": None, "primary_to_new": None}}
