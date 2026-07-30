"""MongoDB manager — single-database build for V6."""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import ConnectionFailure

logger = logging.getLogger(__name__)


class DBManager:
    """Single-database MongoDB manager for InfraDocs V6.

    V5 used a database-per-server pattern (infradocs_oci, infradocs_n150, ...).
    V6 is OCI-only, so we use one database and tag each document with
    server_id='oci' for forward-compatibility when distribution returns.
    """

    def __init__(self, uri: str, database: str):
        self.uri = uri
        self.database = database
        self.client: Optional[MongoClient] = None
        self._connect()

    def _connect(self):
        try:
            self.client = MongoClient(self.uri, serverSelectionTimeoutMS=5000)
            self.client.admin.command("ping")
            logger.info(f"Connected to MongoDB database '{self.database}'")
        except ConnectionFailure as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            raise

    @property
    def db(self):
        return self.client[self.database]

    # ---------- assets ----------

    def insert_assets(self, assets: List[Dict]) -> int:
        if not assets:
            return 0
        now = datetime.now(timezone.utc)
        for a in assets:
            a.setdefault("created_at", now)
            a["updated_at"] = now
        result = self.db.assets.insert_many(assets)
        logger.info(f"Inserted {len(result.inserted_ids)} assets")
        return len(result.inserted_ids)

    def upsert_asset(self, asset: Dict) -> bool:
        query = {
            "category": asset["category"],
            "asset_id": asset["asset_id"],
        }
        now = datetime.now(timezone.utc)
        asset.setdefault("created_at", now)
        asset["updated_at"] = now
        result = self.db.assets.update_one(query, {"$set": asset}, upsert=True)
        return result.acknowledged

    def get_assets(
        self,
        category: Optional[str] = None,
        project: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict]:
        query: Dict = {}
        if category:
            query["category"] = category
        if project:
            query["project"] = project
        if status:
            query["status"] = status

        assets = list(self.db.assets.find(query))
        for a in assets:
            a["_id"] = str(a["_id"])
        return assets

    def delete_all_assets(self) -> int:
        result = self.db.assets.delete_many({})
        logger.info(f"Deleted {result.deleted_count} assets")
        return result.deleted_count

    # ---------- applications ----------

    def upsert_application(self, app: Dict) -> bool:
        query = {"application_id": app["application_id"]}
        now = datetime.now(timezone.utc)
        app.setdefault("created_at", now)
        app["updated_at"] = now
        result = self.db.applications.update_one(query, {"$set": app}, upsert=True)
        return result.acknowledged

    def get_applications(self) -> List[Dict]:
        apps = list(self.db.applications.find({}))
        for a in apps:
            a["_id"] = str(a["_id"])
        return apps

    def get_application(self, name: str) -> Optional[Dict]:
        app = self.db.applications.find_one({"name": name})
        if app:
            app["_id"] = str(app["_id"])
        return app

    # Index specs mirrored onto the staging collection BEFORE an atomic swap, so the
    # collection is fully indexed the instant it goes live (mirrors create_indexes()).
    _REPLACE_INDEXES: Dict[str, List] = {
        "applications": [
            (("name", ASCENDING), {"unique": True}),
            (("application_id", ASCENDING), {"unique": True}),
            (("internet_exposed", ASCENDING), {}),
        ],
        "ports": [
            (("port_id", ASCENDING), {"unique": True}),
            (("port", ASCENDING), {}),
            (("owner_project", ASCENDING), {}),
            (("state", ASCENDING), {}),
        ],
        "storage": [
            (("storage_id", ASCENDING), {"unique": True}),
            (("kind", ASCENDING), {}),
            (("owner_project", ASCENDING), {}),
        ],
    }

    def _atomic_replace(self, name: str, docs: List[Dict]) -> int:
        """Crash-safe whole-collection replace for the UI read models.

        Builds a staging collection, then atomically renames it over the target
        (renameCollection with dropTarget is atomic at the collection level). A reader
        of `name` therefore sees either the entire old set or the entire new set — never
        the empty window that the old delete_many()+insert_many() pair exposed on any
        mid-scan crash or concurrent read. Standalone-Mongo safe: no multi-document
        transaction / replica set required.
        """
        staging = f"{name}__staging"
        self.db.drop_collection(staging)   # clear any leftover from a crashed prior run
        coll = self.db.create_collection(staging)
        now = datetime.now(timezone.utc)
        if docs:
            for d in docs:
                d.setdefault("created_at", now)
                d["updated_at"] = now
            coll.insert_many(docs)
        for keys, opts in self._REPLACE_INDEXES.get(name, []):
            coll.create_index([keys], **opts)
        # Atomic swap: target is dropped and staging takes its name in one operation.
        coll.rename(name, dropTarget=True)
        return len(docs)

    def replace_applications(self, new_apps: List[Dict]) -> int:
        """Atomically replace the applications collection — used by correlator."""
        return self._atomic_replace("applications", new_apps)

    # ---------- ports registry (Phase 7B) ----------

    def replace_ports(self, new_ports: List[Dict]) -> int:
        """Atomically replace the ports collection — used by ports_registry."""
        return self._atomic_replace("ports", new_ports)

    def get_ports(
        self,
        state: Optional[str] = None,
        project: Optional[str] = None,
        port_min: Optional[int] = None,
        port_max: Optional[int] = None,
    ) -> List[Dict]:
        query: Dict = {}
        if state:
            query["state"] = state
        if project:
            query["owner_project"] = project
        if port_min is not None or port_max is not None:
            rng: Dict = {}
            if port_min is not None:
                rng["$gte"] = port_min
            if port_max is not None:
                rng["$lte"] = port_max
            query["port"] = rng
        ports = list(self.db.ports.find(query).sort("port", ASCENDING))
        for p in ports:
            p["_id"] = str(p["_id"])
        return ports

    # ---------- storage registry (Phase 7C) ----------

    def replace_storage(self, new_rows: List[Dict]) -> int:
        """Atomically replace the storage collection — used by storage_registry."""
        return self._atomic_replace("storage", new_rows)

    def get_storage(
        self,
        kind: Optional[str] = None,
        project: Optional[str] = None,
    ) -> List[Dict]:
        query: Dict = {}
        if kind:
            query["kind"] = kind
        if project:
            query["owner_project"] = project
        rows = list(self.db.storage.find(query).sort([("kind", ASCENDING), ("name", ASCENDING)]))
        for r in rows:
            r["_id"] = str(r["_id"])
        return rows

    # ---------- actions log (Phase 8) ----------

    def record_action(self, record: Dict) -> str:
        record["timestamp"] = datetime.now(timezone.utc)
        result = self.db.actions_log.insert_one(record)
        return str(result.inserted_id)

    def get_actions(
        self,
        asset_id: Optional[str] = None,
        action: Optional[str] = None,
        actor: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]:
        query: Dict = {}
        if asset_id:
            query["asset_id"] = asset_id
        if action:
            query["action"] = action
        if actor:
            query["actor"] = actor
        rows = list(
            self.db.actions_log.find(query)
            .sort("timestamp", DESCENDING)
            .limit(limit)
        )
        for r in rows:
            r["_id"] = str(r["_id"])
        return rows

    # ---------- projects ----------

    def upsert_project(self, project: Dict) -> bool:
        query = {"project_name": project["project_name"]}
        now = datetime.now(timezone.utc)
        project.setdefault("created_at", now)
        project["updated_at"] = now
        result = self.db.projects.update_one(query, {"$set": project}, upsert=True)
        return result.acknowledged

    def get_projects(self) -> List[Dict]:
        projects = list(self.db.projects.find({}))
        for p in projects:
            p["_id"] = str(p["_id"])
        return projects

    # ---------- scan logs ----------

    def insert_scan_log(self, scan_log: Dict) -> str:
        scan_log["created_at"] = datetime.now(timezone.utc)
        result = self.db.scan_logs.insert_one(scan_log)
        return str(result.inserted_id)

    def get_scan_logs(self, limit: int = 50) -> List[Dict]:
        logs = list(
            self.db.scan_logs.find({})
            .sort("created_at", DESCENDING)
            .limit(limit)
        )
        for log in logs:
            log["_id"] = str(log["_id"])
        return logs

    # ---------- utilities ----------

    def create_indexes(self):
        self.db.assets.create_index([("category", ASCENDING)])
        self.db.assets.create_index("asset_id")
        self.db.assets.create_index("project")
        self.db.assets.create_index("status")
        self.db.assets.create_index([("updated_at", DESCENDING)])

        self.db.projects.create_index("project_name", unique=True)

        self.db.applications.create_index("name", unique=True)
        self.db.applications.create_index("application_id", unique=True)
        self.db.applications.create_index("internet_exposed")

        self.db.ports.create_index("port_id", unique=True)
        self.db.ports.create_index([("port", ASCENDING)])
        self.db.ports.create_index("owner_project")
        self.db.ports.create_index("state")

        self.db.storage.create_index("storage_id", unique=True)
        self.db.storage.create_index("kind")
        self.db.storage.create_index("owner_project")

        self.db.actions_log.create_index([("timestamp", DESCENDING)])
        self.db.actions_log.create_index("asset_id")
        self.db.actions_log.create_index("action")
        self.db.actions_log.create_index("actor")

        self.db.scan_logs.create_index([("created_at", DESCENDING)])

        logger.info(f"Created indexes for {self.database}")

    def get_stats(self) -> Dict:
        return {
            "database": self.database,
            "assets_count": self.db.assets.count_documents({}),
            "projects_count": self.db.projects.count_documents({}),
            "scan_logs_count": self.db.scan_logs.count_documents({}),
            "collections": self.db.list_collection_names(),
        }

    def close(self):
        if self.client:
            self.client.close()
            logger.info("MongoDB connection closed")
