import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { LayoutDashboard, FolderKanban, Server, Globe, Boxes, History, Search, Radar } from "lucide-react";
import { endpoints } from "../api/client";
import { useApps, useEndpoints } from "../lib/queries";
import { isProject, serverOfApp } from "../lib/derive";
import { useToast } from "../lib/toast";

const NAV = [
  { label: "Dashboard", to: "/", icon: LayoutDashboard },
  { label: "Projects", to: "/projects", icon: FolderKanban },
  { label: "Servers", to: "/servers", icon: Server },
  { label: "Web", to: "/web", icon: Globe },
  { label: "Assets", to: "/assets", icon: Boxes },
  { label: "Audit log", to: "/actions", icon: History },
];

function score(q, s) {
  if (!q) return 1;
  s = (s || "").toLowerCase();
  const i = s.indexOf(q);
  if (i === 0) return 100 - s.length * 0.1;
  if (i > 0) return 60 - i;
  let k = 0;
  for (const ch of s) if (ch === q[k]) k++;
  return k === q.length ? 20 - s.length * 0.05 : -1;
}

export default function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const nav = useNavigate();
  const toast = useToast();
  const apps = useApps();
  const eps = useEndpoints();
  const inputRef = useRef(null);

  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setQ(""); setSel(0); setOpen((v) => !v); }
      if (e.key === "Escape") setOpen(false);
    };
    const onOpen = () => { setQ(""); setSel(0); setOpen(true); };
    window.addEventListener("keydown", onKey);
    window.addEventListener("ifd-palette", onOpen);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("ifd-palette", onOpen); };
  }, []);
  useEffect(() => {
    if (!open) return undefined;
    const t = setTimeout(() => inputRef.current?.focus(), 0);
    return () => clearTimeout(t);
  }, [open]);

  const groups = useMemo(() => {
    const ql = q.trim().toLowerCase();
    const rank = (arr, key) => arr.map((x) => ({ x, s: score(ql, key(x)) })).filter((r) => r.s >= 0).sort((a, b) => b.s - a.s).map((r) => r.x);
    const g = [];
    const navs = rank(NAV, (n) => n.label).map((n) => ({ key: n.to, icon: n.icon, label: n.label, run: () => nav(n.to) }));
    const actions = rank([{ label: "Scan now" }], (n) => n.label).map(() => ({
      key: "scan", icon: Radar, label: "Scan now",
      run: () => endpoints.triggerScan().then(() => toast("Scan started.")).catch(() => toast("Scan could not start", "bad")),
    }));
    const appItems = rank((apps.data || []).filter(isProject), (a) => a.name).slice(0, 8).map((a) => ({
      key: `app:${a.application_id}`, icon: FolderKanban, label: a.name, sub: serverOfApp(a),
      run: () => nav(`/projects/${encodeURIComponent(a.name)}`),
    }));
    const hostItems = ql ? rank(eps.data || [], (e) => e.host).slice(0, 6).map((e, i) => ({
      key: `ep:${e.host}:${i}`, icon: Globe, label: e.host, sub: e.server, run: () => nav("/web"),
    })) : [];
    if (navs.length) g.push(["Go to", navs]);
    if (actions.length) g.push(["Actions", actions]);
    if (appItems.length) g.push(["Projects", appItems]);
    if (hostItems.length) g.push(["Addresses", hostItems]);
    return g;
  }, [q, apps.data, eps.data, nav, toast]);
  const flat = groups.flatMap(([, items]) => items);

  if (!open) return null;
  const choose = (it) => { setOpen(false); it?.run(); };
  let idx = -1;
  return (
    <div className="modal-scrim" onMouseDown={(e) => { if (e.target === e.currentTarget) setOpen(false); }}>
      <div className="palette" role="dialog" aria-label="Search">
        <div className="ph">
          <Search size={17} />
          <input ref={inputRef} value={q} placeholder="Search projects, addresses and pages"
            onChange={(e) => { setQ(e.target.value); setSel(0); }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(flat.length - 1, s + 1)); }
              if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(0, s - 1)); }
              if (e.key === "Enter") choose(flat[sel]);
            }} />
          <kbd className="tag">esc</kbd>
        </div>
        <div className="pl">
          {groups.map(([title, items]) => (
            <div key={title}>
              <div className="pg">{title}</div>
              {items.map((it) => {
                idx += 1;
                const me = idx;
                const I = it.icon;
                return (
                  <button key={it.key} type="button" className={`pi${me === sel ? " on" : ""}`}
                    onMouseEnter={() => setSel(me)} onClick={() => choose(it)}>
                    <I size={15} />{it.label}{it.sub && <span className="s">{it.sub}</span>}
                  </button>
                );
              })}
            </div>
          ))}
          {flat.length === 0 && <div className="empty" style={{ padding: 28 }}><div className="d">No matches</div></div>}
        </div>
      </div>
    </div>
  );
}
