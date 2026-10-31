# InfraDocs V7 — API contract for the new screens

The frontend (Servers screen, Add-a-server drawer, first-run Setup) and the backend are built
against this file. Existing V6 data endpoints (`/api/applications`, `/api/assets`,
`/api/endpoints`, `/api/storage`, `/api/ports`, `/api/scans`, `/api/actions`, `/api/auth`) keep
their shapes; read them in `app/api/routers/` and `frontend/src/api/client.js`.

All times are ISO-8601 UTC strings. All routes below need `Authorization: Bearer <token>` unless
marked **public** or **fleet** (fleet = server-to-server, `X-Fleet-Secret` header).

## Cluster

### `GET /api/cluster/state`

```json
{
  "node_id": "oci",
  "role": "primary",                 // primary | secondary | candidate | standalone
  "term": 14,
  "leader_id": "oci",                // null while there is no primary
  "leader_address": "http://oci.tail6a3622.ts.net:8081",
  "mode": "automatic",               // automatic | manual | standalone
  "mode_reason": "3 voting members", // few words, shown under the mode
  "voting_members": 3,
  "quorum": 2,                       // floor(voting/2) + 1
  "reachable_voters": 3,
  "has_quorum": true,
  "primary_since": "2026-10-28T09:12:00Z",
  "settings": { "heartbeat_interval_s": 2, "election_timeout_s": 10 },
  "members": [
    {
      "node_id": "oci", "address": "http://oci.tail6a3622.ts.net:8081",
      "priority": 1,                 // 1 = most preferred; 0 = never primary
      "votes": 1,                    // 1 voting, 0 non-voting
      "role": "primary",             // primary | secondary | candidate | unreachable
      "healthy": true,
      "is_self": true,
      "last_heartbeat_age_s": 0.0,   // null if never heard
      "last_scan_at": "2026-10-31T17:58:00Z",
      "scan_overdue": false,
      "version": "7.0.0",
      "host": { "os": "Ubuntu 24.04", "cpu_count": 4, "cpu_pct": 18, "mem_pct": 46, "disk_pct": 62 },
      "heartbeats": [true, true, false, true]   // last 16, oldest first
    }
  ],
  "elections": [
    { "term": 14, "winner": "oci", "votes_for": 3, "voters": 3,
      "reason": "OCI-P stepped down", "at": "2026-10-28T09:12:00Z" }
  ]
}
```

### `POST /api/cluster/join-code`
Body `{ "votes": 0 | 1, "priority": 2 }` (priority optional; next free one by default).
Primary only (409 otherwise).
```json
{ "code": "ifd7_J4Q9MZK27TXR", "expires_at": "…",
  "command": "curl -fsSL http://oci…:8081/join.sh | bash -s -- ifd7_J4Q9MZK27TXR",
  "voting_after": 4, "mode_after": "manual", "mode_after_reason": "4 voting members" }
```

### `GET /api/cluster/join-code/{code}/status`
`{ "state": "waiting" | "joined" | "expired" | "failed", "node_id": "n150", "reason": null,
   "directions": { "new_to_primary": true, "primary_to_new": true } }` — the drawer polls this.

### `POST /api/cluster/step-down` — primary only. `{ "ok": true, "term": 15 }`
### `POST /api/cluster/transfer` — `{ "to": "oci-p" }`; primary hands over to a caught-up member.
### `POST /api/cluster/takeover` — manual mode only: this node becomes primary in a new term.
Body `{ "confirm": "<this node_id>" }`. Refused (409, `detail` says why) in automatic mode.
### `POST /api/cluster/members/{node_id}` — `{ "priority": 3, "votes": 0 }` (primary only).
### `DELETE /api/cluster/members/{node_id}?confirm=<node_id>` — primary only.

Errors: `{ "detail": "few words" }` with 400/403/409.

## Setup (first run)

### `GET /api/setup/status` — **public**
```json
{ "setup_complete": false, "needs_setup_code": true,
  "detected": { "hostname": "oci", "os": "Ubuntu 24.04", "cpu_count": 4, "mem_gb": 24,
    "tailscale_ip": "100.107.140.36", "tailscale_dns": "oci.tail6a3622.ts.net",
    "public_ip": "80.225.195.84", "docker_version": "27.3.1", "port": 8081,
    "projects_folder": "/home/msinha/projects", "projects_found": 31,
    "checks": [ { "id": "port", "ok": true, "label": "Port 8081 is free" } ] },
  "suggested_address": "http://oci.tail6a3622.ts.net:8081" }
```

### `POST /api/setup/check-address` — **public while setup is open**
`{ "address": "http://…" }` → `{ "reachable": true, "reason": null }`

### `POST /api/setup/complete` — **public while setup is open**, needs the setup code the
installer printed.
```json
{ "setup_code": "483-219", "admin": { "username": "manish", "password": "…" },
  "server_name": "oci", "projects_folder": "/home/msinha/projects",
  "access": { "mode": "tailscale" | "domain" | "cloudflare", "address": "http://…" } }
```
→ `{ "ok": true, "token": "<session token>", "scan_started": true }`. 400 with `detail` on a bad
field, 403 on a wrong setup code.
