import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2, Archive, CircleCheck, CircleX, TriangleAlert, Loader2 } from "lucide-react";
import { endpoints } from "../api/client";
import { errDetail } from "../lib/format";
import { Btn, Modal } from "./ui";

const OP_LABEL = {
  remove_container: "Remove container", compose_down: "Compose down", disable_unit: "Disable service",
  remove_nginx: "Remove nginx site", reload_nginx: "Reload nginx", remove_volume: "Remove volume",
  remove_dir: "Remove folder", backup_dir: "Back up folder", backup_volume: "Back up volume",
};

// Tear down a project: preview the plan, type its name, run it with a backup first.
export default function Teardown({ name, onClose }) {
  const qc = useQueryClient();
  const [typed, setTyped] = useState("");
  const plan = useQuery({
    queryKey: ["teardown-plan", name],
    queryFn: () => endpoints.teardown(name, { dry_run: true }).then((r) => r.data),
    retry: 0,
  });
  const run = useMutation({
    mutationFn: () => endpoints.teardown(name, { dry_run: false, confirm: name }).then((r) => r.data),
    onSuccess: () => qc.invalidateQueries(),
  });
  const p = plan.data;
  const refused = p?.refusals?.length > 0;
  const done = run.data;

  return (
    <Modal wide title={`Tear down ${name}`} icon={Trash2} onClose={onClose}
      footer={done ? <Btn primary onClick={onClose}>Close</Btn> : <>
        <Btn onClick={onClose}>Cancel</Btn>
        <Btn danger icon={Trash2} busy={run.isPending} disabled={!p || refused || typed !== name || run.isPending}
          onClick={() => run.mutate()}>Tear down</Btn>
      </>}>
      {plan.isLoading && <div style={{ display: "flex", gap: 10, alignItems: "center" }}><Loader2 size={16} className="spin" />Preparing the plan</div>}
      {plan.error && <div className="formerr">{errDetail(plan.error)}</div>}
      {p && !done && (
        <>
          <div>A backup is written to <span className="mono" style={{ color: "var(--fg)" }}>{p.projects_root}/backups/{name}/</span> first. Shared items are skipped.</div>
          {refused && <div className="formerr"><TriangleAlert size={16} />{p.refusals.join("; ")}</div>}
          <div>
            {(p.ops || []).map((o, i) => (
              <div className="oprow" key={i}>
                {o.op.startsWith("backup") ? <Archive size={15} color="var(--info)" /> : <Trash2 size={15} color={o.data_loss ? "var(--danger)" : "var(--subtle)"} />}
                <span><b style={{ color: "var(--fg)", fontWeight: 700 }}>{OP_LABEL[o.op] || o.op}</b> <span className="mono">{o.target}</span></span>
                {o.data_loss && <span className="chip danger"><span className="d" />Data</span>}
              </div>
            ))}
            {(p.skipped || []).map((s, i) => (
              <div className="oprow" key={`s${i}`}>
                <CircleCheck size={15} color="var(--hi)" />
                <span>Kept <span className="mono">{s.name || s.target || String(s)}</span></span>
                <span className="tag sans">Shared</span>
              </div>
            ))}
          </div>
          {!refused && (
            <div className="field">
              <label htmlFor="td-confirm">Type <span className="mono" style={{ color: "var(--fg)" }}>{name}</span> to confirm</label>
              <input id="td-confirm" className="input" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
            </div>
          )}
          {run.error && <div className="formerr">{errDetail(run.error)}</div>}
        </>
      )}
      {done && (
        <div>
          <div style={{ marginBottom: 10 }}>{done.aborted ? "Stopped after a backup failed." : "Teardown finished."} Backup in <span className="mono" style={{ color: "var(--fg)" }}>{done.backup_dir}</span>.</div>
          {(done.results || []).map((r, i) => (
            <div className="oprow" key={i}>
              {r.status === "success" ? <CircleCheck size={15} color="var(--hi)" /> : <CircleX size={15} color="var(--danger)" />}
              <span><b style={{ color: "var(--fg)", fontWeight: 700 }}>{OP_LABEL[r.op] || r.op}</b> <span className="mono">{r.target}</span></span>
              <span className="tag sans">{r.status}</span>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}
