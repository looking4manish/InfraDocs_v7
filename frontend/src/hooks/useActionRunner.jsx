import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ACTION_META, MUTATING } from "../lib/actions";
import { ConfirmModal } from "../components/ui";
import ActionOutput from "../components/ActionOutput";
import { errDetail } from "../lib/format";

// Runs one action: mutating actions confirm first; the result opens in a modal.
// Returns { run, busy, ui } — render `ui` once next to whatever triggers `run`.
export function useActionRunner(invalidateKeys = []) {
  const qc = useQueryClient();
  const [job, setJob] = useState(null);       // { action, fire, target, label }
  const [asking, setAsking] = useState(false);
  const [output, setOutput] = useState(null);
  const mut = useMutation({
    mutationFn: (j) => j.fire(),
    onSuccess: (res) => {
      setAsking(false);
      setOutput(res?.data || {});
      for (const k of invalidateKeys) qc.invalidateQueries({ queryKey: k });
      qc.invalidateQueries({ queryKey: ["actions-list"] });
    },
    onError: (e) => { setAsking(false); setOutput({ status: "failed", stderr: errDetail(e) }); },
  });
  const run = (j) => {
    setJob(j);
    if (MUTATING.has(j.action)) setAsking(true);
    else mut.mutate(j);
  };
  const label = job ? job.label || ACTION_META[job.action]?.label || job.action : "";
  const destructive = job && ["stop", "down", "prune"].includes(job.action);
  const ui = (
    <>
      {asking && job && (
        <ConfirmModal title={`${label} ${job.target || ""}`.trim()} icon={ACTION_META[job.action]?.icon}
          actionLabel={label} danger={destructive} busy={mut.isPending}
          onConfirm={() => mut.mutate(job)} onClose={() => setAsking(false)}>
          <div>This runs <span className="mono" style={{ color: "var(--fg)" }}>{job.action}</span> on the live server.</div>
        </ConfirmModal>
      )}
      {output && job && <ActionOutput action={label} target={job.target} output={output} onClose={() => setOutput(null)} />}
    </>
  );
  return { run, busy: mut.isPending, busyAction: mut.isPending ? job?.action : null, ui };
}
