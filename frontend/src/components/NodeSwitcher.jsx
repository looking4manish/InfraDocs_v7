import { useState, useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Server } from "lucide-react";
import { endpoints } from "../api/client";
import { cn } from "../lib/cn";
import { getSelectedNode, setSelectedNode, setLeader, useSelectedNode } from "../lib/fleet";

// Header dropdown to view/act on any roster node from the single primary URL. A per-node
// status dot (green=reachable, amber=stale/unreachable) shows fleet health before selecting.
// Degrades to a static label on a single-node install (no dropdown, no stale confusion).
export default function NodeSwitcher() {
  const qc = useQueryClient();
  const sel = useSelectedNode();
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const q = useQuery({
    queryKey: ["fleet-nodes"],
    queryFn: () => endpoints.fleetNodes().then((r) => r.data),
    refetchInterval: 5000,
  });
  const nodes = q.data?.nodes || [];
  const leader = q.data?.leader;
  useEffect(() => { if (leader) setLeader(leader); }, [leader]);
  useEffect(() => {
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  if (nodes.length <= 1) {
    return (
      <span className="inline-flex items-center gap-1.5 text-[13px] text-zinc-300">
        <Server size={13} className="text-zinc-500" />
        {nodes[0]?.node_id || "this node"}
      </span>
    );
  }

  const current = sel || leader;
  const curNode = nodes.find((n) => n.node_id === current);
  const dot = (n) => (n.reachable ? "bg-emerald-400" : "bg-amber-400");
  const pick = (id) => { setSelectedNode(id); qc.clear(); setOpen(false); };

  return (
    <div className="relative" ref={ref}>
      <button onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-2 px-2.5 py-1.5 rounded-md border border-white/10 bg-bg-card hover:bg-bg-elev text-sm text-ink transition">
        <span className={cn("w-2 h-2 rounded-full", curNode ? dot(curNode) : "bg-zinc-500")} />
        <span className="font-medium">{current}</span>
        {curNode?.is_leader && <span className="text-[11px] text-zinc-400">this node</span>}
        {curNode && !curNode.reachable && <span className="text-[11px] text-amber-300">stale</span>}
        <ChevronDown size={13} className="text-zinc-500" />
      </button>
      {open && (
        <div className="absolute left-0 top-11 z-50 w-72 rounded-lg bg-bg-panel border border-white/10 shadow-2xl overflow-hidden">
          <div className="px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-400 border-b border-white/10">View node</div>
          {nodes.map((n) => (
            <button key={n.node_id} onClick={() => pick(n.node_id)}
              className={cn("w-full flex items-center gap-2 px-3 py-2.5 text-sm text-left hover:bg-white/5",
                n.node_id === current && "bg-white/5")}>
              <span className={cn("w-2 h-2 rounded-full shrink-0", dot(n))} />
              <span className="font-medium text-ink">{n.node_id}</span>
              {n.is_leader && <span className="text-[11px] text-zinc-400">this node</span>}
              {n.is_primary && !n.is_leader && <span className="text-[11px] text-accent-soft">primary</span>}
              <span className="ml-auto text-[11px]">
                {n.reachable ? <span className="text-emerald-300">reachable</span> : <span className="text-amber-300">stale</span>}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
