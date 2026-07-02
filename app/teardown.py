"""Project teardown — the 'Kill Button'. Dry-run first, backup before delete.

SAFETY MODEL (do not weaken without care — this destroys production state):
  - dry_run is the DEFAULT; execution requires confirm == project name (endpoint).
  - REFUSES: non-project apps, the System bucket, and any project_dir that fails the
    STRUCTURAL catastrophe guards (root /, $HOME, a top-level dir, or a path that resolves
    into a system tree like /etc,/usr,/var,…). It validates the app's OWN discovered
    project_dir wherever it lives — NOT a single blessed root — with the blast-radius
    preview + type-to-confirm as the operator gate.
  - SKIPS shared assets (an image/cert/volume another app needs) — never removed.
  - BACKS UP first to <projects_root>/backups/<project>/<timestamp>/. Backups are
    `critical` ops ordered before every deletion; if any backup fails the executor
    ABORTS before deleting anything.
  - audits every step via the actions log.

Intentionally NOT removed: TLS certs (certbot-managed, often shared) and images
(shared by nature; prune separately). Those are left and surfaced as 'skipped'.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.blast_radius import compute_blast_radius

PROTECTED_PREFIX = "infradocs-v6-"


class TeardownRefused(Exception):
    pass


def _vol_name(v: Any) -> str:
    return v.get("name") if isinstance(v, dict) else v


# System trees a torn-down app dir can NEVER be (the path itself or anything under it).
# This is the catastrophe backstop, NOT a project-root allow-list — real apps live under
# any root (/home/data, /opt, /srv, /data, …) and are torn down where they actually are.
_FORBIDDEN_TREES = ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64",
                    "/boot", "/proc", "/sys", "/dev", "/run", "/var")


def _structural_refusal(project_dir: Optional[str]) -> Optional[str]:
    """Reason to refuse rm -rf of `project_dir`, or None if structurally safe. Independent of
    any blessed root: .resolve() normalises symlinks + .. so a path can't escape, then we
    refuse only genuinely catastrophic targets — never a legitimate app dir wherever it lives."""
    if not project_dir or not str(project_dir).strip():
        return "project_dir is empty — refusing to remove anything"
    p = Path(project_dir).resolve()
    s = str(p)
    if s == "/":
        return "refusing to remove the filesystem root /"
    if len([x for x in p.parts if x not in ("/", "")]) < 2:
        return f"refusing to remove top-level directory {s} — a real app dir is at least two levels deep"
    if p == Path(os.path.expanduser("~")).resolve():
        return f"refusing to remove the home directory itself ({s})"
    for t in _FORBIDDEN_TREES:
        if s == t or s.startswith(t + "/"):
            return f"refusing to remove {s}: it is inside the system tree {t}"
    return None


def _teardown_dir(app: Dict[str, Any]) -> Optional[str]:
    """The dir teardown may remove = the app's OWN discovered project_dir (the real host path
    the scanner recorded, read via the /host mount), resolved — IF it passes the structural
    catastrophe guards. No fixed-prefix rule: an app is torn down wherever it truly lives."""
    pd = app.get("project_dir")
    if not pd or _structural_refusal(pd):
        return None
    return str(Path(pd).resolve())


def build_plan(
    app: Dict[str, Any], all_apps: List[Dict[str, Any]], *, projects_root: str
) -> Dict[str, Any]:
    """Read-only ordered teardown plan. Skips shared/protected; never executes."""
    name = app["name"]
    refusals: List[str] = []
    if app.get("type") != "project":
        refusals.append("only project applications can be torn down")
    if name == "System":
        refusals.append("refusing to tear down the System bucket")

    safe_dir = _teardown_dir(app)
    pd = app.get("project_dir")
    if pd and not safe_dir:
        # Only a structurally-dangerous dir is refused now — NOT merely living outside a
        # blessed root. The discovered dir + blast-radius preview + type-to-confirm are the gate.
        refusals.append(_structural_refusal(pd) or f"project_dir {pd} could not be validated for removal")

    br = compute_blast_radius(app, all_apps)
    shared = {i["name"] for i in br["items"] if i["shared"]}
    skipped: List[Dict[str, Any]] = [
        {"name": i["name"], "category": i["category"],
         "reason": "shared with " + ", ".join(i["shared_with"])}
        for i in br["items"] if i["shared"]
    ]

    vols = [v for v in app.get("volumes", []) if _vol_name(v) not in shared]
    ops: List[Dict[str, Any]] = []

    # ---- backups first (critical: failure aborts the whole teardown) ----
    if safe_dir:
        ops.append({"op": "backup_dir", "target": safe_dir,
                    "label": f"backup {safe_dir}", "critical": True})
    for v in vols:
        vn = _vol_name(v)
        ops.append({"op": "backup_volume", "target": vn,
                    "mountpoint": v.get("mountpoint") if isinstance(v, dict) else None,
                    "label": f"backup volume {vn}", "critical": True})

    # ---- teardown (ordered: runtime -> nginx -> data) ----
    for c in app.get("containers", []):
        if c in shared or str(c).startswith(PROTECTED_PREFIX):
            continue
        ops.append({"op": "remove_container", "target": c,
                    "label": f"stop + remove container {c}", "destructive": True})

    if app.get("compose_file"):
        ops.append({"op": "compose_down", "target": app["compose_file"],
                    "label": "docker compose down (volumes kept)", "destructive": True})

    for u in app.get("systemd_units", []):
        if str(u).startswith(PROTECTED_PREFIX):
            skipped.append({"name": u, "category": "systemd_unit", "reason": "self-protected"})
            continue
        ops.append({"op": "disable_unit", "target": u,
                    "label": f"disable + stop {u}", "destructive": True})

    seen_cfg = set()
    for nd in app.get("nginx_detail", []):
        cf = nd.get("config_file")
        if nd.get("server_name") in shared or not cf or cf in seen_cfg:
            continue
        seen_cfg.add(cf)
        ops.append({"op": "remove_nginx", "target": cf,
                    "server_name": nd.get("server_name"),
                    "label": f"remove nginx {nd.get('server_name')} ({cf})", "destructive": True})
    if seen_cfg:
        ops.append({"op": "reload_nginx", "target": "nginx",
                    "label": "nginx -s reload", "destructive": True})

    for v in vols:  # after backup
        vn = _vol_name(v)
        ops.append({"op": "remove_volume", "target": vn,
                    "label": f"remove volume {vn}", "destructive": True, "data_loss": True})

    if safe_dir:  # after backup
        ops.append({"op": "remove_dir", "target": safe_dir,
                    "label": f"rm -rf {safe_dir}", "destructive": True, "data_loss": True})

    return {
        "project": name,
        "refusals": refusals,
        "skipped": skipped,
        "ops": ops,
        "data_loss": any(o.get("data_loss") for o in ops),
        "blast_radius": br,
        "projects_root": projects_root,
    }


# ----------------------------- execution -----------------------------------


def _run(cmd: List[str], timeout: int = 120) -> Dict[str, Any]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return {"status": "success" if p.returncode == 0 else "failed",
                "rc": p.returncode, "stdout": p.stdout[-2000:], "stderr": p.stderr[-2000:]}
    except Exception as e:
        return {"status": "failed", "rc": None, "stdout": "", "stderr": str(e)}


def _run_op(op: Dict[str, Any], backup_dir: str, projects_root: str, name: str) -> Dict[str, Any]:
    t = op["op"]
    tgt = op["target"]
    if t == "backup_dir":
        parent = str(Path(tgt).parent)
        base = Path(tgt).name
        return _run(["tar", "-czf", f"{backup_dir}/files.tar.gz", "-C", parent, base], timeout=600)
    if t == "backup_volume":
        mp = op.get("mountpoint") or f"/var/lib/docker/volumes/{tgt}/_data"
        return _run(["sudo", "-n", "tar", "-czf", f"{backup_dir}/vol-{tgt}.tar.gz", "-C", mp, "."], timeout=600)
    if t == "remove_container":
        return _run(["docker", "rm", "-f", tgt])
    if t == "compose_down":
        return _run(["docker", "compose", "-f", tgt, "down"])
    if t == "disable_unit":
        return _run(["sudo", "-n", "systemctl", "disable", "--now", tgt], timeout=60)
    if t == "remove_nginx":
        return _run(["sudo", "-n", "rm", "-f", tgt])
    if t == "reload_nginx":
        return _run(["sudo", "-n", "nginx", "-s", "reload"])
    if t == "remove_volume":
        return _run(["docker", "volume", "rm", tgt])
    if t == "remove_dir":
        # Defense in depth: re-run the structural catastrophe guard right before rm -rf.
        reason = _structural_refusal(tgt)
        if reason:
            return {"status": "failed", "rc": None, "stdout": "", "stderr": f"refusing rm -rf {tgt}: {reason}"}
        return _run(["rm", "-rf", str(Path(tgt).resolve())], timeout=120)
    return {"status": "failed", "rc": None, "stdout": "", "stderr": f"unknown op {t}"}


def execute_plan(
    plan: Dict[str, Any], db, actor: str, *, projects_root: str, now: Optional[datetime] = None
) -> Dict[str, Any]:
    name = plan["project"]
    if plan["refusals"]:
        raise TeardownRefused("; ".join(plan["refusals"]))

    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M%S")
    backup_dir = f"{projects_root}/backups/{name}/{ts}"
    os.makedirs(backup_dir, exist_ok=True)

    results: List[Dict[str, Any]] = []
    aborted = False
    for op in plan["ops"]:
        if aborted:
            results.append({**op, "status": "skipped", "detail": "aborted after a backup failure"})
            continue
        r = _run_op(op, backup_dir, projects_root, name)
        row = {**op, **r}
        results.append(row)
        _audit(db, actor, name, op, r)
        if op.get("critical") and r["status"] != "success":
            aborted = True
            row["detail"] = "BACKUP FAILED — aborting teardown, no deletion performed"
    summary = {
        "removed_stopped": [{"op": r["op"], "target": r["target"]}
                            for r in results if r.get("destructive") and r.get("status") == "success"],
        "backed_up": [r["target"] for r in results
                      if str(r["op"]).startswith("backup") and r.get("status") == "success"],
        "skipped_shared": plan.get("skipped", []),
        "failed": [{"op": r["op"], "target": r["target"],
                    "reason": (r.get("detail") or r.get("stderr") or "").strip()[:300]}
                   for r in results if r.get("status") == "failed"],
    }
    summary["ok"] = (not aborted) and not summary["failed"]
    return {"project": name, "backup_dir": backup_dir, "aborted": aborted,
            "results": results, "summary": summary}


def _audit(db, actor: str, name: str, op: Dict[str, Any], r: Dict[str, Any]) -> None:
    try:
        db.record_action({
            "actor": actor,
            "asset_id": None,
            "asset_name": str(op.get("target")),
            "category": "teardown",
            "project": name,
            "action": f"teardown:{op['op']}",
            "args": {"project": name},
            "status": r.get("status"),
            "return_code": r.get("rc"),
            "stdout": (r.get("stdout") or "")[-2000:],
            "stderr": (r.get("stderr") or "")[-2000:],
            "duration_ms": 0,
            "refused_reason": None,
        })
    except Exception:
        pass
