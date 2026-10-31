import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { History, Search, CircleCheck, CircleX, CircleSlash } from "lucide-react";
import { endpoints } from "../api/client";
import { shortDateTime } from "../lib/format";
import { Page, Head, Card, Seg, SearchBox, Empty, Skel, Chip, Modal } from "../components/ui";

export default function Actions() {
  const [status, setStatus] = useState("all");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(null);
  const list = useQuery({
    queryKey: ["actions-list", 300],
    queryFn: () => endpoints.listActions({ limit: 300 }).then((r) => r.data.actions || []),
    refetchInterval: 10000,
  });
  const rows = (list.data || []);
  const ql = q.trim().toLowerCase();
  const shown = rows
    .filter((r) => status === "all" || (status === "failed" ? r.status !== "success" : r.status === "success"))
    .filter((r) => !ql || [r.action, r.asset_name, r.project, r.actor, r.category].some((x) => (x || "").toLowerCase().includes(ql)));
  const failed = rows.filter((r) => r.status !== "success").length;

  return (
    <Page>
      <Head eyebrow="Audit" title="Audit log" sub="Every action, who ran it and what it returned" />
      <div className="toolbar">
        <Seg value={status} onChange={setStatus} options={[
          { value: "all", label: "All", count: rows.length },
          { value: "success", label: "Succeeded", count: rows.length - failed },
          { value: "failed", label: "Failed or refused", count: failed },
        ]} />
        <SearchBox icon={Search} value={q} onChange={setQ} placeholder="Filter by action, target, project or user" />
      </div>
      <Card className="log">
        <div className="tr th"><span /><span>When</span><span>Target</span><span>Project</span><span>Action</span><span>By</span><span>Result</span></div>
        {list.isLoading && <div style={{ padding: 20 }}><Skel h={240} /></div>}
        {!list.isLoading && shown.length === 0 && <Empty icon={History} title={rows.length ? "Nothing matches" : "No actions yet"}>{rows.length ? "Change the filter or the search text." : "Starts, stops, restarts and teardowns are recorded here."}</Empty>}
        {shown.map((r, i) => {
          const ok = r.status === "success";
          const refused = r.status === "refused";
          return (
            <div key={i} className="tr" role="button" tabIndex={0} onClick={() => setOpen(r)} onKeyDown={(e) => { if (e.key === "Enter") setOpen(r); }}>
              <span>{ok ? <CircleCheck size={15} color="var(--hi)" /> : refused ? <CircleSlash size={15} color="var(--warn)" /> : <CircleX size={15} color="var(--danger)" />}</span>
              <span className="when">{shortDateTime(r.timestamp)}</span>
              <span className="nm" title={r.asset_name}>{r.asset_name || "—"}</span>
              <span className="pj">{r.project || "—"}</span>
              <span className="mono" style={{ fontSize: 12.5 }}>{r.action}</span>
              <span className="pj">{r.actor || "—"}</span>
              <span><Chip tone={ok ? "ok" : refused ? "warn" : "danger"}>{r.status}</Chip></span>
            </div>
          );
        })}
      </Card>
      {open && (
        <Modal wide title={`${open.action} · ${open.asset_name || open.project || ""}`} icon={History} onClose={() => setOpen(null)}>
          <div className="kv" style={{ gridTemplateColumns: "120px minmax(0,1fr)" }}>
            <span className="k">When</span><span className="v mono">{shortDateTime(open.timestamp)}</span>
            <span className="k">By</span><span className="v">{open.actor || "—"}</span>
            <span className="k">Category</span><span className="v mono">{open.category || "—"}</span>
            <span className="k">Result</span><span className="v">{open.status}{open.return_code != null ? ` · exit ${open.return_code}` : ""}{open.duration_ms != null ? ` · ${open.duration_ms} ms` : ""}</span>
            {open.refused_reason && <><span className="k">Refused</span><span className="v">{open.refused_reason}</span></>}
          </div>
          {open.stdout && <pre className="out">{open.stdout}</pre>}
          {open.stderr && <pre className="out err">{open.stderr}</pre>}
        </Modal>
      )}
    </Page>
  );
}
