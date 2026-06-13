"""Tests for shared utilities."""

import json

import pytest

from conftest import FakeS3

from scope_core.utils import (
    error_response,
    poll_until_terminal,
    response_envelope,
    success_response,
    write_jsonl_to_s3,
    write_object_to_s3,
)


def test_write_object_to_s3_returns_uri():
    s3 = FakeS3()
    uri = write_object_to_s3(s3, bucket="b", key="k/x.json", body="hi", content_type="application/json")
    assert uri == "s3://b/k/x.json"
    assert s3.objects[0]["Bucket"] == "b"
    assert s3.objects[0]["ContentType"] == "application/json"


def test_write_jsonl_serializes_records():
    s3 = FakeS3()
    uri = write_jsonl_to_s3(s3, bucket="b", key="k.jsonl", records=[{"a": 1}, {"a": 2}])
    assert uri == "s3://b/k.jsonl"
    body = s3.objects[0]["Body"]
    assert [json.loads(line) for line in body.splitlines()] == [{"a": 1}, {"a": 2}]


def test_poll_until_terminal_returns_state():
    states = iter(["RUNNING", "RUNNING", "SUCCEEDED"])
    clock = iter([0, 1, 2, 3, 4])
    state = poll_until_terminal(
        lambda: next(states),
        terminal={"SUCCEEDED", "FAILED"},
        timeout=100,
        interval=0,
        time_fn=lambda: next(clock),
        sleep_fn=lambda _s: None,
    )
    assert state == "SUCCEEDED"


def test_poll_until_terminal_times_out():
    clock = iter([0, 0, 1, 2, 3, 4, 5])
    with pytest.raises(TimeoutError):
        poll_until_terminal(
            lambda: "RUNNING",
            terminal={"SUCCEEDED"},
            timeout=1,
            interval=0,
            time_fn=lambda: next(clock),
            sleep_fn=lambda _s: None,
        )


def test_response_helpers():
    assert response_envelope(201, {"ok": True}) == {"statusCode": 201, "body": json.dumps({"ok": True})}
    assert success_response({"x": 1})["statusCode"] == 200
    err = error_response(ValueError("nope"))
    assert err["statusCode"] == 500
    assert json.loads(err["body"]) == {"error": "nope"}
