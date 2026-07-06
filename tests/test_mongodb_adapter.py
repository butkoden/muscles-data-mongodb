from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from muscles_data.catalog import DataAdapterCatalog
from muscles_data.config import DataConfig
from muscles_data.models import DataCapability
from muscles_data.ports import DocumentStorePort
from muscles_data.runtime import DataRuntime

from muscles_data_mongodb import (
    MongoDBClientMissingError,
    MongoDBConfigError,
    MongoDBConnectionError,
    MongoDocumentStoreFactory,
)


class FakeMongoAdmin:
    def __init__(self, client: "FakeMongoClient") -> None:
        self.client = client

    def command(self, name: str):
        self.client.admin_commands.append(name)
        if self.client.fail_ping:
            raise TimeoutError("mongodb password=mongo-secret timed out")
        return {"ok": 1}


class FakeMongoCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs
        self.limits: list[int] = []

    def limit(self, value: int):
        self.limits.append(value)
        return self

    def __iter__(self):
        limit = self.limits[-1] if self.limits else len(self.docs)
        return iter(self.docs[:limit])


class FakeMongoCollection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.docs: dict[str, dict[str, Any]] = {}
        self.find_one_calls: list[dict[str, Any]] = []
        self.replace_one_calls: list[dict[str, Any]] = []
        self.find_calls: list[dict[str, Any]] = []
        self.delete_one_calls: list[dict[str, Any]] = []
        self.last_cursor: FakeMongoCursor | None = None

    def find_one(self, filter):
        self.find_one_calls.append(dict(filter))
        document_id = str(filter.get("_id"))
        doc = self.docs.get(document_id)
        return dict(doc) if doc is not None else None

    def replace_one(self, filter, replacement, upsert: bool = False):
        self.replace_one_calls.append({"filter": dict(filter), "replacement": dict(replacement), "upsert": upsert})
        document_id = str(filter["_id"])
        matched = 1 if document_id in self.docs else 0
        self.docs[document_id] = dict(replacement)
        return SimpleNamespace(matched_count=matched, modified_count=1 if matched else 0, upserted_id=None if matched else document_id)

    def find(self, filter):
        self.find_calls.append(dict(filter))
        docs = [
            dict(doc)
            for doc in self.docs.values()
            if all(doc.get(key) == value for key, value in dict(filter).items())
        ]
        self.last_cursor = FakeMongoCursor(docs)
        return self.last_cursor

    def delete_one(self, filter):
        self.delete_one_calls.append(dict(filter))
        document_id = str(filter.get("_id"))
        deleted = 1 if self.docs.pop(document_id, None) is not None else 0
        return SimpleNamespace(deleted_count=deleted)


class FakeMongoDatabase:
    def __init__(self, name: str) -> None:
        self.name = name
        self.collections: dict[str, FakeMongoCollection] = {}

    def __getitem__(self, name: str) -> FakeMongoCollection:
        return self.collections.setdefault(name, FakeMongoCollection(name))


class FakeMongoClient:
    def __init__(self, *, fail_ping: bool = False) -> None:
        self.fail_ping = fail_ping
        self.admin = FakeMongoAdmin(self)
        self.databases: dict[str, FakeMongoDatabase] = {}
        self.admin_commands: list[str] = []
        self.closed = False

    def __getitem__(self, name: str) -> FakeMongoDatabase:
        return self.databases.setdefault(name, FakeMongoDatabase(name))

    def close(self) -> None:
        self.closed = True


def _mongodb_config(url: str = "mongodb://user:mongo-secret@localhost:27017") -> dict[str, Any]:
    return {
        "data": {
            "resources": {
                "mongo.content": {
                    "type": "mongodb",
                    "url": url,
                    "database": "content",
                    "timeout": 1.5,
                    "max_limit": 2,
                    "native_client": True,
                }
            }
        }
    }


def _runtime(client: FakeMongoClient | None):
    catalog = DataAdapterCatalog.with_defaults()
    catalog.register(MongoDocumentStoreFactory(client_factory=lambda _config: client))
    return DataRuntime(
        config=DataConfig.from_raw(_mongodb_config()),
        catalog=catalog,
    )


def test_mongodb_document_store_is_registered_lazy_and_maps_operations():
    client = FakeMongoClient()
    runtime = _runtime(client)

    listed = runtime.list_resources()
    mongo = next(item for item in listed if item["name"] == "mongo.content")
    inspected_before = runtime.inspect_resource("mongo.content")

    assert mongo["type"] == "mongodb"
    assert "document_store" in mongo["capabilities"]
    assert "native_client" not in mongo["capabilities"]
    assert mongo["initialized"] is False
    assert inspected_before["initialized"] is False
    assert inspected_before["options"]["url"] == "***"

    store = runtime.require_port("mongo.content", DocumentStorePort)
    write = store.upsert_document("profiles", "denis", {"name": "Denis", "role": "developer"})
    found = store.get_document("profiles", "denis")
    store.upsert_document("profiles", "reader", {"name": "Reader", "role": "developer"})
    store.upsert_document("profiles", "other", {"name": "Other", "role": "developer"})
    docs = store.find_documents("profiles", filters={"role": "developer"}, limit=10)
    deleted = store.delete_document("profiles", "reader")

    collection = client["content"]["profiles"]
    assert write.written == 1
    assert collection.replace_one_calls[0] == {
        "filter": {"_id": "denis"},
        "replacement": {"name": "Denis", "role": "developer", "_id": "denis"},
        "upsert": True,
    }
    assert found == {"name": "Denis", "role": "developer"}
    assert collection.find_one_calls[0] == {"_id": "denis"}
    assert [doc["name"] for doc in docs] == ["Denis", "Reader"]
    assert collection.last_cursor is not None
    assert collection.last_cursor.limits == [2]
    assert deleted.deleted == 1
    assert collection.delete_one_calls[-1] == {"_id": "reader"}

    native = runtime.require_resource("mongo.content", DataCapability.NATIVE_CLIENT).native_client()
    assert native is client
    assert client.admin_commands == []


def test_mongodb_inspect_doctor_close_and_safe_failures():
    client = FakeMongoClient()
    runtime = _runtime(client)

    doctor = runtime.doctor()
    mongo_checks = [check for check in doctor["checks"] if check["resource"] == "mongo.content"]

    assert mongo_checks[0]["status"] == "ok"
    assert client.admin_commands == ["ping"]
    assert "mongo-secret" not in repr(doctor)
    assert runtime.close()["status"] == "ok"
    assert client.closed is True

    missing_client_runtime = _runtime(None)
    with pytest.raises(MongoDBClientMissingError):
        missing_client_runtime.require_port("mongo.content", DocumentStorePort).get_document("profiles", "denis")

    failing_runtime = DataRuntime(
        config=DataConfig.from_raw(_mongodb_config("mongodb://user:mongo-secret@localhost:27017")),
        catalog=_catalog(MongoDocumentStoreFactory(client_factory=lambda _config: FakeMongoClient(fail_ping=True))),
    )
    failing_doctor = failing_runtime.doctor()
    assert failing_doctor["status"] == "failed"
    assert [check for check in failing_doctor["checks"] if check["resource"] == "mongo.content"][0]["status"] == "failed"
    assert "mongo-secret" not in repr(failing_doctor)

    bad_client = FakeMongoClient()
    bad_client["content"]["profiles"].find_one = lambda _filter: (_ for _ in ()).throw(RuntimeError("mongodb://user:mongo-secret@localhost unavailable"))
    bad_runtime = DataRuntime(
        config=DataConfig.from_raw(_mongodb_config()),
        catalog=_catalog(MongoDocumentStoreFactory(client_factory=lambda _config: bad_client)),
    )
    with pytest.raises(MongoDBConnectionError):
        bad_runtime.require_port("mongo.content", DocumentStorePort).get_document("profiles", "denis")

    unsupported_runtime = DataRuntime(
        config=DataConfig.from_raw(
            {
                "data": {
                    "resources": {
                        "mongo.content": {
                            "type": "mongodb",
                            "url": "mongodb://localhost",
                            "database": "content",
                            "unsafe": True,
                        }
                    }
                }
            }
        ),
        catalog=_catalog(MongoDocumentStoreFactory(client_factory=lambda _config: FakeMongoClient())),
    )
    with pytest.raises(MongoDBConfigError, match="Unsupported MongoDB resource options"):
        unsupported_runtime.require_port("mongo.content", DocumentStorePort).get_document("profiles", "denis")


def test_mongodb_resource_requires_url_and_database():
    assert DataConfig.from_raw(_mongodb_config()).resources["mongo.content"].type == "mongodb"

    with pytest.raises(MongoDBConfigError, match="requires database"):
        MongoDocumentStoreFactory(client_factory=lambda _config: FakeMongoClient()).create(
            DataConfig.from_raw(
                {
                    "data": {
                        "resources": {
                            "mongo.content": {"type": "mongodb", "url": "mongodb://localhost"}
                        }
                    }
                }
            ).resources["mongo.content"]
        ).get_document("profiles", "denis")


def _catalog(factory: MongoDocumentStoreFactory) -> DataAdapterCatalog:
    catalog = DataAdapterCatalog.with_defaults()
    catalog.register(factory)
    return catalog
