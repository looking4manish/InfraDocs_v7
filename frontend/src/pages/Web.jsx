import { useMemo, useState } from "react";
import { Globe, Network, Lock, Router, ExternalLink, Search, Sparkles } from "lucide-react";
import { useEndpoints, useApps } from "../lib/queries";
import { useScope } from "../lib/scopeContext";
import { SCOPE_GROUPS, scopeGroup, viaLabel, healthOf, isProject, serverOfApp } from "../lib/derive";
import { Page, Head, Seg, SearchBox, Empty, SkelCard, Tag, Chip } from "../components/ui";

const GROUP_ICON = { internet: Globe, tailnet: Network, network: Router, local: Lock };
const GLYPH_BG = {
  ok: "linear-gradient(150deg,#4dffad,#00c974)",
  info: "linear-gradient(150deg,#a6c9ff,#5b8ff0)",
  idle: "linear-gradient(150deg,#55615a,#323834)",
};

export default function Web() {
  const { server } = useScope();
  const q = useEndpoints();
  const apps = useApps();
  const [group, setGroup] = useState("all");
  const [text, setText] = useState("");
  const all = (q.data || []).filter((e) => !server || e.server === server);
  const servers = new Set(all.map((e) => e.server).filter(Boolean));

  // Project health by (server, name) so a tile can flag a site whose project is down.
  const appState = useMemo(() => {
    const m = new Map();
    for (const a of (apps.data || []).filter(isProject)) m.set(`${serverOfApp(a)}|${a.name}`, healthOf(a));
    return m;
  }, [apps.data]);

  const ql = text.trim().toLowerCase();
  const list = all.filter((e) => !ql || [e.host, e.service, e.recognized, e.server, e.purpose].some((x) => (x || "").toLowerCase().includes(ql)));
  const groups = SCOPE_GROUPS.map((g) => ({ ...g, items: list.filter((e) => scopeGroup(e.scope).key === g.key) }));
  const counts = Object.fromEntries(SCOPE_GROUPS.map((g) => [g.key, all.filter((e) => scopeGroup(e.scope).key === g.key).length]));
  const shownGroups = groups.filter((g) => (group === "all" || group === g.key) && g.items.length);

  return (
    <Page>
      <Head eyebrow="Web & services" title="Every address in the fleet"
        sub={q.isLoading ? "Loading" : `${all.length} endpoints across ${servers.size || 1} server${servers.size === 1 ? "" : "s"}, grouped by who can reach them`} />
      <div className="toolbar">
        <Seg value={group} onChange={setGroup} options={[
          { value: "all", label: "All", count: all.length },
          ...SCOPE_GROUPS.filter((g) => counts[g.key]).map((g) => ({ value: g.key, label: g.label, count: counts[g.key] })),
        ]} />
        <SearchBox icon={Search} value={text} onChange={setText} placeholder="Filter by host, service or server" />
      </div>
      {q.isLoading && <div className="wgrid">{[0, 1, 2, 3, 4, 5, 6, 7].map((i) => <SkelCard key={i} h={110} />)}</div>}
      {!q.isLoading && all.length === 0 && <div className="card"><Empty icon={Globe} title="No endpoints found">Sites and listening services appear after the first scan.</Empty></div>}
      {!q.isLoading && all.length > 0 && shownGroups.length === 0 && <div className="card"><Empty icon={Search} title="No endpoints match" /></div>}
      {shownGroups.map((g) => {
        const GI = GROUP_ICON[g.key];
        return (
          <div className="wgroup" key={g.key}>
            <div className="wgroup-h"><span className="eyebrow"><GI size={14} />{g.label}</span><span className="count">{g.items.length}</span><span className="rule" /></div>
            <div className="wgrid">
              {g.items.map((e, i) => {
                const owned = e.service && e.service !== "System";
                const name = owned ? e.service : e.recognized || "Unclaimed";
                const h = owned ? appState.get(`${e.server}|${e.service}`) : null;
                const down = h?.rt.state === "stopped";
                const certWarn = g.key === "internet" && h?.why && h.why.startsWith("Certificate");
                const openable = e.url && g.key !== "local";
                const Tile = openable ? "a" : "div";
                const desc = e.recognized && e.recognized !== name ? e.recognized : e.purpose || null;
                return (
                  <Tile key={`${e.server}-${e.host}-${i}`} className="card wtile" {...(openable ? { href: e.url, target: "_blank", rel: "noreferrer" } : {})}>
                    <span className="glyph" style={{ background: GLYPH_BG[g.chip], color: g.chip === "idle" ? "#dce8e1" : undefined, boxShadow: g.chip === "ok" ? "0 0 18px rgb(0 232 135 / .35)" : undefined }}>
                      {(name[0] || "?").toUpperCase()}
                    </span>
                    <div className="body">
                      <div className="host" title={e.host}>{e.host}</div>
                      <div className="svc">{name}{desc && <span style={{ color: "var(--faint)", fontWeight: 500 }}>{desc}</span>}</div>
                      <div className="meta">
                        {e.server && <Tag>{e.server}</Tag>}
                        {e.via && <Tag sans>{viaLabel(e.via)}</Tag>}
                        {down && <Chip tone="danger">Down</Chip>}
                        {!down && certWarn && <Chip tone="warn">{h.why.replace("Certificate expires in ", "Cert ")}</Chip>}
                        {!owned && g.key !== "local" && <Chip tone="warn">No owner</Chip>}
                        {e.ai && <Chip tone="trace" dot={false}><Sparkles size={12} />Identified by AI</Chip>}
                      </div>
                    </div>
                    {openable && <span className="open"><ExternalLink size={16} /></span>}
                  </Tile>
                );
              })}
            </div>
          </div>
        );
      })}
    </Page>
  );
}
