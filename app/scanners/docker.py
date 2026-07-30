"""Docker scanner — containers, images, volumes, networks."""

import os
import re
from typing import Any, Dict, List

import docker
from docker.errors import DockerException

from app.scanners.base import BaseScanner


_SECRET_KEY_RE = re.compile(
    r"(PASSWORD|PASSWD|SECRET|TOKEN|KEY|CREDENTIAL|API[_-]?KEY|PRIVATE|AUTH)",
    re.IGNORECASE,
)


def _split_env_keys(env_list):
    """Take ['FOO=bar', 'PASSWORD=secret'] → list of keys; values dropped entirely.

    We don't even keep redacted values. Just key names so the correlator can show
    which config knobs an app reads.
    """
    keys = []
    for item in env_list or []:
        if "=" in item:
            keys.append(item.split("=", 1)[0])
        else:
            keys.append(item)
    return keys


# Pseudo-filesystems whose files report phantom/enormous apparent sizes
# (e.g. /proc/kcore is ~128 TB). Walking into them when sizing a tree rooted
# at / would explode the total into bogus petabytes. Never descend into them.
_PSEUDO_FS_PATHS = frozenset({"/proc", "/sys", "/dev", "/run"})


def _dir_size_bytes(path: str) -> int:
    """Sum file sizes under `path`. Prunes pseudo-filesystems; skips on oserror."""
    if not path or not os.path.isdir(path):
        return 0
    # Defense: never size a pseudo-fs even if asked to walk it directly
    # (e.g. a container binding /proc — /proc/kcore alone is ~128 TB apparent).
    norm = os.path.normpath(path)
    if norm in _PSEUDO_FS_PATHS or any(norm.startswith(p + os.sep) for p in _PSEUDO_FS_PATHS):
        return 0
    total = 0
    try:
        for root, dirs, files in os.walk(path, followlinks=False):
            # Prune /proc, /sys, /dev, /run — their phantom files (kcore etc.)
            # would otherwise blow the sum up to bogus petabyte totals.
            dirs[:] = [d for d in dirs if os.path.join(root, d) not in _PSEUDO_FS_PATHS]
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    continue
    except OSError:
        pass
    return total


class DockerScanner(BaseScanner):
    @property
    def scanner_name(self) -> str:
        return "docker"

    def __init__(self, server_id, project_detector):
        super().__init__(server_id, project_detector)
        try:
            self.client = docker.from_env()
        except DockerException as e:
            self.add_error(f"docker daemon unreachable: {e}")
            self.client = None

    def scan(self) -> List[Dict[str, Any]]:
        if not self.client:
            # Hard dependency down — the daemon is unreachable, so this is a FAILURE,
            # not an empty success. Raising lets base.execute() mark status="failed":
            # the scan job then reports the run as failed instead of quietly wiping the
            # entire docker inventory (correlator would rebuild from zero docker assets)
            # and calling it success.
            raise DockerException("docker daemon unreachable — scan aborted")
        return (
            self._scan_containers()
            + self._scan_images()
            + self._scan_volumes()
            + self._scan_networks()
        )

    def _scan_containers(self) -> List[Dict[str, Any]]:
        assets: List[Dict[str, Any]] = []
        try:
            containers = self.client.containers.list(all=True)
        except DockerException as e:
            self.add_error(f"list containers: {e}")
            return assets

        for c in containers:
            try:
                labels = c.labels or {}
                working_dir = c.attrs.get("Config", {}).get("WorkingDir", "")

                # Pre-extract bind-mount sources so project resolution can
                # use them as a fallback when the container has no compose
                # label and the working_dir isn't a host path.
                bind_sources_for_project = [
                    m.get("Source")
                    for m in c.attrs.get("Mounts", [])
                    if m.get("Type") == "bind" and m.get("Source")
                ]
                project = self.project_detector.get_project_from_container(
                    labels,
                    working_dir,
                    bind_mounts=bind_sources_for_project,
                    container_name=c.name,
                )

                ports = []
                host_ports = []
                for cport, bindings in (c.ports or {}).items():
                    for b in bindings or []:
                        hp = b.get("HostPort")
                        ports.append(
                            {
                                "container_port": cport,
                                "host_port": hp,
                                "host_ip": b.get("HostIp", "0.0.0.0"),
                            }
                        )
                        if hp:
                            try:
                                host_ports.append(int(hp))
                            except ValueError:
                                pass

                mounts = []
                bind_mount_sources = []
                volume_names = []
                for m in c.attrs.get("Mounts", []):
                    mt = {
                        "type": m.get("Type"),
                        "source": m.get("Source"),
                        "destination": m.get("Destination"),
                        "mode": m.get("Mode"),
                    }
                    if m.get("Name"):
                        mt["name"] = m["Name"]
                        volume_names.append(m["Name"])
                    mounts.append(mt)
                    if m.get("Type") == "bind" and m.get("Source"):
                        bind_mount_sources.append(m["Source"])

                cfg = c.attrs.get("Config", {}) or {}
                host_cfg = c.attrs.get("HostConfig", {}) or {}
                healthcheck = cfg.get("Healthcheck") or {}
                env_keys = _split_env_keys(cfg.get("Env"))
                restart_policy = (host_cfg.get("RestartPolicy") or {}).get("Name")

                health = {
                    "running": c.status == "running",
                    "restarts": c.attrs.get("RestartCount", 0),
                    "has_health_check": "Healthcheck" in c.attrs.get("Config", {}),
                }
                health_state = c.attrs.get("State", {}).get("Health")
                if health_state:
                    health["health_status"] = health_state.get("Status")

                image_name = (
                    c.image.tags[0] if c.image.tags else c.image.id[:19]
                )

                assets.append(
                    self.create_asset(
                        category="docker_container",
                        asset_id=f"{self.server_id}:container:{c.id[:12]}",
                        name=c.name,
                        status=c.status,
                        project=project,
                        metadata={
                            "container_id": c.id[:12],
                            "image": image_name,
                            "labels": labels,
                            "ports": ports,
                            "host_ports": sorted(set(host_ports)),
                            "mounts": mounts,
                            "bind_mount_sources": bind_mount_sources,
                            "volume_names": volume_names,
                            "network_mode": host_cfg.get("NetworkMode"),
                            "networks": list(
                                (c.attrs.get("NetworkSettings", {}) or {})
                                .get("Networks", {})
                                .keys()
                            ),
                            "compose_project": labels.get("com.docker.compose.project"),
                            "compose_service": labels.get("com.docker.compose.service"),
                            "compose_working_dir": labels.get(
                                "com.docker.compose.project.working_dir"
                            ),
                            "compose_config_files": labels.get(
                                "com.docker.compose.project.config_files"
                            ),
                            "cmd": cfg.get("Cmd"),
                            "entrypoint": cfg.get("Entrypoint"),
                            "working_dir": cfg.get("WorkingDir"),
                            "env_keys": env_keys,
                            "restart_policy": restart_policy,
                            "healthcheck_defined": bool(healthcheck),
                            "healthcheck_test": healthcheck.get("Test"),
                            "created": c.attrs.get("Created"),
                            "started_at": c.attrs.get("State", {}).get("StartedAt"),
                        },
                        health_indicators=health,
                    )
                )
            except Exception as e:
                self.add_error(f"container {getattr(c, 'name', '?')}: {e}")
        return assets

    def _scan_images(self) -> List[Dict[str, Any]]:
        assets: List[Dict[str, Any]] = []
        try:
            images = self.client.images.list()
            in_use_ids = set()
            for c in self.client.containers.list(all=True):
                in_use_ids.add(c.image.id)
        except DockerException as e:
            self.add_error(f"list images: {e}")
            return assets

        for img in images:
            try:
                tags = img.tags or ["<none>"]
                in_use = img.id in in_use_ids
                assets.append(
                    self.create_asset(
                        category="docker_image",
                        asset_id=f"{self.server_id}:image:{img.id[:12]}",
                        name=tags[0],
                        status="in_use" if in_use else "unused",
                        project="System",
                        metadata={
                            "image_id": img.id[:19],
                            "tags": tags,
                            # Registry digests the image was pulled at — the local
                            # reference for "is there a newer image?" comparisons.
                            "repo_digests": img.attrs.get("RepoDigests", []),
                            "size_mb": round(img.attrs.get("Size", 0) / 1024 / 1024, 2),
                            "created": img.attrs.get("Created"),
                            "architecture": img.attrs.get("Architecture"),
                            "os": img.attrs.get("Os"),
                        },
                        health_indicators={
                            "in_use": in_use,
                            "is_dangling": tags == ["<none>"],
                        },
                    )
                )
            except Exception as e:
                self.add_error(f"image {img.id[:12]}: {e}")
        return assets

    def _scan_volumes(self) -> List[Dict[str, Any]]:
        assets: List[Dict[str, Any]] = []
        try:
            volumes = self.client.volumes.list()
            in_use_names = set()
            for c in self.client.containers.list(all=True):
                for m in c.attrs.get("Mounts", []):
                    if m.get("Name"):
                        in_use_names.add(m["Name"])
        except DockerException as e:
            self.add_error(f"list volumes: {e}")
            return assets

        # Index volume → containers using it, for project inheritance.
        volume_users = {}
        for c in self.client.containers.list(all=True):
            for m in c.attrs.get("Mounts", []):
                if m.get("Name"):
                    volume_users.setdefault(m["Name"], []).append(c)

        for v in volumes:
            try:
                labels = v.attrs.get("Labels") or {}
                project = self.project_detector.get_project_from_container(labels, "")
                # Inherit project from any container using this volume.
                if project == "System":
                    for c in volume_users.get(v.name, []):
                        c_labels = c.labels or {}
                        c_binds = [
                            m.get("Source")
                            for m in c.attrs.get("Mounts", [])
                            if m.get("Type") == "bind" and m.get("Source")
                        ]
                        inherited = self.project_detector.get_project_from_container(
                            c_labels,
                            c.attrs.get("Config", {}).get("WorkingDir", ""),
                            bind_mounts=c_binds,
                            container_name=c.name,
                        )
                        if inherited != "System":
                            project = inherited
                            break
                in_use = v.name in in_use_names
                mountpoint = v.attrs.get("Mountpoint") or ""
                size_bytes = _dir_size_bytes(mountpoint)
                assets.append(
                    self.create_asset(
                        category="docker_volume",
                        asset_id=f"{self.server_id}:volume:{v.name}",
                        name=v.name,
                        status="in_use" if in_use else "unused",
                        project=project,
                        metadata={
                            "driver": v.attrs.get("Driver"),
                            "mountpoint": mountpoint,
                            "size_bytes": size_bytes,
                            "labels": labels,
                            "compose_project": labels.get("com.docker.compose.project"),
                            "created": v.attrs.get("CreatedAt"),
                        },
                        health_indicators={"in_use": in_use},
                    )
                )
            except Exception as e:
                self.add_error(f"volume {v.name}: {e}")
        return assets

    def _scan_networks(self) -> List[Dict[str, Any]]:
        assets: List[Dict[str, Any]] = []
        try:
            networks = self.client.networks.list()
        except DockerException as e:
            self.add_error(f"list networks: {e}")
            return assets

        for n in networks:
            try:
                if n.name in {"bridge", "host", "none"} and not n.attrs.get(
                    "Containers"
                ):
                    continue
                labels = n.attrs.get("Labels") or {}
                project = self.project_detector.get_project_from_container(labels, "")
                connected = list((n.attrs.get("Containers") or {}).keys())
                ipam_cfg = (n.attrs.get("IPAM") or {}).get("Config") or [{}]
                first_cfg = ipam_cfg[0] if ipam_cfg else {}
                assets.append(
                    self.create_asset(
                        category="docker_network",
                        asset_id=f"{self.server_id}:network:{n.id[:12]}",
                        name=n.name,
                        status="active" if connected else "inactive",
                        project=project,
                        metadata={
                            "driver": n.attrs.get("Driver"),
                            "scope": n.attrs.get("Scope"),
                            "subnet": first_cfg.get("Subnet"),
                            "gateway": first_cfg.get("Gateway"),
                            "connected_containers": len(connected),
                            "labels": labels,
                            "created": n.attrs.get("Created"),
                        },
                        health_indicators={"has_containers": bool(connected)},
                    )
                )
            except Exception as e:
                self.add_error(f"network {n.name}: {e}")
        return assets
