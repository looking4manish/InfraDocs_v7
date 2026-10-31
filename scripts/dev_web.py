"""Serve the built frontend the way Caddy does in production: static files with an SPA
fallback, and /api/* + /join.sh proxied to an API. For local end-to-end checks only.

    python scripts/dev_web.py --port 8081 --api http://127.0.0.1:8090 --dist frontend/dist
"""

import argparse
import mimetypes
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def make_handler(api: str, dist: Path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _proxy(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "connection")}
            req = urllib.request.Request(api + self.path, data=body, method=self.command, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    status, rh, data = r.status, r.headers, r.read()
            except urllib.error.HTTPError as e:
                status, rh, data = e.code, e.headers, e.read()
            except Exception as e:  # noqa: BLE001
                status, rh, data = 502, {}, str(e).encode()
            self.send_response(status)
            for k, v in (rh.items() if hasattr(rh, "items") else []):
                if k.lower() not in ("transfer-encoding", "connection", "content-length", "server", "date"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _static(self):
            path = self.path.split("?")[0]
            f = (dist / path.lstrip("/")).resolve()
            if not str(f).startswith(str(dist.resolve())) or not f.is_file():
                f = dist / "index.html"
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(str(f))[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self):
            if self.path.startswith("/api/") or self.path.startswith("/join.sh"):
                self._proxy()
            else:
                self._static()

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _route

    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--api", required=True)
    ap.add_argument("--dist", default="frontend/dist")
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.api.rstrip("/"), Path(a.dist))).serve_forever()
