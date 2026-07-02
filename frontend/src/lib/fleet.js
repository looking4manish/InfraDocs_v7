// Selected-node store for the fleet node-switcher (plain module + a tiny React hook).
// null selection == the leader ("this node"). Lens API calls are repointed to the
// selected node by the axios interceptor in api/client.js.
import { useEffect, useState } from "react";

let _node = null;   // node_id currently being viewed (null → leader/this node)
let _leader = null; // the node actually serving the UI
const subs = new Set();

export function getLeader() { return _leader; }
export function setLeader(id) { _leader = id; }
export function getSelectedNode() { return _node; }
export function isLeaderSelected() { return _node == null || _node === _leader; }
export function setSelectedNode(id) { _node = id; subs.forEach((f) => { try { f(id); } catch { /* noop */ } }); }
export function subscribeNode(fn) { subs.add(fn); return () => subs.delete(fn); }
export function useSelectedNode() {
  const [n, setN] = useState(_node);
  useEffect(() => subscribeNode(setN), []);
  return n;
}
export function relTime(iso) {
  if (!iso) return "an unknown time ago";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "an unknown time ago";
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
