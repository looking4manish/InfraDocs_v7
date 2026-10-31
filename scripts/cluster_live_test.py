"""Live replica-set test: three real API processes on one machine, each with its own MongoDB
database, joined over HTTP exactly as on real servers. Then the primary is killed and the
script measures the failover, restarts the old primary, and transfers the role back.

    INFRADOCS_MONGO_URI=mongodb://127.0.0.1:27017/ python scripts/cluster_live_test.py

Needs a MongoDB at INFRADOCS_MONGO_URI. Drops the three test databases at the start.
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
CODE = "424-242"
NODES = {"oci": 18101, "oci-p": 18102, "n150": 18103}
procs = {}


def url(n):
    return f"http://127.0.0.1:{NODES[n]}"


def call(n, method, path, body=None, token=None, timeout=20):
    req = urllib.request.Request(url(n) + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")


def start(n):
    env = {**os.environ, "INFRADOCS_SERVER_ID": n, "INFRADOCS_DB": f"ifd_live_{n.replace('-', '_')}",
           "INFRADOCS_SETUP_CODE": CODE, "INFRADOCS_WEB_PORT": str(NODES[n]),
           "INFRADOCS_API_PASSWORD": "svc-pass-1234567", "INFRADOCS_PROJECTS_ROOT": "/tmp"}
    log = open(f"/tmp/ifd_live_{n}.log", "a")
    procs[n] = subprocess.Popen([PY, "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1",
                                 "--port", str(NODES[n]), "--log-level", "warning"],
                                cwd=ROOT, env=env, stdout=log, stderr=log)
    for _ in range(60):
        try:
            if call(n, "GET", "/api/cluster/rpc/ping", timeout=2)[0] == 200:
                return
        except Exception:
            time.sleep(0.5)
    raise SystemExit(f"{n} did not start; see /tmp/ifd_live_{n}.log")


def kill(n):
    procs[n].send_signal(signal.SIGKILL)
    procs[n].wait()


def state(n, token):
    return call(n, "GET", "/api/cluster/state", token=token, timeout=5)[1]


def wait_for(pred, timeout, every=0.5):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            v = pred()
            if v:
                return v, time.monotonic() - t0
        except Exception:
            pass
        time.sleep(every)
    return None, time.monotonic() - t0


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def main():
    from pymongo import MongoClient
    mc = MongoClient(os.environ["INFRADOCS_MONGO_URI"])
    for n in NODES:
        mc.drop_database(f"ifd_live_{n.replace('-', '_')}")
    for n in NODES:
        start(n)

    s, r = call("oci", "POST", "/api/setup/complete", {
        "setup_code": CODE, "admin": {"username": "manish", "password": "a-strong-password"},
        "server_name": "oci", "access": {"mode": "tailscale", "address": url("oci")}})
    check(s == 200 and r.get("token"), f"first server set up ({s})")
    tok = r["token"]
    st, _ = wait_for(lambda: state("oci", tok)["role"] == "standalone" and state("oci", tok), 15)
    check(bool(st), "oci is a standalone primary")

    for n in ("oci-p", "n150"):
        s, jc = call("oci", "POST", "/api/cluster/join-code", {"votes": 1}, token=tok)
        check(s == 200, f"join code for {n}: mode after = {jc.get('mode_after')}")
        s, jr = call(n, "POST", "/api/setup/join", {"setup_code": CODE, "primary_url": url("oci"),
                                                     "code": jc["code"], "address": url(n)}, timeout=40)
        check(s == 200, f"{n} joined: {jr}")
        s, js = call("oci", "GET", f"/api/cluster/join-code/{jc['code']}/status", token=tok)
        check(js.get("state") == "joined", f"join code shows joined for {n}")

    st, t = wait_for(lambda: (lambda x: x["mode"] == "automatic" and x["reachable_voters"] == 3 and x)(state("oci", tok)), 30)
    check(bool(st), f"3 voting members, automatic mode, quorum {st and st['quorum']} ({t:.1f}s)")
    term0 = st["term"]

    s, again = call("oci", "POST", "/api/cluster/join-code", {"votes": 1}, token=tok)
    check(again.get("mode_after") == "manual", "a 4th voter would switch to manual mode")

    # login on a secondary works with the replicated administrator
    s, lr = call("oci-p", "POST", "/api/auth/login", {"username": "manish", "password": "a-strong-password"})
    check(s == 200, "administrator account replicated to oci-p")
    tok_p = lr["token"]
    s, lr = call("n150", "POST", "/api/auth/login", {"username": "manish", "password": "a-strong-password"})
    tok_n = lr["token"]

    print("… killing the primary (oci)")
    kill("oci")
    t_kill = time.monotonic()

    def new_primary():
        a, b = state("oci-p", tok_p), state("n150", tok_n)
        prim = [x["node_id"] for x in (a, b) if x["role"] == "primary"]
        return prim if len(prim) == 1 else None
    prim, _ = wait_for(new_primary, 40)
    took = time.monotonic() - t_kill
    check(bool(prim), f"new primary {prim} elected {took:.1f}s after the kill")
    check(prim == ["oci-p"], "the best-priority survivor won")
    sp = state("oci-p", tok_p)
    check(sp["term"] > term0, f"term moved {term0} -> {sp['term']}")
    check(sp["elections"][0]["votes_for"] == 2, f"won with {sp['elections'][0]['votes_for']} of 3 votes")

    print("… restarting oci")
    start("oci")
    ok, t = wait_for(lambda: state("oci-p", tok_p)["reachable_voters"] == 3
                     and state("oci", tok)["role"] == "secondary", 30)
    check(bool(ok), f"oci rejoined as a secondary ({t:.1f}s), no automatic failback")
    check(state("oci", tok)["term"] == state("oci-p", tok_p)["term"], "oci adopted the new term")

    s, r = call("oci-p", "POST", "/api/cluster/transfer", {"to": "oci"}, token=tok_p)
    check(s == 200 and r.get("ok"), f"planned transfer back to oci: {r}")
    ok, t = wait_for(lambda: state("oci", tok)["role"] == "primary" and state("oci-p", tok_p)["role"] == "secondary", 20)
    check(bool(ok), f"oci primary again ({t:.1f}s)")

    print("… partition test: kill both secondaries, primary must step down (no majority)")
    kill("oci-p")
    kill("n150")
    ok, t = wait_for(lambda: state("oci", tok)["role"] == "secondary", 30)
    check(bool(ok), f"isolated primary stepped down after {t:.1f}s")
    print("ALL LIVE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.send_signal(signal.SIGKILL)
