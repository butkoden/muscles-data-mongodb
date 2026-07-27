# `muscles-data-mongodb` RC checklist

The package ships the MongoDB implementation of `DocumentStorePort`.
The dependency on `muscles-data` is versioned as `>=0.1.0,<1.0.0`.

Before publishing a GitHub Release, run:

```bash
PYTHONPATH=../muscles-data/src:src python -m pytest -q
python -m build --wheel --sdist
```

The integration scenario is enabled with `MUSCLES_DATA_INTEGRATION=1` and a
running MongoDB service configured through `MONGODB_URL`. Document content,
credentials and native clients are excluded from diagnostics unless explicitly
requested through the capability API.

The PyPI workflow publishes only after a GitHub Release is published. It uses
the versioned `muscles-data` dependency and trusted publishing.
