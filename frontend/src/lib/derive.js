// Derivations from the raw API documents. Every number on screen comes through here.
import { ageSeconds } from "./format";

export const SYSTEM = "System";

// application_id is `${server_id}:app:${name}`.
export function serverOfApp(a) {
  if (a?.server_id) return a.server_id;
  const id = a?.application_id || "";
  const i = id.indexOf(":app:");
  return i > 0 ? id.slice(0, i) : null;
}

export function isProject(a) {
  return a && a.type !== "system" && a.name !== SYSTEM;
}

export function runtimeOf(a) {
  const cd = a?.containers_detail || [];
  const running = cd.filter((c) => c.running).length;
  const units = (a?.systemd_units || []).length;
  const ports = (a?.listening_ports || []).length;
  const hasRuntime = cd.length > 0 || units > 0 || ports > 0;
  let state = "running";
  if (!hasRuntime) state = "idle";
  else if (cd.length > 0 && running === 0 && units === 0) state = "stopped";
  else if (cd.length > 0 && running < cd.length) state = "partial";
  return { state, running, containers: cd.length, units, ports, hasRuntime };
}

export function minCertDays(a) {
  const days = (a?.certificates_detail || [])
    .map((c) => c.days_until_expiry)
    .filter((d) => typeof d === "number");
  return days.length ? Math.min(...days) : null;
}

// Severity + the one-line reason shown on the card. ok | warn | danger | idle
export function healthOf(a) {
  const rt = runtimeOf(a);
  const certDays = minCertDays(a);
  const exited = a?.hygiene?.exited_restart_always || [];
  if (rt.state === "stopped" && a?.internet_exposed)
    return { sev: "danger", why: "Public hostname answers, no container running", rt };
  if (certDays != null && certDays < 0)
    return { sev: "danger", why: "Certificate expired", rt };
  if (certDays != null && certDays <= 3)
    return { sev: "danger", why: `Certificate expires in ${certDays} days`, rt };
  if (rt.state === "stopped") return { sev: "warn", why: "No container running", rt };
  if (exited.length) return { sev: "warn", why: `${exited[0]} exited despite its restart policy`, rt };
  if (certDays != null && certDays <= 14)
    return { sev: "warn", why: `Certificate expires in ${certDays} days`, rt };
  if (rt.state === "partial") return { sev: "warn", why: `${rt.containers - rt.running} of ${rt.containers} containers stopped`, rt };
  if (rt.state === "idle") return { sev: "idle", why: null, rt };
  return { sev: "ok", why: null, rt };
}

const SEV_RANK = { danger: 0, warn: 1, ok: 2, idle: 3 };
export function sevRank(s) { return SEV_RANK[s] ?? 9; }

export function primaryUrl(a) {
  const ex = (a?.exposure || [])[0];
  if (ex?.hostname) return ex.hostname;
  const u = (a?.urls || [])[0];
  return u ? u.replace(/^[a-z]+:\/\//i, "").replace(/\/$/, "") : null;
}

export function viaLabel(v) {
  return {
    nginx: "nginx", caddy: "Caddy", cloudflare_tunnel: "Cloudflare Tunnel", cloudflared: "Cloudflare Tunnel",
    "reverse-proxy backend": "Reverse-proxy backend", "direct port": "Direct port",
  }[v] || v || "—";
}

// Web endpoint scope -> the group it is shown in.
export const SCOPE_GROUPS = [
  { key: "internet", label: "Internet", scopes: ["public"], chip: "ok" },
  { key: "tailnet", label: "Tailnet", scopes: ["tailnet"], chip: "info" },
  { key: "network", label: "Private network", scopes: ["all-interfaces", "private-lan", "private"], chip: "info" },
  { key: "local", label: "This server only", scopes: ["localhost", "host", "unknown"], chip: "idle" },
];
export function scopeGroup(scope) {
  return SCOPE_GROUPS.find((g) => g.scopes.includes(scope)) || SCOPE_GROUPS[3];
}

// Dashboard "Needs attention": apps + cluster members, most severe first.
export function attentionItems(apps, cluster) {
  const out = [];
  for (const a of apps.filter(isProject)) {
    const h = healthOf(a);
    if (h.sev !== "danger" && h.sev !== "warn") continue;
    const srv = serverOfApp(a);
    const url = primaryUrl(a);
    let title = `${a.name}: ${h.why.charAt(0).toLowerCase()}${h.why.slice(1)}`;
    let detail = url ? `${url} on ${srv || "this server"}.` : `On ${srv || "this server"}.`;
    if (h.why.startsWith("Public hostname")) {
      title = `${a.name} is down but still public`;
      const via = (a.exposure || [])[0]?.via;
      detail = `${url} answers${via ? ` through ${viaLabel(via)}` : ""} on ${srv || "this server"}, and no container is running.`;
    } else if (h.why.startsWith("Certificate")) {
      title = h.why;
      const c = (a.certificates_detail || []).slice().sort((x, y) => (x.days_until_expiry ?? 1e9) - (y.days_until_expiry ?? 1e9))[0];
      detail = `${(c?.domains || [c?.name])[0] || url || a.name} on ${srv || "this server"}${c?.issuer ? `, issued by ${shortIssuer(c.issuer)}` : ""}.`;
    }
    out.push({
      key: `app:${a.application_id || a.name}`, sev: h.sev, kind: "app", app: a.name,
      icon: h.sev === "danger" ? "octagon" : h.why.startsWith("Certificate") ? "shield" : "triangle",
      title, detail, tags: [srv, ...(a.exposure || []).slice(0, 1).map((e) => viaLabel(e.via))].filter(Boolean),
      canStart: h.rt.state === "stopped",
    });
  }
  for (const m of cluster?.members || []) {
    if (m.is_self) continue;
    if (!m.healthy || m.role === "unreachable") {
      out.push({
        key: `node:${m.node_id}`, sev: "danger", kind: "node", node: m.node_id, icon: "octagon",
        title: `${m.node_id} is unreachable`,
        detail: m.last_heartbeat_age_s != null ? `No heartbeat for ${Math.round(m.last_heartbeat_age_s)} s.` : "No heartbeat received.",
        tags: [m.node_id],
      });
    } else if (m.scan_overdue) {
      out.push({
        key: `scan:${m.node_id}`, sev: "warn", kind: "node", node: m.node_id, icon: "clock",
        title: `${m.node_id} last reported ${m.last_scan_at ? agoWords(m.last_scan_at) : "never"}`,
        detail: "Its scan reports are overdue; heartbeats still arrive.",
        tags: [m.node_id],
      });
    }
  }
  if (cluster && cluster.mode === "automatic" && cluster.has_quorum === false) {
    out.push({ key: "quorum", sev: "danger", kind: "cluster", icon: "octagon", title: "Quorum lost",
      detail: `${cluster.reachable_voters} of ${cluster.voting_members} voting members reachable; ${cluster.quorum} needed.`, tags: [] });
  }
  return out.sort((x, y) => sevRank(x.sev) - sevRank(y.sev));
}

function agoWords(v) {
  const s = ageSeconds(v);
  if (!Number.isFinite(s)) return "never";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  return `${Math.round(m / 60)} h ago`;
}

export function shortIssuer(iss) {
  if (!iss) return "";
  const s = String(iss);
  if (/let'?s encrypt/i.test(s)) return "Let’s Encrypt";
  const cn = /CN=([^,]+)/.exec(s);
  const o = /O=([^,]+)/.exec(s);
  return (o?.[1] || cn?.[1] || s).trim();
}

export function linkViaLabel(v) {
  if (!v) return null;
  if (v.startsWith("upstream_port")) return "Proxy upstream port";
  return {
    project_dir: "Project folder", project_tag: "Project folder", compose_dir: "Compose folder",
    compose_label: "Compose labels", port_mapping: "Port mapping", image_tag: "Image in use",
    nginx_domain: "Certificate domain", unit_path: "Service unit path", host_port_index: "Port owner",
    process_cwd: "Process folder", caddy: "Caddy upstream", caddy_site: "Caddy upstream",
    cloudflare_tunnel: "Tunnel ingress", cloudflared: "Tunnel ingress", nginx: "nginx upstream",
  }[v] || v.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

export function linkViaIcon(v) {
  if (!v) return "link";
  if (v.startsWith("upstream_port") || v === "port_mapping" || v === "host_port_index") return "plug";
  if (v.startsWith("project") || v === "process_cwd" || v === "compose_dir") return "folder";
  if (v.startsWith("compose") || v === "image_tag") return "layers";
  if (v === "nginx_domain") return "shield";
  if (v === "unit_path") return "cog";
  return "link";
}
