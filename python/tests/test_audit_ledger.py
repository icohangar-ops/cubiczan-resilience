import json

import pytest

from cubiczan_resilience import (
    DEFAULT_AUDIT_LEDGER_KEY,
    AuditLedger,
    verify_ledger,
)

KEY = "test-key-0123456789"


def _read_records(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_records(path, records):
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def test_relative_path_stays_under_base(tmp_path):
    ledger = AuditLedger("audit.jsonl", key=KEY, base_dir=tmp_path)
    ledger.append("e0", "a")
    assert verify_ledger(tmp_path / "audit.jsonl", key=KEY, base_dir=tmp_path).ok


def test_nested_in_tree_path_is_allowed(tmp_path):
    ledger = AuditLedger("nested/audit.jsonl", key=KEY, base_dir=tmp_path)
    ledger.append("e0", "a")
    assert verify_ledger(
        tmp_path / "nested" / "audit.jsonl", key=KEY, base_dir=tmp_path
    ).ok


def test_relative_traversal_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="escapes allowed base directory"):
        AuditLedger("../escaped.jsonl", key=KEY, base_dir=tmp_path)


def test_nested_traversal_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="escapes allowed base directory"):
        AuditLedger("nested/../../escaped.jsonl", key=KEY, base_dir=tmp_path)
    with pytest.raises(ValueError, match="escapes allowed base directory"):
        verify_ledger("../escaped.jsonl", key=KEY, base_dir=tmp_path)


def test_absolute_path_outside_base_is_rejected(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "audit.jsonl"
    with pytest.raises(ValueError, match="escapes allowed base directory"):
        AuditLedger(outside, key=KEY, base_dir=tmp_path)


def test_append_n_records_and_verify_passes(tmp_path):
    path = tmp_path / "audit.jsonl"
    ledger = AuditLedger(path, key=KEY, base_dir=tmp_path)
    for i in range(5):
        ledger.append(
            "decision",
            "agent-1",
            inputs={"i": i},
            sources=["src-a"],
            confidence=0.9,
            rationale=f"step {i}",
        )
    result = ledger.verify()
    assert result.ok
    assert bool(result) is True
    assert result.count == 5


def test_records_chain_to_prior_signature(tmp_path):
    path = tmp_path / "audit.jsonl"
    ledger = AuditLedger(path, key=KEY, base_dir=tmp_path)
    s0 = ledger.append("e0", "a")
    s1 = ledger.append("e1", "a")

    recs = _read_records(path)
    assert recs[0]["prev_sig"] == ""  # genesis
    assert recs[0]["sig"] == s0
    assert recs[1]["prev_sig"] == s0  # links to prior sig
    assert recs[1]["sig"] == s1


def test_resumes_chain_across_instances(tmp_path):
    path = tmp_path / "audit.jsonl"
    l1 = AuditLedger(path, key=KEY, base_dir=tmp_path)
    l1.append("e0", "a")
    l1.append("e1", "a")

    l2 = AuditLedger(path, key=KEY, base_dir=tmp_path)
    l2.append("e2", "a")

    result = verify_ledger(path, key=KEY, base_dir=tmp_path)
    assert result.ok
    assert result.count == 3


def test_verify_passes_on_absent_ledger(tmp_path):
    result = verify_ledger(tmp_path / "missing.jsonl", key=KEY, base_dir=tmp_path)
    assert result.ok
    assert result.count == 0


def test_detects_edited_payload_at_right_index(tmp_path):
    path = tmp_path / "audit.jsonl"
    ledger = AuditLedger(path, key=KEY, base_dir=tmp_path)
    for i in range(4):
        ledger.append("decision", "agent-1", inputs={"i": i})

    recs = _read_records(path)
    recs[2]["actor"] = "attacker"  # tamper line index 2, keep its sig
    _write_records(path, recs)

    result = verify_ledger(path, key=KEY, base_dir=tmp_path)
    assert not result.ok
    assert result.tampered_index == 2


def test_detects_deleted_interior_line(tmp_path):
    path = tmp_path / "audit.jsonl"
    ledger = AuditLedger(path, key=KEY, base_dir=tmp_path)
    for i in range(4):
        ledger.append("decision", "agent-1", inputs={"i": i})

    recs = _read_records(path)
    del recs[1]  # drop line index 1
    _write_records(path, recs)

    result = verify_ledger(path, key=KEY, base_dir=tmp_path)
    assert not result.ok
    # Former line 2 (now at index 1) has a prev_sig that no longer matches.
    assert result.tampered_index == 1


def test_fails_under_wrong_key(tmp_path):
    path = tmp_path / "audit.jsonl"
    ledger = AuditLedger(path, key=KEY, base_dir=tmp_path)
    ledger.append("e0", "a")
    result = verify_ledger(path, key="the-wrong-key", base_dir=tmp_path)
    assert not result.ok
    assert result.tampered_index == 0


def test_default_key_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("AUDIT_LEDGER_KEY", raising=False)
    path = tmp_path / "audit.jsonl"
    ledger = AuditLedger(path, key=DEFAULT_AUDIT_LEDGER_KEY, base_dir=tmp_path)
    ledger.append("e0", "a")
    # No key passed -> resolves to env (unset) then the default.
    result = verify_ledger(path, base_dir=tmp_path)
    assert result.ok


def test_cross_language_signature_matches(tmp_path):
    # The TypeScript and Rust ports assert this exact signature too, so the
    # three implementations can never silently drift on canonicalization /
    # signing.
    path = tmp_path / "x.jsonl"
    ledger = AuditLedger(path, key="k", base_dir=tmp_path)
    sig = ledger.append(
        "e", "a", inputs={"x": 1}, sources=["s"], ts="2026-01-01T00:00:00Z"
    )
    assert sig == "d379966f5be33822aa1091efa18034e67e679fbadb168bb73c3f42ef712a46fc"


def test_env_key_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LEDGER_KEY", "env-supplied-key")
    path = tmp_path / "audit.jsonl"
    ledger = AuditLedger(path, base_dir=tmp_path)  # picks up env key
    ledger.append("e0", "a")
    assert verify_ledger(path, base_dir=tmp_path).ok  # verify also reads env key
    assert not verify_ledger(path, key="other", base_dir=tmp_path).ok
