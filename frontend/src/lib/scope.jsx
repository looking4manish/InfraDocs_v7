// Provides the server scope chosen in the top bar. Every lens filters by it.
import { useState } from "react";
import { ScopeCtx } from "./scopeContext";

export function ScopeProvider({ children }) {
  const [server, setServer] = useState(() => {
    try { return sessionStorage.getItem("ifd_scope") || null; } catch { return null; }
  });
  const set = (s) => {
    setServer(s);
    try { if (s) sessionStorage.setItem("ifd_scope", s); else sessionStorage.removeItem("ifd_scope"); } catch { /* storage unavailable */ }
  };
  return <ScopeCtx.Provider value={{ server, setServer: set }}>{children}</ScopeCtx.Provider>;
}
