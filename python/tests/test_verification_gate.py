"""Tests for the extracted VerificationGate scaffold (row 29)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from cubiczan_resilience.verification_gate import (
    CLEAR,
    CONFIDENCE_FLOOR,
    PENALTY_PER_VIOLATION,
    REQUIRES_HUMAN_VERIFICATION,
    SEVERITY_FLOOR,
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


# --- severity_hint (v0.2.1): the empty-input floor override retired here ---


def test_default_severity_hint_preserves_existing_arithmetic():
    # v0.2.0 callers pass no hint; every confidence must be byte-identical
    # to the pre-0.2.1 rule.
    assert build_gate(["x"]).confidence == 100 - PENALTY_PER_VIOLATION
    assert build_gate(["x"]) == build_gate(["x"], severity_hint=None)
    assert build_gate([]) == build_gate([], severity_hint=None)
    assert build_gate(["x", "y"]).confidence == 76


def test_severity_floor_hint_pins_one_violation_at_the_floor():
    # The exact case the consumers' deleted overrides covered: an empty
    # input file appends one violation and must score the floor, not 88.
    gate = build_gate(["transcript file is empty"], severity_hint=SEVERITY_FLOOR)
    assert gate.status == REQUIRES_HUMAN_VERIFICATION
    assert gate.confidence == CONFIDENCE_FLOOR
    assert gate.violations == ["transcript file is empty"]
    assert not gate.is_clear


def test_severity_floor_hint_pins_even_when_arithmetic_would_score_higher():
    # Three violations score 64 on the equal-weight rule; the hint must
    # still pin the gate at the floor.
    gate = build_gate(["a", "b", "c"], severity_hint=SEVERITY_FLOOR)
    assert gate.confidence == CONFIDENCE_FLOOR
    assert gate.violations == ["a", "b", "c"]


def test_severity_floor_hint_cannot_manufacture_a_violation_from_blank_input():
    # A hint qualifies real violations; blank-only input stays CLEAR/100.
    gate = build_gate(["", "   "], severity_hint=SEVERITY_FLOOR)
    assert gate.status == CLEAR
    assert gate.confidence == 100
    assert gate.violations == []


def test_unknown_severity_hint_fails_loudly():
    # A typo'd hint ("Floor", "severe", "") must raise, not silently
    # degrade to the default arithmetic.
    for bad in ("Floor", "severe", "", "floor "):
        with pytest.raises(ValueError, match="severity_hint"):
            build_gate(["x"], severity_hint=bad)


def test_severity_floor_hint_gate_is_frozen_and_dict_round_trips():
    gate = build_gate(["holdings file is empty"], severity_hint=SEVERITY_FLOOR)
    with pytest.raises(FrozenInstanceError):
        gate.confidence = 88  # type: ignore[misc]
    d = gate.to_dict()
    assert d == {
        "status": "REQUIRES_HUMAN_VERIFICATION",
        "confidence": 50,
        "violations": ["holdings file is empty"],
    }
    rebuilt = VerificationGate(
        status=d["status"], confidence=d["confidence"], violations=d["violations"]
    )
    assert rebuilt == gate
