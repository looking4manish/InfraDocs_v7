import { createContext, useContext } from "react";

// Server scope chosen in the top bar. null = all servers.
export const ScopeCtx = createContext({ server: null, setServer: () => {} });

export function useScope() {
  return useContext(ScopeCtx);
}
