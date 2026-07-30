import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "../../api/client";
import { cn } from "../../lib/cn";

const SCOPE_STYLE = {
  public: "bg-emerald-500/15 text-emerald-300",
  tailnet: "bg-cyan-500/15 text-cyan-300",
  localhost: "bg-zinc-500/15 text-zinc-300",
  "all-interfaces": "bg-amber-500/15 text-amber-300",
  "private-lan": "bg-violet-500/15 text-violet-300",
  private: "bg-zinc-500/15 text-zinc-300",
};
const KIND_STYLE = {
  web: "bg-accent/15 text-accent-soft",
  database: "bg-rose-500/15 text-rose-300",
  monitoring: "bg-amber-500/15 text-amber-300",
  infra: "bg-zinc-500/15 text-zinc-400",
};

function AIControls() {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["ai-status"], queryFn: () => endpoints.aiStatus().then((r) => r.data) });
  const insightsQ = useQuery({ queryKey: ["ai-insights"], queryFn: () => endpoints.getAiInsights().then((r) => r.data) });
  const [msg, setMsg] = useState("");
  const enrich = useMutation({
    mutationFn: () => endpoints.aiEnrich().then((r) => r.data),
    onSuccess: (d) => {
      setMsg(d.enabled ? `Labeling ${d.scheduled} unknown service(s) in the background…` : d.message);
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ["endpoints"] });
        qc.invalidateQueries({ queryKey: ["ai-status"] });
      }, 7000);
    },
  });
  const runInsights = useMutation({
    mutationFn: () => endpoints.runAiInsights().then((r) => r.data),
    onSuccess: () => insightsQ.refetch(),
  });
  if (!status.data) return null;
  const insights = insightsQ.data?.insights;
  const btn = "text-[12px] px-3 py-1.5 rounded-lg disabled:opacity-50 transition";
  return (
    <div className="mb-4">
      {!status.data.enabled ? (
        <div className="text-[12px] text-zinc-500">
          AI labeling off — add an OpenAI-compatible endpoint in Setup to auto-identify unknown services.
        </div>
      ) : (
        <div className="flex items-center gap-2 flex-wrap">
          <button onClick={() => enrich.mutate()} disabled={enrich.isPending}
            className={cn(btn, "neon-glow border border-[var(--neon)] bg-[var(--neon)]/10 hover:bg-[var(--neon)]/20")}>
            {enrich.isPending ? "…" : "✨ Label unknowns (AI)"}
          </button>
          <button onClick={() => runInsights.mutate()} disabled={runInsights.isPending}
            className={cn(btn, "border border-white/10 hover:bg-bg-elev text-zinc-300")}>
            {runInsights.isPending ? "Analyzing…" : "🧠 Fleet insights"}
          </button>
          <span className="text-[11px] text-zinc-500">{status.data.labeled} AI-labeled · {status.data.model}</span>
          {msg && <span className="text-[11px] text-accent-soft">{msg}</span>}
        </div>
      )}
      {insights && (
        <div className="neon-panel rounded-xl p-4 mt-3 text-sm">
          <div className="text-zinc-200">{insights.summary}</div>
          {insights.observations?.length > 0 && (
            <ul className="list-disc pl-5 mt-2 space-y-1 text-[12.5px] text-zinc-400">
              {insights.observations.map((o, i) => <li key={i}>{o}</li>)}
            </ul>
          )}
          {insights.recommendations?.length > 0 && (
            <div className="mt-2 text-[12.5px]">
              <span className="text-amber-300">Recommendations:</span>
              <ul className="list-disc pl-5 mt-1 space-y-1 text-zinc-400">
                {insights.recommendations.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function WebLens() {
  const q = useQuery({
    queryKey: ["endpoints"],
    queryFn: () => endpoints.listEndpoints().then((r) => r.data),
  });
  const eps = q.data?.endpoints || [];
  return (
    <div>
      <div className="flex items-baseline gap-3 mb-1">
        <h1 className="text-[21px] font-semibold tracking-tight">Web &amp; Services</h1>
        <span className="text-[13px] text-zinc-500">{eps.length} reachable</span>
      </div>
      <p className="text-[13px] text-zinc-500 mb-4">
        Every hosted page / UI / service across the fleet — clickable, with where it
        lives and how it's reached.
      </p>
      <AIControls />
      {q.isLoading && <div className="text-zinc-400 text-sm">Loading…</div>}
      <div className="space-y-2">
        {eps.map((e, i) => (
          <div key={i} className="neon-panel rounded-xl px-4 py-3 flex items-start gap-3">
            <span className={cn("text-[10px] px-2 py-0.5 rounded-full shrink-0 mt-0.5 font-medium",
              KIND_STYLE[e.kind] || KIND_STYLE.infra)}>{e.kind}</span>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                {e.url ? (
                  <a href={e.url} target="_blank" rel="noreferrer"
                     className="text-accent-soft hover:underline font-medium font-mono text-[13px] truncate">
                    {e.url}
                  </a>
                ) : (
                  <span className="font-medium font-mono text-[13px] text-zinc-300">{e.host}</span>
                )}
                <span className={cn("text-[10px] px-1.5 py-0.5 rounded-full shrink-0",
                  SCOPE_STYLE[e.scope] || SCOPE_STYLE.private)}>{e.scope}</span>
                {e.ai && <span className="text-[10px] px-1.5 py-0.5 rounded-full shrink-0 bg-[var(--neon)]/15 text-[var(--neon)]">AI</span>}
              </div>
              <div className="text-xs text-zinc-400 mt-1">
                <span className="text-zinc-200">{e.service}</span>
                {e.recognized && e.recognized !== e.service && <span className="text-zinc-500"> · {e.recognized}</span>}
                <span className="text-zinc-600"> · {e.server}</span>
                {e.via && <span className="text-zinc-600"> · via {e.via}</span>}
              </div>
              {e.purpose && <div className="text-[11px] text-[var(--neon)]/80 mt-1">{e.purpose}</div>}
              <div className="text-[11px] text-zinc-500 mt-1">{e.access}</div>
            </div>
          </div>
        ))}
        {!q.isLoading && eps.length === 0 && (
          <div className="text-sm text-zinc-500">No endpoints found yet — run a scan.</div>
        )}
      </div>
    </div>
  );
}
