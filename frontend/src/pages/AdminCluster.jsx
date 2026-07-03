import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "../api/client";
import { cn } from "../lib/cn";

// Admin / Cluster tab — the single home for cluster role management. Behind the app's
// admin auth (AuthGate) like every other page. Every control is driven by /api/cluster
// endpoints that carry their own guards; when a transition is blocked we SHOW the reason
// the backend returned rather than silently greying the button out.
//
// This file is presentation + copy only — it consumes the existing Neon-Depth tokens
// (tailwind.config.js: bg.*, accent/accent-soft, strain, ink; index.css: --neon,
// .neon-panel/.neon-glow). No behaviour, endpoints, guards, or state fields change here.

function Pill({ tone = "zinc", children }) {
  const tones = {
    zinc: "bg-white/10 text-zinc-100",
    green: "bg-emerald-500/20 text-emerald-300",
    amber: "bg-amber-500/20 text-amber-200",
    rose: "bg-rose-500/20 text-rose-200",
    cyan: "bg-[var(--neon)]/20 text-accent-cyan",
  };
  return <span className={cn("text-[11px] font-medium px-2 py-0.5 rounded-full", tones[tone])}>{children}</span>;
}

function Section({ title, desc, children }) {
  return (
    <div className="neon-panel rounded-xl p-5 mb-4">
      <h2 className="text-base font-semibold text-ink">{title}</h2>
      {desc && <p className="text-[13px] text-zinc-300 mt-1.5 leading-relaxed max-w-prose">{desc}</p>}
      <div className="mt-4">{children}</div>
    </div>
  );
}

// A control + a one-line plain-English "what this means" under it.
function Control({ children, help }) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2.5 flex-wrap">{children}</div>
      {help && <p className="text-[13px] text-zinc-400 leading-relaxed max-w-prose">{help}</p>}
    </div>
  );
}

const btn = "text-sm px-3.5 py-2 rounded-lg border border-white/15 text-ink hover:bg-bg-elev disabled:opacity-40 disabled:cursor-not-allowed transition";
const btnPrimary = "text-sm px-3.5 py-2 rounded-lg font-medium neon-glow border border-[var(--neon)] bg-[var(--neon)]/15 text-ink hover:bg-[var(--neon)]/25 disabled:opacity-40 disabled:cursor-not-allowed transition";
const btnDanger = "text-sm px-3.5 py-2 rounded-lg border border-rose-500/50 bg-rose-500/15 text-rose-100 hover:bg-rose-500/25 disabled:opacity-40 disabled:cursor-not-allowed transition";
const input = "text-sm px-2.5 py-2 rounded-lg bg-bg-card border border-white/15 text-ink placeholder:text-zinc-500 focus:border-[var(--neon)] outline-none";
const label = "text-sm text-zinc-200";
const colHead = "text-[11px] font-semibold uppercase tracking-wide text-zinc-400";

export default function AdminCluster() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState(null);
  const flash = (kind, text) => setMsg({ kind, text });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["cluster-state"] });
    qc.invalidateQueries({ queryKey: ["cluster-audit"] });
    qc.invalidateQueries({ queryKey: ["federation-tokens"] });
  };

  const state = useQuery({
    queryKey: ["cluster-state"],
    queryFn: () => endpoints.clusterState().then((r) => r.data),
    refetchInterval: 5000,
  });
  const audit = useQuery({
    queryKey: ["cluster-audit"],
    queryFn: () => endpoints.clusterAudit(50).then((r) => r.data),
    refetchInterval: 10000,
  });
  const tokens = useQuery({
    queryKey: ["federation-tokens"],
    queryFn: () => endpoints.listFederationTokens().then((r) => r.data),
    refetchInterval: 15000,
  });

  const run = (fn, ok) =>
    useMutation({
      mutationFn: fn,
      onSuccess: (d) => {
        if (d && d.ok === false && (d.reason || d.blocked || d.needs_force)) {
          flash("err", d.reason || "blocked");
        } else {
          flash("ok", typeof ok === "function" ? ok(d) : ok);
        }
        refresh();
      },
      onError: (e) => flash("err", e?.response?.data?.detail?.reason || e?.response?.data?.detail || "request failed"),
    });

  const d = state.data;
  const enable = run((v) => endpoints.clusterEnable(v).then((r) => r.data), (r) => `cluster ${r.cluster_enabled ? "enabled" : "disabled"}`);
  const toPrimary = run((p) => endpoints.clusterToPrimary(p).then((r) => r.data), "now serving as primary");
  const join = run((b) => endpoints.clusterJoin(b).then((r) => r.data), "joined the cluster as secondary");
  const promote = run((f) => endpoints.clusterPromote(f).then((r) => r.data), (r) => (r.promoted ? "promoted to primary" : r.reason));
  const demote = run(() => endpoints.clusterDemote().then((r) => r.data), "demoted to secondary");
  const toStandalone = run((b) => endpoints.clusterToStandalone(b.force, b.confirm).then((r) => r.data), "converted to standalone");
  const setPriority = run((p) => endpoints.clusterSetPriority(p).then((r) => r.data), (r) => `priority set to ${r.priority}`);
  const evict = run((b) => endpoints.clusterEvict(b.node_id, b.confirm).then((r) => r.data), (r) => `evicted ${r.evicted} (fleet now ${r.fleet_size})`);
  const overrideMut = run((v) => endpoints.clusterOverride(v).then((r) => r.data), "override updated");
  const mint = run((sid) => endpoints.mintFederationToken(sid).then((r) => r.data), (r) => `token minted for ${r.server_id}`);
  const revoke = run((t) => endpoints.revokeFederationToken(t).then((r) => r.data), "token revoked");

  if (!d) return <div className="text-zinc-300 text-sm">Loading cluster state…</div>;

  const role = d.role;
  const g = d.guards || {};

  return (
    <div className="max-w-6xl mx-auto">
      {/* header */}
      <div className="flex items-center gap-2.5 flex-wrap mb-2">
        <h1 className="text-2xl font-bold text-ink tracking-tight">Admin · Cluster</h1>
        <Pill tone={role === "primary" ? "green" : role === "secondary" ? "cyan" : "amber"}>role: {role}</Pill>
        <Pill tone={d.cluster_enabled ? "green" : "zinc"}>cluster {d.cluster_enabled ? "enabled" : "paused"}</Pill>
        {d.override && <Pill tone="amber">failover frozen (override)</Pill>}
        {!d.majority && <Pill tone="rose">no majority</Pill>}
        <span className="ml-auto text-[13px] text-zinc-300 font-mono">
          {d.node_id} · priority {d.priority ?? "—"} · fleet {d.fleet_size} · leader {d.current_leader || "—"}
        </span>
      </div>
      <p className="text-[13px] text-zinc-400 mb-5 max-w-prose">
        Manage what this node is in the cluster and who leads. Every action is guarded — if it isn't safe
        right now, the reason is shown instead of letting you break the cluster.
      </p>

      {msg && (
        <div className={cn("text-sm mb-5 px-3.5 py-2.5 rounded-lg border",
          msg.kind === "ok" ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
            : "border-rose-500/40 bg-rose-500/10 text-rose-200")}>
          {msg.text}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-x-5 items-start">
        {/* ---- left column: what this node is / does ---- */}
        <div>
          <Section
            title="Cluster machinery"
            desc="Starts or stops this node's live health-gossip + failover election loop. Pausing keeps its role and roster but stops automatic failover until you turn it back on.">
            <Control help={d.cluster_enabled
              ? "Currently running: peers are health-checked and a lost primary triggers an election."
              : "Currently paused: this node keeps its role and roster but will not gossip or fail over."}>
              <button className={d.cluster_enabled ? btn : btnPrimary}
                disabled={enable.isPending}
                onClick={() => enable.mutate(!d.cluster_enabled)}>
                {d.cluster_enabled ? "Pause failover (quiesce gossip)" : "Enable cluster (start gossip + failover)"}
              </button>
            </Control>
          </Section>

          <Section
            title="Role transitions"
            desc="Change this node's role in the cluster. Options depend on the current role; a blocked action shows why.">
            {role === "standalone" && (
              <div className="flex flex-col gap-5">
                <StandaloneToPrimary onGo={(p) => toPrimary.mutate(p)} busy={toPrimary.isPending} />
                <JoinCluster d={d} sourceRole="standalone" onGo={(b) => join.mutate(b)} busy={join.isPending} />
              </div>
            )}
            {role === "secondary" && (
              <PromoteControl d={d} onPromote={(f) => promote.mutate(f)} busy={promote.isPending} />
            )}
            {role === "primary" && (
              <div className="flex flex-col gap-5">
                <Control help={g.demote_blocked_reason
                  ? null
                  : "Step down as primary and follow another primary. This node stays in the cluster as a secondary."}>
                  <button className={btn} disabled={!!g.demote_blocked_reason || demote.isPending}
                    onClick={() => demote.mutate()}>Demote to secondary</button>
                  {g.demote_blocked_reason &&
                    <span className="text-[13px] text-amber-300 leading-relaxed">Blocked: {g.demote_blocked_reason}</span>}
                </Control>
                <PrimaryToStandalone d={d} onGo={(b) => toStandalone.mutate(b)} busy={toStandalone.isPending} />
                <JoinCluster d={d} sourceRole="primary" onGo={(b) => join.mutate(b)} busy={join.isPending} />
                <Control help={d.override
                  ? "Elections are frozen on the current primary — no automatic failover until you clear this."
                  : "Freeze elections on the current primary so no automatic failover happens (a manual pin)."}>
                  <button className={btn} disabled={overrideMut.isPending}
                    onClick={() => overrideMut.mutate(!d.override)}>
                    {d.override ? "Clear override (allow failover again)" : "Pin this primary (freeze failover)"}
                  </button>
                </Control>
              </div>
            )}
          </Section>

          <Section
            title="Failover priority"
            desc="Lower number = higher priority; 1 is the primary-most. On failover the reachable node with the lowest number becomes primary. The primary rejects a number already in use.">
            <PriorityControl current={d.priority} onGo={(p) => setPriority.mutate(p)} busy={setPriority.isPending} />
          </Section>
        </div>

        {/* ---- right column: the fleet, tokens, history ---- */}
        <div>
          <Section
            title="Fleet roster"
            desc="Every node this one knows about, with its failover priority and whether it's reachable right now. Evict permanently removes a dead node and tombstones it so it can't rejoin — this also shrinks the majority count used for split-brain safety.">
            <div className="flex items-center gap-3 pb-1.5 mb-1 border-b border-white/10">
              <span className="w-2.5" />
              <span className={cn(colHead, "w-14")}>priority</span>
              <span className={cn(colHead, "flex-1")}>node</span>
              <span className={cn(colHead, "w-24")}>reachable?</span>
              <span className={cn(colHead, "w-16 text-right")}>action</span>
            </div>
            <div className="flex flex-col">
              {[...(d.nodes || [])].sort((a, b) => (a.priority ?? 99) - (b.priority ?? 99)).map((n) => (
                <div key={n.node_id} className="flex items-center gap-3 py-2 border-b border-white/5">
                  <span className={cn("w-2.5 h-2.5 rounded-full shrink-0", n.reachable ? "bg-emerald-400" : "bg-rose-500")} />
                  <span className="font-mono text-sm text-zinc-300 w-14">p{n.priority ?? "—"}</span>
                  <span className="flex-1 flex items-center gap-2 min-w-0">
                    <span className={cn("font-mono text-sm truncate", n.is_primary ? "text-accent-soft" : "text-ink")}>{n.node_id}</span>
                    {n.is_primary ? <Pill tone="green">primary</Pill> : <Pill tone="zinc">secondary</Pill>}
                    {n.self && <Pill tone="cyan">this node</Pill>}
                  </span>
                  <span className={cn("w-24 text-[13px] font-medium", n.reachable ? "text-emerald-300" : "text-rose-300")}>
                    {n.reachable ? "reachable" : "unreachable"}
                  </span>
                  <span className="w-16 flex justify-end">
                    {!n.self && <EvictControl nodeId={n.node_id} onGo={(b) => evict.mutate(b)} busy={evict.isPending} />}
                  </span>
                </div>
              ))}
            </div>
          </Section>

          <Section
            title="Join tokens"
            desc="A new secondary needs a join token minted here to enrol. This is the same token store the installer's enrol validates against — mint one per node, and revoke to disable it.">
            <TokenMint onGo={(sid) => mint.mutate(sid)} busy={mint.isPending} />
            <div className="flex items-center gap-3 pb-1.5 mt-4 mb-1 border-b border-white/10">
              <span className={cn(colHead, "w-40")}>for node</span>
              <span className={cn(colHead, "flex-1")}>token</span>
              <span className={cn(colHead, "w-20 text-right")}>action</span>
            </div>
            <div className="flex flex-col">
              {(tokens.data?.tokens || []).length === 0 && <div className="text-[13px] text-zinc-400 py-2">No outstanding tokens.</div>}
              {(tokens.data?.tokens || []).map((t) => (
                <div key={t.token} className="flex items-center gap-3 py-2 border-b border-white/5">
                  <span className="font-mono text-sm text-ink w-40 truncate">{t.server_id}</span>
                  <span className="font-mono text-[13px] text-zinc-300 flex-1 truncate">{t.token_preview}</span>
                  <span className="w-20 flex justify-end">
                    <button className={cn(btnDanger, "py-1 px-2.5")} disabled={revoke.isPending}
                      onClick={() => revoke.mutate(t.token)}>Revoke</button>
                  </span>
                </div>
              ))}
            </div>
          </Section>

          <Section
            title="Audit trail"
            desc="Every role change on this node — who did it, what changed, and why.">
            <div className="flex items-center gap-3 pb-1.5 mb-1 border-b border-white/10">
              <span className={cn(colHead, "w-[132px]")}>timestamp</span>
              <span className={cn(colHead, "w-20")}>actor</span>
              <span className={cn(colHead, "w-28")}>action</span>
              <span className={cn(colHead, "w-24")}>from → to</span>
              <span className={cn(colHead, "w-16")}>result</span>
              <span className={cn(colHead, "flex-1")}>reason</span>
            </div>
            <div className="flex flex-col max-h-[340px] overflow-y-auto">
              {(audit.data?.entries || []).length === 0 && <div className="text-[13px] text-zinc-400 py-2">No cluster actions yet.</div>}
              {(audit.data?.entries || []).map((e, i) => (
                <div key={i} className="flex items-center gap-3 py-2 border-b border-white/5 text-[13px]">
                  <span className="font-mono text-zinc-300 w-[132px] shrink-0">{String(e.ts).replace("T", " ").slice(0, 19)}</span>
                  <span className="text-zinc-200 w-20 truncate">{e.actor}</span>
                  <span className="font-mono text-ink w-28 truncate">{e.action}</span>
                  <span className="text-zinc-300 w-24 truncate">{e.from_role}→{e.to_role}</span>
                  <span className="w-16"><Pill tone={e.result === "ok" ? "green" : e.result === "refused" ? "rose" : "amber"}>{e.result}</Pill></span>
                  {e.reason
                    ? <span className="text-zinc-300 flex-1 truncate" title={e.reason}>{e.reason}</span>
                    : <span className="text-zinc-500 flex-1">—</span>}
                </div>
              ))}
            </div>
          </Section>
        </div>
      </div>
    </div>
  );
}

function StandaloneToPrimary({ onGo, busy }) {
  const [p, setP] = useState(1);
  return (
    <Control help="Turns the cluster on and makes this the first (primary) node. Pick its failover priority — 1 is the primary-most.">
      <span className={cn(label, "font-medium")}>Start a new cluster (this node = primary)</span>
      <input className={cn(input, "w-24")} type="number" min={1} max={99} value={p}
        onChange={(e) => setP(Number(e.target.value))} placeholder="priority" />
      <button className={btnPrimary} disabled={busy} onClick={() => onGo(p)}>Enable + become primary</button>
    </Control>
  );
}

function JoinCluster({ d, sourceRole, onGo, busy }) {
  const [f, setF] = useState({ primary_url: "", join_token: "", advertise_url: "", priority: 2 });
  const [confirm, setConfirm] = useState("");
  const [ack, setAck] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: k === "priority" ? Number(e.target.value) : e.target.value });
  const fromPrimary = sourceRole === "primary";
  const deps = d?.guards?.to_standalone_dependents || [];
  const overridePinned = fromPrimary && !!d?.override;      // must clear pin first
  const needOrphan = fromPrimary && deps.length > 0;         // orphan hard-confirm
  const baseReady = f.primary_url && f.join_token && f.advertise_url && f.priority;
  const ready = baseReady && !overridePinned && (needOrphan ? confirm === d?.node_id : (fromPrimary ? ack : true));
  const submit = () => onGo({
    primary_url: f.primary_url, join_token: f.join_token, advertise_url: f.advertise_url,
    priority: f.priority, force: needOrphan, confirm_hostname: needOrphan ? confirm : "",
  });
  return (
    <div className="flex flex-col gap-2.5 border-t border-white/10 pt-4">
      <span className={cn(label, "font-medium")}>
        {fromPrimary ? "Join an existing primary (become secondary)" : "Join an existing cluster as a secondary"}
      </span>
      {fromPrimary && (
        <p className="text-[13px] text-amber-300 leading-relaxed max-w-prose">
          This node will leave its own cluster and join {f.primary_url || "<primary>"} as a secondary. It will stop being a primary.
        </p>
      )}
      <p className="text-[13px] text-zinc-400 leading-relaxed max-w-prose">
        Enrol with a running primary using a join token minted on that primary for{" "}
        <span className="font-mono text-ink">{d?.node_id || "this node"}</span> — the same enrol path the installer
        uses (bidirectional reachability + the shared fleet secret). Your reachable address is verified both ways.
      </p>
      {overridePinned ? (
        <span className="text-[13px] text-rose-300 leading-relaxed">
          Blocked: override/pin is set on this node — clear it (Clear override, below) before joining another primary.
        </span>
      ) : (
        <>
          <div className="flex items-center gap-2.5 flex-wrap">
            <input className={cn(input, "w-64")} placeholder="primary address (http://…:8081)" value={f.primary_url} onChange={set("primary_url")} />
            <input className={cn(input, "w-56")} placeholder="join token" value={f.join_token} onChange={set("join_token")} />
            <input className={cn(input, "w-64")} placeholder="this node's reachable address" value={f.advertise_url} onChange={set("advertise_url")} />
            <input className={cn(input, "w-24")} type="number" min={1} max={99} value={f.priority} onChange={set("priority")} placeholder="priority" />
          </div>
          {needOrphan && (
            <div className="flex items-center gap-2.5 flex-wrap">
              <span className="text-[13px] text-amber-300 leading-relaxed">
                Orphans {deps.length} secondary({deps.join(", ")}) that point at this node — type <span className="font-mono text-ink">{d?.node_id}</span> to confirm
              </span>
              <input className={cn(input, "w-44")} placeholder={d?.node_id} value={confirm} onChange={(e) => setConfirm(e.target.value)} />
            </div>
          )}
          {fromPrimary && !needOrphan && (
            <label className="flex items-center gap-2 text-[13px] text-zinc-300">
              <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
              I understand this node will stop being its own primary.
            </label>
          )}
          <button className={cn(needOrphan ? btnDanger : btnPrimary, "self-start")} disabled={busy || !ready} onClick={submit}>
            {fromPrimary ? "Leave my cluster + join as secondary" : "Validate token + join"}
          </button>
        </>
      )}
    </div>
  );
}

function PromoteControl({ d, onPromote, busy }) {
  const [needsForce, setNeedsForce] = useState(false);
  // The promote endpoint returns needs_force when peers are unreachable; the parent flash
  // shows the reason. Here we offer force after a first refusal.
  return (
    <Control help="Take over as primary (the failover-restore path). Refused if a live primary is still visible; if some peers are unreachable you'll be asked to force-confirm, because forcing could create two primaries.">
      <button className={btnPrimary} disabled={busy} onClick={() => { setNeedsForce(true); onPromote(false); }}>
        Promote this node to primary
      </button>
      {needsForce && (
        <button className={btnDanger} disabled={busy} onClick={() => onPromote(true)}>
          Force promote (only if the old primary is truly down)
        </button>
      )}
    </Control>
  );
}

function PrimaryToStandalone({ d, onGo, busy }) {
  const deps = d.guards?.to_standalone_dependents || [];
  const [confirm, setConfirm] = useState("");
  const need = deps.length > 0;
  return (
    <Control help="Leave the cluster and run alone, with no failover. If secondaries still point here they'll be orphaned — you must type this node's id to confirm.">
      <button className={need ? btnDanger : btn} disabled={busy || (need && confirm !== d.node_id)}
        onClick={() => onGo({ force: need, confirm })}>
        Convert to standalone
      </button>
      {need && (
        <>
          <span className="text-[13px] text-amber-300 leading-relaxed">
            Orphans {deps.length} secondary({deps.join(", ")}) — type <span className="font-mono text-ink">{d.node_id}</span> to confirm
          </span>
          <input className={cn(input, "w-44")} placeholder={d.node_id} value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </>
      )}
    </Control>
  );
}

function PriorityControl({ current, onGo, busy }) {
  const [p, setP] = useState(current || 1);
  return (
    <div className="flex items-center gap-2.5">
      <span className={label}>New priority (1–99)</span>
      <input className={cn(input, "w-24")} type="number" min={1} max={99} value={p} onChange={(e) => setP(Number(e.target.value))} />
      <button className={btn} disabled={busy} onClick={() => onGo(p)}>Set priority</button>
    </div>
  );
}

function EvictControl({ nodeId, onGo, busy }) {
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState("");
  if (!open) return <button className={cn(btnDanger, "py-1 px-2.5")} onClick={() => setOpen(true)}>Evict</button>;
  return (
    <span className="flex items-center gap-1.5">
      <input className={cn(input, "w-32 py-1")} placeholder={`type ${nodeId}`} value={confirm} onChange={(e) => setConfirm(e.target.value)} />
      <button className={cn(btnDanger, "py-1 px-2.5")} disabled={busy || confirm !== nodeId}
        onClick={() => onGo({ node_id: nodeId, confirm })}>Confirm</button>
      <button className={cn(btn, "py-1 px-2.5")} onClick={() => { setOpen(false); setConfirm(""); }}>×</button>
    </span>
  );
}

function TokenMint({ onGo, busy }) {
  const [sid, setSid] = useState("");
  return (
    <div className="flex items-center gap-2.5">
      <span className={label}>New secondary's node id</span>
      <input className={cn(input, "w-56")} placeholder="e.g. n150" value={sid} onChange={(e) => setSid(e.target.value)} />
      <button className={btn} disabled={busy || !sid} onClick={() => onGo(sid)}>Mint join token</button>
    </div>
  );
}
