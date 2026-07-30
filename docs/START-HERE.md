# InfraDocs — Start Here

> **Operator runbook:** current state, architecture, and reinstall steps.
> Home-lab documentation cockpit · Hosts: **oci** (primary) · **oci-p** · **n150**.

> **Current state:** InfraDocs has been **cleanly removed** from all three servers
> (containers, volumes, images, and networks). The nginx vhost, TLS cert, and DNS record
> for `infra.mdbdemo.in` are **untouched** — so reinstalling on oci brings the public URL
> straight back with no DNS/cert changes.

---

## 1 · How the public URL actually works

`infra.mdbdemo.in` is served by **plain nginx on oci behind Cloudflare's proxy** — *not* a
Cloudflare Tunnel and *not* the compose `cloudflare`/`tailscale` profiles (those are empty on
every host). The full path:

```
infra.mdbdemo.in
  → Cloudflare (proxied / orange-cloud: 104.21.85.122, 172.67.205.180)
  → oci public IP :443
  → nginx  /etc/nginx/sites-enabled/infra.mdbdemo.in  (Let's Encrypt cert)
  → upstream infradocs_primary = 127.0.0.1:8081
  → oci's infradocs-web container
```

The upstream is defined in `/etc/nginx/conf.d/infradocs-primary.conf`:

```nginx
upstream infradocs_primary { server 127.0.0.1:8081; }
```

---

## 2 · Server roles & DNS records

| Host      | SERVER_ID | Web port      | Role                        | Public ingress                        |
|-----------|-----------|---------------|-----------------------------|---------------------------------------|
| **oci**   | OCI       | `:8081`       | Primary (aggregates the UI) | nginx → `infra.mdbdemo.in`            |
| **oci-p** | OCI-P     | `:8081`       | Secondary                   | none                                  |
| **n150**  | N150      | `:8083` caddy | Secondary                   | none (its cloudflared serves other apps) |

| DNS record                   | Status   | Notes                                                                                          |
|------------------------------|----------|------------------------------------------------------------------------------------------------|
| `infra.mdbdemo.in`           | **LIVE** | Cloudflare-proxied → oci nginx. Returns **502** while InfraDocs is down; back to 200 on reinstall. Record + cert + vhost all intact. |
| `infra.ocialwaysfree.site`   | UNUSED   | nginx vhost exists but is **not enabled**; DNS does not resolve. Points at an old *native* (non-Docker) deploy. Ignore it. |

> **Why 502 and not "tunnel error 1033"?** Because there is no tunnel. nginx stays up and just
> has no origin on `:8081` until the stack is running again. Nothing to delete or repoint — the
> URL self-heals on reinstall.

---

## 3 · Reinstall — do oci first

The repo already lives at `~/infradocs` on each host, so reinstall = re-run the deployer.
Start with **oci**, the primary that `infra.mdbdemo.in` fronts:

```bash
# On your Mac
ssh oci
cd ~/infradocs
git pull                       # optional: pull the hardening branch if merged
bash deploy/docker/deploy.sh
```

When `deploy.sh` asks how to expose the browser wizard, choose **3) Skip** — nginx already
fronts oci. Once `:8081` is healthy, `infra.mdbdemo.in` returns 200 automatically.

### Then the secondaries

```bash
ssh oci-p 'cd ~/infradocs && bash deploy/docker/deploy.sh'

# n150: direct ssh times out on :22 — hop through oci
ssh oci 'ssh n150 "cd ~/infradocs && bash deploy/docker/deploy.sh"'
```

For the secondaries, either **Skip** exposure too, or pick **1) Tailscale** if you want a
private URL to reach them directly.

### First login

`admin` / `Changeme001` → change the password immediately → complete the wizard.

---

## 4 · Two heads-ups before you run it

> **① The saved `.env` is reused by default.** Each host's `deploy/docker/.env` was kept during
> removal, so `deploy.sh` reuses it. oci's `.env` was *missing* `MONGO_PASSWORD` — the script
> auto-backfills it. Because removal wiped the old `mongo_data` volume (`down -v`), this is a clean
> first-run and auth won't lock you out; the catalog repopulates on the first scan.
>
> Want a **fully fresh** config instead? Delete the env first, then deploy:
> `rm ~/infradocs/deploy/docker/.env` — it'll re-prompt for SERVER_ID and projects-root.

> **② `remove.sh` can fail silently.** It swallows errors (`2>/dev/null || true`). On oci the
> bundled `down` couldn't parse the compose file (missing required `MONGO_PASSWORD`), so it looked
> done but wasn't. If you ever remove again and containers linger, tear down explicitly with a
> throwaway value (the value is irrelevant for a down):
>
> ```bash
> cd ~/infradocs/deploy/docker && \
>   MONGO_PASSWORD=teardown docker compose --env-file .env down -v --remove-orphans
> ```

---

## 5 · Handy verification & management commands

```bash
# Is the stack up on a host?
docker ps --filter name=infradocs --format '{{.Names}}  {{.Status}}'

# Follow logs / check health (from the deploy dir)
cd ~/infradocs/deploy/docker
docker compose --env-file .env ps
docker compose --env-file .env logs -f api

# Is the public URL healthy? (run from your Mac)
curl -s -o /dev/null -w 'infra.mdbdemo.in -> HTTP %{http_code}\n' https://infra.mdbdemo.in/

# nginx / cert on oci (should already exist — do not recreate)
ls -l /etc/nginx/sites-enabled/infra.mdbdemo.in
sudo nginx -t && sudo systemctl reload nginx
```

---

## 6 · Full decommission (only if NOT reinstalling)

Skip this if you're reinstalling — leave nginx/cert/DNS as-is. To permanently retire the public
URL on oci:

```bash
sudo rm /etc/nginx/sites-enabled/infra.mdbdemo.in && sudo nginx -s reload
sudo certbot delete --cert-name infra.mdbdemo.in
# then delete the infra.mdbdemo.in record in the Cloudflare dashboard
```

---

*InfraDocs V6 · pipeline: scanners → correlator → MongoDB → FastAPI → React · Two-tier RBAC
(viewer/operator), self-protected units prefixed `infradocs`. This is a home-lab deployment,
not production.*

> **Note:** the rendered PDF (`InfraDocs-Start-Here.pdf`) in this directory is generated from this
> markdown file. Edit the markdown, then re-render to keep them in sync.
