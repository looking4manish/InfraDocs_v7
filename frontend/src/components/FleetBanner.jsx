import { useQuery } from "@tanstack/react-query";
import { endpoints } from "../api/client";
import { useSelectedNode, relTime, peerAuthFailed } from "../lib/fleet";

// The HARD-REQUIREMENT visual cue: it must be impossible to mistake stale/unreachable data
// for live. Viewing this node → nothing. Viewing another reachable node → subtle live badge.
// Viewing an unreachable node → prominent amber banner + "live actions are disabled".
export default function FleetBanner() {
  const sel = useSelectedNode();
  const q = useQuery({
    queryKey: ["fleet-nodes"],
    queryFn: () => endpoints.fleetNodes().then((r) => r.data),
    refetchInterval: 5000,
  });
  const nodes = q.data?.nodes || [];
  const leader = q.data?.leader;
  const current = sel || leader;
  if (!current || current === leader || nodes.length <= 1) return null;
  const node = nodes.find((n) => n.node_id === current);
  if (!node) return null;

  const authFail = peerAuthFailed(current);
  if (authFail) {
    return (
      <div className="mb-4 px-3.5 py-2.5 rounded-lg border border-amber-500/50 bg-amber-500/10 text-sm text-amber-100 flex items-start gap-2">
        <span className="mt-0.5">⚠</span>
        <span>
          <span className="font-semibold">{current}</span> — peer authentication failed (the node
          rejected this fleet's credential). Showing last-known data. Live actions are disabled.
          Run <span className="font-mono">update.sh</span> on {current} to sync the shared fleet secret.
        </span>
      </div>
    );
  }
  if (node.reachable) {
    return (
      <div className="mb-4 px-3.5 py-2 rounded-lg border border-[var(--neon)]/45 bg-[var(--neon)]/10 text-sm text-ink flex items-center gap-2">
        <span className="w-2 h-2 rounded-full bg-emerald-400" />
        Viewing <span className="font-semibold">{current}</span> · live
      </div>
    );
  }
  return (
    <div className="mb-4 px-3.5 py-2.5 rounded-lg border border-amber-500/50 bg-amber-500/10 text-sm text-amber-100 flex items-start gap-2">
      <span className="mt-0.5">⚠</span>
      <span>
        <span className="font-semibold">{current}</span> is unreachable — showing last-known data
        from {relTime(node.last_synced_at)}. Live actions are disabled.
      </span>
    </div>
  );
}
