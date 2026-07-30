"""Cron scanner — user crontabs, /etc/crontab, /etc/cron.d, and periodic dirs.

Cron is a classic blind spot: a job can renew a cert, back up a volume, or poke
an app on a schedule with nothing in docker/systemd to show for it. Capturing
cron jobs is part of the "know every asset that could exist" completeness goal —
without it, a project teardown could leave an orphaned schedule behind.

Read-only: parses crontab files / `crontab -l`. Never raises (BaseScanner
contract); unreadable sources are recorded as errors and skipped.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core import hostpath
from app.scanners.base import BaseScanner

# Absolute paths mentioned in a command, used to attribute a job to a project.
_PATH_RE = re.compile(r"(/[^\s'\"]+)")
# A cron schedule shorthand like @daily / @reboot.
_NICKNAME_RE = re.compile(r"^@(reboot|yearly|annually|monthly|weekly|daily|midnight|hourly)\b")


def _short(s: str, n: int = 60) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


class CronScanner(BaseScanner):
    @property
    def scanner_name(self) -> str:
        return "cron"

    def scan(self) -> List[Dict[str, Any]]:
        assets: List[Dict[str, Any]] = []
        assets += self._scan_user_crontabs()
        assets += self._scan_system_crontab()
        assets += self._scan_cron_d()
        assets += self._scan_periodic_dirs()
        return assets

    # ---- sources ----------------------------------------------------------

    def _scan_user_crontabs(self) -> List[Dict[str, Any]]:
        """The invoking user's crontab (read via `crontab -l`), plus any other
        spool crontabs we can actually read (skipped silently when root-only)."""
        out: List[Dict[str, Any]] = []
        try:
            r = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True, timeout=10, check=False
            )
            if r.returncode == 0 and r.stdout.strip():
                user = os.environ.get("USER") or "self"
                out += self._parse_lines(r.stdout, source=f"crontab:{user}",
                                         has_user_field=False, default_user=user)
        except FileNotFoundError:
            pass  # `crontab` not installed (system uses /etc/cron.d) — not an error
        except Exception as e:
            self.add_error(f"crontab -l failed: {e}")

        # Spool crontabs — read through the /host mount when containerized. The
        # spool file's name IS the owning user. `read_host_configs` yields the REAL
        # host path so `source`/user are reported correctly.
        for real_path, text in hostpath.read_host_configs(
            ["/var/spool/cron/crontabs/*", "/var/spool/cron/*"]
        ):
            name = Path(real_path).name
            out += self._parse_lines(text, source=f"spool:{name}",
                                     has_user_field=False, default_user=name)
        return out

    def _scan_system_crontab(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for real_path, text in hostpath.read_host_configs(["/etc/crontab"]):
            out += self._parse_lines(text, source="/etc/crontab", has_user_field=True)
        return out

    def _scan_cron_d(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for real_path, text in sorted(hostpath.read_host_configs(["/etc/cron.d/*"])):
            name = Path(real_path).name
            out += self._parse_lines(text, source=f"/etc/cron.d/{name}", has_user_field=True)
        return out

    def _scan_periodic_dirs(self) -> List[Dict[str, Any]]:
        """run-parts dirs: each executable script runs on the dir's cadence. The
        command IS the REAL host script path (that's what cron runs)."""
        out: List[Dict[str, Any]] = []
        for cadence in ("hourly", "daily", "weekly", "monthly"):
            for read_path in sorted(hostpath.host_glob([f"/etc/cron.{cadence}/*"])):
                try:
                    if not Path(read_path).is_file():
                        continue
                except OSError:
                    continue
                real = hostpath.to_real(read_path)
                if Path(real).name in (".placeholder", "0anacron"):
                    continue
                out.append(self._make_asset(
                    schedule=f"@{cadence}", command=real, user="root",
                    source=f"/etc/cron.{cadence}", raw=f"run-parts {real}",
                ))
        return out

    # ---- parsing ----------------------------------------------------------

    def _parse_lines(
        self, text: str, *, source: str, has_user_field: bool,
        default_user: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Skip environment assignments (e.g. PATH=..., MAILTO=...).
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", line):
                continue

            schedule, rest = self._split_schedule(line)
            if schedule is None or not rest:
                continue

            user = default_user or "root"
            command = rest
            if has_user_field:
                bits = rest.split(None, 1)
                if len(bits) == 2:
                    user, command = bits[0], bits[1]
                else:
                    user, command = bits[0], ""
            if not command:
                continue
            out.append(self._make_asset(
                schedule=schedule, command=command, user=user, source=source, raw=line,
            ))
        return out

    def _split_schedule(self, line: str):
        """Return (schedule, remainder) or (None, None) if not a cron entry."""
        nick = _NICKNAME_RE.match(line)
        if nick:
            return line[: nick.end()], line[nick.end():].strip()
        parts = line.split(None, 5)
        if len(parts) < 6:
            return None, None
        schedule = " ".join(parts[:5])
        # Each of the 5 fields must look cron-ish (digits, * , - / ,).
        if not all(re.fullmatch(r"[\d*/,\-]+", f) for f in parts[:5]):
            return None, None
        return schedule, parts[5]

    # ---- asset ------------------------------------------------------------

    def _make_asset(
        self, *, schedule: str, command: str, user: str, source: str, raw: str
    ) -> Dict[str, Any]:
        project = self._project_for_command(command)
        digest = hashlib.sha1(f"{source}|{schedule}|{command}".encode()).hexdigest()[:10]
        return self.create_asset(
            category="cron_job",
            asset_id=f"{self.server_id}:cron:{digest}",
            name=f"{schedule} {_short(command)}",
            status="active",
            project=project,
            metadata={
                "schedule": schedule,
                "command": command,
                "user": user,
                "source": source,
                "raw": raw,
            },
            health_indicators={"scheduled": True},
        )

    def _project_for_command(self, command: str) -> str:
        """Attribute by the first path in the command that lands under a project."""
        for m in _PATH_RE.findall(command):
            proj = self.project_detector.get_project_from_path(m)
            if proj and proj != "System":
                return proj
        return "System"
