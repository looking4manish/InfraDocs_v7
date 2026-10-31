# Deploying InfraDocs

## First server

```bash
git clone https://github.com/looking4manish/InfraDocs_v7.git ~/infradocs
bash ~/infradocs/install.sh --name oci --projects ~/projects
```

`deploy/docker/deploy.sh` (which `install.sh` runs) does, with a tick or a named failure per
step and a timestamped log in `deploy/docker/install.log`:

1. Checks the server: Linux, curl, Docker and the compose plugin (offers to install Docker
   when run in a terminal), at least 3 GB free disk.
2. Writes `deploy/docker/.env` (mode 600) on the first run: server name, projects folder,
   free web/API/MongoDB ports (from 8081 / 8090 / 27018), Tailscale address and public IP if
   present, a random **setup code**, and a random service password. An existing `.env` is kept.
3. Builds and starts `mongo` + `api` + `web`, then waits for the API and the web server.
4. Finishes setup **in the terminal** (no browser needed): asks for the administrator's
   username and password, offers the addresses it detected (Tailscale name and address,
   public IP, or one you type), checks that the chosen address really reaches this server,
   then creates the administrator, forms a one-member replica set and starts the first scan.

Unattended: `--admin-user manish --admin-password-file ~/pw.txt --address http://oci.example:8081`.
Browser instead: `--browser` prints the address and a setup code for the wizard (also the
fallback when no terminal is attached).

## More servers

Servers → Add a server → choose **voting** or **non-voting** → copy the command and run it on
the new server:

```bash
curl -fsSL http://<primary>:8081/join.sh | bash -s -- <join-code> --name n150
```

`join.sh` (served by the primary) clones the repository into `~/infradocs` and runs
`deploy.sh --join <code> --primary <url>`. The installer starts the stack, then asks the
primary to add this server; the primary connects back to verify the address. A private
repository needs `INFRADOCS_REPO=git@github.com:looking4manish/InfraDocs_v7.git` in front of
`bash`. Voting rules and failover: [`CLUSTER.md`](CLUSTER.md).

## What the stack looks like

- `deploy/docker/docker-compose.yml`: `mongo` + `api` + `web`.
- The **API container** runs with `network_mode: host` and `pid: host`, and mounts the host
  filesystem read-only at `/host` (`INFRADOCS_HOST_ROOT=/host`), so the scanners see real
  containers, processes, listening ports and config files. Read host configs through
  `app/core/hostpath.py`, never `/etc/...` directly.
- The **web (Caddy) container** runs on the host network and proxies `/api/*` and `/join.sh`
  to the API at `localhost:<API_PORT>`. Host networking avoids the docker-bridge → host hop
  that the OCI firewall drops.
- Members reach each other on their web address (default port 8081). Over Tailscale no
  firewall change is needed; over public IPs open the web port between the servers only.
- Optional `cloudflared` and `tailscale` sidecars (compose profiles) for public exposure.

## Update and remove

- `bash ~/infradocs/update.sh` pulls, rebuilds and restarts in place; the MongoDB volume
  (replica-set state, history) is kept. Update secondaries one at a time, then step the
  primary down from the Servers page and update it last.
- `bash ~/infradocs/uninstall.sh` removes the stack (asks first).

## Legacy

`deploy/*.service`, `deploy/*.conf` and `deploy/install_*.sh` are the V6 native systemd +
nginx deployment. They are not used by V7.
