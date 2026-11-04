"""Request-scoped policy protection for MongoDB document operations."""

from collections.abc import Mapping

from muscles.data.errors import DataPolicyDeniedError, DataUnsupportedOperationError
from muscles.data.models import WriteResult
from muscles.data.policies import (
    DataOperationContext, DataOperationKind, DataOperationRequest, DataPolicyPipeline,
    DataPredicate, DataPredicateOperator,
    like_matches, like_regex_body,
)

from .adapter import _public_document, _storage_document


def _field(name):
    if not isinstance(name, str) or not name or name.startswith("$") or "." in name:
        raise DataUnsupportedOperationError("Unsupported policy field")
    return name


def _conditions(predicates):
    result = []
    for item in predicates:
        field = _field(item.field)
        if item.operator is DataPredicateOperator.LIKE:
            body = like_regex_body(item.value)
            result.append({field: {"$type": "string", "$regex": r"\A" + body + r"\z", "$options": "s", "$not": {"$type": "array"}}})
            continue
        if item.operator is DataPredicateOperator.IS_NULL and type(item.value) is bool:
            condition = {"$type": "null"} if item.value else {"$exists": True, "$ne": None}
            condition["$not"] = {"$type": "array"}
            result.append({field: condition})
            continue
        if (item.operator is not DataPredicateOperator.EQ or item.value is None
                or isinstance(item.value, (Mapping, list, tuple, set))):
            raise DataUnsupportedOperationError("Unsupported MongoDB policy predicate")
        # MongoDB equality matches array members; exclude arrays so a scalar
        # policy cannot accidentally grant access to a multivalued field.
        result.append({field: {"$eq": item.value, "$not": {"$type": "array"}}})
    return result


def _query(base, predicates):
    conditions = _conditions(predicates)
    return {"$and": [base, *conditions]} if conditions else base


class MongoPolicyDocumentStore:
    """A per-request wrapper; direct use of the underlying adapter is unprotected."""

    def __init__(self, adapter, *, policies: DataPolicyPipeline, context: DataOperationContext | None = None):
        self.adapter = adapter
        self.policies = policies
        self.context = context or DataOperationContext()

    def _evaluate(self, kind, collection, *, filters=(), values=None):
        return self.policies.evaluate(DataOperationRequest(kind, collection, self.context, filters, values or {}))

    def get_document(self, collection, document_id):
        filters = (DataPredicate("_id", DataPredicateOperator.EQ, str(document_id)),)
        predicates = self._evaluate(DataOperationKind.READ, collection, filters=filters)
        return _public_document(self.adapter._collection(collection).find_one(_query({"_id": str(document_id)}, predicates)))

    def find_documents(self, collection, filters=None, limit=100, options=None):
        if options:
            raise DataUnsupportedOperationError("Mongo policy store does not support query options")
        base = dict(filters or {})
        if any(isinstance(value, Mapping) or _field(key) != key for key, value in base.items()):
            raise DataUnsupportedOperationError("Mongo policy store accepts only equality filters")
        requested = tuple(DataPredicate(key, DataPredicateOperator.EQ, value) for key, value in base.items())
        predicates = self._evaluate(DataOperationKind.READ, collection, filters=requested)
        query = _query(base, predicates)
        bounded_limit = self.adapter._bounded_limit(limit)
        if bounded_limit == 0:
            return []
        cursor = self.adapter._collection(collection).find(query).limit(bounded_limit)
        return [document for document in (_public_document(item) for item in cursor) if document is not None]

    def delete_document(self, collection, document_id):
        requested = (DataPredicate("_id", DataPredicateOperator.EQ, str(document_id)),)
        predicates = self._evaluate(DataOperationKind.DELETE, collection, filters=requested)
        result = self.adapter._collection(collection).delete_one(_query({"_id": str(document_id)}, predicates))
        deleted = int(getattr(result, "deleted_count", 0) or 0)
        return WriteResult(deleted=deleted, matched=deleted)

    def upsert_document(self, collection, document_id, document):
        stored = _storage_document(document, document_id)
        requested = (DataPredicate("_id", DataPredicateOperator.EQ, str(document_id)),)
        predicates = self._evaluate(DataOperationKind.UPSERT, collection, filters=requested, values=stored)
        for predicate in predicates:
            _conditions((predicate,))
            field = _field(predicate.field)
            if field not in stored:
                raise DataPolicyDeniedError("Document values do not satisfy policy")
            actual = stored[field]
            if predicate.operator is DataPredicateOperator.LIKE:
                allowed = like_matches(actual, predicate.value)
            elif predicate.operator is DataPredicateOperator.IS_NULL:
                allowed = not isinstance(actual, (list, tuple, set)) and (actual is None) is predicate.value
            else:
                allowed = type(actual) is type(predicate.value) and actual == predicate.value
            if not allowed:
                raise DataPolicyDeniedError("Document values do not satisfy policy")
        query = _query({"_id": str(document_id)}, predicates)
        result = self.adapter._collection(collection).replace_one(query, stored, upsert=True)
        return WriteResult(written=1, matched=int(bool(getattr(result, "matched_count", 0))))
