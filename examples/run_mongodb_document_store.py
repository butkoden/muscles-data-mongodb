from __future__ import annotations

"""MongoDB document-store port smoke example without a real MongoDB server.

Run:
  PYTHONPATH=../muscles-data/src:src python3 examples/run_mongodb_document_store.py
"""

from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

from muscles_data.catalog import DataAdapterCatalog
from muscles_data.config import DataConfig
from muscles_data.ports import DocumentStorePort
from muscles_data.runtime import DataRuntime
from muscles_data_mongodb import MongoDocumentStoreFactory


class FakeMongoAdmin:
    def command(self, _name: str):
        return {"ok": 1}


class FakeMongoCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs
        self.limit_value = len(docs)

    def limit(self, value: int):
        self.limit_value = value
        return self

    def __iter__(self):
        return iter(self.docs[: self.limit_value])


class FakeMongoCollection:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def find_one(self, filter):
        doc = self.docs.get(str(filter.get("_id")))
        return dict(doc) if doc is not None else None

    def replace_one(self, filter, replacement, upsert: bool = False):
        del upsert
        document_id = str(filter["_id"])
        matched = document_id in self.docs
        self.docs[document_id] = dict(replacement)
        return SimpleNamespace(matched_count=1 if matched else 0)

    def find(self, filter):
        docs = [
            dict(doc)
            for doc in self.docs.values()
            if all(doc.get(key) == value for key, value in dict(filter).items())
        ]
        return FakeMongoCursor(docs)

    def delete_one(self, filter):
        deleted = 1 if self.docs.pop(str(filter.get("_id")), None) is not None else 0
        return SimpleNamespace(deleted_count=deleted)


class FakeMongoDatabase:
    def __init__(self) -> None:
        self.collections: dict[str, FakeMongoCollection] = {}

    def __getitem__(self, name: str) -> FakeMongoCollection:
        return self.collections.setdefault(name, FakeMongoCollection())


class FakeMongoClient:
    def __init__(self) -> None:
        self.admin = FakeMongoAdmin()
        self.databases: dict[str, FakeMongoDatabase] = {}

    def __getitem__(self, name: str) -> FakeMongoDatabase:
        return self.databases.setdefault(name, FakeMongoDatabase())

    def close(self) -> None:
        return None


def main() -> None:
    client = FakeMongoClient()
    catalog = DataAdapterCatalog.with_defaults()
    catalog.register(MongoDocumentStoreFactory(client_factory=lambda _config: client))
    runtime = DataRuntime(
        config=DataConfig.from_raw(
            {
                "data": {
                    "resources": {
                        "mongo.content": {
                            "type": "mongodb",
                            "url": "mongodb://user:mongo-secret@localhost:27017",
                            "database": "content",
                            "max_limit": 5,
                        }
                    }
                }
            }
        ),
        catalog=catalog,
    )

    store = runtime.require_port("mongo.content", DocumentStorePort)
    print("upsert ->", asdict(store.upsert_document("profiles", "denis", {"name": "Denis", "role": "developer"})))
    print("get ->", store.get_document("profiles", "denis"))
    print("find ->", store.find_documents("profiles", filters={"role": "developer"}))
    print("delete ->", asdict(store.delete_document("profiles", "denis")))
    print("doctor ->", runtime.doctor())


if __name__ == "__main__":
    main()
