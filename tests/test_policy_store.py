from types import SimpleNamespace

import pytest

from muscles.data.errors import DataPolicyDeniedError, DataUnsupportedOperationError
from muscles.data.policies import DataOperationContext, DataPolicyDecision, DataPolicyPipeline, DataPredicate, DataPredicateOperator
from muscles.data.mongodb.policy_store import MongoPolicyDocumentStore


class Collection:
    def __init__(self):
        self.calls = []

    def find_one(self, query):
        self.calls.append(("find_one", query))
        return None

    def find(self, query):
        self.calls.append(("find", query))
        return self

    def limit(self, value):
        self.calls.append(("limit", value))
        return self

    def __iter__(self):
        return iter(())

    def delete_one(self, query):
        self.calls.append(("delete_one", query))
        return SimpleNamespace(deleted_count=0)

    def replace_one(self, query, document, upsert):
        self.calls.append(("replace_one", query, document, upsert))
        return SimpleNamespace(matched_count=0)


class Adapter:
    def __init__(self):
        self.collection = Collection()

    def _collection(self, name):
        return self.collection

    def _bounded_limit(self, value):
        return value


class Workspace:
    def evaluate(self, request):
        return DataPolicyDecision(predicates=(DataPredicate("workspace_uid", DataPredicateOperator.EQ, request.context.attributes["workspace_uid"]),))


def test_policy_filters_reads_and_deletes_in_database_before_limit():
    adapter = Adapter()
    store = MongoPolicyDocumentStore(adapter, policies=DataPolicyPipeline((Workspace(),)), context=DataOperationContext(attributes={"workspace_uid": "w1"}))
    store.find_documents("items", {"kind": "note"}, limit=1)
    store.delete_document("items", "x")
    assert adapter.collection.calls == [
        ("find", {"$and": [{"kind": "note"}, {"workspace_uid": {"$eq": "w1", "$not": {"$type": "array"}}}]}),
        ("limit", 1),
        ("delete_one", {"$and": [{"_id": "x"}, {"workspace_uid": {"$eq": "w1", "$not": {"$type": "array"}}}]}),
    ]


def test_policy_upsert_validates_values_and_limits_existing_record():
    adapter = Adapter()
    store = MongoPolicyDocumentStore(adapter, policies=DataPolicyPipeline((Workspace(),)), context=DataOperationContext(attributes={"workspace_uid": "w1"}))
    with pytest.raises(DataPolicyDeniedError):
        store.upsert_document("items", "x", {"workspace_uid": "w2"})
    assert adapter.collection.calls == []
    store.upsert_document("items", "x", {"workspace_uid": "w1"})
    assert adapter.collection.calls[0][1] == {"$and": [{"_id": "x"}, {"workspace_uid": {"$eq": "w1", "$not": {"$type": "array"}}}]}


def test_unsupported_policy_predicate_fails_before_accessing_database():
    class Like:
        def evaluate(self, request):
            return DataPolicyDecision(predicates=(DataPredicate("name", DataPredicateOperator.IN, ["x"]),))

    adapter = Adapter()
    store = MongoPolicyDocumentStore(adapter, policies=DataPolicyPipeline((Like(),)))
    with pytest.raises(DataUnsupportedOperationError):
        store.find_documents("items")
    assert adapter.collection.calls == []


def test_zero_limit_never_returns_unlimited_mongodb_results():
    adapter = Adapter()
    store = MongoPolicyDocumentStore(adapter, policies=DataPolicyPipeline())
    assert store.find_documents("items", limit=0) == []
    assert adapter.collection.calls == []


@pytest.mark.parametrize(("operator", "value", "clause"), [
    (DataPredicateOperator.LIKE, "a_%", {"field": {"$type": "string", "$regex": r"\Aa..*\z", "$options": "s", "$not": {"$type": "array"}}}),
    (DataPredicateOperator.IS_NULL, True, {"field": {"$type": "null", "$not": {"$type": "array"}}}),
    (DataPredicateOperator.IS_NULL, False, {"field": {"$exists": True, "$ne": None, "$not": {"$type": "array"}}}),
])
def test_mongodb_translates_like_and_null_before_limit(operator, value, clause):
    class Policy:
        def evaluate(self, request):
            return DataPolicyDecision(predicates=(DataPredicate("field", operator, value),))

    adapter = Adapter()
    store = MongoPolicyDocumentStore(adapter, policies=DataPolicyPipeline((Policy(),)))
    store.find_documents("items", limit=1)
    assert adapter.collection.calls == [("find", {"$and": [{}, clause]}), ("limit", 1)]
