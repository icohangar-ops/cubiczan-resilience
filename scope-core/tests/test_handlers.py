"""Tests for the base ingestion / analysis handler scaffolds."""

import json

import pytest

from conftest import FakeAthena, FakeS3, make_result_rows

from scope_core.athena import SafeAthenaClient
from scope_core.handlers import BaseAnalysisHandler, BaseIngestionHandler


# -- ingestion scaffold ----------------------------------------------------

class _Ingestion(BaseIngestionHandler):
    def __init__(self, s3, records):
        super().__init__(s3, bucket="raw-bucket")
        self._records = records
        self.fetch_calls = []

    def build_s3_key(self, event):
        return f"prefix/{event['day']}/out.jsonl"

    def fetch_records(self, event):
        self.fetch_calls.append(event)
        return self._records


def test_ingestion_writes_records_and_envelopes():
    s3 = FakeS3()
    h = _Ingestion(s3, records=[{"a": 1}, {"a": 2}])
    resp = h.handle({"day": "2026-06-13"})

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["records_fetched"] == 2
    assert body["s3_path"] == "s3://raw-bucket/prefix/2026-06-13/out.jsonl"
    # fetch_records was the injected hook actually called with the event
    assert h.fetch_calls == [{"day": "2026-06-13"}]
    assert s3.objects[0]["Key"] == "prefix/2026-06-13/out.jsonl"


def test_ingestion_no_records_skips_s3():
    s3 = FakeS3()
    h = _Ingestion(s3, records=[])
    body = json.loads(h.handle({"day": "x"})["body"])
    assert body["records_fetched"] == 0
    assert body["s3_path"] == ""
    assert s3.objects == []


def test_ingestion_error_is_enveloped():
    class Boom(BaseIngestionHandler):
        def fetch_records(self, event):
            raise RuntimeError("api down")

    resp = Boom(FakeS3(), bucket="b").handle({})
    assert resp["statusCode"] == 500
    assert json.loads(resp["body"]) == {"error": "api down"}


def test_ingestion_fetch_records_required():
    with pytest.raises(NotImplementedError):
        BaseIngestionHandler(FakeS3(), bucket="b").ingest({})


# -- analysis scaffold -----------------------------------------------------

class _Analysis(BaseAnalysisHandler):
    """Records the query/columns it was asked to build per entity."""

    def __init__(self, client):
        super().__init__(client, allowlist={"WTI", "BRENT"}, entity_field="commodity_code")
        self.built = []

    def build_query(self, entity, event):
        # `entity` is allowlisted and safe; free text goes through parameters.
        sql = f"SELECT '{entity}' AS commodity_code, price FROM prices WHERE code = '{entity}' AND src = ?"
        params = [event.get("source", "EIA")]
        self.built.append((entity, sql, params))
        return sql, params


def _client(states=("SUCCEEDED",), rows=None):
    athena = FakeAthena(states=states, rows=rows or make_result_rows(["commodity_code", "price"], ["WTI", "80"]))
    client = SafeAthenaClient(athena, database="db", output_location="s3://o/", poll_timeout=5, poll_interval=0)
    return athena, client


def test_analysis_validates_then_calls_injected_query():
    athena, client = _client()
    h = _Analysis(client)

    results = h.analyze(["WTI"], {"source": "EIA"})

    assert len(results) == 1
    res = results[0]
    assert res["status"] == "completed"
    assert res["commodity_code"] == "WTI"
    assert res["rows"] == [{"commodity_code": "WTI", "price": "80"}]
    # the injected build_query hook was called with the validated entity + event
    assert h.built[0][0] == "WTI"
    # the SQL the handler built was the one actually started on Athena
    assert athena.started[0]["QueryString"] == h.built[0][1]
    # free-text source was bound as an execution parameter, not formatted in
    assert athena.started[0]["ExecutionParameters"] == ["EIA"]


def test_analysis_rejects_entity_not_in_allowlist():
    athena, client = _client()
    h = _Analysis(client)

    results = h.analyze(["WTI", "HACK'; DROP--"], {})

    assert results[0]["status"] == "completed"
    assert results[1]["status"] == "error"
    assert "Disallowed commodity_code" in results[1]["error"]
    # only the valid entity ever reached Athena
    assert len(athena.started) == 1


def test_analysis_per_entity_error_isolation_on_query_failure():
    athena = FakeAthena(states=("FAILED",), state_change_reason="bad sql")
    client = SafeAthenaClient(athena, database="db", output_location="s3://o/", poll_timeout=5, poll_interval=0)
    h = _Analysis(client)

    results = h.analyze(["WTI"], {})
    assert results[0]["status"] == "error"
    assert "bad sql" in results[0]["error"]


def test_analysis_build_query_required():
    _, client = _client()
    h = BaseAnalysisHandler(client, allowlist={"WTI"}, entity_field="x")
    out = h.analyze(["WTI"], {})
    # NotImplementedError is caught per-entity and surfaced as an error result
    assert out[0]["status"] == "error"
