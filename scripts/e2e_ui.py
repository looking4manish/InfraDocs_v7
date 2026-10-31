"""End-to-end UI check against REAL members: three API processes + three web servers (the
production routing: static UI, /api and /join.sh proxied). Drives the setup wizard and the
Add-a-server drawer in a browser, joins two servers with the installer's API call, kills the
primary, and screenshots each state into scripts/e2e_shots/.

    INFRADOCS_MONGO_URI=mongodb://127.0.0.1:27017/ python scripts/e2e_ui.py
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "scripts" / "e2e_shots"
SHOTS.mkdir(exist_ok=True)
PY = os.environ.get("IFD_PY", sys.executable)
CODE = "E2E7-TEST"
NODES = {"oci": (18401, 18411), "oci-p": (18402, 18412), "n150": (18403, 18413)}
procs = {}


def web(n):
    return f"http://127.0.0.1:{NODES[n][1]}"


def start(n):
    api, wp = NODES[n]
    env = {**os.environ, "INFRADOCS_SERVER_ID": n, "INFRADOCS_DB": f"ifd_e2e_{n.replace('-', '_')}",
           "INFRADOCS_SETUP_CODE": CODE, "INFRADOCS_WEB_PORT": str(wp), "INFRADOCS_API_PASSWORD": "svc-0123456789ab",
           "INFRADOCS_PROJECTS_ROOT": "/tmp"}
    log = open(f"/tmp/ifd_e2e_{n}.log", "a")
    procs[n + ":api"] = subprocess.Popen([PY, "-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1", "--port", str(api),
                                          "--log-level", "warning"], cwd=ROOT, env=env, stdout=log, stderr=log)
    if n + ":web" not in procs:
        procs[n + ":web"] = subprocess.Popen([PY, "scripts/dev_web.py", "--port", str(wp), "--api", f"http://127.0.0.1:{api}",
                                              "--dist", "frontend/dist"], cwd=ROOT, stdout=log, stderr=log)
    for _ in range(80):
        try:
            urllib.request.urlopen(web(n) + "/api/cluster/rpc/ping", timeout=2)
            return
        except Exception:
            time.sleep(0.5)
    raise SystemExit(f"{n} did not start")


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def shot(page, name):
    page.wait_for_timeout(900)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)
    print("shot", name)


def main():
    from pymongo import MongoClient
    mc = MongoClient(os.environ["INFRADOCS_MONGO_URI"])
    for n in NODES:
        mc.drop_database(f"ifd_e2e_{n.replace('-', '_')}")
        start(n)
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))

        # ── setup wizard on oci
        page.goto(web("oci"))
        page.wait_for_selector("#sc")
        shot(page, "01_setup_admin")
        page.fill("#sc", CODE.lower())
        page.fill("#un", "manish")
        page.fill("#pw", "a-strong-password")
        page.fill("#pw2", "a-strong-password")
        page.click("button[type=submit]")
        page.wait_for_selector("#sn")
        shot(page, "02_setup_server")
        page.click("button[type=submit]")
        page.wait_for_selector("#ad")
        page.fill("#ad", web("oci"))
        page.wait_for_timeout(2500)
        shot(page, "03_setup_access")
        page.click("button[type=submit]")
        page.wait_for_selector("text=Review and run the first scan")
        shot(page, "04_setup_review")
        page.click("button[type=submit]")
        page.wait_for_selector("text=Fleet health", timeout=30000)
        shot(page, "05_dashboard_after_setup")

        # ── add two voting servers through the drawer
        for n in ("oci-p", "n150"):
            page.goto(web("oci") + "/servers")
            page.wait_for_selector("text=Replica set")
            page.get_by_role("button", name=re.compile("Add a server")).first.click()
            page.wait_for_selector("text=Choose how it votes")
            page.get_by_role("button", name=re.compile("^Voting member")).click()
            page.get_by_role("button", name=re.compile("Create join command")).click()
            page.wait_for_selector(".code")
            cmd = page.inner_text(".code")
            code = re.search(r"ifd7_[A-Z0-9]+", cmd).group(0)
            shot(page, f"06_drawer_code_{n}")
            r = post(f"http://127.0.0.1:{NODES[n][0]}/api/setup/join",
                     {"setup_code": CODE, "primary_url": web("oci"), "code": code, "address": web(n), "server_name": n})
            print("joined", n, r)
            page.wait_for_selector("text=Joined", timeout=20000) if page.locator("text=Joined").count() == 0 else None
            shot(page, f"07_drawer_joined_{n}")
            page.keyboard.press("Escape")
        page.goto(web("oci") + "/servers")
        page.wait_for_timeout(5000)
        shot(page, "08_servers_three_members")
        page.goto(web("oci") + "/assets")
        shot(page, "09_assets")
        page.goto(web("oci") + "/projects")
        shot(page, "10_projects")
        page.goto(web("oci") + "/web")
        shot(page, "11_web")

        # ── kill the primary; log in on oci-p, which should win the election
        procs["oci:api"].send_signal(signal.SIGKILL)
        t0 = time.time()
        for _ in range(60):
            st = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{NODES['oci-p'][0]}/api/cluster/rpc/ping").read())
            if st.get("role") == "primary":
                break
            time.sleep(1)
        print(f"oci-p primary after {time.time() - t0:.1f}s")
        page2 = ctx.new_page()
        page2.goto(web("oci-p"))
        page2.wait_for_selector("#u")
        page2.fill("#u", "manish")
        page2.fill("#p", "a-strong-password")
        page2.keyboard.press("Enter")
        page2.wait_for_selector("text=Fleet health", timeout=15000)
        page2.goto(web("oci-p") + "/servers")
        page2.wait_for_timeout(5000)
        shot(page2, "12_servers_after_failover")

        # ── restart oci: its UI should send the browser to the primary
        start("oci")
        time.sleep(6)
        page3 = ctx.new_page()
        page3.goto(web("oci"))
        page3.wait_for_selector("text=This server is a secondary", timeout=10000)
        shot(page3, "13_old_primary_redirect_card")
        page3.wait_for_url(web("oci-p") + "/**", timeout=10000)
        print("old primary sent the browser to", page3.url)
        b.close()
    print("console/page errors:", errors[:10])


if __name__ == "__main__":
    try:
        main()
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.send_signal(signal.SIGKILL)
