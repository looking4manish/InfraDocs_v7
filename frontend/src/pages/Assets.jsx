import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Container, Cog, Timer, Clock, Package, HardDrive, Network, Layers, Plug, Globe, Cloud, ShieldCheck, ShieldAlert,
  Database, Search, Boxes, FolderKanban,
} from "lucide-react";
import { endpoints } from "../api/client";
import { useCluster, useApps, serverList } from "../lib/queries";
import { useScope } from "../lib/scopeContext";
import { bytes, isoDay, relTime, shortDateTime } from "../lib/format";
import { shortIssuer, SYSTEM } from "../lib/derive";
import { Page, Head, Seg, SearchBox, Empty, Skel, Tag, Chip, Card, CardHead } from "../components/ui";
import ActionBar from "../components/ActionBar";

const CATS = [
  ["Runtime", [["docker_container", "Containers", Container], ["systemd_service", "Services", Cog], ["systemd_timer", "Timers", Timer], ["cron_job", "Cron jobs", Clock]]],
  ["Docker", [["docker_image", "Images", Package], ["docker_volume", "Volumes", HardDrive], ["docker_network", "Networks", Network], ["docker_compose", "Compose stacks", Layers]]],
  ["Network", [["network_port", "Ports", Plug], ["nginx_server_block", "nginx sites", Globe], ["caddy_site", "Caddy sites", Globe], ["cloudflare_tunnel", "Tunnels", Cloud], ["tls_certificate", "Certificates", ShieldCheck]]],
  ["Storage", [["storage_mount", "Mounts", Database]]],
];
const CAT_INFO = Object.fromEntries(CATS.flatMap(([, items]) => items.map(([k, l, I]) => [k, { label: l, icon: I }])));

// Per category: the detail column, the state chip, and the column headings.
function describe(a) {
  const m = a.metadata || {};
  switch (a.category) {
    case "docker_container":
      return { detail: m.image, mono: true, state: m.running ? ["ok", "Running"] : ["danger", "Exited"] };
    case "systemd_service":
    case "systemd_timer":
      return { detail: m.description, state: m.active_state === "active" ? ["ok", m.sub_state || "Active"] : m.active_state === "failed" ? ["danger", "Failed"] : ["idle", m.active_state || a.status || "—"] };
    case "cron_job":
      return { detail: <><span className="mono">{m.schedule}</span> {m.command}</>, state: ["ok", m.user || "Active"] };
    case "docker_image":
      return { detail: `${(m.tags || [])[0] || "untagged"} · ${m.size_mb != null ? `${Math.round(m.size_mb)} MB` : bytes(m.size_bytes)}`, state: m.is_dangling ? ["warn", "Dangling"] : m.in_use ? ["ok", "In use"] : ["idle", "Unused"] };
    case "docker_volume":
      return { detail: bytes(m.size_bytes || 0), mono: true, state: m.in_use || a.status === "in_use" ? ["ok", "In use"] : ["warn", "Unused"] };
    case "docker_network":
      return { detail: [m.driver, m.subnet].filter(Boolean).join(" · "), state: a.status === "active" ? ["ok", "Active"] : ["idle", "Idle"] };
    case "docker_compose":
      return { detail: `${m.services_count ?? (m.services || []).length} services`, state: ["idle", "Defined"] };
    case "network_port":
      return { detail: <><span className="mono">{m.local_address}</span>{m.process ? ` · ${m.process}` : ""}</>, state: ["ok", (m.protocol || "tcp").toUpperCase()] };
    case "nginx_server_block":
    case "caddy_site":
      return { detail: m.upstream || (m.upstream_port ? `→ :${m.upstream_port}` : m.root || "static"), mono: true, state: m.internet_exposed ? ["ok", "Public"] : ["idle", "Private"] };
    case "cloudflare_tunnel":
      return { detail: m.upstream_port ? `→ :${m.upstream_port}` : m.service, mono: true, state: m.running === false ? ["danger", "Stopped"] : ["ok", "Public"] };
    case "tls_certificate": {
      const d = m.days_until_expiry;
      const tone = d == null ? "idle" : d < 0 ? "danger" : d <= 14 ? "warn" : "ok";
      return { detail: shortIssuer(m.issuer) || "—", state: [tone, d == null ? "Unknown" : d < 0 ? "Expired" : `${d} days`], warn: tone !== "ok" && tone !== "idle" };
    }
    case "storage_mount": {
      const p = m.usage_pct ?? m.usage_percent;
      const used = m.used_bytes != null && m.total_bytes ? `${bytes(m.used_bytes)} of ${bytes(m.total_bytes)}` : null;
      return { detail: used || m.source, mono: true, state: p == null ? ["idle", "Mounted"] : [p >= 90 ? "danger" : p >= 80 ? "warn" : "ok", `${Math.round(p)}% used`] };
    }
    default:
      return { detail: a.status, state: ["idle", a.status || "—"] };
  }
}
const COL_HEADS = {
  tls_certificate: ["Issuer", "Expires in"], storage_mount: ["Space", "Usage"], network_port: ["Address", "Protocol"],
  docker_container: ["Image", "State"], docker_image: ["Tag", "State"], cron_job: ["Schedule", "User"],
};

export default function Assets() {
  const [params, setParams] = useSearchParams();
  const { server, setServer } = useScope();
  const [owner, setOwner] = useState(params.get("owner") === "system" ? "system" : "any");
  const [q, setQ] = useState("");
  const [selId, setSelId] = useState(null);
  const cats = useQuery({ queryKey: ["asset-categories"], queryFn: () => endpoints.assetCategories().then((r) => r.data.categories || []) });
  const counts = Object.fromEntries((cats.data || []).map((c) => [c.category, c.count]));
  const firstCat = (cats.data || []).slice().sort((a, b) => b.count - a.count)[0]?.category;
  const cat = params.get("c") || firstCat;
  const setCat = (c) => { setSelId(null); setParams({ c, ...(owner === "system" ? { owner: "system" } : {}) }, { replace: true }); };
  const list = useQuery({
    queryKey: ["assets", cat],
    queryFn: () => endpoints.listAssets({ category: cat, limit: 2000 }).then((r) => r.data.assets || []),
    enabled: Boolean(cat),
  });
  const cluster = useCluster();
  const apps = useApps();
  const servers = serverList(cluster.data, apps.data);

  const ql = q.trim().toLowerCase();
  const rows = useMemo(() => (list.data || [])
    .filter((a) => !server || a.server_id === server)
    .filter((a) => owner === "any" || (owner === "system" ? (a.project || SYSTEM) === SYSTEM : (a.project || SYSTEM) !== SYSTEM))
    .filter((a) => !ql || [a.name, a.project, JSON.stringify(a.metadata || {})].some((x) => (x || "").toLowerCase().includes(ql)))
    .map((a) => ({ a, d: describe(a) }))
    .sort((x, y) => (y.d.warn ? 1 : 0) - (x.d.warn ? 1 : 0) || (x.a.name || "").localeCompare(y.a.name || "")),
  [list.data, server, owner, ql]);
  const sel = rows.find((r) => r.a.asset_id === selId) || rows[0];
  const selKey = sel?.a.asset_id;
  const extra = (cats.data || []).filter((c) => !CAT_INFO[c.category]);
  const heads = COL_HEADS[cat] || ["Detail", "State"];
  const info = CAT_INFO[cat] || { label: cat, icon: Boxes };

  return (
    <Page>
      <Head eyebrow="Inventory" title="Assets" sub={`Everything the scanners found${servers.length > 1 ? `, across ${servers.length} servers` : ""}`} />
      <div className="toolbar">
        {servers.length > 1 && (
          <Seg value={server || ""} onChange={(v) => setServer(v || null)} options={[{ value: "", label: "All servers" }, ...servers.map((s) => ({ value: s, label: s }))]} />
        )}
        <Seg value={owner} onChange={setOwner} options={[{ value: "any", label: "Any owner" }, { value: "project", label: "In a project" }, { value: "system", label: "System" }]} />
        <SearchBox icon={Search} value={q} onChange={setQ} placeholder="Filter by name, project or detail" />
      </div>
      <div className="alayout">
        <nav className="card rail" aria-label="Categories">
          {cats.isLoading && <div style={{ padding: 10 }}><Skel h={300} /></div>}
          {CATS.map(([g, items]) => {
            const present = items.filter(([k]) => counts[k]);
            if (!present.length) return null;
            return (
              <div key={g}>
                <div className="grp">{g}</div>
                {present.map(([k, l, I]) => (
                  <button key={k} type="button" className={k === cat ? "on" : ""} onClick={() => setCat(k)}>
                    <I size={16} />{l}<span className="c">{counts[k]}</span>
                  </button>
                ))}
              </div>
            );
          })}
          {extra.length > 0 && (
            <div>
              <div className="grp">Other</div>
              {extra.map((c) => (
                <button key={c.category} type="button" className={c.category === cat ? "on" : ""} onClick={() => setCat(c.category)}>
                  <Boxes size={16} />{c.category.replace(/_/g, " ")}<span className="c">{c.count}</span>
                </button>
              ))}
            </div>
          )}
        </nav>
        <div className="asplit">
          <div className="card atable">
            <CardHead title={info.label} meta={<span className="mono">{rows.length}</span>} />
            <div style={{ height: 12 }} />
            {!cats.isLoading && !cat && <Empty icon={Boxes} title="No assets yet">Run a scan to fill the inventory.</Empty>}
            {cat && (
              <div className="tr th"><span /><span>Name</span><span>Project</span><span>Server</span><span>{heads[0]}</span><span>{heads[1]}</span></div>
            )}
            {list.isLoading && <div style={{ padding: 20 }}><Skel h={200} /></div>}
            {!list.isLoading && cat && rows.length === 0 && ((list.data || []).length
              ? <Empty icon={Search} title="Nothing matches">Change the filters or the search text.</Empty>
              : <Empty icon={info.icon} title={`No ${info.label.toLowerCase()} found`}>The latest scan recorded none.</Empty>)}
            {rows.map(({ a, d }) => {
              const I = info.icon;
              return (
                <div key={a.asset_id} className={`tr${a.asset_id === selKey ? " sel" : ""}`} onClick={() => setSelId(a.asset_id)}
                  role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === "Enter") setSelId(a.asset_id); }}>
                  <span className="ki" style={d.warn ? { color: "var(--warn)", background: "rgb(255 203 107 / .1)" } : undefined}>
                    {a.category === "tls_certificate" && d.warn ? <ShieldAlert size={15} /> : <I size={15} />}
                  </span>
                  <span className="nm" title={a.name}>{a.name}</span>
                  <span className={`pj${(a.project || SYSTEM) === SYSTEM ? " sys" : ""}`}>{a.project || SYSTEM}</span>
                  <span>{a.server_id && <Tag>{a.server_id}</Tag>}</span>
                  <span className="dt" title={typeof d.detail === "string" ? d.detail : undefined}>{d.detail || "—"}</span>
                  <span><Chip tone={d.state[0]}>{d.state[1]}</Chip></span>
                </div>
              );
            })}
          </div>
          {sel && <Inspector a={sel.a} d={sel.d} info={info} />}
        </div>
      </div>
    </Page>
  );
}

const SKIP = new Set(["labels", "env_keys", "environment_keys", "raw", "mounts", "ports", "repo_digests", "connected_containers",
  "healthcheck_test", "days_until_expiry", "expiring_soon", "expired", "not_after", "usage_pct", "usage_percent", "running"]);
const DATE_KEY = /(_at|^not_before|^created|^started)$/;
const BYTES_KEY = /(bytes|^size)$/;
const MONO_KEY = /path|file|address|image|_id$|dir|target|source|upstream|domains|subject|command|exec|unit|schedule|tags|port/;

function factValue(k, v) {
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (/issuer/.test(k)) return shortIssuer(v);
  if (DATE_KEY.test(k) && typeof v === "string") return shortDateTime(v);
  if (BYTES_KEY.test(k) && typeof v === "number") return bytes(v);
  return String(v);
}

function Inspector({ a, d, info }) {
  const m = a.metadata || {};
  const I = a.category === "tls_certificate" && d.warn ? ShieldAlert : info.icon;
  const tone = d.state[0];
  const iconStyle = tone === "warn" ? { background: "rgb(255 203 107 / .12)", color: "var(--warn)" }
    : tone === "danger" ? { background: "rgb(251 136 154 / .12)", color: "var(--danger)" }
      : { background: "rgb(var(--glow) / .1)", color: "var(--hi)" };
  const facts = Object.entries(m)
    .filter(([k, v]) => !SKIP.has(k) && v !== null && v !== "" && typeof v !== "object")
    .slice(0, 12);
  const lists = Object.entries(m).filter(([k, v]) => !SKIP.has(k) && Array.isArray(v) && v.length && v.every((x) => typeof x !== "object")).slice(0, 3);
  const days = m.days_until_expiry;
  return (
    <Card className="insp">
      <div className="h">
        <span className="ic2" style={iconStyle}><I size={22} /></span>
        <div style={{ minWidth: 0 }}>
          <div className="nm">{a.name}</div>
          <div className="cat">{info.label.replace(/s$/, "")}{a.project ? ` · ${a.project}` : ""}{a.server_id ? ` · ${a.server_id}` : ""}</div>
        </div>
      </div>
      {a.category === "tls_certificate" && days != null && (
        <div className="expiry">
          <div className="etop"><span>Expires</span><span className="mono">{isoDay(m.not_after)}</span></div>
          <div className="big" style={{ color: tone === "ok" ? "var(--hi)" : tone === "danger" ? "var(--danger)" : "var(--warn)" }}>{days < 0 ? "Expired" : days}<small>{days < 0 ? "" : "days left"}</small></div>
          <div className="track"><i style={{ width: `${Math.max(2, Math.min(100, 100 - (days / 90) * 100))}%` }} /></div>
        </div>
      )}
      {a.category === "storage_mount" && (m.usage_pct ?? m.usage_percent) != null && (
        <div className="gauge">
          <div className="g">Used<span className="bar"><i className={(m.usage_pct ?? m.usage_percent) > 80 ? "w" : ""} style={{ width: `${m.usage_pct ?? m.usage_percent}%` }} /></span><span className="val">{Math.round(m.usage_pct ?? m.usage_percent)}%</span></div>
        </div>
      )}
      <div className="kv">
        {facts.map(([k, v]) => (
          <span key={k} style={{ display: "contents" }}>
            <span className="k">{k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase())}</span>
            <span className={`v${typeof v === "number" || MONO_KEY.test(k) ? " mono" : ""}`}>{factValue(k, v)}</span>
          </span>
        ))}
        {lists.map(([k, v]) => (
          <span key={k} style={{ display: "contents" }}>
            <span className="k">{k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase())}</span>
            <span className="v mono">{v.join(", ")}</span>
          </span>
        ))}
        {a.discovered_at && <><span className="k">Last seen</span><span className="v">{relTime(a.updated_at || a.discovered_at)}</span></>}
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <ActionBar entity={{ category: a.category, asset_id: a.asset_id, name: a.name }} inline={3} />
        {a.project && a.project !== SYSTEM && <Link className="btn sm" to={`/projects/${encodeURIComponent(a.project)}`}><FolderKanban size={14} />Open {a.project}</Link>}
      </div>
    </Card>
  );
}
