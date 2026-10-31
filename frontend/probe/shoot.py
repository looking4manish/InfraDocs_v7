"""Screenshot probe: serve the production build with `vite preview`, answer every /api/* call
from fixtures.py, and capture each route at 1440 and 1280 wide plus the interactive states.

    cd frontend && npm run build && python3 probe/shoot.py [--setup-only]

Prints layout problems found in each capture (clipped text, wrapped controls, page wider than
the viewport, console errors) so they can be fixed before a human looks."""
import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fixtures as F  # noqa: E402

FRONT = pathlib.Path(__file__).resolve().parent.parent
OUT = FRONT / "probe" / "shots"
OUT.mkdir(exist_ok=True)
PORT = 4199
BASE = f"http://127.0.0.1:{PORT}"


def respond(route, setup_open):
    url = route.request.url
    path = url.split("://", 1)[1].split("/", 1)[1]
    path = "/" + path
    method = route.request.method
    body = None
    if path.startswith("/api/setup/status"):
        body = F.SETUP_OPEN if setup_open else F.SETUP_DONE
    elif path.startswith("/api/setup/check-address"):
        body = {"reachable": True, "reason": None}
    elif path.startswith("/api/setup/complete"):
        body = {"ok": True, "token": "t", "scan_started": True}
    elif path.startswith("/api/auth/me"):
        body = {"username": "manish", "must_change_password": False}
    elif path.startswith("/api/applications/list"):
        body = F.APPLICATIONS
    elif re.match(r"^/api/applications/[^/?]+$", path.split("?")[0]):
        name = path.split("?")[0].rsplit("/", 1)[1]
        body = next((a for a in F.APPS if a["name"] == name), None)
        if body is None:
            return route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": "application not found"}))
    elif path.startswith("/api/endpoints"):
        body = F.ENDPOINTS
    elif path.startswith("/api/cluster/state"):
        body = F.CLUSTER
    elif path.startswith("/api/cluster/join-code") and method == "POST":
        body = F.JOIN
    elif path.startswith("/api/cluster/join-code"):
        body = F.JOIN_STATUS
    elif path.startswith("/api/actions/allowed"):
        body = F.ALLOWED
    elif path.startswith("/api/actions"):
        body = F.ACTIONS
    elif path.startswith("/api/scans"):
        body = F.SCANS
    elif path.startswith("/api/assets/categories"):
        body = F.CATEGORIES
    elif path.startswith("/api/assets"):
        m = re.search(r"category=([a-z_]+)", path)
        rows = F.ASSETS_BY_CAT.get(m.group(1), []) if m else F.CONTAINERS
        body = {"count": len(rows), "assets": rows}
    else:
        body = {}
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body, default=str))


def handler_for(setup_open):
    # Playwright passes (route, request) to a two-argument handler, so close over the flag.
    return lambda route: respond(route, setup_open)


CHECK = """
() => {
  const bad = [];
  for (const el of document.querySelectorAll('main *, header *, aside *, .modal *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || el.closest('svg')) continue;
    if ((cs.textOverflow === 'ellipsis') && el.scrollWidth > el.clientWidth + 1)
      bad.push('clipped: ' + (el.className || el.tagName) + ' :: ' + el.textContent.trim().slice(0, 50));
  }
  if (document.documentElement.scrollWidth > innerWidth + 1) bad.push('page wider than viewport: ' + document.documentElement.scrollWidth);
  for (const el of document.querySelectorAll('.btn:not(.icon), .chip, .tag, .seg button, .nav a, .scope, .kbd')) {
    const r = el.getBoundingClientRect();
    if (r.height > 40 && r.width > 0) bad.push('wrapped: ' + el.textContent.trim().slice(0, 40));
  }
  return bad;
}
"""


def main():
    setup_only = "--setup-only" in sys.argv
    srv = subprocess.Popen(["npx", "vite", "preview", "--port", str(PORT), "--strictPort", "--host", "127.0.0.1"],
                           cwd=FRONT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(BASE, timeout=1)
                break
            except Exception:  # noqa: BLE001 — server still starting
                time.sleep(0.5)
        with sync_playwright() as p:
            b = p.chromium.launch()
            report = {}
            for W in (1440, 1280):
                for setup_open in ([True] if setup_only else [False, True]):
                    ctx = b.new_context(viewport={"width": W, "height": 900})
                    ctx.add_init_script("localStorage.setItem('ifd_token','fixture')")
                    pg = ctx.new_page()
                    errs = []
                    pg.on("pageerror", lambda e, errs=errs: errs.append(str(e)))
                    pg.on("console", lambda m, errs=errs: m.type == "error" and errs.append(m.text))
                    pg.route("**/api/**", handler_for(setup_open))

                    def shot(name, full=True):
                        pg.wait_for_timeout(900)
                        pg.screenshot(path=str(OUT / f"{W}_{name}.png"), full_page=full)
                        report[f"{W}_{name}"] = pg.evaluate(CHECK)

                    if setup_open:
                        pg.goto(BASE + "/")
                        pg.wait_for_selector(".wiz")
                        shot("setup-1")
                        pg.fill("#sc", "483-219"); pg.fill("#un", "manish"); pg.fill("#pw", "correct-horse-battery"); pg.fill("#pw2", "correct-horse-battery")
                        pg.click("button[type=submit]"); shot("setup-2")
                        pg.click("button[type=submit]"); pg.wait_for_timeout(900); shot("setup-3")
                        pg.click("button[type=submit]"); shot("setup-4")
                    else:
                        for route, name in [("/", "dashboard"), ("/projects", "projects"), ("/servers", "servers"),
                                            ("/web", "web"), ("/assets", "assets"), ("/actions", "audit")]:
                            pg.goto(BASE + route)
                            pg.wait_for_selector(".page")
                            shot(name)
                        pg.goto(BASE + "/projects")
                        pg.click("a.pcard:has-text('authentik')")
                        pg.wait_for_selector(".path")
                        shot("app-authentik")
                        pg.goto(BASE + "/projects/nextcloud"); pg.wait_for_selector(".app-head"); shot("app-nextcloud")
                        pg.goto(BASE + "/projects/backup-scripts"); pg.wait_for_selector(".app-head"); shot("app-backup")
                        pg.goto(BASE + "/servers"); pg.wait_for_selector(".mgrid")
                        pg.click(".head .btn.primary"); pg.wait_for_timeout(500); shot("add-server", full=False)
                        pg.click(".drawer .btn.primary"); pg.wait_for_timeout(700); shot("add-server-code", full=False)
                        pg.goto(BASE + "/assets"); pg.wait_for_selector(".rail")
                        pg.click(".rail button:has-text('Certificates')"); shot("assets-certs")
                        pg.click(".rail button:has-text('Mounts')"); shot("assets-mounts")
                        pg.goto(BASE + "/"); pg.wait_for_selector(".page"); pg.click(".kbd"); pg.keyboard.type("auth"); shot("palette", full=False)
                    report[f"{W}_errors_{'setup' if setup_open else 'app'}"] = errs
                    ctx.close()
            ctx = b.new_context(viewport={"width": 1440, "height": 900})
            pg = ctx.new_page()
            pg.route("**/api/**", handler_for(False))
            pg.goto(BASE + "/"); pg.wait_for_selector(".auth"); pg.wait_for_timeout(600)
            pg.screenshot(path=str(OUT / "1440_login.png"))
            ctx.close()
            b.close()
        for k, v in report.items():
            if v:
                print(k, v)
        print("shots in", OUT)
    finally:
        srv.terminate()


if __name__ == "__main__":
    main()
