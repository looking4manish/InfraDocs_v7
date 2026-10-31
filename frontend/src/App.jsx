import { useCallback, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { Server, ArrowRight } from "lucide-react";
import { endpoints, isAuthed, clearToken } from "./api/client";
import { Login, ChangePassword } from "./pages/Login";
import Setup from "./pages/Setup";
import Dashboard from "./pages/Dashboard";
import Projects from "./pages/Projects";
import Application from "./pages/Application";
import Servers from "./pages/Servers";
import Web from "./pages/Web";
import Assets from "./pages/Assets";
import Actions from "./pages/Actions";
import TopBar from "./components/TopBar";
import CommandPalette from "./components/CommandPalette";
import { ToastProvider } from "./components/Toasts";
import { ScopeProvider } from "./lib/scope";

function Shell({ user }) {
  const loc = useLocation();
  useEffect(() => { window.scrollTo(0, 0); }, [loc.pathname]);
  return (
    <>
      <TopBar user={user} />
      <CommandPalette />
      <main key={loc.pathname}><Outlet /></main>
    </>
  );
}

// First run → Setup. Otherwise sign-in, forced password change, then the app.
function Gate({ children }) {
  const [state, setState] = useState({ status: "loading" });

  const resolve = useCallback(async () => {
    try {
      const { data } = await endpoints.setupStatus();
      if (!data.setup_complete) return setState({ status: "setup", setup: data });
    } catch { /* status unavailable — fall through to sign-in */ }
    if (!isAuthed()) return setState({ status: "login" });
    try {
      const { data } = await endpoints.me();
      if (data.must_change_password) return setState({ status: "change", user: data.username });
      setState({ status: "ready", user: data.username });
    } catch {
      clearToken();
      setState({ status: "login" });
    }
  }, []);

  useEffect(() => {
    const t = setTimeout(resolve, 0);
    const onUnauth = () => setState({ status: "login" });
    window.addEventListener("ifd-unauthorized", onUnauth);
    return () => { clearTimeout(t); window.removeEventListener("ifd-unauthorized", onUnauth); };
  }, [resolve]);

  if (state.status === "loading") return <div className="gate" />;
  if (state.status === "setup") return <Setup initial={state.setup} onDone={resolve} />;
  if (state.status === "login") return <Login onLoggedIn={resolve} />;
  if (state.status === "change") return <ChangePassword onDone={resolve} />;
  return children(state.user);
}

// A secondary sends the browser to the primary, which holds the fleet's data.
function PrimaryRedirect({ children }) {
  const [st, setSt] = useState(null);
  useEffect(() => {
    let alive = true;
    const check = () => endpoints.clusterState().then((r) => alive && setSt(r.data)).catch(() => alive && setSt(null));
    check();
    const t = setInterval(check, 10000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  const go = !!(st && st.role !== "primary" && st.role !== "standalone" && st.leader_id &&
    st.leader_id !== st.node_id && st.leader_address);
  useEffect(() => {
    if (!go) return undefined;
    const t = setTimeout(() => { window.location.href = st.leader_address; }, 2500);
    return () => clearTimeout(t);
  }, [go, st]);
  if (!go) return children;
  return (
    <div className="gate">
      <div className="card">
        <span className="app-icon" style={{ width: 52, height: 52 }}><Server size={24} /></span>
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 800 }}>{st.node_id} is a secondary</h1>
        <div style={{ color: "var(--subtle)" }}>The fleet is managed on the primary, <span className="mono" style={{ color: "var(--fg)" }}>{st.leader_id}</span>. Opening it now.</div>
        <a className="btn primary" href={st.leader_address}>Open {st.leader_id}<ArrowRight size={15} /></a>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <Gate>
        {(user) => (
          <PrimaryRedirect>
            <ScopeProvider>
              <BrowserRouter>
                <Routes>
                  <Route element={<Shell user={user} />}>
                    <Route path="/" element={<Dashboard />} />
                    <Route path="/projects" element={<Projects />} />
                    <Route path="/projects/:name" element={<Application />} />
                    <Route path="/servers" element={<Servers />} />
                    <Route path="/web" element={<Web />} />
                    <Route path="/assets" element={<Assets />} />
                    <Route path="/actions" element={<Actions />} />
                    <Route path="/setup" element={<Navigate to="/" replace />} />
                    <Route path="*" element={<Navigate to="/" replace />} />
                  </Route>
                </Routes>
              </BrowserRouter>
            </ScopeProvider>
          </PrimaryRedirect>
        )}
      </Gate>
    </ToastProvider>
  );
}
