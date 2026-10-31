// Replica-set diagram: the primary on top, the other members below, heartbeat links between them.
const COL = { primary: "#4dffad", secondary: "#7db0ff", nonvoting: "#7c9286", down: "#fb889a", candidate: "#ffcb6b" };

export default function ReplicaSetMap({ members, heartbeatS }) {
  const prim = members.find((m) => m.role === "primary");
  const rest = members.filter((m) => m !== prim);
  const W = 720;
  const top = { x: W / 2, y: 92 };
  const n = rest.length;
  const span = Math.min(520, 190 * Math.max(1, n - 1));
  const placed = rest.map((m, i) => ({ m, x: n === 1 ? W / 2 : W / 2 - span / 2 + (span / (n - 1)) * i, y: 220 }));
  const kind = (m) => (m.role === "unreachable" || !m.healthy ? "down" : m.role === "candidate" ? "candidate" : m.votes === 0 ? "nonvoting" : "secondary");
  const nodes = prim ? [{ m: prim, ...top, p: true }, ...placed] : placed.map((p, i) => (i === 0 ? { ...p, ...top } : p));

  return (
    <svg id="rsmap" viewBox={`0 0 ${W} 300`} preserveAspectRatio="xMidYMid meet" role="img"
      aria-label={`Replica set of ${members.length} members${prim ? `, ${prim.node_id} primary` : ", no primary"}`}>
      <defs>
        <radialGradient id="rs-core" cx="50%" cy="40%" r="60%"><stop offset="0" stopColor="#1d3a2c" /><stop offset="1" stopColor="#0b0f0d" /></radialGradient>
        <radialGradient id="rs-halo"><stop offset="0" stopColor="rgb(0 232 135)" stopOpacity=".3" /><stop offset="1" stopColor="rgb(0 232 135)" stopOpacity="0" /></radialGradient>
        <filter id="rs-gl" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="3" /></filter>
      </defs>
      {prim && placed.map(({ m, x, y }) => {
        const d = `M${top.x},${top.y} L${x},${y}`;
        const down = kind(m) === "down";
        return (
          <g key={`l-${m.node_id}`}>
            <path d={d} stroke="rgb(78 84 81)" strokeWidth="1.5" fill="none" />
            {!down && <path d={d} stroke="#4dffad" strokeWidth="2" fill="none" className="flow" opacity=".9" />}
            {!down && (
              <>
                <circle r="3.5" fill="#4dffad" filter="url(#rs-gl)"><animateMotion dur="2s" repeatCount="indefinite" path={d} /></circle>
                <circle r="2" fill="#eafff4"><animateMotion dur="2s" repeatCount="indefinite" path={d} /></circle>
              </>
            )}
          </g>
        );
      })}
      {placed.length > 1 && placed.length <= 4 && placed.slice(1).map((p, i) => {
        const a = placed[i];
        const d = `M${a.x},${a.y} L${p.x},${p.y}`;
        return (
          <g key={`s-${i}`}>
            <path d={d} stroke="rgb(78 84 81)" strokeWidth="1.5" fill="none" />
            <path d={d} stroke="#7db0ff" strokeWidth="2" fill="none" className="flow" opacity=".45" />
          </g>
        );
      })}
      {placed.length > 1 && heartbeatS && (
        <text x={W / 2} y="234" textAnchor="middle" fill="#7c9286" fontSize="11" fontWeight="700" letterSpacing="1.2">HEARTBEAT · {heartbeatS} s</text>
      )}
      {nodes.map(({ m, x, y, p }) => {
        const k = p ? "primary" : kind(m);
        const R = p ? 40 : 32;
        const col = COL[k];
        return (
          <g key={m.node_id}>
            {p && <circle cx={x} cy={y} r={R + 40} fill="url(#rs-halo)" />}
            {p && <circle className="pulse" cx={x} cy={y} r={R + 6} fill="none" stroke="#4dffad" strokeWidth="1.5" />}
            <circle cx={x} cy={y} r={R} fill="url(#rs-core)" stroke={col} strokeWidth={p ? 2.2 : 1.5}
              strokeDasharray={k === "nonvoting" ? "4 4" : undefined}
              style={{ filter: `drop-shadow(0 0 ${p ? 14 : 8}px ${p ? "rgb(0 232 135 / .6)" : k === "down" ? "rgb(251 136 154 / .5)" : "rgb(125 176 255 / .45)"})` }} />
            <text x={x} y={y - 2} textAnchor="middle" fill={col} fontFamily="JetBrains Mono" fontSize={p ? 22 : 18} fontWeight="500">
              {p ? "P" : k === "nonvoting" ? "N" : k === "down" ? "!" : "S"}
            </text>
            <text x={x} y={y + (p ? 18 : 15)} textAnchor="middle" fill="#f2fbf6" fontSize="12" fontWeight="800">{m.node_id}</text>
            {!p && (
              <text x={x} y={y + R + 20} textAnchor="middle" fill={k === "down" ? "#fb889a" : "#96b0a2"} fontSize="11.5" fontWeight="700">
                {k === "down" ? "Unreachable" : m.votes === 0 ? "No vote" : "1 vote"}
              </text>
            )}
          </g>
        );
      })}
      {prim && <text x={W / 2} y="22" textAnchor="middle" fill="#96b0a2" fontSize="11.5" fontWeight="700">Primary · {prim.votes ? "1 vote" : "no vote"} · priority {prim.priority}</text>}
      {!prim && <text x={W / 2} y="22" textAnchor="middle" fill="#ffcb6b" fontSize="11.5" fontWeight="700">No primary</text>}
    </svg>
  );
}
