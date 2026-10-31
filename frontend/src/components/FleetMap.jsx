// The fleet constellation: the primary in the middle, secondaries around it, each
// server ringed by one dot per project coloured by its state.
const C = { ok: "#4dffad", warn: "#ffcb6b", danger: "#fb889a", idle: "#7c9286" };
const SLOTS = [[138, 112], [626, 120], [150, 316], [612, 316], [60, 220], [700, 220]];

function ServerIcon({ x, y, scale, color }) {
  return (
    <g transform={`translate(${x},${y}) scale(${scale})`} color={color}>
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <rect width="20" height="8" x="2" y="2" rx="2" ry="2" />
        <rect width="20" height="8" x="2" y="14" rx="2" ry="2" />
        <line x1="6" x2="6.01" y1="6" y2="6" />
        <line x1="6" x2="6.01" y1="18" y2="18" />
      </svg>
    </g>
  );
}

// nodes: [{ id, primary: bool, warn: bool, apps: [sev...], role, note }]
export default function FleetMap({ nodes }) {
  const prim = nodes.find((n) => n.primary) || nodes[0];
  const others = nodes.filter((n) => n !== prim);
  const H = others.length > 2 ? 440 : 360;
  const P = { ...prim, x: 380, y: 186 };
  const placed = [P, ...others.map((n, i) => ({ ...n, x: SLOTS[i % SLOTS.length][0], y: SLOTS[i % SLOTS.length][1] }))];

  return (
    <svg className="map" viewBox={`0 0 760 ${H}`} preserveAspectRatio="xMidYMid meet" role="img"
      aria-label={`Fleet of ${nodes.length} server${nodes.length === 1 ? "" : "s"}`} style={{ minHeight: H }}>
      <defs>
        <radialGradient id="fm-core" cx="50%" cy="40%" r="60%"><stop offset="0" stopColor="#1d3a2c" /><stop offset="1" stopColor="#0b0f0d" /></radialGradient>
        <radialGradient id="fm-halo"><stop offset="0" stopColor="rgb(0 232 135)" stopOpacity=".28" /><stop offset="1" stopColor="rgb(0 232 135)" stopOpacity="0" /></radialGradient>
        <radialGradient id="fm-haloW"><stop offset="0" stopColor="#ffcb6b" stopOpacity=".22" /><stop offset="1" stopColor="#ffcb6b" stopOpacity="0" /></radialGradient>
        <filter id="fm-gl" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="3" /></filter>
      </defs>

      {placed.slice(1).map((sv, k) => {
        const mx = (sv.x + P.x) / 2;
        const my = Math.min(sv.y, P.y) - 40;
        const d = `M${sv.x},${sv.y} Q${mx},${my} ${P.x},${P.y}`;
        const col = sv.warn ? "#ffcb6b" : "#4dffad";
        return (
          <g key={`l-${sv.id}`}>
            <path d={d} fill="none" stroke="rgb(78 84 81)" strokeWidth="1.5" />
            <path d={d} fill="none" stroke={col} strokeWidth="2" className="flow" opacity={sv.warn ? 0.45 : 0.85} />
            {!sv.warn && (
              <>
                <circle r="3.5" fill={col} filter="url(#fm-gl)"><animateMotion dur={`${2.6 + k * 0.7}s`} repeatCount="indefinite" path={d} /></circle>
                <circle r="2.2" fill="#eafff4"><animateMotion dur={`${2.6 + k * 0.7}s`} repeatCount="indefinite" path={d} /></circle>
              </>
            )}
          </g>
        );
      })}

      <g opacity=".9">
        <text x="380" y="28" textAnchor="middle" fill="#96b0a2" fontSize="11" fontWeight="800" letterSpacing="1.6">INTERNET</text>
        <line x1="380" y1="36" x2="380" y2="118" stroke="rgb(78 84 81)" strokeWidth="1.5" strokeDasharray="3 5" />
      </g>

      {placed.map((sv) => {
        const big = sv === P;
        const R = big ? 48 : 38;
        const orbit = R + 26;
        const ring = sv.warn ? "#ffcb6b" : "#4dffad";
        return (
          <g key={sv.id}>
            <circle cx={sv.x} cy={sv.y} r={R + 46} fill={`url(#${sv.warn ? "fm-haloW" : "fm-halo"})`} />
            <circle cx={sv.x} cy={sv.y} r={orbit} fill="none" stroke="rgb(255 255 255 / .07)" strokeDasharray="2 5" />
            {sv.apps.map((st, i) => {
              const a = -Math.PI / 2 + (i / Math.max(1, sv.apps.length)) * Math.PI * 2 + 0.35;
              const x = sv.x + Math.cos(a) * orbit;
              const y = sv.y + Math.sin(a) * orbit;
              return (
                <g key={i}>
                  {st === "danger" && <circle className="pulse" cx={x} cy={y} r="9" fill="none" stroke={C[st]} strokeWidth="2" />}
                  <circle cx={x} cy={y} r={sv.apps.length > 16 ? 4.5 : 6} fill={C[st] || C.idle}
                    style={st !== "idle" ? { filter: `drop-shadow(0 0 5px ${C[st]})` } : undefined} />
                </g>
              );
            })}
            <circle cx={sv.x} cy={sv.y} r={R} fill="url(#fm-core)" stroke={ring} strokeWidth={big ? 2 : 1.5}
              style={{ filter: `drop-shadow(0 0 ${big ? 14 : 9}px ${sv.warn ? "rgb(255 203 107 / .5)" : "rgb(0 232 135 / .55)"})` }} />
            <ServerIcon x={sv.x - (big ? 13 : 11)} y={sv.y - (big ? 22 : 19)} scale={big ? 1.1 : 0.92} color={ring} />
            <text x={sv.x} y={sv.y + (big ? 20 : 17)} textAnchor="middle" fill="#f2fbf6" fontSize={big ? 14 : 12.5} fontWeight="800">{sv.id}</text>
            <text x={sv.x} y={sv.y + orbit + 30} textAnchor="middle" fill={sv.warn ? "#ffcb6b" : "#bcd4c8"} fontSize="12.5" fontWeight="700">{sv.role}</text>
            <text x={sv.x} y={sv.y + orbit + 47} textAnchor="middle" fill="#7c9286" fontSize="12" fontWeight="600">{sv.note}</text>
          </g>
        );
      })}
    </svg>
  );
}
