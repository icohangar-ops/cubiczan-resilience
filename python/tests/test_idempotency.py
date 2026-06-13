import json

import pytest

from cubiczan_resilience import (
    FileIdempotencyStore,
    IdempotencyStore,
    InMemoryIdempotencyStore,
)


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path) -> IdempotencyStore:
    if request.param == "memory":
        return InMemoryIdempotencyStore()
    return FileIdempotencyStore(tmp_path / "idem.json")


def test_blocks_second_call(store: IdempotencyStore):
    charges = {"n": 0}

    def charge(key: str):
        if store.already_done(key):
            return store.get_result(key)
        # claim first; mark_done is the atomic gate
        if not store.mark_done(key, result="charged"):
            return store.get_result(key)
        charges["n"] += 1
        return "charged"

    assert charge("order-1") == "charged"
    assert charge("order-1") == "charged"  # second call short-circuits
    assert charges["n"] == 1


def test_mark_done_returns_false_on_duplicate(store: IdempotencyStore):
    assert store.mark_done("k", 1) is True
    assert store.mark_done("k", 2) is False
    assert store.get_result("k") == 1  # not overwritten


def test_isinstance_protocol():
    assert isinstance(InMemoryIdempotencyStore(), IdempotencyStore)


def test_file_store_persists(tmp_path):
    path = tmp_path / "idem.json"
    s1 = FileIdempotencyStore(path)
    s1.mark_done("payout-9", {"amount": 100})
    # New instance reads the persisted state.
    s2 = FileIdempotencyStore(path)
    assert s2.already_done("payout-9")
    assert s2.get_result("payout-9") == {"amount": 100}
    # File is valid JSON (atomic write never left it partial).
    json.loads(path.read_text())
