"""TLS certificate scanner — Let's Encrypt lineages + expiry/issuer/SANs.

Captures the cert a web app depends on (and which the certbot cron renews) as a
first-class asset, so a project's blast radius includes its certificate. Cert
files live under /etc/letsencrypt (root-only), so reads fall back to `sudo -n`
for the PUBLIC cert only (fullchain.pem — never the private key).

Read-only; never raises (BaseScanner contract). Degrades to 'expiry unknown'
when neither a direct read nor sudo can parse the cert.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core import hostpath
from app.scanners.base import BaseScanner

LE_LIVE = "/etc/letsencrypt/live"


def _run(cmd: List[str], timeout: int = 10):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except Exception:
        return None


class CertScanner(BaseScanner):
    @property
    def scanner_name(self) -> str:
        return "certs"

    def scan(self) -> List[Dict[str, Any]]:
        assets: List[Dict[str, Any]] = []
        for lineage in self._lineages():
            cert_path = f"{LE_LIVE}/{lineage}/fullchain.pem"
            info = self._read_cert(cert_path)
            assets.append(self._make_asset(lineage, cert_path, info))
        return assets

    def _lineages(self) -> List[str]:
        """List cert lineage dirs under /etc/letsencrypt/live. In a container the
        root-owned dir is reachable via the /host mount; native falls back to
        `ls` then `sudo -n ls`."""
        dirs = hostpath.host_glob([f"{LE_LIVE}/*"])
        if dirs:
            out = []
            for d in dirs:
                name = Path(d).name
                if name and name != "README":
                    out.append(name)
            if out:
                return out
        r = _run(["ls", "-1", LE_LIVE])
        if not r or r.returncode != 0:
            r = _run(["sudo", "-n", "ls", "-1", LE_LIVE])
        if not r or r.returncode != 0:
            self.add_error(f"cannot list {LE_LIVE} (no read perms / sudo)")
            return []
        out = []
        for line in r.stdout.splitlines():
            name = line.strip()
            if name and name != "README" and not name.startswith("total "):
                out.append(name)
        return out

    def _read_cert(self, path: str) -> Dict[str, Any]:
        """`path` is the REAL host cert path. Read it via the /host mount if that's
        where this process can reach it; else openssl directly, then `sudo -n`."""
        read_path = hostpath.to_read_path(path)
        args = [
            "openssl", "x509", "-in", read_path, "-noout",
            "-enddate", "-startdate", "-issuer", "-subject",
            "-ext", "subjectAltName",
        ]
        r = _run(args)
        if not r or r.returncode != 0:
            # sudo fallback reads the REAL path (native root-only cert, no /host).
            r = _run(["sudo", "-n", "openssl", "x509", "-in", path, "-noout",
                      "-enddate", "-startdate", "-issuer", "-subject",
                      "-ext", "subjectAltName"])
        if not r or r.returncode != 0:
            return {}
        return self._parse_openssl(r.stdout)

    @staticmethod
    def _parse_openssl(text: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("notAfter="):
                out["not_after"] = line[len("notAfter="):].strip()
            elif line.startswith("notBefore="):
                out["not_before"] = line[len("notBefore="):].strip()
            elif line.startswith("issuer="):
                out["issuer"] = line[len("issuer="):].strip()
            elif line.startswith("subject="):
                out["subject"] = line[len("subject="):].strip()
            elif "DNS:" in line:
                out["sans"] = [
                    s.strip()[len("DNS:"):]
                    for s in line.split(",")
                    if s.strip().startswith("DNS:")
                ]
        return out

    @staticmethod
    def _parse_not_after(raw: Optional[str]):
        """openssl prints e.g. 'Aug 21 16:57:06 2026 GMT'. Returns (days, expired)."""
        if not raw:
            return None, None
        try:
            dt = datetime.strptime(
                " ".join(raw.split()), "%b %d %H:%M:%S %Y %Z"
            ).replace(tzinfo=timezone.utc)
            days = (dt - datetime.now(timezone.utc)).days
            return days, days < 0
        except Exception:
            return None, None

    def _make_asset(
        self, lineage: str, cert_path: str, info: Dict[str, Any]
    ) -> Dict[str, Any]:
        days, expired = self._parse_not_after(info.get("not_after"))
        domains = info.get("sans") or [lineage]

        project = "System"
        for d in domains:
            p = self.project_detector.get_project_from_domain(d)
            if p and p != "System":
                project = p
                break

        if expired:
            status = "expired"
        elif days is not None and days < 30:
            status = "expiring"
        elif days is not None:
            status = "valid"
        else:
            status = "unknown"

        return self.create_asset(
            category="tls_certificate",
            asset_id=f"{self.server_id}:cert:{lineage}",
            name=lineage,
            status=status,
            project=project,
            metadata={
                "domains": domains,
                "cert_path": cert_path,
                "not_after": info.get("not_after"),
                "not_before": info.get("not_before"),
                "issuer": info.get("issuer"),
                "subject": info.get("subject"),
                "days_until_expiry": days,
                "source": "letsencrypt",
            },
            health_indicators={
                "expired": expired,
                "expiring_soon": (days is not None and 0 <= days < 30),
            },
        )
