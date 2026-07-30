"""Systemd scanner — services and timers."""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.scanners.base import BaseScanner

# Trailing components of an ExecStart binary path that are wrappers, not the app dir —
# walk past them to find the real project root (…/mdb-discovery/venv/bin/python → …/mdb-discovery).
_GENERIC_BIN_DIRS = {"bin", "sbin", "venv", ".venv", "dist", "build", "scripts", ".local", "libexec"}


# Properties we ask `systemctl show` for in one shot — cheaper than N calls.
SYSTEMD_SHOW_PROPS = [
    "FragmentPath",
    "DropInPaths",
    "ExecStart",
    "WorkingDirectory",
    "User",
    "Group",
    "Environment",
    "EnvironmentFiles",
    "Restart",
    "Description",
    "UnitFileState",
    # Runtime state — also valid for installed-but-unloaded units (so a
    # disabled+stopped unit, which drops out of `list-units`, still reports
    # the real ActiveState=inactive instead of going stale at "active").
    "ActiveState",
    "SubState",
    "LoadState",
]


def _parse_show_output(text: str) -> Dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k] = v
    return out


def _env_keys_from_systemd(env_value: str) -> List[str]:
    """systemd's Environment= is space-separated KEY=VAL pairs (quoted as needed)."""
    if not env_value:
        return []
    keys = []
    # Simple tokenizer: split on whitespace not in quotes
    tokens = re.findall(r'(?:[^\s"]+|"[^"]*")+', env_value)
    for t in tokens:
        t = t.strip('"')
        if "=" in t:
            keys.append(t.split("=", 1)[0])
    return keys


class SystemdScanner(BaseScanner):
    @property
    def scanner_name(self) -> str:
        return "systemd"

    def scan(self) -> List[Dict[str, Any]]:
        self._show_cache: Dict[str, Dict[str, str]] = {}
        # Pre-pass: promote each app's dir ONCE, using the shallowest dir across all
        # units, so a multi-service app (e.g. mdb-discovery-proxy in /…/mdb-discovery
        # and mdb-discovery-backend in /…/mdb-discovery/backend) collapses to a single
        # `mdb-discovery` project instead of also spawning a bogus `backend`.
        self._promote_service_dirs()
        return self._scan_units("service") + self._scan_units("timer")

    def _candidate_dir(self, show: Dict[str, str]) -> str:
        """The app dir a service runs from: WorkingDirectory if set, else the ExecStart
        binary's dir walked up past generic bin/venv/… wrappers. '' if neither is usable."""
        wd = show.get("WorkingDirectory", "")
        if wd.startswith("/"):
            return wd
        m = re.search(r"path=(/\S+)", show.get("ExecStart", ""))
        if m:
            d = Path(m.group(1)).parent
            while d.name in _GENERIC_BIN_DIRS and len(d.parts) > 2:
                d = d.parent
            return str(d)
        return ""

    def _promote_service_dirs(self) -> None:
        """Register the shallowest app dir per install area so all of an app's services
        attribute to one project. Reserved/system dirs are dropped by the detector."""
        names = set(self._list_unit_file_names("service"))
        r = self.run_cmd(  # best-effort here — no label, silent on failure
            ["systemctl", "list-units", "--type=service", "--all", "--no-pager", "--plain"],
            timeout=15,
        )
        if r is not None:
            for line in r.stdout.splitlines():
                parts = line.split(None, 4)
                if parts and parts[0].endswith(".service"):
                    names.add(parts[0])
        # Only PROMOTABLE dirs are candidates — filter first so a reserved dir (e.g. a
        # service with WorkingDirectory=/) can't count as everyone's ancestor and drop
        # every real candidate in the shallowest-wins reduce below.
        cands = []
        for n in names:
            d = self._candidate_dir(self._systemctl_show(n))
            if d and self.project_detector.is_promotable_dir(d):
                cands.append(d)
        # Keep only dirs with no other candidate as a proper ancestor (shallowest wins).
        for d in dict.fromkeys(cands):
            dp = Path(d)
            if any(Path(c) in dp.parents for c in cands if c != d):
                continue
            # Already inside a project the detector found (e.g. the app's real root was
            # discovered by its .git/compose marker, and this service runs from a subdir
            # like /data/mxh/backend)? Then don't mint a component-named project —
            # longest-prefix attribution will map the service to that app.
            if self.project_detector.get_project_from_path(d) != "System":
                continue
            self.project_detector.register_project_from_dir(d)

    def _scan_units(self, unit_type: str) -> List[Dict[str, Any]]:
        category = f"systemd_{unit_type}"
        assets: List[Dict[str, Any]] = []
        seen: set = set()

        # Pass A — loaded units (runtime state comes inline from list-units).
        result = self.run_cmd(
            [
                "systemctl", "list-units", f"--type={unit_type}",
                "--all", "--no-pager", "--plain",
            ],
            timeout=15, label=f"systemctl list-units {unit_type}",
        )
        if result is not None:
            for line in result.stdout.splitlines():
                parts = line.split(None, 4)
                if len(parts) < 4:
                    continue
                unit_name = parts[0]
                if not unit_name.endswith(f".{unit_type}"):
                    continue
                seen.add(unit_name)
                # Per-unit guard: a single malformed unit must not sink the whole
                # scan (it would lose every other unit as partial results).
                try:
                    assets.append(self._build_unit_asset(
                        unit_type, category, unit_name,
                        load_state=parts[1], active_state=parts[2], sub_state=parts[3],
                    ))
                except Exception as e:  # noqa: BLE001
                    self.add_error(f"unit {unit_name}: {e}")

        # Pass B — installed-but-unloaded units (disabled/stopped drop out of
        # list-units). Pull their real state from `systemctl show` so they don't
        # go stale at the last-known "active".
        for unit_name in self._list_unit_file_names(unit_type):
            if unit_name in seen:
                continue
            try:
                show = self._systemctl_show(unit_name)
                assets.append(self._build_unit_asset(
                    unit_type, category, unit_name,
                    load_state=show.get("LoadState", "loaded"),
                    active_state=show.get("ActiveState", "inactive"),
                    sub_state=show.get("SubState", "dead"),
                    show=show,
                ))
            except Exception as e:  # noqa: BLE001
                self.add_error(f"unit {unit_name}: {e}")
        return assets

    def _list_unit_file_names(self, unit_type: str) -> List[str]:
        r = self.run_cmd(
            [
                "systemctl", "list-unit-files", f"--type={unit_type}",
                "--no-pager", "--plain",
            ],
            timeout=15, label=f"systemctl list-unit-files {unit_type}",
        )
        if r is None:
            return []
        names = []
        for line in r.stdout.splitlines():
            parts = line.split(None, 1)
            if parts and parts[0].endswith(f".{unit_type}"):
                names.append(parts[0])
        return names

    def _build_unit_asset(
        self, unit_type: str, category: str, unit_name: str, *,
        load_state: str, active_state: str, sub_state: str,
        show: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        if show is None:
            show = self._systemctl_show(unit_name)
        unit_file_path = show.get("FragmentPath", "")
        project = self.project_detector.get_project_from_service_name(
            unit_name, unit_file_path
        )
        # Resolve via WorkingDirectory / ExecStart if the unit file is in /etc/systemd
        # but the app lives elsewhere. A unit's WorkingDirectory is proof the dir is a
        # real app, so PROMOTE it to a project (register_project_from_dir applies the
        # deny-list + reserved-top guard) rather than only matching existing ones — this
        # is what surfaces a systemd-tracked app under a non-marker path like
        # /home/data/project/<app>. ExecStart's binary path only matches (no promote,
        # to avoid naming a project after a stray /bin dir). Absolute paths only —
        # systemd quoting prefixes like `!/root` are not real paths.
        # The pre-pass (_promote_service_dirs) already registered the shallowest app dir,
        # so MATCH it here (longest-prefix) — both /…/mdb-discovery and /…/mdb-discovery/
        # backend resolve to the one `mdb-discovery` project. If unmatched (a lone service
        # the pre-pass didn't cover, or _build_unit_asset called directly), promote its
        # own dir; register applies the deny-list so /usr/bin etc. stay System.
        cand = self._candidate_dir(show)
        if project == "System" and cand:
            project = self.project_detector.get_project_from_path(cand)
            if project == "System":
                project = self.project_detector.register_project_from_dir(cand)

        metadata = {
            "load_state": load_state,
            "active_state": active_state,
            "sub_state": sub_state,
            "unit_type": unit_type,
            "unit_file": unit_file_path or None,
            "drop_in_paths": [p for p in (show.get("DropInPaths") or "").split() if p],
            "exec_start": show.get("ExecStart") or None,
            "working_directory": show.get("WorkingDirectory") or None,
            "user": show.get("User") or None,
            "group": show.get("Group") or None,
            "restart": show.get("Restart") or None,
            "unit_file_state": show.get("UnitFileState") or None,
            "description": show.get("Description") or None,
            "environment_keys": _env_keys_from_systemd(show.get("Environment") or ""),
            "environment_files": [
                p for p in (show.get("EnvironmentFiles") or "").split() if p
            ],
        }
        health = {
            "loaded": load_state == "loaded",
            "active": active_state == "active",
        }
        if unit_type == "service":
            health["enabled"] = self._is_enabled(unit_name)

        return self.create_asset(
            category=category,
            asset_id=f"{self.server_id}:{unit_type}:{unit_name}",
            name=unit_name,
            status=active_state,
            project=project,
            metadata=metadata,
            health_indicators=health,
        )

    def _systemctl_show(self, unit_name: str) -> Dict[str, str]:
        cache = getattr(self, "_show_cache", None)
        if cache is not None and unit_name in cache:
            return cache[unit_name]
        result = self.run_cmd(
            ["systemctl", "show", "-p", ",".join(SYSTEMD_SHOW_PROPS), unit_name],
            timeout=5,
        )
        parsed = _parse_show_output(result.stdout) if result is not None else {}
        if cache is not None:
            cache[unit_name] = parsed
        return parsed

    def _is_enabled(self, unit_name: str) -> bool:
        result = self.run_cmd(
            ["systemctl", "is-enabled", "--quiet", unit_name], timeout=5, text=False,
        )
        return result is not None and result.returncode == 0
