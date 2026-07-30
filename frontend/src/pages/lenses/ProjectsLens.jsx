import { useMemo } from "react";
import { motion } from "motion/react";
import { Container, Cog, Globe, HardDrive, Package } from "lucide-react";
import ActionButton from "../../components/ActionButton";
import { endpoints } from "../../api/client";
import { formatBytes } from "../../components/Bytes";
import { cn } from "../../lib/cn";
import { SPRING, gridVariants, cardVariants } from "./motion";

function hasRuntime(a) {
  return (
    (a.components_count ?? 0) > 0 ||
    (a.containers?.length || 0) > 0 ||
    (a.systemd_units?.length || 0) > 0 ||
    (a.listening_ports?.length || 0) > 0
  );
}

// Attention weight: down/exposed-but-idle rises; healthy sinks.
function attentionScore(a) {
  let s = 0;
  if (a.internet_exposed && !hasRuntime(a)) s += 100; // exposed but nothing running
  if (!hasRuntime(a)) s += 10;
  if (a.internet_exposed) s += 5;
  s += a.components_count ?? 0;
  return -s; // higher concern first
}

function Chip({ icon: Icon, n, label }) {
  return (
    <span title={label} className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border border-bg-hover text-[11px] text-zinc-400">
      <Icon size={12} className="opacity-80" /> {n}
    </span>
  );
}

function appFireWrapper(name, action) {
  return () =>
    endpoints.fireApplicationAction(name, action).then((res) => {
      const d = res?.data || {};
      const lines = (d.results || []).map(
        (r) => `${r.status === "success" ? "✓" : r.status === "skipped" ? "·" : "✗"} ${r.category} ${r.asset_name} → ${r.status}`
      );
      const ok = (d.results || []).filter((r) => r.status === "success").length;
      return { data: { status: ok > 0 ? "success" : "failed",
        stdout: `${action} on ${name} — ${d.targets || 0} target(s), ${ok} ok\n\n` + lines.join("\n"),
        stderr: "" } };
    });
}

function AppActionRow({ name }) {
  return (
    <div className="absolute bottom-3 right-3 z-[2] flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity"
      onClick={(e) => { e.stopPropagation(); e.preventDefault(); }}>
      {["restart", "up", "down"].map((v) => (
        <ActionButton key={v} action={v} label={v} size="xs" fire={appFireWrapper(name, v)} />
      ))}
    </div>
  );
}

function ProjectLensCard({ app, onOpen, reduce }) {
  const live = hasRuntime(app);
  const dot = !live ? "bg-zinc-600" : app.internet_exposed ? "bg-emerald-400" : "bg-sky-400";
  return (
    <motion.div
      variants={cardVariants(reduce)}
      whileHover={reduce ? undefined : { y: -2 }}
      transition={SPRING}
      className="group text-left neon-panel neon-panel-hover rounded-2xl p-4 hover:bg-bg-elev relative overflow-hidden"
    >
      <button type="button" onClick={() => onOpen(app.name)} className="absolute inset-0 z-0" aria-label={`Open ${app.name}`} />
      <span className="pointer-events-none absolute top-4 right-4 text-accent-soft opacity-0 -translate-x-1 group-hover:opacity-100 group-hover:translate-x-0 transition-all duration-200 text-sm">→</span>
      <div className="pointer-events-none flex items-center gap-2">
        <span className={cn("w-2 h-2 rounded-full shrink-0", dot)} />
        <span className={cn("text-[15px] font-semibold tracking-tight truncate", !live && "text-zinc-500")}>{app.name}</span>
        <span className="text-[9.5px] font-semibold px-1.5 py-px rounded bg-accent/15 text-accent-soft shrink-0">{app.type}</span>
        {app.internet_exposed && (
          <span className="text-[9.5px] font-semibold px-1.5 py-px rounded bg-emerald-500/15 text-emerald-300 shrink-0">{app.cloudflare ? "CF" : "live"}</span>
        )}
      </div>
      {app.urls?.length > 0 ? (
        <div className="pointer-events-none text-[12px] text-zinc-500 mt-2 truncate">{app.urls[0].replace(/^https?:\/\//, "")}</div>
      ) : (
        <div className="pointer-events-none text-[12px] text-zinc-600 mt-2 truncate">{live ? "no public url" : "folder tracked only"}</div>
      )}
      <div className="pointer-events-none text-[12px] text-mono text-zinc-400 font-mono tabular-nums mt-2">
        {app.components_count ?? 0} comp
        <span className="text-zinc-700 mx-1.5">·</span>
        {app.listening_ports?.length || 0} ports
        <span className="text-zinc-700 mx-1.5">·</span>
        {formatBytes(app.total_size_bytes)}
      </div>
      <div className="pointer-events-none flex flex-wrap gap-1.5 mt-3">
        {app.containers?.length > 0 && <Chip icon={Container} n={app.containers.length} label="containers" />}
        {app.systemd_units?.length > 0 && <Chip icon={Cog} n={app.systemd_units.length} label="services" />}
        {app.nginx_sites?.length > 0 && <Chip icon={Globe} n={app.nginx_sites.length} label="nginx" />}
        {app.volumes?.length > 0 && <Chip icon={HardDrive} n={app.volumes.length} label="volumes" />}
        {app.images?.length > 0 && <Chip icon={Package} n={app.images.length} label="images" />}
      </div>
    <AppActionRow name={app.name} />
    </motion.div>
  );
}

export default function ProjectsLens({ apps, onOpen, reduce }) {
  const sorted = useMemo(
    () => [...apps].sort((a, b) => attentionScore(a) - attentionScore(b)),
    [apps]
  );
  return (
    <>
      <div className="flex items-baseline gap-3 mb-4">
        <h1 className="text-[21px] font-semibold tracking-tight">Projects</h1>
        <p className="text-[13px] text-zinc-500">
          One card per <span className="font-mono text-zinc-400">~/projects/&lt;name&gt;</span> — attention-sorted
        </p>
      </div>
      <motion.div
        variants={gridVariants(reduce)}
        initial="hidden"
        animate="show"
        className="grid gap-3"
        style={{ gridTemplateColumns: "repeat(auto-fill,minmax(280px,1fr))" }}
      >
        {sorted.map((a) => (
          <ProjectLensCard key={a.application_id || a.name} app={a} onOpen={onOpen} reduce={reduce} />
        ))}
      </motion.div>
    </>
  );
}
