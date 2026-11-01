"""Project / application detection.

A "project" (application) is a directory that owns assets. Apps do NOT only live
under one home-relative folder — on a real Linux server they are scattered across
`/opt`, `/srv`, `/var/www`, `/usr/local`, user homes, etc. This detector therefore
scans a CONFIGURABLE set of roots with a sensible full-disk default (see
`config.yml` → `paths.direct_roots` / `paths.scan_roots`) so an app installed
anywhere on disk shows up — it can never silently regress to a single directory.

Projects are discovered from:
  - direct subfolders of each *direct* root — `projects_root` plus `direct_roots`
    (the classic "one folder per app" install layout: /opt/<app>, /srv/<app>, …),
  - a bounded, guarded filesystem scan for project markers
    (docker-compose.yml / .git) under both the direct roots AND the broader
    `scan_roots` — so nested/scattered projects are found without turning every
    subfolder of /home into a "project", and
  - Docker Compose working-dirs (passed in via `discovered`) — the exact host
    path of each compose app, wherever it lives.

Full-disk-scan hazards are guarded: pseudo-filesystems (/proc, /sys, /dev, /run)
and network/tmpfs/overlay/squashfs mounts are skipped, traversal depth is bounded
by both the configured `scan_depth` and a hard ceiling, noise dirs
(node_modules, .git, venv, snap/flatpak internals, …) are excluded,
permission-denied is tolerated per-dir, and a wall-clock deadline caps the whole
detector so a scan can never hang the pipeline. A configured root that exists but
cannot be read is logged LOUDLY with a named reason (it is not swallowed); a root
that is merely absent on this box is skipped quietly.

Every project carries its full path, so scattered layouts (/data/x,
/home/data/project/y, …) are listed with their real location. Assets whose path
falls under a project's directory get that project (longest-prefix wins);
everything else is "System". Project names are NEVER inferred from service-name
prefixes (the V5 'cloud-init -> Cloud' bug).
"""

import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

# In a container the host root is mounted here; read filesystem scans through it
# but record the REAL host path so it matches asset paths from docker/scanners.
_HOST_ROOT = os.environ.get("INFRADOCS_HOST_ROOT", "").rstrip("/")

_MARKERS = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml", ".git")
_SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", "venv", ".venv", "env", "vendor",
    "site-packages", ".cache", "dist", "build", "target", "backups",
    ".npm", ".cargo", ".rustup", ".gradle", ".m2", ".terraform",
    "snap", ".snap", "flatpak", ".flatpak", ".local", "lost+found",
}

# Real (host) absolute paths never entered: pseudo / virtual / image-store dirs.
# Matched against the REAL path so it works both native and via the /host mount.
_PSEUDO_DIRS = {
    "/proc", "/sys", "/dev", "/run", "/lost+found",
    "/var/lib/docker", "/var/lib/containerd", "/var/lib/snapd",
    "/snap", "/sys/fs/cgroup",
}

# Top-level dirs that are the OS, not an app-install area. A service/container whose
# working dir sits under one of these is NOT promoted to a project (its markerless dir
# would just be system noise). /opt, /srv, /home, /data, /mnt, /media, /root, and any
# custom top-level stay eligible — so an app under /home/data/project/<app> still counts.
_SYSTEM_TOP_DIRS = {
    "usr", "bin", "sbin", "lib", "lib32", "lib64", "libx32", "boot",
    "proc", "sys", "dev", "run", "snap", "var", "etc", "tmp", "lost+found",
}

# Filesystem types whose mountpoints we refuse to descend into: network shares,
# ephemeral/in-memory FSes, and image/overlay FSes. Keeps a full-disk walk from
# wandering onto an NFS mount or blowing up on a squashfs snap.
_SKIP_FSTYPES = {
    "tmpfs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2", "devpts",
    "mqueue", "debugfs", "tracefs", "securityfs", "pstore", "bpf", "configfs",
    "nfs", "nfs4", "cifs", "smb3", "fuse.sshfs", "fuse.rclone", "fuse.gvfsd-fuse",
    "overlay", "squashfs", "autofs", "fusectl", "binfmt_misc",
}

# Hard ceiling on traversal depth regardless of the configured scan_depth, plus a
# cap on how many directories a single detector run will visit. Belt-and-braces
# alongside the wall-clock deadline.
_MAX_DEPTH = 8
_MAX_DIRS_VISITED = 200_000
# Default wall-clock budget for the whole detector (seconds). Overridable via
# config (paths.scan_timeout_seconds) / the constructor.
DEFAULT_SCAN_TIMEOUT = 120

_log = logging.getLogger("app.core.project_detector")


def _parse_skip_mounts(lines, host_root: str) -> Set[str]:
    """REAL (host) mountpoints whose fstype is in `_SKIP_FSTYPES`. When containerized
    (host_root set, e.g. "/host"), host submounts appear under the /host bind, so strip
    that prefix to get real host paths that match what the walk compares against.

    The filesystem ROOT is NEVER included: a container's own root is an `overlay` mount,
    and since "/" is an ancestor of EVERY path, including it would mark the whole disk —
    and every candidate app dir — unscannable (this silently killed the from-/ walk and
    all systemd/service promotion inside the container)."""
    out: Set[str] = set()
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or parts[2] not in _SKIP_FSTYPES:
            continue
        mp = parts[1]
        if host_root and mp == host_root:
            continue                          # the /host bind of the container's own root
        if host_root and mp.startswith(host_root + "/"):
            mp = mp[len(host_root):]          # /host/mnt/nfs -> /mnt/nfs (real host path)
        if mp == "/" or not mp:
            continue                          # never skip the filesystem root itself
        out.add(mp)
    return out


def _skip_mountpoints() -> Set[str]:
    """Real mountpoints to refuse to descend into during a full-disk walk. Best-effort:
    any read error → empty set."""
    try:
        with open("/proc/mounts", "r") as f:
            lines = f.readlines()
    except OSError:
        return set()
    return _parse_skip_mounts(lines, _HOST_ROOT)


def _read_base(root: Path) -> Optional[Path]:
    """Where to actually read `root` from. In a container (_HOST_ROOT set) ALWAYS go
    via /host — the container's own /data, /srv, … are not the host's. Native: direct."""
    if _HOST_ROOT:
        hp = Path(_HOST_ROOT + str(root))
        return hp if hp.exists() else None
    return root if root.exists() else None


def _to_real(p: Path) -> Path:
    """Strip the /host prefix so stored paths are the real host paths."""
    s = str(p)
    if _HOST_ROOT and s.startswith(_HOST_ROOT + "/"):
        return Path(s[len(_HOST_ROOT):])
    if _HOST_ROOT and s == _HOST_ROOT:
        return Path("/")
    return p


def attach_root_paths(applications: List[dict], project_paths: Dict[str, str]) -> None:
    """Tag each project's application doc with its full root path (for the UI)."""
    for app in applications:
        path = project_paths.get(app.get("name"))
        if path:
            app["root_path"] = path


def discover_docker_projects() -> Dict[str, str]:
    """name -> host working-dir for every Compose project Docker knows about."""
    out: Dict[str, str] = {}
    try:
        import docker  # local import: keep the detector usable without docker

        client = docker.from_env()
        for c in client.containers.list(all=True):
            labels = c.labels or {}
            proj = labels.get("com.docker.compose.project")
            wd = labels.get("com.docker.compose.project.working_dir")
            if proj and wd:
                out.setdefault(proj, wd)
            for m in c.attrs.get("Mounts", []) or []:
                src = m.get("Source")
                if proj and not wd and m.get("Type") == "bind" and src:
                    out.setdefault(proj, src)
    except Exception:
        pass
    return out


def is_home_dir(path) -> bool:
    """A user's home folder itself (/home/<user>, /root) holds projects; it is never one.
    Without this, a marker in the home folder (a dotfiles .git, a stray compose file) or a
    service run from it turned the whole home folder into a project named after the user."""
    parts = Path(str(path)).parts
    return parts == ("/", "root") or (len(parts) == 3 and parts[:2] == ("/", "home"))


class ProjectDetector:
    DOMAIN_MAPPING = {
        "infra": "InfraDocs_V6",
        "chat": "openwebui",
        "openwebui": "openwebui",
        "rws": "raveuploader_rws",
        "dashboard": "OCI_Dashboard",
    }

    def __init__(
        self,
        projects_root: Optional[str] = None,
        scan_roots: Optional[List[str]] = None,
        direct_roots: Optional[List[str]] = None,
        scan_depth: int = 2,
        scan_timeout_seconds: Optional[int] = None,
        exclude_paths: Optional[List[str]] = None,
        discovered: Optional[Dict[str, str]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.projects_root = Path(projects_root or "/home/msinha/projects")
        # Extra roots whose *direct* children are each an application (install-style
        # layout: /opt/<app>, /srv/<app>, /var/www/<site>). Configurable so this
        # can't silently regress to one hardcoded directory.
        self.direct_roots: List[Path] = [Path(r) for r in (direct_roots or [])]
        # Roots to recursively HUNT for apps by marker only (docker-compose.yml /
        # .git). Defaults to the filesystem root — a deny-list walk of the whole
        # disk — so an app under ANY top-level dir (/data/<app>, …) is found.
        self.scan_roots: List[Path] = [Path(r) for r in (scan_roots or [])]
        self.scan_depth = scan_depth
        # Exclusion set (deny-list) — real host paths pruned during the walk. The
        # built-in pseudo-fs guards always apply; config exclusions layer on top.
        # `None` (native/tests) keeps just the built-ins so guard behaviour is stable.
        self.exclude_paths: Set[str] = set(_PSEUDO_DIRS) | {
            str(Path(p)) for p in (exclude_paths or [])
        }
        self._log = logger or _log

        self._projects: Dict[str, Path] = {}  # name -> resolved dir
        self._skip_mounts = _skip_mountpoints()
        self._dirs_visited = 0
        self._truncated = False
        timeout = DEFAULT_SCAN_TIMEOUT if scan_timeout_seconds is None else scan_timeout_seconds
        # A non-positive timeout means "no wall-clock cap".
        self._deadline = (time.monotonic() + timeout) if timeout and timeout > 0 else None

        # Preflight: log the resolved plan (timestamped via the scan logger) so a
        # misconfigured mount / over-broad exclusion is obvious in the scan log.
        self._log.info(
            "project_detector: discovery plan — direct_roots=%s scan_roots=%s "
            "scan_depth=%s (ceiling=%s) timeout=%ss max_dirs=%s host_root=%s "
            "exclusions(%d)=%s",
            [str(r) for r in [self.projects_root, *self.direct_roots]],
            [str(r) for r in self.scan_roots],
            scan_depth, _MAX_DEPTH, timeout, _MAX_DIRS_VISITED,
            _HOST_ROOT or "(native)",
            len(self.exclude_paths), sorted(self.exclude_paths),
        )

        # Direct roots: every direct subfolder is a project (classic) + nested markers.
        for r in [self.projects_root, *self.direct_roots]:
            self._discover_root(r, scan_depth, direct=True)
        # Scan roots: ONLY marker-bearing dirs — a walk from `/` must not turn every
        # top-level dir into a project; the exclusion set filters the noise.
        for r in self.scan_roots:
            self._discover_root(r, scan_depth, direct=False)
        for name, path in (discovered or {}).items():
            self._add(name, Path(path))

        if self._truncated:
            self._log.warning(
                "project_detector: filesystem scan hit a resource cap "
                "(deadline=%ss / max_depth=%s / max_dirs=%s, visited=%s dirs) — "
                "results may be incomplete",
                timeout, _MAX_DEPTH, _MAX_DIRS_VISITED, self._dirs_visited,
            )

    def discovery_roots(self) -> List[Path]:
        """All roots this detector walks (dedup, order-preserving) — so other
        scanners (e.g. compose) can cover the SAME full-disk footprint."""
        seen: Set[str] = set()
        out: List[Path] = []
        for r in [self.projects_root, *self.direct_roots, *self.scan_roots]:
            key = str(r)
            if key not in seen:
                seen.add(key)
                out.append(r)
        return out

    # ---- guards ----
    def _budget_exhausted(self) -> bool:
        """True once the wall-clock deadline or the visited-dir cap is hit."""
        if self._deadline is not None and time.monotonic() > self._deadline:
            self._truncated = True
            return True
        if self._dirs_visited >= _MAX_DIRS_VISITED:
            self._truncated = True
            return True
        return False

    def _is_skippable(self, real_path: Path, name: str) -> bool:
        """A dir we must not enter: noise dir, hidden, an excluded path (built-in
        pseudo-fs + configured deny-list), or a mount whose fstype is
        network/tmpfs/overlay/etc. Checked on the REAL (host) path."""
        if name in _SKIP_DIRS or name.startswith("."):
            return True
        rp = str(real_path)
        if rp in self.exclude_paths or rp in self._skip_mounts:
            return True
        return False

    # ---- discovery ----
    def _add(self, name: str, path: Path) -> None:
        if not name:
            return
        try:
            path = path.resolve()
        except OSError:
            return
        if is_home_dir(path):
            self._log.info("project_detector: %s is a home folder, not a project — skipped", path)
            return
        # Keep the first path seen for a name (don't let a later root shadow it).
        self._projects.setdefault(name, path)

    def _discover_root(self, root: Path, depth: int, direct: bool = True) -> None:
        # Distinguish "absent on this box" (normal — skip quietly) from "present but
        # unreadable" (a real problem — log LOUDLY with a named reason, don't crash).
        base = _read_base(root)   # read directly or via the container /host mount
        if base is None:
            # The filesystem root being unreadable means the /host bind is missing/
            # broken — fail LOUD with a named reason (no host apps can be found).
            if str(root) == os.sep:
                self._log.error(
                    "project_detector: filesystem root '/' is NOT readable via the "
                    "/host mount (INFRADOCS_HOST_ROOT=%r) — the container is likely "
                    "missing the '/:/host:ro' bind; NO host applications can be "
                    "discovered until this is fixed",
                    _HOST_ROOT or "(native)",
                )
            else:
                self._log.info("project_detector: scan root absent, skipping: %s", root)
            return
        real_root = _to_real(base)
        if str(real_root) in self.exclude_paths or str(real_root) in self._skip_mounts:
            self._log.info("project_detector: scan root is excluded/skipped mount, skipping: %s", root)
            return
        try:
            entries = list(base.iterdir())
        except (PermissionError, OSError) as e:
            self._log.warning(
                "project_detector: scan root UNREADABLE, skipping: %s (%s: %s)",
                root, type(e).__name__, e,
            )
            return
        if direct:
            for d in entries:
                try:
                    is_dir = d.is_dir()
                except OSError:
                    continue
                real = _to_real(d)
                if is_dir and not self._is_skippable(real, d.name):
                    self._add(d.name, real)   # classic: direct subfolder = project
        self._marker_scan(base, depth)         # bounded hunt for (nested) project markers

    def _marker_scan(self, root: Path, depth: int) -> None:
        cap = min(depth, _MAX_DEPTH)
        frontier = [(root, 0)]
        while frontier:
            if self._budget_exhausted():
                return
            d, lvl = frontier.pop()
            try:
                entries = list(d.iterdir())
            except (PermissionError, OSError):
                # Permission-denied on a nested dir is expected on a full-disk walk;
                # tolerate it and keep going (the root-level case is logged above).
                continue
            self._dirs_visited += 1
            names = {e.name for e in entries}
            if d != root and names.intersection(_MARKERS):
                self._add(d.name, _to_real(d))
                continue                  # a project marker → don't descend further
            if lvl < cap:
                for e in entries:
                    try:
                        if not e.is_dir():
                            continue
                    except OSError:
                        continue
                    if not self._is_skippable(_to_real(e), e.name):
                        frontier.append((e, lvl + 1))

    # ---- queries ----
    def list_projects(self) -> List[str]:
        return list(self._projects.keys())

    def project_paths(self) -> Dict[str, str]:
        """name -> full path, for the UI to show where each project lives."""
        return {n: str(p) for n, p in self._projects.items()}

    def get_project_from_path(self, path: str) -> str:
        if not path:
            return "System"
        try:
            p = Path(path).resolve()
        except OSError:
            return "System"
        best, best_len = "System", -1
        for name, root in self._projects.items():
            if p == root or root in p.parents:
                if len(str(root)) > best_len:
                    best, best_len = name, len(str(root))
        if best != "System":
            return best
        # Fallback: the app is deployed outside its discovered dir but a path
        # component exactly matches a known project name — e.g. an nginx
        # `root /home/x/mxh/dist` while the mxh project lives in /data/mxh.
        parts = set(p.parts)
        for name in self._projects:
            if name in parts:
                return name
        return "System"

    def register_project_from_dir(self, path_str: str) -> str:
        """Promote a directory a service/container is KNOWN to run from (a systemd
        WorkingDirectory, a docker working_dir, …) to a project — so an app the system
        actively tracks surfaces even without a .git/compose marker and even when it
        lives outside the direct roots (e.g. /home/data/project/<app>). Applies the
        same deny-list plus a reserved-top-level guard so OS/system service dirs
        (/usr, /var, /etc, …) don't become projects. Returns the project name, or
        'System' if the path is reserved/excluded. The dir need not exist in this
        process (it's a real host path); the correlator sizes it via the /host mount."""
        if not self.is_promotable_dir(path_str):
            return "System"
        p = Path(path_str)
        self._add(p.name, p)
        return p.name if p.name in self._projects else "System"

    def is_promotable_dir(self, path_str: str) -> bool:
        """Whether a service/container dir is eligible to become a project: an absolute
        path at least /<top>/<app> deep, whose top-level is not the OS (_SYSTEM_TOP_DIRS)
        and which is not under an excluded path / skipped mount. Callers filter candidate
        dirs with this BEFORE choosing the shallowest, so a reserved dir like '/' can't
        swallow every real candidate as its 'ancestor'."""
        if not path_str or not path_str.startswith("/"):
            return False
        p = Path(path_str)
        if len(p.parts) < 3 or p.parts[1] in _SYSTEM_TOP_DIRS or is_home_dir(p):
            return False
        for anc in [p, *p.parents]:
            s = str(anc)
            if s in self.exclude_paths or s in self._skip_mounts:
                return False
        return True

    def get_project_from_service_name(self, service_name: str, unit_file_path: str = "") -> str:
        if unit_file_path:
            return self.get_project_from_path(unit_file_path)
        return "System"

    def get_project_from_container(
        self,
        container_labels: dict,
        working_dir: str = "",
        bind_mounts: List[str] = None,
        container_name: str = "",
    ) -> str:
        compose_project = container_labels.get("com.docker.compose.project")
        if compose_project and compose_project in self._projects:
            return compose_project
        if working_dir:
            tagged = self.get_project_from_path(working_dir)
            if tagged != "System":
                return tagged
        for src in bind_mounts or []:
            tagged = self.get_project_from_path(src)
            if tagged != "System":
                return tagged
        if container_name and container_name in self._projects:
            return container_name
        return "System"

    def get_project_from_domain(self, domain: str) -> str:
        if not domain:
            return "System"
        subdomain = domain.lower().split(".")[0]
        candidate = self.DOMAIN_MAPPING.get(subdomain)
        if candidate and candidate in self._projects:
            return candidate
        return "System"
