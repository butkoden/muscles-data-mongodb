# muscles-data-mongodb

MongoDB adapter package for `muscles-data`.

This package is intentionally separate from `muscles-data`: the core package
owns typed ports and runtime, while this package owns the PyMongo-backed
`DocumentStorePort` adapter.
