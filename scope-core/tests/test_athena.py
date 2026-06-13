"""Tests for the safe Athena helpers."""

import pytest

from conftest import FakeAthena, make_result_rows

from scope_core.athena import (
    AthenaQueryError,
    AthenaTimeoutError,
    IdentifierError,
    SafeAthenaClient,
    validate_identifier,
    validate_in_allowlist,
)


# -- allowlist / identifier validation (the SQL-injection fix) -------------

def test_allowlist_accepts_member():
    assert validate_in_allowlist("WTI", {"WTI", "BRENT"}, field="commodity_code") == "WTI"


def test_allowlist_rejects_injection_payload():
    payload = "O'; DROP TABLE reits;--"
    with pytest.raises(IdentifierError):
        validate_in_allowlist(payload, {"O", "PLD"}, field="ticker")


def test_allowlist_rejects_non_member():
    with pytest.raises(IdentifierError):
        validate_in_allowlist("XYZ", {"WTI", "BRENT"})


def test_allowlist_accepts_iterable_not_just_set():
    assert validate_in_allowlist("A", ["A", "B"]) == "A"


def test_validate_identifier_accepts_dotted():
    assert validate_identifier("db.table.col") == "db.table.col"


@pytest.mark.parametrize("bad", ["1abc", "a b", "a;b", "drop table x", "a'--", 42, None])
def test_validate_identifier_rejects_bad_shapes(bad):
    with pytest.raises(IdentifierError):
        validate_identifier(bad)


# -- query executor returns rows ------------------------------------------

def _client(athena):
    return SafeAthenaClient(
        athena,
        database="db",
        output_location="s3://out/",
        poll_timeout=5,
        poll_interval=0,
    )


def test_execute_returns_rows():
    rows = make_result_rows(
        ["commodity_code", "latest_price"],
        ["WTI", "80.5"],
        ["BRENT", "84.2"],
    )
    athena = FakeAthena(states=("SUCCEEDED",), rows=rows)
    client = _client(athena)

    result = client.execute("SELECT * FROM t")

    assert result["state"] == "SUCCEEDED"
    assert result["query_execution_id"] == "qid-123"
    assert result["rows"] == [
        {"commodity_code": "WTI", "latest_price": "80.5"},
        {"commodity_code": "BRENT", "latest_price": "84.2"},
    ]


def test_execute_eventually_succeeds_after_running():
    rows = make_result_rows(["x"], ["1"])
    athena = FakeAthena(states=["RUNNING", "RUNNING", "SUCCEEDED"], rows=rows)
    client = _client(athena)
    result = client.execute("SELECT 1")
    assert result["rows"] == [{"x": "1"}]


def test_execute_passes_execution_parameters():
    athena = FakeAthena(states=("SUCCEEDED",), rows=make_result_rows(["x"]))
    client = _client(athena)
    client.execute("SELECT * FROM t WHERE name = ?", parameters=["a'b"])
    assert athena.started[0]["ExecutionParameters"] == ["a'b"]


# -- query executor times out / fails -------------------------------------

def test_execute_times_out():
    athena = FakeAthena(states=("RUNNING",))  # never terminal
    client = SafeAthenaClient(
        athena, database="db", output_location="s3://out/",
        poll_timeout=0.01, poll_interval=0,
    )
    with pytest.raises(AthenaTimeoutError):
        client.execute("SELECT 1")


def test_execute_raises_on_failed_query():
    athena = FakeAthena(states=("FAILED",), state_change_reason="syntax error")
    client = _client(athena)
    with pytest.raises(AthenaQueryError) as exc:
        client.execute("SELECT bad")
    assert "syntax error" in str(exc.value)


def test_execute_no_fetch_returns_none_rows():
    athena = FakeAthena(states=("SUCCEEDED",))
    client = _client(athena)
    result = client.execute("INSERT INTO t VALUES (1)", fetch=False)
    assert result["rows"] is None
