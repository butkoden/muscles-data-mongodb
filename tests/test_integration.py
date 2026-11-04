from __future__ import annotations

import os
from uuid import uuid4

import pytest
from muscles.data.catalog import DataAdapterCatalog
from muscles.data.config import DataConfig
from muscles.data.models import DataCapability
from muscles.data.ports import DocumentStorePort
from muscles.data.runtime import DataRuntime

from muscles.data.policies import DataOperationContext, DataPolicyDecision, DataPolicyPipeline, DataPredicate, DataPredicateOperator
from muscles.data.mongodb import MongoDocumentStoreAdapter, MongoDocumentStoreFactory, MongoPolicyDocumentStore


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("MUSCLES_DATA_INTEGRATION"), reason="backend integration is disabled"),
]


def test_mongodb_real_document_lifecycle():
    database = f"muscles_data_it_{uuid4().hex[:12]}"
    config = DataConfig.from_raw(
        {
            "data": {
                "resources": {
                    "mongo.content": {
                        "type": "mongodb",
                        "url_env": "MONGODB_URL",
                        "database": database,
                        "timeout": 3,
                        "native_client": True,
                    }
                }
            }
        }
    )
    catalog = DataAdapterCatalog.with_defaults()
    catalog.register(MongoDocumentStoreFactory())
    runtime = DataRuntime(config=config, catalog=catalog)

    client = None
    try:
        store = runtime.require_port("mongo.content", DocumentStorePort)
        contracts = pytest.importorskip("muscles.data.contracts")
        contract = getattr(contracts, "assert_document_store_contract", None)
        if contract is not None:
            contract(lambda: store)
        assert runtime.doctor()["status"] == "ok"
    finally:
        try:
            if client is None:
                try:
                    client = runtime.require_resource("mongo.content", DataCapability.NATIVE_CLIENT).native_client()
                except Exception:
                    client = None
            if client is not None:
                client.drop_database(database)
        finally:
            runtime.close()


def test_policy_store_enforces_workspace_in_database():
    pymongo = pytest.importorskip("pymongo")
    database = f"muscles_data_policy_it_{uuid4().hex[:12]}"
    config = DataConfig.from_raw({"resources": {"mongo": {
        "type": "mongodb", "url_env": "MONGODB_URL", "database": database,
    }}}).resources["mongo"]

    class WorkspacePolicy:
        def evaluate(self, request):
            return DataPolicyDecision(predicates=(DataPredicate(
                "workspace", DataPredicateOperator.EQ, request.context.attributes["workspace"],
            ),))

    adapter = MongoDocumentStoreAdapter(config)
    client = None
    try:
        client = adapter.native_client()
        collection = client[database]["documents"]
        collection.insert_many([
            {"_id": "other", "workspace": "b"},
            {"_id": "array", "workspace": ["a", "b"]},
        ])
        store = MongoPolicyDocumentStore(
            adapter, policies=DataPolicyPipeline((WorkspacePolicy(),)),
            context=DataOperationContext(attributes={"workspace": "a"}),
        )
        assert store.find_documents("documents") == []
        assert store.delete_document("documents", "other").deleted == 0
        assert store.delete_document("documents", "array").deleted == 0
        store.upsert_document("documents", "mine", {"workspace": "a"})
        assert store.get_document("documents", "mine") == {"workspace": "a"}
        with pytest.raises(pymongo.errors.DuplicateKeyError):
            store.upsert_document("documents", "other", {"workspace": "a"})
        assert collection.find_one({"_id": "other"})["workspace"] == "b"
    finally:
        if client is not None:
            client.drop_database(database)
        adapter.close()


@pytest.mark.parametrize(("operator", "value", "expected"), [
    (DataPredicateOperator.LIKE, "a_%", {"string"}),
    (DataPredicateOperator.IS_NULL, True, {"null"}),
    (DataPredicateOperator.IS_NULL, False, {"string", "upper", "short"}),
])
def test_policy_like_and_null_on_real_mongodb(operator, value, expected):
    database = f"muscles_data_policy_it_{uuid4().hex[:12]}"
    config = DataConfig.from_raw({"resources": {"mongo": {
        "type": "mongodb", "url_env": "MONGODB_URL", "database": database,
    }}}).resources["mongo"]

    class Policy:
        def evaluate(self, request):
            return DataPolicyDecision(predicates=(DataPredicate("field", operator, value),))

    adapter = MongoDocumentStoreAdapter(config)
    client = None
    try:
        client = adapter.native_client()
        client[database]["documents"].insert_many([
            {"_id": "string", "field": "abc", "name": "string"},
            {"_id": "upper", "field": "ABC", "name": "upper"},
            {"_id": "short", "field": "a", "name": "short"},
            {"_id": "null", "field": None, "name": "null"},
            {"_id": "missing", "name": "missing"},
            {"_id": "array", "field": [None, "abc"], "name": "array"},
        ])
        store = MongoPolicyDocumentStore(adapter, policies=DataPolicyPipeline((Policy(),)))
        assert {doc["name"] for doc in store.find_documents("documents")} == expected
        if operator is DataPredicateOperator.LIKE:
            store.upsert_document("documents", "new", {"field": "abcd"})
            assert store.get_document("documents", "new")["field"] == "abcd"
        elif operator is DataPredicateOperator.IS_NULL:
            store.upsert_document("documents", "new", {"field": None if value else "safe"})
            assert store.get_document("documents", "new")["field"] == (None if value else "safe")
    finally:
        if client is not None:
            client.drop_database(database)
        adapter.close()
