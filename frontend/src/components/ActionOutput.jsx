import { CircleCheck, CircleX } from "lucide-react";
import { Modal } from "./ui";

export default function ActionOutput({ action, target, output, onClose }) {
  const failed = ["failed", "error", "refused"].includes(output.status);
  const results = output.results;
  return (
    <Modal wide title={`${action}${target ? ` · ${target}` : ""}`} icon={failed ? CircleX : CircleCheck} onClose={onClose}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <span className={`chip ${failed ? "danger" : "ok"}`}><span className="d" />{output.status || "done"}</span>
        {output.duration_ms != null && <span className="tag">{output.duration_ms} ms</span>}
        {output.return_code != null && <span className="tag">exit {output.return_code}</span>}
        {output.targets != null && <span className="tag">{output.targets} targets</span>}
      </div>
      {Array.isArray(results) && results.length > 0 && (
        <div>
          {results.map((r, i) => (
            <div key={i} className="oprow">
              {r.status === "success" ? <CircleCheck size={15} color="var(--hi)" /> : <CircleX size={15} color="var(--danger)" />}
              <span className="mono">{r.asset_name}</span>
              <span className="tag sans">{r.status}</span>
              {(r.stdout_tail || r.stderr_tail) && (
                <pre className={`out${r.stderr_tail && !r.stdout_tail ? " err" : ""}`} style={{ gridColumn: "1 / -1" }}>{r.stdout_tail || r.stderr_tail}</pre>
              )}
            </div>
          ))}
        </div>
      )}
      {output.stdout && <pre className="out">{output.stdout}</pre>}
      {output.stderr && <pre className="out err">{output.stderr}</pre>}
      {!output.stdout && !output.stderr && !(results && results.length) && <div>Completed with no output.</div>}
    </Modal>
  );
}
