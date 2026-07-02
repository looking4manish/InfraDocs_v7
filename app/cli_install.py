"""CLI install helper — the testable logic behind install.sh.

install.sh handles the terminal prompts + orchestration; the API-driven validation and
config rendering live here so they can be unit-tested and so we DRIVE the existing APIs
rather than reimplementing them:
  - priority uniqueness  -> GET  <primary>/api/cluster/health  (unauth; returns priority + peers)
  - bidirectional enroll -> POST <local>/api/setup/complete     (which calls /federation/enroll,
                            and the primary connects back to this node's /federation/ping)

Mesh-agnostic: nothing here installs or assumes Tailscale/any VPN. A node is reachable at
the ADDRESS the operator supplies; that address is what gets stored + redirected to.
"""

import argparse
import base64
import ipaddress
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


# ----------------------- HTTP (injectable for tests) ------------------------


def _get(url: str, timeout: int = 8) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode())


def _post(url: str, body: dict, auth=None, timeout: int = 20):
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if auth:
        tok = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        headers["Authorization"] = f"Basic {tok}"
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return getattr(r, "status", 200), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            payload = {}
        return e.code, payload


# ----------------------- validation -----------------------------------------


def validate_priority(priority) -> tuple:
    try:
        p = int(priority)
    except (TypeError, ValueError):
        return False, "priority must be a whole number"
    if not (1 <= p <= 99):
        return False, f"priority {p} is out of range (must be 1-99)"
    return True, None


def taken_priorities(primary_url: str, getter=_get) -> dict:
    """node_id -> priority known to the primary (its own + every peer it has heard)."""
    h = getter(primary_url.rstrip("/") + "/api/cluster/health")
    taken = {}
    if h.get("node_id") and h.get("priority") is not None:
        taken[h["node_id"]] = h["priority"]
    for p in h.get("peers") or []:
        if p.get("priority") is not None:
            taken[p.get("node_id")] = p["priority"]
    return taken


def check_priority_free(primary_url: str, priority, getter=_get) -> tuple:
    """Range-check AND query the primary so a duplicate is rejected before we deploy."""
    ok, reason = validate_priority(priority)
    if not ok:
        return False, reason
    p = int(priority)
    try:
        taken = taken_priorities(primary_url, getter)
    except Exception as e:  # noqa: BLE001
        return False, f"could not reach the primary at {primary_url} to check priorities: {e}"
    for nid, tp in taken.items():
        if tp == p:
            return False, f"priority {p} already in use (by '{nid}') — pick a free one"
    return True, None


def primary_reachable(primary_url: str, getter=_get) -> bool:
    """Cheap secondary->primary precheck before we bother deploying."""
    try:
        getter(primary_url.rstrip("/") + "/api/health")
        return True
    except Exception:  # noqa: BLE001
        return False


# ----------------------- reachable-address detection ------------------------
# Best-effort, mesh-agnostic candidates so the installer can offer a pick-list instead
# of a blank "type your address from memory" prompt. Tailscale is used ONLY if present;
# nothing here hard-depends on it. All command output is read through an injectable
# runner so this is unit-testable without a network.


def _cmd_out(runner, cmd, timeout: int = 5) -> str:
    try:
        r = runner(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception:  # noqa: BLE001 — missing binary / timeout / anything → no data
        return ""
    if getattr(r, "returncode", 1) != 0:
        return ""
    return r.stdout or ""


def _tailscale_ipv4(runner) -> list:
    ips = []
    for line in _cmd_out(runner, ["tailscale", "ip", "-4"]).splitlines():
        s = line.strip()
        try:
            ipaddress.ip_address(s)
        except ValueError:
            continue
        ips.append(s)
    return ips


def _interface_ipv4(runner) -> list:
    """[(ip, iface)] for IPv4 addrs. Prefer `ip -j -4 addr`; fall back to `hostname -I`."""
    pairs = []
    out = _cmd_out(runner, ["ip", "-j", "-4", "addr"])
    if out:
        try:
            data = json.loads(out)
        except Exception:  # noqa: BLE001
            data = []
        for link in data:
            iface = link.get("ifname", "")
            for a in link.get("addr_info", []):
                if a.get("family") == "inet" and a.get("local"):
                    pairs.append((a["local"], iface))
    if not pairs:
        for tok in _cmd_out(runner, ["hostname", "-I"]).split():
            try:
                ipaddress.ip_address(tok)
            except ValueError:
                continue
            pairs.append((tok, ""))
    return pairs


def _in_cgnat(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def _is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False

def _is_public(ip: str) -> bool:
    """A globally-routable IPv4 (a real public address), not private/CGNAT/link-local."""
    try:
        a = ipaddress.ip_address(ip)
        return a.is_global and not a.is_private
    except ValueError:
        return False


def detect_addresses(web_port, runner=subprocess.run) -> list:
    """Peer-reachable candidate addresses for THIS node, ordered best-first (tailscale,
    then LAN). Loopback is excluded and localhost is NOT included here — install.sh adds
    the explicit 'this machine only' + 'enter manually' options to the menu. The CGNAT
    range 100.64.0.0/10 (Tailscale's range) is labelled tailscale even when it shows up
    as a plain interface. Returns [{'url','label','kind'}]; empty if nothing is found."""
    try:
        port = int(web_port)
    except (TypeError, ValueError):
        port = 8081
    out, seen = [], set()

    def add(ip, label, kind):
        url = f"http://{ip}:{port}"
        if url in seen:
            return
        seen.add(url)
        out.append({"url": url, "label": label, "kind": kind})

    for ip in _tailscale_ipv4(runner):
        add(ip, "tailscale", "tailscale")
    for ip, iface in _interface_ipv4(runner):
        if _is_loopback(ip):
            continue
        if _in_cgnat(ip):
            add(ip, "tailscale", "tailscale")
        elif _is_public(ip):
            add(ip, f"public IP — only if :{port} open in cloud firewall", "public")
        else:
            add(ip, f"lan / {iface}" if iface else "lan", "lan")
    rank = {"tailscale": 0, "lan": 1, "public": 2}
    out.sort(key=lambda c: rank.get(c["kind"], 9))  # stable: insertion order kept within a kind
    return out


def normalize_url(url, default_port: int = 8081) -> str:
    """Ensure a reachable address carries an EXPLICIT port. A portless http://host is
    normalized to http://host:<default_port> before it is stored or probed — otherwise
    the bidirectional enroll probe hits 80/443 and fails with a misleading TLS alert."""
    if not url:
        return url
    from urllib.parse import urlsplit, urlunsplit
    u = url.strip()
    if "://" not in u:
        u = "http://" + u
    parts = urlsplit(u)
    host = parts.hostname
    if host and parts.port is None:
        netloc = f"[{host}]" if ":" in host else host
        netloc = f"{netloc}:{default_port}"
        if parts.username:
            auth = parts.username + (f":{parts.password}" if parts.password else "")
            netloc = f"{auth}@{netloc}"
        parts = parts._replace(netloc=netloc)
    return urlunsplit(parts)


def _port_free(port) -> bool:
    """True iff we can bind 0.0.0.0:port right now (no other listener). Used by the installer
    preflight so a busy web/API port is caught BEFORE bring-up, not after a crash-loop."""
    import socket
    try:
        p = int(port)
    except (TypeError, ValueError):
        return False
    if not (1 <= p <= 65535):
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        sock.bind(("0.0.0.0", p))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _find_free_port(from_port, count: int = 50):
    """First free port scanning upward from `from_port` (inclusive). None if none in range."""
    try:
        start = int(from_port)
    except (TypeError, ValueError):
        return None
    for p in range(start, min(start + max(count, 1), 65536)):
        if _port_free(p):
            return p
    return None


# ----------------------- config rendering -----------------------------------


def render_env(cfg: dict) -> str:
    """The deploy/docker/.env this node uses. Mesh-agnostic — no tailscale/cloudflare
    sidecar profiles; the reachable address is supplied separately by the operator."""
    g = cfg.get
    lines = [
        f"SERVER_ID={cfg['server_id']}",
        f"SERVER_NAME={g('server_name') or cfg['server_id']}",
        "ADMIN_USER=admin",
        f"ADMIN_PASSWORD={g('admin_password') or 'Changeme001'}",
        f"PROJECTS_ROOT={g('projects_root') or os.path.expanduser('~/projects')}",
        f"DOMAIN=:{g('web_port') or 8081}",
        "COMPOSE_PROFILES=",   # no tailscale/cloudflare sidecars — transport-agnostic
        "CF_TUNNEL_TOKEN=",
        "TS_AUTHKEY=",
        f"WEB_PORT={g('web_port') or 8081}",
        f"WEB_TLS_PORT={g('web_tls_port') or 8443}",
        f"API_PORT={g('api_port') or 8090}",
        f"MONGO_PORT={g('mongo_port') or 27018}",
    ]
    return "\n".join(lines) + "\n"


# ----------------------- enroll / setup completion --------------------------


def build_complete_body(cfg: dict) -> dict:
    role = cfg["role"]
    body = {"server_name": cfg.get("server_name"), "role": role,
            "exposure": "domain", "advertise_url": cfg.get("advertise_url")}
    if role in ("primary", "standalone"):
        body["priority"] = 1
    else:
        body.update({"priority": int(cfg["priority"]), "primary_url": cfg.get("primary_url"),
                     "join_token": cfg.get("join_token")})
    return body


def complete_setup(api_base: str, body: dict, auth, poster=_post) -> tuple:
    """Drive POST /api/setup/complete. For a secondary this triggers the bidirectional
    reachability test + priority recheck on the primary. Returns (ok, reason, directions)."""
    status, payload = poster(api_base.rstrip("/") + "/api/setup/complete", body, auth=auth)
    if status == 200:
        directions = ({"secondary_to_primary": True, "primary_to_secondary": True}
                      if body.get("role") == "secondary" else None)
        return True, None, directions
    detail = payload.get("detail")
    if isinstance(detail, dict):
        return False, detail.get("reason") or detail.get("message") or "enrollment refused", detail.get("directions")
    return False, (detail if isinstance(detail, str) else None) or f"setup failed (HTTP {status})", None


# ----------------------- non-interactive deploy -----------------------------


def deploy(repo_dir: str, runner=subprocess.run, env: dict = None):
    """Invoke the existing docker deploy in NON-INTERACTIVE mode (it reuses the .env we
    wrote and skips its own prompts/exposure menu)."""
    e = dict(os.environ if env is None else env)
    e["INFRADOCS_NONINTERACTIVE"] = "1"
    return runner(["bash", os.path.join(repo_dir, "deploy", "docker", "deploy.sh")], env=e, check=False)


# ----------------------- thin CLI for install.sh ----------------------------


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cli_install")
    sub = ap.add_subparsers(dest="cmd")

    cp = sub.add_parser("check-primary"); cp.add_argument("--primary-url", required=True)
    ck = sub.add_parser("check-priority")
    ck.add_argument("--primary-url", required=True); ck.add_argument("--priority", required=True)
    re = sub.add_parser("render-env"); re.add_argument("--out", default="-")
    da = sub.add_parser("detect-addresses"); da.add_argument("--web-port", default="8081")
    pc = sub.add_parser("port-check"); pc.add_argument("--port", required=True)
    ff = sub.add_parser("find-free-port"); ff.add_argument("--from", dest="from_port", required=True)
    ff.add_argument("--count", type=int, default=50)
    co = sub.add_parser("complete")
    co.add_argument("--api", required=True); co.add_argument("--user", default="admin")
    co.add_argument("--password", default="Changeme001")
    co.add_argument("--role", required=True); co.add_argument("--server-name")
    # advertise-url is required for primary/secondary (a clustered node needs a reachable
    # address) but NOT for standalone, which runs on one box with no federation.
    co.add_argument("--advertise-url")
    co.add_argument("--web-port", default="8081")  # default port for a portless advertise/primary URL
    co.add_argument("--priority"); co.add_argument("--primary-url"); co.add_argument("--join-token")

    args = ap.parse_args(argv)

    if args.cmd == "check-primary":
        if primary_reachable(args.primary_url):
            return 0
        print(f"FAIL: cannot reach the primary at {args.primary_url}", file=sys.stderr)
        return 1
    if args.cmd == "check-priority":
        ok, reason = check_priority_free(args.primary_url, args.priority)
        if ok:
            return 0
        print(reason, file=sys.stderr)
        return 1
    if args.cmd == "render-env":
        # install.sh exports the config as INSTALL_<KEY> env vars (e.g. INSTALL_SERVER_ID).
        cfg = {k[len("INSTALL_"):].lower(): v for k, v in os.environ.items() if k.startswith("INSTALL_")}
        text = render_env(cfg)
        if args.out == "-":
            sys.stdout.write(text)
        else:
            with open(args.out, "w") as f:
                f.write(text)
        return 0
    if args.cmd == "detect-addresses":
        # One candidate per line: url<TAB>label<TAB>kind. install.sh renders the menu.
        for c in detect_addresses(args.web_port):
            print(f"{c['url']}\t{c['label']}\t{c['kind']}")
        return 0
    if args.cmd == "port-check":
        return 0 if _port_free(args.port) else 3
    if args.cmd == "find-free-port":
        fp = _find_free_port(args.from_port, args.count)
        if fp is None:
            print("no free port found", file=sys.stderr)
            return 1
        print(fp)
        return 0
    if args.cmd == "complete":
        _dp = int(args.web_port) if str(args.web_port).isdigit() else 8081
        adv = normalize_url(args.advertise_url, _dp) if args.advertise_url else args.advertise_url
        cfg = {"role": args.role, "server_name": args.server_name, "advertise_url": adv}
        if args.role == "secondary":
            cfg.update({"priority": args.priority,
                        "primary_url": normalize_url(args.primary_url, _dp) if args.primary_url else args.primary_url,
                        "join_token": args.join_token})
        body = build_complete_body(cfg)
        ok, reason, directions = complete_setup(args.api, body, (args.user, args.password))
        if ok:
            print("OK")
            return 0
        if directions:
            print(f"secondary->primary: {'ok' if directions.get('secondary_to_primary') else 'FAIL'}", file=sys.stderr)
            print(f"primary->secondary: {'ok' if directions.get('primary_to_secondary') else 'FAIL'}", file=sys.stderr)
        print(f"FAIL: {reason}", file=sys.stderr)
        return 1
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
