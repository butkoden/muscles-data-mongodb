# muscles-data-mongodb

MongoDB adapter package for `muscles-data`.

This package is intentionally separate from `muscles-data`: the core package
owns typed ports and runtime, while this package owns the PyMongo-backed
`DocumentStorePort` adapter.

## Install

```bash
python -m pip install muscles-data-mongodb
```

For local framework development:

```bash
PYTHONPATH=../muscles-data/src:src python3 -m pytest -q
```

## Configuration

```yaml
data:
  resources:
    mongo.content:
      type: mongodb
      url: ${MONGO_URL}
      database: content
      timeout: 3
      max_limit: 100
```

## Usage

Register the external factory in the project composition root:

```python
from muscles_data.catalog import DataAdapterCatalog
from muscles_data.config import DataConfig
from muscles_data.ports import DocumentStorePort
from muscles_data.runtime import DataRuntime
from muscles_data_mongodb import MongoDocumentStoreFactory

catalog = DataAdapterCatalog.with_defaults()
catalog.register(MongoDocumentStoreFactory())

runtime = DataRuntime(config=DataConfig.from_raw(config), catalog=catalog)
store = runtime.require_port("mongo.content", DocumentStorePort)
```

Then use the narrow port:

```python
store.upsert_document("profiles", "denis", {"name": "Denis"})
document = store.get_document("profiles", "denis")
documents = store.find_documents("profiles", filters={"role": "developer"}, limit=20)
store.delete_document("profiles", "denis")
```

The adapter maps `document_id` to MongoDB `_id` internally and returns public
documents without `_id` to keep the port backend-neutral.

## Capabilities

`MongoDocumentStoreFactory` provides:

- `document_store`;
- `healthcheck`;
- `native_client` only when the resource declares `native_client: true`.

Native access is an advanced project escape hatch:

```python
from muscles_data import DataCapability

client = runtime.require_resource("mongo.content", DataCapability.NATIVE_CLIENT).native_client()
```

Use the native client for backend-specific aggregations, indexes or operations
that do not belong in `DocumentStorePort`.

## Boundaries

This package owns:

- lazy PyMongo client creation;
- database binding;
- simple document get/find/upsert/delete;
- safe `inspect()` and `doctor()`;
- bounded `find_documents()` limits.

It does not own:

- ORM/document model layers;
- schema validation;
- migrations;
- complex aggregation abstraction;
- business repository APIs.

`data.doctor` performs a MongoDB `ping`. `data.resources.list` and package
initialization do not open a MongoDB connection.
