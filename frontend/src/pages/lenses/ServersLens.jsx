import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { motion } from "motion/react";
import { endpoints } from "../../api/client";
import { cn } from "../../lib/cn";
import { SPRING, gridVariants, cardVariants } from "./motion";

// A secondary is considered stale if it hasn't pushed within this window.
const SERVER_STALE_MS = 15 * 60 * 1000;

// application_id is `${server_id}:app:${name}` — recover the owning server.
function serverOfApp(a) {
  const id = a.application_id || "";
  const i = id.indexOf(":app:");
  return i > 0 ? id.slice(0, i) : null;
}

function relTime(iso) {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "unknown";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

// Switcher chip — scopes the fleet view to one server (or All).
function SwitchChip({ active, onClick, label, dot }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "px-3 py-1.5 rounded-lg text-[12px] font-medium border transition inline-flex items-center gap-1.5",
        active
          ? "text-accent-soft bg-accent/10 border-accent/30"
          : "text-zinc-400 border-transparent hover:text-zinc-100 hover:bg-bg-elev"
      )}
    >
      {dot && <span className={cn("w-1.5 h-1.5 rounded-full", dot)} />}
      {label}
    </button>
  );
}

function ServerCard({ s, reduce, onOpen }) {
  return (
    <motion.div
      variants={cardVariants(reduce)}
      whileHover={reduce ? undefined : { y: -2 }}
      transition={SPRING}
      onClick={onOpen}
      className={cn(
        "rounded-2xl p-5 cursor-pointer",
        s.online ? "neon-panel neon-panel-hover" : "neon-dashed bg-bg-card/30"
      )}
    >
      <div className="flex items-center gap-2.5">
        <span className={cn("w-2.5 h-2.5 rounded-full shrink-0", s.online ? "bg-emerald-400" : "bg-zinc-600")} />
        <span className={cn("text-[16px] font-semibold tracking-tight", !s.online && "text-zinc-400")}>{s.name}</span>
        <span className={cn(
          "text-[9.5px] font-semibold px-1.5 py-px rounded shrink-0",
          s.role === "primary" ? "bg-accent/15 text-accent-soft" : "bg-zinc-700/40 text-zinc-300"
        )}>
          {s.role}
        </span>
        <span className={cn(
          "text-[9.5px] font-semibold px-1.5 py-px rounded shrink-0",
          s.online ? "bg-emerald-500/15 text-emerald-300" : "bg-zinc-700/40 text-zinc-400"
        )}>
          {s.online ? "online" : "stale"}
        </span>
        <span className="ml-auto text-[10.5px] text-zinc-600 font-mono shrink-0">
          {s.role === "primary" ? "this host" : relTime(s.lastSeen)}
        </span>
      </div>
      <div className="grid grid-cols-3 gap-3 mt-4">
        <Fact k="apps" v={s.apps} />
        {s.role === "primary" ? (
          <Fact k="exposed" v={s.exposed} tone={s.exposed ? "#00ED64" : undefined} />
        ) : (
          <Fact k="assets" v={s.assets} />
        )}
        <Fact k="role" v={s.role} mono />
      </div>
    </motion.div>
  );
}

// "Add a server" — mints a join token, then shows the operator exactly how to
// enroll the secondary (it pushes outbound to this primary, so NAT-friendly).
function AddServerPanel({ qc }) {
  const [serverId, setServerId] = useState("");
  const [minted, setMinted] = useState(null);
  const mint = useMutation({
    mutationFn: (id) => endpoints.mintFederationToken(id).then((r) => r.data),
    onSuccess: (d) => {
      setMinted(d);
      qc.invalidateQueries({ queryKey: ["federation-servers"] });
    },
  });
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  return (
    <div className="neon-panel rounded-xl p-4 mb-4">
      <div className="text-[13px] text-zinc-200 mb-1 font-medium">Add a secondary server</div>
      <p className="text-[12px] text-zinc-500 mb-3">
        Mint a join token below, then on the new host run the InfraDocs setup wizard,
        choose role <span className="font-mono text-zinc-400">secondary</span>, and enter this
        primary's URL + the token. The secondary scans itself and pushes{" "}
        <span className="font-mono text-zinc-400">outbound</span> to this primary — no inbound
        ports required.
      </p>
      <div className="flex gap-2 items-center flex-wrap">
        <input
          value={serverId}
          onChange={(e) => setServerId(e.target.value)}
          placeholder="server id (e.g. n150)"
          className="bg-bg-elev border border-bg-hover rounded-lg px-3 py-1.5 text-[13px] text-zinc-200 font-mono focus:outline-none focus:border-accent/40"
        />
        <button
          onClick={() => mint.mutate(serverId.trim())}
          disabled={!serverId.trim() || mint.isPending}
          className="text-[12px] px-3 py-1.5 rounded-lg disabled:opacity-50 neon-glow border border-[var(--neon)] bg-[var(--neon)]/10 hover:bg-[var(--neon)]/20"
        >
          {mint.isPending ? "Minting…" : "Mint join token"}
        </button>
      </div>
      {mint.isError && (
        <div className="text-[12px] text-rose-300 mt-2">Failed to mint token — check you're signed in as the primary.</div>
      )}
      {minted && (
        <div className="mt-3 text-[12px]">
          <div className="text-zinc-400 mb-1">
            Join token for <span className="font-mono text-accent-soft">{minted.server_id}</span> — copy it now (shown once):
          </div>
          <div className="bg-black/40 border border-bg-hover rounded-lg p-3 font-mono text-[11.5px] text-zinc-200 break-all flex items-start gap-2">
            <span className="flex-1">{minted.token}</span>
            <button
              onClick={() => navigator.clipboard?.writeText(minted.token)}
              className="text-accent-soft hover:underline shrink-0"
            >
              copy
            </button>
          </div>
          <div className="text-zinc-500 mt-2">
            Primary URL for the wizard: <span className="font-mono text-zinc-300">{origin}</span>
          </div>
        </div>
      )}
    </div>
  );
}

// Live cluster: nodes + priorities + current leader + override + per-node reachability,
// plus the guarded manual promote and the override (pin) toggle. Promote first asks the
// backend (force=false); only if peers are unreachable do we surface the two-primaries
// warning + an explicit force button. We never auto-force.
function ClusterLeaderPanel() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["cluster-state"],
    queryFn: () => endpoints.clusterState().then((r) => r.data),
    refetchInterval: 5000,
  });
  const [pending, setPending] = useState(null);
  const [msg, setMsg] = useState(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["cluster-state"] });
  const promote = useMutation({
    mutationFn: (force) => endpoints.clusterPromote(force).then((r) => r.data),
    onSuccess: (d) => {
      if (d.promoted) {
        setPending(null);
        setMsg({ kind: "ok", text: d.forced ? `Force-promoted. ${d.warning || ""}` : "Promoted to primary." });
        refresh();
      } else if (d.needs_force) {
        setPending(d);
        setMsg(null);
      } else {
        setPending(null);
        setMsg({ kind: "err", text: d.reason || "Promotion refused." });
      }
    },
    onError: () => setMsg({ kind: "err", text: "Promotion request failed." }),
  });
  const override = useMutation({
    mutationFn: (value) => endpoints.clusterOverride(value).then((r) => r.data),
    onSuccess: refresh,
  });
  const d = q.data;
  if (!d) return null;
  const leader = d.current_leader;

  return (
    <div className="neon-panel rounded-xl p-4 mb-4">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[13px] text-zinc-200 font-medium">Cluster</span>
        {leader ? (
          <span className="text-[12px]">
            leader <span className="font-mono text-accent-soft">{leader}</span>
            {leader === d.node_id && <span className="text-emerald-300"> (this node)</span>}
          </span>
        ) : (
          <span className="text-[12px] text-amber-300">no current leader</span>
        )}
        {d.override && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-500/15 text-amber-300">override pinned</span>}
        {!d.majority && <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-rose-500/15 text-rose-300">no majority</span>}
        <span className="ml-auto text-[11px] text-zinc-500 font-mono">this node: {d.node_id} · p{d.priority ?? "—"}</span>
      </div>

      {/* roster: priority, reachability, leader marker */}
      <div className="mt-3 flex flex-col gap-1">
        {[...(d.nodes || [])].sort((a, b) => (a.priority ?? 99) - (b.priority ?? 99)).map((n) => (
          <div key={n.node_id} className="flex items-center gap-2 text-[12px]">
            <span className={cn("w-1.5 h-1.5 rounded-full", n.reachable ? "bg-emerald-400" : "bg-zinc-600")} />
            <span className="font-mono text-zinc-500 w-8">p{n.priority ?? "—"}</span>
            <span className={cn("font-mono", n.is_primary ? "text-accent-soft" : "text-zinc-300")}>{n.node_id}</span>
            {n.is_primary && <span className="text-[10px] text-emerald-300">primary</span>}
            {n.self && <span className="text-[10px] text-zinc-500">this node</span>}
            <span className="ml-auto text-[10.5px] text-zinc-600">{n.reachable ? "reachable" : "unreachable"}</span>
          </div>
        ))}
      </div>

      <div className="mt-3 flex items-center gap-2 flex-wrap">
        {leader === d.node_id ? (
          <button
            onClick={() => override.mutate(!d.override)}
            disabled={override.isPending}
            className="text-[12px] px-3 py-1.5 rounded-lg border border-white/10 text-zinc-300 hover:bg-bg-elev disabled:opacity-50"
          >
            {d.override ? "Clear override (allow elections)" : "Override: pin this primary"}
          </button>
        ) : (
          !pending && (
            <button
              onClick={() => { setMsg(null); promote.mutate(false); }}
              disabled={promote.isPending}
              className="text-[12px] px-3 py-1.5 rounded-lg neon-glow border border-[var(--neon)] bg-[var(--neon)]/10 hover:bg-[var(--neon)]/20 disabled:opacity-50"
            >
              {promote.isPending ? "Checking…" : "Promote this node to primary"}
            </button>
          )
        )}
      </div>

      {pending && (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-[12px] mt-2">
          <div className="text-amber-300 font-medium">⚠ Can't confirm the current primary is down</div>
          <div className="text-zinc-300 mt-1">{pending.warning}</div>
          <div className="flex gap-2 mt-2">
            <button
              onClick={() => promote.mutate(true)}
              disabled={promote.isPending}
              className="text-[12px] px-3 py-1.5 rounded-lg border border-rose-500/40 bg-rose-500/10 text-rose-200 hover:bg-rose-500/20"
            >
              Force promote anyway
            </button>
            <button
              onClick={() => setPending(null)}
              className="text-[12px] px-3 py-1.5 rounded-lg border border-white/10 text-zinc-300 hover:bg-bg-elev"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      {msg && (
        <div className={cn("text-[12px] mt-2", msg.kind === "ok" ? "text-emerald-300" : "text-rose-300")}>
          {msg.text}
        </div>
      )}
    </div>
  );
}

function Fact({ k, v, mono, tone }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-[0.08em] text-zinc-600">{k}</div>
      <div
        className={cn("text-[13px] text-zinc-200 mt-0.5", mono && "font-mono")}
        style={tone ? { color: tone } : undefined}
      >
        {v}
      </div>
    </div>
  );
}

export default function ServersLens({ apps, reduce, selected, setSelected }) {
  const qc = useQueryClient();
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => endpoints.health().then((r) => r.data),
  });
  const fed = useQuery({
    queryKey: ["federation-servers"],
    queryFn: () => endpoints.federationServers().then((r) => r.data),
  });
  const [adding, setAdding] = useState(false);

  const primaryId = health.data?.server;
  const secondaries = fed.data?.servers || [];

  const servers = useMemo(() => {
    const list = [];
    if (primaryId) {
      const own = apps.filter((a) => serverOfApp(a) === primaryId);
      list.push({
        id: primaryId,
        name: health.data?.server_name || primaryId,
        role: "primary",
        online: health.data?.status === "ok",
        apps: own.length,
        exposed: own.filter((a) => a.internet_exposed).length,
        assets: null,
        lastSeen: null,
      });
    }
    for (const s of secondaries) {
      if (s.server_id === primaryId) continue; // never double-list the primary
      const online = s.last_seen
        ? Date.now() - new Date(s.last_seen).getTime() < SERVER_STALE_MS
        : false;
      list.push({
        id: s.server_id,
        name: s.server_id,
        role: "secondary",
        online,
        apps: s.app_count ?? 0,
        assets: s.asset_count ?? 0,
        exposed: null,
        lastSeen: s.last_seen,
      });
    }
    return list;
  }, [apps, primaryId, secondaries, health.data]);

  const shown = selected ? servers.filter((s) => s.id === selected) : servers;
  const onlineCount = servers.filter((s) => s.online).length;
  const loading = health.isLoading || fed.isLoading;

  return (
    <>
      <div className="flex items-baseline gap-3 mb-4 flex-wrap">
        <h1 className="text-[21px] font-semibold tracking-tight">Servers</h1>
        <p className="text-[13px] text-zinc-500">
          {onlineCount} of {servers.length} reporting · federation mesh
        </p>
        <button
          onClick={() => setAdding((v) => !v)}
          className="ml-auto text-[12px] px-3 py-1.5 rounded-lg neon-glow border border-[var(--neon)] bg-[var(--neon)]/10 hover:bg-[var(--neon)]/20"
        >
          {adding ? "× Close" : "+ Add a server"}
        </button>
      </div>

      <ClusterLeaderPanel />

      {adding && <AddServerPanel qc={qc} />}

      {servers.length > 0 && (
        <div className="flex gap-1.5 mb-4 flex-wrap">
          <SwitchChip active={!selected} onClick={() => setSelected("")} label={`All (${servers.length})`} />
          {servers.map((s) => (
            <SwitchChip
              key={s.id}
              active={selected === s.id}
              onClick={() => setSelected(s.id)}
              label={s.name}
              dot={s.online ? "bg-emerald-400" : "bg-zinc-600"}
            />
          ))}
        </div>
      )}

      {loading && <div className="text-zinc-400 text-sm">Loading fleet…</div>}

      <motion.div
        variants={gridVariants(reduce)}
        initial="hidden"
        animate="show"
        className="grid gap-4"
        style={{ gridTemplateColumns: "repeat(auto-fill,minmax(340px,1fr))" }}
      >
        {shown.map((s) => (
          <ServerCard key={s.id} s={s} reduce={reduce} onOpen={() => setSelected(s.id)} />
        ))}
      </motion.div>

      {!loading && servers.length === 0 && (
        <div className="text-sm text-zinc-500">
          No servers reporting yet. Use “Add a server” to enroll a secondary.
        </div>
      )}
    </>
  );
}
