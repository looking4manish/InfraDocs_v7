"""Read host config files transparently — directly (native) or via the container
/host mount (dockerized). Lets path-based scanners (cloudflared, caddy, …) find the
REAL host's configs instead of the container's empty ones, and report real paths.
"""

import glob
import os
from pathlib import Path
from typing import Iterator, List, Tuple

HOST_ROOT = os.environ.get("INFRADOCS_HOST_ROOT", "").rstrip("/")


def to_real(p: str) -> str:
    """Strip the /host mount prefix so a readable path becomes the REAL host path
    it should be stored/reported as. `HOST_ROOT` itself maps to "/" (the whole-host
    bind). Native (no HOST_ROOT) is a no-op. Single source of truth for host-path
    mapping — project_detector and the path-based scanners all route through here."""
    if HOST_ROOT and p.startswith(HOST_ROOT + "/"):
        return p[len(HOST_ROOT):]
    if HOST_ROOT and p == HOST_ROOT:
        return "/"
    return p


# Back-compat alias for internal callers below.
_to_real = to_real


def to_read_path(real_path: str) -> str:
    """Map a REAL host path to the path THIS process can actually read it at:
    prefixed with the /host mount inside a container (if that prefixed path
    exists), else returned unchanged. Native (no HOST_ROOT) is a no-op. Lets
    code that stores real host paths still stat/size them from inside the
    container."""
    if not real_path:
        return real_path
    if HOST_ROOT and not real_path.startswith(HOST_ROOT + "/") and real_path != HOST_ROOT:
        candidate = HOST_ROOT + real_path
        if os.path.exists(candidate):
            return candidate
    return real_path


def host_glob(patterns: List[str]) -> List[str]:
    """Real host paths matching any pattern (searched directly + under /host)."""
    seen, out = set(), []
    for pat in patterns:
        bases = ["", HOST_ROOT] if HOST_ROOT else [""]
        for base in bases:
            for fp in glob.glob(base + pat):
                real = _to_real(fp)
                if real not in seen:
                    seen.add(real)
                    out.append(fp)  # the readable path (may be /host-prefixed)
    return out


def read_host_configs(patterns: List[str]) -> Iterator[Tuple[str, str]]:
    """Yield (real_path, text) for each readable file matching the patterns."""
    for fp in host_glob(patterns):
        try:
            yield _to_real(fp), Path(fp).read_text(errors="replace")
        except OSError:
            continue
