import { useEffect, useState, useCallback } from "react";
import { BrowserRouter, Route, Routes, Outlet, Navigate, useParams, useLocation } from "react-router-dom";
import { AnimatePresence, motion } from "motion/react";
import { endpoints, isAuthed, clearToken } from "./api/client";
import { Login, ChangePassword } from "./pages/Login";
import Setup from "./pages/Setup";
import Header from "./components/Header";
import FleetBanner from "./components/FleetBanner";
import DrawerProvider from "./components/DrawerProvider";
import CommandPalette from "./components/CommandPalette";
import LensHome from "./pages/LensHome";
import Projects from "./pages/Projects";
import ProjectDetail from "./pages/ProjectDetail";
import Applications from "./pages/Applications";
import Assets from "./pages/Assets";
import Ports from "./pages/Ports";
import Storage from "./pages/Storage";
import Actions from "./pages/Actions";
import Scans from "./pages/Scans";
import AdminCluster from "./pages/AdminCluster";

// Old detail links (/applications/<name>) fold into the master-detail split view,
// which carries the selection in ?sel= so the list stays mounted.
function AppDetailRedirect() {
  const { name } = useParams();
  return <Navigate to={`/applications?sel=${encodeURIComponent(name)}`} replace />;
}

function AppShell() {
  const location = useLocation();
  return (
    <div className="h-screen flex flex-col">
      <Header />
      <div className="flex-1 flex overflow-hidden">
        <main className="flex-1 overflow-y-auto">
          <div className="px-6 pt-5"><FleetBanner /></div>
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={location.pathname}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              transition={{ duration: 0.18, ease: "easeOut" }}
              className="p-6"
            >
              <Outlet />
            </motion.div>
          </AnimatePresence>
        </main>
      </div>
    </div>
  );
}

// Gates the whole app behind login. Verifies the stored token via /me, forces
// a password change when flagged, and falls back to the login screen on 401.
function AuthGate({ children }) {
  // loading | login | change | setup | ready
  const [status, setStatus] = useState("loading");

  // After auth + password are OK, decide setup vs ready.
  const afterPasswordOk = useCallback(async () => {
    try {
      const { data } = await endpoints.setupStatus();
      setStatus(data.setup_complete ? "ready" : "setup");
    } catch {
      setStatus("ready"); // never block the app if status check fails
    }
  }, []);

  const refresh = useCallback(async () => {
    if (!isAuthed()) return setStatus("login");
    try {
      const { data } = await endpoints.me();
      if (data.must_change_password) return setStatus("change");
      await afterPasswordOk();
    } catch {
      clearToken();
      setStatus("login");
    }
  }, [afterPasswordOk]);

  useEffect(() => {
    refresh();
    const onUnauth = () => setStatus("login");
    window.addEventListener("ifd-unauthorized", onUnauth);
    return () => window.removeEventListener("ifd-unauthorized", onUnauth);
  }, [refresh]);

  if (status === "loading") return <div className="min-h-screen bg-[#0a0e14]" />;
  if (status === "login")
    return (
      <Login
        onLoggedIn={(d) =>
          d.must_change_password ? setStatus("change") : afterPasswordOk()
        }
      />
    );
  if (status === "change")
    return <ChangePassword onDone={afterPasswordOk} />;
  if (status === "setup") return <Setup onDone={() => setStatus("ready")} />;
  return children;
}

// App-level leader redirect (no VIP/keepalived — VRRP can't cross the tailnet L3
// overlay). If this node is not the cluster primary and a primary with a known address
// exists, show a lightweight page naming it and redirect there. The target follows
// gossip, so after a failover this points at the NEW primary automatically.
function ClusterRedirectGate({ children }) {
  const [state, setState] = useState(null);
  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const { data } = await endpoints.clusterState();
        if (alive) setState(data);
      } catch {
        if (alive) setState({ error: true });
      }
    };
    check();
    const t = setInterval(check, 10000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const leader = state?.current_leader;
  const addr = (state?.nodes || []).find((n) => n.node_id === leader)?.address;
  const shouldRedirect = !!(state && !state.error && leader && leader !== state.node_id && addr);

  useEffect(() => {
    if (!shouldRedirect) return;
    const t = setTimeout(() => { window.location.href = addr; }, 1500);
    return () => clearTimeout(t);
  }, [shouldRedirect, addr]);

  if (!shouldRedirect) return children;
  return (
    <div className="min-h-screen flex items-center justify-center bg-[#0a0e14] text-zinc-100 p-6">
      <div className="neon-panel rounded-2xl p-7 max-w-md text-center">
        <div className="text-lg font-semibold">This node is a cluster secondary</div>
        <div className="text-sm text-zinc-400 mt-2">
          The current primary is <span className="font-mono text-accent-soft">{leader}</span>.
          Redirecting…
        </div>
        <a href={addr}
          className="inline-block mt-4 text-[13px] px-4 py-2 rounded-lg neon-glow border border-[var(--neon)] bg-[var(--neon)]/10 hover:bg-[var(--neon)]/20">
          Go to the primary →
        </a>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <AuthGate>
    <ClusterRedirectGate>
    <BrowserRouter>
      <DrawerProvider>
        <CommandPalette />
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/" element={<LensHome />} />
            {/* Dashboard now lives as a lens on "/"; keep the old path deep-linkable. */}
            <Route path="/dashboard" element={<Navigate to="/" replace />} />
            <Route path="/applications" element={<Applications />} />
            <Route path="/applications/:name" element={<AppDetailRedirect />} />
            <Route path="/projects" element={<Projects />} />
            <Route path="/projects/:name" element={<ProjectDetail />} />
            <Route path="/assets" element={<Assets />} />
            <Route path="/ports" element={<Ports />} />
            <Route path="/storage" element={<Storage />} />
            <Route path="/actions" element={<Actions />} />
            <Route path="/scans" element={<Scans />} />
            <Route path="/admin" element={<AdminCluster />} />
          </Route>
        </Routes>
      </DrawerProvider>
    </BrowserRouter>
    </ClusterRedirectGate>
    </AuthGate>
  );
}
