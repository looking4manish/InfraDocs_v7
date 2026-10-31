import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Server, Activity, Clock, Waypoints, ShieldCheck, RotateCw, ArrowRight, CircleX, TriangleAlert, Crown, Network,
} from "lucide-react";
import { endpoints } from "../api/client";
import { useCluster } from "../lib/queries";
import { relTime, shortDateTime, errDetail, plural } from "../lib/format";
import { Page, Head, Card, CardHead, Btn, Chip, Empty, Skel, ConfirmModal } from "../components/ui";
import { useToast } from "../lib/toast";
import ReplicaSetMap from "../components/ReplicaSetMap";
import AddServer from "../components/AddServer";

function modeLine(cs) {
  if (cs.mode === "standalone") return "Standalone";
  if (cs.mode === "automatic") return "automatic failover on";
  return "automatic failover paused";
}

export default function Servers() {
  const q = useCluster();
  const qc = useQueryClient();
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const [confirm, setConfirm] = useState(null); // { kind, node }
  const cs = q.data;

  const act = useMutation({
    mutationFn: ({ kind, node }) => {
      if (kind === "stepdown") return endpoints.clusterStepDown();
      if (kind === "transfer") return endpoints.clusterTransfer(node);
      if (kind === "remove") return endpoints.clusterRemoveMember(node);
      if (kind === "takeover") return endpoints.clusterTakeover(node);
      return Promise.reject(new Error("unknown action"));
    },
    onSuccess: (_, v) => {
      setConfirm(null);
      qc.invalidateQueries({ queryKey: ["cluster-state"] });
      toast({ stepdown: "Stepped down; an election is running.", transfer: `Handing over to ${v.node}.`, remove: `${v.node} removed.`, takeover: "This server is now primary." }[v.kind]);
    },
  });

  if (q.isLoading) {
    return <Page><Head eyebrow="Cluster" title="Servers" /><div className="rs"><Card style={{ padding: 20 }}><Skel h={300} /></Card><Card style={{ padding: 20 }}><Skel h={300} /></Card></div></Page>;
  }
  if (q.error || !cs) {
    return (
      <Page>
        <Head eyebrow="Cluster" title="Servers" />
        <Card><Empty icon={Server} title="Cluster state unavailable">{errDetail(q.error, "The server did not answer.")}</Empty></Card>
      </Page>
    );
  }

  const members = cs.members || [];
  const self = members.find((m) => m.is_self);
  const isPrimary = cs.role === "primary" || cs.role === "standalone";
  const n = cs.voting_members || 0;
  const tolerance = Math.max(0, n - (cs.quorum || 0));
  const timeout = cs.settings?.election_timeout_s;
  const hb = cs.settings?.heartbeat_interval_s;
  const ladder = n > 5 ? [3, 4, 5, 6, 7] : [1, 2, 3, 4, 5];
  const rungLabel = (k) => (k === 1 ? "Standalone" : k % 2 ? "Automatic" : "Manual");
  const leader = members.find((m) => m.role === "primary");
  const quorumTitle = cs.mode === "standalone" ? "Standalone" : cs.has_quorum ? "Quorum held" : "Quorum lost";
  const quorumText = cs.mode === "standalone"
    ? "One voting server. Add two more to turn on automatic failover."
    : cs.mode === "manual"
      ? `${n} voting members is an even count, so a failed primary is replaced by hand. Make the count odd to turn on automatic failover.`
      : cs.has_quorum
        ? `A majority is ${cs.quorum} of ${n} votes. ${tolerance === 1 ? "One server" : `${tolerance} servers`} can fail and the others elect a new primary within about ${timeout ? timeout + 2 : 12} seconds.`
        : `Only ${cs.reachable_voters} of ${n} voting members answer; ${cs.quorum} are needed to elect a primary.`;
  const C = 2 * Math.PI * 36;
  const frac = n ? Math.min(1, cs.reachable_voters / n) : 1;

  return (
    <Page>
      <Head eyebrow="Cluster" title="Servers"
        sub={`${plural(n, "voting member")} · ${leader ? `${leader.node_id} is primary` : "no primary"} · ${modeLine(cs)}`}>
        {cs.mode === "manual" && cs.role !== "primary" && (
          <Btn icon={Crown} onClick={() => setConfirm({ kind: "takeover", node: cs.node_id })}>Take over as primary</Btn>
        )}
        <Btn primary icon={Server} disabled={!isPrimary} title={isPrimary ? undefined : "Open the primary to add a server"} onClick={() => setAdding(true)}>Add a server</Btn>
      </Head>

      {!cs.has_quorum && cs.mode === "automatic" && (
        <div className="notice"><TriangleAlert size={16} />No majority answers. Writes stop on every member until a majority is back.</div>
      )}

      <div className="rs">
        <Card className="rs-map">
          <CardHead title="Replica set" meta={hb ? <><Activity size={14} />Heartbeats every {hb} s</> : null} />
          <div className="dotgrid" />
          <ReplicaSetMap members={members} heartbeatS={hb} />
          <div className="rs-strip">
            <div className="stat"><div className="k">Term</div><div className="v">{cs.term ?? "—"}</div></div>
            <div className="stat"><div className="k">Primary since</div><div className="v">{cs.primary_since ? shortDateTime(cs.primary_since) : "—"}</div></div>
            <div className="stat"><div className="k">Votes reachable</div><div className={`v ${cs.has_quorum ? "okv" : ""}`}>{cs.reachable_voters} / {n}</div></div>
            <div className="stat"><div className="k">Election timeout</div><div className="v">{timeout != null ? `${timeout} s` : "—"}</div></div>
          </div>
        </Card>
        <Card className="quorum">
          <div className="qbig">
            <svg className="ring" width="86" height="86" viewBox="0 0 86 86" role="img" aria-label={`${cs.reachable_voters} of ${n} voting members reachable`}>
              <circle cx="43" cy="43" r="36" fill="none" stroke="rgb(255 255 255 / .06)" strokeWidth="8" />
              <circle cx="43" cy="43" r="36" fill="none" stroke={cs.has_quorum || cs.mode === "standalone" ? "#4dffad" : "#fb889a"} strokeWidth="8" strokeLinecap="round"
                strokeDasharray={`${C * frac} ${C}`} transform="rotate(-90 43 43)" style={{ filter: "drop-shadow(0 0 6px rgb(0 232 135 / .7))" }} />
              <text x="43" y="40" textAnchor="middle" fill="#f2fbf6" fontFamily="JetBrains Mono" fontSize="19" fontWeight="500">{cs.reachable_voters}/{n}</text>
              <text x="43" y="57" textAnchor="middle" fill="#96b0a2" fontSize="10.5" fontWeight="700">reachable</text>
            </svg>
            <div><div className="t">{quorumTitle}</div><div className="d">{quorumText}</div></div>
          </div>
          <div>
            <CardHead flush title="Voting members" meta="Automatic failover needs an odd count" />
            <div className="ladder">
              {ladder.map((k) => {
                const auto = k >= 3 && k % 2 === 1;
                return (
                  <div key={k} className={`rung${auto ? " auto" : ""}${k === n ? " now" : ""}`}>
                    <div className="n">{k}</div>
                    <div className="dots">{Array.from({ length: Math.min(k, 7) }, (_, i) => <i key={i} />)}</div>
                    <div className="l">{rungLabel(k)}</div>
                  </div>
                );
              })}
            </div>
          </div>
          {cs.mode !== "standalone" && (
            <div>
              <CardHead flush title={cs.mode === "automatic" ? "When the primary fails" : "Replacing a failed primary"} />
              <div className="fo">
                {(cs.mode === "automatic" ? [
                  [Clock, <><b>{timeout ?? 10} s</b> without a heartbeat</>],
                  [Waypoints, "Secondaries ask for votes in a new term"],
                  [ShieldCheck, <><b>{cs.quorum} of {n}</b> votes elect the highest priority</>],
                  [RotateCw, "The old primary rejoins as a secondary"],
                ] : [
                  [Clock, "The primary stops answering"],
                  [TriangleAlert, "No election runs with an even count"],
                  [Crown, "Take over on the server that should lead"],
                  [RotateCw, "The old primary rejoins as a secondary"],
                ]).map(([I, t], i) => (
                  <div className="fs" key={i}><span className="fi"><I size={16} /></span><div>{t}</div></div>
                ))}
              </div>
            </div>
          )}
        </Card>
      </div>

      <div className="mgrid" style={members.length <= 3 ? { gridTemplateColumns: `repeat(${members.length}, minmax(0, 1fr))${isPrimary ? " minmax(0, .8fr)" : ""}` } : { gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))" }}>
        {members.map((m) => <Member key={m.node_id} m={m} cs={cs} isPrimary={cs.role === "primary"}
          onAct={(kind) => setConfirm({ kind, node: m.node_id })} />)}
        {isPrimary && (
          <button type="button" className="card addcard" onClick={() => setAdding(true)}>
            <span className="plus">+</span>
            <div className="t">Add a server</div>
            <div className="d">{n % 2 === 1 && n >= 3 ? "A fourth voter pauses automatic failover; a non-voting member does not." : n === 1 ? "Two more voting servers turn on automatic failover." : "One more voting server makes the count odd."}</div>
          </button>
        )}
      </div>

      <Card className="hist" style={{ marginTop: 16 }}>
        <CardHead title="Elections" meta={(cs.elections || []).length ? "Newest first" : null} />
        <div className="card-b">
          {(cs.elections || []).length === 0 && <div style={{ color: "var(--subtle)", fontSize: 13 }}>No elections yet. The first one runs when a second voting server joins.</div>}
          {(cs.elections || []).map((e) => {
            const full = e.votes_for === e.voters;
            return (
              <div className="ev" key={`${e.term}-${e.at}`}>
                <span className="term">Term {e.term}</span>
                <span className={`m ${full ? "" : "w"}`}>{full ? <ShieldCheck size={13} /> : <TriangleAlert size={13} />}</span>
                <span className="w"><b>{e.winner}</b> elected with {e.votes_for} of {e.voters} votes{e.reason && <span style={{ color: "var(--subtle)" }}> · {e.reason}</span>}</span>
                <span className="when">{shortDateTime(e.at)}</span>
              </div>
            );
          })}
        </div>
      </Card>

      {adding && <AddServer cs={cs} onClose={() => setAdding(false)} />}
      {confirm && (
        <ConfirmModal
          title={{ stepdown: `Step down ${confirm.node}`, transfer: `Make ${confirm.node} primary`, remove: `Remove ${confirm.node}`, takeover: `Take over as primary` }[confirm.kind]}
          icon={{ stepdown: ArrowRight, transfer: ShieldCheck, remove: CircleX, takeover: Crown }[confirm.kind]}
          actionLabel={{ stepdown: "Step down", transfer: "Hand over", remove: "Remove", takeover: "Take over" }[confirm.kind]}
          danger={confirm.kind === "remove" || confirm.kind === "takeover"}
          confirmWord={confirm.kind === "remove" || confirm.kind === "takeover" ? confirm.node : null}
          busy={act.isPending} error={act.error ? errDetail(act.error) : null}
          onConfirm={() => act.mutate(confirm)} onClose={() => { setConfirm(null); act.reset(); }}>
          <div>{{
            stepdown: "The secondaries elect a new primary in a new term. This server rejoins as a secondary.",
            transfer: `${confirm.node} takes over once it has caught up. This server becomes a secondary.`,
            remove: `${confirm.node} leaves the replica set and stops voting. Its InfraDocs keeps running on its own.`,
            takeover: `${confirm.node} becomes primary in a new term. Do this only when the old primary is down.`,
          }[confirm.kind]}</div>
          {self && confirm.kind === "remove" && n % 2 === 0 && members.find((x) => x.node_id === confirm.node)?.votes === 1 && (
            <div className="verdict" style={{ borderColor: "rgb(77 255 173 / .3)", background: "rgb(var(--glow) / .06)" }}>
              <ShieldCheck size={16} color="var(--hi)" /><span>Leaves {n - 1} voting members, which turns automatic failover on.</span>
            </div>
          )}
        </ConfirmModal>
      )}
    </Page>
  );
}

function Member({ m, cs, isPrimary, onAct }) {
  const prim = m.role === "primary";
  const down = m.role === "unreachable" || !m.healthy;
  const badge = prim ? "P" : m.votes === 0 ? "N" : "S";
  const roleName = prim ? "Primary" : m.role === "candidate" ? "Candidate" : m.votes === 0 ? "Non-voting" : "Secondary";
  const host = m.host || {};
  const gauges = [["CPU", host.cpu_pct], ["Memory", host.mem_pct], ["Disk", host.disk_pct]].filter(([, v]) => typeof v === "number");
  const beats = Array.isArray(m.heartbeats) ? m.heartbeats.slice(-16) : [];
  return (
    <Card className={`member${prim ? " prim" : ""}`}>
      <div className="h">
        <span className={`badge${prim ? "" : " sec"}`}>{badge}</span>
        <div style={{ minWidth: 0 }}>
          <div className="nm">{m.node_id}</div>
          <div className="role">{roleName} · priority {m.priority} · {m.votes ? "1 vote" : "no vote"}</div>
        </div>
        {down ? <Chip tone="danger">Unreachable</Chip> : m.scan_overdue ? <Chip tone="warn">Scans overdue</Chip> : <Chip tone="ok">Healthy</Chip>}
      </div>
      <div className="kv">
        <span className="k">Address</span><span className="v mono">{(m.address || "—").replace(/^https?:\/\//, "")}</span>
        {(host.os || host.cpu_count) && <><span className="k">Host</span><span className="v">{[host.os, host.cpu_count ? `${host.cpu_count} CPU` : null].filter(Boolean).join(" · ")}</span></>}
        <span className="k">Heartbeat</span><span className="v" style={down ? { color: "var(--danger)" } : undefined}>
          {m.is_self ? "This server" : m.last_heartbeat_age_s == null ? "Never" : `${m.last_heartbeat_age_s < 10 ? m.last_heartbeat_age_s.toFixed(1) : Math.round(m.last_heartbeat_age_s)} s ago`}
        </span>
        <span className="k">Last scan</span><span className="v" style={m.scan_overdue ? { color: "var(--warn)" } : undefined}>{m.last_scan_at ? relTime(m.last_scan_at) : "Never"}</span>
        {m.version && <><span className="k">Version</span><span className="v mono">{m.version}</span></>}
      </div>
      {gauges.length > 0 && (
        <div className="gauge">
          {gauges.map(([k, v]) => (
            <div className="g" key={k}>{k}<span className="bar"><i className={v > 80 ? "w" : ""} style={{ width: `${v}%` }} /></span><span className="val">{Math.round(v)}%</span></div>
          ))}
        </div>
      )}
      {beats.length > 0 && !m.is_self && (
        <div>
          <div style={{ fontSize: 12, color: "var(--subtle)", fontWeight: 600, marginBottom: 6 }}>Last {beats.length} heartbeats</div>
          <div className="hb" aria-label={`${beats.filter(Boolean).length} of ${beats.length} heartbeats answered`}>
            {beats.map((b, i) => <i key={i} className={b ? "" : "miss"} style={{ height: b ? 10 + ((i * 7) % 12) : 6 }} />)}
          </div>
        </div>
      )}
      <div className="acts">
        {prim && m.is_self && cs.mode !== "standalone" && <Btn sm icon={ArrowRight} onClick={() => onAct("stepdown")}>Step down</Btn>}
        {isPrimary && !m.is_self && !prim && m.votes === 1 && m.priority > 0 && !down && <Btn sm icon={ShieldCheck} onClick={() => onAct("transfer")}>Make primary</Btn>}
        {isPrimary && !m.is_self && <Btn sm danger icon={CircleX} onClick={() => onAct("remove")}>Remove</Btn>}
        {!isPrimary && m.is_self && <span style={{ color: "var(--subtle)", fontSize: 12.5, display: "inline-flex", gap: 6, alignItems: "center" }}><Network size={14} />Managed from the primary</span>}
      </div>
    </Card>
  );
}
