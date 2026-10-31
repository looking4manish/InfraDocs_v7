// Shared react-query hooks — one cache entry per API resource.
import { useQuery } from "@tanstack/react-query";
import { endpoints } from "../api/client";

export function useApps() {
  return useQuery({
    queryKey: ["applications"],
    queryFn: () => endpoints.listApplications().then((r) => r.data.applications || []),
    refetchInterval: 30000,
  });
}

export function useCluster() {
  return useQuery({
    queryKey: ["cluster-state"],
    queryFn: () => endpoints.clusterState().then((r) => r.data),
    refetchInterval: 5000,
    retry: 0,
  });
}

export function useEndpoints() {
  return useQuery({
    queryKey: ["endpoints"],
    queryFn: () => endpoints.listEndpoints().then((r) => r.data.endpoints || []),
    refetchInterval: 60000,
  });
}

export function useActions(limit = 50) {
  return useQuery({
    queryKey: ["actions-list", limit],
    queryFn: () => endpoints.listActions({ limit }).then((r) => r.data.actions || []),
    refetchInterval: 15000,
  });
}

export function useScans(limit = 5) {
  return useQuery({
    queryKey: ["scans", limit],
    queryFn: () => endpoints.listScans(limit).then((r) => r.data.scans || []),
    refetchInterval: 30000,
  });
}

// Distinct servers seen in the fleet: cluster members first, then any server id in the data.
export function serverList(cluster, apps) {
  const ids = [];
  for (const m of cluster?.members || []) if (m.node_id && !ids.includes(m.node_id)) ids.push(m.node_id);
  for (const a of apps || []) {
    const id = a.server_id || (a.application_id || "").split(":app:")[0];
    if (id && !ids.includes(id)) ids.push(id);
  }
  return ids;
}
