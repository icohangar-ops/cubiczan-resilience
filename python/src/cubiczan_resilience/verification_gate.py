"""VerificationGate: the report scaffold that keeps unverified output from looking decision-ready.

Extracted from the byte-similar ``VerificationGate`` scaffolds carried by
``earnings-call-nlp-lab``, ``market-sentiment-fedgpt``, and
``hedge-fund-13f-radar`` (matrix row 29). The three copies had already
drifted at extraction time: the per-violation confidence penalty was
``-12`` in two repos and ``-10`` in the third. The whole point of this
module is that the rule lives in exactly one place from now on.

The pattern: every generated report ends with a gate object that states,
in the report itself, whether the report's own checks passed. A report
rendered without its gate cannot masquerade as verified; a gate with
violations carries a visibly reduced confidence and names every
violation verbatim.

Design rules, taken from the donor implementations:

* **Violations are strings, and they are the evidence.** Each violation is
  a human-readable sentence naming what is missing or malformed — not a
  code the reader must decode.
* **Confidence is deterministic.** Two runs over the same violations
  produce the same number. There is no model in this module.
* **The floor is real.** Confidence stops at :data:`CONFIDENCE_FLOOR`
  (50): a failing gate never scores 0, because a reader must still be
  able to act on the listed violations rather than discard the report.
* **The status vocabulary has two words.** :data:`CLEAR` only at full
  confidence; :data:`REQUIRES_HUMAN_VERIFICATION` for anything else.

The CHP alternative, priced (row 29): CHP's runtime (consensus-hardening-
protocol) ships an *enforced decision gate* — R0, adversary pass,
foundation scoring, human lock — which is heavier machinery than a
report-level scaffold needs, and its published npm package covers
Profile B only. A report that merely states its own verification status
should not have to depend on a protocol runtime; this module is the
lighter primitive. Fold into CHP instead only if CHP publishes a
Profile-A report-gate surface in the Python runtime.

Package boundary, recorded deliberately (not an accident of history): this
scaffold lives in ``cubiczan-resilience`` rather than a new
``cubiczan-report`` package because the donor repos already consume
cubiczan-resilience as a git dependency — extraction here added zero
distribution steps, and the pattern is resilience-adjacent (a gate that
keeps degraded output from being trusted silently). Revisit trigger: the
moment one or two more report-level primitives appear (source-attribution
checks, citation integrity), split them into ``cubiczan-report`` instead
of growing this module.

Example::

    gate = build_gate(violations=["row 7 missing source_url"])
    if gate.status is REQUIRES_HUMAN_VERIFICATION:
        render_warning(gate.violations)
    report.verification = gate          # the report carries its gate
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

#: Status of a gate whose checks all passed.
CLEAR = "CLEAR"

#: Status of a gate with at least one violation.
REQUIRES_HUMAN_VERIFICATION = "REQUIRES_HUMAN_VERIFICATION"

#: Confidence subtracted per violation. Canonical number for the whole
#: portfolio; do not fork it per repo (that is the drift this module exists
#: to end).
#:
#: Why 12: majority basis — 2 of the 3 donor repos (earnings-call-nlp-lab,
#: market-sentiment-fedgpt) already subtracted 12; hedge-fund-13f-radar used
#: 10 and is migrated to 12 here. Revisit only with domain sign-off — the
#: pinning test is the standing ground against a casual "why not 10".
#:
#: Import policy: import these constants to verify or display them (tests,
#: audit logs, report footnotes) — never to derive thresholds from them.
#: Derived consumer logic breaks silently when the canonical number moves.
PENALTY_PER_VIOLATION = 12

#: Confidence never drops below this, so a failing report stays actionable.
CONFIDENCE_FLOOR = 50

#: severity_hint value for :func:`build_gate`: pins a failing gate's
#: confidence at :data:`CONFIDENCE_FLOOR` no matter how few violations
#: produced it.
#:
#: Why this exists (row 29 follow-through): the donor repos' empty-input
#: cases (an empty transcript, an empty holdings file) are categorically
#: worse than one minor gap, but the equal-weight arithmetic scores a
#: single violation 88. earnings-call-nlp-lab and hedge-fund-13f-radar
#: each carried a local override pinning that case at the floor; this
#: hint moves the decision here so those overrides could be deleted. The
#: caller states only where the gate must sit — which violations, which
#: penalty, which floor all stay canonical.
#:
#: Import policy: import these constants to verify or display them (tests,
#: audit logs, report footnotes) — never to derive thresholds from them.
#: Derived consumer logic breaks silently when the canonical number moves.
SEVERITY_FLOOR = "floor"


@dataclass(frozen=True)
class VerificationGate:
    """The verification block a generated report carries inside itself.

    ``status`` is :data:`CLEAR` exactly when ``violations`` is empty and
    ``confidence`` is 100; otherwise
    :data:`REQUIRES_HUMAN_VERIFICATION`.
    """

    status: str
    confidence: int
    violations: List[str] = field(default_factory=list)

    @property
    def is_clear(self) -> bool:
        return self.status == CLEAR

    def to_dict(self) -> Dict[str, Any]:
        """JSON-ready form, matching the donor repos' ``to_dict`` shape."""
        return {
            "status": self.status,
            "confidence": self.confidence,
            "violations": list(self.violations),
        }

    def blocking_issues_lines(self) -> List[str]:
        """The markdown block the donor repos render under a violations
        heading; empty when the gate is clear."""
        if not self.violations:
            return []
        return [f"- {item}" for item in self.violations]


def build_gate(
    violations: Sequence[str],
    severity_hint: str | None = None,
) -> VerificationGate:
    """Compute the canonical gate from a list of violation strings.

    This function is the single source of the confidence arithmetic. The
    donor repos inlined it with diverging penalties (-12 / -10); every
    consumer now calls this instead.

    Args:
        violations: Human-readable violation sentences; blank and
            whitespace-only entries are silently filtered before scoring.
        severity_hint: ``None`` (the default) keeps the equal-weight
            arithmetic: ``100 - PENALTY_PER_VIOLATION * len(violations)``,
            floored at :data:`CONFIDENCE_FLOOR`. :data:`SEVERITY_FLOOR`
            pins a failing gate at :data:`CONFIDENCE_FLOOR` regardless of
            violation count — for the categorically-severe cases (an empty
            input file) where one violation does not capture how bad the
            situation is. Any other value raises ``ValueError``: a typo'd
            hint must fail loudly, not silently no-op.

    Warning: blank and whitespace-only entries are silently filtered
    before scoring, so a violations list containing only blanks yields a
    CLEAR gate at confidence 100 — even with
    ``severity_hint=SEVERITY_FLOOR``. A hint qualifies the severity of
    real violations; it cannot manufacture a violation out of blank
    input. That is "no violations found", which is only as trustworthy as
    the checks that produced the list — callers assembling violations
    programmatically must not treat it as proof the checks ran.
    """
    if severity_hint is not None and severity_hint != SEVERITY_FLOOR:
        raise ValueError(
            f"unknown severity_hint {severity_hint!r}: the only supported "
            f"value is SEVERITY_FLOOR ({SEVERITY_FLOOR!r}) or None"
        )
    cleaned = [str(v) for v in violations if str(v).strip()]
    if not cleaned:
        return VerificationGate(
            status=CLEAR,
            confidence=100,
            violations=[],
        )
    if severity_hint == SEVERITY_FLOOR:
        confidence = CONFIDENCE_FLOOR
    else:
        confidence = max(
            CONFIDENCE_FLOOR,
            100 - PENALTY_PER_VIOLATION * len(cleaned),
        )
    return VerificationGate(
        status=REQUIRES_HUMAN_VERIFICATION,
        confidence=confidence,
        violations=cleaned,
    )
