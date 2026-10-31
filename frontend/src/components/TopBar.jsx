import { useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Layers, LayoutDashboard, FolderKanban, Server, Globe, Boxes, Search, ChevronDown, Radar, History, LogOut, Check } from "lucide-react";
import { endpoints, clearToken } from "../api/client";
import { useScope } from "../lib/scopeContext";
import { useApps, useCluster, serverList } from "../lib/queries";
import { useToast } from "../lib/toast";
import { Btn } from "./ui";
import { useOutside } from "../hooks/useOutside";
import { errDetail } from "../lib/format";

const NAV = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true },
  { to: "/projects", label: "Projects", icon: FolderKanban },
  { to: "/servers", label: "Servers", icon: Server },
  { to: "/web", label: "Web", icon: Globe },
  { to: "/assets", label: "Assets", icon: Boxes },
];

export default function TopBar({ user }) {
  const nav = useNavigate();
  const toast = useToast();
  const qc = useQueryClient();
  const { server, setServer } = useScope();
  const apps = useApps();
  const cluster = useCluster();
  const servers = serverList(cluster.data, apps.data);
  const [scopeOpen, setScopeOpen] = useState(false);
  const [userOpen, setUserOpen] = useState(false);
  const scopeRef = useOutside(() => setScopeOpen(false));
  const userRef = useOutside(() => setUserOpen(false));

  const scan = useMutation({
    mutationFn: () => endpoints.triggerScan(),
    onSuccess: () => {
      toast("Scan started. Results appear in about a minute.");
      setTimeout(() => qc.invalidateQueries(), 20000);
    },
    onError: (e) => toast(errDetail(e, "Scan could not start"), "bad"),
  });

  const initials = (user || "").slice(0, 2).toUpperCase() || "ID";

  return (
    <header className="top">
      <NavLink to="/" className="brand"><span className="brand-mark"><Layers size={17} /></span>InfraDocs</NavLink>
      <nav className="nav" aria-label="Main">
        {NAV.map(({ to, label, icon: I, end }) => (
          <NavLink key={to} to={to} end={end} className={({ isActive }) => (isActive ? "on" : "")}>
            <I size={15} />{label}
          </NavLink>
        ))}
      </nav>
      <div className="top-right">
        <button type="button" className="kbd" onClick={() => window.dispatchEvent(new Event("ifd-palette"))}>
          <Search size={15} />Search apps, ports, hosts<kbd>⌘K</kbd>
        </button>
        {servers.length > 1 && (
          <div ref={scopeRef} style={{ position: "relative" }}>
            <button type="button" className="scope" aria-haspopup="menu" aria-expanded={scopeOpen} onClick={() => setScopeOpen((v) => !v)}>
              <span className="dotlive" />{server || "All servers"}<ChevronDown size={14} />
            </button>
            {scopeOpen && (
              <div className="menu" role="menu">
                <button type="button" className={!server ? "on" : ""} onClick={() => { setServer(null); setScopeOpen(false); }}>
                  {!server ? <Check size={14} /> : <span style={{ width: 14 }} />}All servers<span className="c">{servers.length}</span>
                </button>
                <div className="sep" />
                {servers.map((s) => (
                  <button key={s} type="button" className={server === s ? "on" : ""} onClick={() => { setServer(s); setScopeOpen(false); }}>
                    {server === s ? <Check size={14} /> : <span style={{ width: 14 }} />}<span className="mono">{s}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        <Btn primary icon={Radar} busy={scan.isPending} onClick={() => scan.mutate()}>Scan now</Btn>
        <div ref={userRef} style={{ position: "relative" }}>
          <button type="button" className="avatar" aria-label="Account" onClick={() => setUserOpen((v) => !v)}>{initials}</button>
          {userOpen && (
            <div className="menu" role="menu">
              {user && <div style={{ padding: "8px 10px", color: "var(--subtle)", fontSize: 12.5 }}>Signed in as <span className="mono" style={{ color: "var(--fg)" }}>{user}</span></div>}
              <button type="button" onClick={() => { setUserOpen(false); nav("/actions"); }}><History size={14} />Audit log</button>
              <div className="sep" />
              <button type="button" onClick={async () => {
                try { await endpoints.logout(); } catch { /* token may already be gone */ }
                clearToken(); window.location.href = "/";
              }}><LogOut size={14} />Sign out</button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
