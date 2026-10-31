import { useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Activity, Globe, TriangleAlert, HardDrive, Network, ChevronRight, OctagonAlert, Clock, ShieldAlert,
  Play, FileText, RotateCw, Square, Radar, Search, CircleCheck,
} from "lucide-react";
import { endpoints } from "../api/client";
import { useApps, useCluster, useEndpoints, useActions, useScans } from "../lib/queries";
import { useScope } from "../lib/scopeContext";
import { isProject, serverOfApp, healthOf, attentionItems, scopeGroup, SCOPE_GROUPS } from "../lib/derive";
import { bytes, bytesParts, relTime, clock, plural } from "../lib/format";
import { Page, Head, CardHead, Btn, Tag, Empty, Skel } from "../components/ui";
import FleetMap from "../components/FleetMap";
import { useActionRunner } from "../hooks/useActionRunner";

const SEV_ICON = { octagon: OctagonAlert, clock: Clock, shield: ShieldAlert, triangle: TriangleAlert };
const EV_ICON = { restart: RotateCw, stop: Square, down: Square, start: Play, up: Play, logs: FileText, status: Activity };

export default function Dashboard() {
  const { server } = useScope();
  const appsQ = useApps();
  const cluster = useCluster();
  const epsQ = useEndpoints();
  const acts = useActions(8);
  const scans = useScans(1);
  const nav = useNavigate();
  const runner = useActionRunner([["applications"]]);

  const allApps = useMemo(() => appsQ.data || [], [appsQ.data]);
  const apps = useMemo(() => allApps.filter((a) => !server || serverOfApp(a) === server), [allApps, server]);
  const projects = apps.filter(isProject);
  const eps = (epsQ.data || []).filter((e) => !server || e.server === server);
  const cs = cluster.data;

  const health = projects.map((a) => healthOf(a));
  const running = health.filter((h) => h.rt.state === "running" || h.rt.state === "partial").length;
  const stopped = health.filter((h) => h.rt.state === "stopped").length;
  const idle = health.filter((h) => h.rt.state === "idle").length;
  const publicEps = eps.filter((e) => e.scope === "public");
  const attn = attentionItems(apps, server ? null : cs);
  const crit = attn.filter((a) => a.sev === "danger").length;
  const storage = projects.reduce((s, a) => s + (a.total_size_bytes || 0), 0);
  const largest = projects.slice().sort((a, b) => (b.total_size_bytes || 0) - (a.total_size_bytes || 0))[0];
  const st = bytesParts(storage);

  // Fleet map nodes: cluster members, else the servers seen in the data.
  const ids = [];
  for (const m of cs?.members || []) ids.push(m.node_id);
  for (const a of allApps) { const s = serverOfApp(a); if (s && !ids.includes(s)) ids.push(s); }
  const lastScan = scans.data?.[0];
  const nodes = ids.map((id) => {
    const m = (cs?.members || []).find((x) => x.node_id === id);
    const mine = allApps.filter((a) => isProject(a) && serverOfApp(a) === id);
    const isPrim = m ? m.role === "primary" : id === (cs?.leader_id || ids[0]);
    const warn = m ? (!m.healthy || m.scan_overdue) : false;
    const when = m?.last_scan_at ? relTime(m.last_scan_at) : isPrim && lastScan ? relTime(lastScan.finished_at || lastScan.started_at) : null;
    return {
      id, primary: isPrim, warn,
      role: m ? (m.role === "primary" ? "Primary" : m.role === "unreachable" ? "Unreachable" : m.votes === 0 ? "Non-voting" : "Secondary") : (ids.length === 1 ? "Standalone" : "Server"),
      apps: mine.map((a) => { const h = healthOf(a); return h.rt.state === "stopped" && h.sev !== "danger" ? "idle" : h.sev; }),
      note: `${plural(mine.length, "app")}${when ? ` · ${isPrim ? "scanned" : "reported"} ${when}` : ""}`,
    };
  });
  const primaryId = nodes.find((n) => n.primary)?.id;
  const serverCount = server ? 1 : Math.max(ids.length, 1);

  const topStorage = projects.filter((a) => a.total_size_bytes > 0)
    .sort((a, b) => b.total_size_bytes - a.total_size_bytes).slice(0, 6);
  const maxStorage = topStorage[0]?.total_size_bytes || 1;

  const groups = SCOPE_GROUPS.map((g) => ({ ...g, n: eps.filter((e) => scopeGroup(e.scope).key === g.key).length })).filter((g) => g.n > 0);
  const ringColor = { internet: "#4dffad", tailnet: "#7db0ff", network: "#b7a0ff", local: "#7c9286" };

  const loading = appsQ.isLoading;

  return (
    <Page>
      <Head eyebrow="Dashboard" title="Fleet health"
        sub={loading ? "Loading" : `${plural(serverCount, "server")} · ${plural(projects.length, "application")} · last fleet scan ${lastScan ? relTime(lastScan.finished_at || lastScan.started_at) : "not run yet"}`} />

      <div className="kpis">
        <div className="card kpi">
          <div className="top-row"><span className="ic"><Activity size={18} /></span>Applications running</div>
          {loading ? <Skel h={34} w="50%" style={{ marginTop: 10 }} /> : <div className="v">{running}<small>/ {projects.length}</small></div>}
          {projects.length > 0 && (
            <div className="bar" aria-label={`${running} running, ${stopped} stopped, ${idle} with nothing running`}>
              {running > 0 && <i style={{ flex: running, background: "linear-gradient(90deg,var(--lo),var(--hi))" }} />}
              {stopped > 0 && <i style={{ flex: stopped, background: "var(--danger)" }} />}
              {idle > 0 && <i style={{ flex: idle, background: "var(--faint)" }} />}
            </div>
          )}
        </div>
        <div className="card kpi">
          <div className="top-row"><span className="ic"><Globe size={18} /></span>Internet-facing endpoints</div>
          {epsQ.isLoading ? <Skel h={34} w="50%" style={{ marginTop: 10 }} /> : <div className="v">{publicEps.length}<small>of {eps.length}</small></div>}
          <div className="s">{publicEps.length === 0 ? "Nothing public" : publicEps.every((e) => (e.url || "").startsWith("https")) ? "Every one behind TLS" : `${publicEps.filter((e) => !(e.url || "").startsWith("https")).length} without TLS`}</div>
        </div>
        <div className={`card kpi ${attn.length ? "hot warnk" : ""}`}>
          <div className="top-row"><span className="ic">{attn.length ? <TriangleAlert size={18} /> : <CircleCheck size={18} />}</span>Needs attention</div>
          <div className="v">{attn.length}</div>
          <div className="s">{attn.length ? `${crit} critical · ${attn.length - crit} warning${attn.length - crit === 1 ? "" : "s"}` : "Nothing outstanding"}</div>
        </div>
        <div className="card kpi">
          <div className="top-row"><span className="ic"><HardDrive size={18} /></span>Storage held by projects</div>
          <div className="v">{st.v}<small>{st.unit}</small></div>
          <div className="s">{largest?.total_size_bytes ? <>Largest: <span className="mono">{largest.name}</span></> : "No project data yet"}</div>
        </div>
      </div>

      <div className="dash-grid">
        <div className="card fleet">
          <CardHead title="Fleet" meta={nodes.length > 1 && primaryId ? <><Network size={14} />Secondaries report to <span className="mono">{primaryId}</span></> : null} />
          <div className="dotgrid" />
          {nodes.length ? <FleetMap nodes={nodes} /> : <Empty icon={Radar} title="No servers scanned yet">Run a scan to map this server.</Empty>}
          <div className="legend">
            <span><i style={{ background: "var(--hi)", boxShadow: "0 0 8px rgb(var(--glow)/.8)" }} />Running</span>
            <span><i style={{ background: "var(--warn)" }} />Warning</span>
            <span><i style={{ background: "var(--danger)" }} />Critical</span>
            <span><i style={{ background: "var(--faint)" }} />Stopped</span>
          </div>
        </div>

        <div className="card attn">
          <CardHead title="Needs attention" meta={attn.length ? "Most severe first" : null} />
          <div className="card-b">
            {attn.length === 0 && !loading && <Empty icon={CircleCheck} title="All clear">Every application is running and every certificate is valid.</Empty>}
            {attn.slice(0, 5).map((a) => {
              const I = SEV_ICON[a.icon] || TriangleAlert;
              return (
                <div className="item" key={a.key}>
                  <span className={`sev ${a.sev}`}><I size={18} /></span>
                  <div className="body">
                    <div className="t">{a.title}</div>
                    <div className="d">{a.detail}</div>
                    <div className="row2">
                      {a.tags.map((t) => <Tag key={t}>{t}</Tag>)}
                      <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
                        {a.kind === "app" && a.canStart && (
                          <Btn sm primary={a.sev === "danger"} icon={Play}
                            onClick={() => runner.run({ action: "start", label: "Start", target: a.app, fire: () => endpoints.fireApplicationAction(a.app, "start") })}>Start</Btn>
                        )}
                        {a.kind === "app" && <Btn sm icon={Search} onClick={() => nav(`/projects/${encodeURIComponent(a.app)}`)}>Open</Btn>}
                        {a.kind !== "app" && <Btn sm icon={Search} onClick={() => nav("/servers")}>Open</Btn>}
                      </span>
                    </div>
                  </div>
                </div>
              );
            })}
            {attn.length > 5 && <div style={{ paddingTop: 12 }}><Link className="link" style={{ marginLeft: 0 }} to="/projects?f=attention">{attn.length - 5} more<ChevronRight size={14} /></Link></div>}
          </div>
        </div>
      </div>

      <div className="dash-row3">
        <div className="card">
          <CardHead title="Storage by project"><Link className="link" to="/projects">All projects<ChevronRight size={14} /></Link></CardHead>
          <div className="card-b">
            {topStorage.length === 0 && <Empty icon={HardDrive} title="No project storage" />}
            {topStorage.map((a, i) => (
              <div className="hbar" key={a.application_id}>
                <span className="n">{a.name}</span>
                <span className="track"><i style={{ width: `${Math.max(2, (a.total_size_bytes / maxStorage) * 100)}%`, animationDelay: `${i * 70}ms` }} /></span>
                <span className="val">{bytes(a.total_size_bytes)}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="card">
          <CardHead title="Exposure"><Link className="link" to="/web">Open Web<ChevronRight size={14} /></Link></CardHead>
          <div className="card-b">
            {eps.length === 0 ? <Empty icon={Globe} title="No endpoints found" /> : (
              <div className="expo-ring">
                <Ring groups={groups} total={eps.length} colors={ringColor} />
                <div className="expo-list">
                  {groups.map((g) => <div key={g.key}><i style={{ background: ringColor[g.key] }} />{g.label}<b>{g.n}</b></div>)}
                </div>
              </div>
            )}
          </div>
        </div>
        <div className="card">
          <CardHead title="Recent actions"><Link className="link" to="/actions">Audit log<ChevronRight size={14} /></Link></CardHead>
          <div className="card-b tl">
            {(acts.data || []).length === 0 && <Empty icon={Activity} title="No actions yet">Starts, stops and restarts are recorded here.</Empty>}
            {(acts.data || []).slice(0, 5).map((e, i) => {
              const verb = String(e.action || "").replace(/^teardown:/, "");
              const I = EV_ICON[verb] || Activity;
              const ok = e.status === "success";
              return (
                <div className="ev" key={i}>
                  <span className={`m ${ok ? "" : "f"}`}><I size={13} /></span>
                  <span className="w"><b>{verb.charAt(0).toUpperCase() + verb.slice(1)}</b> <span className="mono">{e.asset_name || e.project}</span>{e.project && e.asset_name !== e.project ? ` · ${e.project}` : ""}</span>
                  <span className="when">{clock(e.timestamp)}</span>
                </div>
              );
            })}
          </div>
        </div>
      </div>
      {runner.ui}
    </Page>
  );
}

function Ring({ groups, total, colors }) {
  const r = 46;
  const c = 2 * Math.PI * r;
  const starts = groups.map((_, i) => groups.slice(0, i).reduce((s, g) => s + (g.n / total) * c, 0));
  return (
    <svg width="120" height="120" viewBox="0 0 120 120" role="img" aria-label={`${total} endpoints`}>
      <circle cx="60" cy="60" r={r} fill="none" stroke="rgb(255 255 255 / .05)" strokeWidth="12" />
      {groups.map((g, gi) => {
        const len = Math.max(0.01, (g.n / total) * c - (groups.length > 1 ? 4 : 0));
        const el = (
          <circle key={g.key} cx="60" cy="60" r={r} fill="none" stroke={colors[g.key]} strokeWidth="12" strokeLinecap="round"
            strokeDasharray={`${len} ${c - len}`} strokeDashoffset={-starts[gi]} transform="rotate(-90 60 60)"
            style={g.key === "internet" ? { filter: "drop-shadow(0 0 6px rgb(0 232 135 / .6))" } : undefined} />
        );
        return el;
      })}
      <text x="60" y="58" textAnchor="middle" fill="#f2fbf6" fontFamily="JetBrains Mono" fontSize="24" fontWeight="500">{total}</text>
      <text x="60" y="76" textAnchor="middle" fill="#96b0a2" fontSize="11" fontWeight="700">endpoints</text>
    </svg>
  );
}
