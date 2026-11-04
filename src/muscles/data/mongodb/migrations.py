"""Experimental MongoDB migration journal and persistent exclusive lock."""
from contextlib import contextmanager
from datetime import datetime, timezone
import secrets
from typing import Any

from muscles.data.migration_identity import migration_identity
from muscles.data.versioned_migrations import MigrationStateError, VersionedMigrationBackend


class MongoJournal:
    def __init__(self, collection: Any, source: str) -> None:
        self.collection, self.source = collection, source

    def get(self, version: str) -> tuple[str, str] | None:
        entry = self.collection.find_one({"_id": migration_identity(self.source, version)})
        return (entry["checksum"], entry["state"]) if entry else None

    def versions(self, known: set[str]) -> set[str]:
        del known
        result = set()
        for entry in self.collection.find({"source": self.source}, {"version": 1}):
            version = entry.get("version")
            if not isinstance(version, str) or not version or entry.get("_id") != migration_identity(self.source, version):
                raise MigrationStateError("A MongoDB migration journal entry is invalid")
            result.add(version)
        return result

    def begin(self, version: str, checksum: str) -> None:
        self.collection.insert_one({"_id": migration_identity(self.source, version), "source": self.source,
                                    "version": version, "checksum": checksum, "state": "running"})

    def _transition(self, version: str, checksum: str, before: str, after: str) -> None:
        changes = {"state": after}
        if after == "complete":
            changes["applied_at"] = datetime.now(timezone.utc)
        result = self.collection.update_one(
            {"_id": migration_identity(self.source, version), "checksum": checksum, "state": before},
            {"$set": changes},
        )
        if result.matched_count != 1:
            raise MigrationStateError(f"Migration {version} state changed")

    def complete(self, version: str, checksum: str) -> None:
        self._transition(version, checksum, "running", "complete")

    def begin_revert(self, version: str, checksum: str) -> None:
        self._transition(version, checksum, "complete", "reverting")

    def remove(self, version: str, checksum: str) -> None:
        result = self.collection.delete_one({"_id": migration_identity(self.source, version),
                                             "checksum": checksum, "state": "reverting"})
        if result.deleted_count != 1:
            raise MigrationStateError(f"Migration {version} state changed")


class MongoMigrationBackend(VersionedMigrationBackend):
    def __init__(self, collection, source, migrations):
        super().__init__(MongoJournal(collection, source), migrations)
        self.collection = collection
        self.lock_id = f"lock:{migration_identity(source, 'lock')}"

    @contextmanager
    def _lock(self):
        token = secrets.token_hex(32)
        self.collection.insert_one({"_id": self.lock_id, "token": token})
        try:
            yield
        finally:
            result = self.collection.delete_one({"_id": self.lock_id, "token": token})
            if result.deleted_count != 1:
                raise MigrationStateError("Migration lock ownership was lost")

    def upgrade(self, target="head"):
        with self._lock():
            super().upgrade(target)

    def downgrade(self, target):
        with self._lock():
            super().downgrade(target)
