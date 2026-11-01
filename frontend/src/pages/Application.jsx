// The project page. Same content and linking as V6's ApplicationDetail (topology lane built
// from nginx_detail / containers_detail, the storage row from the storage registry, every
// join from links[] with its evidence, then the per-type sections), in the V7 visual system.
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ChevronRight, ChevronDown, Server, FolderKanban, Globe, ShieldCheck, ShieldAlert, Plug, Box, HardDrive, Cog,
  FileText, RotateCw, ExternalLink, Layers, Link2, Trash2, Activity, Play, Square, OctagonAlert, TriangleAlert,
  Package, Network, Database, KeyRound, Folder, FileLock2, Timer,
} from "lucide-react";
import { endpoints } from "../api/client";
import { serverOfApp, healthOf } from "../lib/derive";
import { bytes, clock, errDetail, isoDay } from "../lib/format";
import { Page, Card, CardHead, Btn, Chip, Empty, Skel } from "../components/ui";
import ActionBar from "../components/ActionBar";
import { useActionRunner } from "../hooks/useActionRunner";
import Teardown from "../components/Teardown";

const STATE_CHIP = {
  running: ["ok", "Running"], partial: ["warn", "Partly running"], stopped: ["danger", "Stopped"], idle: ["idle", "Nothing running"],
};
const EV_ICON = { restart: RotateCw, stop: Square, start: Play, logs: FileText };

// links[].src_kind / dst_kind → readable name + icon
const KIND = {
  application: ["Project", FolderKanban], docker_container: ["Container", Box], docker_image: ["Image", Package],
  docker_volume: ["Volume", HardDrive], docker_network: ["Network", Network], docker_compose: ["Compose", Layers],
  network_port: ["Port", Plug], nginx_server_block: ["Nginx site", Globe], caddy_site: ["Caddy site", Globe],
  cloudflare_tunnel: ["Tunnel", Globe], tls_certificate: ["Certificate", FileLock2], systemd_service: ["Service", Cog],
  systemd_timer: ["Timer", Timer], cron_job: ["Cron job", Timer], storage_mount: ["Mount", Database],
  project_directory: ["Folder", Folder],
};
const kindOf = (k) => KIND[k] || [String(k || "").replace(/_/g, " "), Link2];

// V6 TopologyLane: the authoritative nginx block is one that proxies somewhere and/or has the url.
function strongestNginx(list = []) {
  if (!list.length) return null;
  const score = (n) => (n.upstream_port ? 4 : 0) + (n.url ? 2 : 0) + (n.internet_exposed ? 1 : 0);
  return [...list].sort((a, b) => score(b) - score(a))[0];
}

function LaneNode({ icon: I, kind, title, children, state, warn }) {
  return (
    <div className={`lnode${warn ? " warn" : ""}`}>
      <div className="lk"><I size={14} />{kind}</div>
      <div className="lt" title={title}>{title}</div>
      {children && <div className="ld">{children}</div>}
      {state}
    </div>
  );
}

function Connector({ label, weak }) {
  return (
    <div className={`lconn${weak ? " weak" : ""}`}>
      <span className="ll">{label}</span>
      <span className="lw"><i /></span>
    </div>
  );
}

function Topology({ app, storage }) {
  const nginx = strongestNginx(app.nginx_detail);
  const upstreamPort = nginx?.upstream_port ?? null;
  const url = nginx?.url || app.urls?.[0] || null;
  const containers = app.containers_detail || [];
  const container = (upstreamPort != null && containers.find((c) => (c.host_ports || []).includes(upstreamPort))) || containers[0] || null;
  const volumes = storage || [];
  const units = app.systemd_units?.length || 0;

  let lane;
  if (!nginx && !container) {
    lane = units > 0
      ? <div className="lane-note">Runs as {units} systemd unit{units > 1 ? "s" : ""}; no web or container flow to map.</div>
      : <div className="lane-note warn"><TriangleAlert size={15} />No topology evidence yet: no nginx or container detail. Run a scan to populate it.</div>;
  } else {
    lane = (
      <div className="lane">
        {url && (
          <>
            <LaneNode icon={Globe} kind="URL" title={url.replace(/^https?:\/\//, "")}>
              <span><em>scheme</em> {url.startsWith("https") ? "https" : "http"}</span>
            </LaneNode>
            <Connector label={nginx?.cloudflare_origin ? "cloudflare" : "tls"} />
          </>
        )}
        {nginx && (
          <>
            <LaneNode icon={Server} kind="Nginx" title={nginx.server_name}
              state={<div className="lstate">
                {nginx.internet_exposed && <Chip tone="ok" dot={false}><Globe size={11} />Internet-exposed</Chip>}
                <ActionBar entity={{ category: "nginx_server_block", name: nginx.server_name }} />
              </div>}>
              <span><em>listen</em> <span className="mono">{(nginx.listen_ports || []).join(" · ") || "—"}</span></span>
              <span><em>ssl</em> {nginx.has_ssl
                ? (nginx.ssl_not_after
                  ? <><ShieldCheck size={12} className="okc" /><span className="mono">{String(nginx.ssl_not_after).slice(0, 10)}</span></>
                  : <><ShieldAlert size={12} className="warnc" /><span className="warnc">unparsed</span></>)
                : "—"}</span>
            </LaneNode>
            {upstreamPort != null && <Connector label={`proxy_pass :${upstreamPort}`} />}
          </>
        )}
        {upstreamPort != null && (
          <>
            <LaneNode icon={Plug} kind="Host port" title={`:${upstreamPort}`}>
              <span><em>upstream</em> {nginx?.upstream_host || "localhost"}</span>
            </LaneNode>
            {container && <Connector label={`host_port:${upstreamPort}`} />}
          </>
        )}
        {container && (
          <LaneNode icon={Box} kind="Container" title={container.name}
            state={<div className="lstate">
              <Chip tone={container.running ? "ok" : "danger"}>{container.running ? "Running" : "Exited"}</Chip>
              <ActionBar entity={{ category: "docker_container", name: container.name }} />
            </div>}>
            <span><em>image</em> <span className="mono">{container.image?.split("/").pop()}</span></span>
            <span><em>restart</em> {typeof container.restart_policy === "string" ? container.restart_policy : container.restart_policy?.Name || "—"}</span>
            <span><em>health</em> {container.has_health_check ? <><Activity size={12} className="okc" />check defined</> : "none"}</span>
          </LaneNode>
        )}
      </div>
    );
  }

  return (
    <Card>
      <CardHead title="Topology" meta="Derived from the scanners' nginx, container and storage detail" />
      <div className="card-b">
        {lane}
        {volumes.length > 0 && (
          <>
            <div className="lane-h">Storage</div>
            <div className="lane store">
              {volumes.map((v) => (
                <LaneNode key={v.storage_id || v.name} icon={HardDrive} kind={v.kind || "volume"} title={v.name}>
                  <span className="mono path" title={v.path || v.mountpoint}>{v.path || v.mountpoint}</span>
                  <span><em>size</em> <span className="mono">{bytes(v.size_bytes || 0)}</span></span>
                </LaneNode>
              ))}
            </div>
          </>
        )}
        <LinkEvidence links={app.links || []} />
      </div>
    </Card>
  );
}

function LinkEvidence({ links }) {
  if (!links.length) return null;
  return (
    <div className="joins">
      <div className="joins-h"><Link2 size={14} />Link evidence<span className="mono">{links.length} joins</span></div>
      <div className="joins-row hd"><span>Via</span><span>From</span><span /><span>To</span><span>Pass</span></div>
      {links.map((l, i) => {
        const weak = l.via === "project_tag";
        const [sk, SI] = kindOf(l.src_kind);
        const [dk, DI] = kindOf(l.dst_kind);
        return (
          <div key={i} className={`joins-row${weak ? " weak" : ""}`} title={weak ? "Weaker evidence: a project tag only" : undefined}>
            <span className="via mono">{l.via}</span>
            <span className="end"><SI size={14} /><b>{sk}</b><span className="mono" title={l.src}>{l.src}</span></span>
            <span className="arrow">→</span>
            <span className="end"><DI size={14} /><b>{dk}</b><span className="mono" title={l.dst}>{l.dst}</span></span>
            <span className="pass mono">{l.pass}</span>
          </div>
        );
      })}
    </div>
  );
}

function BlastRadius({ name, isProject, onTeardown }) {
  const [open, setOpen] = useState(false);
  const q = useQuery({
    queryKey: ["blast-radius", name],
    queryFn: () => endpoints.blastRadius(name).then((r) => r.data),
    enabled: open,
  });
  const d = q.data;
  return (
    <Card className="blast-card">
      <button type="button" className="blast-toggle" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <TriangleAlert size={16} className="warnc" />
        <span className="t">Blast radius</span>
        <span className="d">Teardown preview · read-only</span>
        {d && <span className="mono d">{d.summary.total} assets{d.summary.data_loss > 0 && ` · ${d.summary.data_loss} data loss`}{d.summary.shared > 0 && ` · ${d.summary.shared} shared`}</span>}
        <ChevronDown size={16} className={`chev${open ? " up" : ""}`} />
      </button>
      {open && (
        <div className="card-b" style={{ paddingTop: 0 }}>
          {q.isLoading && <Skel h={60} />}
          {q.isError && <div className="formerr">{errDetail(q.error, "Blast radius unavailable")}</div>}
          {d && (
            <>
              {(d.warnings || []).map((w, i) => <div key={i} className="notice"><TriangleAlert size={15} />{w}</div>)}
              <div className="blast-list">
                {(d.items || []).map((it, i) => {
                  const [label, I] = kindOf(it.category);
                  const prot = (d.protected || []).includes(it.name);
                  return (
                    <div key={i} className="bi">
                      <I size={15} /><span className="cat">{label}</span><span className="mono nm" title={it.name}>{it.name}</span>
                      <span className="flags">
                        {it.data_loss && <Chip tone="danger" dot={false}><Database size={11} />Data loss</Chip>}
                        {it.shared && <Chip tone="warn" dot={false}><Link2 size={11} />Shared · {(it.shared_with || []).join(", ")}</Chip>}
                        {prot && <Chip tone="info" dot={false}><ShieldCheck size={11} />Protected</Chip>}
                      </span>
                    </div>
                  );
                })}
              </div>
              <div className="blast-foot">
                <span>Red: removal destroys data. Amber: shared with another project, skipped and never removed.</span>
                {isProject && <Btn sm danger icon={Trash2} onClick={onTeardown}>Tear down…</Btn>}
              </div>
            </>
          )}
        </div>
      )}
    </Card>
  );
}

function Section({ title, count, right, wide, children }) {
  return (
    <Card className={wide ? "sec wide" : "sec"}>
      <CardHead title={title} meta={right || (typeof count === "number" ? <span className="mono">{count}</span> : null)} />
      <div className="card-b">{children}</div>
    </Card>
  );
}

function Row({ icon: I, name, sub, state, children }) {
  return (
    <div className="srow">
      {I && <span className="ki"><I size={15} /></span>}
      <span className="nm" title={name}>{name}</span>
      {sub && <span className="sub" title={typeof sub === "string" ? sub : undefined}>{sub}</span>}
      {state && <span className="st">{state}</span>}
      <span className="acts">{children}</span>
    </div>
  );
}

export default function Application() {
  const { name } = useParams();
  const [tearing, setTearing] = useState(false);
  const runner = useActionRunner([["applications"], ["application", name]]);
  const q = useQuery({ queryKey: ["application", name], queryFn: () => endpoints.getApplication(name).then((r) => r.data), refetchInterval: 30000 });
  const ports = useQuery({ queryKey: ["ports", { project: name }], queryFn: () => endpoints.listPorts({ project: name }).then((r) => r.data) });
  const storage = useQuery({ queryKey: ["storage", { project: name }], queryFn: () => endpoints.listStorage({ project: name }).then((r) => r.data) });
  const compose = useQuery({
    queryKey: ["asset-by-name", "docker_compose", name],
    queryFn: () => endpoints.listAssets({ category: "docker_compose", project: name }).then((r) => (r.data.assets || [])[0] || null),
    enabled: !!q.data?.compose_file,
  });
  const acts = useQuery({ queryKey: ["actions-list", 200], queryFn: () => endpoints.listActions({ limit: 200 }).then((r) => r.data.actions || []) });

  if (q.isLoading) return <Page><Skel h={22} w={200} /><div style={{ height: 20 }} /><Skel h={56} w={420} /><div style={{ height: 24 }} /><Skel h={220} /></Page>;
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
  const isSystem = a.type === "system";
  const h = healthOf(a);
  const srv = serverOfApp(a);
  const chip = STATE_CHIP[h.rt.state];
  const containers = a.containers || [];
  const units = a.systemd_units || [];
  const canAct = containers.length > 0 || units.length > 0;
  const store = storage.data?.storage || [];
  const portRows = ports.data?.ports || [];
  const url0 = (a.urls || [])[0];
  const history = (acts.data || []).filter((e) => e.project === a.name).slice(0, 6);
  const detail = Object.fromEntries((a.containers_detail || []).map((c) => [c.name, c]));

  return (
    <Page>
      <div className="crumbs"><Link to="/projects">Projects</Link><ChevronRight size={13} /><span>{a.name}</span></div>
      <div className="app-head">
        <div className="app-icon"><FolderKanban size={26} /></div>
        <div style={{ minWidth: 0 }}>
          <h1>{a.name} <Chip tone={chip[0]}>{chip[1]}</Chip>
            <Chip tone={isSystem ? "idle" : "info"} dot={false}>{a.type}</Chip>
            {a.internet_exposed && <Chip tone="ok" dot={false}><Globe size={11} />{a.cloudflare ? "Via Cloudflare" : "Internet-exposed"}</Chip>}
          </h1>
          <div className="facts">
            {srv && <span><Server size={14} />{srv}</span>}
            {a.project_dir && <span className="mono">{a.project_dir}</span>}
          </div>
        </div>
        <div className="act">
          {canAct && <Btn icon={FileText} onClick={() => runner.run({ action: "logs", target: a.name, fire: () => endpoints.fireApplicationAction(a.name, "logs", { tail: 200 }) })}>Logs</Btn>}
          {containers.length > 0 && <Btn icon={RotateCw} onClick={() => runner.run({ action: "restart", target: a.name, fire: () => endpoints.fireApplicationAction(a.name, "restart") })}>Restart app</Btn>}
          {url0 && <a className="btn primary" href={url0} target="_blank" rel="noreferrer"><ExternalLink size={15} />Open</a>}
        </div>
      </div>

      {h.why && (
        <div className="notice" style={h.sev === "danger" ? { borderColor: "rgb(251 136 154 / .35)", background: "rgb(251 136 154 / .07)" } : undefined}>
          {h.sev === "danger" ? <OctagonAlert size={16} color="var(--danger)" /> : <TriangleAlert size={16} />}{h.why}
        </div>
      )}

      <Topology app={a} storage={store} />

      <div className="statstrip">
        {[["Components", a.components_count ?? 0], ["Containers", containers.length], ["Systemd units", units.length],
          ["Nginx sites", (a.nginx_sites || []).length], ["Disk", bytes(a.total_size_bytes || 0)]].map(([k, v]) => (
          <div className="stat" key={k}><div className="k">{k}</div><div className="v">{v}</div></div>
        ))}
      </div>

      <BlastRadius name={a.name} isProject={a.type === "project"} onTeardown={() => setTearing(true)} />

      <div className="sections">
        {(a.urls || []).length > 0 && (
          <Section title="URLs" count={a.urls.length}>
            {a.urls.map((u) => <a key={u} className="url-row mono" href={u} target="_blank" rel="noopener noreferrer"><Globe size={14} />{u}<ExternalLink size={13} /></a>)}
          </Section>
        )}

        {containers.length > 0 && (
          <Section title="Containers" count={containers.length} wide>
            {containers.map((c) => {
              const pm = a.port_mappings?.find((m) => m.container === c);
              const d = detail[c];
              return (
                <Row key={c} icon={Box} name={c}
                  sub={<>{d?.image && <span className="mono">{d.image.split("/").pop()}</span>}{pm && <span className="mono"> · {pm.host_port} → {pm.container_port}</span>}</>}
                  state={d ? <Chip tone={d.running ? "ok" : "danger"}>{d.running ? "Running" : "Exited"}</Chip> : null}>
                  <ActionBar entity={{ category: "docker_container", name: c }} />
                </Row>
              );
            })}
          </Section>
        )}

        {(a.images || []).length > 0 && (
          <Section title="Images" count={a.images.length}>
            {a.images.map((img) => <Row key={img} icon={Package} name={img}><ActionBar entity={{ category: "docker_image", name: img }} /></Row>)}
          </Section>
        )}

        {a.compose_file && (
          <Section title="Compose">
            <Row icon={Layers} name={a.compose_file}>{compose.data && <ActionBar entity={{ category: "docker_compose", asset_id: compose.data.asset_id, name: compose.data.name }} />}</Row>
          </Section>
        )}

        {units.length > 0 && (
          <Section title="Systemd units" count={units.length}>
            {units.map((u) => <Row key={u} icon={u.endsWith(".timer") ? Timer : Cog} name={u}><ActionBar entity={{ category: u.endsWith(".timer") ? "systemd_timer" : "systemd_service", name: u }} /></Row>)}
          </Section>
        )}

        {(a.nginx_sites || []).length > 0 && (
          <Section title="Nginx sites" count={a.nginx_sites.length}>
            {a.nginx_sites.map((s) => <Row key={s} icon={Globe} name={s}><ActionBar entity={{ category: "nginx_server_block", name: s }} /></Row>)}
          </Section>
        )}

        {(a.volumes || []).length > 0 && (
          <Section title="Docker volumes" count={a.volumes.length} wide>
            {a.volumes.map((v) => (
              <Row key={v.name} icon={HardDrive} name={v.name} sub={<span className="mono" title={v.mountpoint}>{v.mountpoint}</span>} state={<span className="mono size">{bytes(v.size_bytes || 0)}</span>}>
                <ActionBar entity={{ category: "docker_volume", name: v.name }} inline={1} />
              </Row>
            ))}
          </Section>
        )}

        {portRows.length > 0 && (
          <Section title="Ports" count={portRows.length} right={<Link className="link" to="/assets?c=network_port">All ports<ChevronRight size={14} /></Link>}>
            {portRows.map((p) => (
              <Row key={p.port_id} icon={Plug} name={`${p.port}/${p.protocol}`} sub={p.process || "—"}
                state={<Chip tone={p.state === "in_use" || p.state === "listening" ? "ok" : "idle"}>{String(p.state || "").replace(/_/g, " ")}</Chip>} />
            ))}
          </Section>
        )}

        {store.length > 0 && (
          <Section title="Storage" count={store.length} right={<Link className="link" to="/assets?c=storage_mount">All storage<ChevronRight size={14} /></Link>}>
            {store.map((s) => <Row key={s.storage_id} icon={Database} name={s.name} sub={<><span className="tag sans">{s.kind}</span> <span className="mono" title={s.path}>{s.path}</span></>} state={<span className="mono size">{bytes(s.size_bytes || 0)}</span>} />)}
          </Section>
        )}

        {(a.env_keys || []).length > 0 && (
          <Section title="Environment keys" right={<span className="meta">Names only · <span className="mono">{a.env_keys.length}</span></span>}>
            <div className="keys">{a.env_keys.map((k) => <span key={k} className="mono"><KeyRound size={11} />{k}</span>)}</div>
          </Section>
        )}

        {a.project_dir && (
          <Section title="Project directory">
            <div className="kv" style={{ gridTemplateColumns: "150px minmax(0,1fr)" }}>
              <span className="k">Path</span><span className="v mono">{a.project_dir}</span>
              <span className="k">Tree size</span><span className="v mono">{bytes(a.project_dir_size_bytes || 0)}</span>
              <span className="k">Total with volumes</span><span className="v mono">{bytes(a.total_size_bytes || 0)}</span>
            </div>
          </Section>
        )}

        <Section title="Activity" right={<Link className="link" to="/actions">Audit log<ChevronRight size={14} /></Link>}>
          <div className="tl">
            {history.length === 0 && <div style={{ color: "var(--subtle)", fontSize: 13 }}>No actions recorded for this project.</div>}
            {history.map((e, i) => {
              const verb = String(e.action || "").replace(/^teardown:/, "");
              const I = EV_ICON[verb] || Activity;
              return (
                <div className="ev" key={i}>
                  <span className={`m ${e.status === "success" ? "" : "f"}`}><I size={13} /></span>
                  <span className="w"><b>{verb.charAt(0).toUpperCase() + verb.slice(1).replace(/_/g, " ")}</b> <span className="mono">{e.asset_name}</span>{e.actor ? ` · by ${e.actor}` : ""}</span>
                  <span className="when">{clock(e.timestamp) || isoDay(e.timestamp)}</span>
                </div>
              );
            })}
          </div>
        </Section>
      </div>
      {tearing && <Teardown name={a.name} onClose={() => setTearing(false)} />}
      {runner.ui}
    </Page>
  );
}
