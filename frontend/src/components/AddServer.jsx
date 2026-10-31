import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Server, CircleX, Copy, Check, TriangleAlert, CircleCheck, Loader2, KeyRound } from "lucide-react";
import { endpoints } from "../api/client";
import { errDetail, relTime, clock } from "../lib/format";
import { Btn, Chip } from "./ui";

function modeAfter(voters) {
  if (voters <= 1) return "standalone";
  return voters % 2 === 1 ? "automatic" : "manual";
}

// The Add-a-server drawer: choose how the new server votes, create a one-time join code,
// run the command on the new server, then watch both directions confirm.
export default function AddServer({ cs, onClose }) {
  const qc = useQueryClient();
  const n = cs.voting_members || 1;
  const recommendVote = !(n >= 3 && n % 2 === 1);
  const [votes, setVotes] = useState(recommendVote ? 1 : 0);
  const [shown, setShown] = useState(false);
  const [copied, setCopied] = useState(false);

  const close = () => { setShown(false); setTimeout(onClose, 300); };
  useEffect(() => {
    const t = requestAnimationFrame(() => setShown(true));
    return () => cancelAnimationFrame(t);
  }, []);
  useEffect(() => {
    const k = (e) => { if (e.key === "Escape") { setShown(false); setTimeout(onClose, 300); } };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);

  const mint = useMutation({ mutationFn: () => endpoints.clusterJoinCode(votes).then((r) => r.data) });
  const code = mint.data?.code;
  const status = useQuery({
    queryKey: ["join-status", code],
    queryFn: () => endpoints.clusterJoinStatus(code).then((r) => r.data),
    enabled: Boolean(code),
    refetchInterval: (q) => (["joined", "expired", "failed"].includes(q.state.data?.state) ? false : 3000),
  });
  const st = status.data;
  useEffect(() => { if (st?.state === "joined") qc.invalidateQueries({ queryKey: ["cluster-state"] }); }, [st?.state, qc]);

  const after = n + votes;
  const mode = modeAfter(after);
  const expires = mint.data?.expires_at ? clock(mint.data.expires_at) : null;
  const step = st?.state === "joined" ? 3 : code ? 2 : 1;
  const copy = async () => {
    try { await navigator.clipboard.writeText(mint.data.command); setCopied(true); setTimeout(() => setCopied(false), 1600); } catch { /* clipboard refused */ }
  };

  const verdict = mode === "automatic"
    ? { ok: true, text: <><b>{after} voting members</b> keeps an odd count. Automatic failover {n >= 3 && n % 2 === 1 ? "stays" : "turns"} on.</> }
    : mode === "manual"
      ? { ok: false, text: <><b>{after} voting members</b> is an even count, so automatic failover {n % 2 === 1 && n >= 3 ? "pauses" : "stays off"} until {after === 2 ? "a third" : "another"} voter joins.</> }
      : { ok: true, text: <>This server stays the only voter. The new server reports its scans here.</> };

  return createPortal(
    <div className={shown ? "drawer-open" : ""}>
      <div className="scrim" onClick={close} />
      <aside className="drawer" aria-label="Add a server" role="dialog" aria-modal="true">
        <div className="dh">
          <span className="app-icon" style={{ width: 40, height: 40, borderRadius: 12 }}><Server size={18} /></span>
          <h2>Add a server</h2>
          <Btn className="x" iconOnly icon={CircleX} label="Close" onClick={close} />
        </div>
        <div className="db">
          <div className="steps3">
            <div className={`s3 ${step > 1 ? "done" : "cur"}`}>
              <span className="n">1</span>
              <div>
                <div className="t">Choose how it votes</div>
                {[1, 0].sort((x, y) => (recommendVote ? y - x : x - y)).map((v) => (
                  <button key={v} type="button" className={`opt${votes === v ? " on" : ""}`} disabled={!!code}
                    onClick={() => setVotes(v)} style={{ width: "100%", textAlign: "left", font: "inherit", color: "inherit" }}>
                    <span className="rad" />
                    <div>
                      <div className="t">{v ? "Voting member" : "Non-voting member"} {(v === 1) === recommendVote && <Chip tone="ok" dot={false}>Recommended</Chip>}</div>
                      <div className="d">{v
                        ? `Can become primary. Makes ${n + 1} voting members${modeAfter(n + 1) === "automatic" ? ", which turns automatic failover on" : modeAfter(n + 1) === "manual" ? ", so automatic failover pauses" : ""}.`
                        : `Reports its scans, never votes or becomes primary. Keeps ${n} ${n === 1 ? "voter" : "voters"}.`}</div>
                    </div>
                  </button>
                ))}
              </div>
            </div>

            <div className={`s3 ${step > 2 ? "done" : step === 2 ? "cur" : ""}`}>
              <span className="n">2</span>
              <div>
                <div className="t">Run this on the new server</div>
                {!code && (
                  <>
                    <div className="d">Creates a join code that works once.</div>
                    <div style={{ marginTop: 10 }}>
                      <Btn primary icon={KeyRound} busy={mint.isPending} onClick={() => mint.mutate()}>Create join command</Btn>
                    </div>
                    {mint.error && <div className="formerr" style={{ marginTop: 10 }}>{errDetail(mint.error)}</div>}
                  </>
                )}
                {code && (
                  <>
                    <div className="d">The join code works once{expires ? ` and expires at ${expires}` : ""}.</div>
                    <div className="code">
                      <span><span className="c">$ </span>{mint.data.command}</span>
                      <Btn iconOnly icon={copied ? Check : Copy} label={copied ? "Copied" : "Copy command"} onClick={copy} />
                    </div>
                  </>
                )}
              </div>
            </div>

            <div className={`s3 ${st?.state === "joined" ? "done" : step === 2 ? "" : ""}`}>
              <span className="n">3</span>
              <div>
                <div className="t">Confirm both directions</div>
                <div className="d">Each server checks it can reach the other, then the new server appears in the replica set.</div>
                {code && st && (
                  <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
                    {[["new_to_primary", "New server reaches this one"], ["primary_to_new", "This server reaches the new one"]].map(([k, l]) => {
                      const v = st.directions?.[k];
                      return (
                        <div key={k} className="det"><div className="r">
                          {v === true ? <CircleCheck size={15} /> : v === false ? <CircleX size={15} color="var(--danger)" /> : <Loader2 size={15} className="spin" color="var(--subtle)" />}
                          {l}
                        </div></div>
                      );
                    })}
                    {st.state === "waiting" && <div style={{ color: "var(--faint)", fontSize: 12.5 }}>Waiting for the command to run{status.dataUpdatedAt ? ` · checked ${relTime(new Date(status.dataUpdatedAt))}` : ""}</div>}
                    {st.state === "joined" && <div className="verdict" style={{ borderColor: "rgb(77 255 173 / .3)", background: "rgb(var(--glow) / .06)" }}><CircleCheck size={16} color="var(--hi)" /><span><b>{st.node_id}</b> joined the replica set.</span></div>}
                    {(st.state === "expired" || st.state === "failed") && <div className="formerr">{st.state === "expired" ? "The join code expired. Create a new one." : st.reason || "The join failed."}</div>}
                  </div>
                )}
              </div>
            </div>
          </div>

          <div className="verdict" style={verdict.ok ? { borderColor: "rgb(77 255 173 / .3)", background: "rgb(var(--glow) / .06)" } : undefined}>
            {verdict.ok ? <CircleCheck size={16} color="var(--hi)" /> : <TriangleAlert size={16} />}
            <span>{verdict.text}</span>
          </div>
          {st?.state === "joined" && <Btn primary onClick={close}>Done</Btn>}
        </div>
      </aside>
    </div>,
    document.body
  );
}
