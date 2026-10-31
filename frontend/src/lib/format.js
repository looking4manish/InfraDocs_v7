// Formatting helpers shared by every screen.

export function toDate(v) {
  if (!v) return null;
  if (v instanceof Date) return v;
  const s = typeof v === "string" && !/[zZ]|[+-]\d\d:?\d\d$/.test(v) ? v + "Z" : v;
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function relTime(v) {
  const d = toDate(v);
  if (!d) return "never";
  const s = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
  if (s < 60) return `${s} s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h ago`;
  return `${Math.round(h / 24)} d ago`;
}

export function ageSeconds(v) {
  const d = toDate(v);
  return d ? Math.max(0, (Date.now() - d.getTime()) / 1000) : Infinity;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function shortDateTime(v) {
  const d = toDate(v);
  if (!d) return "—";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
export function shortDate(v) {
  const d = toDate(v);
  if (!d) return "—";
  return `${d.getDate()} ${MONTHS[d.getMonth()]}`;
}
export function isoDay(v) {
  const d = toDate(v);
  return d ? d.toISOString().slice(0, 10) : "—";
}
// "14:02" today, otherwise "1 Nov 00:16".
export function timeOfDay(v) {
  const d = toDate(v);
  if (!d) return "—";
  const p = (n) => String(n).padStart(2, "0");
  const t = `${p(d.getHours())}:${p(d.getMinutes())}`;
  return d.toDateString() === new Date().toDateString() ? t : `${d.getDate()} ${MONTHS[d.getMonth()]} ${t}`;
}

export function clock(v) {
  const d = toDate(v);
  if (!d) return "—";
  const p = (n) => String(n).padStart(2, "0");
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay ? `${p(d.getHours())}:${p(d.getMinutes())}` : `${d.getDate()} ${MONTHS[d.getMonth()]}`;
}

export function bytes(n) {
  if (n == null || Number.isNaN(n)) return "—";
  if (n < 1024) return `${n} B`;
  const u = ["KB", "MB", "GB", "TB", "PB"];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  const t = v >= 100 ? String(Math.round(v)) : v.toFixed(1).replace(/\.0$/, "");
  return `${t} ${u[i]}`;
}
export function bytesParts(n) {
  const s = bytes(n);
  const [v, unit] = s.split(" ");
  return { v, unit: unit || "" };
}

export function hostOf(url) {
  if (!url) return "";
  return String(url).replace(/^[a-z]+:\/\//i, "").replace(/\/$/, "");
}

export function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many || one + "s"}`;
}

export function errDetail(e, fallback = "Request failed") {
  const d = e?.response?.data?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d[0]?.msg) return d[0].msg;
  return e?.message || fallback;
}
