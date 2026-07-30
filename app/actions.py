"""Operational actions (Phase 8).

A small dispatcher that maps (asset_category, action) → a callable that
actually performs the action against the host (docker SDK, systemctl,
nginx, etc.). Returns a uniform `ActionResult` so the API layer can log
and return without caring which subsystem was touched.

Safety rails in this module:
  - allow-list per asset category (unknown action → ActionNotAllowed)
  - explicit refusal to act on our own deployment (see SELF_PROTECT_PREFIXES)
    so the API can't kill itself mid-request
  - logs() is capped at 1000 lines

Everything else (sudo permissions, container ownership) is enforced by
the OS. The dispatcher just runs the command and reports stdout/stderr.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import docker
from docker.errors import APIError, DockerException, NotFound


# ----------------------- allow-list per category ----------------------------

# Card & Action Registry (Phase 3.5) — single source of truth for what each
# category permits. `allowed` = every fireable action; `destructive` = subset
# that needs a confirm gate; `self_protect` = enforce infradocs-v6-* refusal.
# ALLOWED_ACTIONS is derived below so existing consumers keep working unchanged.
ACTION_REGISTRY: Dict[str, Dict[str, Any]] = {
    "docker_container": {
        # check_update is read-only (queries the registry); never destructive.
        "allowed": {"start", "stop", "restart", "logs", "inspect", "stats", "check_update"},
        "destructive": {"stop", "restart"},
        "self_protect": True,
    },
    "docker_compose": {
        # update = pull newer images + recreate (the real "upgrade this app").
        "allowed": {"up", "down", "restart", "recreate", "update"},
        "destructive": {"down", "restart", "recreate", "update"},
        "self_protect": True,
    },
    "systemd_service": {
        "allowed": {"start", "stop", "restart", "logs", "status", "enable", "disable"},
        "destructive": {"stop", "restart", "disable"},
        "self_protect": True,
    },
    "systemd_timer": {
        "allowed": {"start", "stop", "restart", "status", "enable", "disable", "trigger"},
        "destructive": {"stop", "restart", "disable", "trigger"},
        "self_protect": True,
    },
    "nginx_server_block": {
        "allowed": {"test", "reload"},
        "destructive": {"reload"},
        "self_protect": False,
    },
    "docker_image": {
        "allowed": {"pull", "prune", "check_update"},
        "destructive": {"prune"},
        "self_protect": False,
    },
    "docker_volume": {
        "allowed": {"inspect", "prune"},
        "destructive": {"prune"},
        "self_protect": False,
    },
    "storage_mount": {
        "allowed": {"inspect"},
        "destructive": set(),
        "self_protect": False,
    },
}

# Derived view — preserves the original API/dispatch consumers verbatim.
ALLOWED_ACTIONS: Dict[str, set] = {
    cat: set(spec["allowed"]) for cat, spec in ACTION_REGISTRY.items()
}

# Destructive map (used by /api/actions/allowed additively, and by the UI).
DESTRUCTIVE_ACTIONS: Dict[str, set] = {
    cat: set(spec.get("destructive", set())) for cat, spec in ACTION_REGISTRY.items()
}

# Refuse to act on our own containers/units — an action here would kill the API
# mid-request. This MUST match the real deployment naming, not a stale literal:
#   docker product : infradocs-api-1 / infradocs-web-1 / infradocs-mongo-1
#                     (compose project "infradocs")  → all start with "infradocs"
#   legacy native  : infradocs-v6-api.service        → also starts with "infradocs"
# Derived, not hard-coded: honour the compose project name and an explicit env
# override so a differently-named deployment still self-protects correctly.
_DEFAULT_SELF_PROTECT = ("infradocs",)


def _load_self_protect_prefixes():
    env = os.environ.get("INFRADOCS_SELF_PROTECT_PREFIXES", "")
    prefixes = [p.strip() for p in env.split(",") if p.strip()]
    project = os.environ.get("COMPOSE_PROJECT_NAME", "").strip()
    if project and project not in prefixes:
        prefixes.append(project)
    return tuple(prefixes) or _DEFAULT_SELF_PROTECT


SELF_PROTECT_PREFIXES = _load_self_protect_prefixes()


# --------------------- argv-safety for ingested/scanned metadata ------------
# Asset metadata (compose file_path, unit name, volume name, mountpoint, image ref)
# ends up as a subprocess argv token. Under federation these fields can originate
# from a secondary's /ingest payload — attacker-influencable — so validate before
# they ever reach execve: reject values that would be parsed as an option flag
# (leading '-'), or that carry argv/shell-breaking control characters.
_COMPOSE_NAMES = frozenset({
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
})


def _reject_optlike(value: Any, field: str) -> str:
    if not value or not isinstance(value, str):
        raise ActionError(f"{field} is missing or not a string")
    if value.startswith("-"):
        raise ActionError(f"{field} '{value}' looks like a command-line option — refused")
    if any(c in value for c in ("\x00", "\n", "\r")):
        raise ActionError(f"{field} contains control characters — refused")
    return value


def _allowed_compose_roots() -> List[str]:
    """The discovery roots a compose file is allowed to live under (realpath'd).
    Derived from config so it tracks the deployment, not a hard-coded literal."""
    from app.core.config_loader import get_config
    cfg = get_config()
    roots: List[str] = []
    if cfg.paths.projects_root:
        roots.append(cfg.paths.projects_root)
    roots += list(cfg.paths.direct_roots or [])
    roots += list(cfg.paths.scan_roots or [])
    return [os.path.realpath(r) for r in roots if r]


def _validate_compose_file(file_path: str) -> str:
    """A compose -f target must be an absolute path, carry a recognized compose
    basename, contain no traversal, and resolve under a configured discovery root."""
    _reject_optlike(file_path, "compose file_path")
    if not os.path.isabs(file_path):
        raise ActionError(f"compose file_path must be absolute: {file_path}")
    base = os.path.basename(file_path)
    if base not in _COMPOSE_NAMES:
        raise ActionError(f"refusing compose file with unexpected name: {base}")
    real = os.path.realpath(file_path)
    roots = _allowed_compose_roots()
    if roots and not any(real == r or real.startswith(r.rstrip("/") + "/") for r in roots):
        raise ActionError(
            f"compose file_path is outside the configured discovery roots: {file_path}"
        )
    return file_path


class ActionError(Exception):
    pass


class ActionNotAllowed(ActionError):
    pass


class SelfActionRefused(ActionError):
    pass


@dataclass
class ActionResult:
    status: str  # "success" | "failed"
    stdout: str = ""
    stderr: str = ""
    return_code: Optional[int] = None
    duration_ms: int = 0
    details: Dict[str, Any] = field(default_factory=dict)


# ----------------------- docker container actions ---------------------------


def _docker_client():
    try:
        return docker.from_env()
    except DockerException as e:
        raise ActionError(f"docker daemon unreachable: {e}")


def _check_image_update(client, ref: Optional[str]) -> ActionResult:
    """Compare the locally-pulled image digest against the registry's current
    digest for the same tag. Read-only: one anonymous registry lookup, no pull.

    `details.update_available` is True/False, or None when it can't be decided
    (e.g. registry unreachable, or the local image wasn't pulled by digest).
    """
    if not ref or ref == "<none>":
        raise ActionError("no image tag to check (image is untagged/dangling)")
    # Remote: canonical manifest digest for this tag, straight from the registry.
    try:
        remote = client.images.get_registry_data(ref).id
    except (APIError, DockerException, NotFound) as e:
        return ActionResult(
            status="failed",
            stderr=f"registry lookup failed for {ref}: {e}",
            details={"update_available": None, "ref": ref},
        )
    # Local: the digest the running image was pulled at (RepoDigests).
    local_digest = None
    try:
        attrs = client.images.get(ref).attrs
        for d in attrs.get("RepoDigests") or []:
            if "@" in d:
                local_digest = d.split("@", 1)[1]
                break
    except (APIError, DockerException, NotFound):
        pass

    if local_digest is None:
        update_available = None
        verdict = "local digest unknown (image not pulled by digest) — cannot compare"
    else:
        update_available = remote != local_digest
        verdict = (
            "UPDATE AVAILABLE — run `update` to pull + recreate"
            if update_available else "up to date"
        )
    summary = (
        f"{ref}\n"
        f"local:  {local_digest or 'unknown'}\n"
        f"remote: {remote}\n\n"
        f"{verdict}"
    )
    return ActionResult(
        status="success",
        stdout=summary,
        details={
            "update_available": update_available,
            "ref": ref,
            "local": local_digest,
            "remote": remote,
        },
    )


def _act_docker_container(
    asset: Dict[str, Any], action: str, args: Dict[str, Any]
) -> ActionResult:
    client = _docker_client()
    container_id = asset.get("metadata", {}).get("container_id") or asset["name"]
    try:
        container = client.containers.get(container_id)
    except NotFound:
        raise ActionError(f"container not found: {container_id}")

    if action == "start":
        container.start()
        return ActionResult(status="success", stdout=f"started {container.name}")
    if action == "stop":
        timeout = int(args.get("timeout", 10))
        container.stop(timeout=timeout)
        return ActionResult(status="success", stdout=f"stopped {container.name}")
    if action == "restart":
        timeout = int(args.get("timeout", 10))
        container.restart(timeout=timeout)
        return ActionResult(status="success", stdout=f"restarted {container.name}")
    if action == "logs":
        tail = min(int(args.get("tail", 100)), 1000)
        raw = container.logs(tail=tail).decode("utf-8", errors="replace")
        return ActionResult(
            status="success",
            stdout=raw,
            details={"tail": tail, "lines": raw.count("\n")},
        )
    if action == "inspect":
        attrs = container.attrs
        import json as _json
        return ActionResult(
            status="success",
            stdout=_json.dumps(attrs, indent=2, default=str),
            details={"state": attrs.get("State", {})},
        )
    if action == "stats":
        st = container.stats(stream=False)
        import json as _json
        return ActionResult(
            status="success",
            stdout=_json.dumps(st, indent=2, default=str),
            details={"read": st.get("read")},
        )
    if action == "check_update":
        img = getattr(container, "image", None)
        ref = (getattr(img, "tags", None) or [None])[0] if img else None
        if not ref:
            ref = asset.get("metadata", {}).get("image")
        return _check_image_update(client, ref)
    raise ActionNotAllowed(action)


# ----------------------- docker compose actions -----------------------------


def _act_docker_compose(
    asset: Dict[str, Any], action: str, args: Dict[str, Any]
) -> ActionResult:
    """Run `docker compose <action>` in the compose file's directory."""
    file_path = asset.get("metadata", {}).get("file_path")
    if not file_path:
        raise ActionError("compose file path missing")
    file_path = _validate_compose_file(file_path)

    # update = pull newer images, then recreate onto them. This is the real
    # "upgrade this app": `recreate` alone reuses the local image and never pulls.
    if action == "update":
        pull = _run_subprocess(
            ["docker", "compose", "-f", file_path, "pull"], timeout=600
        )
        if pull.status != "success":
            return pull
        up = _run_subprocess(
            ["docker", "compose", "-f", file_path, "up", "-d"], timeout=600
        )
        up.stdout = (f"$ docker compose pull\n{pull.stdout}\n"
                     f"$ docker compose up -d\n{up.stdout}").strip()
        return up

    sub = {"up": ["up", "-d"], "down": ["down"], "restart": ["restart"], "recreate": ["up", "-d", "--force-recreate"]}.get(action)
    if sub is None:
        raise ActionNotAllowed(action)

    cmd = ["docker", "compose", "-f", file_path] + sub
    return _run_subprocess(cmd, timeout=120)


# ----------------------- systemd actions ------------------------------------


def _act_systemd(
    asset: Dict[str, Any], action: str, args: Dict[str, Any]
) -> ActionResult:
    name = _reject_optlike(asset.get("name"), "unit name")
    if any(name.startswith(p) for p in SELF_PROTECT_PREFIXES):
        raise SelfActionRefused(f"refusing to act on protected unit: {name}")

    if action in ("start", "stop", "restart", "enable", "disable"):
        cmd = ["sudo", "-n", "systemctl", action, name]
        return _run_subprocess(cmd, timeout=30)
    if action == "trigger":
        svc = name[:-6] + ".service" if name.endswith(".timer") else name
        cmd = ["sudo", "-n", "systemctl", "start", svc]
        return _run_subprocess(cmd, timeout=30)
    if action == "status":
        cmd = ["systemctl", "status", "--no-pager", "-n", "0", name]
        return _run_subprocess(cmd, timeout=10, allow_nonzero=True)
    if action == "logs":
        tail = min(int(args.get("tail", 100)), 1000)
        cmd = [
            "journalctl",
            "-u",
            name,
            "--no-pager",
            "-n",
            str(tail),
        ]
        return _run_subprocess(cmd, timeout=15)
    raise ActionNotAllowed(action)


# ----------------------- nginx actions --------------------------------------


def _act_nginx(
    asset: Dict[str, Any], action: str, args: Dict[str, Any]
) -> ActionResult:
    if action == "test":
        # nginx -t is read-only; no sudo needed if msinha can read configs.
        return _run_subprocess(["sudo", "-n", "nginx", "-t"], timeout=10,
                               allow_nonzero=True)
    if action == "reload":
        return _run_subprocess(
            ["sudo", "-n", "nginx", "-s", "reload"], timeout=10
        )
    raise ActionNotAllowed(action)


# ----------------------- docker image actions -------------------------------


def _act_docker_image(
    asset: Dict[str, Any], action: str, args: Dict[str, Any]
) -> ActionResult:
    client = _docker_client()
    if action == "pull":
        ref = (asset.get("metadata", {}).get("tags") or [asset.get("name")])[0]
        ref = _reject_optlike(ref, "image ref")
        try:
            client.images.pull(ref)
        except (APIError, DockerException) as e:
            raise ActionError(f"pull failed: {e}")
        return ActionResult(status="success", stdout=f"pulled {ref}")
    if action == "prune":
        return _run_subprocess(["docker", "image", "prune", "-f"], timeout=60)
    if action == "check_update":
        ref = (asset.get("metadata", {}).get("tags") or [asset.get("name")])[0]
        return _check_image_update(client, ref)
    raise ActionNotAllowed(action)


# ----------------------- subprocess runner ----------------------------------


def _run_subprocess(
    cmd: List[str], *, timeout: int, allow_nonzero: bool = False
) -> ActionResult:
    start = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return ActionResult(
            status="failed",
            stderr=f"timeout after {timeout}s: {shlex.join(cmd)}",
            duration_ms=int((time.time() - start) * 1000),
        )
    except FileNotFoundError as e:
        return ActionResult(
            status="failed",
            stderr=f"command not found: {e}",
            duration_ms=int((time.time() - start) * 1000),
        )
    duration = int((time.time() - start) * 1000)
    ok = allow_nonzero or proc.returncode == 0
    return ActionResult(
        status="success" if ok else "failed",
        stdout=proc.stdout,
        stderr=proc.stderr,
        return_code=proc.returncode,
        duration_ms=duration,
    )


# ----------------------- top-level dispatch ---------------------------------


def _act_docker_volume(asset, action, args):
    name = _reject_optlike(asset.get("name"), "volume name")
    if action == "inspect":
        return _run_subprocess(["docker", "volume", "inspect", name], timeout=15, allow_nonzero=True)
    if action == "prune":
        return _run_subprocess(["docker", "volume", "prune", "-f"], timeout=60)
    raise ActionNotAllowed(action)


def _act_storage_mount(asset, action, args):
    if action == "inspect":
        mp = _reject_optlike(asset.get("metadata", {}).get("mountpoint") or asset.get("name"), "mountpoint")
        return _run_subprocess(["findmnt", "-T", mp, "--output", "TARGET,SOURCE,FSTYPE,SIZE,USED,AVAIL,USE%"], timeout=10, allow_nonzero=True)
    raise ActionNotAllowed(action)

_DISPATCH = {
    "docker_volume": _act_docker_volume,
    "storage_mount": _act_storage_mount,
    "docker_container": _act_docker_container,
    "docker_compose": _act_docker_compose,
    "systemd_service": _act_systemd,
    "systemd_timer": _act_systemd,
    "nginx_server_block": _act_nginx,
    "docker_image": _act_docker_image,
}


def dispatch(
    asset: Dict[str, Any], action: str, args: Optional[Dict[str, Any]] = None
) -> ActionResult:
    """Run `action` against `asset`. Raises ActionNotAllowed / ActionError."""
    category = asset.get("category")
    handler = _DISPATCH.get(category)
    if not handler:
        raise ActionNotAllowed(
            f"category '{category}' has no operational actions"
        )
    # Centralized self-protect: refuse infradocs-v6-* for every category,
    # not just systemd, so the API can never act on itself mid-request.
    if ACTION_REGISTRY.get(category, {}).get("self_protect"):
        _nm = asset.get("name", "")
        if any(_nm.startswith(_p) for _p in SELF_PROTECT_PREFIXES):
            raise SelfActionRefused(
                f"refusing to act on protected asset: {_nm}"
            )
    if action not in ALLOWED_ACTIONS.get(category, set()):
        raise ActionNotAllowed(
            f"action '{action}' not allowed for category '{category}' "
            f"(allowed: {sorted(ALLOWED_ACTIONS.get(category, []))})"
        )

    start = time.time()
    try:
        result = handler(asset, action, args or {})
    except ActionNotAllowed:
        raise
    except SelfActionRefused:
        raise
    except ActionError as e:
        return ActionResult(
            status="failed",
            stderr=str(e),
            duration_ms=int((time.time() - start) * 1000),
        )
    except (APIError, DockerException) as e:
        return ActionResult(
            status="failed",
            stderr=f"docker: {e}",
            duration_ms=int((time.time() - start) * 1000),
        )
    if result.duration_ms == 0:
        result.duration_ms = int((time.time() - start) * 1000)
    return result
