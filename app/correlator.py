"""Application correlator.

Takes the flat `assets` collection (which the scanners produce) and joins
related assets into `application` documents.

**Ownership invariant (Phase 7):** every application is either
  - a project bucket — one per subdirectory of `projects_root`, OR
  - the single `System` bucket — everything that can't be tied to a project.

Every asset emitted by the scanners is routed to exactly one of those
buckets; nothing is silently dropped. The `System` app is therefore the
catch-all that surfaces docker images, host-level mounts, host-level
listening ports, and any container/service that doesn't live in a project
directory.

**V7 additions (Phase 1, additive):**
  - `links[]` — every pass records *why* it joined an asset to an app:
    {src_kind, src, dst_kind, dst, via, pass}. Evidence, not inference.
  - `containers_detail[]` / `nginx_detail[]` — denormalized component
    state so the UI renders topology from a single application fetch.
  - `hygiene{}` — dangling/unused images, orphaned volumes,
    exited-with-restart=always containers.
  - `resilience{}` — would this app survive a reboot?
All existing fields and their shapes are unchanged.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from app.core.hostpath import to_read_path
from app.scanners.docker import _dir_size_bytes
from app.storage_registry import _is_unsizable_bind

logger = logging.getLogger(__name__)


def _asset_label(x) -> str:
    """Short, exception-safe label for an asset/app used in resilience logs."""
    try:
        if isinstance(x, dict):
            cat = x.get("category")
            name = x.get("name", "?")
            return f"{cat}/{name}" if cat else str(name)
        return repr(x)[:60]
    except Exception:
        return "?"


SYSTEM_BUCKET = "System"

# Categories the passes below explicitly place into app buckets. Anything else must
# still be surfaced (in its project bucket or System) — never silently dropped — so
# the module's "nothing is dropped" invariant actually holds. See the catch-all pass.
_HANDLED_CATEGORIES = frozenset({
    "docker_compose", "docker_container", "docker_image", "docker_volume",
    "docker_network", "nginx_server_block", "tls_certificate", "caddy_site",
    "cloudflare_tunnel", "systemd_service", "systemd_timer", "network_port",
    "storage_mount",
})


def _empty_app(name: str, *, source: Optional[str], type_: str) -> Dict[str, Any]:
    """Empty application document. `type_` is "project" or "system"."""
    return {
        "name": name,
        "type": type_,
        "source": source,
        "containers": [],
        "images": [],
        "compose_file": None,
        "compose_files": [],
        "systemd_units": [],
        "nginx_sites": [],
        "urls": [],
        "port_mappings": [],
        "listening_ports": [],
        "volumes": [],
        "networks": [],
        "storage_paths": [],
        "storage_mounts": [],
        "project_dir": None,
        "project_dir_size_bytes": 0,
        "total_size_bytes": 0,
        "internet_exposed": False,
        "exposure": [],  # [{via, hostname, upstream_port}] across nginx/caddy/tunnel
        "cloudflare": False,
        "env_keys": [],
        "components_count": 0,
        "other_assets": [],  # assets of a category no pass handles — surfaced, not dropped
        "links": [],
        "containers_detail": [],
        "nginx_detail": [],
        "certificates": [],
        "certificates_detail": [],
        "hygiene": {
            "exited_restart_always": [],
            "dangling_images": [],
            "unused_images": [],
            "orphaned_volumes": [],
        },
        "resilience": {
            "reboot_safe": True,
            "issues": [],
        },
    }


def _group(assets: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for a in assets:
        try:
            out[a["category"]].append(a)
        except Exception as _e:
            logger.error("correlate: skipping asset %s — %s: %s", _asset_label(a), type(_e).__name__, _e, exc_info=True)
            continue
    return out


def _project_dirs(projects_root: str, direct_roots: Optional[List[str]] = None) -> Dict[str, str]:
    """name -> full path for every direct child of projects_root and each direct
    root (/opt/<app>, /srv/<app>, …). Fallback ONLY: production passes the
    ProjectDetector's already-discovered, host-mount-aware `project_dirs` into
    correlate() instead — this bare walk is not /host-aware, so inside the
    container it would see the container's own empty /opt, /srv, … and miss every
    app installed elsewhere on the host. Kept for native use + tests that don't
    supply project_dirs."""
    out: Dict[str, str] = {}
    for root_str in [projects_root, *(direct_roots or [])]:
        root = Path(root_str)
        try:
            if not root.exists():
                continue
            for p in root.iterdir():
                if p.is_dir() and not p.name.startswith("."):
                    out.setdefault(p.name, str(p))  # first root wins on name clash
        except OSError:
            continue
    return out


def _domain_matches(cert_domain: str, server_name: str) -> bool:
    """RFC-6125-style match: 'foo.example.com' == 'foo.example.com', and a
    wildcard '*.example.com' matches exactly one extra label (foo.example.com,
    not example.com and not a.b.example.com)."""
    cert_domain = (cert_domain or "").lower().strip()
    server_name = (server_name or "").lower().strip()
    if not cert_domain or not server_name:
        return False
    if cert_domain == server_name:
        return True
    if cert_domain.startswith("*."):
        base = cert_domain[2:]
        if server_name.endswith("." + base):
            label = server_name[: -(len(base) + 1)]
            return bool(label) and "." not in label
    return False


def _expose(app: Dict[str, Any], via: str, hostname: str, upstream_port) -> None:
    """Record an exposure mechanism (nginx/caddy/cloudflare_tunnel) on the app."""
    if not hostname:
        return
    app["internet_exposed"] = True
    url = f"https://{hostname}"
    if url not in app["urls"]:
        app["urls"].append(url)
    if not any(e["hostname"] == hostname and e["via"] == via for e in app["exposure"]):
        app["exposure"].append({"via": via, "hostname": hostname, "upstream_port": upstream_port})


def _link(
    app: Dict[str, Any],
    src_kind: str,
    src: str,
    dst_kind: str,
    dst: str,
    via: str,
    pass_no: int,
) -> None:
    """Record linking evidence on the app document.

    Convention: links to the System bucket are NOT recorded — System is the
    absence of evidence, and an app-level `links == []` with assets present
    is itself the signal the UI surfaces ("no linking evidence found").
    Container→port links are the exception kind whose dst is not an app.
    """
    if dst_kind == "application" and dst == SYSTEM_BUCKET:
        return
    app["links"].append(
        {
            "src_kind": src_kind,
            "src": src,
            "dst_kind": dst_kind,
            "dst": dst,
            "via": via,
            "pass": pass_no,
        }
    )


def _restart_policy_name(rp: Any) -> Optional[str]:
    """Scanner may emit the policy as a string or a docker {"Name": ...} dict."""
    if isinstance(rp, dict):
        return rp.get("Name") or None
    if isinstance(rp, str):
        return rp or None
    return None


def correlate(
    assets: List[Dict[str, Any]],
    *,
    server_id: str,
    projects_root: str,
    direct_roots: Optional[List[str]] = None,
    project_dirs: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Run correlation and return one app document per project + one System app.

    `project_dirs` (name -> real host path) is the set of applications the
    ProjectDetector discovered across ALL configured roots (host-mount-aware).
    When supplied (production does), a bucket is materialized for every one of
    them so an app installed ANYWHERE on disk shows up even with no running
    evidence yet. When omitted (native/tests), it falls back to a bare walk of
    projects_root + direct_roots."""
    by_cat = _group(assets)
    apps: Dict[str, Dict[str, Any]] = {}

    if project_dirs is None:
        project_dirs = _project_dirs(projects_root, direct_roots)
    for pname, ppath in project_dirs.items():
        apps[pname] = _empty_app(
            pname,
            source=ppath,
            type_="project",
        )
    for a in assets:
        try:
            proj = a.get("project")
            if proj and proj != SYSTEM_BUCKET and proj not in apps:
                apps[proj] = _empty_app(proj, source=None, type_="project")
        except Exception as _e:
            logger.error("correlate: skipping asset %s — %s: %s", _asset_label(a), type(_e).__name__, _e, exc_info=True)
            continue
    apps[SYSTEM_BUCKET] = _empty_app(SYSTEM_BUCKET, source=None, type_="system")

    def _route(project_tag: str, compose_proj: Optional[str] = None) -> str:
        """Decide app name from (project_tag, compose_project). Falls back to System."""
        if compose_proj and compose_proj in apps and compose_proj != SYSTEM_BUCKET:
            return compose_proj
        if project_tag and project_tag in apps and project_tag != SYSTEM_BUCKET:
            return project_tag
        return SYSTEM_BUCKET

    # ---- Pass 1: compose files --------------------------------------------
    for compose in by_cat.get("docker_compose", []):
        try:
            path = compose["metadata"]["file_path"]
            compose_dir = Path(path).parent.name
            app_name = _route(compose["project"], compose_proj=compose_dir)
            app = apps[app_name]
            if app["compose_file"] is None:
                app["compose_file"] = path
            if path not in app["compose_files"]:
                app["compose_files"].append(path)
            _link(
                app, "docker_compose", path, "application", app_name,
                "compose_dir" if app_name == compose_dir else "project_tag", 1,
            )

        except Exception as _e:
            logger.error("correlate: skipping asset %s — %s: %s", _asset_label(compose), type(_e).__name__, _e, exc_info=True)
            continue
    # ---- Pass 2: containers + host-port index -----------------------------
    host_port_to_app: Dict[int, str] = {}
    container_to_app: Dict[str, str] = {}

    for c in by_cat.get("docker_container", []):
        try:
            meta = c["metadata"]
            running = bool(
                meta.get("running")
                or (c.get("health_indicators") or {}).get("running")
                or c.get("status") == "running"
            )
            compose_proj = meta.get("compose_project")
            app_name = _route(c["project"], compose_proj=compose_proj)
            container_to_app[c["name"]] = app_name
            app = apps[app_name]
            app["containers"].append(c["name"])

            if compose_proj and app_name == compose_proj:
                via = "compose_label"
            else:
                via = "project_dir"
            _link(app, "docker_container", c["name"], "application", app_name, via, 2)

            rp_name = _restart_policy_name(meta.get("restart_policy"))
            app["containers_detail"].append(
                {
                    "name": c["name"],
                    "image": meta.get("image"),
                    "running": running,
                    "restarts": meta.get("restarts", 0),
                    "restart_policy": rp_name,
                    "has_health_check": bool(
                        meta.get("has_health_check") or meta.get("healthcheck_defined")
                    ),
                    "started_at": meta.get("started_at"),
                    "host_ports": meta.get("host_ports") or [],
                    "compose_service": meta.get("compose_service"),
                }
            )

            if not running and rp_name in ("always", "unless-stopped"):
                app["hygiene"]["exited_restart_always"].append(c["name"])

            seen_mappings = {
                (pm["host_port"], pm["container"], pm["container_port"])
                for pm in app["port_mappings"]
            }
            for pm in meta.get("ports") or []:
                hp = pm.get("host_port")
                try:
                    hp_int = int(hp) if hp is not None else None
                except (TypeError, ValueError):
                    hp_int = None
                if hp_int is not None:
                    host_port_to_app.setdefault(hp_int, app_name)
                    key = (hp_int, c["name"], pm.get("container_port"))
                    if key not in seen_mappings:
                        app["port_mappings"].append(
                            {
                                "host_port": hp_int,
                                "container": c["name"],
                                "container_port": pm.get("container_port"),
                            }
                        )
                        seen_mappings.add(key)
                    _link(
                        app, "docker_container", c["name"],
                        "network_port", str(hp_int), "port_mapping", 2,
                    )

            for src in meta.get("bind_mount_sources") or []:
                app["storage_paths"].append(src)
            for k in meta.get("env_keys") or []:
                if k not in app["env_keys"]:
                    app["env_keys"].append(k)

        except Exception as _e:
            logger.error("correlate: skipping asset %s — %s: %s", _asset_label(c), type(_e).__name__, _e, exc_info=True)
            continue
    # Extend host_port index from project-tagged listening ports so nginx
    # can link to non-Docker apps (e.g. an OCI_Dashboard service on :8080).
    for p in by_cat.get("network_port", []):
        try:
            if p["project"] != SYSTEM_BUCKET and p["project"] in apps:
                port = p["metadata"].get("port")
                if isinstance(port, int):
                    host_port_to_app.setdefault(port, p["project"])

        except Exception as _e:
            logger.error("correlate: skipping asset %s — %s: %s", _asset_label(p), type(_e).__name__, _e, exc_info=True)
            continue
    # ---- Pass 3: images attach to the apps whose containers use them ------
    image_to_apps: Dict[str, Set[str]] = defaultdict(set)
    for c in by_cat.get("docker_container", []):
        try:
            img = c["metadata"].get("image")
            if img:
                image_to_apps[img].add(container_to_app[c["name"]])

        except Exception as _e:
            logger.error("correlate: skipping asset %s — %s: %s", _asset_label(c), type(_e).__name__, _e, exc_info=True)
            continue
    for img in by_cat.get("docker_image", []):
        try:
            candidates: Set[str] = set()
            for tag in (img["metadata"].get("tags") or []) + [img["name"]]:
                candidates |= image_to_apps.get(tag, set())
            target_apps = candidates or {SYSTEM_BUCKET}
            for app_name in target_apps:
                if img["name"] not in apps[app_name]["images"]:
                    apps[app_name]["images"].append(img["name"])
                if candidates:
                    _link(
                        apps[app_name], "docker_image", img["name"],
                        "application", app_name, "image_tag", 3,
                    )

            meta = img["metadata"]
            owner = sorted(target_apps)[0]
            if meta.get("is_dangling"):
                apps[owner]["hygiene"]["dangling_images"].append(img["name"])
            elif meta.get("in_use") is False:
                apps[owner]["hygiene"]["unused_images"].append(img["name"])

        except Exception as _e:
            logger.error("correlate: skipping asset %s — %s: %s", _asset_label(img), type(_e).__name__, _e, exc_info=True)
            continue
    # ---- Pass 4: volumes --------------------------------------------------
    for v in by_cat.get("docker_volume", []):
        try:
            meta = v["metadata"]
            compose_proj = meta.get("compose_project")
            app_name = _route(v["project"], compose_proj=compose_proj)
            apps[app_name]["volumes"].append(
                {
                    "name": v["name"],
                    "mountpoint": meta.get("mountpoint"),
                    "size_bytes": meta.get("size_bytes", 0),
                }
            )
            _link(
                apps[app_name], "docker_volume", v["name"], "application", app_name,
                "compose_label" if compose_proj and app_name == compose_proj
                else "project_tag", 4,
            )
            if (v.get("health_indicators") or {}).get("in_use") is False:
                apps[app_name]["hygiene"]["orphaned_volumes"].append(v["name"])

        except Exception as _e:
            logger.error("correlate: skipping asset %s — %s: %s", _asset_label(v), type(_e).__name__, _e, exc_info=True)
            continue
    # ---- Pass 5: networks --------------------------------------------------
    for n in by_cat.get("docker_network", []):
        try:
            labels = n["metadata"].get("labels") or {}
            compose_proj = labels.get("com.docker.compose.project")
            app_name = _route(n["project"], compose_proj=compose_proj)
            apps[app_name]["networks"].append(n["name"])
            _link(
                apps[app_name], "docker_network", n["name"], "application", app_name,
                "compose_label" if compose_proj and app_name == compose_proj
                else "project_tag", 5,
            )

        except Exception as _e:
            logger.error("correlate: skipping asset %s — %s: %s", _asset_label(n), type(_e).__name__, _e, exc_info=True)
            continue
    # ---- Pass 6: nginx blocks ---------------------------------------------
    domain_to_app: Dict[str, str] = {}  # server_name -> owning app (cert fallback)
    cert_path_to_apps: Dict[str, Set[str]] = defaultdict(set)  # ssl_certificate file -> apps
    for ng in by_cat.get("nginx_server_block", []):
        try:
            meta = ng["metadata"]
            upstream_port = meta.get("upstream_port")
            if upstream_port and upstream_port in host_port_to_app:
                app_name = host_port_to_app[upstream_port]
                via = f"upstream_port:{upstream_port}"
            else:
                app_name = _route(ng["project"])
                via = "project_tag"
            app = apps[app_name]
            domain_to_app[ng["name"]] = app_name
            cert_file = meta.get("ssl_certificate")
            if cert_file:
                cert_path_to_apps[cert_file].add(app_name)
            app["nginx_sites"].append(ng["name"])
            _link(app, "nginx_server_block", ng["name"], "application", app_name, via, 6)
            app["nginx_detail"].append(
                {
                    "server_name": ng["name"],
                    "config_file": meta.get("config_file"),
                    "ssl_certificate": cert_file,
                    "listen_ports": meta.get("listen_ports") or [],
                    "upstream_host": meta.get("upstream_host"),
                    "upstream_port": upstream_port,
                    "has_ssl": bool(meta.get("has_ssl")),
                    "ssl_issuer": meta.get("ssl_issuer"),
                    "ssl_not_after": meta.get("ssl_not_after"),
                    "cloudflare_origin": bool(meta.get("cloudflare_origin")),
                    "internet_exposed": bool(meta.get("internet_exposed")),
                    "url": meta.get("url"),
                }
            )
            url = meta.get("url")
            if url and url not in app["urls"]:
                app["urls"].append(url)
            if meta.get("internet_exposed"):
                _expose(app, "nginx", ng["name"], upstream_port)
            if meta.get("cloudflare_origin"):
                app["cloudflare"] = True

        except Exception as _e:
            logger.error("correlate: skipping asset %s — %s: %s", _asset_label(ng), type(_e).__name__, _e, exc_info=True)
            continue
    # ---- Pass 6b: TLS certificates → the app(s) that use them --------------
    # Primary: the exact cert FILE an nginx block points at (ssl_certificate) —
    # this is the real dependency, and a file used by >1 app => shared. Fallback:
    # domain/SAN coverage (incl. wildcard) when no file match; then attribution.
    for cert in by_cat.get("tls_certificate", []):
        try:
            cmeta = cert["metadata"]
            domains = cmeta.get("domains") or [cert["name"]]
            linked = set(cert_path_to_apps.get(cmeta.get("cert_path"), set()))
            if not linked:
                for cd in domains:
                    for server_name, app_name in domain_to_app.items():
                        if _domain_matches(cd, server_name):
                            linked.add(app_name)
            if not linked:
                # No nginx match at all — fall back to the cert's own attribution.
                fallback = _route(cert.get("project") or SYSTEM_BUCKET)
                if fallback != SYSTEM_BUCKET:
                    linked = {fallback}
            for app_name in linked:
                app = apps[app_name]
                if cert["name"] in app["certificates"]:
                    continue
                app["certificates"].append(cert["name"])
                app["certificates_detail"].append({
                    "name": cert["name"],
                    "domains": domains,
                    "not_after": cmeta.get("not_after"),
                    "days_until_expiry": cmeta.get("days_until_expiry"),
                    "issuer": cmeta.get("issuer"),
                    "status": cert.get("status"),
                    "cert_path": cmeta.get("cert_path"),
                })
                _link(app, "tls_certificate", cert["name"], "application", app_name,
                      "nginx_domain", 6)

        except Exception as _e:
            logger.error("correlate: skipping asset %s — %s: %s", _asset_label(cert), type(_e).__name__, _e, exc_info=True)
            continue
    # ---- Pass 6c: extra exposure mechanisms (Caddy, Cloudflare Tunnel) -----
    # A service can be public via Caddy or a Cloudflare tunnel (no open port) —
    # attribute by the upstream port it fronts (else by the cert/domain project),
    # and record the mechanism + public hostname so exposure isn't nginx-only.
    for ex in by_cat.get("caddy_site", []) + by_cat.get("cloudflare_tunnel", []):
        try:
            m = ex["metadata"]
            via = m.get("exposure_via") or ex["category"]
            hostname = m.get("server_name") or m.get("hostname") or ex["name"]
            up = m.get("upstream_port")
            app_name = host_port_to_app.get(up) if up else None
            if not app_name:
                app_name = _route(ex.get("project") or SYSTEM_BUCKET)
            app = apps[app_name]
            _expose(app, via, hostname, up)
            _link(app, ex["category"], hostname, "application", app_name, via, 6)

        except Exception as _e:
            logger.error("correlate: skipping asset %s — %s: %s", _asset_label(ex), type(_e).__name__, _e, exc_info=True)
            continue
    # ---- Pass 7: systemd services + timers --------------------------------
    for s in by_cat.get("systemd_service", []) + by_cat.get("systemd_timer", []):
        try:
            app_name = _route(s["project"])
            apps[app_name]["systemd_units"].append(s["name"])
            _link(
                apps[app_name], s["category"], s["name"],
                "application", app_name, "unit_path", 7,
            )
            unit_state = s["metadata"].get("unit_file_state")
            if (
                app_name != SYSTEM_BUCKET
                and unit_state
                and unit_state not in ("enabled", "enabled-runtime", "static")
            ):
                apps[app_name]["resilience"]["issues"].append(
                    f"unit {s['name']} is {unit_state} (won't start on boot)"
                )
            for k in s["metadata"].get("environment_keys") or []:
                if k not in apps[app_name]["env_keys"]:
                    apps[app_name]["env_keys"].append(k)

        except Exception as _e:
            logger.error("correlate: skipping asset %s — %s: %s", _asset_label(s), type(_e).__name__, _e, exc_info=True)
            continue
    # ---- Pass 8: listening ports ------------------------------------------
    for p in by_cat.get("network_port", []):
        try:
            port = p["metadata"].get("port")
            if not isinstance(port, int):
                continue
            if port in host_port_to_app:
                app_name = host_port_to_app[port]
                via = "host_port_index"
            else:
                app_name = _route(p["project"])
                via = "process_cwd"
            if port not in apps[app_name]["listening_ports"]:
                apps[app_name]["listening_ports"].append(port)
            _link(
                apps[app_name], "network_port", str(port),
                "application", app_name, via, 8,
            )

        except Exception as _e:
            logger.error("correlate: skipping asset %s — %s: %s", _asset_label(p), type(_e).__name__, _e, exc_info=True)
            continue
    # ---- Pass 9: storage mounts -------------------------------------------
    for m in by_cat.get("storage_mount", []):
        try:
            app_name = _route(m["project"])
            apps[app_name]["storage_mounts"].append(m["name"])
            _link(
                apps[app_name], "storage_mount", m["name"],
                "application", app_name, "project_tag", 9,
            )

        except Exception as _e:
            logger.error("correlate: skipping asset %s — %s: %s", _asset_label(m), type(_e).__name__, _e, exc_info=True)
            continue
    # ---- Pass 9b: catch-all — never silently drop an unhandled category ----
    # A new/unknown scanner category would otherwise vanish from the application
    # view (no pass places it), quietly breaking the "nothing is dropped"
    # invariant. Route each to its project bucket (else System) as evidence.
    for cat, items in by_cat.items():
        if cat in _HANDLED_CATEGORIES:
            continue
        logger.warning("correlate[%s]: %d asset(s) of unhandled category '%s' — routing to "
                       "buckets as other_assets (add a pass to classify them)", server_id, len(items), cat)
        for a in items:
            try:
                app_name = _route(a.get("project") or SYSTEM_BUCKET)
                app = apps[app_name]
                entry = {"category": cat, "name": a.get("name")}
                if entry not in app["other_assets"]:
                    app["other_assets"].append(entry)
                _link(app, cat, a.get("name") or "?", "application", app_name, "unclassified", 9)
            except Exception as _e:
                logger.error("correlate: skipping asset %s — %s: %s", _asset_label(a), type(_e).__name__, _e, exc_info=True)
                continue
    # ---- Pass 10: project_dir size for project apps -----------------------
    # Size the app's OWN discovered directory wherever it lives (/opt/x, /srv/y,
    # …), not just projects_root/<name>. `source` is the real host path the
    # detector found; read/size it through the /host mount when containerized.
    for app_name, app in apps.items():
        try:
            if app["type"] != "project":
                continue
            real_dir = app.get("source") or str(Path(projects_root) / app_name)
            read_dir = to_read_path(real_dir)
            if os.path.isdir(read_dir):
                app["project_dir"] = real_dir
                app["project_dir_size_bytes"] = _dir_size_bytes(read_dir)
                if real_dir not in app["storage_paths"]:
                    app["storage_paths"].insert(0, real_dir)

        except Exception as _e:
            logger.error("correlate: skipping app %s during finalization — %s: %s", app.get('name','?'), type(_e).__name__, _e, exc_info=True)
            continue
    # ---- Pass 11: totals + dedup + resilience ------------------------------
    for app in apps.values():
        try:
            total = app["project_dir_size_bytes"]
            counted_dir = app.get("project_dir")
            for v in app["volumes"]:
                total += v.get("size_bytes") or 0
            for src in app["storage_paths"]:
                if counted_dir and src == counted_dir:
                    continue  # the app's own dir — already counted via project_dir_size_bytes
                if src.startswith(projects_root):
                    continue  # under the classic root — counted (or a sibling project)
                if _is_unsizable_bind(src):
                    continue  # system/observability mount (/, /proc, …) — not app data
                total += _dir_size_bytes(to_read_path(src))
            app["total_size_bytes"] = total

            for k in (
                "containers",
                "images",
                "compose_files",
                "nginx_sites",
                "urls",
                "systemd_units",
                "networks",
                "storage_paths",
                "storage_mounts",
                "env_keys",
            ):
                if isinstance(app.get(k), list):
                    app[k] = sorted(set(app[k]), key=str)

            # Ports are ints — sort NUMERICALLY. The str-keyed sort above put 100
            # before 22 (lexicographic), so ports listed in a nonsensical order.
            if isinstance(app.get("listening_ports"), list):
                app["listening_ports"] = sorted({p for p in app["listening_ports"]
                                                 if isinstance(p, int)})

            seen_links: Set[tuple] = set()
            deduped: List[Dict[str, Any]] = []
            for ln in app["links"]:
                key = (ln["src_kind"], ln["src"], ln["dst_kind"], ln["dst"], ln["via"])
                if key not in seen_links:
                    seen_links.add(key)
                    deduped.append(ln)
            app["links"] = deduped

            if app["type"] == "project":
                for cd in app["containers_detail"]:
                    if cd["restart_policy"] in (None, "no"):
                        app["resilience"]["issues"].append(
                            f"container {cd['name']} has no restart policy"
                        )
            app["resilience"]["reboot_safe"] = not app["resilience"]["issues"]

            app["components_count"] = (
                len(app["containers"])
                + len(app["nginx_sites"])
                + len(app["systemd_units"])
                + len(app["volumes"])
            )

        except Exception as _e:
            logger.error("correlate: skipping app %s during finalization — %s: %s", app.get('name','?'), type(_e).__name__, _e, exc_info=True)
            continue
    _n_project = sum(1 for _a in apps.values() if _a.get("type") == "project")
    logger.info("correlate[%s]: %d applications (%d project + System) from %d assets",
                server_id, len(apps), _n_project, len(assets))
    if assets and _n_project == 0:
        logger.warning("correlate[%s]: 0 project applications from %d assets — project detection / "
                       "host paths likely failed (check the /host mount + scan roots)", server_id, len(assets))
    result = []
    for name in sorted(apps.keys()):
        app = apps[name]
        app["application_id"] = f"{server_id}:app:{name}"
        result.append(app)
    return result
