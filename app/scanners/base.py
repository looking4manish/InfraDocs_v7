"""Base scanner interface.

V6 fixes V5's constructor inconsistency: every scanner takes the same
(server_id, project_detector) pair — no scanner-specific kwargs in the
base init. Mongo writes are owned by the orchestrator, not scanners.
"""

import logging
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from app.core.project_detector import ProjectDetector

logger = logging.getLogger(__name__)


class BaseScanner(ABC):
    def __init__(self, server_id: str, project_detector: ProjectDetector):
        self.server_id = server_id
        self.project_detector = project_detector
        self.scan_timestamp: Optional[datetime] = None
        self.errors: List[Dict[str, str]] = []

    @property
    @abstractmethod
    def scanner_name(self) -> str:
        ...

    @abstractmethod
    def scan(self) -> List[Dict[str, Any]]:
        """Discover assets and return them. Must not raise."""

    def execute(self) -> Dict[str, Any]:
        self.scan_timestamp = datetime.now(timezone.utc)
        self.errors = []
        start = datetime.now(timezone.utc)
        logger.info(f"Running {self.scanner_name} scanner")

        try:
            assets = self.scan()
            status = "success" if not self.errors else "partial_success"
        except Exception as e:
            logger.error(f"{self.scanner_name} crashed: {e}", exc_info=True)
            self.errors.append({"message": f"unhandled: {e}"})
            assets = []
            status = "failed"

        duration = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info(
            f"{self.scanner_name} done: {len(assets)} assets in {duration:.2f}s"
        )

        return {
            "scanner": self.scanner_name,
            "server_id": self.server_id,
            "timestamp": self.scan_timestamp.isoformat(),
            "duration_seconds": duration,
            "assets_found": len(assets),
            "assets": assets,
            "errors": self.errors,
            "status": status,
        }

    def create_asset(
        self,
        *,
        category: str,
        asset_id: str,
        name: str,
        status: str,
        metadata: Dict[str, Any],
        project: str = "System",
        health_indicators: Optional[Dict[str, Any]] = None,
        relationships: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        asset = {
            "server_id": self.server_id,
            "category": category,
            "asset_id": asset_id,
            "name": name,
            "status": status,
            "project": project or "System",
            "metadata": metadata,
            "scanner": self.scanner_name,
            "discovered_at": (self.scan_timestamp or datetime.now(timezone.utc)).isoformat(),
        }
        if health_indicators is not None:
            asset["health_indicators"] = health_indicators
        if relationships:
            asset["relationships"] = relationships
        return asset

    def run_cmd(
        self,
        cmd: Sequence[str],
        *,
        timeout: int = 10,
        text: bool = True,
        label: Optional[str] = None,
    ) -> Optional[subprocess.CompletedProcess]:
        """Run a subprocess safely and uniformly. Returns the CompletedProcess, or
        None on ANY failure (missing binary, timeout, OSError). `check=False` — the
        caller inspects `returncode`. When `label` is given, a failure is recorded
        via add_error (so it surfaces as partial_success); without a label the
        failure is silent (e.g. an optional tool that simply isn't installed)."""
        try:
            return subprocess.run(
                list(cmd), capture_output=True, text=text, timeout=timeout, check=False
            )
        except Exception as e:  # noqa: BLE001 — one place that must never raise
            if label:
                self.add_error(f"{label}: {e}")
            return None

    def add_error(self, message: str):
        self.errors.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": message,
            }
        )
        logger.warning(f"{self.scanner_name}: {message}")
