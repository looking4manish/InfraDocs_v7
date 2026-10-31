# InfraDocs — LLM Context

**Read this first.** This is the single onboarding doc for an AI assistant working on this
repo. It gives you the whole mental model in one page, then **breadcrumbs** telling you which
files/docs to open for the specific task you've been given. (For a human "what is this + how
do I deploy it" walkthrough, see [`README.md`](README.md).)

> V7 (Nov 2026): pure-black interface (design of record `docs/V7_DESIGN_mockup.html`,
> API contract `docs/V7_API_CONTRACT.md`), a replica set with quorum elections
> (`docs/CLUSTER.md`), and a two-step install (`install.sh`, then join codes).

---

## 1. What this is

An **infrastructure documentation cockpit**. It scans one or more servers → correlates →
exposes → visualizes → **acts on** everything running on them.

Pipeline: **scanners → correlator → MongoDB → FastAPI → React frontend.**


## 2. Deployment

One way: the Docker stack in `deploy/docker/` (MongoDB + API + Caddy), installed by
`install.sh` → `deploy/docker/deploy.sh`. The first server is set up in the browser with the
installer's setup code; later servers join with a join code from the Servers page. The V6
native systemd + nginx deployment (`infra.ocialwaysfree.site`) is retired.

## 3. Capabilities (what exists today)

- **Auth:** bcrypt + DB-backed session tokens (`Authorization: Bearer`). The administrator
  is created in the setup wizard (no default password). `verify_auth` also accepts HTTP Basic
  (DB user or the installer's service credential), the fleet secret (`X-Fleet-Auth`), and
  `INFRADOCS_AUTH_DISABLED=1` for local dev.
- **First-run setup:** setup code from the installer → administrator → server name →
  access (Tailscale / domain / Cloudflare Tunnel, address verified) → first scan.
- **Ten scanners:** docker, compose, systemd, port, storage, nginx, **caddy**, **cloudflared**,
  **certs**, **cron**. nginx/caddy/cloudflared are pluggable **exposure detectors** (report
  mechanism + public hostname).
- **Correlator:** joins flat assets into per-application docs with `links[]` evidence.
- **AI layer (3 tiers):** recognize (deterministic) → LLM enrich (label unknowns) → fleet
  insights. Any OpenAI-compatible endpoint; disabled cleanly when unset; only non-sensitive
  metadata sent.
- **Web tab:** `/api/endpoints` — every reachable UI/service across the fleet, deduped by
  host/port, with browsable URLs + access scope (internet/localhost/tailnet).
- **Replica set:** quorum elections (terms, one vote per term, pre-vote, check-quorum),
  automatic failover only with 3/5/7 voting members, manual takeover otherwise, non-voting
  members, join codes. Secondaries push scans to the elected primary. See `docs/CLUSTER.md`.
- **Actions:** guarded dispatcher (start/stop/restart/logs/enable/disable/…) + audit log;
  `infradocs-v6-*` units self-protected (409).
- **Interface:** pure-black theme, Manrope + JetBrains Mono; Dashboard, Projects,
  Application, Servers, Web, Assets, Setup.

## 4. Data model — 30 seconds

Every asset lands in **exactly one bucket**: a `~/projects/<name>` project bucket, or the
single `System` bucket. Enforced by `correlator.py`, checked by `audit_ownership()`. Rule:
**only `~/projects/<X>` paths name a project; never infer from a string prefix** (a V5
regression tests guard). Mongo collections (db `infradocs`): `assets`, `applications`,
`ports`, `storage`, `actions_log`, `scan_logs`, `settings` (wizard + AI config), `ai_labels`,
federation join/server records.

## 5. Repo map

```
app/
├── core/      config_loader, db_manager, project_detector, recognize, hostpath, logger
├── scanners/  docker, compose, systemd, port, storage, nginx, caddy, cloudflared, certs, cron
├── correlator.py · actions.py · ai.py · auth.py · federation.py
├── blast_radius.py · teardown.py · ports_registry.py · storage_registry.py
├── agent.py   CLI: python -m app.agent scan
└── api/       main.py · dependencies.py · routers/{auth,setup,federation,endpoints,ai,
                 assets,projects,applications,ports,storage,scans,actions,health}.py
frontend/      React 19 + Vite + Tailwind + React Query (src/App.jsx gate, pages/, components/)
deploy/        docker/ (deploy.sh, remove.sh, compose, Caddyfile, Dockerfiles) · native *.service/*.conf
docs/          ARCHITECTURE · DEPLOY · DEVELOPMENT · V7_PLAN · REGISTRY (root) · phases/
tests/         pytest suite (auth, setup, federation, scanners, correlator, actions, …)
```

---

## 6. 🧭 Breadcrumbs — where to look, by task

| If your task is about… | Open these |
|---|---|
| **Deploying / running it** | [`README.md`](README.md) (quick start) → [`docs/DEPLOY.md`](docs/DEPLOY.md) (docker product + native), `deploy/docker/{deploy.sh,remove.sh,docker-compose.yml}` |
| **Local dev / tests / conventions** | [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md), `tests/` |
| **How it's structured / why** | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| **Auth / login / sessions** | `app/auth.py`, `app/api/dependencies.py` (`verify_auth`), `app/api/routers/auth.py`, `frontend/src/App.jsx` |
| **Setup / join** | `deploy/docker/deploy.sh`, `app/api/routers/setup.py`, the Setup page in `frontend/src/` |
| **A scanner / new asset type** | `app/scanners/<name>.py`, `app/scanners/registry.py`, `config.yml` (`enabled_scanners`); exposure detectors = nginx/caddy/cloudflared |
| **Correlation / application docs / links** | `app/correlator.py`, `app/core/project_detector.py`; tests `test_v7_phase1_correlator.py` |
| **AI labeling / insights** | `app/ai.py`, `app/core/recognize.py`, `app/api/routers/ai.py`; model notes in §7 |
| **Web tab / endpoints list** | `app/api/routers/endpoints.py`, `frontend/src/pages/LensHome.jsx` (Web lens) |
| **Replica set / failover** | `docs/CLUSTER.md`, `app/election.py`, `app/election_manager.py`, `app/cluster_runtime.py`, `app/api/routers/cluster.py`; tests `tests/test_election.py`, `scripts/cluster_live_test.py` |
| **Scan push / ingest** | `app/federation.py`, `app/api/routers/federation.py`, `push_to_leader` in `app/api/routers/scans.py` |
| **Actions / card+action registry** | `app/actions.py`, [`REGISTRY_SPEC.md`](REGISTRY_SPEC.md), `frontend/src/registry/cards.js`, `components/ActionBar.jsx` |
| **Reading host configs in a container** | `app/core/hostpath.py` (the `/host` mount) — never read `/etc/...` directly |
| **Teardown / blast radius** | `app/teardown.py`, `app/blast_radius.py` |
| **The roadmap / what's next** | [`docs/V7_PLAN.md`](docs/V7_PLAN.md) (§0 reality update), and §8 below |
| **History of a past session** | `git log` — the per-session `*HANDOFF*.md` files and `docs/phases/PHASE_*_STATUS.md` build journal were pruned once their work landed on `main` |

---

## 7. Run / develop (cheat sheet)

```bash
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
cp .env.example .env                                  # set INFRADOCS_MONGO_URI
python -m app.agent scan --summary                    # scan + populate
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8005   # API (≠ prod 8004)
cd frontend && npm install && npm run dev             # → :5173 (proxies /api → :8004)
python -m pytest tests/ -v                            # tests
npx vite build --outDir /tmp/ifd-check                # compile-check WITHOUT clobbering live dist
```

**AI model findings** (against the user's Ollama at `https://ai.sinhahub.in/v1`):
- ✅ `gpt-oss:120b-cloud` (~8s) and `qwen3-coder:480b-cloud` (~4s) — fast, clean JSON.
  **Recommended default = `gpt-oss:120b-cloud`.**
- ❌ `ministral-3:8b` too weak (ignores schema); `kimi-k2.6:cloud` /
  `mistral-large-3:675b-cloud` time out (>150s).
- Two fixes that made it work: strip ```` ```json ```` fences, and send a real `User-Agent`
  (the gateway 403s the default `python-urllib` UA).

## 8. Open items / next

- **First deployment:** OCI first, then OCI-P and N150 join as voting members (P-S-S).
- **Command dispatch** (primary → secondary actions) — not started; the fleet proxy covers
  live actions on reachable members.
- **Administrator accounts** are copied to a member when it joins; later password changes
  are not yet propagated to existing members.
- **Scanner wave 1 remainder** (per V7_PLAN): `db.py`, firewall exposure cross-check,
  `backup.py`, substrate IMDS/SDK, `hardware.py` (N150).

## 9. Key reference

| Item | Value |
|---|---|
| Hosts in scope | OCI (primary), OCI-P, N150 |
| SSH | OCI-P (`msinha@100.70.18.9`) |
| Default login | `admin / Changeme001` (forced change); config-cred Basic fallback `admin:Changeme001` |
| Ollama | `https://ai.sinhahub.in/v1`, model `gpt-oss:120b-cloud` |
| Docker API/web ports | API `:8090`, Caddy web `:8081` (host network) |
| Branch | `feature/neon-depth-theme` (kept in sync with `origin/main`) |
| Commit footer | `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` |
