"""Configuration loader — single-host (OCI) build."""

import os
from pathlib import Path
from typing import List, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel


class ServerConfig(BaseModel):
    id: str
    name: str
    api_port: int


class FeaturesConfig(BaseModel):
    scanner_enabled: bool = True
    api_enabled: bool = True
    frontend_enabled: bool = True


class MongoDBConfig(BaseModel):
    uri_env: str = "INFRADOCS_MONGO_URI"
    database: str

    @property
    def uri(self) -> str:
        value = os.environ.get(self.uri_env)
        if not value:
            raise RuntimeError(
                f"MongoDB URI not set: env var {self.uri_env} is missing. "
                f"Populate .env from .env.example."
            )
        return value


# Discovery is a DENY-LIST WALK FROM `/`, not an allow-list of blessed dirs — so an
# app installed under ANY top-level directory (incl. /data/<app>) is found without
# being named. `scan_roots` defaults to the filesystem root; the exclusion set below
# does the filtering. `direct_roots` still names a few install roots whose direct
# children are apps even without a marker. Used when config.yml omits these keys.
DEFAULT_SCAN_ROOTS: List[str] = ["/"]
DEFAULT_DIRECT_ROOTS: List[str] = ["/opt", "/srv", "/var/www"]
# Top-level / system trees a walk from `/` must NOT churn through: pseudo-fs, boot,
# volatile caches/logs, image + package stores, and the OS binary/library trees.
# Deny-list (prune these), NOT an allow-list — /data, /opt, /srv, /home, /etc,
# /var/www, /usr/local, /mnt, /media all stay discoverable. Non-local mounts
# (nfs/tmpfs/overlay/…) are pruned separately by fstype at scan time.
DEFAULT_SCAN_EXCLUSIONS: List[str] = [
    "/proc", "/sys", "/dev", "/run",              # pseudo / virtual
    "/boot", "/lost+found",                        # boot + fs artifacts
    "/tmp", "/var/tmp", "/var/cache", "/var/log", "/var/spool",  # volatile
    "/var/lib/docker", "/var/lib/containerd", "/var/lib/snapd", "/snap",  # image/pkg stores
    "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",       # OS binaries/libs
    "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/lib32", "/usr/lib64",
    "/usr/libx32", "/usr/libexec", "/usr/share", "/usr/include",
    "/usr/src", "/usr/games",                      # (…/usr/local stays discoverable)
]


class PathsConfig(BaseModel):
    projects_root: str
    # Install roots whose DIRECT children are each an application even without a
    # marker (/opt/<app>, /srv/<app>, /var/www/<site>). Complements the walk from `/`.
    direct_roots: Optional[List[str]] = None
    # Roots to recursively HUNT for apps by marker (docker-compose.yml / .git).
    # Defaults to ["/"] — a deny-list walk of the whole disk (see scan_exclusions).
    scan_roots: Optional[List[str]] = None
    # Absolute host paths pruned during the walk (deny-list). Defaults to
    # DEFAULT_SCAN_EXCLUSIONS. Built-in pseudo-fs guards apply regardless.
    scan_exclusions: Optional[List[str]] = None
    # How deep to descend under each root. From `/`, an app is one level deeper than
    # from a named root (/data/<app> is depth 2), so the default is a touch higher.
    scan_depth: int = 4
    # Wall-clock cap (seconds) for the whole filesystem discovery pass so a walk
    # from `/` can never hang the pipeline. <=0 disables the cap.
    scan_timeout_seconds: int = 90
    data_root: str
    logs_dir: str = "logs"
    artifacts_dir: str = "artifacts"


class ScanningConfig(BaseModel):
    schedule: str = "0 */6 * * *"
    timeout: int = 300
    enabled_scanners: List[str] = []


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "json"
    retention_days: int = 30


class AuthConfig(BaseModel):
    username: str
    password_env: str = "INFRADOCS_API_PASSWORD"
    dev_password: str = "changeme"


class FederationConfig(BaseModel):
    # Gossip cluster + failover. OFF by default so installing this code on a host
    # changes nothing until a real multi-node fleet is deliberately wired up.
    cluster_enabled: bool = False
    # Every node health-checks every peer this often. A peer unheard-from for
    # ~MISS_ROUNDS intervals (unreachable_after) is treated as down; the primary is
    # considered lost after the same window, which then (in a majority) triggers an
    # election. Loose enough that a single missed round never causes a failover.
    health_interval_seconds: int = 10
    unreachable_after_seconds: int = 30  # 3 missed rounds
    # AUTOMATIC FAILOVER — when a primary is lost, may a surviving node in the majority
    # partition auto-promote itself? OFF for the moment: elections still run (roster stays
    # fresh, followers follow, a partitioned primary still steps down = split-brain guard
    # intact), but NO node auto-takes-primary. Taking over is a MANUAL operator action
    # (Admin · Cluster → Promote / Make primary). Hot-toggleable at runtime.
    auto_failover: bool = True


class Config(BaseModel):
    server: ServerConfig
    features: FeaturesConfig
    mongodb: MongoDBConfig
    paths: PathsConfig
    scanning: ScanningConfig
    logging: LoggingConfig
    auth: AuthConfig
    federation: FederationConfig = FederationConfig()


def load_config(config_path: str = "config.yml") -> Config:
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    # Load .env sitting next to config.yml (project root) if present.
    env_file = config_file.parent / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)

    with open(config_file, "r") as f:
        raw = yaml.safe_load(f)

    # Env overrides so the SAME image/config.yml deploys on any host via .env.
    _env_override(raw, "server", "id", "INFRADOCS_SERVER_ID")
    _env_override(raw, "server", "name", "INFRADOCS_SERVER_NAME")
    _env_override(raw, "paths", "projects_root", "INFRADOCS_PROJECTS_ROOT")
    _env_override(raw, "mongodb", "database", "INFRADOCS_DB")
    _env_override(raw, "auth", "username", "INFRADOCS_API_USERNAME")

    # Comma-separated root overrides (env wins). Marker-hunt roots and direct
    # (one-folder-per-app) roots are configured independently.
    paths = raw.get("paths")
    if isinstance(paths, dict):
        # NOTE: an EMPTY/whitespace value means "not set" here — the compose file passes
        # INFRADOCS_SCAN_ROOTS="${SCAN_ROOTS:-}" (empty when unset), and treating "" as
        # an override would collapse scan_roots to [] and silently DISABLE the whole
        # from-/ walk. So only override when the env var has real content.
        roots_env = os.environ.get("INFRADOCS_SCAN_ROOTS")
        if roots_env and roots_env.strip():
            paths["scan_roots"] = [r.strip() for r in roots_env.split(",") if r.strip()]
        direct_env = os.environ.get("INFRADOCS_DIRECT_ROOTS")
        if direct_env and direct_env.strip():
            paths["direct_roots"] = [r.strip() for r in direct_env.split(",") if r.strip()]
        excl_env = os.environ.get("INFRADOCS_SCAN_EXCLUSIONS")
        if excl_env and excl_env.strip():
            paths["scan_exclusions"] = [r.strip() for r in excl_env.split(",") if r.strip()]
        # Deny-list-from-`/` defaults when neither config.yml nor env supplied a
        # value, so discovery can never silently collapse back to an allow-list.
        if paths.get("scan_roots") is None:
            paths["scan_roots"] = list(DEFAULT_SCAN_ROOTS)
        if paths.get("direct_roots") is None:
            paths["direct_roots"] = list(DEFAULT_DIRECT_ROOTS)
        if paths.get("scan_exclusions") is None:
            paths["scan_exclusions"] = list(DEFAULT_SCAN_EXCLUSIONS)

    return Config(**raw)


def _env_override(raw: dict, section: str, key: str, env: str) -> None:
    val = os.environ.get(env)
    if val and isinstance(raw.get(section), dict):
        raw[section][key] = val


_config: Optional[Config] = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config() -> Config:
    global _config
    _config = load_config()
    return _config
