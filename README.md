# InfraDocs

**An infrastructure documentation cockpit — and now a deployable, multi-server, AI-enhanced product.**

InfraDocs scans one or more servers, correlates everything it finds (containers, compose
files, systemd units, listening ports, nginx/Caddy/Cloudflare-tunnel exposures, storage,
certs, cron) into per-application documents, and renders a React cockpit that answers
"what is running on my fleet, where is it exposed, and what is it?" — without manually
re-joining `docker ps`, `systemctl status`, `nginx -T`, `ss`, and `du`.

> V7: the pure-black interface (design of record `docs/V7_DESIGN_mockup.html`), a replica set
> with quorum elections ([`docs/CLUSTER.md`](docs/CLUSTER.md)), and a two-step install. If
> you're an AI assistant picking this up, start at [`CONTEXT.md`](CONTEXT.md).

## What it does

- **Discovers** infrastructure via ten scanners: docker, compose, systemd, listening
  ports, storage, nginx, **Caddy**, **Cloudflare tunnel (cloudflared)**, **certs**, and
  **cron** — exposure detectors are pluggable per category.
- **Correlates** the flat scan output into per-application documents with `links[]`
  evidence (the heart of the app — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)).
- **Exposes** the data through FastAPI (`/api/applications`, `/api/assets`,
  `/api/projects`, `/api/ports`, `/api/storage`, `/api/endpoints`, `/api/scans`,
  `/api/actions`, plus `/api/auth`, `/api/setup`, `/api/federation`, `/api/ai`).
- **Labels** unknown services with an optional **AI layer** (deterministic recognition →
  LLM enrichment → fleet insights) against any OpenAI-compatible endpoint.
- **Surfaces** every reachable UI/service across the fleet in one **Web tab** — clickable
  URL, which server, which service, access scope (internet / localhost / tailnet).
- **Acts on** containers / systemd units / nginx via `POST /api/assets/{id}/action` and
  `POST /api/applications/{name}/action`, with a full audit log at `/api/actions/`.
- **Visualizes** it through a React + Vite SPA in the "Neon-Depth" theme (MongoDB-green):
  lens-based home (Dashboard / Projects / Servers / Web / Resources / Assets), application
  topology lanes, registries, and an audit log.

## Install

**First server** (becomes the primary):

```bash
git clone https://github.com/looking4manish/InfraDocs_v7.git ~/infradocs
bash ~/infradocs/install.sh            # optional: --name oci --projects ~/projects
```

The installer checks the server, picks free ports, starts the stack (MongoDB + API + Caddy)
and finishes setup in the terminal: administrator, the address other servers use (detected
Tailscale/public addresses offered, reachability checked), first scan. No browser needed.
Add `--browser` to finish in the setup wizard instead (address + setup code printed).

**More servers**: Servers → Add a server. Choose voting or non-voting, then run the printed
command on the new server:

```bash
curl -fsSL http://<primary>:8081/join.sh | bash -s -- <join-code> --name n150
```

Three voting members turn on automatic failover. See [`docs/CLUSTER.md`](docs/CLUSTER.md).

**Update** a server in place: `bash ~/infradocs/update.sh` (secondaries first, then step the
primary down and update it). **Remove**: `bash ~/infradocs/uninstall.sh`.

### Local development

```bash
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
export INFRADOCS_MONGO_URI=mongodb://127.0.0.1:27017/ INFRADOCS_SETUP_CODE=TEST-CODE
python -m uvicorn app.api.main:app --port 8090
cd frontend && npm install && npm run dev
```

## Auth

The administrator is created in the setup wizard (no default password). Sessions are opaque
tokens stored in MongoDB (`Authorization: Bearer`). Members talk to each other with a shared
fleet secret (`X-Fleet-Auth`), handed to each new member when it joins.

## Testing

```bash
python -m pytest tests/ -v
```

Suites cover scanner contracts, correlation, ports/storage registries, the ownership
audit, the action dispatcher, auth, setup, the election rules and a partition simulator,
scan ingest, exposure detectors, cert/cron scanners, teardown/blast-radius, and FastAPI
endpoints with a real MongoDB. Integration tests skip when `INFRADOCS_MONGO_URI` is unset.
`python scripts/cluster_live_test.py` runs three real members and a failover.

## Project layout

```
.
├── app/
│   ├── core/             # config, db_manager, project_detector, recognize, hostpath, logger
│   ├── scanners/         # docker, compose, systemd, port, storage, nginx, caddy,
│   │                     #   cloudflared, certs, cron (+ base, registry)
│   ├── correlator.py     # joins flat assets into application docs (+ links[] evidence)
│   ├── actions.py        # action dispatcher + allow-list
│   ├── ai.py             # optional LLM layer (label_service + fleet_insights)
│   ├── auth.py           # bcrypt + DB sessions
│   ├── election.py       # replica-set election rules (pure)
│   ├── election_manager.py / cluster_runtime.py   # one member: timers, RPCs, storage
│   ├── federation.py     # scan push to the primary
│   ├── blast_radius.py / teardown.py        # guarded project teardown
│   ├── agent.py          # CLI: python -m app.agent scan
│   └── api/              # FastAPI app + routers (auth, setup, federation, endpoints, ai, …)
├── frontend/             # React 19 + Vite + React Query (V7 pure-black theme)
├── deploy/
│   ├── docker/           # docker-compose product: deploy.sh, remove.sh, Caddyfile, Dockerfiles
├── docs/                 # CLUSTER, ARCHITECTURE, DEPLOY, DEVELOPMENT, V7 design + API contract
├── tests/                # pytest suite (unit + integration)
├── CONTEXT.md            # single LLM-onboarding doc (mental model + task breadcrumbs)
├── config.yml            # non-secret app config
└── .env.example          # copy to .env; never commit .env
```

## Status

V7: new interface, quorum-based replica set with automatic failover for an odd number of
voting members, simpler install and join. Next: run the first server on OCI, then join OCI-P
and N150 to form the P-S-S set.

## License

Personal project. No formal license declared yet.
