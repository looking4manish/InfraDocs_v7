import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ChevronRight, Server, Clock, FolderKanban, Globe, ShieldCheck, Plug, Container, Database, HardDrive, Cog,
  FileText, RotateCw, ExternalLink, Layers, Link2, Shield, Trash2, Waypoints, Activity, Play, Square, OctagonAlert,
  TriangleAlert,
} from "lucide-react";
import { endpoints } from "../api/client";
import { useEndpoints } from "../lib/queries";
import { serverOfApp, healthOf, primaryUrl, viaLabel, minCertDays, linkViaLabel, linkViaIcon, shortIssuer } from "../lib/derive";
import { bytes, isoDay, toDate, clock, errDetail } from "../lib/format";
import { Page, Card, CardHead, Btn, Chip, Empty, Skel } from "../components/ui";
import ActionBar from "../components/ActionBar";
import { useActionRunner } from "../hooks/useActionRunner";
import Teardown from "../components/Teardown";

const STATE_CHIP = {
  running: ["ok", "Running"], partial: ["warn", "Partly running"], stopped: ["danger", "Stopped"], idle: ["idle", "Nothing running"],
};
const LINK_ICON = { plug: Plug, folder: FolderKanban, layers: Layers, shield: Shield, cog: Cog, link: Link2 };
const EV_ICON = { restart: RotateCw, stop: Square, start: Play, logs: FileText };

function uptime(iso) {
  const d = toDate(iso);
  if (!d) return null;
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 3600) return `Up ${Math.max(1, Math.round(s / 60))} min`;
  if (s < 86400) return `Up ${Math.round(s / 3600)} h`;
  const days = Math.round(s / 86400);
  return `Up ${days} day${days === 1 ? "" : "s"}`;
}

export default function Application() {
  const { name } = useParams();
  const [tearing, setTearing] = useState(false);
  const runner = useActionRunner([["applications"], ["application", name]]);
  const q = useQuery({
    queryKey: ["application", name],
    queryFn: () => endpoints.getApplication(name).then((r) => r.data),
    refetchInterval: 30000,
  });
  const eps = useEndpoints();
  const acts = useQuery({
    queryKey: ["actions-list", 200],
    queryFn: () => endpoints.listActions({ limit: 200 }).then((r) => r.data.actions || []),
  });

  if (q.isLoading) {
    return <Page><Skel h={22} w={200} /><div style={{ height: 20 }} /><Skel h={56} w={420} /><div style={{ height: 24 }} /><Skel h={170} /></Page>;
  }
  if (q.error) {
    return (
      <Page>
        <div className="crumbs"><Link to="/projects">Projects</Link><ChevronRight size={13} /><span>{name}</span></div>
        <div className="card" style={{ marginTop: 16 }}>
          <Empty icon={FolderKanban} title="Project not found" action={<Link className="btn sm" to="/projects">All projects</Link>}>
            {q.error?.response?.status === 404 ? `No project named ${name} in the latest scan.` : errDetail(q.error)}
          </Empty>
        </div>
      </Page>
    );
  }

  const a = q.data;
  const h = healthOf(a);
  const srv = serverOfApp(a);
  const url = primaryUrl(a);
  const ex = (a.exposure || [])[0];
  const ng = (a.nginx_detail || []).find((n) => n.upstream_port) || (a.nginx_detail || [])[0];
  const certDays = minCertDays(a);
  const cert = (a.certificates_detail || []).slice().sort((x, y) => (x.days_until_expiry ?? 1e9) - (y.days_until_expiry ?? 1e9))[0];
  const port = ex?.upstream_port || ng?.upstream_port || a.port_mappings?.[0]?.host_port || a.listening_ports?.[0];
  const portEp = port ? (eps.data || []).find((e) => e.server === srv && String(e.host || "").endsWith(`:${port}`)) : null;
  const scopeText = { localhost: "Bound to localhost only", tailnet: "Reachable on the tailnet", "all-interfaces": "Listening on all interfaces", "private-lan": "Private network", public: "Public" }[portEp?.scope];
  const started = (a.containers_detail || []).filter((c) => c.running && c.started_at).map((c) => c.started_at).sort()[0];
  const containers = a.containers_detail || [];
  const units = a.systemd_units || [];
  const volumes = a.volumes || [];
  const chip = STATE_CHIP[h.rt.state];
  const vias = [...new Set((a.links || []).map((l) => l.via).filter(Boolean))];
  const history = (acts.data || []).filter((e) => e.project === a.name).slice(0, 6);
  const canAct = containers.length > 0 || units.length > 0;

  // Request path hops — only the ones the scan found.
  const hops = [];
  if (url) hops.push({ key: "host", edge: true, icon: Globe, k: "Hostname", v: url, s: a.internet_exposed ? `Internet${ex?.via ? ` · ${viaLabel(ex.via)}` : ""}` : "Private" });
  if (ex || ng) hops.push({
    key: "edge", icon: ShieldCheck, k: "Edge",
    v: ex?.via === "cloudflare_tunnel" ? "Cloudflare Tunnel" : `${viaLabel(ex?.via || "nginx")}${(ng?.listen_ports || []).length ? ` :${ng.listen_ports[0]}` : " :443"}`,
    s: certDays != null ? (certDays < 0 ? "Certificate expired" : `TLS valid · ${certDays} days left`) : ng?.has_ssl ? "TLS" : "No certificate seen",
  });
  if (port) hops.push({ key: "port", icon: Plug, k: "Port", v: portEp?.host || `:${port}`, s: scopeText || "Listening" });
  if (containers.length) hops.push({ key: "c", icon: Container, k: "Containers", stack: containers.slice(0, 4).map((c) => ({ t: c.compose_service || c.name, orb: c.running ? "ok" : "danger" })), more: containers.length - 4 });
  else if (units.length) hops.push({ key: "u", icon: Cog, k: "Services", stack: units.slice(0, 4).map((u) => ({ t: u, orb: "ok" })), more: units.length - 4 });
  const data = [...volumes.map((v) => ({ t: `${v.name} · ${bytes(v.size_bytes || 0)}` })),
    ...(a.project_dir ? [{ t: `folder · ${bytes(a.project_dir_size_bytes || 0)}` }] : [])];
  if (data.length) hops.push({ key: "d", icon: Database, k: "Data", stack: data.slice(0, 3).map((d) => ({ ...d, icon: HardDrive })), more: data.length - 3 });

  return (
    <Page>
      <div className="crumbs"><Link to="/projects">Projects</Link><ChevronRight size={13} /><span>{a.name}</span></div>
      <div className="app-head">
        <div className="app-icon"><FolderKanban size={26} /></div>
        <div style={{ minWidth: 0 }}>
          <h1>{a.name} <Chip tone={chip[0]}>{chip[1]}</Chip></h1>
          <div className="facts">
            {srv && <span><Server size={14} />{srv}</span>}
            {a.project_dir && <span className="mono">{a.project_dir}</span>}
            {started && <span><Clock size={14} />{uptime(started)}</span>}
          </div>
        </div>
        <div className="act">
          {canAct && <Btn icon={FileText} onClick={() => runner.run({ action: "logs", target: a.name, fire: () => endpoints.fireApplicationAction(a.name, "logs", { tail: 200 }) })}>Logs</Btn>}
          {canAct && h.rt.state !== "stopped" && <Btn icon={RotateCw} onClick={() => runner.run({ action: "restart", target: a.name, fire: () => endpoints.fireApplicationAction(a.name, "restart") })}>Restart</Btn>}
          {canAct && h.rt.state === "stopped" && <Btn icon={Play} onClick={() => runner.run({ action: "start", target: a.name, fire: () => endpoints.fireApplicationAction(a.name, "start") })}>Start</Btn>}
          {url && a.internet_exposed && <a className="btn primary" href={`https://${url}`} target="_blank" rel="noreferrer"><ExternalLink size={15} />Open {url}</a>}
        </div>
      </div>

      {h.why && (
        <div className="notice" style={h.sev === "danger" ? { borderColor: "rgb(251 136 154 / .35)", background: "rgb(251 136 154 / .07)" } : undefined}>
          {h.sev === "danger" ? <OctagonAlert size={16} color="var(--danger)" /> : <TriangleAlert size={16} />}{h.why}
        </div>
      )}

      {hops.length > 0 && (
        <Card>
          <CardHead title="Request path" meta={url ? "From the public hostname to the data on disk" : "From the listener to the data on disk"} />
          <div className="path">
            <div className="path-row" style={{ gridTemplateColumns: `repeat(${hops.length}, minmax(0, 1fr))` }}>
              {hops.length > 1 && (
                <div className="wire">
                  <svg viewBox="0 0 1000 14" preserveAspectRatio="none">
                    <line x1="40" y1="7" x2="960" y2="7" stroke="rgb(78 84 81)" strokeWidth="2" />
                    <line className="flow" x1="40" y1="7" x2="960" y2="7" stroke="#4dffad" strokeWidth="2.5" style={{ filter: "drop-shadow(0 0 4px rgb(0 232 135 / .8))" }} />
                  </svg>
                </div>
              )}
              {hops.map((hp) => {
                const I = hp.icon;
                return (
                  <div key={hp.key} className={`hop${hp.edge ? " edge" : ""}`}>
                    <div className="k"><I size={14} />{hp.k}</div>
                    {hp.v && <div className="v">{hp.v}</div>}
                    {hp.s && <div className="s">{hp.s}</div>}
                    {hp.stack && (
                      <div className="stack">
                        {hp.stack.map((x, i) => {
                          const SI = x.icon;
                          return <div key={i}>{SI ? <SI size={13} /> : <span className={`orb ${x.orb}`} />}{x.t}</div>;
                        })}
                        {hp.more > 0 && <div style={{ color: "var(--faint)" }}>+{hp.more} more</div>}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </Card>
      )}

      <div className="detail-grid">
        <div className="col">
          <Card>
            <CardHead title="Components" meta={<span className="mono">{containers.length + units.length + volumes.length + (a.nginx_sites || []).length}</span>} />
            <div className="card-b rows">
              <div className="r hd"><span /><span>Name</span><span>Source</span><span>State</span><span /></div>
              {containers.map((c) => (
                <div className="r" key={c.name}>
                  <span className="ki"><Container size={15} /></span>
                  <span className="nm" title={c.name}>{c.name}</span>
                  <span className="sub" title={c.image}>{c.image || "—"}</span>
                  <span><Chip tone={c.running ? "ok" : "danger"}>{c.running ? "Running" : "Exited"}</Chip></span>
                  <ActionBar entity={{ category: "docker_container", name: c.name }} />
                </div>
              ))}
              {units.map((u) => (
                <div className="r" key={u}>
                  <span className="ki"><Cog size={15} /></span>
                  <span className="nm" title={u}>{u}</span>
                  <span className="sub">systemd unit</span>
                  <span />
                  <ActionBar entity={{ category: u.endsWith(".timer") ? "systemd_timer" : "systemd_service", name: u }} />
                </div>
              ))}
              {(a.nginx_sites || []).map((n) => (
                <div className="r" key={n}>
                  <span className="ki"><Globe size={15} /></span>
                  <span className="nm" title={n}>{n}</span>
                  <span className="sub">nginx site</span>
                  <span />
                  <ActionBar entity={{ category: "nginx_server_block", name: n }} />
                </div>
              ))}
              {volumes.map((v) => (
                <div className="r" key={v.name}>
                  <span className="ki"><HardDrive size={15} /></span>
                  <span className="nm" title={v.name}>{v.name}</span>
                  <span className="sub">volume · {bytes(v.size_bytes || 0)}</span>
                  <span><Chip tone="idle">Mounted</Chip></span>
                  <ActionBar entity={{ category: "docker_volume", name: v.name }} inline={1} />
                </div>
              ))}
              {containers.length + units.length + volumes.length + (a.nginx_sites || []).length === 0 && (
                <Empty icon={FolderKanban} title="Folder only">No container, service or volume belongs to this project yet.</Empty>
              )}
            </div>
          </Card>
          <Card>
            <CardHead title="Activity"><Link className="link" to="/actions">Audit log<ChevronRight size={14} /></Link></CardHead>
            <div className="card-b tl">
              {history.length === 0 && <div style={{ color: "var(--subtle)", fontSize: 13 }}>No actions recorded for this project.</div>}
              {history.map((e, i) => {
                const verb = String(e.action || "").replace(/^teardown:/, "");
                const I = EV_ICON[verb] || Activity;
                return (
                  <div className="ev" key={i}>
                    <span className={`m ${e.status === "success" ? "" : "f"}`}><I size={13} /></span>
                    <span className="w"><b>{verb.charAt(0).toUpperCase() + verb.slice(1).replace(/_/g, " ")}</b> <span className="mono">{e.asset_name}</span>{e.actor ? ` · by ${e.actor}` : ""}</span>
                    <span className="when">{clock(e.timestamp)}</span>
                  </div>
                );
              })}
            </div>
          </Card>
        </div>

        <div className="col">
          <Card>
            <CardHead title="Details" />
            <div className="card-b facts-list">
              {a.compose_file && <div className="f"><span className="k">Compose file</span><span className="v mono">{a.compose_file.split("/").pop()}</span></div>}
              {ex && <div className="f"><span className="k">Exposed via</span><span className="v">{viaLabel(ex.via)}{srv && <> on <span className="mono">{srv}</span></>}</span></div>}
              {cert && <div className="f"><span className="k">Certificate</span><span className="v"><span className="mono">{(cert.domains || [cert.name])[0]}</span>{cert.issuer ? ` · ${shortIssuer(cert.issuer)}` : ""}</span></div>}
              <div className="f"><span className="k">Storage</span><span className="v mono">{bytes(a.total_size_bytes || 0)}</span></div>
              {a.resilience && <div className="f"><span className="k">Reboot</span><span className="v">{a.resilience.reboot_safe ? "Comes back on its own" : (a.resilience.issues || [])[0] || "Needs a manual start"}</span></div>}
              {(a.listening_ports || []).length > 0 && <div className="f"><span className="k">Ports</span><span className="v mono">{a.listening_ports.join(", ")}</span></div>}
              {cert?.not_after && <div className="f"><span className="k">Expires</span><span className="v mono">{isoDay(cert.not_after)}</span></div>}
            </div>
          </Card>
          {vias.length > 0 && (
            <Card>
              <CardHead title="Linked by" />
              <div className="card-b evid">
                {vias.map((v) => { const I = LINK_ICON[linkViaIcon(v)] || Link2; return <span key={v}><I size={14} />{linkViaLabel(v)}</span>; })}
              </div>
            </Card>
          )}
          {a.type === "project" && (
            <Card className="danger-zone">
              <CardHead title="Tear down" meta={<><Waypoints size={14} />Backup first</>} />
              <div className="card-b" style={{ paddingTop: 10, display: "flex", gap: 12, alignItems: "center" }}>
                <span style={{ color: "var(--subtle)", fontSize: 13, flex: 1 }}>Removes its containers, site configuration, volumes and folder.</span>
                <Btn sm danger icon={Trash2} onClick={() => setTearing(true)}>Review</Btn>
              </div>
            </Card>
          )}
        </div>
      </div>
      {tearing && <Teardown name={a.name} onClose={() => setTearing(false)} />}
      {runner.ui}
    </Page>
  );
}
