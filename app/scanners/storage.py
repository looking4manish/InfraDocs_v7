"""Storage scanner — mountpoints and usage.

Native: parses `df`. Containerized: when INFRADOCS_HOST_ROOT is set (e.g. /host,
the host root mounted read-only), reads the HOST mount table from /proc/1/mounts
(needs --pid host) and statvfs's each mount via the /host prefix — so a container
reports the real host filesystems, not its own.
"""

import os
import subprocess
from typing import Any, Dict, List

from app.core.hostpath import HOST_ROOT  # single source of truth (empty = native)
from app.scanners.base import BaseScanner

SKIP_SOURCES = {"tmpfs", "devtmpfs", "udev", "overlay", "squashfs"}
# Pseudo / virtual filesystems that are never real storage.
_PSEUDO_FSTYPES = {
    "proc", "sysfs", "cgroup", "cgroup2", "devpts", "mqueue", "debugfs",
    "tracefs", "securityfs", "pstore", "bpf", "autofs", "fusectl", "configfs",
    "hugetlbfs", "tmpfs", "devtmpfs", "ramfs", "overlay", "squashfs",
    "binfmt_misc", "nsfs", "fuse.lxcfs", "rpc_pipefs",
}


def _human(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T", "P"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}E"


def _df_mounts() -> List[Dict[str, Any]]:
    """Native: parse `df` (human + byte-exact) into mount dicts."""
    def run(flag: str):
        try:
            r = subprocess.run(
                ["df", flag, "--output=source,target,size,used,avail,pcent,fstype"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except Exception:
            return []
        rows = []
        for line in r.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 7:
                rows.append(tuple(parts[:7]))
        return rows

    human = run("-h")
    byte_idx = {(r[0], r[1]): r for r in run("-B1")}
    out = []
    for source, target, size, used, avail, percent, fstype in human:
        if source in SKIP_SOURCES or fstype in SKIP_SOURCES or target.startswith("/snap/"):
            continue
        try:
            pct = int(percent.rstrip("%"))
        except ValueError:
            pct = 0
        bs = bu = ba = bt = None
        br = byte_idx.get((source, target))
        if br:
            try:
                bs = int(br[2]); bu = int(br[3]); ba = int(br[4]); bt = bs
            except (TypeError, ValueError):
                pass
        out.append({
            "source": source, "target": target, "fstype": fstype,
            "size": size, "used": used, "avail": avail, "usage_pct": pct,
            "size_bytes": bs, "used_bytes": bu, "free_bytes": ba, "total_bytes": bt,
        })
    return out


def _host_mounts(root: str) -> List[Dict[str, Any]]:
    """Containerized: real host filesystems via /proc/1/mounts + statvfs over /host."""
    lines = []
    for path in ("/proc/1/mounts", "/proc/mounts"):
        try:
            with open(path) as f:
                lines = f.read().splitlines()
            break
        except OSError:
            continue
    out, seen = [], set()
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        source, target, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
        if fstype in _PSEUDO_FSTYPES or source in SKIP_SOURCES:
            continue
        if not target.startswith("/") or target.startswith("/snap/") or target in seen:
            continue
        seen.add(target)
        statpath = root + target if target != "/" else root
        try:
            st = os.statvfs(statpath)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total == 0:
            continue
        free = st.f_bavail * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        pct = round(used / total * 100) if total else 0
        out.append({
            "source": source, "target": target, "fstype": fstype,
            "size": _human(total), "used": _human(used), "avail": _human(free),
            "usage_pct": pct, "size_bytes": total, "used_bytes": used,
            "free_bytes": free, "total_bytes": total,
        })
    return out


class StorageScanner(BaseScanner):
    @property
    def scanner_name(self) -> str:
        return "storage"

    def scan(self) -> List[Dict[str, Any]]:
        mounts = _host_mounts(HOST_ROOT) if HOST_ROOT else _df_mounts()
        if not mounts:
            self.add_error("no mounts found (df/proc empty)")
            return []

        assets: List[Dict[str, Any]] = []
        for m in mounts:
            target = m["target"]
            project = self.project_detector.get_project_from_path(target)
            pct = m["usage_pct"]
            assets.append(
                self.create_asset(
                    category="storage_mount",
                    asset_id=f"{self.server_id}:mount:{target}",
                    name=target,
                    status="mounted",
                    project=project,
                    metadata={
                        "source": m["source"],
                        "fstype": m["fstype"],
                        "size": m["size"],
                        "used": m["used"],
                        "available": m["avail"],
                        "usage_percent": pct,
                        "size_bytes": m["size_bytes"],
                        "total_bytes": m["total_bytes"],
                        "used_bytes": m["used_bytes"],
                        "free_bytes": m["free_bytes"],
                    },
                    health_indicators={
                        "usage_percent": pct,
                        "near_full": pct >= 75,
                        "critical": pct >= 90,
                    },
                )
            )
        return assets
