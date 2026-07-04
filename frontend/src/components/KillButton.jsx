import { useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Skull, ShieldAlert, Loader2, Archive, Check, X, AlertTriangle,
} from "lucide-react";
import { endpoints } from "../api/client";
import { cn } from "../lib/cn";

// The Kill Button. Two-step: dry-run plan -> type-to-confirm -> execute, with a
// live log window that streams each teardown step (polled from the audit log).
export default function KillButton({ name }) {
  const [plan, setPlan] = useState(null);
  const [confirm, setConfirm] = useState("");
  const [result, setResult] = useState(null);
  const [startedAt, setStartedAt] = useState(null); // ms; non-null = log window open

  const planMut = useMutation({
    mutationFn: () => endpoints.teardown(name, { dry_run: true }).then((r) => r.data),
    onSuccess: (d) => { setPlan(d); setResult(null); setConfirm(""); },
  });
  const killMut = useMutation({
    mutationFn: () => endpoints.teardown(name, { dry_run: false, confirm: name }).then((r) => r.data),
    onMutate: () => { setStartedAt(Date.now()); setResult(null); },
    onSuccess: setResult,
    onError: (e) => setResult({
      error: true,
      detail: e?.response?.data?.detail || e?.message || "request failed",
    }),
  });

  // Live log: poll the audit log for this teardown's steps while it runs.
  const killing = killMut.isPending;
  const logQ = useQuery({
    queryKey: ["teardown-log", name, startedAt],
    queryFn: () => endpoints.listActions({ limit: 80 }).then((r) => r.data),
    enabled: Boolean(startedAt),
    refetchInterval: killing ? 1000 : false,
  });
  const logRows = (logQ.data?.actions || [])
    .filter((a) =>
      String(a.action || "").startsWith("teardown:") &&
      a.project === name &&
      new Date(a.timestamp).getTime() >= startedAt - 3000
    )
    .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));

  const refused = plan?.refusals?.length > 0;
  const armed = confirm === name && !refused;

  function fireKill() {
    if (!armed) return;
    const ok = window.confirm(
      `PERMANENTLY tear down "${name}".\n\nA backup is written to ` +
      `~/projects/backups/${name}/ first, then containers, nginx, volumes and the ` +
      `project directory are removed. Shared assets are skipped.\n\nThis cannot be undone. Continue?`
    );
    if (ok) killMut.mutate();
  }

  return (
    <div className="mt-4 rounded-lg border border-red-500/30 bg-red-500/[0.04] p-3">
      <div className="flex items-center gap-2 mb-2">
        <Skull size={14} className="text-red-400" />
        <span className="text-[13px] font-semibold text-red-300">Danger zone — Kill {name}</span>
      </div>

      {!plan && (
        <button
          onClick={() => planMut.mutate()}
          disabled={planMut.isPending}
          className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-md border border-red-500/30 text-red-300 hover:bg-red-500/10 transition disabled:opacity-50"
        >
          {planMut.isPending ? <Loader2 size={12} className="animate-spin" /> : <ShieldAlert size={12} />}
          Plan teardown (dry-run)
        </button>
      )}
      {planMut.isError && (
        <div className="text-[12px] text-amber-300/90 mt-1">
          Teardown endpoint unavailable — the API may need a restart to expose it.
        </div>
      )}

      {plan && (
        <div className="space-y-2.5">
          {refused ? (
            <div className="text-[12.5px] text-red-300">Refused: {plan.refusals.join("; ")}</div>
          ) : (
            <>
              <div className="text-[11px] text-zinc-500 flex items-center gap-1.5">
                <Archive size={11} /> backup → <code className="text-zinc-400">~/projects/backups/{name}/&lt;timestamp&gt;/</code>
              </div>
              <ol className="text-[12px] font-mono space-y-0.5">
                {plan.ops.map((o, i) => (
                  <li key={i} className="flex items-center gap-2">
                    <span className="text-zinc-600 w-4 text-right">{i + 1}</span>
                    <span className={cn(
                      "px-1.5 py-px rounded text-[10px]",
                      o.critical ? "bg-accent-cyan/15 text-accent-cyan"
                        : o.data_loss ? "bg-red-500/15 text-red-300"
                        : "bg-white/[0.06] text-zinc-400"
                    )}>{o.op}</span>
                    <span className="text-zinc-400 truncate">{o.label}</span>
                  </li>
                ))}
              </ol>
              {plan.skipped?.length > 0 && (
                <div className="text-[11px] text-amber-300/80">
                  Skipped (shared/protected): {plan.skipped.map((s) => s.name).join(", ")}
                </div>
              )}
              <div className="flex items-center gap-2 pt-1">
                <input
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  placeholder={`type "${name}" to confirm`}
                  className="bg-bg-base border border-red-500/30 rounded px-2 py-1 text-xs font-mono w-56 focus:outline-none focus:border-red-500/60"
                />
                <button
                  onClick={fireKill}
                  disabled={!armed || killing}
                  className={cn(
                    "inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md font-semibold transition",
                    armed ? "bg-red-500/80 text-white hover:bg-red-500"
                          : "bg-red-500/15 text-red-300/50 cursor-not-allowed"
                  )}
                >
                  {killing ? <Loader2 size={12} className="animate-spin" /> : <Skull size={12} />}
                  Kill {name}
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {startedAt &&
        createPortal(
          <KillLogModal
            name={name}
            rows={logRows}
            killing={killing}
            result={result}
            onClose={() => { setStartedAt(null); setResult(null); }}
          />,
          document.body
        )}
    </div>
  );
}

function statusIcon(s) {
  if (s === "success") return <Check size={12} className="text-emerald-400 shrink-0" />;
  if (s === "skipped") return <span className="text-zinc-600 w-3 text-center shrink-0">·</span>;
  return <X size={12} className="text-red-400 shrink-0" />;
}

function KillLogModal({ name, rows, killing, result, onClose }) {
  return (
    <div className="fixed inset-0 z-[80] bg-black/65 backdrop-blur-[2px] flex items-center justify-center p-4">
      <div className="w-full max-w-2xl max-h-[80vh] flex flex-col rounded-xl border border-red-500/30 bg-bg-panel shadow-2xl">
        <div className="flex items-center gap-2 px-4 py-3 border-b border-bg-hover">
          {killing
            ? <Loader2 size={15} className="text-red-400 animate-spin" />
            : <Skull size={15} className="text-red-400" />}
          <span className="text-sm font-semibold">
            {killing ? `Killing ${name}…` : `Teardown of ${name}`}
          </span>
          {!killing && (
            <button onClick={onClose} className="ml-auto p-1 rounded text-zinc-500 hover:text-zinc-200 hover:bg-white/[0.06]">
              <X size={15} />
            </button>
          )}
        </div>

        <div className="flex-1 overflow-y-auto p-4 font-mono text-[12px] space-y-1">
          {rows.length === 0 && (
            <div className="text-zinc-500">Starting teardown… backing up first.</div>
          )}
          {rows.map((r, i) => (
            <div key={i} className="flex items-start gap-2">
              {statusIcon(r.status)}
              <span className="text-zinc-500">{String(r.action).replace("teardown:", "")}</span>
              <span className="text-zinc-300 truncate">{r.asset_name}</span>
              {r.stderr && <span className="text-red-300/70 truncate">— {r.stderr.split("\n")[0]}</span>}
            </div>
          ))}
          {killing && rows.length > 0 && (
            <div className="flex items-center gap-2 text-zinc-500">
              <Loader2 size={11} className="animate-spin" /> running…
            </div>
          )}
        </div>

        <div className="px-4 py-3 border-t border-bg-hover text-[12px]">
          {killing ? (
            <span className="text-zinc-500">Do not close — teardown in progress.</span>
          ) : result?.error ? (
            <span className="text-red-300 flex items-start gap-1.5"><AlertTriangle size={13} className="mt-0.5 shrink-0" /> <span>{result.detail || "Request failed — see the actions log."}</span></span>
          ) : result?.aborted ? (
            <span className="text-amber-300 flex items-start gap-1.5"><AlertTriangle size={13} className="mt-0.5 shrink-0" /> <span>Aborted — nothing was deleted.{result.summary?.failed?.length ? " " + result.summary.failed.map((f) => f.reason).join("; ") : ""}</span></span>
          ) : result ? (
            <span className="text-emerald-300 flex items-center gap-1.5">
              <Check size={13} /> Done · backup at <code className="text-zinc-400">{result.backup_dir}</code>
            </span>
          ) : (
            <span className="text-zinc-500">Finishing…</span>
          )}
        </div>
      </div>
    </div>
  );
}
