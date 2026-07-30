import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "../api/client";

function StatusPill({ status }) {
  const tone =
    status === "success"
      ? "bg-emerald-500/20 text-emerald-300"
      : status === "running" || status === "queued"
      ? "bg-amber-500/20 text-amber-300"
      : status === "failed"
      ? "bg-rose-500/20 text-rose-300"
      : status === "degraded"
      ? "bg-amber-500/20 text-amber-300"
      : "bg-slate-500/20 text-slate-300";
  return <span className={`text-xs px-2 py-0.5 rounded ${tone}`}>{status}</span>;
}

export default function Scans() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["scans"],
    queryFn: () => endpoints.listScans(25).then((r) => r.data),
    refetchInterval: 3000,
  });

  const trigger = useMutation({
    mutationFn: () => endpoints.triggerScan(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scans"] }),
  });

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-xl font-semibold">Scans</h1>
        <button
          onClick={() => trigger.mutate()}
          disabled={trigger.isPending}
          className="bg-accent hover:bg-accent-dim disabled:opacity-50 text-bg-base font-semibold text-sm px-3 py-1.5 rounded"
        >
          {trigger.isPending ? "Queueing…" : "Run scan"}
        </button>
      </div>

      <div className="neon-panel rounded-lg overflow-hidden">
        <div className="grid grid-cols-12 gap-3 px-3 py-2 text-xs uppercase tracking-wide text-slate-500 border-b border-bg-hover">
          <div className="col-span-3">Scan</div>
          <div className="col-span-2">Status</div>
          <div className="col-span-2">Assets</div>
          <div className="col-span-2">Duration</div>
          <div className="col-span-3">When</div>
        </div>
        {q.data?.scans?.map((s) => (
          <div
            key={s._id}
            className="grid grid-cols-12 gap-3 px-3 py-2 border-b border-bg-card text-sm"
          >
            <div className="col-span-3 font-mono text-xs">
              {(s.scan_id || s._id || "").slice(0, 12)}
            </div>
            <div className="col-span-2">
              <StatusPill status={s.status || "?"} />
            </div>
            <div className="col-span-2 text-slate-300">
              {s.total_assets ?? "—"}
            </div>
            <div className="col-span-2 text-slate-400">
              {s.duration_seconds ? `${s.duration_seconds.toFixed(1)}s` : "—"}
            </div>
            <div className="col-span-3 text-slate-400 text-xs">
              {s.started_at
                ? new Date(s.started_at).toLocaleString()
                : s.created_at
                ? new Date(s.created_at).toLocaleString()
                : "—"}
            </div>
          </div>
        ))}
        {q.data?.scans?.length === 0 && (
          <div className="p-4 text-sm text-slate-400">No scans yet.</div>
        )}
      </div>
    </div>
  );
}
