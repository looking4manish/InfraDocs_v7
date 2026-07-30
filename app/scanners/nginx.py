"""Nginx scanner — server blocks with brace-aware parsing.

V5 used a naive regex (`server\\s*\\{([^}]+)\\}`) that breaks on real
configs because nginx server blocks contain nested `location { ... }`
blocks. V6 uses a brace-balanced extractor that handles arbitrary nesting.
"""

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core import hostpath
from app.scanners.base import BaseScanner


def _read_cert_info(cert_path: str) -> Dict[str, Any]:
    """Read SSL cert issuer + expiry. Returns empty dict on any failure. `cert_path`
    is a REAL host path; read it at the path this process can actually reach (the
    /host mount inside a container)."""
    if not cert_path:
        return {}
    read_path = hostpath.to_read_path(cert_path)
    try:
        if not Path(read_path).exists():
            return {}
    except (OSError, PermissionError):
        # /etc/letsencrypt/live is typically root:root 0700 — we can't even
        # stat the dir. Try the openssl command anyway; it may have CAP_DAC.
        pass
    try:
        result = subprocess.run(
            ["openssl", "x509", "-noout", "-issuer", "-enddate", "-in", read_path],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return {}
    if result.returncode != 0:
        return {}
    info = {}
    for line in result.stdout.splitlines():
        if line.startswith("issuer="):
            info["issuer"] = line[len("issuer=") :].strip()
        elif line.startswith("notAfter="):
            info["not_after"] = line[len("notAfter=") :].strip()
    info["cloudflare_origin"] = "Cloudflare" in info.get("issuer", "")
    return info


def _extract_listen_ports(listen_directives: List[str]) -> List[int]:
    """Turn ['443 ssl', '[::]:443 ssl', '80'] into [443, 443, 80] (deduped)."""
    ports = set()
    for d in listen_directives:
        # First token holds the addr:port (or just port)
        first = d.strip().split()[0] if d.strip() else ""
        if ":" in first:
            first = first.rsplit(":", 1)[-1]
        try:
            ports.add(int(first))
        except ValueError:
            continue
    return sorted(ports)


def _parse_upstream(upstream: str) -> Tuple[Optional[str], Optional[int]]:
    """'localhost:8080' or '127.0.0.1:8080/foo' -> ('localhost', 8080)."""
    if not upstream:
        return (None, None)
    host_port = upstream.split("/", 1)[0]
    if ":" in host_port:
        host, port = host_port.rsplit(":", 1)
        try:
            return (host, int(port))
        except ValueError:
            return (host, None)
    return (host_port, None)


# Real host globs for the enabled server-block files. Resolved through
# `hostpath` so a containerized scan reads the host's configs via /host and
# reports the REAL host paths (presence of matching files — not a local nginx
# binary — is what tells us there's anything to scan).
NGINX_CONFIG_GLOBS = (
    "/etc/nginx/sites-enabled/*",
    "/etc/nginx/conf.d/*",
)


class NginxScanner(BaseScanner):
    @property
    def scanner_name(self) -> str:
        return "nginx"

    def scan(self) -> List[Dict[str, Any]]:
        assets: List[Dict[str, Any]] = []
        for read_path in hostpath.host_glob(list(NGINX_CONFIG_GLOBS)):
            p = Path(read_path)
            try:
                if not (p.is_file() or p.is_symlink()):
                    continue
            except OSError:
                continue
            # Report the REAL host path; read from the (maybe /host-prefixed) one.
            assets.extend(self._parse_file(p, Path(hostpath.to_real(read_path))))
        return assets

    def _parse_file(self, read_path: Path, path: Optional[Path] = None) -> List[Dict[str, Any]]:
        # Native / direct callers pass a single real path; the containerized `scan()`
        # passes the readable (/host-prefixed) path plus the real host path to report.
        if path is None:
            path = read_path
        try:
            content = read_path.read_text()
        except (OSError, UnicodeDecodeError) as e:
            self.add_error(f"read {path}: {e}")
            return []

        # Strip comments so they don't poison the brace counter
        content = re.sub(r"#[^\n]*", "", content)

        assets: List[Dict[str, Any]] = []
        for block in self._iter_server_blocks(content):
            asset = self._parse_block(block, path)
            if asset:
                assets.append(asset)
        return assets

    def _iter_server_blocks(self, content: str):
        """Yield the contents of each top-level `server { ... }` block."""
        i = 0
        n = len(content)
        while i < n:
            m = re.search(r"\bserver\s*\{", content[i:])
            if not m:
                return
            start = i + m.end()
            depth = 1
            j = start
            while j < n and depth > 0:
                ch = content[j]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        yield content[start:j]
                        i = j + 1
                        break
                j += 1
            else:
                # Unbalanced braces — stop
                return

    def _parse_block(self, block: str, path: Path) -> Optional[Dict[str, Any]]:
        names = re.findall(r"\bserver_name\s+([^;]+);", block)
        if not names:
            return None
        # Take the first server_name directive and split into individual hosts
        primary_names = names[0].split()
        if not primary_names:
            return None
        server_name = primary_names[0]

        listen = re.findall(r"\blisten\s+([^;]+);", block)
        listen_ports = _extract_listen_ports(listen)

        upstream_match = re.search(r"\bproxy_pass\s+https?://([^/;\s]+)", block)
        upstream = upstream_match.group(1) if upstream_match else ""
        upstream_host, upstream_port = _parse_upstream(upstream)

        # Static sites: the `root` directive points at the served directory, which
        # is the strongest project signal for non-proxied blocks.
        root_match = re.search(r"\broot\s+([^;{]+);", block)
        root_dir = root_match.group(1).strip() if root_match else None

        ssl_cert_match = re.search(r"\bssl_certificate\s+([^;]+);", block)
        ssl_cert_path = ssl_cert_match.group(1).strip() if ssl_cert_match else None
        has_ssl = bool(ssl_cert_path) or 443 in listen_ports

        cert_info = _read_cert_info(ssl_cert_path) if ssl_cert_path else {}

        # Asset id needs the listen port too — a single file commonly defines
        # both an HTTP redirect block (`listen 80`) and an HTTPS block
        # (`listen 443 ssl`) for the same server_name.
        listen_port_for_id = (
            str(listen_ports[0]) if listen_ports else "?"
        )

        # Attribute by domain (hardcoded hints), then the served root dir, then the
        # config file's own location — so static/scattered sites stop landing in System.
        project = self.project_detector.get_project_from_domain(server_name)
        if project == "System" and root_dir:
            project = self.project_detector.get_project_from_path(root_dir)
        if project == "System":
            project = self.project_detector.get_project_from_path(str(path))
        internet_exposed = has_ssl and 443 in listen_ports

        return self.create_asset(
            category="nginx_server_block",
            asset_id=f"{self.server_id}:nginx:{path.name}:{server_name}:{listen_port_for_id}",
            name=server_name,
            status="configured",
            project=project,
            metadata={
                "config_file": str(path),
                "root": root_dir,
                "server_names": primary_names,
                "listen": [l.strip() for l in listen],
                "listen_ports": listen_ports,
                "upstream": upstream,
                "upstream_host": upstream_host,
                "upstream_port": upstream_port,
                "has_ssl": has_ssl,
                "ssl_certificate": ssl_cert_path,
                "ssl_issuer": cert_info.get("issuer"),
                "ssl_not_after": cert_info.get("not_after"),
                "cloudflare_origin": cert_info.get("cloudflare_origin", False),
                "internet_exposed": internet_exposed,
                "url": f"https://{server_name}" if internet_exposed else None,
            },
            health_indicators={
                "has_ssl": has_ssl,
                "has_upstream": bool(upstream),
                "internet_exposed": internet_exposed,
            },
        )
