import axios from "axios";
import { getSelectedNode, isLeaderSelected, setPeerAuthFailed } from "../lib/fleet";

// Session-token auth: login → opaque token → sent as `Authorization: Bearer`.
// No credentials ever live in the JS bundle or persist beyond the token.
const TOKEN_KEY = "ifd_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}
export function setToken(t) {
  localStorage.setItem(TOKEN_KEY, t);
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}
export function isAuthed() {
  return !!getToken();
}

export const api = axios.create({
  baseURL: "",
  timeout: 30000,
});

api.interceptors.request.use((config) => {
  const t = getToken();
  if (t) config.headers.Authorization = `Bearer ${t}`;
  return config;
});

// Fleet node-switcher: when viewing a NON-leader node, repoint the data lenses through the
// primary-side proxy (/api/fleet/<node>/...). Only lens/data + action routes are repointed;
// auth/setup/cluster/fleet/federation/health always hit the node actually serving the UI.
const FLEET_LENS = /^\/api\/(applications|assets|projects|ports|storage|scans|endpoints|ai)(\/|\?|$)/;
api.interceptors.request.use((config) => {
  const node = getSelectedNode();
  if (node && !isLeaderSelected() && typeof config.url === "string" && FLEET_LENS.test(config.url)) {
    config.url = `/api/fleet/${encodeURIComponent(node)}${config.url}`;
  }
  return config;
});

// On 401, drop the (now invalid/expired) token and let the app fall back to
// the login screen instead of looping with a dead session.
api.interceptors.response.use(
  (r) => {
    // A proxied fleet call whose peer rejected our credential flags the node so the
    // FleetBanner can explain it — WITHOUT ever treating it as the operator's session dying.
    if (r?.headers?.["x-fleet-auth-failed"]) {
      const m = /^\/api\/fleet\/([^/]+)\//.exec(r.config?.url || "");
      if (m) setPeerAuthFailed(decodeURIComponent(m[1]), true);
    }
    return r;
  },
  (err) => {
    const url = err?.config?.url || "";
    const isFleet = url.startsWith("/api/fleet/");
    // A 401 from a PROXIED peer call is not our session expiring — never log out / redirect
    // (the fleet view falls back to stale). Only a 401 on a normal call ends the session.
    if (err?.response?.status === 401 && getToken() && !isFleet) {
      clearToken();
      window.dispatchEvent(new Event("ifd-unauthorized"));
    }
    return Promise.reject(err);
  }
);

export const endpoints = {
  health: () => api.get("/api/health"),

  // auth
  login: (username, password) =>
    api.post("/api/auth/login", { username, password }),
  me: () => api.get("/api/auth/me"),
  changePassword: (new_password) =>
    api.post("/api/auth/change-password", { new_password }),
  logout: () => api.post("/api/auth/logout"),

  // first-run setup wizard
  setupStatus: () => api.get("/api/setup/status"),
  detectIp: () => api.get("/api/setup/detect-ip"),
  completeSetup: (body) => api.post("/api/setup/complete", body),

  // assets (Phase 3)
  listAssets: (params = {}) => api.get("/api/assets/", { params }),
  assetCategories: () => api.get("/api/assets/categories"),
  getAsset: (id) => api.get(`/api/assets/${encodeURIComponent(id)}`),

  // projects (Phase 3)
  listProjects: () => api.get("/api/projects/list"),
  getProject: (name) => api.get(`/api/projects/${encodeURIComponent(name)}`),

  // web endpoints / UIs
  listEndpoints: (server) =>
    api.get("/api/endpoints", { params: server ? { server } : {} }),

  // federation (multi-server)
  federationServers: () => api.get("/api/federation/servers"),
  mintFederationToken: (server_id) =>
    api.post("/api/federation/tokens", { server_id }),
  fleetNodes: () => api.get("/api/fleet/nodes"),
  clusterState: () => api.get("/api/cluster/state"),
  clusterPromote: (force = false) =>
    api.post("/api/cluster/promote", { force }),
  clusterOverride: (value) =>
    api.post("/api/cluster/override", { value }),
  // Admin / Cluster tab — role-transition matrix, enable toggle, evict, tokens, audit.
  clusterEnable: (value) => api.post("/api/cluster/enable", { value }),
  clusterAutoFailover: (value) => api.post("/api/cluster/auto-failover", { value }),
  clusterToPrimary: (priority) => api.post("/api/cluster/to-primary", { priority }),
  clusterJoin: (body) => api.post("/api/cluster/join", body),
  clusterDemote: () => api.post("/api/cluster/demote"),
  clusterHandoff: (target) => api.post("/api/cluster/handoff", { target }),
  clusterToStandalone: (force, confirm_hostname) =>
    api.post("/api/cluster/to-standalone", { force, confirm_hostname }),
  clusterSetPriority: (priority) => api.post("/api/cluster/priority", { priority }),
  clusterEvict: (node_id, confirm_hostname) =>
    api.post("/api/cluster/evict", { node_id, confirm_hostname }),
  clusterAudit: (limit = 100) => api.get("/api/cluster/audit", { params: { limit } }),
  listFederationTokens: () => api.get("/api/federation/tokens"),
  revokeFederationToken: (token) =>
    api.delete(`/api/federation/tokens/${encodeURIComponent(token)}`),

  // AI layer
  aiStatus: () => api.get("/api/ai/status"),
  aiEnrich: (server) =>
    api.post("/api/ai/enrich", null, { params: server ? { server } : {} }),
  getAiInsights: () => api.get("/api/ai/insights"),
  runAiInsights: (server) =>
    api.post("/api/ai/insights", null, { params: server ? { server } : {} }),

  // applications (Phase 5+ correlated docs)
  listApplications: (params = {}) =>
    api.get("/api/applications/list", { params }),
  getApplication: (name) =>
    api.get(`/api/applications/${encodeURIComponent(name)}`),
  blastRadius: (name) =>
    api.get(`/api/applications/${encodeURIComponent(name)}/blast-radius`),
  teardown: (name, body) =>
    api.post(`/api/applications/${encodeURIComponent(name)}/teardown`, body),

  // ports registry (Phase 7B)
  listPorts: (params = {}) => api.get("/api/ports/", { params }),
  portsSummary: () => api.get("/api/ports/summary"),
  probePorts: (range, proto = "tcp") =>
    api.get("/api/ports/probe", { params: { range, proto } }),

  // storage registry (Phase 7C)
  listStorage: (params = {}) => api.get("/api/storage/", { params }),
  storageSummary: () => api.get("/api/storage/summary"),

  // scans (Phase 3)
  listScans: (limit = 25) => api.get("/api/scans/", { params: { limit } }),
  getScan: (id) => api.get(`/api/scans/${id}`),
  triggerScan: () => api.post("/api/scans/trigger"),

  // actions (Phase 8)
  allowedActions: () => api.get("/api/actions/allowed"),
  listActions: (params = {}) => api.get("/api/actions/", { params }),
  fireAssetAction: (assetId, action, args = {}) =>
    api.post(`/api/assets/${encodeURIComponent(assetId)}/action`, {
      action,
      args,
    }),
  fireApplicationAction: (name, action, args = {}) =>
    api.post(`/api/applications/${encodeURIComponent(name)}/action`, {
      action,
      args,
    }),
};
