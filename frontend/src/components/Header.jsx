import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { RefreshCw, Search, TriangleAlert, CircleAlert, LogOut, Network } from "lucide-react";
import { endpoints, clearToken } from "../api/client";
import { cn } from "../lib/cn";
import {
  computeAttention, freshness, normalizeList, parseScanTime,
} from "../lib/attention";
import { useDrawer } from "./DrawerProvider";
import ThemeSwitcher from "./ThemeSwitcher";
import NodeSwitcher from "./NodeSwitcher";
import { useSelectedNode } from "../lib/fleet";

function openPalette() {
  window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }));
}

export default function Header() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { openDrawer } = useDrawer();
  const [showAttn, setShowAttn] = useState(false);

  const triggerScan = useMutation({
    mutationFn: () => endpoints.triggerScan(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scans"] }),
  });

  const scans = useQuery({
    queryKey: ["scans", "latest"],
    queryFn: () => endpoints.listScans(1).then((r) => r.data),
    refetchInterval: 60000,
  });
  const apps = useQuery({
    queryKey: ["applications", "attention"],
    queryFn: () => endpoints.listApplications().then((r) => r.data),
    refetchInterval: 120000,
  });
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => endpoints.health().then((r) => r.data),
    staleTime: 300000,
  });
  const selNode = useSelectedNode();
  const fleet = useQuery({
    queryKey: ["fleet-nodes"],
    queryFn: () => endpoints.fleetNodes().then((r) => r.data),
    refetchInterval: 5000,
  });
  const _fcur = selNode || fleet.data?.leader;
  const fleetStale = !!(fleet.data?.nodes || []).find((n) => n.node_id === _fcur && !n.reachable);
  const serverName = health.data?.server_name || health.data?.server || "server";

  const lastScanAt = parseScanTime(
    normalizeList(scans.data, ["scans", "items", "results"])[0]
  );
  const appList = normalizeList(apps.data, ["applications", "items", "results", "apps"]);
  const attention = computeAttention(appList, lastScanAt);
  const crit = attention.filter((i) => i.sev === 3).length;
  const fresh = freshness(lastScanAt);

  return (
    <header className="h-14 shrink-0 flex items-center gap-3 px-4 border-b border-bg-hover bg-bg-panel">
      <button
        onClick={() => navigate("/")}
        title="Home"
        className="group flex items-baseline gap-1.5 select-none rounded-md -mx-1 px-1 transition hover:bg-bg-elev"
      >
        <span className="text-[15px] font-semibold tracking-tight text-zinc-50">InfraDocs</span>
        <span className="text-[11px] text-zinc-500 group-hover:text-accent-soft font-mono transition">v7</span>
      </button>

      <NodeSwitcher />

      <span className={cn(
        "inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-medium",
        fresh.level === "ok" && "bg-emerald-500/10 text-emerald-400",
        fresh.level === "warn" && "bg-amber-500/10 text-amber-400",
        fresh.level === "bad" && "bg-red-500/10 text-red-400"
      )}>
        <span className={cn(
          "w-1.5 h-1.5 rounded-full",
          fresh.level === "ok" && "bg-emerald-400",
          fresh.level === "warn" && "bg-amber-400",
          fresh.level === "bad" && "bg-red-400"
        )} />
        {serverName} · {fresh.label}
      </span>

      <button
        onClick={openPalette}
        className="group relative flex-1 max-w-md flex items-center gap-2.5 bg-bg-card border border-bg-hover hover:border-zinc-700 rounded-md pl-3 pr-2 py-1.5 text-sm text-zinc-500 transition text-left"
      >
        <Search size={14} className="text-zinc-600 group-hover:text-zinc-500 transition" />
        <span className="flex-1 truncate">Search applications, assets, pages…</span>
        <kbd className="text-[10px] text-zinc-500 border border-bg-hover rounded px-1.5 py-0.5 font-mono">⌘K</kbd>
      </button>

      <div className="relative ml-auto">
        {attention.length > 0 && (
          <button
            onClick={() => setShowAttn((s) => !s)}
            className={cn(
              "inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] font-medium transition border",
              crit > 0
                ? "bg-red-500/10 text-red-400 border-red-500/25 hover:bg-red-500/15"
                : "bg-amber-500/10 text-amber-400 border-amber-500/25 hover:bg-amber-500/15"
            )}
          >
            {crit > 0 ? <CircleAlert size={13} /> : <TriangleAlert size={13} />}
            {attention.length}
          </button>
        )}
        {showAttn && (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setShowAttn(false)} />
            <div className="absolute right-0 top-11 z-50 w-[380px] rounded-xl bg-bg-panel border border-bg-hover shadow-2xl overflow-hidden">
              <div className="px-4 py-2.5 text-[10px] font-medium uppercase tracking-[0.08em] text-zinc-600 border-b border-bg-hover">
                needs attention · {attention.length}
              </div>
              <div className="max-h-[50vh] overflow-y-auto py-1">
                {attention.map((it, i) => (
                  <button
                    key={i}
                    onClick={() => {
                      if (it.app) openDrawer({ type: "application", name: it.app });
                      setShowAttn(false);
                    }}
                    className="w-full flex items-start gap-2.5 px-4 py-2.5 text-left hover:bg-white/[0.04] transition"
                  >
                    <span className={cn(
                      "mt-1.5 w-1.5 h-1.5 rounded-full shrink-0",
                      it.sev === 3 ? "bg-red-400" : it.sev === 2 ? "bg-amber-400" : "bg-sky-400"
                    )} />
                    <span className="flex-1 text-[12.5px] text-zinc-300 leading-snug">
                      {it.app && <span className="text-zinc-500 font-mono">{it.app} · </span>}
                      {it.text}
                    </span>
                    <span className="text-[10px] text-zinc-600 font-mono mt-0.5 shrink-0">{it.kind}</span>
                  </button>
                ))}
              </div>
            </div>
          </>
        )}
      </div>

      <ThemeSwitcher />

      <button
        onClick={() => navigate("/admin")}
        title="Admin · Cluster"
        className="p-1.5 rounded-md text-zinc-500 hover:text-accent-soft hover:bg-bg-elev transition"
      >
        <Network size={15} />
      </button>

      <button
        onClick={async () => {
          try { await endpoints.logout(); } catch { /* ignore */ }
          clearToken();
          window.location.reload();
        }}
        title="Sign out"
        className="p-1.5 rounded-md text-zinc-500 hover:text-zinc-200 hover:bg-bg-elev transition"
      >
        <LogOut size={15} />
      </button>

      <button
        onClick={() => triggerScan.mutate()}
        disabled={triggerScan.isPending || fleetStale}
        title={fleetStale ? "node unreachable — actions unavailable" : "Scan the selected node"}
        className="inline-flex items-center gap-1.5 bg-accent hover:bg-accent-dim text-bg-base text-sm font-semibold px-3 py-1.5 rounded-md disabled:opacity-50 transition"
      >
        <RefreshCw size={14} className={cn(triggerScan.isPending && "animate-spin")} />
        {triggerScan.isPending ? "Scanning…" : "Scan now"}
      </button>

      {triggerScan.isSuccess && (
        <span className="text-xs text-zinc-500 font-mono">
          queued {triggerScan.data.data.scan_id.slice(0, 8)}
        </span>
      )}
    </header>
  );
}
