from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, Mapping
from urllib.parse import urlsplit

from muscles_data.config import DataResourceConfig
from muscles_data.errors import DataError
from muscles_data.models import DataCapability, HealthResult, InspectResult, WriteResult


_CLIENT_UNSET = object()
_ALLOWED_OPTIONS = {
    "url",
    "database",
    "username",
    "password",
    "auth_source",
    "timeout",
    "timeout_ms",
    "server_selection_timeout_ms",
    "max_limit",
    "native_client",
    "tls",
}


class MongoDBAdapterError(DataError):
    """Base error for MongoDB document-store adapter failures."""


class MongoDBConfigError(ValueError, MongoDBAdapterError):
    """Raised when a MongoDB resource config cannot be mapped safely."""


class MongoDBClientMissingError(MongoDBAdapterError):
    """Raised when MongoDB adapter is used without an available client."""


class MongoDBConnectionError(MongoDBAdapterError):
    """Raised when a MongoDB operation cannot reach or use the backend."""


class MongoDocumentStoreAdapter:
    resource_type = "mongodb"

    def __init__(
        self,
        config: DataResourceConfig,
        *,
        client_factory: Callable[[DataResourceConfig], Any] | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self._client: Any = _CLIENT_UNSET
        self._lock = threading.RLock()
        self.closed = False

    def get_document(self, collection: str, document_id: str) -> Mapping[str, Any] | None:
        try:
            raw = self._collection(collection).find_one({"_id": str(document_id)})
        except (MongoDBClientMissingError, MongoDBConfigError):
            raise
        except Exception as exc:
            raise MongoDBConnectionError(self._safe_error(exc)) from exc
        return _public_document(raw)

    def upsert_document(self, collection: str, document_id: str, document: Mapping[str, Any]) -> WriteResult:
        stored = _storage_document(document, document_id)
        try:
            result = self._collection(collection).replace_one({"_id": str(document_id)}, stored, upsert=True)
        except (MongoDBClientMissingError, MongoDBConfigError):
            raise
        except Exception as exc:
            raise MongoDBConnectionError(self._safe_error(exc)) from exc
        matched = int(getattr(result, "matched_count", 0) or 0)
        return WriteResult(written=1, matched=1 if matched else 0)

    def find_documents(
        self,
        collection: str,
        filters: Mapping[str, Any] | None = None,
        limit: int = 100,
        options: Mapping[str, Any] | None = None,
    ) -> list[Mapping[str, Any]]:
        options = dict(options or {})
        query = dict(filters or {})
        bounded_limit = self._bounded_limit(limit)
        try:
            cursor = self._collection(collection).find(query)
            if "sort" in options:
                cursor = cursor.sort(options["sort"])
            if "skip" in options:
                cursor = cursor.skip(max(0, int(options["skip"])))
            cursor = cursor.limit(bounded_limit)
            return [document for document in (_public_document(item) for item in cursor) if document is not None]
        except (MongoDBClientMissingError, MongoDBConfigError):
            raise
        except Exception as exc:
            raise MongoDBConnectionError(self._safe_error(exc)) from exc

    def delete_document(self, collection: str, document_id: str) -> WriteResult:
        try:
            result = self._collection(collection).delete_one({"_id": str(document_id)})
        except (MongoDBClientMissingError, MongoDBConfigError):
            raise
        except Exception as exc:
            raise MongoDBConnectionError(self._safe_error(exc)) from exc
        deleted = int(getattr(result, "deleted_count", 0) or 0)
        return WriteResult(deleted=deleted, matched=deleted)

    def inspect(self) -> dict[str, Any]:
        return asdict(
            InspectResult(
                name=self.config.name,
                type=self.config.type,
                capabilities=[],
                initialized=self._client is not _CLIENT_UNSET,
                status="ok",
                options=self.config.safe_options(),
                details={"backend": "mongodb", "database": self.database_name(), "max_limit": self.max_limit()},
            )
        )

    def health(self) -> HealthResult:
        try:
            self._client_instance().admin.command("ping")
        except Exception as exc:
            return HealthResult(status="failed", message=self._safe_error(exc), details={"database": self.database_name()})
        return HealthResult(status="ok", message="MongoDB connection is available", details={"database": self.database_name()})

    def close(self) -> None:
        if self._client is _CLIENT_UNSET:
            self.closed = True
            return
        close = getattr(self._client, "close", None)
        if callable(close):
            close()
        self.closed = True

    def native_client(self):
        return self._client_instance()

    def database_name(self) -> str:
        database = str(self.config.options.get("database", "")).strip()
        if not database:
            raise MongoDBConfigError("MongoDB resource requires database")
        return database

    def max_limit(self) -> int:
        value = int(self.config.options.get("max_limit", 100))
        if value <= 0:
            raise MongoDBConfigError("MongoDB max_limit must be positive")
        return value

    def _bounded_limit(self, limit: int) -> int:
        return min(max(0, int(limit)), self.max_limit())

    def _collection(self, collection: str):
        name = _collection_name(collection)
        return self._client_instance()[self.database_name()][name]

    def _client_instance(self):
        if self._client is _CLIENT_UNSET:
            with self._lock:
                if self._client is _CLIENT_UNSET:
                    self._validate_options()
                    client = (
                        self._client_factory(self.config)
                        if self._client_factory
                        else _default_mongo_client(self.config)
                    )
                    if client is None:
                        raise MongoDBClientMissingError("MongoDB client is not available")
                    self._client = client
        return self._client

    def _validate_options(self) -> None:
        unknown = sorted(set(self.config.options) - _ALLOWED_OPTIONS)
        if unknown:
            names = ", ".join(unknown)
            raise MongoDBConfigError(f"Unsupported MongoDB resource options: {names}")
        if not self.config.options.get("url"):
            raise MongoDBConfigError("MongoDB resource requires url")
        self.database_name()
        self.max_limit()

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc)
        sensitive_values = {
            str(self.config.options.get("url", "")),
            str(self.config.options.get("username", "")),
            str(self.config.options.get("password", "")),
        }
        try:
            parsed = urlsplit(str(self.config.options.get("url", "")))
            if parsed.username:
                sensitive_values.add(parsed.username)
            if parsed.password:
                sensitive_values.add(parsed.password)
        except Exception:  # pragma: no cover
            pass
        for value in sorted((item for item in sensitive_values if item), key=len, reverse=True):
            message = message.replace(value, "***")
        return message


class MongoDocumentStoreFactory:
    resource_type = "mongodb"

    def __init__(self, *, client_factory: Callable[[DataResourceConfig], Any] | None = None) -> None:
        self._client_factory = client_factory

    def capabilities(self, config: DataResourceConfig) -> set[DataCapability]:
        native = {DataCapability.NATIVE_CLIENT} if bool(config.options.get("native_client")) else set()
        return {DataCapability.DOCUMENT_STORE, DataCapability.HEALTHCHECK} | native

    def create(self, config: DataResourceConfig) -> MongoDocumentStoreAdapter:
        return MongoDocumentStoreAdapter(config, client_factory=self._client_factory)


def _default_mongo_client(config: DataResourceConfig):
    try:
        pymongo = importlib.import_module("pymongo")
    except ImportError as exc:
        raise MongoDBClientMissingError("pymongo package is not installed") from exc
    client_cls = getattr(pymongo, "MongoClient", None)
    if client_cls is None:
        raise MongoDBClientMissingError("pymongo does not expose MongoClient")

    kwargs: dict[str, Any] = {}
    if "timeout" in config.options:
        timeout_ms = int(float(config.options["timeout"]) * 1000)
        kwargs["timeoutMS"] = timeout_ms
        kwargs["serverSelectionTimeoutMS"] = timeout_ms
    if "timeout_ms" in config.options:
        kwargs["timeoutMS"] = int(config.options["timeout_ms"])
    if "server_selection_timeout_ms" in config.options:
        kwargs["serverSelectionTimeoutMS"] = int(config.options["server_selection_timeout_ms"])
    if "username" in config.options:
        kwargs["username"] = config.options["username"]
    if "password" in config.options:
        kwargs["password"] = config.options["password"]
    if "auth_source" in config.options:
        kwargs["authSource"] = config.options["auth_source"]
    if "tls" in config.options:
        kwargs["tls"] = bool(config.options["tls"])
    return client_cls(str(config.options["url"]), **kwargs)


def _collection_name(value: str) -> str:
    name = str(value).strip()
    if not name:
        raise MongoDBConfigError("MongoDB collection name must not be empty")
    if "\x00" in name:
        raise MongoDBConfigError("MongoDB collection name must not contain null bytes")
    return name


def _storage_document(document: Mapping[str, Any], document_id: str) -> dict[str, Any]:
    stored = dict(document)
    existing_id = stored.get("_id")
    if existing_id is not None and str(existing_id) != str(document_id):
        raise MongoDBConfigError("MongoDB document _id must match document_id")
    stored["_id"] = str(document_id)
    return stored


def _public_document(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    document = dict(raw)
    document.pop("_id", None)
    return document
