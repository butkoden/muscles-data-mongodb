from __future__ import annotations

import os
from uuid import uuid4

import pytest
from muscles_data.catalog import DataAdapterCatalog
from muscles_data.config import DataConfig
from muscles_data.models import DataCapability
from muscles_data.ports import DocumentStorePort
from muscles_data.runtime import DataRuntime

from muscles_data_mongodb import MongoDocumentStoreFactory


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
        contracts = pytest.importorskip("muscles_data.contracts")
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
