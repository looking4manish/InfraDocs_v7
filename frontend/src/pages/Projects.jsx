import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Container, Cog, Plug, HardDrive, Globe, OctagonAlert, TriangleAlert, Search, ChevronRight, FolderKanban } from "lucide-react";
import { useApps, useCluster, serverList } from "../lib/queries";
import { useScope } from "../lib/scopeContext";
import { isProject, serverOfApp, healthOf, sevRank, primaryUrl, SYSTEM } from "../lib/derive";
import { bytes, plural } from "../lib/format";
import { Page, Head, Seg, SearchBox, Empty, SkelCard, Tag } from "../components/ui";

const GLYPHS = [
  ["containers", Container, "containers"],
  ["systemd_units", Cog, "services"],
  ["listening_ports", Plug, "ports"],
  ["volumes", HardDrive, "volumes"],
];

export default function Projects() {
  const [params, setParams] = useSearchParams();
  const filter = params.get("f") || "all";
  const setFilter = (f) => setParams(f === "all" ? {} : { f }, { replace: true });
  const { server: scopeServer, setServer } = useScope();
  const [q, setQ] = useState("");
  const appsQ = useApps();
  const cluster = useCluster();
  const all = useMemo(() => appsQ.data || [], [appsQ.data]);
  const servers = serverList(cluster.data, all);

  const scoped = all.filter((a) => !scopeServer || serverOfApp(a) === scopeServer);
  const projects = scoped.filter(isProject).map((a) => ({ a, h: healthOf(a) }));
  const totalBytes = projects.reduce((s, p) => s + (p.a.total_size_bytes || 0), 0) || 1;
  const counts = {
    all: projects.length,
    attention: projects.filter((p) => p.h.sev === "danger" || p.h.sev === "warn").length,
    public: projects.filter((p) => p.a.internet_exposed).length,
    stopped: projects.filter((p) => p.h.rt.state === "stopped").length,
  };
  const ql = q.trim().toLowerCase();
  const shown = projects
    .filter((p) => filter === "all" || (filter === "attention" && (p.h.sev === "danger" || p.h.sev === "warn"))
      || (filter === "public" && p.a.internet_exposed) || (filter === "stopped" && p.h.rt.state === "stopped"))
    .filter((p) => !ql || p.a.name.toLowerCase().includes(ql) || (p.a.urls || []).some((u) => u.toLowerCase().includes(ql))
      || (p.a.listening_ports || []).some((x) => String(x).includes(ql)))
    .sort((x, y) => sevRank(x.h.sev) - sevRank(y.h.sev) || (y.a.total_size_bytes || 0) - (x.a.total_size_bytes || 0));
  const system = scoped.filter((a) => a.name === SYSTEM);
  const sysAssets = system.reduce((s, a) => s + (a.components_count || 0) + (a.images || []).length + (a.listening_ports || []).length, 0);

  return (
    <Page>
      <Head eyebrow="Inventory" title="Projects" sub="One card per project folder, most urgent first"
        right={
          <div className="legend-row">
            <span><Container size={14} />Containers</span><span><Cog size={14} />Services</span>
            <span><Plug size={14} />Ports</span><span><HardDrive size={14} />Volumes</span>
          </div>
        } />
      <div className="toolbar">
        <Seg value={filter} onChange={setFilter} options={[
          { value: "all", label: "All", count: counts.all },
          { value: "attention", label: "Needs attention", count: counts.attention },
          { value: "public", label: "Internet-facing", count: counts.public },
          { value: "stopped", label: "Stopped", count: counts.stopped },
        ]} />
        {servers.length > 1 && (
          <Seg value={scopeServer || ""} onChange={(v) => setServer(v || null)}
            options={[{ value: "", label: "All servers" }, ...servers.map((s) => ({ value: s, label: s }))]} />
        )}
        <SearchBox icon={Search} value={q} onChange={setQ} placeholder="Filter by name, host or port" />
      </div>

      {appsQ.isLoading && <div className="pgrid">{[0, 1, 2, 3, 4, 5].map((i) => <SkelCard key={i} h={190} />)}</div>}
      {!appsQ.isLoading && projects.length === 0 && (
        <div className="card"><Empty icon={FolderKanban} title="No project folders found">Projects appear here after a scan finds folders under the projects root.</Empty></div>
      )}
      {!appsQ.isLoading && projects.length > 0 && shown.length === 0 && (
        <div className="card"><Empty icon={Search} title="No projects match">Change the filter or the search text.</Empty></div>
      )}

      <div className="pgrid">
        {shown.map(({ a, h }) => {
          const url = primaryUrl(a);
          const srv = serverOfApp(a);
          const ports = a.listening_ports || [];
          const pct = Math.min(100, Math.max(1, ((a.total_size_bytes || 0) / totalBytes) * 100));
          return (
            <Link key={a.application_id || a.name} className={`card pcard sev-${h.sev}`} to={`/projects/${encodeURIComponent(a.name)}`}>
              <div className="h"><span className={`orb ${h.sev}`} /><span className="nm">{a.name}</span>{srv && <Tag>{srv}</Tag>}</div>
              {url ? (
                <div className="url"><Globe size={13} />{url}</div>
              ) : (
                <div className="url none">
                  {h.rt.state === "idle" ? "Folder only · nothing listening" : ports.length ? `Not public · ${ports.length === 1 ? "port" : "ports"} ${ports.slice(0, 3).join(", ")}` : "Not public"}
                </div>
              )}
              {h.why && (
                <div className={`why ${h.sev === "danger" ? "" : "w"}`}>
                  {h.sev === "danger" ? <OctagonAlert size={14} /> : <TriangleAlert size={14} />}{h.why}
                </div>
              )}
              <div className="comp">
                {GLYPHS.map(([k, I, label]) => {
                  const n = (a[k] || []).length;
                  return n ? <span key={k} title={`${n} ${label}`}><I size={14} />{n}</span> : null;
                })}
              </div>
              <div className="foot">
                <span className="sz">{bytes(a.total_size_bytes || 0)}</span>
                <span className="mini"><i style={{ width: `${pct}%` }} /></span>
                <span>{h.rt.state === "idle" ? "Nothing running" : h.rt.state === "stopped" ? "Stopped" : `${plural(h.rt.running + h.rt.units, "component")} up`}</span>
              </div>
            </Link>
          );
        })}
      </div>

      {system.length > 0 && (
        <div className="card sys-strip">
          <span className="sev warn" style={{ width: 36, height: 36, borderRadius: 11, display: "grid", placeItems: "center" }}><Cog size={18} /></span>
          <div><div className="t">System</div><div className="d">{plural(sysAssets, "asset")} outside any project folder</div></div>
          <Link className="btn sm" style={{ marginLeft: "auto" }} to="/assets?owner=system">Review<ChevronRight size={14} /></Link>
        </div>
      )}
    </Page>
  );
}
