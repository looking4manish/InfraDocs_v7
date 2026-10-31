import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { MoreHorizontal } from "lucide-react";
import { endpoints } from "../api/client";
import { useAllowedActions } from "../hooks/useAllowedActions";
import { ACTION_META, actionsFor, isDestructive } from "../lib/actions";
import { useActionRunner } from "../hooks/useActionRunner";
import { Btn } from "./ui";
import { useOutside } from "../hooks/useOutside";

// Icon buttons for one asset: the first allowed actions inline, the rest in a menu.
// entity: { category, asset_id?, name }
export default function ActionBar({ entity, inline = 2 }) {
  const { allowed, destructive } = useAllowedActions();
  const [open, setOpen] = useState(false);
  const ref = useOutside(() => setOpen(false));
  const runner = useActionRunner([["applications"], ["assets"]]);
  const category = entity?.category;
  const byName = useQuery({
    queryKey: ["asset-by-name", category, entity?.name],
    queryFn: () => endpoints.listAssets({ category }).then((r) => (r.data.assets || []).find((a) => a.name === entity?.name) || null),
    enabled: Boolean(entity && !entity.asset_id && category),
    staleTime: 60000,
  });
  const acts = actionsFor(category, allowed);
  const id = entity?.asset_id || byName.data?.asset_id;
  if (!acts.length || !id) return null;
  const isSelf = /^infradocs/.test(entity.name || "");
  const blocked = (a) => isSelf && isDestructive(category, a, destructive);
  const go = (a) => runner.run({
    action: a, target: entity.name,
    fire: () => endpoints.fireAssetAction(id, a, a === "logs" ? { tail: 200 } : {}),
  });
  const shown = acts.slice(0, inline);
  const rest = acts.slice(inline);
  return (
    <span className="acts" ref={ref} style={{ position: "relative" }}>
      {shown.map((a) => (
        <Btn key={a} iconOnly label={blocked(a) ? `${ACTION_META[a]?.label} (protected)` : ACTION_META[a]?.label || a}
          icon={ACTION_META[a]?.icon} danger={a === "stop"} disabled={blocked(a) || runner.busy}
          busy={runner.busyAction === a} onClick={(e) => { e.stopPropagation(); go(a); }} />
      ))}
      {rest.length > 0 && (
        <Btn iconOnly label="More actions" icon={MoreHorizontal} onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }} />
      )}
      {open && (
        <div className="menu">
          {rest.map((a) => {
            const I = ACTION_META[a]?.icon;
            return (
              <button key={a} type="button" disabled={blocked(a)} onClick={(e) => { e.stopPropagation(); setOpen(false); go(a); }}>
                {I && <I size={14} />}{ACTION_META[a]?.label || a}
              </button>
            );
          })}
        </div>
      )}
      {runner.ui}
    </span>
  );
}
