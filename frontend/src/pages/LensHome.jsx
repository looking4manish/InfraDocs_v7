import { useSearchParams, useNavigate } from "react-router-dom";
import { motion, AnimatePresence, useReducedMotion } from "motion/react";
import { useQuery } from "@tanstack/react-query";
import { endpoints } from "../api/client";
import { cn } from "../lib/cn";
import Dashboard from "./Dashboard";
import ProjectsLens from "./lenses/ProjectsLens";
import ServersLens from "./lenses/ServersLens";
import WebLens from "./lenses/WebLens";

const LENSES = ["Dashboard", "Projects", "Servers", "Web", "Assets"];

export default function LensHome() {
  const [params, setParams] = useSearchParams();
  const lens = LENSES.includes(params.get("lens")) ? params.get("lens") : "Dashboard";
  const setLens = (l) =>
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      if (l === "Dashboard") next.delete("lens");
      else next.set("lens", l);
      return next;
    });
  const selectedServer = params.get("server") || "";
  const setSelectedServer = (id) =>
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      if (!id) next.delete("server");
      else next.set("server", id);
      return next;
    });
  const reduce = useReducedMotion();
  const navigate = useNavigate();
  const q = useQuery({
    queryKey: ["applications"],
    queryFn: () => endpoints.listApplications().then((r) => r.data),
  });

  const all = q.data?.applications || [];
  const projects = all.filter((a) => a.type === "project");
  const open = (name) => navigate(`/applications?sel=${encodeURIComponent(name)}`);

  return (
    <div>
      <div className="flex gap-1.5 mb-6">
        {LENSES.map((l) => (
          <button
            key={l}
            onClick={() => (l === "Assets" ? navigate("/assets") : setLens(l))}
            className={cn(
              "px-3 py-1.5 rounded-lg text-[13px] font-medium border transition",
              lens === l
                ? "text-accent-soft bg-accent/10 border-accent/30"
                : "text-zinc-400 border-transparent hover:text-zinc-100 hover:bg-bg-elev"
            )}
          >
            {l}
          </button>
        ))}
      </div>

      {q.isLoading && (
        <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(280px,1fr))" }}>
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="h-[132px] rounded-2xl bg-bg-card border border-bg-hover skeleton-shimmer" />
          ))}
        </div>
      )}

      {!q.isLoading && (
        <AnimatePresence mode="wait">
          <motion.div
            key={lens}
            initial={reduce ? { opacity: 0 } : { opacity: 0, y: 4 }}
            animate={reduce ? { opacity: 1 } : { opacity: 1, y: 0 }}
            exit={reduce ? { opacity: 0 } : { opacity: 0, y: -4 }}
            transition={{ duration: 0.16 }}
          >
            {lens === "Projects" && <ProjectsLens apps={projects} onOpen={open} reduce={reduce} />}
            {lens === "Servers" && (
              <ServersLens apps={all} reduce={reduce} selected={selectedServer} setSelected={setSelectedServer} />
            )}
            {lens === "Web" && <WebLens />}
            {lens === "Dashboard" && <Dashboard />}
            {lens === "Assets" && (
              <div className="flex items-baseline gap-3 mb-4">
                <h1 className="text-[21px] font-semibold tracking-tight">Assets</h1>
                <p className="text-[13px] text-zinc-500">
                  Open the <button onClick={() => navigate("/assets")} className="text-accent-soft hover:underline">full asset table →</button>
                </p>
              </div>
            )}
          </motion.div>
        </AnimatePresence>
      )}
    </div>
  );
}
