# muscles-data-mongodb

MongoDB adapter package for `muscles-data`.

This package is intentionally separate from `muscles-data`: the core package
owns typed ports and runtime, while this package owns the PyMongo-backed
`DocumentStorePort` adapter.

## Related packages

- Core runtime and port contracts:
  [`muscles-data`](https://github.com/butkoden/muscles-data)
- Elasticsearch search adapter:
  [`muscles-data-elasticsearch`](https://github.com/butkoden/muscles-data-elasticsearch)
- OpenSearch search adapter:
  [`muscles-data-opensearch`](https://github.com/butkoden/muscles-data-opensearch)
- Redis key-value/lock/stream adapter:
  [`muscles-data-redis`](https://github.com/butkoden/muscles-data-redis)
- Qdrant vector adapter:
  [`muscles-data-qdrant`](https://github.com/butkoden/muscles-data-qdrant)
- S3 object-store adapter:
  [`muscles-data-s3`](https://github.com/butkoden/muscles-data-s3)
- SQLAlchemy direct SQL resource adapter:
  [`muscles-data-sqlalchemy`](https://github.com/butkoden/muscles-data-sqlalchemy)
- Executable example:
  [`example_data_mongodb_1`](https://github.com/butkoden/muscular-example/tree/master/example_data_mongodb_1)

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
      url_env: MONGO_URL
      database: content
      timeout: 3
      max_limit: 100
```

## Usage

Register this adapter once in the application composition root when building
the shared `DataAdapterCatalog`, alongside other configured adapters:

```python
catalog.register(MongoDocumentStoreFactory())
```

The `mongo.content` resource itself is declared in the application's central
`data.resources` configuration shown below. A package resolves the port from
the existing runtime; it does not build another catalog or `DataRuntime`:

```python
from muscles.data.ports import DocumentStorePort

store = app.resource.connect("mongo.content", DocumentStorePort)
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
