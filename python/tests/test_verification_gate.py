"""Tests for the extracted VerificationGate scaffold (row 29)."""

from __future__ import annotations

import pytest

from cubiczan_resilience.verification_gate import (
    CLEAR,
    CONFIDENCE_FLOOR,
    PENALTY_PER_VIOLATION,
    REQUIRES_HUMAN_VERIFICATION,
    VerificationGate,
    build_gate,
)


def test_no_violations_is_clear_at_full_confidence():
    gate = build_gate([])
    assert gate.status == CLEAR
    assert gate.confidence == 100
    assert gate.violations == []
    assert gate.is_clear
    assert gate.to_dict() == {"status": "CLEAR", "confidence": 100, "violations": []}


def test_penalty_per_violation_is_the_canonical_number():
    # The donor trio drifted (-12 in two repos, -10 in the third); the
    # canonical penalty is pinned here so a fork is a failing test.
    assert PENALTY_PER_VIOLATION == 12


def test_one_violation():
    gate = build_gate(["row 7 missing source_url"])
    assert gate.status == REQUIRES_HUMAN_VERIFICATION
    assert gate.confidence == 100 - PENALTY_PER_VIOLATION
    assert gate.violations == ["row 7 missing source_url"]
    assert not gate.is_clear


def test_confidence_floors_and_never_goes_negative():
    violations = [f"violation {i}" for i in range(10)]
    gate = build_gate(violations)
    assert gate.confidence == CONFIDENCE_FLOOR
    assert len(gate.violations) == 10
    # Floor preserved even with far more violations.
    assert build_gate([f"v{i}" for i in range(100)]).confidence == CONFIDENCE_FLOOR


def test_computation_is_deterministic():
    a = build_gate(["x", "y", "z"])
    b = build_gate(["x", "y", "z"])
    assert a == b


def test_blank_strings_are_dropped():
    gate = build_gate(["real", "", "   "])
    assert gate.violations == ["real"]


def test_blocking_issues_lines_render_verbatim():
    gate = build_gate(["a", "b"])
    assert gate.blocking_issues_lines() == ["- a", "- b"]
    assert build_gate([]).blocking_issues_lines() == []


def test_gate_is_frozen():
    gate = build_gate(["x"])
    with pytest.raises(Exception):  # noqa: B017 — dataclass FrozenInstanceError
        gate.confidence = 100  # type: ignore[misc]


def test_status_vocabulary_is_exactly_two_words():
    assert build_gate([]).status == "CLEAR"
    assert build_gate(["x"]).status == "REQUIRES_HUMAN_VERIFICATION"


def test_to_dict_round_trip():
    gate = build_gate(["missing source_date"])
    d = gate.to_dict()
    rebuilt = VerificationGate(
        status=d["status"], confidence=d["confidence"], violations=d["violations"]
    )
    assert rebuilt == gate
