// Action presentation. The backend (/api/actions/allowed) is authoritative on what is
// permitted and destructive; this only decides labels, order and the primary button.
import {
  Play, Square, RotateCw, RefreshCw, FileText, Activity, ArrowUp, ArrowDown, FlaskConical,
  Search, BarChart3, ToggleRight, ToggleLeft, Download, ArrowUpCircle, Trash2, PlayCircle,
} from "lucide-react";

export const ACTION_META = {
  start: { label: "Start", icon: Play },
  stop: { label: "Stop", icon: Square },
  restart: { label: "Restart", icon: RotateCw },
  logs: { label: "Logs", icon: FileText },
  status: { label: "Status", icon: Activity },
  inspect: { label: "Inspect", icon: Search },
  stats: { label: "Stats", icon: BarChart3 },
  enable: { label: "Enable", icon: ToggleRight },
  disable: { label: "Disable", icon: ToggleLeft },
  up: { label: "Start", icon: ArrowUp },
  down: { label: "Stop", icon: ArrowDown },
  reload: { label: "Reload", icon: RefreshCw },
  test: { label: "Test config", icon: FlaskConical },
  pull: { label: "Pull", icon: Download },
  prune: { label: "Prune", icon: Trash2 },
  recreate: { label: "Recreate", icon: RefreshCw },
  trigger: { label: "Run now", icon: PlayCircle },
  update: { label: "Update", icon: ArrowUpCircle },
  check_update: { label: "Check for update", icon: RefreshCw },
};

// Per category: display order of actions and the one shown as the primary button.
export const CATEGORY_ACTIONS = {
  docker_container: { actions: ["logs", "restart", "stop", "start", "inspect", "stats", "check_update"], primary: "logs" },
  docker_compose: { actions: ["update", "up", "restart", "recreate", "down"], primary: "update" },
  systemd_service: { actions: ["status", "logs", "restart", "start", "stop", "enable", "disable"], primary: "status" },
  systemd_timer: { actions: ["status", "trigger", "restart", "start", "stop", "enable", "disable"], primary: "status" },
  nginx_server_block: { actions: ["test", "reload"], primary: "test" },
  docker_image: { actions: ["check_update", "pull", "prune"], primary: "check_update" },
  docker_volume: { actions: ["inspect", "prune"], primary: "inspect" },
  storage_mount: { actions: ["inspect"], primary: "inspect" },
};

export const MUTATING = new Set(["start", "stop", "restart", "up", "down", "reload", "update", "recreate", "prune", "pull", "enable", "disable", "trigger"]);
export const TEXT_OUTPUT = new Set(["logs", "status", "test", "check_update", "inspect", "stats"]);

export function actionsFor(category, allowedMap) {
  const reg = CATEGORY_ACTIONS[category];
  if (!reg) return [];
  const allowed = new Set(allowedMap?.[category] || []);
  return reg.actions.filter((a) => allowed.has(a));
}

export function isDestructive(category, action, destructiveMap) {
  return new Set(destructiveMap?.[category] || []).has(action);
}
