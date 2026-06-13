"""Shared test fixtures: a fake boto3 Athena/S3 client (no live AWS)."""

import sys
from pathlib import Path

import pytest

# Ensure src layout is importable even without an editable install.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class FakeAthena:
    """Minimal stand-in for a boto3 Athena client.

    Drives a scripted sequence of states for GetQueryExecution and returns a
    canned ResultSet for GetQueryResults. Records the kwargs passed to
    start_query_execution so tests can assert on the SQL / parameters.
    """

    def __init__(self, states=("SUCCEEDED",), rows=None, state_change_reason="boom"):
        # `states` is consumed one entry per get_query_execution call; the last
        # entry repeats if polled more times than provided.
        self._states = list(states)
        self._rows = rows or []
        self._reason = state_change_reason
        self.started = []  # captured start_query_execution kwargs

    def start_query_execution(self, **kwargs):
        self.started.append(kwargs)
        return {"QueryExecutionId": "qid-123"}

    def get_query_execution(self, QueryExecutionId):
        state = self._states[0] if len(self._states) == 1 else self._states.pop(0)
        status = {"State": state}
        if state in ("FAILED", "CANCELLED"):
            status["StateChangeReason"] = self._reason
        return {"QueryExecution": {"Status": status}}

    def get_query_results(self, QueryExecutionId, MaxResults=1000):
        return {"ResultSet": {"Rows": self._rows}}


class FakeS3:
    """Minimal stand-in for a boto3 S3 client; records put_object calls."""

    def __init__(self):
        self.objects = []

    def put_object(self, **kwargs):
        self.objects.append(kwargs)
        return {"ETag": "fake"}


def make_result_rows(header, *data_rows):
    """Build an Athena-shaped Rows list (header row first)."""
    def to_row(values):
        return {"Data": [{"VarCharValue": v} for v in values]}

    return [to_row(header)] + [to_row(r) for r in data_rows]


@pytest.fixture
def fake_athena():
    return FakeAthena


@pytest.fixture
def fake_s3():
    return FakeS3()
